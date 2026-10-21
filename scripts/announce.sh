#!/bin/sh
set -eu

message="${DUNE_ANNOUNCE_MESSAGE:-${1:-}}"
restart_at="${DUNE_ANNOUNCE_RESTART_AT:-}"
job_id="${DUNE_ANNOUNCE_JOB_ID:-manual}"

if [ -z "$message" ]; then
  printf 'missing DUNE_ANNOUNCE_MESSAGE\n' >&2
  exit 64
fi

if [ "${DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES:-true}" != "false" ] && [ "${DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES:-true}" != "0" ] && [ "$job_id" != "manual" ]; then
  case "$message" in
    "!!! "*" !!!") ;;
    *) message="!!! ${message} !!!" ;;
  esac
fi

python_bin="${DUNE_ANNOUNCE_PYTHON:-python3}"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export DUNE_ANNOUNCE_SCRIPT_DIR="$script_dir"
default_vendor_dir="$script_dir/vendor"
if [ -d /workspace/scripts/vendor ]; then
  default_vendor_dir="/workspace/scripts/vendor"
fi
vendor_dir="${DUNE_ANNOUNCE_PYTHONPATH:-$default_vendor_dir}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${vendor_dir}${PYTHONPATH:+:$PYTHONPATH}"
if ! "$python_bin" -c 'import pika' >/dev/null 2>&1; then
  printf 'missing bundled pika module at %s\n' "$vendor_dir" >&2
  exit 78
fi

"$python_bin" - "$message" "$restart_at" "$job_id" <<'PY'
import base64
import json
import os
import pika
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.environ.get("DUNE_ANNOUNCE_SCRIPT_DIR", os.getcwd()))
from secret_env import read_secret_env
from dune_whisper_route import whisper_queue_for_fls_id, normalize_fls_id


message, restart_at, job_id = sys.argv[1:4]


def read_env_file(path):
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


file_env = {}
configured_env_files = os.environ.get("DUNE_ANNOUNCE_ENV_FILE")
if configured_env_files is None:
    env_paths = ("/workspace/.env", os.path.join(os.getcwd(), ".env"))
else:
    env_paths = tuple(path for path in configured_env_files.split(os.pathsep) if path)
for env_path in env_paths:
    file_env.update(read_env_file(env_path))


def env(name, default=""):
    value = os.environ.get(name)
    env_first = os.environ.get("DUNE_ANNOUNCE_ENV_OVERRIDES_FILE", "").lower() in ("1", "true", "yes", "on")
    if env_first and value is not None and value != "":
        return value
    if name.startswith("DUNE_ANNOUNCE_") and name in file_env:
        return file_env[name]
    if value is not None and value != "":
        return value
    return file_env.get(name, default)


def secret_env(name, default=""):
    if os.environ.get(f"{name}_FILE", "").strip():
        return read_secret_env(name, default)
    return env(name, default)


def env_bool(name, default=False):
    value = env(name, "true" if default else "false").lower()
    return value in ("1", "true", "yes", "on")


def split_csv(value):
    items = []
    for item in value.split(","):
        item = item.strip()
        if item in ("<empty>", "empty", "EMPTY"):
            items.append("")
        elif item:
            items.append(item)
    return items


default_url = "http://game-rmq:15672" if os.path.exists("/workspace/.env") else "http://127.0.0.1:15673"
rmq_url = env("DUNE_ANNOUNCE_GAME_RMQ_MANAGEMENT_URL", default_url).rstrip("/")
sender_user = env("DUNE_ANNOUNCE_CHAT_USER", "A000000000000001")
sender_password = secret_env("DUNE_ANNOUNCE_CHAT_PASSWORD")
sender_funcom_id = env("DUNE_ANNOUNCE_CHAT_FUNCOM_ID", "ADMIN#00001")
sender_name = env("DUNE_ANNOUNCE_CHAT_SPOOF_NAME", "Paul")
exchange = env("DUNE_ANNOUNCE_CHAT_EXCHANGE", "chat.map")
routing_keys = split_csv(env("DUNE_ANNOUNCE_CHAT_ROUTING_KEYS", "<empty>"))
channel_type = env("DUNE_ANNOUNCE_CHAT_CHANNEL", "Map")
user_name_to = env("DUNE_ANNOUNCE_CHAT_USER_NAME_TO", "")
use_spoof_name = env_bool("DUNE_ANNOUNCE_CHAT_USE_SPOOF_NAME", False)
bind_online_queues = env_bool("DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES", True)
allow_management_publish = env_bool("DUNE_ANNOUNCE_ALLOW_MANAGEMENT_PUBLISH", False)
queue_pattern = re.compile(env("DUNE_ANNOUNCE_CHAT_QUEUE_PATTERN", r"^[0-9A-Fa-f]{16}_queue$"))
target_queues = split_csv(env("DUNE_ANNOUNCE_CHAT_TARGET_QUEUES", ""))
target_fls_ids = [normalize_fls_id(item) for item in split_csv(env("DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS", ""))]
target_fls_ids = [item for item in target_fls_ids if item]
if target_fls_ids:
    existing_queues = set(target_queues)
    for fls_id in target_fls_ids:
        queue_name = whisper_queue_for_fls_id(fls_id)
        if queue_name and queue_name not in existing_queues:
            target_queues.append(queue_name)
            existing_queues.add(queue_name)
    if "DUNE_ANNOUNCE_CHAT_ROUTING_KEYS" not in os.environ or routing_keys == [""]:
        routing_keys = target_fls_ids
