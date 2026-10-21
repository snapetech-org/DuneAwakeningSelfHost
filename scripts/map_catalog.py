#!/usr/bin/env python3
"""Discover game-map workers from the resolved Docker Compose model.

Compose is the roster source of truth. A service is a map worker when its
command contains -PartitionIndex=N. The cooked map asset is read from its
command, with an explicit database-name label for workers whose map is
implicit (currently Survival_1).
"""

import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile


SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
DB_MAP_RE = re.compile(r"^[A-Za-z0-9_]+$")
ASSET_RE = re.compile(r"^/Game/[A-Za-z0-9_./-]+$")
PARTITION_RE = re.compile(r"^-PartitionIndex=(\d+)$")
PORT_RE = re.compile(r"^-(Port|IGWPort)=(\d+)$")
LABEL_PREFIX = "dune.map."
DEFAULT_CACHE_FILE = pathlib.Path(__file__).resolve().parents[1] / "backups" / "admin-panel" / "map-catalog.json"
ENV_VALUE_KEYS = {"DUNE_WORLD_PARTITION_COUNT", "COMPOSE_PROFILES"}


class CatalogError(ValueError):
    pass


def read_env_file(path):
    values = {}
    if not path:
        return values
    try:
        handle = pathlib.Path(path).open(encoding="utf-8")
    except OSError:
        return values
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in ENV_VALUE_KEYS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def _as_labels(value):
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        labels = {}
        for entry in value:
            key, separator, item = str(entry).partition("=")
            if separator:
                labels[key] = item
        return labels
    return {}


def _command_tokens(command):
    if isinstance(command, list):
        return [str(item) for item in command]
    if isinstance(command, str):
        return shlex.split(command)
    return []


def _map_asset(tokens):
    candidates = []
    for token in tokens:
        if token.startswith("-Map="):
            candidates.append(token.partition("=")[2])
        elif token.startswith("/Game/"):
            candidates.append(token)
    if len(candidates) > 1:
        raise CatalogError("map worker command has multiple cooked map arguments")
    if not candidates:
        return None, None
    asset = candidates[0]
    if not ASSET_RE.fullmatch(asset):
        raise CatalogError(f"invalid cooked map asset argument: {asset!r}")
    package = asset.rsplit("/", 1)[-1]
    object_name = package.rpartition(".")[2]
    if not object_name or not DB_MAP_RE.fullmatch(object_name):
        raise CatalogError(f"cooked map asset has no valid object name: {asset!r}")
    return asset, object_name


def _network_ip(service):
    networks = service.get("networks") or {}
    if not isinstance(networks, dict):
        return None
    addresses = [
        str(details.get("ipv4_address"))
        for details in networks.values()
        if isinstance(details, dict) and details.get("ipv4_address")
    ]
    if len(addresses) > 1:
        raise CatalogError("map worker declares multiple static IPv4 addresses")
    return addresses[0] if addresses else None


