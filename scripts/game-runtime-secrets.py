#!/usr/bin/env python3
"""Stage protected Compose secrets and install game credentials into Unreal config."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import stat
import sys
import tempfile


SECRET_SOURCES = {
    "dune_postgres_password": ("dune-postgres-password", "POSTGRES_DUNE_PASSWORD", True),
    "dune_fls_token": ("dune-fls-token", "FLS_SECRET", True),
    "dune_server_commands_auth_token": (
        "dune-server-commands-token", "DUNE_SERVER_COMMANDS_AUTH_TOKEN", False
    ),
    "dune_server_login_password": (
        "dune-server-login-password", "DUNE_SERVER_LOGIN_PASSWORD", False
    ),
    "dune_postgres_replication_password": (
        "dune-postgres-replication-password", "POSTGRES_REPLICATION_PASSWORD", False
    ),
    "dune_admin_token": ("dune-admin-token", "DUNE_ADMIN_TOKEN", True),
    "dune_bot_api_token": ("dune-bot-api-token", "DUNE_BOT_API_TOKEN", False),
    "dune_announce_chat_password": (
        "dune-announce-chat-password", "DUNE_ANNOUNCE_CHAT_PASSWORD", False
    ),
    "dune_announce_rmq_password": (
        "dune-announce-rmq-password", "DUNE_ANNOUNCE_RMQ_PASSWORD", False
    ),
    "dune_gm_command_amqp_password": (
        "dune-gm-command-amqp-password", "DUNE_GM_COMMAND_AMQP_PASSWORD", False
    ),
    "dune_gm_command_rmq_password": (
        "dune-gm-command-rmq-password", "DUNE_GM_COMMAND_RMQ_PASSWORD", False
    ),
    "dune_chat_command_amqp_password": (
        "dune-chat-command-amqp-password", "DUNE_CHAT_COMMAND_AMQP_PASSWORD", False
    ),
    "dune_steam_password": ("dune-steam-password", "DUNE_STEAM_PASSWORD", False),
}

RUNTIME_FILES = {
    "dune_postgres_password": ("Game.ini", "DuneDatabaseInterfacePSQL", "DatabasePassword", True),
    "dune_fls_token": ("Engine.ini", "FuncomLiveServices", "ServiceAuthToken", True),
    "dune_server_commands_auth_token": (
        "Engine.ini", "FuncomLiveServices", "ServerCommandsAuthToken", False
    ),
}


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def sync_from_env(env_file: pathlib.Path, secret_dir: pathlib.Path) -> None:
    values = parse_env(env_file)
    secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secret_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    written = 0
    for _compose_name, (filename, env_key, required) in SECRET_SOURCES.items():
        value = values.get(env_key, "")
        if required and not value:
            raise ValueError(f"required environment key is empty: {env_key}")
        if any(char in value for char in "\x00\r\n"):
            raise ValueError(f"environment key contains a newline or NUL: {env_key}")
        atomic_write(secret_dir / filename, value.encode("utf-8"))
        written += 1
    print(f"staged {written} protected runtime secret files")


def read_runtime_secret(secret_dir: pathlib.Path, name: str, *, required: bool) -> str:
    path = secret_dir / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError(f"required runtime secret is missing: {name}") from None
        return ""
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"runtime secret is not a regular file: {name}")
    raw = path.read_bytes().rstrip(b"\r\n")
    if b"\x00" in raw or b"\n" in raw or b"\r" in raw:
        raise ValueError(f"runtime secret contains a newline or NUL: {name}")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"runtime secret is not UTF-8 text: {name}") from None
    if required and not value:
        raise ValueError(f"required runtime secret is empty: {name}")
    return value


def ini_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_ini_value(path: pathlib.Path, section: str, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    section_header = f"[{section}]"
    section_start = next((i for i, line in enumerate(lines) if line.strip() == section_header), None)
    rendered = f"{key}={ini_value(value)}\n"
    if section_start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend((section_header + "\n", rendered))
    else:
        section_end = next(
            (i for i in range(section_start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        matcher = re.compile(rf"^\s*{re.escape(key)}\s*=")
        matches = [i for i in range(section_start + 1, section_end) if matcher.match(lines[i])]
        if matches:
            lines[matches[0]] = rendered
            for i in reversed(matches[1:]):
                del lines[i]
        else:
            lines.insert(section_end, rendered)
    atomic_write(path, "".join(lines).encode("utf-8"))


def install_config(server_root: pathlib.Path, secret_dir: pathlib.Path) -> None:
    required = os.environ.get("DUNE_RUNTIME_SECRETS_REQUIRED", "false").lower() in {
        "1", "true", "yes", "on"
    }
    values: dict[str, str] = {}
    for compose_name, (_filename, _env_key, source_required) in SECRET_SOURCES.items():
        values[compose_name] = read_runtime_secret(
            secret_dir,
            compose_name,
            required=required and source_required and compose_name in RUNTIME_FILES,
        )

    sietch_login = read_runtime_secret(secret_dir, "dune_sietch_login_password", required=False)
    login = sietch_login or values["dune_server_login_password"]

    config_root = server_root / "DuneSandbox" / "Saved" / "Config" / "LinuxServer"
    user_root = server_root / "DuneSandbox" / "Saved" / "UserSettings"
    targets = {
        "Engine.ini": (config_root / "Engine.ini", user_root / "UserEngine.ini"),
        "Game.ini": (config_root / "Game.ini", user_root / "UserGame.ini"),
    }
    for compose_name, (filename, section, key, source_required) in RUNTIME_FILES.items():
        value = values[compose_name]
        if not value and not (required and source_required):
            continue
        secret_ini, _section, _key, _required = RUNTIME_FILES[compose_name]
        for target in targets[secret_ini]:
            set_ini_value(target, section, key, value)

    if login:
        for target in targets["Engine.ini"]:
            set_ini_value(target, "ConsoleVariables", "Bgd.ServerLoginPassword", login)

    print("installed protected runtime credentials into per-server Unreal config")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    sync = subparsers.add_parser("sync-from-env")
    sync.add_argument("--env-file", type=pathlib.Path, default=pathlib.Path(".env"))
    sync.add_argument("--secret-dir", type=pathlib.Path, default=pathlib.Path("config/secrets"))
    install = subparsers.add_parser("install-config")
    install.add_argument("--server-root", type=pathlib.Path, required=True)
    install.add_argument("--secret-dir", type=pathlib.Path, default=pathlib.Path("/run/secrets"))
    args = parser.parse_args()
    try:
        if args.action == "sync-from-env":
            sync_from_env(args.env_file, args.secret_dir)
        else:
            install_config(args.server_root, args.secret_dir)
    except (OSError, ValueError) as exc:
        print(f"game runtime secret operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
