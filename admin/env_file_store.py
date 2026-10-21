#!/usr/bin/env python3
"""Durable, inode-preserving updates for a bind-mounted dotenv file."""

from __future__ import annotations

import fcntl
import os
import pathlib
import re
import stat
from contextlib import contextmanager
from typing import Callable


DEFAULT_MAX_BYTES = 4 * 1024 * 1024
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class EnvFileError(ValueError):
    """The env file or requested update is unsafe or invalid."""


def _validate_payload(payload: bytes, max_bytes: int) -> None:
    if len(payload) > max_bytes:
        raise EnvFileError(f"env file exceeds the {max_bytes}-byte limit")
    if b"\x00" in payload:
        raise EnvFileError("env file must not contain NUL bytes")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileError("env file must be valid UTF-8") from exc


def _validate_updates(updates) -> list[tuple[str, str]]:
    rendered = []
    seen = set()
    for raw_key, raw_value in updates:
        key = str(raw_key)
        value = str(raw_value)
        if not KEY_PATTERN.fullmatch(key):
            raise EnvFileError(f"invalid env key: {key!r}")
        if key in seen:
            raise EnvFileError(f"duplicate requested env key: {key}")
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise EnvFileError(f"env value for {key} contains a forbidden control character")
        rendered.append((key, value))
        seen.add(key)
    if not rendered:
        raise EnvFileError("at least one env update is required")
    return rendered


@contextmanager
def _locked_file(path: pathlib.Path, max_bytes: int):
    path = pathlib.Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise EnvFileError(f"env file does not exist: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise EnvFileError(f"env file must be an existing regular file, not a symlink: {path}")

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EnvFileError(f"cannot safely open env file: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EnvFileError(f"env file changed while it was being opened: {path}")
        if opened.st_size > max_bytes:
            raise EnvFileError(f"env file exceeds the {max_bytes}-byte limit")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = os.fstat(descriptor)
        if locked.st_size > max_bytes:
            raise EnvFileError(f"env file exceeds the {max_bytes}-byte limit")
        yield descriptor, locked
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_locked(descriptor: int, size: int, max_bytes: int) -> bytes:
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    payload = b"".join(chunks)
    _validate_payload(payload, max_bytes)
    return payload


def _write_locked(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.pwrite(descriptor, payload[offset:], offset)
        if written <= 0:
            raise OSError("env file write made no progress")
        offset += written
    os.ftruncate(descriptor, len(payload))
    os.fsync(descriptor)
    verified = _read_locked(descriptor, len(payload), max(len(payload), 1))
    if verified != payload:
        raise OSError("env file verification failed after write")


def replace_contents(path, content, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Replace content through the existing inode, retaining mode/owner and mounts."""
    if max_bytes < 1:
        raise EnvFileError("max_bytes must be positive")
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    _validate_payload(payload, max_bytes)
    with _locked_file(pathlib.Path(path), max_bytes) as (descriptor, opened):
        current = _read_locked(descriptor, opened.st_size, max_bytes)
        if current != payload:
            _write_locked(descriptor, payload)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("env file inode changed during write")
        return {
            "changed": current != payload,
            "bytes": len(payload),
            "device": after.st_dev,
            "inode": after.st_ino,
        }


def update_values(
    path,
    updates,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    before_write: Callable[[bytes], None] | None = None,
) -> dict:
    """Set dotenv keys under one exclusive lock while preserving the file inode."""
    requested = _validate_updates(updates)
    wanted = dict(requested)
    with _locked_file(pathlib.Path(path), max_bytes) as (descriptor, opened):
        original = _read_locked(descriptor, opened.st_size, max_bytes)
        lines = original.decode("utf-8").splitlines()
        seen = set()
        rendered = []
        for line in lines:
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                rendered.append(line)
                continue
            candidate = line.split("=", 1)[0].strip()
            if candidate not in wanted:
                rendered.append(line)
                continue
            if candidate not in seen:
                rendered.append(f"{candidate}={wanted[candidate]}")
                seen.add(candidate)
        for key, value in requested:
            if key not in seen:
                rendered.append(f"{key}={value}")
        payload = ("\n".join(rendered) + "\n").encode("utf-8")
        _validate_payload(payload, max_bytes)
        if original != payload:
            if before_write is not None:
                before_write(original)
            _write_locked(descriptor, payload)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("env file inode changed during update")
        return {
            "changed": original != payload,
            "keys": len(requested),
            "bytes": len(payload),
            "device": after.st_dev,
            "inode": after.st_ino,
        }