def _profiles(service):
    value = service.get("profiles") or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CatalogError("service profiles must be a list")
    profiles = sorted({str(profile) for profile in value if str(profile)})
    if any(not SERVICE_RE.fullmatch(profile) for profile in profiles):
        raise CatalogError(f"invalid Compose profile name: {profiles!r}")
    return profiles


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_compose_config(config, *, env_values=None, active_profiles=None):
    """Return validated map metadata parsed from docker compose config JSON."""
    env_values = env_values or {}
    active_profiles = set(active_profiles or [])
    try:
        service_defs = config["services"]
    except (KeyError, TypeError) as exc:
        raise CatalogError("Compose config must contain a services object") from exc
    if not isinstance(service_defs, dict):
        raise CatalogError("Compose services must be an object")

    try:
        optional_partition_ceiling = int(env_values.get("DUNE_WORLD_PARTITION_COUNT", "30"))
        if optional_partition_ceiling < 1:
            raise ValueError
    except (TypeError, ValueError):
        if str(env_values.get("DUNE_WORLD_PARTITION_COUNT", "")).strip().lower() == "auto":
            optional_partition_ceiling = 0
        else:
            raise CatalogError("DUNE_WORLD_PARTITION_COUNT must be a positive integer or 'auto'")

    maps = []
    all_partition_ids = set()
    all_db_maps = set()
    for service_name, service in service_defs.items():
        tokens = _command_tokens((service or {}).get("command"))
        partition_tokens = [match for token in tokens if (match := PARTITION_RE.fullmatch(token))]
        if not partition_tokens:
            continue
        if len(partition_tokens) != 1:
            raise CatalogError(f"{service_name}: expected exactly one -PartitionIndex argument")
        if not SERVICE_RE.fullmatch(str(service_name)):
            raise CatalogError(f"invalid Compose map service name: {service_name!r}")
        partition_id = int(partition_tokens[0].group(1))
        if partition_id < 1:
            raise CatalogError(f"{service_name}: partition id must be positive")
        if partition_id in all_partition_ids:
            raise CatalogError(f"duplicate map partition id: {partition_id}")
        all_partition_ids.add(partition_id)

        labels = _as_labels((service or {}).get("labels"))
        map_labels = {key[len(LABEL_PREFIX):]: value for key, value in labels.items() if key.startswith(LABEL_PREFIX)}
        asset_path, package_map_name = _map_asset(tokens)
        map_name = map_labels.get("database-name") or package_map_name
        if not map_name:
            raise CatalogError(
                f"{service_name}: no cooked map argument; set label {LABEL_PREFIX}database-name"
            )
        if not DB_MAP_RE.fullmatch(map_name):
            raise CatalogError(f"{service_name}: invalid database map name: {map_name!r}")

        try:
            dimension_index = int(map_labels.get("dimension-index", "0"))
        except ValueError as exc:
            raise CatalogError(f"{service_name}: dimension-index must be an integer") from exc
        if dimension_index < 0:
            raise CatalogError(f"{service_name}: dimension-index must be non-negative")
        database_key = (map_name, dimension_index)
        if database_key in all_db_maps:
            raise CatalogError(f"duplicate database map/dimension: {map_name}/{dimension_index}")
        all_db_maps.add(database_key)

        ports = {match.group(1): int(match.group(2)) for token in tokens if (match := PORT_RE.fullmatch(token))}
        map_profiles = _profiles(service or {})
        count_opt_in = _truthy(map_labels.get("enabled-by-partition-count"))
        active_by_profile = bool(set(map_profiles) & active_profiles)
        active_by_count = count_opt_in and optional_partition_ceiling >= partition_id
        active = not map_profiles or active_by_profile or active_by_count
        enabled_profiles = []
        if active and map_profiles:
            enabled_profiles = sorted(set(map_profiles) & active_profiles)
            if not enabled_profiles:
                enabled_profiles = map_profiles[:1]

        maps.append({
            "service": str(service_name),
            "partition_id": partition_id,
            "map": map_name,
            "dimension_index": dimension_index,
            "label": map_labels.get("partition-label") or None,
            "asset_path": asset_path,
            "profiles": map_profiles,
            "enabled_profiles": enabled_profiles,
            "active": active,
            "enabled_by_partition_count": count_opt_in,
            "remove_when_disabled": _truthy(map_labels.get("remove-when-disabled")),
            "port": ports.get("Port"),
            "igw_port": ports.get("IGWPort"),
            "ipv4_address": _network_ip(service or {}),
        })

    maps.sort(key=lambda item: (item["partition_id"], item["service"]))
    active_maps = [item for item in maps if item["active"]]
    if not active_maps:
        raise CatalogError("resolved Compose model contains no active map workers")
    active_services = {item["service"] for item in active_maps}
    active_profiles_result = sorted({
        profile for item in active_maps for profile in item["enabled_profiles"]
    })
    return {
        "schema_version": 1,
        "source": "docker-compose-config",
        "maps": maps,
        "active_services": [item["service"] for item in active_maps],
        "active_partition_ids": [item["partition_id"] for item in active_maps],
        "active_profiles": active_profiles_result,
        "all_profiles": sorted({profile for item in maps for profile in item["profiles"]}),
        "inactive_profile_services": [
            item["service"] for item in maps
            if item["profiles"] and item["service"] not in active_services
        ],
    }


