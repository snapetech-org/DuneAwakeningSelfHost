#!/usr/bin/env python3
import json
import os
import pathlib
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "vendor"))

import pika
from secret_env import read_secret_env


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_env_file(path):
    values = {}
    try:
        for raw in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


FILE_ENV = {}
for env_path in ("/workspace/.env", ROOT / ".env"):
    FILE_ENV.update(read_env_file(env_path))


def env(name, default=""):
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    return FILE_ENV.get(name, default)


def secret_env(name, default=""):
    if os.environ.get(f"{name}_FILE", "").strip():
        return read_secret_env(name, default)
    return env(name, default)


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def default_admin_rmq_host():
    return "127.0.0.1" if not pathlib.Path("/workspace/.env").exists() else "admin-rmq"


def default_admin_rmq_port():
    return "5673" if not pathlib.Path("/workspace/.env").exists() else "5672"


def amqp_connection():
    user = env("DUNE_GM_COMMAND_AMQP_USER", env("DUNE_ANNOUNCE_RMQ_USER"))
    password = secret_env("DUNE_GM_COMMAND_AMQP_PASSWORD") or secret_env("DUNE_ANNOUNCE_RMQ_PASSWORD")
    if not user or not password:
        raise RuntimeError("missing DUNE_GM_COMMAND_AMQP_USER/PASSWORD or DUNE_ANNOUNCE_RMQ_USER/PASSWORD")
    ssl_options = None
    if env_bool("DUNE_GM_COMMAND_AMQP_TLS", False):
        context = ssl.create_default_context()
        context.check_hostname = env_bool("DUNE_GM_COMMAND_AMQP_TLS_CHECK_HOSTNAME", False)
        if env_bool("DUNE_GM_COMMAND_AMQP_TLS_VERIFY", False):
            ca_file = env("DUNE_GM_COMMAND_AMQP_TLS_CA_FILE")
            if ca_file:
                context.load_verify_locations(cafile=ca_file)
        else:
            context.verify_mode = ssl.CERT_NONE
        ssl_options = pika.SSLOptions(context, env("DUNE_GM_COMMAND_AMQP_HOST", env("DUNE_GM_PROBE_AMQP_HOST", default_admin_rmq_host())))
    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=env("DUNE_GM_COMMAND_AMQP_HOST", env("DUNE_GM_PROBE_AMQP_HOST", default_admin_rmq_host())),
            port=int(env("DUNE_GM_COMMAND_AMQP_PORT", env("DUNE_GM_PROBE_AMQP_PORT", default_admin_rmq_port()))),
            virtual_host=env("DUNE_GM_COMMAND_AMQP_VHOST", "/"),
            credentials=pika.PlainCredentials(user, password),
            ssl_options=ssl_options,
            heartbeat=0,
            blocked_connection_timeout=10,
        )
    )


