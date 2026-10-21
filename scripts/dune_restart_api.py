#!/usr/bin/env python3
"""Systemd socket-activated, map-only host restart API for the admin panel."""

from __future__ import annotations

import ast
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
from concurrent.futures import ThreadPoolExecutor


ROOT = pathlib.Path(__file__).resolve().parents[1]
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
    ("shutdown", "stop"),
    ("shutdown", "shutdown"),
}
SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
JOB_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{0,128}\Z")
RESTART_LOCK = threading.Lock()


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
        result = subprocess.run(
            [str(RESTART_SCRIPT), request["target"]], cwd=ROOT,
            env=restart_environment(request), capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS, check=False,
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[-MAX_OUTPUT_CHARS:]
        return {
            "ok": result.returncode == 0,
            "exitCode": result.returncode,
            "output": output,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exitCode": 124, "error": "host restart exceeded its time limit", "output": ""}
    except OSError as exc:
        return {"ok": False, "exitCode": 1, "error": f"host restart could not run ({type(exc).__name__})", "output": ""}
    finally:
        RESTART_LOCK.release()


def send_response(conn: socket.socket, status: int, result: dict) -> None:
    body = json.dumps(result, separators=(",", ":")).encode("utf-8")
    reasons = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 503: "Service Unavailable"}
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
        _pid, peer_uid, _gid = struct.unpack("3i", peer)
        if peer_uid != 0:
            send_response(conn, 403, {"ok": False, "error": "restart API is restricted to the admin container"})
            return
        reader = conn.makefile("rb")
        request_line = reader.readline(4097)
        if len(request_line) > 4096 or request_line not in (b"POST /v1/restart HTTP/1.1\r\n", b"POST /v1/restart HTTP/1.0\r\n"):
            send_response(conn, 404, {"ok": False, "error": "unknown restart API route"})
            return
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
            headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in headers:
            send_response(conn, 400, {"ok": False, "error": "transfer-encoded requests are not accepted"})
            return
        try:
            length = int(headers.get("content-length", "-1"))
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
        try:
            payload = json.loads(raw.decode("utf-8"))
            request = validate_request(payload, configured_map_services())
        except RequestError as exc:
            send_response(conn, exc.status, {"ok": False, "error": str(exc)})
            return
        except (UnicodeDecodeError, json.JSONDecodeError):
            send_response(conn, 400, {"ok": False, "error": "request body is not valid JSON"})
            return
        except (OSError, RuntimeError, subprocess.SubprocessError):
            send_response(conn, 503, {"ok": False, "error": "the configured game-map roster could not be verified"})
            return
        result = execute_restart(request)
        send_response(conn, 200 if result.get("ok") else 409 if result.get("exitCode") == 75 else 503, result)
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
    pool.shutdown(wait=False, cancel_futures=True)
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