cleanup_target_bindings = env_bool("DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS", False)
docker_socket = env("DUNE_RESTART_DOCKER_SOCKET", "/var/run/docker.sock")
http_timeout = float(env("DUNE_ANNOUNCE_HTTP_TIMEOUT_SECONDS", "2"))
amqp_host = env("DUNE_ANNOUNCE_HOST_AMQP_HOST", env("DUNE_ANNOUNCE_GAME_RMQ_AMQP_HOST", "172.31.240.1" if os.path.exists("/workspace/.env") else "127.0.0.1"))
amqp_port = int(env("DUNE_ANNOUNCE_HOST_AMQP_PORT", env("DUNE_ANNOUNCE_GAME_RMQ_AMQP_PORT", env("GAME_RMQ_PUBLIC_PORT", "31982"))))
amqp_tls = env_bool("DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS", True)
management_user = env("DUNE_ANNOUNCE_RMQ_USER", sender_user)
management_password = secret_env("DUNE_ANNOUNCE_RMQ_PASSWORD", sender_password)
ensure_account = env_bool("DUNE_ANNOUNCE_CHAT_ENSURE_ACCOUNT", False)
db_host = env("DUNE_ADMIN_DB_HOST", "postgres")
db_port = env("DUNE_ADMIN_DB_PORT", "5432")
db_user = env("DUNE_ADMIN_DB_USER", "dune")
db_password = secret_env("DUNE_ADMIN_DB_PASSWORD") or secret_env("POSTGRES_DUNE_PASSWORD")
db_name = env("DUNE_ADMIN_DB_NAME", "dune")
platform_id = env("DUNE_ANNOUNCE_CHAT_PLATFORM_ID", "PAUL")
platform_name = env("DUNE_ANNOUNCE_CHAT_PLATFORM_NAME", "Paul")

if not routing_keys:
    print("missing DUNE_ANNOUNCE_CHAT_ROUTING_KEYS", file=sys.stderr)
    sys.exit(64)

credentials = base64.b64encode(f"{sender_user}:{sender_password}".encode()).decode()
headers = {
    "Authorization": f"Basic {credentials}",
    "Content-Type": "application/json",
}
vhost = urllib.parse.quote("/", safe="")


def ensure_sender_account():
    if not ensure_account:
        return {"ok": True, "skipped": True}
    if not db_password:
        return {"ok": False, "error": "missing DUNE_ADMIN_DB_PASSWORD or POSTGRES_DUNE_PASSWORD"}
    sql = (
        "select id from dune.login_account("
        ":'account',:'funcom_id',:'platform_id',:'platform_name',0,:'character_name',0,0"
        ") limit 1;"
    )
    command = [
        "psql",
        "--no-psqlrc",
        "--quiet",
        "--tuples-only",
        "--host", db_host,
        "--port", str(db_port),
        "--username", db_user,
        "--dbname", db_name,
        "--set", f"account={sender_user}",
        "--set", f"funcom_id={sender_funcom_id}",
        "--set", f"platform_id={platform_id}",
        "--set", f"platform_name={platform_name}",
        "--set", f"character_name={sender_name}",
        "--command", sql,
    ]
    env_vars = os.environ.copy()
    env_vars["PGPASSWORD"] = db_password
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env_vars,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "psql is not installed"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    return {"ok": True, "output": result.stdout.strip()}


