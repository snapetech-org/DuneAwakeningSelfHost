#!/usr/bin/env python3
"""Run the isolated RabbitMQ restore drill with the deployed broker image."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


INSPECT_FORMAT = (
    '{"image":{{json .Image}},"running":{{json .State.Running}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
)


def deployed_image(project):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("Invalid Compose project")
    services = {"admin-rmq", "game-rmq"}
    names = [f"{project}-{service}-1" for service in sorted(services)]
    result = subprocess.run(
        ["docker", "inspect", "--type", "container", "--format", INSPECT_FORMAT, *names],
        check=True, capture_output=True, text=True, timeout=15,
    )
    brokers = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if len(brokers) != 2 or {b.get("service") for b in brokers} != services:
        raise ValueError("Both expected brokers must be present")
    for broker in brokers:
        if broker.get("project") != project or broker.get("running") is not True:
            raise ValueError("Broker identity or running state does not match")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", broker.get("image", "")):
            raise ValueError("Broker image must be an immutable image ID")
    images = {b["image"] for b in brokers}
    if len(images) != 1:
        raise ValueError("Broker images differ; recovery needs an explicit compatibility review")
    return images.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--host-workspace", type=Path)
    parser.add_argument("--project", default="dune_server")
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    script = workspace / "scripts/rabbitmq-restore-drill.py"
    if not script.is_file():
        raise ValueError("RabbitMQ restore-drill script is missing")
    host_workspace = args.host_workspace or workspace
    if not host_workspace.is_absolute():
        raise ValueError("Host workspace must be absolute")
    image = deployed_image(args.project)
    # The existing drill owns locking, backup validation, isolation and cleanup.
    # This wrapper only inspects live brokers; it never starts or stops them.
    os.execv(sys.executable, [
        sys.executable, str(script), "--workspace", str(workspace),
        "--host-workspace", str(host_workspace), "--image", image,
    ])


if __name__ == "__main__":
    main()
