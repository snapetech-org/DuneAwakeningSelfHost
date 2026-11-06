#!/usr/bin/env python3
"""Systemd socket-activated, host restart and guarded recovery API for the admin panel."""

from __future__ import annotations

import ast
import fcntl
import json
import os
import pathlib
import pwd
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))
sys.path.insert(0, str(ROOT / "scripts"))
import host_service_control
ENV_FILE = pathlib.Path(os.environ.get("DUNE_RESTART_API_ENV_FILE", str(ROOT / ".env"))).resolve()
RESTART_SCRIPT = ROOT / "scripts" / "restart-target.sh"
MAX_REQUEST_BYTES = 16 * 1024
MAX_OUTPUT_CHARS = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 1800
MAX_WORKERS = 4
VALID_ACTION_PHASES = {
    ("restart", "start"),
    ("restart", "restart"),
    ("restart", "stop"),
    ("restart", "check-current-build"),
    ("shutdown", "stop"),
    ("shutdown", "shutdown"),
}
SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
JOB_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{0,128}\Z")
RESTART_LOCK = threading.Lock()
CONTROL_STATE_LOCK = threading.Lock()
CONTROL_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guarded-service-control")
CONTROL_STATE = pathlib.Path(os.environ.get("DUNE_RESTART_API_STATE_DIR", "/var/lib/dune-restart-api"))
CONTROL_ACTIVE = {"jobId": None}


def peer_allowed(pid, uid):
    """A container peer must be the actual project Admin container, not just root."""
    try:
        group = pathlib.Path(f"/proc/{int(pid)}/cgroup").read_text()
        match = re.search(r"(?:^|/)(?:docker-)?([0-9a-f]{64})(?:\.scope)?(?:/|$)", group, re.M)
        if match:
            admission = host_service_control.Admission(ROOT, ENV_FILE, os.environ.get("DUNE_RESTART_API_EXPECTED_HOSTNAME", ""))
            project = admission.require_policy()["project"]
            result = subprocess.run(
                ["docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}', match[1]],
                text=True, capture_output=True, timeout=5, check=False,
            )
            return result.returncode == 0 and result.stdout.strip() == project + "|admin-panel"
        if any(name in group for name in ("docker", "libpod", "containerd", "kubepods")):
            return False
        return uid in {0, os.getuid()}
    except (OSError, ValueError, KeyError, host_service_control.Blocked, subprocess.SubprocessError):
        return False


def control_health():
    try:
        expected = os.environ.get("DUNE_RESTART_API_EXPECTED_HOSTNAME", "")
        admission = host_service_control.Admission(ROOT, ENV_FILE, expected)
        admission.require_policy()
        values = admission.values
        available = bool(expected and socket.gethostname().split(".")[0] == expected)
        return {
            "ok": True, "protocol": host_service_control.PROTOCOL,
            "serviceControlReady": available and host_service_control.enabled(values.get("DUNE_ADMIN_SERVICE_CONTROL_ENABLED")),
            "statefulControlReady": available and host_service_control.enabled(values.get("DUNE_ADMIN_STATEFUL_SERVICE_CONTROL_ENABLED")),
            "guards": list(host_service_control.GUARDS),
            "busy": RESTART_LOCK.locked() or CONTROL_ACTIVE["jobId"] is not None,
        }
    except (OSError, ValueError, KeyError, host_service_control.Blocked):
        return {"ok": False, "protocol": host_service_control.PROTOCOL, "serviceControlReady": False, "statefulControlReady": False}


def save_control_job(job):
    CONTROL_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONTROL_STATE, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".job-", dir=CONTROL_STATE)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(job, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONTROL_STATE / (job["jobId"] + ".json"))
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def load_control_job(identifier):
    if not host_service_control.JOB_PATTERN.fullmatch(identifier):
        raise RequestError(400, "service-control job identifier is invalid")
    path = CONTROL_STATE / (identifier + ".json")
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 32768:
        raise RequestError(503, "service-control record is unsafe")
    result = json.loads(path.read_text())
    if result.get("jobId") != identifier:
        raise RequestError(503, "service-control record identity is invalid")
    return result


