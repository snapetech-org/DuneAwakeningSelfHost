#!/usr/bin/env python3
"""Safely widen only the AsusWRT Dune map UDP forwarding ranges."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile


PORT_RANGE_RE = re.compile(r"\s*(\d+)\s*:\s*(\d+)\s*")
RULE_PATTERNS = {
    "duneA1": re.compile(r"(<duneA1>)(\d+):(\d+)(>[^>]+>>UDP>)"),
    "duneA2": re.compile(r"(<duneA2>)(\d+):(\d+)(>[^>]+>>UDP>)"),
}


class RouterRangeError(ValueError):
    pass


def parse_port_range(value: str, name: str) -> tuple[int, int]:
    match = PORT_RANGE_RE.fullmatch(value)
    if not match:
        raise RouterRangeError(f"{name} must use START:END format")
    start, end = (int(part) for part in match.groups())
    if start < 1 or end > 65535 or start > end:
        raise RouterRangeError(f"{name} must be an ascending UDP range within 1:65535")
    return start, end


def _rule_destination(match: re.Match[str]) -> str:
    # Group 4 starts with `>destination>>UDP>`.
    destination = match.group(4)[1:].split(">", 1)[0]
    if not destination or any(char in destination for char in "\r\n\x00"):
        raise RouterRangeError("router map-forward destination is malformed")
    return destination


def _widen_rule_group(
    current: str,
    rule_name: str,
    desired_range: tuple[int, int],
) -> tuple[str, tuple[int, int], tuple[str, ...]]:
    pattern = RULE_PATTERNS[rule_name]
    matches = list(pattern.finditer(current))
    occurrence_count = current.count(f"<{rule_name}>")
    if not matches:
        if occurrence_count:
            raise RouterRangeError(f"router rule {rule_name} exists but has an unsupported format")
        raise RouterRangeError(f"router rule {rule_name} was not found")
    if occurrence_count != len(matches):
        if occurrence_count:
            raise RouterRangeError(f"router rule {rule_name} exists but has an unsupported format")
        raise RouterRangeError(f"router rule {rule_name} was not found")

    for match in matches:
        start, end = int(match.group(2)), int(match.group(3))
        if start < 1 or end > 65535 or start > end:
            raise RouterRangeError(f"router rule {rule_name} has an invalid port range")
        _rule_destination(match)

    widened = (
        min([desired_range[0], *(int(match.group(2)) for match in matches)]),
        max([desired_range[1], *(int(match.group(3)) for match in matches)]),
    )
    destinations = tuple(_rule_destination(match) for match in matches)

    def replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}{widened[0]}:{widened[1]}{match.group(4)}"

    updated = pattern.sub(replace, current)
    return updated, widened, destinations


def transform_rulelist(
    current: str,
    game_range: tuple[int, int],
    igw_range: tuple[int, int],
) -> tuple[str, dict[str, tuple[int, int]], dict[str, tuple[str, ...]]]:
    """Widen matching map rules without changing destinations or other rules."""
    if not current:
        raise RouterRangeError("router forwarding rule list is empty")
    if not (game_range[1] < igw_range[0] or igw_range[1] < game_range[0]):
        raise RouterRangeError("requested game and IGW ranges overlap")

    updated, actual_game_range, game_destinations = _widen_rule_group(
        current, "duneA1", game_range,
    )
    updated, actual_igw_range, igw_destinations = _widen_rule_group(
        updated, "duneA2", igw_range,
    )
    if not (actual_game_range[1] < actual_igw_range[0] or actual_igw_range[1] < actual_game_range[0]):
        raise RouterRangeError("existing router game and IGW forwarding ranges overlap")
    return (
        updated,
        {"duneA1": actual_game_range, "duneA2": actual_igw_range},
        {"duneA1": game_destinations, "duneA2": igw_destinations},
    )


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if separator:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def _private_backup(backup_dir: pathlib.Path, current: str) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f"map-port-rules-{timestamp}-", suffix=".txt", dir=backup_dir,
    )
    backup = pathlib.Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(current)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        backup.unlink(missing_ok=True)
        raise
    return backup


def _ssh(router: str, remote_command: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "ConnectionAttempts=1", router, remote_command,
            ],
            check=False, capture_output=True, text=True, timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, stdout="", stderr="timed out")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", nargs="?", default=".env")
    parser.add_argument("--game-range", required=True)
    parser.add_argument("--igw-range", required=True)
    parser.add_argument("--router")
    parser.add_argument("--backup-dir", type=pathlib.Path)
    parser.add_argument("--apply", action="store_true", help="commit changed rules and restart router firewall")
    args = parser.parse_args(argv)

    game_range = parse_port_range(args.game_range, "GAME_UDP_PORT_RANGE")
    igw_range = parse_port_range(args.igw_range, "IGW_UDP_PORT_RANGE")
    if not (game_range[1] < igw_range[0] or igw_range[1] < game_range[0]):
        raise RouterRangeError("requested game and IGW ranges overlap")

    env_path = pathlib.Path(args.env_file).resolve()
    env_values = read_env_file(env_path)
    router = args.router or os.environ.get("DUNE_FAILOVER_ROUTER_SSH") or os.environ.get("DUNE_ROUTER_SSH") or env_values.get("DUNE_FAILOVER_ROUTER_SSH") or env_values.get("DUNE_ROUTER_SSH")
    if not router or router.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", router):
        raise RouterRangeError("a valid DUNE_FAILOVER_ROUTER_SSH target is required")

    result = _ssh(router, "nvram get vts_rulelist")
    if result.returncode:
        detail = " ".join((result.stderr or "").split())
        suffix = f": {detail[-400:]}" if detail else ""
        raise RouterRangeError(f"router SSH read failed (exit {result.returncode}){suffix}")
    current = result.stdout.rstrip("\r\n")
    updated, widened_ranges, destinations = transform_rulelist(current, game_range, igw_range)
    changed = updated != current

    root = pathlib.Path(__file__).resolve().parents[1]
    backup_dir = args.backup_dir or (root / "backups" / "router-inspection")
    if not backup_dir.is_absolute():
        backup_dir = (root / backup_dir).resolve()

    if args.apply and changed:
        backup_path = _private_backup(backup_dir, current)
        remote_command = (
            f"nvram set vts_rulelist={shlex.quote(updated)}"
            " && nvram commit && service restart_firewall"
        )
        write_result = _ssh(router, remote_command)
        if write_result.returncode:
            detail = " ".join((write_result.stderr or "").split())
            suffix = f": {detail[-400:]}" if detail else ""
            raise RouterRangeError(
                f"router update failed (exit {write_result.returncode}){suffix}; private backup saved at {backup_path}"
            )
        verify = _ssh(router, "nvram get vts_rulelist")
        if verify.returncode:
            detail = " ".join((verify.stderr or "").split())
            suffix = f": {detail[-400:]}" if detail else ""
            raise RouterRangeError(
                f"router update readback failed (exit {verify.returncode}){suffix}; private backup saved at {backup_path}"
            )
        if verify.stdout.rstrip("\r\n") != updated:
            raise RouterRangeError(
                f"router update readback did not exactly match the port-only change; private backup saved at {backup_path}"
            )

    action = "widened" if args.apply and changed else "already covers requested ranges" if not changed else "would widen"
    print(
        f"router map forwarding {action}: game={widened_ranges['duneA1'][0]}:{widened_ranges['duneA1'][1]} "
        f"IGW={widened_ranges['duneA2'][0]}:{widened_ranges['duneA2'][1]}; "
        f"destination-preservation checks passed ({len(destinations['duneA1'])} game, {len(destinations['duneA2'])} IGW rules)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RouterRangeError as exc:
        print(f"router map port-range update failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except OSError as exc:
        print(f"router map port-range update failed: local I/O error ({exc})", file=sys.stderr)
        raise SystemExit(2)
