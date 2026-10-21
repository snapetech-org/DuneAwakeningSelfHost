#!/usr/bin/env python3
"""Narrow Docker Engine API broker for the Dune admin panel.

Only this process mounts the host Docker socket. Client containers connect to
the broker's Unix socket and receive project-scoped inspection, bounded
service control, the approved RabbitMQ commands, and isolated restore drills.
Generic Docker API access is denied.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import stat
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer


PROJECT = os.environ.get("DUNE_DOCKER_API_PROXY_PROJECT", "dune_server")
HOST_DOCKER_SOCKET = os.environ.get("DUNE_DOCKER_API_PROXY_UPSTREAM", "/var/run/docker.sock")
RESTORE_HOST_ROOT = os.environ.get("DUNE_DOCKER_API_PROXY_RESTORE_HOST_ROOT", "")
RESTORE_VISIBLE_ROOT = os.environ.get("DUNE_DOCKER_API_PROXY_RESTORE_VISIBLE_ROOT", "/restore-backups")
ADMIN_SOCKET = os.environ.get("DUNE_DOCKER_API_PROXY_ADMIN_SOCKET", "/run/dune-docker-api/docker.sock")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_STATS_IN_FLIGHT = 8

ID_RE = re.compile(r"[a-fA-F0-9]{12,64}\Z")
PG_NAME_RE = re.compile(r"dash-restore-drill-[a-f0-9]{12}\Z")
RMQ_NAME_RE = re.compile(r"dash-rmq-restore-drill-[A-Za-z0-9_.-]{1,64}-[a-f0-9]{10}\Z")
RMQ_IMAGE_RE = re.compile(
    r"registry\.funcom\.com/funcom/self-hosting/seabass-server-rabbitmq:[A-Za-z0-9._-]{1,128}\Z"
)
RMQ_EVAL_RE = re.compile(
    r'^Outer = base64:decode\(<<"([A-Za-z0-9+/]*={0,2})">>\), '
    r'XName = rabbit_misc:r\(<<"/">>, exchange, <<"heartbeats">>\), '
    r'X = rabbit_exchange:lookup_or_die\(XName\), '
    r'MsgId = list_to_binary\("dash-([a-z0-9-]{1,48})-" \+\+ integer_to_list\(erlang:system_time\(millisecond\)\)\), '
    r'P = \{list_to_atom\("P_basic"\), <<"Content">>, undefined, \[\], undefined, undefined, undefined, undefined, undefined, MsgId, undefined, undefined, <<"fls">>, <<"fls_backend">>, undefined\}, '
    r'Content = rabbit_basic:build_content\(P, Outer\), '
    r'\{ok, Msg\} = rabbit_basic:message\(XName, <<"notifications">>, Content\), '
    r'Result = rabbit_queue_type:publish_at_most_once\(X, Msg\), '
    r'io:format\("publish=~p exchange=heartbeats routing=notifications app_id=fls_backend user_id=fls~n", \[Result\]\)\.$'
)

DRILL_LABELS = {
    "pg": ("com.dash.restore-drill", "true"),
    "rabbitmq": ("com.dash.rabbitmq-restore-drill", "true"),
}
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
PROTECTED_SERVICES = {
    "admin-panel", "admin-panel-ingress", "admin-chat-commands",
    "docker-api-broker", "postgres", "admin-rmq", "game-rmq",
    "prometheus", "node-exporter", "cadvisor",
}


class ProxyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def upstream_request(method: str, path: str, body: bytes = b"", *, timeout: float = 10,
                     max_bytes: int = MAX_RESPONSE_BYTES):
    conn = UnixHTTPConnection(HOST_DOCKER_SOCKET, timeout)
    try:
        headers = {"Host": "docker", "Connection": "close"}
        if body:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body or None, headers=headers)
        response = conn.getresponse()
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ProxyError(502, "Docker response exceeded the broker size limit")
        return response.status, dict(response.getheaders()), data
    finally:
        conn.close()


def decode_json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProxyError(502, "Docker returned invalid JSON")


def project_container(identifier: str):
    if not ID_RE.fullmatch(identifier):
        raise ProxyError(400, "invalid container id")
    status, _headers, body = upstream_request("GET", f"/containers/{identifier}/json", timeout=8, max_bytes=4 * 1024 * 1024)
    if status != 200:
        raise ProxyError(status, "container inspection failed")
    inspected = decode_json(body)
    labels = ((inspected.get("Config") or {}).get("Labels") or {})
    if labels.get(PROJECT_LABEL) == PROJECT:
        return inspected, "project"
    role = safe_drill_role(inspected)
    if role:
        return inspected, role
    raise ProxyError(403, "container is outside the allowed Dune project")


def _restore_source_allowed(source: str) -> bool:
    host_root = os.path.normpath(RESTORE_HOST_ROOT)
    source = os.path.normpath(source)
    if not os.path.isabs(host_root) or not os.path.isabs(source):
        return False
    try:
        if os.path.commonpath((host_root, source)) != host_root or source == host_root:
            return False
        relative = os.path.relpath(source, host_root)
        visible_root = os.path.realpath(RESTORE_VISIBLE_ROOT)
        visible_source = os.path.realpath(os.path.join(visible_root, relative))
        return (
            os.path.commonpath((visible_root, visible_source)) == visible_root
            and os.path.exists(visible_source)
        )
    except (OSError, ValueError):
        return False


def _user_pair(value):
    match = re.fullmatch(r"([0-9]+):([0-9]+)", str(value or ""))
    if not match:
        return None
    uid, gid = int(match.group(1)), int(match.group(2))
    return (uid, gid) if uid > 0 and gid >= 0 else None


def _env_map(rows):
    if not isinstance(rows, list) or any(not isinstance(row, str) or "=" not in row for row in rows):
        return None
    result = {}
    for row in rows:
        key, value = row.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in result:
            return None
        result[key] = value
    return result


def _bounded_host_config(host, role, uid, gid):
    allowed_keys = {
        "NetworkMode", "ReadonlyRootfs", "CapDrop", "SecurityOpt", "PidsLimit",
        "NanoCpus", "Memory", "MemorySwap", "RestartPolicy", "Mounts", "Tmpfs",
    }
    if not isinstance(host, dict) or set(host) - allowed_keys:
        return False
    if host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True:
        return False
    if host.get("CapDrop") != ["ALL"] or host.get("SecurityOpt") != ["no-new-privileges:true"]:
        return False
    if not isinstance(host.get("RestartPolicy"), dict) or host["RestartPolicy"] != {"Name": "no", "MaximumRetryCount": 0}:
        return False
    try:
        memory = int(host.get("Memory", 0))
        memory_swap = int(host.get("MemorySwap", 0))
        cpus = int(host.get("NanoCpus", 0))
        pids = int(host.get("PidsLimit", 0))
    except (TypeError, ValueError):
        return False
    if not (256 * 1024**2 <= memory <= 2 * 1024**3 and memory_swap == memory):
        return False
    if not (1 <= cpus <= 2_000_000_000 and 1 <= pids <= 256):
        return False

    tmpfs = host.get("Tmpfs")
    allowed_tmpfs = ({"/var/lib/postgresql/data", "/var/run/postgresql", "/tmp"}
                     if role == "pg" else {"/tmp", "/var/log/rabbitmq"})
    if not isinstance(tmpfs, dict) or not set(tmpfs) or set(tmpfs) - allowed_tmpfs:
        return False
    tmpfs_limits = {
        "/var/lib/postgresql/data": 2 * 1024**3,
        "/var/run/postgresql": 16 * 1024**2,
        "/tmp": 256 * 1024**2,
        "/var/log/rabbitmq": 64 * 1024**2,
    }
    for path, value in tmpfs.items():
        options = str(value).split(",")
        if not {"rw", "noexec", "nosuid"}.issubset(options):
            return False
        parsed = {}
        for option in options:
            if "=" in option:
                key, val = option.split("=", 1)
                if key in parsed:
                    return False
                parsed[key] = val
            elif option not in {"rw", "noexec", "nosuid"}:
                return False
        if set(parsed) != {"size", "uid", "gid", "mode"}:
            return False
        try:
            if int(parsed["size"]) > tmpfs_limits[path] or int(parsed["uid"]) != uid or int(parsed["gid"]) != gid:
                return False
            if int(parsed["mode"], 8) & 0o700 != 0o700:
                return False
        except ValueError:
            return False

    mounts = host.get("Mounts")
    if not isinstance(mounts, list) or not mounts:
        return False
    targets = set()
    pg_targets = {"/drill/source.dump", "/etc/passwd", "/etc/group"}
    rmq_required = {
        "/var/lib/rabbitmq", "/etc/rabbitmq/conf.d/99-dune.conf",
        "/etc/rabbitmq/enabled_plugins", "/etc/passwd", "/etc/group",
    }
    rmq_optional = {"/etc/rabbitmq/cacert.pem", "/etc/rabbitmq/cert.pem", "/etc/rabbitmq/key.pem"}
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) != {"Type", "Source", "Target", "ReadOnly", "BindOptions"}:
            return False
        target = mount.get("Target")
        if target in targets or mount.get("Type") != "bind" or not _restore_source_allowed(str(mount.get("Source") or "")):
            return False
        if mount.get("BindOptions") != {"Propagation": "rprivate"}:
            return False
        if role == "pg":
            if target not in pg_targets or mount.get("ReadOnly") is not True:
                return False
        else:
            if target not in rmq_required | rmq_optional:
                return False
            if mount.get("ReadOnly") is not (target != "/var/lib/rabbitmq"):
                return False
        targets.add(target)
    if role == "pg" and targets != pg_targets:
        return False
    if role == "rabbitmq" and (not rmq_required.issubset(targets) or targets - rmq_required - rmq_optional):
        return False
    return True


def validate_restore_create(name: str, spec: dict) -> str | None:
    """Return the safe restore profile, or None if the body exceeds policy."""
    if not isinstance(spec, dict):
        return None
    labels = spec.get("Labels")
    if labels == {DRILL_LABELS["pg"][0]: "true"}:
        role = "pg"
        allowed_top = {"Image", "User", "Env", "Labels", "StopTimeout", "HostConfig"}
        if not PG_NAME_RE.fullmatch(name) or spec.get("Image") != "registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13":
            return None
        expected_env = {
            "POSTGRES_DB": "drill", "POSTGRES_USER": "dune",
            "POSTGRES_HOST_AUTH_METHOD": "trust", "PGDATA": "/var/lib/postgresql/data/pgdata",
        }
    elif labels == {DRILL_LABELS["rabbitmq"][0]: "true"}:
        role = "rabbitmq"
        allowed_top = {"Image", "Hostname", "User", "Env", "Labels", "StopTimeout", "HostConfig"}
        if not RMQ_NAME_RE.fullmatch(name) or not RMQ_IMAGE_RE.fullmatch(str(spec.get("Image") or "")):
            return None
        hostname = str(spec.get("Hostname") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", hostname):
            return None
        expected_env = {
            "HOME": "/var/lib/rabbitmq", "RABBITMQ_NODENAME": f"rabbit@{hostname}",
            "RABBITMQ_USE_LONGNAME": "false", "RABBITMQ_LOGS": "-", "RABBITMQ_SASL_LOGS": "-",
            "RMQ_AUTH_BACKEND_1": "cache", "RMQ_HTTP_TOKEN_AUTH_SECRET": "restore-drill-no-network",
        }
    else:
        return None
    if set(spec) != allowed_top or spec.get("StopTimeout") != 30:
        return None
    pair = _user_pair(spec.get("User"))
    env = _env_map(spec.get("Env"))
    if not pair or env is None:
        return None
    uid, gid = pair
    if role == "pg":
        if env != expected_env:
            return None
    else:
        expected_env["FuncomLiveServices__RmqTlsEnabled"] = env.get("FuncomLiveServices__RmqTlsEnabled", "")
        if expected_env["FuncomLiveServices__RmqTlsEnabled"] not in {"true", "false"} or env != expected_env:
            return None
    return role if _bounded_host_config(spec.get("HostConfig"), role, uid, gid) else None


def safe_drill_role(inspected: dict) -> str | None:
    config = inspected.get("Config") or {}
    labels = config.get("Labels") or {}
    if labels.get(DRILL_LABELS["pg"][0]) == "true":
        role = "pg"
        if config.get("Image") != "registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13":
            return None
    elif labels.get(DRILL_LABELS["rabbitmq"][0]) == "true":
        role = "rabbitmq"
        if not RMQ_IMAGE_RE.fullmatch(str(config.get("Image") or "")):
            return None
    else:
        return None
    pair = _user_pair(config.get("User"))
    host = inspected.get("HostConfig") or {}
    if not pair or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True:
        return None
    if host.get("Privileged") is True or host.get("PidMode") == "host" or host.get("IpcMode") == "host":
        return None
    if "ALL" not in (host.get("CapDrop") or []) or host.get("CapAdd"):
        return None
    if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
        return None
    if host.get("PortBindings"):
        return None
    try:
        if int(host.get("Memory") or 0) > 2 * 1024**3 or int(host.get("PidsLimit") or 0) > 256 or int(host.get("NanoCpus") or 0) > 2_000_000_000:
            return None
    except (TypeError, ValueError):
        return None
    expected_targets = (
        {"/drill/source.dump", "/etc/passwd", "/etc/group"}
        if role == "pg" else {
            "/var/lib/rabbitmq", "/etc/rabbitmq/conf.d/99-dune.conf",
            "/etc/rabbitmq/enabled_plugins", "/etc/passwd", "/etc/group",
        }
    )
    optional_targets = set() if role == "pg" else {"/etc/rabbitmq/cacert.pem", "/etc/rabbitmq/cert.pem", "/etc/rabbitmq/key.pem"}
    mounts = inspected.get("Mounts") or []
    seen = set()
    for mount in mounts:
        target = mount.get("Destination")
        if target in seen or target not in expected_targets | optional_targets or mount.get("Type") != "bind":
            return None
        if not _restore_source_allowed(str(mount.get("Source") or "")):
            return None
        if role == "pg" and mount.get("RW") is not False:
            return None
        if role == "rabbitmq" and bool(mount.get("RW")) != (target == "/var/lib/rabbitmq"):
            return None
        seen.add(target)
    if not expected_targets.issubset(seen):
        return None
    return role


def allowed_rabbitmq_command(command) -> bool:
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or len(x) > 65536 or "\x00" in x for x in command):
        return False
    if command == ["rabbitmqctl", "-q", "list_queues", "name", "consumers", "messages", "state"]:
        return True
    if len(command) == 4 and command[:3] == ["rabbitmqctl", "-q", "delete_queue"]:
        return bool(re.fullmatch(r"[A-Za-z0-9_+.#-]{1,200}_queue", command[3]))
    return len(command) == 3 and command[:2] == ["rabbitmqctl", "eval"] and bool(RMQ_EVAL_RE.fullmatch(command[2]))


def allowed_drill_command(command) -> bool:
    if not isinstance(command, list) or not command or len(command) > 64:
        return False
    if any(not isinstance(x, str) or len(x) > 65536 or "\x00" in x for x in command):
        return False
    # These are utilities used by the restore workflows. The container must
    # independently pass the no-network, no-capability, read-only-root policy.
    return command[0] in {
        "pg_isready", "sha256sum", "stat", "pg_restore", "psql", "vacuumdb",
        "pg_dump", "rabbitmq-diagnostics", "rabbitmqctl",
    }


class ProxyServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
    max_workers = 48

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._request_slots = threading.BoundedSemaphore(self.max_workers)
        self._stats_slots = threading.BoundedSemaphore(MAX_STATS_IN_FLIGHT)
        self.exec_ids = {}
        self.exec_lock = threading.Lock()
        parent = os.path.dirname(socket_path)
        os.makedirs(parent, mode=0o755, exist_ok=True)
        try:
            existing = os.lstat(socket_path)
            if not stat.S_ISSOCK(existing.st_mode):
                raise RuntimeError(f"refusing to replace non-socket path: {socket_path}")
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        super().__init__(socket_path, ProxyHandler)
        os.chmod(socket_path, 0o666)

    def process_request(self, request, client_address):
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler diagnostics can include request paths and query strings.
        return

    def log_request(self, code="-", size="-"):
        # Keep useful status telemetry without writing IDs, paths, or query strings.
        route = getattr(self, "route_name", "request")
        started = getattr(self, "request_started_at", time.monotonic())
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(f"docker-api-proxy method={self.command} route={route} status={code} elapsed_ms={elapsed_ms}", flush=True)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PUT(self):
        self._error(405, "method not allowed")

    def do_PATCH(self):
        self._error(405, "method not allowed")

    def _read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ProxyError(400, "transfer-encoded requests are not accepted")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ProxyError(400, "invalid content length")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ProxyError(413, "request body exceeds the broker size limit")
        return self.rfile.read(length) if length else b""

    def _dispatch(self):
        self.close_connection = True
        self.request_started_at = time.monotonic()
        stats_slot_acquired = False
        try:
            body = self._read_body()
            split = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(split.path)
            version = re.fullmatch(r"/v[0-9]+\.[0-9]+(/.*|)", path)
            if version:
                path = version.group(1) or "/"
            if "%" in path or "\\" in path or ".." in path.split("/"):
                raise ProxyError(400, "invalid Docker API path")
            self.route_name = self._route_name(path)
            print(f"docker-api-proxy method={self.command} route={self.route_name} phase=start", flush=True)
            if self.command == "GET" and self.route_name == "container-stats":
                stats_slot_acquired = self.server._stats_slots.acquire(blocking=False)
                if not stats_slot_acquired:
                    raise ProxyError(429, "container stats requests are temporarily rate-limited")
            query = urllib.parse.parse_qs(split.query, keep_blank_values=True, strict_parsing=False)
            method = self.command
            upstream_path, timeout, max_bytes, action = self._authorize(method, path, query, body)
            status, headers, response = upstream_request(method, upstream_path, body, timeout=timeout, max_bytes=max_bytes)
            content_type = headers.get("Content-Type", "application/json")
            if status == 200 and action == "sanitize-info":
                info = decode_json(response)
                response = json.dumps({
                    "Name": info.get("Name"),
                    "ServerVersion": info.get("ServerVersion"),
                }, separators=(",", ":")).encode("utf-8")
                content_type = "application/json"
            elif status == 200 and action == "filter-list":
                rows = decode_json(response)
                if not isinstance(rows, list):
                    raise ProxyError(502, "Docker returned an invalid container list")
                rows = [row for row in rows if self._listed_row_allowed(row)]
                response = json.dumps(rows, separators=(",", ":")).encode("utf-8")
                content_type = "application/json"
            self._record_result(action, status, response)
            self._send(status, content_type, response)
        except ProxyError as exc:
            self._error(exc.status, str(exc))
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            self._error(502, f"Docker broker upstream failure: {type(exc).__name__}")
        except Exception as exc:
            self._error(500, f"Docker broker internal error: {type(exc).__name__}")
        finally:
            if stats_slot_acquired:
                self.server._stats_slots.release()

    def _authorize(self, method, path, query, body):
        if method == "GET" and path in {"/_ping", "/version"} and not query:
            return path, 5, 1024 * 1024, None
        if method == "GET" and path == "/info" and not query:
            return "/info", 8, 1024 * 1024, "sanitize-info"
        if method == "GET" and path == "/containers/json":
            return self._list_path(query), 10, 8 * 1024 * 1024, "filter-list"

        match = re.fullmatch(r"/containers/([a-fA-F0-9]{12,64})/(json|stats|logs)", path)
        if match and method == "GET":
            identifier, endpoint = match.groups()
            inspected, role = project_container(identifier)
            if endpoint == "json":
                if set(query) - {"size"} or (query.get("size") and query["size"] not in (["0"], ["1"])):
                    raise ProxyError(400, "unsupported inspect query")
                return path, 8, 4 * 1024 * 1024, None
            if endpoint == "stats":
                if role != "project" or set(query) - {"stream"} or query.get("stream") != ["false"]:
                    raise ProxyError(403, "only bounded project stats are available")
                return path, 8, 8 * 1024 * 1024, None
            return self._logs_path(path, query), 15, 4 * 1024 * 1024, None

        match = re.fullmatch(r"/containers/([a-fA-F0-9]{12,64})/(start|stop|restart|wait)", path)
        if match and method == "POST":
            identifier, action_name = match.groups()
            inspected, role = project_container(identifier)
            if action_name == "wait":
                if role not in {"pg", "rabbitmq"} or set(query) - {"condition"} or query.get("condition", ["not-running"]) != ["not-running"]:
                    raise ProxyError(403, "wait is available only for isolated restore drills")
                return path + "?" + urllib.parse.urlencode({"condition": "not-running"}), 1800, 1024 * 1024, None
            if role == "project":
                service = ((inspected.get("Config") or {}).get("Labels") or {}).get(SERVICE_LABEL, "")
                if service in PROTECTED_SERVICES:
                    raise ProxyError(403, "control of this infrastructure service is disabled")
                if action_name == "wait":
                    raise ProxyError(403, "wait is not available for project services")
                if action_name == "start" and query:
                    raise ProxyError(400, "start does not accept query parameters")
                if action_name in {"stop", "restart"}:
                    self._validate_timeout_query(query)
                elif query:
                    raise ProxyError(400, "unsupported control query")
                return path + (("?" + urllib.parse.urlencode({"t": query["t"][0]})) if "t" in query else ""), 310, 1024 * 1024, None
            if role in {"pg", "rabbitmq"} and action_name == "start" and not query:
                return path, 20, 1024 * 1024, None
            raise ProxyError(403, "control is not allowed for this container")

        match = re.fullmatch(r"/containers/([a-fA-F0-9]{12,64})/exec", path)
        if match and method == "POST":
            identifier = match.group(1)
            inspected, role = project_container(identifier)
            spec = decode_json(body)
            if not isinstance(spec, dict) or set(spec) - {"AttachStdin", "AttachStdout", "AttachStderr", "Tty", "Cmd"}:
                raise ProxyError(403, "unsupported Docker exec options")
            if spec.get("AttachStdout") is not True or spec.get("AttachStderr") is not True or spec.get("Tty") is not False or spec.get("AttachStdin", False):
                raise ProxyError(403, "Docker exec must be non-interactive")
            command = spec.get("Cmd")
            labels = ((inspected.get("Config") or {}).get("Labels") or {})
            service = labels.get(SERVICE_LABEL, "")
            if role == "project":
                if service != "game-rmq" or not allowed_rabbitmq_command(command):
                    raise ProxyError(403, "only approved RabbitMQ commands are available")
            elif role in {"pg", "rabbitmq"}:
                if not allowed_drill_command(command):
                    raise ProxyError(403, "command is not allowed in the isolated restore drill")
            else:
                raise ProxyError(403, "Docker exec is unavailable for this container")
            return path, 10, 1024 * 1024, ("record-exec", identifier)

        match = re.fullmatch(r"/exec/([a-fA-F0-9]{12,64})/(start|json)", path)
        if match:
            exec_id, endpoint = match.groups()
            with self.server.exec_lock:
                container_id = self.server.exec_ids.get(exec_id)
            if not container_id:
                raise ProxyError(403, "exec id was not created through this broker")
            _inspected, role = project_container(container_id)
            if role not in {"project", "pg", "rabbitmq"}:
                raise ProxyError(403, "exec target is outside broker policy")
            if endpoint == "json" and method == "GET" and not query:
                return path, 10, 2 * 1024 * 1024, ("finish-exec", exec_id)
            if endpoint == "start" and method == "POST" and not query:
                spec = decode_json(body)
                if spec != {"Detach": False, "Tty": False}:
                    raise ProxyError(403, "Docker exec must run attached without a TTY")
                return path, 1800, 32 * 1024 * 1024, None

        if method == "POST" and path == "/containers/create":
            if set(query) != {"name"} or len(query["name"]) != 1:
                raise ProxyError(403, "only named isolated restore containers may be created")
            name = query["name"][0]
            spec = decode_json(body)
            role = validate_restore_create(name, spec)
            if not role:
                raise ProxyError(403, "container does not match an approved isolated restore profile")
            return path + "?" + urllib.parse.urlencode({"name": name}), 15, 1024 * 1024, ("record-container", role)

        match = re.fullmatch(r"/containers/([a-fA-F0-9]{12,64})", path)
        if match and method == "DELETE":
            identifier = match.group(1)
            _inspected, role = project_container(identifier)
            if role not in {"pg", "rabbitmq"}:
                raise ProxyError(403, "only isolated restore containers may be removed")
            if set(query) - {"force", "v"} or query.get("force") not in (["true"], ["1"]) or query.get("v") not in (["true"], ["1"]):
                raise ProxyError(400, "restore container removal must be explicit")
            return path + "?" + urllib.parse.urlencode({"force": query["force"][0], "v": query["v"][0]}), 20, 1024 * 1024, None

        raise ProxyError(403, "Docker API operation is not allowed by policy")

    def _list_path(self, query):
        if set(query) - {"all", "filters"}:
            raise ProxyError(400, "unsupported container-list query")
        all_values = query.get("all", ["1"])
        if len(all_values) != 1 or all_values[0] not in {"0", "1", "true", "false"}:
            raise ProxyError(400, "invalid container-list all flag")
        filters = {}
        if query.get("filters"):
            if len(query["filters"]) != 1:
                raise ProxyError(400, "invalid container-list filters")
            try:
                filters = json.loads(query["filters"][0])
            except json.JSONDecodeError:
                raise ProxyError(400, "invalid container-list filters")
        if not isinstance(filters, dict) or set(filters) - {"label"}:
            raise ProxyError(400, "unsupported container-list filter")
        labels = filters.get("label", [])
        if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels):
            raise ProxyError(400, "invalid label filter")
        for drill_role, (key, value) in DRILL_LABELS.items():
            if labels == [f"{key}={value}"]:
                chosen = {"label": [f"{key}={value}"]}
                return "/containers/json?" + urllib.parse.urlencode({"all": all_values[0], "filters": json.dumps(chosen, separators=(",", ":"))})
        allowed = []
        for label in labels:
            if label == f"{PROJECT_LABEL}={PROJECT}":
                continue
            if label.startswith(f"{SERVICE_LABEL}=") and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", label.split("=", 1)[1]):
                allowed.append(label)
            else:
                raise ProxyError(403, "container-list filter is outside broker policy")
        chosen = {"label": [f"{PROJECT_LABEL}={PROJECT}", *allowed]}
        return "/containers/json?" + urllib.parse.urlencode({"all": all_values[0], "filters": json.dumps(chosen, separators=(",", ":"))})

    @staticmethod
    def _route_name(path):
        if path in {"/_ping", "/version", "/info"}:
            return {"/_ping": "ping", "/version": "version", "/info": "info"}[path]
        if path == "/containers/json":
            return "container-list"
        if path == "/containers/create":
            return "container-create"
        match = re.fullmatch(r"/containers/[a-fA-F0-9]{12,64}/(json|stats|logs|start|stop|restart|wait|exec)", path)
        if match:
            return f"container-{match.group(1)}"
        match = re.fullmatch(r"/exec/[a-fA-F0-9]{12,64}/(start|json)", path)
        if match:
            return f"exec-{match.group(1)}"
        return "other"

    @staticmethod
    def _logs_path(path, query):
        allowed = {"stdout", "stderr", "timestamps", "tail", "since", "until"}
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise ProxyError(400, "unsupported log query")
        options = {}
        for name, default in (("stdout", "1"), ("stderr", "1"), ("timestamps", "0")):
            value = query.get(name, [default])[0]
            if value not in {"0", "1", "false", "true"}:
                raise ProxyError(400, f"invalid log {name} flag")
            options[name] = value
        tail = query.get("tail", ["1000"])[0]
        if not tail.isdigit() or int(tail) > 10000:
            raise ProxyError(400, "log tail is outside the allowed range")
        options["tail"] = str(max(1, int(tail)))
        options["follow"] = "0"
        for name in ("since", "until"):
            if name in query:
                value = query[name][0]
                if not value.isdigit():
                    raise ProxyError(400, f"invalid log {name}")
                options[name] = value
        return path + "?" + urllib.parse.urlencode(options)

    @staticmethod
    def _listed_row_allowed(row):
        labels = row.get("Labels") or {}
        if labels.get(PROJECT_LABEL) == PROJECT:
            return True
        return any(labels.get(key) == value for key, value in DRILL_LABELS.values())

    @staticmethod
    def _validate_timeout_query(query):
        if set(query) - {"t"} or len(query.get("t", ["30"])) != 1:
            raise ProxyError(400, "unsupported control timeout")
        try:
            timeout = int(query.get("t", ["30"])[0])
        except ValueError:
            raise ProxyError(400, "invalid control timeout")
        if timeout < 1 or timeout > 300:
            raise ProxyError(400, "control timeout is outside the allowed range")

    def _record_result(self, action, status, body):
        if not isinstance(action, tuple) or status not in (200, 201):
            return
        kind, value = action
        if kind == "record-exec":
            try:
                exec_id = str((decode_json(body) or {}).get("Id") or "")
            except ProxyError:
                return
            if ID_RE.fullmatch(exec_id):
                with self.server.exec_lock:
                    self.server.exec_ids[exec_id] = value
        elif kind == "finish-exec":
            try:
                inspected = decode_json(body)
            except ProxyError:
                return
            if inspected.get("Running") is False:
                with self.server.exec_lock:
                    self.server.exec_ids.pop(value, None)

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.wfile.flush()

    def _error(self, status, message):
        body = json.dumps({"message": message}, separators=(",", ":")).encode("utf-8")
        self._send(status, "application/json", body)


def serve(socket_path=ADMIN_SOCKET):
    server = ProxyServer(socket_path)
    print(f"docker-api-proxy listening on {socket_path} for project={PROJECT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        if not os.path.exists(HOST_DOCKER_SOCKET):
            raise SystemExit(1)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            sock.connect(ADMIN_SOCKET)
            sock.sendall(b"GET /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n")
            if b"200 OK" not in sock.recv(4096):
                raise SystemExit(1)
        finally:
            sock.close()
        return
    serve()


if __name__ == "__main__":
    main()