account_result = ensure_sender_account()


def request_json(method, path, body=None, timeout=None):
    if timeout is None:
        timeout = http_timeout
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{rmq_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode()
    return json.loads(text) if text else {}


def safe_request(method, path, body=None):
    try:
        return request_json(method, path, body), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:
        return None, str(exc)


def decode_chunked(payload):
    decoded = bytearray()
    rest = payload
    while rest:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            raise ValueError("truncated chunked response")
        size = int(line.split(b";", 1)[0], 16)
        if size == 0:
            return bytes(decoded)
        decoded.extend(rest[:size])
        rest = rest[size + 2:]
    return bytes(decoded)


def docker_api(method, path, body=None, timeout=30):
    data = b"" if body is None else json.dumps(body).encode()
    request = [
        f"{method} {path} HTTP/1.1",
        "Host: docker",
        "Connection: close",
    ]
    if body is not None:
        request.extend(["Content-Type: application/json", f"Content-Length: {len(data)}"])
    request.append("")
    request.append("")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(docker_socket)
    sock.sendall("\r\n".join(request).encode() + data)
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    sock.close()
    raw = b"".join(chunks)
    header, _, payload = raw.partition(b"\r\n\r\n")
    header_text = header.decode("iso-8859-1")
    status = int(header_text.split(" ", 2)[1])
    response_headers = {}
    for line in header_text.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            response_headers[name.strip().lower()] = value.strip().lower()
    if response_headers.get("transfer-encoding") == "chunked":
        payload = decode_chunked(payload)
    return status, payload


def current_image():
    container_name = env("HOSTNAME", "")
    if not container_name:
        return "registry.funcom.com/funcom/self-hosting/seabass-server-db-utils:" + env("DUNE_IMAGE_TAG", "latest")
    status, payload = docker_api("GET", f"/containers/{container_name}/json")
    if status == 200:
        return json.loads(payload.decode()).get("Config", {}).get("Image", "")
    return "registry.funcom.com/funcom/self-hosting/seabass-server-db-utils:" + env("DUNE_IMAGE_TAG", "latest")


def publish_with_host_container():
    script_dir = os.path.realpath(os.environ.get("DUNE_ANNOUNCE_SCRIPT_DIR", ""))
    if not script_dir or not os.path.isdir(script_dir):
        raise RuntimeError("announcement script directory is unavailable")
    name = "dash-announce-" + uuid.uuid4().hex
    image = env("DUNE_ANNOUNCE_DOCKER_IMAGE", current_image())
    merged_env = dict(file_env)
    merged_env.update(os.environ)
    merged_env["DUNE_ANNOUNCE_HOST_AMQP_HOST"] = env("DUNE_ANNOUNCE_HOST_AMQP_HOST", "172.31.240.1")
    merged_env["DUNE_ANNOUNCE_HOST_AMQP_PORT"] = env("DUNE_ANNOUNCE_HOST_AMQP_PORT", env("GAME_RMQ_PUBLIC_PORT", "31982"))
    runtime_names = (
        "DUNE_ANNOUNCE_CHAT_CHANNEL",
        "DUNE_ANNOUNCE_CHAT_EXCHANGE",
        "DUNE_ANNOUNCE_CHAT_FUNCOM_ID",
        "DUNE_ANNOUNCE_CHAT_PASSWORD",
        "DUNE_ANNOUNCE_CHAT_SPOOF_NAME",
        "DUNE_ANNOUNCE_CHAT_TIMESTAMP_FIELD",
        "DUNE_ANNOUNCE_CHAT_USE_SPOOF_NAME",
        "DUNE_ANNOUNCE_CHAT_USER",
        "DUNE_ANNOUNCE_CHAT_USER_NAME_TO",
        "DUNE_ANNOUNCE_CHAT_ROUTING_KEYS",
        "DUNE_ANNOUNCE_GAME_RMQ_AMQP_HOST",
        "DUNE_ANNOUNCE_GAME_RMQ_AMQP_PORT",
        "DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS",
        "DUNE_ANNOUNCE_HOST_AMQP_HOST",
        "DUNE_ANNOUNCE_HOST_AMQP_PORT",
        "GAME_RMQ_PUBLIC_PORT",
    )
    runtime_values = {key: merged_env[key] for key in runtime_names if key in merged_env}
    runtime_values["DUNE_ANNOUNCE_MESSAGE"] = message
    runtime_values["DUNE_ANNOUNCE_RESTART_AT"] = restart_at
    runtime_values["DUNE_ANNOUNCE_JOB_ID"] = job_id

    secret_dir = os.path.join(os.path.dirname(script_dir), "config", "secrets")
    if not os.path.isdir(secret_dir):
        secret_dir = tempfile.gettempdir()
    secret_fd, secret_path = tempfile.mkstemp(prefix=".announce-runtime-", suffix=".json", dir=secret_dir)
    try:
        os.fchmod(secret_fd, 0o600)
        with os.fdopen(secret_fd, "w", encoding="utf-8") as handle:
            json.dump(runtime_values, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())

        child_env = ["DUNE_ANNOUNCE_ENV_JSON=/tmp/announce-runtime.json"]
        body = {
            "Image": image,
            "Entrypoint": ["python3"],
            "Cmd": ["/workspace/scripts/announce_pika.py"],
            "WorkingDir": "/workspace/scripts",
            "Env": child_env,
            "HostConfig": {
                "NetworkMode": "host",
                "Mounts": [
                    {"Type": "bind", "Source": script_dir, "Target": "/workspace/scripts", "ReadOnly": True},
                    {"Type": "bind", "Source": secret_path, "Target": "/tmp/announce-runtime.json", "ReadOnly": True},
                ],
                "AutoRemove": False,
            },
        }
        status, payload_bytes = docker_api("POST", f"/containers/create?name={urllib.parse.quote(name)}", body)
        if status != 201:
            raise RuntimeError(f"container create failed: HTTP {status}")
        container_id = json.loads(payload_bytes.decode())["Id"]
        try:
            status, payload_bytes = docker_api("POST", f"/containers/{container_id}/start")
            if status != 204:
                raise RuntimeError(f"container start failed: HTTP {status}")
            status, payload_bytes = docker_api("POST", f"/containers/{container_id}/wait", timeout=60)
            if status != 200:
                raise RuntimeError(f"container wait failed: HTTP {status}")
            wait_result = json.loads(payload_bytes.decode() or "{}")
            status, logs = docker_api("GET", f"/containers/{container_id}/logs?stdout=true&stderr=true")
            text = logs.decode(errors="replace")
            if int(wait_result.get("StatusCode") or 1) != 0:
                for line in reversed(text.strip().splitlines()):
                    start = line.find("{")
                    if start < 0:
                        continue
                    try:
                        parsed = json.loads(line[start:])
                    except json.JSONDecodeError:
                        continue
                    if parsed.get("ok"):
                        return parsed
                raise RuntimeError("announcement fallback container failed")
            for line in reversed(text.strip().splitlines()):
                start = line.find("{")
                if start >= 0:
                    return json.loads(line[start:])
            raise RuntimeError("announcement fallback returned no result")
        finally:
            status, _ = docker_api("DELETE", f"/containers/{container_id}?force=true")
            if status not in (204, 404):
                raise RuntimeError(f"could not remove announcement fallback container (HTTP {status})")
    finally:
        try:
            os.unlink(secret_path)
        except FileNotFoundError:
            pass
def shortstr(value):
    data = value.encode()
    if len(data) > 255:
        raise ValueError("AMQP shortstr is too long")
    return bytes([len(data)]) + data


def longstr(value):
    data = value.encode()
    return struct.pack(">I", len(data)) + data


def empty_table():
    return struct.pack(">I", 0)


def amqp_frame(frame_type, channel, payload):
    return struct.pack(">BHI", frame_type, channel, len(payload)) + payload + b"\xce"


def amqp_method(channel, class_id, method_id, args=b""):
    return amqp_frame(1, channel, struct.pack(">HH", class_id, method_id) + args)


def amqp_read_frame(sock):
    header = sock.recv(7)
    if len(header) != 7:
        raise RuntimeError("short AMQP frame header")
    frame_type, channel, size = struct.unpack(">BHI", header)
    payload = b""
    while len(payload) < size:
        chunk = sock.recv(size - len(payload))
        if not chunk:
            raise RuntimeError("short AMQP frame payload")
        payload += chunk
    frame_end = sock.recv(1)
    if frame_end != b"\xce":
        raise RuntimeError("invalid AMQP frame terminator")
    return frame_type, channel, payload


def amqp_wait_method(sock, expected):
    while True:
        frame_type, channel, payload = amqp_read_frame(sock)
        if frame_type != 1:
            continue
        class_id, method_id = struct.unpack(">HH", payload[:4])
        if (class_id, method_id) == expected:
            return channel, payload[4:]
        if (class_id, method_id) == (10, 50):
            raise RuntimeError("AMQP connection.close from broker")


def amqp_publish_once(routing_key, body, properties):
    raw = socket.create_connection((amqp_host, amqp_port), timeout=8)
    if amqp_tls:
        context = ssl._create_unverified_context()
        sock = context.wrap_socket(raw, server_hostname=amqp_host)
    else:
        sock = raw
    sock.settimeout(8)
    try:
        sock.sendall(b"AMQP\x00\x00\x09\x01")
        amqp_wait_method(sock, (10, 10))
        response = "\0%s\0%s" % (sender_user, sender_password)
        sock.sendall(amqp_method(0, 10, 11, empty_table() + shortstr("PLAIN") + longstr(response) + shortstr("en_US")))
        _, tune = amqp_wait_method(sock, (10, 30))
        channel_max, frame_max, heartbeat = struct.unpack(">HIH", tune[:8])
        sock.sendall(amqp_method(0, 10, 31, struct.pack(">HIH", channel_max, frame_max, heartbeat)))
        sock.sendall(amqp_method(0, 10, 40, shortstr("/") + shortstr("") + b"\x00"))
        amqp_wait_method(sock, (10, 41))
        sock.sendall(amqp_method(1, 20, 10, shortstr("")))
        amqp_wait_method(sock, (20, 11))
        sock.sendall(amqp_method(1, 60, 40, struct.pack(">H", 0) + shortstr(exchange) + shortstr(routing_key) + b"\x00\x00"))
        body_bytes = body.encode()
        # AMQP basic property flags must be encoded in spec order. Chat clients
        # discard these messages if user_id/type/message_id are shifted into
        # the wrong slots.
        flags = 0x8000 | 0x1000 | 0x0080 | 0x0040 | 0x0020 | 0x0010
        header_payload = (
            struct.pack(">HHQH", 60, 0, len(body_bytes), flags)
            + shortstr(properties["content_type"])
            + struct.pack(">B", properties["delivery_mode"])
            + shortstr(properties["message_id"])
            + struct.pack(">Q", properties["timestamp"])
            + shortstr(properties["type"])
            + shortstr(properties["user_id"])
        )
        sock.sendall(amqp_frame(2, 1, header_payload))
        max_body = min(frame_max or 131072, 131072) - 8
        for offset in range(0, len(body_bytes), max_body):
            sock.sendall(amqp_frame(3, 1, body_bytes[offset:offset + max_body]))
        sock.sendall(amqp_method(1, 20, 40))
        sock.sendall(amqp_method(0, 10, 50, struct.pack(">H", 200) + shortstr("OK") + struct.pack(">HH", 0, 0)))
        return True
    finally:
        sock.close()


def publish_with_amqp():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host=amqp_host,
        port=amqp_port,
        virtual_host="/",
        credentials=pika.PlainCredentials(sender_user, sender_password),
        ssl_options=pika.SSLOptions(context, amqp_host) if amqp_tls else None,
        heartbeat=0,
        blocked_connection_timeout=10,
    ))
    channel = connection.channel()
    amqp_results = []
    try:
        for routing_key in routing_keys:
            properties = pika.BasicProperties(
                content_type="Content",
                delivery_mode=1,
                timestamp=int(time.time()),
                type="text_chat",
                user_id=sender_user,
                message_id=secrets.token_urlsafe(16),
            )
            try:
                channel.basic_publish(
                    exchange,
                    routing_key,
                    json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    properties,
                    mandatory=False,
                )
                amqp_results.append({"routingKey": routing_key, "ok": True})
            except Exception as exc:
                amqp_results.append({"routingKey": routing_key, "ok": False, "error": str(exc)})
    finally:
        connection.close()
    return amqp_results


