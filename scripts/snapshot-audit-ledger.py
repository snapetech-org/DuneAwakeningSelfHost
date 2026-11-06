#!/usr/bin/env python3
"""Capture one internally consistent DASH admin audit-ledger evidence set."""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import audit_ledger


def snapshot(source_database, source_key, source_anchor, destination, attempts=5):
    source_database = pathlib.Path(source_database)
    source_key = pathlib.Path(source_key)
    source_anchor = pathlib.Path(source_anchor)
    destination = pathlib.Path(destination)
    sources = (source_database, source_key, source_anchor)
    if any(not source.is_file() or source.is_symlink() for source in sources):
        raise ValueError("audit ledger snapshot requires regular database, HMAC key, and anchor files")
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    target_database = destination / "audit-ledger.sqlite3"
    target_key = destination / "audit-ledger.hmac.key"
    target_anchor = destination / "audit-ledger.anchor.json"
    targets = (target_database, target_key, target_anchor)
    last_error = None
    attempts = max(1, min(int(attempts), 20))
    for attempt in range(attempts):
        for target in targets:
            target.unlink(missing_ok=True)
        try:
            shutil.copyfile(source_key, target_key)
            shutil.copyfile(source_anchor, target_anchor)
            source = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
            target = sqlite3.connect(target_database)
            try:
                target.execute("pragma mmap_size=1073741824")
                source.backup(target)
                if target.execute("pragma integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("audit ledger snapshot failed SQLite integrity_check")
            finally:
                target.close()
                source.close()
            for target in targets:
                target.chmod(0o600)
            verified = audit_ledger.Store(
                target_database, key_path=target_key, anchor_path=target_anchor
            ).verify()
            if not verified.get("ok"):
                raise RuntimeError(f"audit ledger snapshot verification failed: {verified}")
            return {
                "ok": True,
                "attempts": attempt + 1,
                "events": verified["events"],
                "headSequence": verified["headSequence"],
                "files": [target.name for target in targets],
            }
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.05)
    for target in targets:
        target.unlink(missing_ok=True)
    raise RuntimeError(f"unable to capture a consistent audit ledger snapshot: {last_error}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_database")
    parser.add_argument("source_key")
    parser.add_argument("source_anchor")
    parser.add_argument("destination")
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args(argv)
    result = snapshot(
        args.source_database, args.source_key, args.source_anchor,
        args.destination, attempts=args.attempts,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
