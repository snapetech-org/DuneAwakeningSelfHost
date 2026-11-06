#!/usr/bin/env python3
"""Archive a bounded set of immutable signed capsules through held file handles."""
import contextlib
import os
import pathlib
import re
import stat
import tarfile
import tempfile
import time


def snapshot(source, target, attempts=5):
    source, target = pathlib.Path(source), pathlib.Path(target)
    if source.is_symlink() or not source.is_dir() or target.exists():
        raise ValueError('invalid evidence source or existing archive')
    for attempt in range(attempts):
        temporary = None
        try:
            with contextlib.ExitStack() as handles:
                paths = sorted(source.glob('*.signed.json'))
                if not 1 <= len(paths) <= 1000:
                    raise ValueError('operator evidence backup requires 1..1000 signed capsules')
                opened, total = [], 0
                for path in paths:
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.signed\.json', path.name):
                        raise ValueError('unsafe evidence capsule name')
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    handle = handles.enter_context(os.fdopen(fd, 'rb'))
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 10*1024**2:
                        raise ValueError('evidence capsule must be a bounded regular file')
                    total += info.st_size
                    if total > 100*1024**2:
                        raise ValueError('evidence capsules exceed 100 MiB backup bound')
                    opened.append((path.name, handle, info))
                # All selected inodes are now pinned even if retention unlinks
                # their names. A disappeared name during acquisition retries
                # the entire selection rather than silently omitting evidence.
                fd, temporary = tempfile.mkstemp(prefix='.operator-evidence-', suffix='.partial', dir=target.parent)
                with os.fdopen(fd, 'wb') as output:
                    with tarfile.open(fileobj=output, mode='w:gz') as archive:
                        for name, handle, before in opened:
                            info = archive.gettarinfo(arcname='operator-evidence/'+name, fileobj=handle)
                            archive.addfile(info, handle)
                            after = os.fstat(handle.fileno())
                            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise ValueError('evidence capsule changed while archiving')
                    output.flush(); os.fsync(output.fileno())
                # Exclusive publication also refuses a concurrently created
                # target; only this attempt's temporary file is removed.
                os.link(temporary, target)
                return len(opened)
        except FileNotFoundError:
            if attempt+1 == attempts:
                raise RuntimeError('evidence retention did not stabilize for backup') from None
            time.sleep(0.05)
        finally:
            if temporary is not None:
                pathlib.Path(temporary).unlink(missing_ok=True)
    raise RuntimeError('evidence snapshot attempt bound exhausted')


if __name__ == '__main__':
    import sys
    snapshot(*sys.argv[1:])