bound_queues = []
bind_errors = []
if bind_online_queues or target_queues:
    queues, error = safe_request("GET", f"/api/queues/{vhost}")
    if error:
        bind_errors.append({"step": "listQueues", "error": error})
    else:
        seen = set(target_queues)
        if bind_online_queues:
            for queue in queues:
                name = queue.get("name", "")
                if queue_pattern.match(name) and int(queue.get("consumers") or 0) > 0:
                    seen.add(name)
        for queue_name in sorted(seen):
            if not queue_name:
                continue
            for routing_key in routing_keys:
                path = (
                    f"/api/bindings/{vhost}/e/{urllib.parse.quote(exchange, safe='')}"
                    f"/q/{urllib.parse.quote(queue_name, safe='')}"
                )
                _, error = safe_request("POST", path, {"routing_key": routing_key, "arguments": {}})
                if error:
                    bind_errors.append({"queue": queue_name, "routingKey": routing_key, "error": error})
                else:
                    bound_queues.append({"queue": queue_name, "routingKey": routing_key})


def cleanup_with_management(bound):
    errors = []
    for item in bound:
        props = item.get("routingKey") or "~"
        path = (
            f"/api/bindings/{vhost}/e/{urllib.parse.quote(exchange, safe='')}"
            f"/q/{urllib.parse.quote(item.get('queue', ''), safe='')}"
            f"/{urllib.parse.quote(props, safe='')}"
        )
        _, error = safe_request("DELETE", path)
        if error:
            errors.append({"queue": item.get("queue"), "routingKey": item.get("routingKey"), "error": error})
    return errors

