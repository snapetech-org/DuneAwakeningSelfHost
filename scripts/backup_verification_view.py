"""Private, bounded local read views for immutable recovery-point verification."""
from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

MAX_BYTES = 8 * 1024**3
MAX_FILES = 4096
CHUNK = 1024**2


def identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _walk_error(error):
    raise error


def inventory(root):
    files = {}
    directories = 0
    total = 0
    for directory, children, names in os.walk(root, followlinks=False, onerror=_walk_error):
        directories += 1
        if directories + len(files) > MAX_FILES:
            raise ValueError("backup verification inventory exceeds its file bound")
        for name in children:
            if pathlib.Path(directory, name).is_symlink():
                raise ValueError("backup verification refuses symbolic links")
        for name in names:
            path = pathlib.Path(directory, name)
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("backup verification requires regular files")
            # SQLite readers can create these transient indexes/empty journals.
            # A nonempty WAL is recovery data and must be copied and checked.
            if name.endswith(".sqlite3-shm") or (name.endswith(".sqlite3-wal") and metadata.st_size == 0):
                continue
            total += metadata.st_size
            if total > MAX_BYTES or directories + len(files) >= MAX_FILES:
                raise ValueError("backup verification copy exceeds its size bound")
            files[path.relative_to(root)] = identity(metadata)
    return files, total


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            value.update(chunk)
    return value.digest()


@contextlib.contextmanager
def local_read_view(source, *, scratch_root=None):
    source = pathlib.Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("backup verification requires a real recovery-point directory")
    source = source.resolve()
    before, total = inventory(source)
    scratch = pathlib.Path(scratch_root or os.environ.get("DUNE_BACKUP_VERIFICATION_SCRATCH_ROOT", "/var/tmp"))
    if scratch.resolve().is_relative_to(source):
        raise ValueError("backup verification scratch must be outside the recovery point")
    if not scratch.is_dir():
        raise ValueError("backup verification scratch directory is unavailable")
    space = os.statvfs(scratch)
    if space.f_bavail * space.f_frsize < total + 1024**3:
        raise ValueError("insufficient local space for backup verification plus reserve")
    with tempfile.TemporaryDirectory(prefix="dune-backup-verify-", dir=scratch) as directory:
        target = pathlib.Path(directory) / source.name
        target.mkdir(mode=0o700)
        hashes = {}
        for relative, expected in before.items():
            path = source / relative
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as reader:
                if identity(os.fstat(reader.fileno())) != expected:
                    raise ValueError("backup changed before its verification copy")
                output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                value = hashlib.sha256()
                with os.fdopen(output, "wb") as writer:
                    count = 0
                    while chunk := reader.read(CHUNK):
                        count += len(chunk)
                        if count > expected[2]:
                            raise ValueError("backup grew during its verification copy")
                        writer.write(chunk)
                        value.update(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                if count != expected[2] or identity(os.fstat(reader.fileno())) != expected:
                    raise ValueError("backup changed during its verification copy")
                hashes[relative] = value.digest()
        if inventory(source)[0] != before:
            raise ValueError("backup inventory changed during its verification copy")
        yield target
        if inventory(source)[0] != before:
            raise ValueError("backup changed during verification")
        if set(inventory(target)[0]) != set(before):
            raise ValueError("verifier changed the recovery-file inventory in its local read view")
        for relative, expected in hashes.items():
            if digest(target / relative) != expected:
                raise ValueError("verifier modified recovery data in its local read view")


def main():
    if len(sys.argv) != 4 or sys.argv[1] != "--script":
        raise SystemExit("usage: backup_verification_view.py --script VERIFIER BACKUP_DIR")
    script = pathlib.Path(sys.argv[2]).resolve()
    if script != pathlib.Path(__file__).with_name("verify-backup.sh").resolve():
        raise SystemExit("only the native backup verifier is permitted")
    with local_read_view(sys.argv[3]) as view:
        environment = dict(os.environ, DUNE_BACKUP_VERIFY_VIEW_ACTIVE="true")
        # Wait for every native verifier consumer before the private view exits.
        return subprocess.run(["bash", str(script), str(view)], env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"backup verification local view failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
