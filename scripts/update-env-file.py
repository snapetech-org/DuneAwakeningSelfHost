#!/usr/bin/env python3
"""CLI for locked, inode-preserving dotenv updates."""

from __future__ import annotations

import argparse
import os
import pathlib
import stat
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import env_file_store  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(
        description="Update an existing dotenv file without replacing its bind-mounted inode."
    )
    parser.add_argument("env_file", type=pathlib.Path)
    parser.add_argument(
        "--allow-protected-link",
        action="store_true",
        help="follow only the standard private ~/.config/dune-selfhost/compose.env link",
    )
    parser.add_argument(
        "--set", dest="updates", action="append", nargs=2, metavar=("KEY", "VALUE"), required=True,
        help="set one key; repeat to update multiple keys under one lock",
    )
    parser.add_argument("--max-bytes", type=int, default=env_file_store.DEFAULT_MAX_BYTES)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def resolve_env_file(path: pathlib.Path, *, allow_protected_link: bool) -> pathlib.Path:
    """Resolve the one supported external dotenv link without weakening the store."""
    if not path.is_symlink():
        return path
    if not allow_protected_link:
        return path

    try:
        target = path.resolve(strict=True)
        expected = (pathlib.Path.home() / ".config/dune-selfhost/compose.env").resolve(strict=True)
        target_stat = target.stat()
        parent_stat = target.parent.stat()
    except OSError as exc:
        raise env_file_store.EnvFileError(f"protected env link cannot be resolved safely: {path}") from exc

    if target != expected:
        raise env_file_store.EnvFileError(
            "protected env link must resolve exactly to ~/.config/dune-selfhost/compose.env"
        )
    if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_uid != os.geteuid():
        raise env_file_store.EnvFileError("protected env target must be a regular file owned by the current user")
    if target_stat.st_mode & 0o077:
        raise env_file_store.EnvFileError("protected env target must not be accessible by group or other users")
    if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o077:
        raise env_file_store.EnvFileError("protected env directory must be private and owned by the current user")
    return target


def main():
    args = parse_args()
    try:
        env_file = resolve_env_file(args.env_file, allow_protected_link=args.allow_protected_link)
        result = env_file_store.update_values(env_file, args.updates, max_bytes=args.max_bytes)
    except (env_file_store.EnvFileError, OSError) as exc:
        print(f"env update refused: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        state = "updated" if result["changed"] else "unchanged"
        print(
            f"{state} {args.env_file}: keys={result['keys']} bytes={result['bytes']} "
            f"inode={result['device']}:{result['inode']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