timestamp = time.strftime("%Y.%m.%d-%H.%M.%S", time.gmtime())
timestamp_field = env("DUNE_ANNOUNCE_CHAT_TIMESTAMP_FIELD", "m_TimeStamp")
chat_message = {
    "m_Id": uuid.uuid4().hex.upper(),
    "m_ChannelType": channel_type,
    "m_bUseSpoofedUserName": use_spoof_name,
    "m_SpoofedUserNameFrom": {
        "m_TableId": "",
        "m_Key": "",
        "m_UnlocalizedName": sender_name if use_spoof_name else "",
    },
    "m_FuncomIdFrom": sender_funcom_id,
    "m_UserNameTo": user_name_to,
    "m_Message": {
        "m_UnlocalizedMessage": message,
        "m_LocalizedMessage": {"m_TableId": "", "m_Key": "", "m_FormatArgs": []},
    },
    "m_OriginLocation": {"X": 0.0, "Y": 0.0, "Z": 0.0},
    "m_HasSeenMessage": False,
}
chat_message[timestamp_field] = timestamp
payload = {"content": json.dumps(chat_message, separators=(",", ":")), "Type": "TextChat"}

fallback = ""
results = []
try:
    results = publish_with_amqp()
    fallback = "direct-pika"
except Exception as exc:
    bind_errors.append({"step": "directPika", "error": str(exc)})
    try:
        host_result = publish_with_host_container()
        results = host_result.get("routingKeys", [])
        fallback = "docker-host-pika"
    except Exception as host_exc:
        bind_errors.append({"step": "dockerHostPika", "error": str(host_exc)})