def execute_control(job):
    lock = None
    acquired = False
    started = time.monotonic()
    try:
        acquired = RESTART_LOCK.acquire(timeout=5)
        if not acquired:
            raise host_service_control.Blocked("another-native-restart-is-running")
        admission = host_service_control.Admission(ROOT, ENV_FILE, os.environ.get("DUNE_RESTART_API_EXPECTED_HOSTNAME", ""))
        admission.require_policy()
        admission.require_gate(job["service"])
        lock_path = ROOT / "backups/admin-panel/operation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise host_service_control.Blocked("backup-or-maintenance-is-running") from None
        job.update(state="checking", ok=False)
        save_control_job(job)
        admission.players()
        receipt = admission.readiness()
        original_image = admission.same_image(job["service"])
        phase = "stop" if job["action"] == "stop" else job["action"]
        native = {"target": job["service"], "services": [job["service"]], "action": "shutdown" if phase == "stop" else "restart",
                  "phase": phase, "dryRun": False, "fastDynamicStart": False, "jobId": job["jobId"]}
        environment = admission.approved_environment(restart_environment(native))
        environment["DUNE_OPERATION_LOCK_HELD"] = "true"
        environment["DUNE_RESTART_ALLOW_STATEFUL"] = "true" if job["service"] in host_service_control.STATEFUL else "false"
        job["state"] = "backing-up"
        save_control_job(job)
        job["backupId"] = admission.backup(environment)
        job["state"] = "rechecking"
        save_control_job(job)
        admission.unchanged_environment()
        admission.players()
        if receipt != admission.readiness() or original_image != admission.same_image(job["service"]):
            raise host_service_control.Blocked("admission-changed-during-backup")
        job["state"] = "executing"
        save_control_job(job)
        result = admission.recover_existing(job["service"], job["action"], environment)
        job.update(result, state="completed" if result["ok"] else "failed")
    except host_service_control.Blocked as exc:
        job.update(state="blocked", ok=False, error=str(exc))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        job.update(state="failed", ok=False, error="native-service-control-failed")
    finally:
        if lock is not None:
            lock.close()
        if acquired:
            RESTART_LOCK.release()
        job["durationMs"] = int((time.monotonic() - started) * 1000)
        save_control_job(job)
        with CONTROL_STATE_LOCK:
            CONTROL_ACTIVE["jobId"] = None


def submit_control(payload):
    request = host_service_control.validate_request(payload, configured_map_services())
    with CONTROL_STATE_LOCK:
        previous = load_control_job(request["jobId"])
        if previous is not None:
            if any(previous.get(key) != request[key] for key in ("service", "action")):
                raise RequestError(409, "service-control job identifier already has another operation")
            return previous
        if CONTROL_ACTIVE["jobId"] is not None:
            raise RequestError(409, "another service-control operation is running")
        # An interrupted operation is evidence to inspect, never an instruction to retry.
        if CONTROL_STATE.exists():
            for path in CONTROL_STATE.glob("*.json"):
                if host_service_control.JOB_PATTERN.fullmatch(path.stem):
                    prior = load_control_job(path.stem)
                    if prior and prior.get("state") not in {"completed", "failed", "blocked"}:
                        raise RequestError(409, "an interrupted native operation requires review")
        request.update(state="queued", ok=False)
        save_control_job(request)
        CONTROL_ACTIVE["jobId"] = request["jobId"]
        CONTROL_POOL.submit(execute_control, dict(request))
        return request


class RequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def configured_map_services() -> set[str]:
    if not ENV_FILE.is_file():
        raise RuntimeError("the host Dune environment file is unavailable")
    admin_source = (ROOT / "admin" / "admin_panel.py").read_text(encoding="utf-8")
    tree = ast.parse(admin_source, filename="admin/admin_panel.py")
    roster = None
    roster_kind = ""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if "FALLBACK_GAME_MAPS" in names:
            roster = ast.literal_eval(node.value)
            roster_kind = "tuples"
            break
        if "GAME_MAP_SERVICES" in names:
            roster = ast.literal_eval(node.value)
            roster_kind = "services"
            break
    if not isinstance(roster, list) or not roster:
        raise RuntimeError("the admin panel game-map roster is unavailable")
    if roster_kind == "tuples":
        services = {str(row[0]) for row in roster if isinstance(row, (list, tuple)) and len(row) == 2}
    else:
        services = {str(service) for service in roster if isinstance(service, str)}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("DUNE_WORLD_PARTITION_COUNT="):
            raw_count = line.split("=", 1)[1].strip().strip("\"'")
            if not raw_count.isdigit():
                raise RuntimeError("the configured world partition count is invalid")
            if int(raw_count) >= 31:
                services.add("deep-desert-pvp")
            else:
                services.discard("deep-desert-pvp")
            break
    if not services or any(not SERVICE_RE.fullmatch(service) for service in services):
        raise RuntimeError("the configured game-map service roster is invalid")
    return services


