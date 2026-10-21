#!/usr/bin/env python3
import json
import os
import pathlib
import secrets
import ssl
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "vendor"))
import pika
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from secret_env import read_secret_env


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


configured_json = os.environ.get("DUNE_ANNOUNCE_ENV_JSON")
if configured_json:
    file_env = json.loads(pathlib.Path(configured_json).read_text(encoding="utf-8"))
else:
    configured_env_files = os.environ.get("DUNE_ANNOUNCE_ENV_FILE")
    if configured_env_files is None:
        env_paths = ("/workspace/.env", ".env")
    else:
        env_paths = tuple(path for path in configured_env_files.split(os.pathsep) if path)
    file_env = {}
    for env_path in env_paths:
        file_env.update(read_env_file(env_path))


def env(name, default=""):
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    return file_env.get(name, default)


def secret_env(name, default=""):
    if os.environ.get(f"{name}_FILE", "").strip():
        return read_secret_env(name, default)
    value = env(name, default)
    return "" if value == "dash-admin-test" else value


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def split_csv(value):
    out = []
    for item in value.split(","):
        item = item.strip()
        if item in ("<empty>", "empty", "EMPTY"):
            out.append("")
        elif item:
            out.append(item)
    return out


message = sys.argv[1] if len(sys.argv) > 1 else env("DUNE_ANNOUNCE_MESSAGE")
if not message:
    print("missing message", file=sys.stderr)
    sys.exit(64)

host = env("DUNE_ANNOUNCE_HOST_AMQP_HOST", "127.0.0.1")
port = int(env("DUNE_ANNOUNCE_HOST_AMQP_PORT", env("GAME_RMQ_PUBLIC_PORT", "31982")))
tls = env_bool("DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS", True)
user = env("DUNE_ANNOUNCE_CHAT_USER", "A000000000000001")
password = secret_env("DUNE_ANNOUNCE_CHAT_PASSWORD")
funcom_id = env("DUNE_ANNOUNCE_CHAT_FUNCOM_ID", "ADMIN#00001")
exchange = env("DUNE_ANNOUNCE_CHAT_EXCHANGE", "chat.map")
routes = split_csv(env("DUNE_ANNOUNCE_CHAT_ROUTING_KEYS", "HaggaBasin.0,Survival_1.dim_0,<empty>"))
spoof = env_bool("DUNE_ANNOUNCE_CHAT_USE_SPOOF_NAME", False)
spoof_name = env("DUNE_ANNOUNCE_CHAT_SPOOF_NAME", "Paul")

context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
connection = pika.BlockingConnection(pika.ConnectionParameters(
    host=host,
    port=port,
    virtual_host="/",
    credentials=pika.PlainCredentials(user, password),
    ssl_options=pika.SSLOptions(context, host) if tls else None,
    heartbeat=0,
    blocked_connection_timeout=10,
))
channel = connection.channel()
results = []
try:
    for route in routes:
        chat = {
            "m_Id": uuid.uuid4().hex.upper(),
            "m_ChannelType": env("DUNE_ANNOUNCE_CHAT_CHANNEL", "Map"),
            "m_bUseSpoofedUserName": spoof,
            "m_SpoofedUserNameFrom": {"m_TableId": "", "m_Key": "", "m_UnlocalizedName": spoof_name if spoof else ""},
            "m_FuncomIdFrom": funcom_id,
            "m_UserNameTo": env("DUNE_ANNOUNCE_CHAT_USER_NAME_TO", ""),
            "m_Message": {
                "m_UnlocalizedMessage": message,
                "m_LocalizedMessage": {"m_TableId": "", "m_Key": "", "m_FormatArgs": []},
            },
            "m_OriginLocation": {"X": 0.0, "Y": 0.0, "Z": 0.0},
            "m_HasSeenMessage": False,
        }
        chat[env("DUNE_ANNOUNCE_CHAT_TIMESTAMP_FIELD", "m_TimeStamp")] = time.strftime("%Y.%m.%d-%H.%M.%S", time.gmtime())
        body = json.dumps({"content": json.dumps(chat, separators=(",", ":")), "Type": "TextChat"}, separators=(",", ":"))
        props = pika.BasicProperties(
            content_type="Content",
            delivery_mode=1,
            timestamp=int(time.time()),
            type="text_chat",
            user_id=user,
            message_id=secrets.token_urlsafe(16),
        )
        channel.basic_publish(exchange, route, body.encode("utf-8"), props, mandatory=False)
        results.append({"routingKey": route, "ok": True})
finally:
    connection.close()

print(json.dumps({"ok": True, "transport": "pika-host", "exchange": exchange, "sender": user, "routingKeys": results}, separators=(",", ":")))
