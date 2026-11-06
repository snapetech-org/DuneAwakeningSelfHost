#!/usr/bin/env python3
import ipaddress
import json
import os
import subprocess
import sys


def compose_config(env_file: str, compose_files: list[str]) -> dict:
    cmd = ["docker", "compose"]
    for compose_file in compose_files:
        cmd.extend(["-f", compose_file])
    cmd.extend(["--env-file", env_file, "config", "--format", "json"])
    return json.loads(subprocess.check_output(cmd, text=True))


def main() -> int:
    env_file = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ENV_FILE", ".env.example")
    compose_files = [
        item
        for item in os.environ.get("COMPOSE_FILES", "compose.yaml:compose.allmaps.yaml").split(":")
        if item
    ]
    config = compose_config(env_file, compose_files)
    networks = config.get("networks", {})
    default_network = networks.get("default", {})
    ipam_config = (default_network.get("ipam") or {}).get("config") or []
    dynamic_ranges = [
        ipaddress.ip_network(item["ip_range"])
        for item in ipam_config
        if item.get("ip_range")
    ]

    seen: dict[str, str] = {}
    errors: list[str] = []
    for service, service_config in sorted(config.get("services", {}).items()):
        # Host-networked and network-isolated helpers cannot consume or collide
        # with an address from the project's static container subnet.
        if service_config.get("network_mode") in {"host", "none"}:
            continue
        service_networks = service_config.get("networks") or {}
        default_attachment = service_networks.get("default")
        if default_attachment is None:
            errors.append(f"{service}: missing default network attachment")
            continue
        ip_value = default_attachment.get("ipv4_address")
        if not ip_value:
            errors.append(f"{service}: missing default network ipv4_address")
            continue
        ip_addr = ipaddress.ip_address(ip_value)
        if ip_value in seen:
            errors.append(f"{service}: duplicates {ip_value} already used by {seen[ip_value]}")
        seen[ip_value] = service
        if any(ip_addr in dynamic_range for dynamic_range in dynamic_ranges):
            errors.append(f"{service}: fixed IP {ip_value} is inside dynamic ip_range")

    if errors:
        print("static compose IP check failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    print(f"static compose IP check passed: {len(seen)} services pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