def validate_request(payload: object, map_services: set[str]) -> dict:
    expected_keys = {"target", "services", "action", "phase", "dryRun", "fastDynamicStart", "jobId"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise RequestError(400, "restart request fields are invalid")
    target = payload["target"]
    if not isinstance(target, str) or target != "all" and not SERVICE_RE.fullmatch(target):
        raise RequestError(400, "restart target is invalid")
    if target != "all" and target not in map_services:
        raise RequestError(403, "target is not a configured game-map service")
    services = payload["services"]
    if not isinstance(services, list) or any(not isinstance(service, str) or not SERVICE_RE.fullmatch(service) for service in services):
        raise RequestError(400, "restart service list is invalid")
    if len(services) != len(set(services)) or any(service not in map_services for service in services):
        raise RequestError(403, "restart service list is outside the game-map roster")
    if target != "all" and services != [target]:
        raise RequestError(403, "a single-map request must name only its target service")
    action = payload["action"]
    phase = payload["phase"]
    if (action, phase) not in VALID_ACTION_PHASES:
        raise RequestError(403, "restart action or phase is not allowed")
    if phase == "check-current-build" and (target != "all" or services or payload["dryRun"] or payload["fastDynamicStart"]):
        raise RequestError(403, "build checks require the all-farm target without services or lifecycle flags")
    if not isinstance(payload["dryRun"], bool) or not isinstance(payload["fastDynamicStart"], bool):
        raise RequestError(400, "restart flags must be booleans")
    job_id = payload["jobId"]
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise RequestError(400, "restart job id is invalid")
    return {
        "target": target,
        "services": services,
        "action": action,
        "phase": phase,
        "dryRun": payload["dryRun"],
        "fastDynamicStart": payload["fastDynamicStart"],
        "jobId": job_id,
    }


def restart_environment(request: dict) -> dict[str, str]:
    account = pwd.getpwuid(os.getuid())
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "ENV_FILE": str(ENV_FILE),
        "DUNE_RESTART_TARGET": request["target"],
        "DUNE_RESTART_SERVICES": " ".join(request["services"]),
        "DUNE_RESTART_ACTION": request["action"],
        "DUNE_RESTART_PHASE": request["phase"],
        "DUNE_RESTART_DRY_RUN": "true" if request["dryRun"] else "false",
        "DUNE_RESTART_FAST_DYNAMIC_START": "true" if request["fastDynamicStart"] else "false",
        "DUNE_RESTART_JOB_ID": request["jobId"],
        "DUNE_RESTART_CHECK_STEAM_UPDATE": "false",
        "DUNE_RESTART_USE_HOST_COMPOSE": "false",
    }
    if "LC_ALL" in os.environ:
        env["LC_ALL"] = os.environ["LC_ALL"]
    return env