if not any(item["ok"] for item in results) and allow_management_publish:
    for routing_key in routing_keys:
        properties = {
            "content_type": "Content",
            "delivery_mode": 1,
            "timestamp": int(time.time()),
            "type": "text_chat",
            "user_id": sender_user,
            "message_id": secrets.token_urlsafe(16),
        }
        body = {
            "properties": properties,
            "routing_key": routing_key,
            "payload": json.dumps(payload, separators=(",", ":")),
            "payload_encoding": "string",
        }
        path = f"/api/exchanges/{vhost}/{urllib.parse.quote(exchange, safe='')}/publish"
        response, error = safe_request("POST", path, body)
        routed = bool(response and response.get("routed"))
        results.append({"routingKey": routing_key, "ok": routed, "error": error})
    fallback = "management-publish"

cleanup_errors = []
if cleanup_target_bindings:
    cleanup_items = [item for item in bound_queues if item.get("queue") in set(target_queues)]
    cleanup_errors = cleanup_with_management(cleanup_items)

ok = any(item["ok"] for item in results)
print(json.dumps({
    "ok": ok,
    "transport": exchange,
    "fallback": fallback,
    "exchange": exchange,
    "sender": sender_user,
    "account": account_result,
    "routingKeys": results,
    "boundQueues": bound_queues,
    "bindErrors": bind_errors,
    "cleanupErrors": cleanup_errors,
    "jobId": job_id,
    "restartAt": restart_at,
}, separators=(",", ":")))
sys.exit(0 if ok else 75)
PY