def _compose_config(compose_files, _env_file, container_runtime):
    files = [item for item in str(compose_files).split(":") if item]
    if not files:
        raise CatalogError("no Compose files configured")
    command = [container_runtime, "compose"]
    for file_name in files:
        command.extend(["-f", file_name])
    # Map discovery needs static command, label, profile, and network metadata.
    # Never interpolate Compose secrets into the JSON held by this process.
    command.extend(["--env-file", os.devnull])
    # Include inactive-profile services in the read-only model, then apply the
    # project's explicit profile/count policy in parse_compose_config().
    command.extend(["--profile", "*", "config", "--format", "json", "--no-interpolate"])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise CatalogError(f"Docker Compose config failed ({result.returncode}): {details}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Docker Compose returned invalid JSON: {exc}") from exc


def _sql_literal(value):
    if value is None:
        return "null"
    return "'" + str(value).replace("'", "''") + "'"


def _write_cache(catalog, path):
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(catalog, sort_keys=True, indent=2) + "\n"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(destination.parent),
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", nargs="?", default=os.environ.get("ENV_FILE", ".env"))
    parser.add_argument("--compose-files", default=os.environ.get("COMPOSE_FILES", "compose.yaml:compose.allmaps.yaml"))
    parser.add_argument("--container-runtime", default=os.environ.get("CONTAINER_RUNTIME", "docker"))
    parser.add_argument(
        "--no-env-file-read",
        action="store_true",
        help="use only exported map-catalog settings instead of reading the dotenv file",
    )
    parser.add_argument("--field", choices=("json", "services", "all-services", "profiles", "all-profiles", "map-partitions", "all-map-partitions", "inactive-map-partitions", "partition-ids", "count", "sql-values", "prune-values"), default="json")
    parser.add_argument("--write-cache", nargs="?", const=os.environ.get("DUNE_MAP_CATALOG_FILE", str(DEFAULT_CACHE_FILE)))
    args = parser.parse_args(argv)

    try:
        env_values = {} if args.no_env_file_read else read_env_file(args.env_file)
        env_values.update({key: value for key, value in os.environ.items() if key in ENV_VALUE_KEYS})
        config = _compose_config(args.compose_files, args.env_file, args.container_runtime)
        profiles = {
            profile.strip()
            for profile in str(env_values.get("COMPOSE_PROFILES", "")).split(",")
            if profile.strip()
        }
        catalog = parse_compose_config(config, env_values=env_values, active_profiles=profiles)
        if args.write_cache:
            _write_cache(catalog, args.write_cache)
        active_maps = [item for item in catalog["maps"] if item["active"]]
        if args.field == "json":
            print(json.dumps(catalog, separators=(",", ":")))
        elif args.field == "services":
            print(" ".join(catalog["active_services"]))
        elif args.field == "all-services":
            print(" ".join(item["service"] for item in catalog["maps"]))
        elif args.field == "profiles":
            print(" ".join(catalog["active_profiles"]))
        elif args.field == "all-profiles":
            print(" ".join(catalog["all_profiles"]))
        elif args.field == "map-partitions":
            print("\n".join(f'{item["service"]}:{item["partition_id"]}' for item in active_maps))
        elif args.field == "all-map-partitions":
            print("\n".join(f'{item["service"]}:{item["partition_id"]}' for item in catalog["maps"]))
        elif args.field == "inactive-map-partitions":
            print("\n".join(
                f'{item["service"]}:{",".join(item["profiles"])}'
                for item in catalog["maps"] if item["profiles"] and not item["active"]
            ))
        elif args.field == "partition-ids":
            print(",".join(str(item["partition_id"]) for item in active_maps))
        elif args.field == "count":
            print(len(active_maps))
        elif args.field == "sql-values":
            rows = []
            for item in active_maps:
                rows.append("(" + ", ".join((
                    str(item["partition_id"]),
                    _sql_literal(item["map"]),
                    str(item["dimension_index"]),
                    _sql_literal(item["label"]),
                )) + ")")
            print(",\n  ".join(rows))
        elif args.field == "prune-values":
            rows = []
            for item in catalog["maps"]:
                if item["active"] or not item["remove_when_disabled"]:
                    continue
                rows.append("(" + ", ".join((
                    str(item["partition_id"]),
                    _sql_literal(item["map"]),
                    str(item["dimension_index"]),
                )) + ")")
            print(",\n  ".join(rows))
    except (CatalogError, OSError, subprocess.SubprocessError) as exc:
        print(f"map catalog error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
