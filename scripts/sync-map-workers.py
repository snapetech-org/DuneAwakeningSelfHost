#!/usr/bin/env python3
"""Generate Compose workers for installed maps in Funcom's world template.

The official world template is the routing roster; the cooked package index is
the asset roster. Maps already provided by Compose are left untouched. Missing
maps with cooked .umap assets receive new partition IDs, ports, and static IPs.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised on minimally provisioned hosts
    raise SystemExit("PyYAML is required for automatic map discovery (python3-yaml)") from exc


DEFAULT_WORLD_TEMPLATE = pathlib.Path(
    "/home/keith/dune-steamcmd-server/scripts/setup/templates/world-template.yaml"
)
DEFAULT_OVERLAY = pathlib.Path("backups/admin-panel/compose.auto-maps.yaml")
DEFAULT_CACHE = pathlib.Path("backups/admin-panel/map-auto-discovery-cache.json")
ASSET_INDEX_BINARY = pathlib.Path("scripts/map-asset-index/bin/dune-map-asset-index")
ASSET_INDEX_ROOT = "/home/dune/server/DuneSandbox/Content/Paks"
MAP_SERVICE_TEMPLATE = "dungeon-thepit"
DB_MAP_RE = re.compile(r"^[A-Za-z0-9_]+$")
SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
PARTITION_RE = re.compile(r"^-PartitionIndex=(\d+)$")
PORT_RE = re.compile(r"^-(Port|IGWPort)=(\d+)$")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
DEFAULT_GAME_PORT_RANGE = (7777, 7871)
DEFAULT_IGW_PORT_RANGE = (7888, 7982)
DEFAULT_GAME_PORT_MIN = 7000
DEFAULT_IGW_PORT_MAX = 9000
DEFAULT_PORT_GROWTH_STEP = 64


class DiscoveryError(ValueError):
    pass


class ComposeSafeLoader(yaml.SafeLoader):
    """PyYAML loader that treats Compose's merge-control tags as their values."""