def execute_restart(request: dict) -> dict:
    if not RESTART_SCRIPT.is_file() or not os.access(RESTART_SCRIPT, os.X_OK):
        return {"ok": False, "exitCode": 78, "error": "the trusted host restart script is unavailable", "output": ""}
    if not RESTART_LOCK.acquire(timeout=5):
        return {"ok": False, "exitCode": 75, "error": "another host restart is already running", "output": ""}
    started = time.monotonic()
    try:
        admission = host_service_control.Admission(ROOT, ENV_FILE, os.environ.get("DUNE_RESTART_API_EXPECTED_HOSTNAME", ""))
        policy = admission.require_policy()
        if not admission.expected_hostname or socket.gethostname().split(".")[0] != admission.expected_hostname:
            raise host_service_control.Blocked("host-identity-not-approved")
        if request["phase"] == "check-current-build":
            result = subprocess.run(
                [str(ROOT / "scripts/check-steam-update.sh"), str(ENV_FILE)],
                cwd=ROOT, env=restart_environment(request), capture_output=True,
                text=True, timeout=90, check=False,
            )
            return {"ok": result.returncode == 0, "exitCode": result.returncode,
                    "output": (result.stdout + result.stderr).strip()[-MAX_OUTPUT_CHARS:],
                    "durationMs": int((time.monotonic() - started) * 1000)}
        environment = admission.approved_environment(restart_environment(request))
        services = ([*sorted(configured_map_services()), *sorted(host_service_control.CONTROL)]
                    if request["target"] == "all" else request["services"])
        # Admit the entire batch before touching any existing container. New
        # containers and image/configuration changes require the native installer.
        for service in services:
            admission.same_image(service)
        action = "stop" if request["phase"] in {"stop", "shutdown"} else request["phase"]
        if not request["dryRun"] and action in {"start", "restart"}:
            admission.run([str(ROOT / "scripts/validate-landsraad-coriolis-cycle.sh"), environment["ENV_FILE"]], env=environment)
        for service in services:
            admission.unchanged_environment()
            admission.recover_existing(service, action, environment, dry_run=request["dryRun"], post_hooks=False)
        if not request["dryRun"] and action in {"start", "restart"}:
            admission.post_start_hooks(environment)
        return {"ok": True, "exitCode": 0, "output": "Existing approved containers processed.",
                "durationMs": int((time.monotonic() - started) * 1000)}
    except (host_service_control.Blocked, ValueError, KeyError):
        return {"ok": False, "exitCode": 78, "error": "host-approved existing-container recovery admission failed", "output": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exitCode": 124, "error": "host restart exceeded its time limit", "output": ""}
    except OSError as exc:
        return {"ok": False, "exitCode": 1, "error": f"host restart could not run ({type(exc).__name__})", "output": ""}
    finally:
        RESTART_LOCK.release()


def send_response(conn: socket.socket, status: int, result: dict) -> None:
    body = json.dumps(result, separators=(",", ":")).encode("utf-8")
    reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 503: "Service Unavailable"}
    headers = (
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")
    try:
        conn.sendall(headers + body)
    except OSError:
        pass


def handle_connection(conn: socket.socket) -> None:
    conn.settimeout(10)
    try:
        peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, peer_uid, _gid = struct.unpack("3i", peer)
        if not peer_allowed(pid, peer_uid):
            send_response(conn, 403, {"ok": False, "error": "restart API peer is not authorized"})
            return
        reader = conn.makefile("rb")
        request_line = reader.readline(4097)
        match = re.fullmatch(rb"(GET|POST) (/v1/[a-z0-9/-]+) HTTP/1\.[01]\r\n", request_line)
        if len(request_line) > 4096 or not match:
            send_response(conn, 404, {"ok": False, "error": "unknown restart API route"})
            return
        method, route = (value.decode("ascii") for value in match.groups())
        headers = {}
        for _ in range(32):
            line = reader.readline(4097)
            if not line or len(line) > 4096:
                send_response(conn, 400, {"ok": False, "error": "invalid request headers"})
                return
            if line in (b"\r\n", b"\n"):
                break
            name, separator, value = line.decode("latin-1").partition(":")
            if not separator:
                send_response(conn, 400, {"ok": False, "error": "invalid request headers"})
                return
            name = name.strip().lower()
            if name in headers:
                raise RequestError(400, "duplicate request header")
            headers[name] = value.strip()
        else:
            raise RequestError(400, "too many request headers")
        if "transfer-encoding" in headers:
            send_response(conn, 400, {"ok": False, "error": "transfer-encoded requests are not accepted"})
            return
        try:
            length = int(headers.get("content-length", "0" if method == "GET" else "-1"))
        except ValueError:
            length = -1
        if length < 0:
            send_response(conn, 400, {"ok": False, "error": "content length is required"})
            return
        if length > MAX_REQUEST_BYTES:
            send_response(conn, 413, {"ok": False, "error": "request body exceeds the size limit"})
            return
        raw = reader.read(length)
        if len(raw) != length:
            send_response(conn, 400, {"ok": False, "error": "request body is truncated"})
            return
        if method == "GET":
            if length:
                raise RequestError(400, "GET request body is not accepted")
            if route == "/v1/health":
                send_response(conn, 200, control_health())
            elif route.startswith("/v1/service-control/"):
                record = load_control_job(route.removeprefix("/v1/service-control/"))
                send_response(conn, 200 if record else 404, record or {"ok": False, "error": "job not found"})
            else:
                raise RequestError(404, "unknown restart API route")
            return
        if route not in {"/v1/restart", "/v1/service-control"}:
            raise RequestError(404, "unknown restart API route")
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
            if route == "/v1/service-control":
                result = submit_control(payload)
                send_response(conn, 202, result)
                return
            request = validate_request(payload, configured_map_services())
        except RequestError:
            raise
        except (UnicodeDecodeError, ValueError):
            raise RequestError(400, "request body or fields are invalid") from None
        except (OSError, RuntimeError, subprocess.SubprocessError):
            raise RequestError(503, "the configured game-map roster could not be verified") from None
        result = execute_restart(request)
        send_response(conn, 200 if result.get("ok") else 409 if result.get("exitCode") == 75 else 503, result)
    except RequestError as exc:
        send_response(conn, exc.status, {"ok": False, "error": str(exc)})
    except (OSError, TimeoutError):
        send_response(conn, 400, {"ok": False, "error": "request connection failed"})
    finally:
        conn.close()


def serve(listener: socket.socket) -> None:
    listener.listen(32)
    listener.settimeout(1)
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="dune-restart-api")
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            if stopping.is_set():
                break
            raise
        pool.submit(handle_connection, conn)
    pool.shutdown(wait=True, cancel_futures=True)
    CONTROL_POOL.shutdown(wait=True)
    listener.close()


def main() -> int:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        print("dune restart API requires one systemd-activated listening socket", file=sys.stderr)
        return 78
    listener = socket.socket(fileno=3)
    if listener.family != socket.AF_UNIX or listener.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
        print("dune restart API received an unexpected socket", file=sys.stderr)
        return 78
    serve(listener)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
