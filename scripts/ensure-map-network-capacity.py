#!/usr/bin/env python3
"""Grow exhausted map UDP pools and reconcile router/host forwarding before starts."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "admin"))
import env_file_store  # noqa: E402


PRODUCTION_HOSTNAME = "kspls0"
RANGE_KEYS = ("GAME_UDP_PORT_RANGE", "IGW_UDP_PORT_RANGE")
ENV_VALUE_KEYS = {*RANGE_KEYS, "DUNE_MAP_NETWORK_AUTOGROW_ENABLED"}
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
RANGE_RE = re.compile(r"\s*(\d+)\s*:\s*(\d+)\s*")


class NetworkCapacityError(RuntimeError):
    pass


def resolve_env_path(env_arg: pathlib.Path | str, *, root: pathlib.Path) -> pathlib.Path:
    """Resolve a regular env file or the one protected production .env link."""
    path = pathlib.Path(env_arg)
    if not path.is_absolute():
        path = root / path
    path = pathlib.Path(os.path.abspath(path))

    if not path.is_symlink():
        try:
            info = path.stat()
        except OSError as exc:
            raise NetworkCapacityError("env file cannot be inspected safely") from exc
        if not stat.S_ISREG(info.st_mode):
            raise NetworkCapacityError("env file must be a regular file")
        return path.resolve(strict=True)

    if path != root / ".env":
        raise NetworkCapacityError("only the standard protected .env link is supported")
    try:
        target = path.resolve(strict=True)
        expected = (pathlib.Path.home() / ".config/dune-selfhost/compose.env").resolve(strict=False)
        if target != expected:
            raise NetworkCapacityError("protected env link must resolve to ~/.config/dune-selfhost/compose.env")
        target_info = target.stat()
        parent_info = target.parent.stat()
    except NetworkCapacityError:
        raise
    except OSError as exc:
        raise NetworkCapacityError("protected env link cannot be resolved safely") from exc
    if not stat.S_ISREG(target_info.st_mode) or target_info.st_uid != os.geteuid():
        raise NetworkCapacityError("protected env target must be a regular file owned by the current user")
    if target_info.st_mode & 0o077:
        raise NetworkCapacityError("protected env target must not be accessible by group or other users")
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o077:
        raise NetworkCapacityError("protected env directory must be private and owned by the current user")
    return target


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise NetworkCapacityError(f"cannot read env file: {path}") from exc
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in ENV_VALUE_KEYS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def parse_port_range(value: str, name: str) -> tuple[int, int]:
    match = RANGE_RE.fullmatch(value)
    if not match:
        raise NetworkCapacityError(f"{name} must use START:END format")
    start, end = (int(part) for part in match.groups())
    if start < 1 or end > 65535 or start > end:
        raise NetworkCapacityError(f"{name} must be an ascending UDP range within 1:65535")
    return start, end


def _range_line_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^(\s*(?:export\s+)?{re.escape(key)}\s*=\s*).*$", re.MULTILINE)


def replace_env_ranges(
    content: str,
    game_range: str,
    igw_range: str,
) -> str:
    """Replace exactly one existing value for each range without touching other lines."""
    replacements = {
        "GAME_UDP_PORT_RANGE": game_range,
        "IGW_UDP_PORT_RANGE": igw_range,
    }
    updated = content
    for key, value in replacements.items():
        pattern = _range_line_pattern(key)
        matches = list(pattern.finditer(updated))
        if len(matches) != 1:
            raise NetworkCapacityError(f"env file must contain exactly one {key} assignment")
        updated = pattern.sub(lambda match, replacement=value: match.group(1) + replacement, updated, count=1)
    return updated


def _atomic_private_write(path: pathlib.Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = pathlib.Path(raw_temp)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def update_env_ranges(
    env_path: pathlib.Path,
    game_range: str,
    igw_range: str,
    *,
    backup_root: pathlib.Path,
) -> pathlib.Path:
    info = env_path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise NetworkCapacityError("env file must be a regular file, not a symlink or special file")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=backup_root))
    os.chmod(backup_dir, 0o700)
    backup_path = backup_dir / env_path.name
    def archive_before_write(original: bytes) -> None:
        descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    try:
        result = env_file_store.update_values(
            env_path,
            (("GAME_UDP_PORT_RANGE", game_range), ("IGW_UDP_PORT_RANGE", igw_range)),
            before_write=archive_before_write,
        )
        if (result["device"], result["inode"]) != (info.st_dev, info.st_ino):
            raise NetworkCapacityError("env file inode changed during update")
    except Exception:
        if not backup_path.exists():
            backup_dir.rmdir()
        raise
    return backup_path


def _write_state(state_path: pathlib.Path, game_range: str, igw_range: str) -> None:
    state = {
        "schema_version": 1,
        "game_port_range": game_range,
        "igw_port_range": igw_range,
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _atomic_private_write(state_path, json.dumps(state, sort_keys=True, indent=2) + "\n")


def _run(command: list[str], *, cwd: pathlib.Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=False, capture_output=True, text=True)


def _verify_production_host() -> str:
    result = subprocess.run(["hostname"], check=False, capture_output=True, text=True)
    if result.returncode:
        raise NetworkCapacityError("could not verify the production hostname")
    actual = result.stdout.strip().split(".", 1)[0]
    if actual != PRODUCTION_HOSTNAME:
        raise NetworkCapacityError(
            f"network auto-growth is enabled for production only; hostname is {actual or 'unknown'}"
        )
    return actual


def _ensure_capacity_locked(env_arg: str, *, root: pathlib.Path | None = None) -> int:
    root = (root or pathlib.Path(__file__).resolve().parents[1]).resolve()
    env_path = resolve_env_path(env_arg, root=root)
    env_values = read_env_file(env_path)

    enabled_value = os.environ.get(
        "DUNE_MAP_NETWORK_AUTOGROW_ENABLED",
        env_values.get("DUNE_MAP_NETWORK_AUTOGROW_ENABLED", "false"),
    ).strip().lower()
    if enabled_value in FALSE_VALUES:
        return 0
    if enabled_value not in TRUE_VALUES:
        raise NetworkCapacityError("DUNE_MAP_NETWORK_AUTOGROW_ENABLED must be true or false")
    _verify_production_host()

    if not all(key in env_values and env_values[key] for key in RANGE_KEYS):
        raise NetworkCapacityError("both game UDP ranges must be set in the env file")
    current_game = parse_port_range(env_values["GAME_UDP_PORT_RANGE"], "GAME_UDP_PORT_RANGE")
    current_igw = parse_port_range(env_values["IGW_UDP_PORT_RANGE"], "IGW_UDP_PORT_RANGE")

    compose_env = os.environ.copy()
    compose_env["DUNE_MAP_AUTO_DISCOVERY_ENABLED"] = "false"
    compose_list = _run(
        [str(root / "scripts" / "compose-files.sh"), str(env_path)],
        cwd=root,
        env=compose_env,
    )
    if compose_list.returncode:
        raise NetworkCapacityError(f"could not resolve base Compose files (exit {compose_list.returncode})")
    base_compose_files = compose_list.stdout.strip()
    if not base_compose_files:
        raise NetworkCapacityError("base Compose file list is empty")

    planner_env = os.environ.copy()
    # The env file is canonical for generated worker port assignments. An
    # inherited shell override could become stale immediately after growth.
    planner_env.pop("GAME_UDP_PORT_RANGE", None)
    planner_env.pop("IGW_UDP_PORT_RANGE", None)
    plan_result = _run(
        [
            sys.executable,
            str(root / "scripts" / "sync-map-workers.py"),
            str(env_path),
            "--root", str(root),
            "--compose-files", base_compose_files,
            "--network-plan",
        ],
        cwd=root,
        env=planner_env,
    )
    if plan_result.returncode:
        raise NetworkCapacityError(f"map port planner failed (exit {plan_result.returncode})")
    try:
        plan = json.loads(plan_result.stdout)
    except json.JSONDecodeError as exc:
        raise NetworkCapacityError("map port planner returned invalid JSON") from exc

    new_game_text = str(plan.get("game_port_range", ""))
    new_igw_text = str(plan.get("igw_port_range", ""))
    new_game = parse_port_range(new_game_text, "planned GAME_UDP_PORT_RANGE")
    new_igw = parse_port_range(new_igw_text, "planned IGW_UDP_PORT_RANGE")
    if new_game[0] > current_game[0] or new_game[1] < current_game[1]:
        raise NetworkCapacityError("planner attempted to shrink the game UDP range")
    if new_igw[0] > current_igw[0] or new_igw[1] < current_igw[1]:
        raise NetworkCapacityError("planner attempted to shrink the IGW UDP range")
    if not (new_game[1] < new_igw[0] or new_igw[1] < new_game[0]):
        raise NetworkCapacityError("planned game and IGW UDP ranges overlap")

    desired_state = (new_game_text, new_igw_text)
    try:
        state_identity = str(env_path.relative_to(root))
    except ValueError:
        state_identity = env_path.name
    state_id = hashlib.sha256(state_identity.encode("utf-8")).hexdigest()[:12]
    state_path = root / "backups" / "map-network-autogrow" / f"state-{state_id}.json"
    env_already_matches = (
        env_values["GAME_UDP_PORT_RANGE"] == new_game_text
        and env_values["IGW_UDP_PORT_RANGE"] == new_igw_text
    )
    # Reconcile both paths on every guarded lifecycle call. The operations are
    # idempotent, and this repairs router/LAN drift or a previously interrupted
    # run even when the allocator does not need a new port range.
    router_result = _run(
        [
            sys.executable,
            str(root / "scripts" / "update-router-map-port-ranges.py"),
            str(env_path),
            "--game-range", new_game_text,
            "--igw-range", new_igw_text,
            "--apply",
        ],
        cwd=root,
    )
    if router_result.returncode:
        detail = router_result.stderr.strip()
        suffix = f": {detail[-500:]}" if detail else ""
        raise NetworkCapacityError(f"router forwarding update failed (exit {router_result.returncode}){suffix}")

    if not env_already_matches:
        backup_path = update_env_ranges(
            env_path,
            new_game_text,
            new_igw_text,
            backup_root=root / "backups" / "map-network-autogrow",
        )
        env_values["GAME_UDP_PORT_RANGE"] = new_game_text
        env_values["IGW_UDP_PORT_RANGE"] = new_igw_text
        print(f"expanded map UDP ranges; env backup saved: {backup_path}")

    lan_env = os.environ.copy()
    lan_env["GAME_UDP_PORT_RANGE"] = new_game_text
    lan_env["IGW_UDP_PORT_RANGE"] = new_igw_text
    lan_result = _run(
        ["bash", str(root / "scripts" / "setup-lan-reflection.sh"), str(env_path)],
        cwd=root,
        env=lan_env,
    )
    if lan_result.returncode:
        raise NetworkCapacityError(f"host LAN reflection update failed (exit {lan_result.returncode})")
    _write_state(state_path, *desired_state)
    if router_result.stdout.strip():
        print(router_result.stdout.strip())

    if plan.get("growth_required"):
        print(
            f"map network capacity expanded for {plan.get('planned_additions', 0)} new worker(s): "
            f"game={new_game_text}, IGW={new_igw_text}"
        )
    else:
        print(f"map network forwarding verified: game={new_game_text}, IGW={new_igw_text}")
    return 0


def ensure_capacity(env_arg: str, *, root: pathlib.Path | None = None) -> int:
    root = (root or pathlib.Path(__file__).resolve().parents[1]).resolve()
    env_path = resolve_env_path(env_arg, root=root)
    values = read_env_file(env_path)
    enabled = os.environ.get(
        "DUNE_MAP_NETWORK_AUTOGROW_ENABLED",
        values.get("DUNE_MAP_NETWORK_AUTOGROW_ENABLED", "false"),
    ).strip().lower()
    if enabled in FALSE_VALUES:
        return 0

    lock_root = root / "backups" / "map-network-autogrow"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_root, 0o700)
    lock_path = lock_root / ".capacity.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            return _ensure_capacity_locked(env_arg, root=root)
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", nargs="?", default=".env")
    args = parser.parse_args(argv)
    return ensure_capacity(args.env_file)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NetworkCapacityError as exc:
        print(f"map network capacity check failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except OSError as exc:
        print(f"map network capacity check failed: local I/O error ({exc})", file=sys.stderr)
        raise SystemExit(2)
