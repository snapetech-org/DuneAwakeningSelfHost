#!/usr/bin/env python3
"""Systemd socket-activated signer for the public Dune directory entry."""

from __future__ import annotations

import json
import os
import socket
import sys


MAX_REQUEST_BYTES = 262_144
CONFIG_KEYS = {
    "DUNE_PUBLIC_DIRECTORY_ENABLED",
    "DUNE_PUBLIC_DIRECTORY_ENTRY_URL",
    "DUNE_PUBLIC_SITE_URL",
    "DUNE_PUBLIC_DIRECTORY_REGION",
    "DUNE_PUBLIC_DIRECTORY_CAPACITY",
    "DUNE_PUBLIC_DIRECTORY_TTL_SECONDS",
    "DUNE_PUBLIC_DIRECTORY_DISCORD_INVITE",
    "DUNE_PUBLIC_DIRECTORY_NAME",
    "DUNE_PUBLIC_DIRECTORY_DESCRIPTION",
    "DUNE_IMAGE_TAG",
}
SNAPSHOT_KEYS = {"ok", "onlineCount", "mapHealth", "sietches"}


def load_public_directory():
    module_path = os.environ.get("DUNE_PUBLIC_DIRECTORY_MODULE_DIR", "/usr/local/libexec/dune-public-directory")
    if module_path not in sys.path:
        sys.path.insert(0, module_path)
    import public_directory

    return public_directory


def sign_request(request):
    if not isinstance(request, dict) or set(request) != {"config", "snapshot"}:
        raise ValueError("invalid request shape")
    values = request["config"]
    snapshot = request["snapshot"]
    if not isinstance(values, dict) or not set(values).issubset(CONFIG_KEYS):
        raise ValueError("invalid public configuration")
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("public configuration values must be strings")
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_KEYS:
        raise ValueError("invalid public snapshot")
    if not isinstance(snapshot["ok"], bool) or not isinstance(snapshot["mapHealth"], dict):
        raise ValueError("invalid public snapshot values")

    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credentials:
        raise RuntimeError("systemd signing credential is unavailable")
    signing_key = os.path.join(credentials, "public-directory-ed25519.pem")
    env = dict(values)
    env["DUNE_PUBLIC_DIRECTORY_ENABLED"] = "true"
    env["DUNE_PUBLIC_DIRECTORY_KEY_FILE"] = signing_key
    public_directory = load_public_directory()
    config = public_directory.public_config(env, root="/")
    if not config.get("enabled"):
        return {"ok": True, "enabled": False}
    entry = public_directory.build_entry(snapshot, config)
    public_directory.verify_entry(entry, expected_url=config["entryUrl"])
    return {"ok": True, "enabled": True, "entry": entry}


def handle(connection):
    connection.settimeout(10)
    request_bytes = bytearray()
    while len(request_bytes) <= MAX_REQUEST_BYTES:
        chunk = connection.recv(min(16_384, MAX_REQUEST_BYTES + 1 - len(request_bytes)))
        if not chunk:
            break
        request_bytes.extend(chunk)
        if b"\n" in chunk:
            break
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds size limit")
    line = bytes(request_bytes).split(b"\n", 1)[0]
    request = json.loads(line)
    return sign_request(request)


def main():
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("signer must be started by its systemd socket")
    listener = socket.socket(fileno=3)
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                response = handle(connection)
            except Exception as exc:
                response = {"ok": False, "error": type(exc).__name__}
            connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"public directory signer stopped: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