def _construct_compose_tag(loader, _tag_suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


ComposeSafeLoader.add_multi_constructor("!", _construct_compose_tag)


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
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _read_yaml(path: pathlib.Path, *, compose: bool = False) -> dict:
    loader = ComposeSafeLoader if compose else yaml.SafeLoader
    try:
        parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    except (OSError, yaml.YAMLError) as exc:
        raise DiscoveryError(f"cannot parse YAML file {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DiscoveryError(f"expected a YAML object in {path}")
    return parsed


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def extract_template_maps(template: dict) -> list[dict]:
    """Find enabled (map, dimension) entries in the official template."""
    found: dict[tuple[str, int], dict] = {}

    def visit(value) -> None:
        if isinstance(value, dict):
            map_name = value.get("map")
            partitions = value.get("partitions")
            if isinstance(map_name, str) and isinstance(partitions, list):
                if not DB_MAP_RE.fullmatch(map_name):
                    raise DiscoveryError(f"invalid map name in world template: {map_name!r}")
                for partition in partitions:
                    if not isinstance(partition, dict) or _truthy(partition.get("disable")):
                        continue
                    try:
                        dimension = int(partition.get("dimension", 0))
                    except (TypeError, ValueError) as exc:
                        raise DiscoveryError(
                            f"{map_name}: invalid partition dimension {partition.get('dimension')!r}"
                        ) from exc
                    if dimension < 0:
                        raise DiscoveryError(f"{map_name}: dimension must be non-negative")
                    key = (map_name, dimension)
                    row = {
                        "map": map_name,
                        "dimension_index": dimension,
                        "template_partition_id": partition.get("id"),
                    }
                    previous = found.get(key)
                    if previous and previous != row:
                        raise DiscoveryError(
                            f"world template repeats {map_name}/{dimension} with conflicting partitions"
                        )
                    found[key] = row
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(template)
    return list(found.values())


def _tokens(command) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    if isinstance(command, str):
        return shlex.split(command)
    return []


def _labels(value) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        result = {}
        for raw in value:
            key, separator, item = str(raw).partition("=")
            if separator:
                result[key] = item
        return result
    return {}


def _map_asset(tokens: list[str]) -> tuple[str | None, str | None]:
    candidates = []
    for token in tokens:
        if token.startswith("-Map="):
            candidates.append(token.partition("=")[2])
        elif token.startswith("/Game/"):
            candidates.append(token)
    if len(candidates) > 1:
        raise DiscoveryError("map worker template contains multiple cooked map arguments")
    if not candidates:
        return None, None
    asset = candidates[0]
    object_name = asset.rsplit("/", 1)[-1].rpartition(".")[2]
    if not object_name or not DB_MAP_RE.fullmatch(object_name):
        raise DiscoveryError(f"invalid cooked map asset argument: {asset!r}")
    return asset, object_name


def _service_map(service_name: str, service: dict) -> dict | None:
    tokens = _tokens(service.get("command"))
    partitions = [match for token in tokens if (match := PARTITION_RE.fullmatch(token))]
    if not partitions:
        return None
    if len(partitions) != 1:
        raise DiscoveryError(f"{service_name}: expected exactly one partition argument")
    labels = _labels(service.get("labels"))
    asset_path, package_map_name = _map_asset(tokens)
    map_name = labels.get("dune.map.database-name") or package_map_name
    if not map_name or not DB_MAP_RE.fullmatch(map_name):
        raise DiscoveryError(f"{service_name}: missing or invalid database map name")
    try:
        dimension = int(labels.get("dune.map.dimension-index", "0"))
        partition_id = int(partitions[0].group(1))
    except ValueError as exc:
        raise DiscoveryError(f"{service_name}: invalid partition metadata") from exc
    ports = {match.group(1): int(match.group(2)) for token in tokens if (match := PORT_RE.fullmatch(token))}
    network_ips = []
    networks = service.get("networks") or {}
    if isinstance(networks, dict):
        network_ips = [
            str(details["ipv4_address"])
            for details in networks.values()
            if isinstance(details, dict) and details.get("ipv4_address")
        ]
    return {
        "service": service_name,
        "map": map_name,
        "dimension_index": dimension,
        "partition_id": partition_id,
        "asset_path": asset_path,
        "port": ports.get("Port"),
        "igw_port": ports.get("IGWPort"),
        "ipv4_addresses": network_ips,
        "auto_discovered": _truthy(labels.get("dune.map.auto-discovered")),
    }


def _services_from_config(config: dict) -> dict[str, dict]:
    services = config.get("services")
    if not isinstance(services, dict):
        raise DiscoveryError("Compose model has no services object")
    return {str(name): service for name, service in services.items() if isinstance(service, dict)}


def _host_published_ports(config: dict) -> set[int]:
    ports: set[int] = set()
    for service in _services_from_config(config).values():
        for port in service.get("ports") or []:
            if isinstance(port, dict) and port.get("published") is not None:
                try:
                    ports.add(int(port["published"]))
                except (TypeError, ValueError):
                    continue
    return ports


def _runtime_network_ips(config: dict, runtime: str) -> set[str]:
    networks = config.get("networks") or {}
    default_network = networks.get("default") if isinstance(networks, dict) else None
    network_name = default_network.get("name") if isinstance(default_network, dict) else None
    if not network_name:
        return set()
    result = subprocess.run(
        [runtime, "network", "inspect", str(network_name), "--format", "{{range .Containers}}{{.IPv4Address}}{{println}}{{end}}"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode:
        return set()
    addresses = set()
    for line in result.stdout.splitlines():
        address = line.strip().split("/", 1)[0]
        try:
            ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            continue
        addresses.add(address)
    return addresses


def _host_listening_udp_ports() -> set[int]:
    result = subprocess.run(
        ["ss", "-H", "-lun"], check=False, capture_output=True, text=True,
    )
    if result.returncode:
        return set()
    ports = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields:
            match = re.search(r":(\d+)$", field)
            if match:
                ports.add(int(match.group(1)))
                break
    return ports


def _slug(map_name: str, dimension: int) -> str:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", map_name)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", separated).strip("-").lower()
    if dimension:
        slug += f"-d{dimension}"
    if not slug or not SERVICE_RE.fullmatch(slug):
        raise DiscoveryError(f"cannot derive a Compose service name from {map_name!r}")
    return slug


def _parse_port_range(value: str | None, name: str, default: tuple[int, int]) -> tuple[int, int]:
    if value is None or not value.strip():
        return default
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", value)
    if not match:
        raise DiscoveryError(f"{name} must use the START:END format")
    start, end = (int(part) for part in match.groups())
    if start < 1 or end > 65535 or start > end:
        raise DiscoveryError(f"{name} must be an ascending UDP port range within 1:65535")
    return start, end


def _next_port(candidate: int, occupied: set[int], *, end: int, range_name: str) -> int:
    while candidate in occupied and candidate <= end:
        candidate += 1
    if candidate < 1 or candidate > end:
        raise DiscoveryError(f"no free UDP port remains in {range_name}")
    occupied.add(candidate)
    return candidate


def _next_game_port(
    candidate: int,
    occupied_ports: set[int],
    *,
    start: int,
    end: int,
) -> int:
    # Prefer the unused tail after the current farm. If there are holes (for
    # example, a removed worker or a host process using one port), use those
    # before declaring the pool exhausted and asking the guarded lifecycle
    # helper to widen it.
    preferred_start = min(max(candidate, start), end + 1)
    for candidates in (range(preferred_start, end + 1), range(start, preferred_start)):
        for port in candidates:
            if port not in occupied_ports:
                occupied_ports.add(port)
                return port
    raise DiscoveryError("no free UDP port remains in GAME_UDP_PORT_RANGE")


def _parse_int_setting(value: str | None, name: str, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or not value.strip():
        return default
    if not re.fullmatch(r"\s*\d+\s*", value):
        raise DiscoveryError(f"{name} must be an integer")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise DiscoveryError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _validate_port_pools(
    game_port_range: tuple[int, int],
    igw_port_range: tuple[int, int],
) -> None:
    game_start, game_end = game_port_range
    igw_start, igw_end = igw_port_range
    if not (game_end < igw_start or igw_end < game_start):
        raise DiscoveryError("GAME_UDP_PORT_RANGE and IGW_UDP_PORT_RANGE must not overlap")


def _next_ip(start_after: list[str], used: set[str], network_config: dict | None) -> str:
    interfaces = []
    for raw in start_after:
        try:
            interfaces.append(ipaddress.ip_interface(raw if "/" in raw else f"{raw}/24"))
        except ValueError as exc:
            raise DiscoveryError(f"invalid existing map-worker IP: {raw!r}") from exc
    if interfaces:
        network = interfaces[0].network
        if any(interface.network != network for interface in interfaces):
            raise DiscoveryError("map workers use more than one static IPv4 subnet")
        next_address = max(int(interface.ip) for interface in interfaces) + 1
    else:
        subnet = None
        if isinstance(network_config, dict):
            ipam = network_config.get("ipam") or {}
            entries = ipam.get("config") or []
            if entries and isinstance(entries[0], dict):
                subnet = entries[0].get("subnet")
        if not subnet:
            raise DiscoveryError("cannot determine the Compose IPv4 subnet for new map workers")
        network = ipaddress.ip_network(subnet, strict=False)
        next_address = int(network.network_address) + 1
    for integer in range(next_address, int(network.broadcast_address)):
        address = ipaddress.IPv4Address(integer)
        text = str(address)
        if text in used:
            continue
        if not isinstance(network_config, dict):
            continue
        ipam = network_config.get("ipam") or {}
        dynamic_ranges = [
            ipaddress.ip_network(item["ip_range"], strict=False)
            for item in ipam.get("config", [])
            if isinstance(item, dict) and item.get("ip_range")
        ]
        if any(address in dynamic for dynamic in dynamic_ranges):
            continue
        return text
    raise DiscoveryError(f"no free static IPv4 address remains in {network}")


def _asset_entries(raw: str, *, required_map_names: set[str] | None = None) -> dict[str, str]:
    assets: dict[str, str] = {}
    required = set(required_map_names) if required_map_names is not None else None
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        map_name, separator, asset_path = line.partition("\t")
        if not separator:
            raise DiscoveryError(f"invalid map asset index line {line_number}")
        if required is not None and map_name not in required:
            continue
        if not DB_MAP_RE.fullmatch(map_name) or not asset_path.startswith("/Game/"):
            raise DiscoveryError(f"invalid map asset index line {line_number}")
        previous = assets.get(map_name)
        if previous and previous != asset_path:
            raise DiscoveryError(f"multiple cooked map assets share package name {map_name}")
        assets[map_name] = asset_path
    return assets


def _expand_env(value: str, env_values: dict[str, str]) -> str:
    def replace(match):
        key, fallback = match.group(1), match.group(2)
        if key in env_values and env_values[key] != "":
            return env_values[key]
        if key in os.environ and os.environ[key] != "":
            return os.environ[key]
        return fallback or ""

    return ENV_RE.sub(replace, value)


def _image_for_scan(raw_image: str, env_values: dict[str, str], runtime: str) -> tuple[str, str]:
    requested_image = _expand_env(raw_image, env_values)
    if "${" in requested_image or not requested_image:
        raise DiscoveryError("DUNE_IMAGE_TAG is required to index cooked map assets")
    inspected = subprocess.run(
        [runtime, "image", "inspect", "--format", "{{.Id}}", requested_image],
        check=False, capture_output=True, text=True,
    )
    if inspected.returncode == 0 and inspected.stdout.strip():
        return requested_image, inspected.stdout.strip()

    # During a tag rollout, the desired image may not have been pulled yet.
    # Scan the image backing an already-running Survival worker; the new map
    # will become eligible automatically once the updated image is available.
    running = subprocess.run(
        [runtime, "ps", "--filter", "label=com.docker.compose.service=survival", "--format", "{{.ID}}"],
        check=False, capture_output=True, text=True,
    )
    for container_id in running.stdout.splitlines():
        container_id = container_id.strip()
        if not container_id:
            continue
        result = subprocess.run(
            [runtime, "inspect", "--format", "{{.Image}}", container_id],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            image_id = result.stdout.strip()
            return image_id, image_id
    raise DiscoveryError(
        f"neither the configured map image nor a running Survival worker is available for indexing"
    )


def _scan_assets(runtime: str, image: str, binary: pathlib.Path) -> tuple[str, str]:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise DiscoveryError(
            f"map asset indexer is missing or not executable: {binary}; "
            "build it with scripts/build-map-asset-index.sh"
        )
    command = [
        runtime, "run", "--rm", "--network", "none",
        "--volume", f"{binary.resolve()}:/usr/local/bin/dune-map-asset-index:ro",
        "--entrypoint", "/usr/local/bin/dune-map-asset-index",
        image, ASSET_INDEX_ROOT,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        # Do not echo container output; it can contain command-line details.
        raise DiscoveryError(f"cooked map asset scan failed (exit {result.returncode})")
    return result.stdout, result.stderr


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_assets(cache_path: pathlib.Path, fingerprint: str) -> dict[str, str] | None:
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cache, dict):
        return None
    if cache.get("fingerprint") != fingerprint or not isinstance(cache.get("assets"), dict):
        return None
    try:
        return {
            str(name): str(asset)
            for name, asset in cache["assets"].items()
            if DB_MAP_RE.fullmatch(str(name)) and str(asset).startswith("/Game/")
        }
    except (TypeError, AttributeError):
        return None


def _write_json_atomic(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = pathlib.Path(handle.name)
            handle.write(json.dumps(data, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o644)
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def collect_existing_workers(
    config: dict,
) -> tuple[dict[tuple[str, int], dict], set[int], set[int], set[int], set[str]]:
    workers = {}
    partition_ids: set[int] = set()
    occupied_game_ports: set[int] = set()
    occupied_igw_ports: set[int] = set()
    worker_ips: set[str] = set()
    for service_name, service in _services_from_config(config).items():
        row = _service_map(service_name, service)
        if not row:
            continue
        key = (row["map"], row["dimension_index"])
        if key in workers:
            raise DiscoveryError(f"multiple Compose workers serve {key[0]}/{key[1]}")
        if row["partition_id"] in partition_ids:
            raise DiscoveryError(f"duplicate Compose partition id {row['partition_id']}")
        workers[key] = row
        partition_ids.add(row["partition_id"])
        if row["port"] is not None:
            occupied_game_ports.add(row["port"])
        if row["igw_port"] is not None:
            occupied_igw_ports.add(row["igw_port"])
        worker_ips.update(row["ipv4_addresses"])
    return workers, partition_ids, occupied_game_ports, occupied_igw_ports, worker_ips


def plan_new_workers(
    template_maps: list[dict],
    existing_workers: dict[tuple[str, int], dict],
    assets: dict[str, str],
    partition_ids: set[int],
    occupied_game_ports: set[int],
    occupied_igw_ports: set[int],
    worker_ips: set[str],
    *,
    all_used_ips: set[str],
    occupied_host_ports: set[int],
    network_config: dict,
    game_port_range: tuple[int, int] = DEFAULT_GAME_PORT_RANGE,
    igw_port_range: tuple[int, int] = DEFAULT_IGW_PORT_RANGE,
) -> tuple[list[dict], list[str]]:
    _validate_port_pools(game_port_range, igw_port_range)
    next_partition = max(partition_ids, default=0) + 1
    next_game_port = max(game_port_range[0], max(occupied_game_ports, default=game_port_range[0] - 1) + 1)
    next_igw_port = max(igw_port_range[0], max(occupied_igw_ports, default=igw_port_range[0] - 1) + 1)
    allocated_game_ports = set(occupied_game_ports)
    occupied_ports = allocated_game_ports | set(occupied_igw_ports) | set(occupied_host_ports)
    used_ips = set(all_used_ips) | set(worker_ips)
    worker_ip_interfaces = sorted(
        worker_ips,
        key=lambda raw: int(ipaddress.ip_interface(raw if "/" in raw else f"{raw}/24").ip),
    )
    planned = []
    pending_assets = []
    used_services = {row["service"] for row in existing_workers.values()}
    template_by_key = {
        (item["map"], item["dimension_index"]): item
        for item in template_maps
    }

    # Keep generated worker assignments stable across template reorderings and
    # process restarts. Existing generated IDs are also the DB row identities.
    for key, current in existing_workers.items():
        if not current.get("auto_discovered"):
            continue
        if current["port"] not in range(game_port_range[0], game_port_range[1] + 1):
            raise DiscoveryError(
                f"generated worker {current['service']} game port is outside GAME_UDP_PORT_RANGE"
            )
        if current["igw_port"] not in range(igw_port_range[0], igw_port_range[1] + 1):
            raise DiscoveryError(
                f"generated worker {current['service']} IGW port is outside IGW_UDP_PORT_RANGE"
            )
        template_row = template_by_key.get(key) or {
            "map": current["map"],
            "dimension_index": current["dimension_index"],
            "template_partition_id": None,
        }
        planned.append({
            **template_row,
            "service": current["service"],
            "asset_path": assets.get(current["map"]) or current["asset_path"],
            "partition_id": current["partition_id"],
            "port": current["port"],
            "igw_port": current["igw_port"],
            "ipv4_address": current["ipv4_addresses"][0] if current["ipv4_addresses"] else None,
        })

    for item in template_maps:
        key = (item["map"], item["dimension_index"])
        if key in existing_workers:
            continue
        asset_path = assets.get(item["map"])
        if not asset_path:
            pending_assets.append(item["map"])
            continue
        service_name = _slug(item["map"], item["dimension_index"])
        if service_name in used_services:
            raise DiscoveryError(f"generated service name collides with existing service: {service_name}")
        used_services.add(service_name)
        partition_id = next_partition
        next_partition += 1
        game_port = _next_game_port(
            next_game_port,
            occupied_ports,
            start=game_port_range[0],
            end=game_port_range[1],
        )
        allocated_game_ports.add(game_port)
        next_game_port = game_port + 1
        igw_port = _next_port(
            next_igw_port, occupied_ports, end=igw_port_range[1], range_name="IGW_UDP_PORT_RANGE",
        )
        next_igw_port = igw_port + 1
        ip_address = _next_ip(worker_ip_interfaces, used_ips, network_config)
        used_ips.add(ip_address)
        worker_ip_interfaces.append(f"{ip_address}/24")
        planned.append({
            **item,
            "service": service_name,
            "asset_path": asset_path,
            "partition_id": partition_id,
            "port": game_port,
            "igw_port": igw_port,
            "ipv4_address": ip_address,
        })
    planned.sort(key=lambda row: row["partition_id"])
    return planned, pending_assets


def plan_new_workers_with_port_growth(
    template_maps: list[dict],
    existing_workers: dict[tuple[str, int], dict],
    assets: dict[str, str],
    partition_ids: set[int],
    occupied_game_ports: set[int],
    occupied_igw_ports: set[int],
    worker_ips: set[str],
    *,
    all_used_ips: set[str],
    occupied_host_ports: set[int],
    network_config: dict,
    game_port_range: tuple[int, int],
    igw_port_range: tuple[int, int],
    game_port_min: int = DEFAULT_GAME_PORT_MIN,
    igw_port_max: int = DEFAULT_IGW_PORT_MAX,
    growth_step: int = DEFAULT_PORT_GROWTH_STEP,
) -> tuple[list[dict], list[str], tuple[int, int], tuple[int, int]]:
    """Plan workers and widen only the exhausted end of each UDP pool."""
    if growth_step < 1:
        raise DiscoveryError("DUNE_MAP_NETWORK_GROWTH_STEP must be positive")
    _validate_port_pools(game_port_range, igw_port_range)
    game_start, game_end = game_port_range
    igw_start, igw_end = igw_port_range
    game_floor = min(game_port_min, game_start)
    igw_ceiling = max(igw_port_max, igw_end)

    while True:
        current_game_range = (game_start, game_end)
        current_igw_range = (igw_start, igw_end)
        try:
            planned, pending = plan_new_workers(
                template_maps,
                existing_workers,
                assets,
                partition_ids,
                occupied_game_ports,
                occupied_igw_ports,
                worker_ips,
                all_used_ips=all_used_ips,
                occupied_host_ports=occupied_host_ports,
                network_config=network_config,
                game_port_range=current_game_range,
                igw_port_range=current_igw_range,
            )
            return planned, pending, current_game_range, current_igw_range
        except DiscoveryError as exc:
            message = str(exc)
            if "no free UDP port remains in GAME_UDP_PORT_RANGE" in message:
                if game_start <= game_floor:
                    raise DiscoveryError(
                        f"{message}; automatic game-port growth reached its configured floor"
                    ) from exc
                game_start = max(game_floor, game_start - growth_step)
                continue
            if "no free UDP port remains in IGW_UDP_PORT_RANGE" in message:
                if igw_end >= igw_ceiling:
                    raise DiscoveryError(
                        f"{message}; automatic IGW-port growth reached its configured ceiling"
                    ) from exc
                igw_end = min(igw_ceiling, igw_end + growth_step)
                continue
            raise


def _render_yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_overlay(
    workers: list[dict],
    *,
    root: pathlib.Path,
    template_compose_file: pathlib.Path,
    template_service: dict,
) -> str:
    template_command = _tokens(template_service.get("command"))
    if not template_command:
        raise DiscoveryError(f"{MAP_SERVICE_TEMPLATE} has no command to clone")
    lines = [
        "# Generated by scripts/sync-map-workers.py. Do not edit.",
        "services:",
    ]
    if not workers:
        lines[-1] = "services: {}"
        return "\n".join(lines) + "\n"

    for worker in workers:
        game_mapping = f"{worker['port']}:{worker['port']}/udp"
        igw_mapping = f"{worker['igw_port']}:{worker['igw_port']}/udp"
        command = []
        seen_map = False
        seen_partition = False
        seen_game_port = False
        seen_igw_port = False
        for token in template_command:
            if token.startswith("-Map=") or token.startswith("/Game/"):
                command.append(worker["asset_path"])
                seen_map = True
            elif PARTITION_RE.fullmatch(token):
                command.append(f"-PartitionIndex={worker['partition_id']}")
                seen_partition = True
            elif match := PORT_RE.fullmatch(token):
                if match.group(1) == "Port":
                    command.append(f"-Port={worker['port']}")
                    seen_game_port = True
                else:
                    command.append(f"-IGWPort={worker['igw_port']}")
                    seen_igw_port = True
            else:
                command.append(token)
        if not all((seen_map, seen_partition, seen_game_port, seen_igw_port)):
            raise DiscoveryError(
                f"{MAP_SERVICE_TEMPLATE} command must contain one map, partition, game port, and IGW port"
            )
        service = worker["service"]
        save_path = root / "data" / "server-saved" / service
        lines += [
            f"  {service}:",
            "    extends:",
            f"      file: {_render_yaml_string(str(template_compose_file.resolve()))}",
            f"      service: {MAP_SERVICE_TEMPLATE}",
            "    labels:",
            f"      dune.map.database-name: {_render_yaml_string(worker['map'])}",
            f"      dune.map.dimension-index: {_render_yaml_string(str(worker['dimension_index']))}",
            "      dune.map.auto-discovered: \"true\"",
            "    command:",
        ]
        lines.extend(f"      - {_render_yaml_string(token)}" for token in command)
        lines += [
            "    environment:",
            f"      POD_IP: {_render_yaml_string(worker['ipv4_address'])}",
            "    ports: !override",
            f"      - {_render_yaml_string(game_mapping)}",
            f"      - {_render_yaml_string(igw_mapping)}",
            "    volumes: !override",
            f"      - {_render_yaml_string(str(root.resolve()) + ':/workspace:ro')}",
            f"      - {_render_yaml_string(str(save_path.resolve()) + ':/home/dune/server/DuneSandbox/Saved')}",
            "    networks:",
            "      default:",
            f"        ipv4_address: {worker['ipv4_address']}",
        ]
    return "\n".join(lines) + "\n"


def write_text_atomic(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = pathlib.Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o644)
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", nargs="?", default=".env")
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument("--world-template", type=pathlib.Path)
    parser.add_argument("--compose-files", help="colon-delimited source Compose files; omit generated overlay")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--asset-index", type=pathlib.Path, help="TSV fixture; skips Docker image scanning")
    parser.add_argument("--force", action="store_true", help="ignore the cooked-asset cache")
    parser.add_argument("--dry-run", action="store_true", help="report additions without writing the overlay")
    parser.add_argument(
        "--network-plan", action="store_true",
        help="emit the required UDP port ranges as JSON without writing the worker overlay",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    env_file = args.env_file
    if not pathlib.Path(env_file).is_absolute():
        env_file = str((root / env_file).resolve())
    env_path = pathlib.Path(env_file)
    env_values = read_env_file(env_path)
    env_values.update({key: value for key, value in os.environ.items() if key in {
        "DUNE_IMAGE_TAG", "COMPOSE_PROFILES", "DUNE_WORLD_PARTITION_COUNT",
        "GAME_UDP_PORT_RANGE", "IGW_UDP_PORT_RANGE",
        "DUNE_MAP_NETWORK_GROWTH_STEP", "DUNE_MAP_NETWORK_GAME_PORT_MIN",
        "DUNE_MAP_NETWORK_IGW_PORT_MAX",
    }})

    template_path = args.world_template or pathlib.Path(
        env_values.get("DUNE_WORLD_TEMPLATE_PATH") or os.environ.get("DUNE_WORLD_TEMPLATE_PATH") or DEFAULT_WORLD_TEMPLATE
    )
    template_doc = _read_yaml(template_path)
    template_maps = extract_template_maps(template_doc)
    if not template_maps:
        raise DiscoveryError(f"no map partitions found in world template: {template_path}")

    output_path = args.output or (root / DEFAULT_OVERLAY)
    if not output_path.is_absolute():
        output_path = (root / output_path).resolve()
    compose_files_raw = args.compose_files or os.environ.get("COMPOSE_FILES") or env_values.get("COMPOSE_FILES") or "compose.yaml:compose.allmaps.yaml"
    compose_files = [
        pathlib.Path(item).resolve() if pathlib.Path(item).is_absolute() else (root / item).resolve()
        for item in str(compose_files_raw).split(":") if item
    ]
    compose_files = [path for path in compose_files if path != output_path and path.is_file()]
    if not compose_files:
        raise DiscoveryError("no source Compose files are available for map discovery")

    runtime = os.environ.get("CONTAINER_RUNTIME", "docker")
    command = [runtime, "compose"]
    config_compose_files = list(compose_files)
    if output_path.is_file():
        config_compose_files.append(output_path)
    for path in config_compose_files:
        command.extend(["-f", str(path)])
    if env_path.is_file():
        command.extend(["--env-file", str(env_path)])
    command.extend(["--profile", "*", "config", "--format", "json"])
    resolved = subprocess.run(command, check=False, capture_output=True, text=True)
    if resolved.returncode:
        raise DiscoveryError(f"Docker Compose map model failed ({resolved.returncode})")
    try:
        compose_config = json.loads(resolved.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError("Docker Compose returned invalid map model JSON") from exc

    # Feed only the resolved metadata to the existing catalog parser. The JSON
    # may contain secrets in command/environment values; it is never logged or
    # written to disk.
    map_catalog_path = pathlib.Path(__file__).with_name("map_catalog.py")
    if not map_catalog_path.is_file():
        raise DiscoveryError("scripts/map_catalog.py is required for map metadata validation")
    sys.path.insert(0, str(map_catalog_path.parent))
    import map_catalog

    catalog_env = dict(env_values)
    catalog_env["COMPOSE_PROFILES"] = env_values.get("COMPOSE_PROFILES", os.environ.get("COMPOSE_PROFILES", ""))
    map_catalog.parse_compose_config(compose_config, env_values=catalog_env, active_profiles=set())
    (
        existing_workers,
        partition_ids,
        occupied_game_ports,
        occupied_igw_ports,
        worker_ips,
    ) = collect_existing_workers(compose_config)
    if not existing_workers:
        raise DiscoveryError("source Compose model contains no map workers")

    map_compose_path = next((path for path in compose_files if path.name == "compose.allmaps.yaml"), None)
    if map_compose_path is None:
        raise DiscoveryError("compose.allmaps.yaml is required as the new-worker service template")
    raw_map_compose = _read_yaml(map_compose_path, compose=True)
    template_service = _services_from_config(raw_map_compose).get(MAP_SERVICE_TEMPLATE)
    if not template_service:
        raise DiscoveryError(f"Compose service template is missing: {MAP_SERVICE_TEMPLATE}")

    if args.asset_index:
        assets = _asset_entries(
            args.asset_index.read_text(encoding="utf-8"),
            required_map_names={row["map"] for row in template_maps},
        )
        image_id = "fixture"
    else:
        scanner = root / ASSET_INDEX_BINARY
        raw_image = str(template_service.get("image") or "")
        image, image_id = _image_for_scan(raw_image, env_values, runtime)
        fingerprint_data = {
            "schema": 1,
            "image_id": image_id,
            "template_sha256": _sha256(template_path),
            "compose_sha256": {str(path): _sha256(path) for path in compose_files},
            "indexer_sha256": _sha256(scanner) if scanner.is_file() else "missing",
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cache_path = root / DEFAULT_CACHE
        assets = None if args.force else _cached_assets(cache_path, fingerprint)
        if assets is None:
            asset_output, asset_warnings = _scan_assets(runtime, image, scanner)
            if asset_warnings.strip():
                print(asset_warnings.strip(), file=sys.stderr)
            assets = _asset_entries(
                asset_output,
                required_map_names={row["map"] for row in template_maps},
            )
            _write_json_atomic(cache_path, {
                "schema_version": 1,
                "fingerprint": fingerprint,
                "image_id": image_id,
                "assets": assets,
            })

    all_used_ips = set()
    for service in _services_from_config(compose_config).values():
        networks = service.get("networks") or {}
        if isinstance(networks, dict):
            for details in networks.values():
                if isinstance(details, dict) and details.get("ipv4_address"):
                    all_used_ips.add(str(details["ipv4_address"]).split("/", 1)[0])
    all_used_ips.update(_runtime_network_ips(compose_config, runtime))
    network_config = (compose_config.get("networks") or {}).get("default") or {}
    occupied_host_ports = _host_published_ports(compose_config) | _host_listening_udp_ports()
    game_port_range = _parse_port_range(
        env_values.get("GAME_UDP_PORT_RANGE"), "GAME_UDP_PORT_RANGE", DEFAULT_GAME_PORT_RANGE,
    )
    igw_port_range = _parse_port_range(
        env_values.get("IGW_UDP_PORT_RANGE"), "IGW_UDP_PORT_RANGE", DEFAULT_IGW_PORT_RANGE,
    )
    if args.network_plan:
        growth_step = _parse_int_setting(
            env_values.get("DUNE_MAP_NETWORK_GROWTH_STEP"),
            "DUNE_MAP_NETWORK_GROWTH_STEP", DEFAULT_PORT_GROWTH_STEP,
            minimum=1, maximum=65535,
        )
        game_port_min = _parse_int_setting(
            env_values.get("DUNE_MAP_NETWORK_GAME_PORT_MIN"),
            "DUNE_MAP_NETWORK_GAME_PORT_MIN", DEFAULT_GAME_PORT_MIN,
            minimum=1, maximum=65535,
        )
        igw_port_max = _parse_int_setting(
            env_values.get("DUNE_MAP_NETWORK_IGW_PORT_MAX"),
            "DUNE_MAP_NETWORK_IGW_PORT_MAX", DEFAULT_IGW_PORT_MAX,
            minimum=1, maximum=65535,
        )
        planned, pending, planned_game_range, planned_igw_range = plan_new_workers_with_port_growth(
            template_maps,
            existing_workers,
            assets,
            partition_ids,
            occupied_game_ports,
            occupied_igw_ports,
            worker_ips,
            all_used_ips=all_used_ips,
            occupied_host_ports=occupied_host_ports,
            network_config=network_config,
            game_port_range=game_port_range,
            igw_port_range=igw_port_range,
            game_port_min=game_port_min,
            igw_port_max=igw_port_max,
            growth_step=growth_step,
        )
        new_workers = sum(
            1 for row in planned
            if (row["map"], row["dimension_index"]) not in existing_workers
        )
        print(json.dumps({
            "game_port_range": f"{planned_game_range[0]}:{planned_game_range[1]}",
            "igw_port_range": f"{planned_igw_range[0]}:{planned_igw_range[1]}",
            "growth_required": (
                planned_game_range != game_port_range or planned_igw_range != igw_port_range
            ),
            "planned_additions": new_workers,
            "pending_assets": sorted(set(pending)),
        }, separators=(",", ":")))
        return 0

    planned, pending = plan_new_workers(
        template_maps,
        existing_workers,
        assets,
        partition_ids,
        occupied_game_ports,
        occupied_igw_ports,
        worker_ips,
        all_used_ips=all_used_ips,
        occupied_host_ports=occupied_host_ports,
        network_config=network_config,
        game_port_range=game_port_range,
        igw_port_range=igw_port_range,
    )
    content = render_overlay(
        planned,
        root=root,
        template_compose_file=map_compose_path,
        template_service=template_service,
    )
    if not args.dry_run:
        write_text_atomic(output_path, content)

    print(
        f"world template maps={len(template_maps)}; existing Compose workers={len(existing_workers)}; "
        f"new workers={len(planned)}"
    )
    for worker in planned:
        print(
            f"added {worker['service']}: partition={worker['partition_id']} "
            f"map={worker['map']} dimension={worker['dimension_index']}"
        )
    if pending:
        print(
            "waiting for cooked map assets before generating workers: "
            + ", ".join(sorted(set(pending)),
        ),
        file=sys.stderr,
    )
    if not args.dry_run:
        print(f"wrote generated map overlay: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DiscoveryError as exc:
        print(f"map worker discovery failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
