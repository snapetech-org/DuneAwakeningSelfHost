#!/usr/bin/env python3
import argparse
import base64
import json
import os
import pathlib
import sys
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from http_url import require_http_url


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


def env(name, file_env, default=""):
    return os.environ.get(name) or file_env.get(name, default)


def request_json(base_url, user, password, path):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(base_url.rstrip("/") + path)
    request.add_header("Authorization", f"Basic {token}")
    require_http_url(request.full_url)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def slim(items, fields):
    return [{field: item.get(field) for field in fields if field in item} for item in items]


def main():
    parser = argparse.ArgumentParser(description="Capture RabbitMQ topology through the management API.")
    parser.add_argument("env_file", nargs="?", default=".env")
    parser.add_argument("--broker", choices=("admin", "game"), default="admin")
    args = parser.parse_args()
    file_env = read_env_file(args.env_file)
    if args.broker == "admin":
        base_url = env("DUNE_RESEARCH_ADMIN_RMQ_URL", file_env, "http://127.0.0.1:15672")
        user = env("DUNE_RESEARCH_ADMIN_RMQ_USER", file_env, env("DUNE_ANNOUNCE_RMQ_USER", file_env))
        password = env("DUNE_RESEARCH_ADMIN_RMQ_PASSWORD", file_env, env("DUNE_ANNOUNCE_RMQ_PASSWORD", file_env))
    else:
        base_url = env("DUNE_RESEARCH_GAME_RMQ_URL", file_env, "http://127.0.0.1:15673")
        user = env(
            "DUNE_RESEARCH_GAME_RMQ_USER",
            file_env,
            env("DUNE_ANNOUNCE_RMQ_USER", file_env, env("DUNE_ANNOUNCE_CHAT_USER", file_env, "A000000000000001")),
        )
        password = env(
            "DUNE_RESEARCH_GAME_RMQ_PASSWORD",
            file_env,
            env("DUNE_ANNOUNCE_RMQ_PASSWORD", file_env, env("DUNE_ANNOUNCE_CHAT_PASSWORD", file_env)),
        )
    if not user or not password:
        raise SystemExit("missing RabbitMQ management credentials")
    exchanges = request_json(base_url, user, password, "/api/exchanges")
    queues = request_json(base_url, user, password, "/api/queues")
    bindings = request_json(base_url, user, password, "/api/bindings")
    consumers = request_json(base_url, user, password, "/api/consumers")
    print(json.dumps({
        "ok": True,
        "broker": args.broker,
        "exchanges": slim(exchanges, ("name", "vhost", "type", "durable", "auto_delete")),
        "queues": slim(queues, ("name", "vhost", "durable", "auto_delete", "consumers", "messages")),
        "bindings": slim(bindings, ("source", "vhost", "destination", "destination_type", "routing_key")),
        "consumers": slim(consumers, ("queue", "channel_details", "consumer_tag", "ack_required", "exclusive")),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
