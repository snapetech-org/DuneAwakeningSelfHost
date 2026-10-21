#!/usr/bin/env python3
"""Stage only the player-presence service settings from the Dune .env file."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / "config" / "secrets"
DEFAULT_OUTPUT = SECRET_DIR / "player-presence.env"
ALLOWED_PREFIXES = ("DUNE_PLAYER_PRESENCE_",)
ALLOWED_NAMES = {
    "COMPOSE_PROJECT_NAME",
    "CONTAINER_RUNTIME",
    "DOCKER_COMPOSE_PROJECT",
    "DOCKER_SOCKET",
    "DUNE_ADMIN_BOT_MAX_BASES_WARN",
    "DUNE_ADMIN_BOT_STUCK_TRANSITION_MINUTES",
    "DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES",
    "DUNE_ANNOUNCE_CHAT_CHANNEL",
    "DUNE_ANNOUNCE_CHAT_EXCHANGE",
    "DUNE_ANNOUNCE_CHAT_FUNCOM_ID",
    "DUNE_ANNOUNCE_CHAT_PASSWORD",
    "DUNE_ANNOUNCE_CHAT_QUEUE_PATTERN",
    "DUNE_ANNOUNCE_CHAT_ROUTING_KEYS",
    "DUNE_ANNOUNCE_CHAT_SPOOF_NAME",
    "DUNE_ANNOUNCE_CHAT_TIMESTAMP_FIELD",
    "DUNE_ANNOUNCE_CHAT_USER",
    "DUNE_ANNOUNCE_CHAT_USE_SPOOF_NAME",
    "DUNE_ANNOUNCE_GAME_RMQ_AMQP_HOST",
    "DUNE_ANNOUNCE_GAME_RMQ_AMQP_PORT",
    "DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS",
    "DUNE_ANNOUNCE_GAME_RMQ_MANAGEMENT_URL",
    "DUNE_ANNOUNCE_HOST_AMQP_HOST",
    "DUNE_ANNOUNCE_HOST_AMQP_PORT",
    "DUNE_ANNOUNCE_HTTP_TIMEOUT_SECONDS",
    "DUNE_ANNOUNCE_RMQ_PASSWORD",
    "DUNE_ANNOUNCE_RMQ_USER",
    "DUNE_CHAT_COMMAND_ADMINS",
    "DUNE_CHAT_COMMAND_ADMIN_FLS_IDS",
    "DUNE_DB_NAME",
    "DUNE_DIRECTOR_HEALTH_CONTAINER",
    "DUNE_FLS_PUBLICATION_HEALTH_LOG_TAIL",
    "DUNE_IMAGE_TAG",
    "DUNE_PUBLIC_SITE_URL",
    "DUNE_SERVER_DISPLAY_NAME",
    "DUNE_SERVER_NAME",
    "DUNE_SUBFIEF_BASE_LIMIT",
    "DUNE_SUBFIEF_LIMIT",
    "DUNE_SUBFIEF_LIMIT_BONUS",
    "DUNE_RESTART_DOCKER_SOCKET",
    "GAME_RMQ_PUBLIC_PORT",
    "WORLD_NAME",
}
BLOCKED_NAMES = {
    # The unit or parent process supplies these fixed values. Do not permit the
    # source .env to redirect dotenv reads or choose an executable.
    "DUNE_ADMIN_ANNOUNCE_COMMAND",
    "DUNE_ADMIN_BOT_ENV_FILE",
    "DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND",
    "DUNE_PLAYER_PRESENCE_ENV_FILE",
    "DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_COMMAND",
}
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def allowed(name: str) -> bool:
    return name not in BLOCKED_NAMES and (name in ALLOWED_NAMES or name.startswith(ALLOWED_PREFIXES))


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if not allowed(name):
                continue
            if not KEY_RE.fullmatch(name):
                raise ValueError("invalid environment key in source file")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if "\0" in value or "\n" in value or "\r" in value:
                raise ValueError(f"unsupported value format for {name}")
            values[name] = value
    return values


def systemd_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_private(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        if path.is_symlink():
            raise ValueError("refusing to replace a symlink output")
    except OSError as exc:
        raise ValueError("cannot inspect output path") from exc
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = pathlib.Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=pathlib.Path, default=ROOT / ".env")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    source = args.env_file if args.env_file.is_absolute() else ROOT / args.env_file
    output = args.output if args.output.is_absolute() else ROOT / args.output
    values = parse_env(source)
    if not values:
        raise SystemExit("no player-presence settings were found; output was not changed")
    content = "".join(f"{name}={systemd_value(value)}\n" for name, value in sorted(values.items()))
    write_private(output, content)
    print(f"staged {len(values)} player-presence settings in mode-0600 file: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"player-presence environment staging failed: {exc}") from None
