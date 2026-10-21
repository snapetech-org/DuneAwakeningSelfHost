"""Read runtime credentials from protected files or environment values."""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile


def read_secret_env(name: str, default: str = "") -> str:
    """Read NAME_FILE first, failing closed when that path is configured."""
    file_value = os.environ.get(f"{name}_FILE", "").strip()
    if file_value:
        path = pathlib.Path(file_value)
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{name}_FILE must name a regular file")
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read {name}_FILE: {exc.strerror or 'I/O error'}") from None
        if len(raw) > 65536:
            raise ValueError(f"{name}_FILE is too large")
        raw = raw.rstrip(b"\r\n")
        if any(char in raw for char in (b"\x00", b"\n", b"\r")):
            raise ValueError(f"{name}_FILE must contain one line of text")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{name}_FILE must contain UTF-8 text") from None
    value = os.environ.get(name)
    return value if value is not None else default


def write_secret_file(path: pathlib.Path, value: str) -> None:
    """Atomically write one owner-only text credential file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if any(char in value for char in "\x00\r\n"):
        raise ValueError("secret must be one line of text")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
