#!/usr/bin/env python3
"""Stage independent local CVE rebuilds from an operator's image inventory."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {"game", "gateway", "director", "text-router", "db-utils", "rabbitmq", "steamcmd"}


def read_inventory(path):
    rows = []
    names = set()
    for line in path.read_text().splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("Expected NAME COMPONENT SOURCE in inventory")
        name, component, source = fields
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) or name in names:
            raise ValueError(f"Invalid or duplicate inventory name: {name}")
        if component not in COMPONENTS or source.startswith("-"):
            raise ValueError(f"Invalid component or source for {name}")
        if component != "game" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:@{}-]*", source):
            raise ValueError(f"Invalid source image for {name}")
        if component == "game" and source != "@env":
            raise ValueError("Game qualification requires @env as its source")
        names.add(name)
        rows.append((name, component, source))
    if not rows:
        raise ValueError("Empty rebuild inventory")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    rows = read_inventory(args.inventory)
    env_file = args.env_file.resolve()
    tag = ""
    if any("{vendor_tag}" in source for _, _, source in rows):
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "DUNE_IMAGE_TAG":
                tag = value.strip().strip("\"'")
                break
        if not re.fullmatch(r"[0-9]+-[0-9]+-shipping", tag):
            raise ValueError("Missing or invalid DUNE_IMAGE_TAG")
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / "refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        run = Path(tempfile.mkdtemp(prefix=date, dir=output))
        print(f"Candidate evidence: {run}", flush=True)
        results = {}
        for name, component, source in rows:
            if component == "game":
                command = [sys.executable, str(ROOT / "scripts/qualify-game-runtime.py"),
                           str(env_file), "--fresh"]
            else:
                command = [str(ROOT / "scripts/build-local-security-image.sh"), component,
                           source.replace("{vendor_tag}", tag), str(run / name)]
            with (run / f"{name}.log").open("w") as log:
                results[name] = subprocess.run(command, cwd=ROOT, stdout=log,
                                               stderr=subprocess.STDOUT).returncode
            print(f"{name}: exit {results[name]}", flush=True)
        (run / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        return int(any(results.values()))


if __name__ == "__main__":
    sys.exit(main())