def split_command(command_text):
    command_text = command_text.strip()
    if not command_text:
        raise ValueError("command text is required")
    parts = command_text.split(None, 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    return command, args


def build_envelope(mode, command_text, target_player="", admin_player=""):
    command, args = split_command(command_text)
    target_player = target_player or admin_player
    if mode == "jsonrpc-notify-array":
        return {"jsonrpc": "2.0", "method": "ServerCommand", "params": [command_text]}
    if mode == "jsonrpc-send-dune-array":
        return {"jsonrpc": "2.0", "method": "SendDuneServerCommand", "params": [command_text, target_player, admin_player]}
    if mode == "jsonrpc-serverexec-array":
        return {"jsonrpc": "2.0", "method": "ServerExec", "params": [target_player, command_text]}
    if mode == "service-message":
        return {"Command": "ServerCommand", "CommandText": command_text, "TargetPlayer": target_player, "AdminPlayer": admin_player}
    if mode == "send-dune-server-command":
        return {"Command": "SendDuneServerCommand", "Params": [command_text, target_player, admin_player]}
    if mode == "server-exec":
        return {"Command": "ServerExec", "CommandText": command_text, "TargetPlayer": target_player, "AdminPlayer": admin_player}
    if mode == "server-exec-rpc":
        return {"Command": "ServerExecRPC", "TargetPlayer": target_player, "ConsoleCommand": command_text, "AdminPlayer": admin_player}
    if mode == "command-object":
        return {"Command": command, "Args": args, "TargetPlayer": target_player, "AdminPlayer": admin_player}
    if mode == "rpc-task-array":
        return ["ServerCommand", command_text, target_player, admin_player]
    if mode == "rpc-task-object":
        return {"m_Command": "ServerCommand", "m_Args": [command_text, target_player, admin_player]}
    if mode == "rpc-api-positional-one":
        return [command_text]
    if mode == "rpc-api-positional-two":
        return [target_player, command_text]
    if mode == "rpc-api-positional-method-one":
        return ["ServerCommand", [command_text]]
    if mode == "rpc-api-positional-method-two":
        return ["ServerExec", [target_player, command_text]]
    if mode == "rpc-api-object-one":
        return {"Api": "ServerCommand", "Arguments": [command_text]}
    if mode == "rpc-api-object-two":
        return {"Api": "ServerExec", "Arguments": [target_player, command_text]}
    if mode == "dw-notification-message":
        return {
            "m_Api": "ServerCommand",
            "m_Method": "ServerCommand",
            "m_Payload": [command_text],
            "m_Sender": admin_player,
        }
    if mode == "dw-rpc-task-method-params":
        return {"method": command, "params": [args] if args else []}
    if mode == "dw-rpc-task-api-args":
        return {"api": command, "args": [args] if args else []}
    if mode == "dw-rpc-task-api-arguments":
        return {"api": command, "arguments": [args] if args else []}
    if mode == "dw-rpc-task-upper-api-args":
        return {"Api": command, "Args": [args] if args else []}
    if mode == "dw-rpc-task-name-arguments":
        return {"name": command, "arguments": [args] if args else []}
    if mode == "dw-rpc-task-commandline":
        return {"commandLine": command_text}
    if mode == "dw-rpc-task-command":
        return {"command": command, "arguments": [args] if args else []}
    if mode == "ue-fstring-array":
        return [command_text, target_player, admin_player]
    if mode == "plain":
        return command_text
    if mode == "plain-serverexec":
        return f"ServerExec {target_player} {command_text}".strip()
    raise ValueError(f"unknown GM envelope mode: {mode}")


def candidate_modes():
    return [
        "jsonrpc-notify-array",
        "jsonrpc-send-dune-array",
        "jsonrpc-serverexec-array",
        "service-message",
        "send-dune-server-command",
        "server-exec",
        "server-exec-rpc",
        "command-object",
        "rpc-task-array",
        "rpc-task-object",
        "rpc-api-positional-one",
        "rpc-api-positional-two",
        "rpc-api-positional-method-one",
        "rpc-api-positional-method-two",
        "rpc-api-object-one",
        "rpc-api-object-two",
        "dw-notification-message",
        "dw-rpc-task-method-params",
        "dw-rpc-task-api-args",
        "dw-rpc-task-api-arguments",
        "dw-rpc-task-upper-api-args",
        "dw-rpc-task-name-arguments",
        "dw-rpc-task-commandline",
        "dw-rpc-task-command",
        "ue-fstring-array",
        "plain",
        "plain-serverexec",
    ]


def serialize_body(envelope):
    if isinstance(envelope, str):
        return envelope.encode("utf-8"), "text/plain"
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8"), "application/json"


def publish_command(command_text, route, target_player="", admin_player="", mode=None, exchange=None, app_id="DASH", reply_to=None):
    mode = mode or env("DUNE_GM_COMMAND_ENVELOPE_MODE", "service-message")
    exchange = exchange or env("DUNE_GM_COMMAND_EXCHANGE", "rpc")
    envelope = build_envelope(mode, command_text, target_player=target_player, admin_player=admin_player)
    body, content_type = serialize_body(envelope)
    correlation_id = f"dash-gm-{mode}-{uuid.uuid4().hex[:8]}"
    props = pika.BasicProperties(
        content_type=content_type,
        delivery_mode=1,
        timestamp=int(time.time()),
        type=env("DUNE_GM_COMMAND_AMQP_TYPE", "json_rpc"),
        reply_to=reply_to or env("DUNE_GM_COMMAND_REPLY_TO", "bgdRpc"),
        correlation_id=correlation_id,
        message_id=correlation_id,
        app_id=app_id,
        user_id=env("DUNE_GM_COMMAND_AMQP_USER_ID", env("DUNE_GM_COMMAND_AMQP_USER", "")) or None,
        headers={
            "dash_gm_command": True,
            "dash_gm_mode": mode,
            "dash_nonce": secrets.token_hex(4),
        },
    )
    conn = amqp_connection()
    try:
        channel = conn.channel()
        channel.basic_publish(
            exchange=exchange,
            routing_key=route,
            body=body,
            properties=props,
            mandatory=False,
        )
    finally:
        conn.close()
    return {
        "ok": True,
        "transport": "amqp",
        "exchange": exchange,
        "route": route,
        "mode": mode,
        "correlationId": correlation_id,
        "commandText": command_text,
        "targetPlayer": target_player,
        "adminPlayer": admin_player,
        "envelope": envelope,
    }


def publish_command_management(command_text, route, target_player="", admin_player="", mode=None, exchange=None):
    mode = mode or env("DUNE_GM_COMMAND_ENVELOPE_MODE", "service-message")
    exchange = exchange or env("DUNE_GM_COMMAND_EXCHANGE", "rpc")
    envelope = build_envelope(mode, command_text, target_player=target_player, admin_player=admin_player)
    body, content_type = serialize_body(envelope)
    url = env("DUNE_GM_COMMAND_RMQ_URL", env("DUNE_ANNOUNCE_RMQ_URL", "http://127.0.0.1:15672")).rstrip("/")
    user = env("DUNE_GM_COMMAND_RMQ_USER", env("DUNE_GM_COMMAND_AMQP_USER", env("DUNE_ANNOUNCE_RMQ_USER", "guest")))
    password = secret_env("DUNE_GM_COMMAND_RMQ_PASSWORD") or secret_env("DUNE_GM_COMMAND_AMQP_PASSWORD") or secret_env("DUNE_ANNOUNCE_RMQ_PASSWORD")
    if not password:
        raise RuntimeError("missing DUNE_GM_COMMAND_RMQ_PASSWORD or DUNE_ANNOUNCE_RMQ_PASSWORD")
    payload = {
        "properties": {
            "content_type": content_type,
            "delivery_mode": 1,
            "timestamp": int(time.time()),
            "type": env("DUNE_GM_COMMAND_AMQP_TYPE", "json_rpc"),
            "reply_to": env("DUNE_GM_COMMAND_REPLY_TO", "bgdRpc"),
            "correlation_id": f"dash-gm-mgmt-{mode}-{uuid.uuid4().hex[:8]}",
            "app_id": "DASH",
            "user_id": env("DUNE_GM_COMMAND_AMQP_USER_ID", env("DUNE_GM_COMMAND_AMQP_USER", "")),
            "headers": {"dash_gm_command": True, "dash_gm_mode": mode, "dash_nonce": secrets.token_hex(4)},
        },
        "routing_key": route,
        "payload": body.decode("utf-8"),
        "payload_encoding": "string",
    }
    req = urllib.request.Request(
        f"{url}/api/exchanges/%2F/{exchange}/publish",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    req.add_header("Authorization", "Basic " + __import__("base64").b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii"))
    try:
        with urllib.request.urlopen(req, timeout=float(env("DUNE_GM_COMMAND_HTTP_TIMEOUT_SECONDS", "5"))) as resp:
            response = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"RabbitMQ management publish failed HTTP {exc.code}: {detail}") from exc
    return {
        "ok": bool(response.get("routed")),
        "transport": "management",
        "exchange": exchange,
        "route": route,
        "mode": mode,
        "commandText": command_text,
        "targetPlayer": target_player,
        "adminPlayer": admin_player,
        "envelope": envelope,
        "response": response,
    }
