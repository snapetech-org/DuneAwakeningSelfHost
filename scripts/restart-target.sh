#!/bin/sh
set -eu

target="${DUNE_RESTART_TARGET:-${1:-}}"
services="${DUNE_RESTART_SERVICES:-}"
action="${DUNE_RESTART_ACTION:-restart}"
phase="${DUNE_RESTART_PHASE:-$action}"
fast_dynamic_start="${DUNE_RESTART_FAST_DYNAMIC_START:-false}"

if [ -z "$target" ]; then
  printf 'missing DUNE_RESTART_TARGET\n' >&2
  exit 64
fi
case "$action" in
  restart|shutdown) ;;
  *)
    printf 'invalid DUNE_RESTART_ACTION: %s\n' "$action" >&2
    exit 64
    ;;
esac
case "$phase" in
  restart|shutdown|stop|update|start|reboot|recovery) ;;
  *)
    printf 'invalid DUNE_RESTART_PHASE: %s\n' "$phase" >&2
    exit 64
    ;;
esac

# Additional Survival_1 dimensions are generated services outside the static
# Compose farm. Include them in all-farm restart/shutdown flows when enabled.
if [ "$target" = "all" ] && command -v docker >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 \
    && [ "${DUNE_RESTART_DRY_RUN:-false}" != "true" ] && [ "${DUNE_RESTART_DRY_RUN:-0}" != "1" ] \
    && [ -x "$(dirname "$0")/sietches.sh" ]; then
  sietch_env="${ENV_FILE:-.env}"
  if grep -Eq '^DUNE_SIETCH_MUTATIONS_ENABLED=(1|true|yes|on)$' "$sietch_env" 2>/dev/null; then
    case "$phase" in
      restart|shutdown|stop|reboot)
        "$(dirname "$0")/sietches.sh" "$sietch_env" stop-managed --execute
        ;;
    esac
    case "$phase" in
      restart|start)
        trap 'rc=$?; if [ "$rc" -eq 0 ]; then "$(dirname "$0")/sietches.sh" "${ENV_FILE:-.env}" reconcile --execute || rc=$?; fi; exit "$rc"' EXIT
        ;;
    esac
  fi
fi

steam_update_enabled() {
  case "${DUNE_RESTART_CHECK_STEAM_UPDATE:-true}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

map_watchdog_control_enabled() {
  case "${DUNE_MAP_WATCHDOG_CONTROL:-true}" in
    0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

map_watchdog_control() {
  verb="$1"
  env_file="${ENV_FILE:-.env}"
  if ! map_watchdog_control_enabled; then
    return 0
  fi
  if [ -x ./scripts/map-watchdog-control.sh ]; then
    ./scripts/map-watchdog-control.sh "$verb" "$env_file" || true
  fi
}

seed_gateway_neighbors() {
  seed_timeout="${DUNE_SEED_NEIGHBOR_TIMEOUT_SECONDS:-90}"
  if [ ! -x ./scripts/seed-gateway-neighbor.sh ]; then
    return 0
  fi
  case "$seed_timeout" in
    ''|*[!0-9]*)
      seed_timeout=90
      ;;
  esac
  if [ "$seed_timeout" -gt 0 ] && command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5s "${seed_timeout}s" ./scripts/seed-gateway-neighbor.sh || true
  else
    ./scripts/seed-gateway-neighbor.sh || true
  fi
}

run_hardcore_dd_weekly_wipe() {
  env_file="${ENV_FILE:-.env}"
  enabled="${DUNE_HARDCORE_DD_WEEKLY_WIPE_ENABLED:-$(env_file_value DUNE_HARDCORE_DD_WEEKLY_WIPE_ENABLED "$env_file")}"
  if [ -z "$enabled" ]; then
    enabled="${DUNE_PVP_DD_WEEKLY_WIPE_ENABLED:-$(env_file_value DUNE_PVP_DD_WEEKLY_WIPE_ENABLED "$env_file")}"
  fi
  case "$enabled" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  case " $services " in
    *" deep-desert-pvp "*|*" all "*) ;;
    *) return 0 ;;
  esac
  if [ ! -x ./scripts/wipe-hardcore-deep-desert.sh ]; then
    printf 'Hardcore DD weekly wipe enabled but scripts/wipe-hardcore-deep-desert.sh is missing or not executable\n' >&2
    return 1
  fi
  ./scripts/wipe-hardcore-deep-desert.sh "$env_file" --execute --if-due
}

run_landsraad_goal_tuning() {
  env_file="${ENV_FILE:-.env}"
  enabled="${DUNE_LANDSRAAD_GOAL_TUNING_ENABLED:-$(env_file_value DUNE_LANDSRAAD_GOAL_TUNING_ENABLED "$env_file")}"
  case "$enabled" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [ ! -x ./scripts/tune-landsraad-goals.sh ]; then
    printf 'Landsraad goal tuning enabled but scripts/tune-landsraad-goals.sh is missing or not executable\n' >&2
    return 1
  fi
  ./scripts/tune-landsraad-goals.sh "$env_file" --execute
}

run_landsraad_term_length_tuning() {
  env_file="${ENV_FILE:-.env}"
  enabled="${DUNE_LANDSRAAD_TERM_LENGTH_TUNING_ENABLED:-$(env_file_value DUNE_LANDSRAAD_TERM_LENGTH_TUNING_ENABLED "$env_file")}"
  case "$enabled" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [ ! -x ./scripts/tune-landsraad-term-length.sh ]; then
    printf 'Landsraad term length tuning enabled but scripts/tune-landsraad-term-length.sh is missing or not executable\n' >&2
    return 1
  fi
  ./scripts/tune-landsraad-term-length.sh "$env_file" --execute
}

run_landsraad_term_alignment_guard() {
  env_file="${ENV_FILE:-.env}"
  enabled="${DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED:-$(env_file_value DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED "$env_file")}"
  enabled="${enabled:-true}"
  case "$enabled" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [ ! -x ./scripts/validate-landsraad-term-coriolis-alignment.sh ]; then
    printf 'Landsraad term Coriolis alignment guard enabled but scripts/validate-landsraad-term-coriolis-alignment.sh is missing or not executable\n' >&2
    return 1
  fi
  ./scripts/validate-landsraad-term-coriolis-alignment.sh "$env_file"
}

run_landsraad_reveal_watchdog() {
  env_file="${ENV_FILE:-.env}"
  enabled="${DUNE_LANDSRAAD_REVEAL_WATCHDOG_ENABLED:-$(env_file_value DUNE_LANDSRAAD_REVEAL_WATCHDOG_ENABLED "$env_file")}"
  enabled="${enabled:-true}"
  case "$enabled" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [ ! -x ./scripts/landsraad-reveal-watchdog.sh ]; then
    printf 'Landsraad reveal watchdog enabled but scripts/landsraad-reveal-watchdog.sh is missing or not executable\n' >&2
    return 1
  fi
  ./scripts/landsraad-reveal-watchdog.sh "$env_file" --execute
}

run_landsraad_coriolis_guard() {
  env_file="${ENV_FILE:-.env}"
  enabled="${DUNE_LANDSRAAD_CORIOLIS_GUARD_ENABLED:-$(env_file_value DUNE_LANDSRAAD_CORIOLIS_GUARD_ENABLED "$env_file")}"
  enabled="${enabled:-true}"
  case "$enabled" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [ ! -x ./scripts/validate-landsraad-coriolis-cycle.sh ]; then
    printf 'Landsraad Coriolis guard enabled but scripts/validate-landsraad-coriolis-cycle.sh is missing or not executable\n' >&2
    return 1
  fi
  ./scripts/validate-landsraad-coriolis-cycle.sh "$env_file"
}

pre_start_hygiene() {
  env_file="${ENV_FILE:-.env}"
  if [ -x ./scripts/apply-official-db-patches.sh ]; then
    ./scripts/apply-official-db-patches.sh "$env_file"
  fi
  run_landsraad_coriolis_guard
  run_landsraad_term_length_tuning
  run_landsraad_term_alignment_guard
  run_landsraad_goal_tuning
  run_landsraad_reveal_watchdog
  run_hardcore_dd_weekly_wipe
  clear_player_rmq="${DUNE_RESTART_CLEAR_PLAYER_RMQ_SESSIONS:-}"
  if [ -z "$clear_player_rmq" ]; then
    case "$target" in
      all) clear_player_rmq=true ;;
      *) clear_player_rmq=false ;;
    esac
  fi
  case "$clear_player_rmq" in
    1|true|yes|on)
      if [ -x ./scripts/clear-player-rmq-sessions.sh ]; then
        ./scripts/clear-player-rmq-sessions.sh "$env_file" || true
      fi
      ;;
  esac
}

env_file_value() {
  key="$1"
  file="${2:-${ENV_FILE:-.env}}"
  eval "value=\${$key:-}"
  if [ -n "$value" ]; then
    printf '%s\n' "$value"
    return 0
  fi
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "")
      gsub(/^["'\''"]|["'\''"]$/, "")
      print
      exit
    }
  ' "$file" 2>/dev/null || true
}

run_steam_update_check() {
  env_file="${ENV_FILE:-.env}"
  steam_required="$(env_file_value DUNE_RESTART_STEAMCMD_REQUIRED "$env_file")"
  steam_required="${steam_required:-true}"
  if ! steam_update_enabled; then
    printf 'Steam package update check disabled by DUNE_RESTART_CHECK_STEAM_UPDATE\n'
    return 0
  fi
  if [ -x ./scripts/update-steam-tool.sh ]; then
    steamcmd_command="$(env_file_value DUNE_STEAMCMD_COMMAND "$env_file")"
    steamcmd_command="${steamcmd_command:-steamcmd}"
    steam_mode="$(env_file_value DUNE_RESTART_STEAM_UPDATE_MODE "$env_file")"
    steam_mode="${steam_mode:-auto}"
    if [ "$steam_mode" = "none" ]; then
      printf 'Steam package acquisition disabled; using only the already staged candidate\n'
    elif [ "$steam_mode" != "steamcmd" ]; then
      ./scripts/update-steam-tool.sh "$env_file"
    elif command -v "$steamcmd_command" >/dev/null 2>&1; then
      ./scripts/update-steam-tool.sh "$env_file"
    else
      helper_image="$(env_file_value DUNE_RESTART_STEAMCMD_HELPER_IMAGE "$env_file")"
      helper_image="${helper_image:-cm2network/steamcmd:root}"
      steam_dir="$(env_file_value DUNE_STEAM_SERVER_DIR "$env_file")"
      if [ -n "$helper_image" ] && [ -n "$steam_dir" ] && command -v docker >/dev/null 2>&1; then
        steam_mount="$steam_dir"
        case "$steam_dir" in
          */steamapps/common/*|*/Steam/steamapps/common/*)
            steam_mount="$(dirname "$(dirname "$(dirname "$steam_dir")")")"
            ;;
        esac
        steam_uid_gid="$(stat -c '%u:%g' "$steam_dir")"
        docker run --rm \
          --network host \
          --user "$steam_uid_gid" \
          -v "$PWD:$PWD" \
          -v "$PWD:/workspace" \
          -v "$steam_mount:$steam_mount" \
          -w "$PWD" \
          -e HOME=/tmp \
          -e "DUNE_RESTART_STEAMCMD_UPDATE=$(env_file_value DUNE_RESTART_STEAMCMD_UPDATE "$env_file")" \
          -e "DUNE_RESTART_STEAM_UPDATE_MODE=$(env_file_value DUNE_RESTART_STEAM_UPDATE_MODE "$env_file")" \
          -e "DUNE_RESTART_STEAM_CLIENT_TRIGGER=$(env_file_value DUNE_RESTART_STEAM_CLIENT_TRIGGER "$env_file")" \
          -e "DUNE_RESTART_STEAM_CLIENT_WAIT_SECONDS=$(env_file_value DUNE_RESTART_STEAM_CLIENT_WAIT_SECONDS "$env_file")" \
          -e "DUNE_RESTART_STEAM_CLIENT_MIN_WAIT_SECONDS=$(env_file_value DUNE_RESTART_STEAM_CLIENT_MIN_WAIT_SECONDS "$env_file")" \
          -e "DUNE_STEAM_SERVER_DIR=$(env_file_value DUNE_STEAM_SERVER_DIR "$env_file")" \
          -e "DUNE_STEAM_CLIENT_COMMAND=$(env_file_value DUNE_STEAM_CLIENT_COMMAND "$env_file")" \
          -e "DUNE_RESTART_STEAMCMD_REQUIRED=$(env_file_value DUNE_RESTART_STEAMCMD_REQUIRED "$env_file")" \
          -e "DUNE_STEAM_APP_ID=$(env_file_value DUNE_STEAM_APP_ID "$env_file")" \
          -e "DUNE_STEAM_FORCE_PLATFORM=$(env_file_value DUNE_STEAM_FORCE_PLATFORM "$env_file")" \
          -e "DUNE_STEAM_LOGIN=$(env_file_value DUNE_STEAM_LOGIN "$env_file")" \
          -e "DUNE_OWNED_STEAM_LOGIN=$(env_file_value DUNE_OWNED_STEAM_LOGIN "$env_file")" \
          -e "DUNE_STEAM_PASSWORD=$(env_file_value DUNE_STEAM_PASSWORD "$env_file")" \
          -e "DUNE_STEAM_PASSWORD_FILE=$(env_file_value DUNE_STEAM_PASSWORD_FILE "$env_file")" \
          -e "DUNE_STEAMCMD_HOME=$(env_file_value DUNE_STEAMCMD_HOME "$env_file")" \
          -e "DUNE_STEAMCMD_COMMAND=$(env_file_value DUNE_STEAMCMD_COMMAND "$env_file")" \
          -e "DUNE_STEAMCMD_VALIDATE=$(env_file_value DUNE_STEAMCMD_VALIDATE "$env_file")" \
          -e "DUNE_STEAMCMD_TIMEOUT_SECONDS=$(env_file_value DUNE_STEAMCMD_TIMEOUT_SECONDS "$env_file")" \
          "$helper_image" bash ./scripts/update-steam-tool.sh "$env_file"
      else
        ./scripts/update-steam-tool.sh "$env_file"
      fi
    fi
  else
    printf 'SteamCMD package update skipped: scripts/update-steam-tool.sh is missing or not executable\n' >&2
    case "$steam_required" in
      1|true|yes|on)
        printf 'Steam package update is required; aborting restart\n' >&2
        exit 127
        ;;
    esac
  fi
  if [ ! -x ./scripts/check-steam-update.sh ]; then
    printf 'Steam package update check skipped: scripts/check-steam-update.sh is missing or not executable\n' >&2
    case "$steam_required" in
      1|true|yes|on)
        printf 'Steam package update check is required; aborting restart\n' >&2
        exit 127
        ;;
    esac
    return 0
  fi
  set +e
  ./scripts/check-steam-update.sh "$env_file"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    ensure_official_images_loaded
    return 0
  fi
  if [ "$rc" -eq 1 ]; then
    printf 'Steam package update available; loading official images and updating DUNE_IMAGE_TAG\n'
    ./scripts/load-images.sh "$env_file"
    ./scripts/check-steam-update.sh "$env_file" --write-env
    return 0
  fi
  case "$steam_required" in
    1|true|yes|on)
      printf 'Steam package update is required and package state is unsafe; aborting restart\n' >&2
      exit "$rc"
      ;;
  esac
  printf 'Steam package update check could not determine a safe tag; aborting restart before starting old images\n' >&2
  exit "$rc"
}

ensure_official_images_loaded() {
  env_file="${ENV_FILE:-.env}"
  image_tag="$(env_file_value DUNE_IMAGE_TAG "$env_file")"
  if [ -z "$image_tag" ]; then
    printf 'DUNE_IMAGE_TAG is empty; cannot verify official Dune images before start\n' >&2
    exit 1
  fi
  missing_image=0
  for repo in \
    seabass-server-rabbitmq \
    seabass-server-text-router \
    seabass-server-bg-director \
    seabass-server-gateway \
    seabass-server-db-utils \
    seabass-server
  do
    image="registry.funcom.com/funcom/self-hosting/${repo}:${image_tag}"
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      printf 'official Dune image is not loaded: %s\n' "$image" >&2
      missing_image=1
    fi
  done
  if [ "$missing_image" -eq 0 ]; then
    return 0
  fi
  if [ ! -x ./scripts/load-images.sh ]; then
    printf 'one or more official Dune images are missing and scripts/load-images.sh is unavailable\n' >&2
    exit 1
  fi
  printf 'loading official Dune images from Steam package because one or more required images are missing\n'
  ./scripts/load-images.sh "$env_file"
}

restart_api_phase=false
case "$phase" in
  start|stop|restart|shutdown) restart_api_phase=true ;;
esac
if ! command -v docker >/dev/null 2>&1 \
  || { [ -n "${DUNE_RESTART_API_SOCKET:-}" ] \
    && [ -S "$DUNE_RESTART_API_SOCKET" ] \
    && [ "$restart_api_phase" = true ]; }; then
  restart_api_socket="${DUNE_RESTART_API_SOCKET:-}"
  case "$phase" in
    start|stop|restart|shutdown)
      if [ -n "$restart_api_socket" ] && [ -S "$restart_api_socket" ]; then
        python3 - "$target" "$services" "$restart_api_socket" "$action" "$phase" <<'PY'
import json
import os
import socket
import sys


target, services, socket_path, action, phase = sys.argv[1:]
timeout = int(os.environ.get("DUNE_ADMIN_RESTART_COMMAND_TIMEOUT_SECONDS", "1800"))
service_list = services.split()
if target != "all" and not service_list:
    service_list = [target]
payload = json.dumps({
    "target": target,
    "services": service_list,
    "action": action,
    "phase": phase,
    "dryRun": os.environ.get("DUNE_RESTART_DRY_RUN", "").lower() in ("1", "true", "yes", "on"),
    "fastDynamicStart": os.environ.get("DUNE_RESTART_FAST_DYNAMIC_START", "").lower() in ("1", "true", "yes", "on"),
    "jobId": os.environ.get("DUNE_RESTART_JOB_ID", ""),
}).encode("utf-8")
request = (
    b"POST /v1/restart HTTP/1.1\r\n"
    b"Host: dune-restart-api\r\n"
    b"Connection: close\r\n"
    b"Content-Type: application/json\r\n"
    + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    + payload
)
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.settimeout(timeout)
    sock.connect(socket_path)
    sock.sendall(request)
    response = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        response.extend(chunk)
finally:
    sock.close()

headers, separator, body = bytes(response).partition(b"\r\n\r\n")
if not separator:
    print("host restart API returned an invalid HTTP response", file=sys.stderr)
    sys.exit(75)
try:
    status = int(headers.split(b" ", 2)[1])
    result = json.loads(body.decode("utf-8"))
except (ValueError, IndexError, UnicodeDecodeError, json.JSONDecodeError):
    print("host restart API returned an invalid response body", file=sys.stderr)
    sys.exit(75)
output = str(result.get("output") or "").strip()
if output:
    print(output)
if status != 200 or not result.get("ok"):
    error = str(result.get("error") or "host restart operation failed").strip()
    if error:
        print(error, file=sys.stderr)
    sys.exit(int(result.get("exitCode") or 1))
sys.exit(0)
PY
        exit $?
      fi
      ;;
  esac

  docker_socket="${DUNE_RESTART_DOCKER_SOCKET:-/var/run/docker.sock}"
  if [ ! -S "$docker_socket" ]; then
    cat >&2 <<EOF
Docker CLI is not available and the configured Docker API socket is unavailable: ${docker_socket}
Target: ${target}
Services: ${services}

Run this hook from the Docker host or configure a constrained restart API.
EOF
    exit 78
  fi
  python3 - "$target" "$services" <<'PY'
import json
import os
import re
import shlex
import socket
import sys
import urllib.parse


target = sys.argv[1]
services_arg = sys.argv[2]
project = os.environ.get("DUNE_RESTART_COMPOSE_PROJECT", "dune_server")
socket_path = os.environ.get("DUNE_RESTART_DOCKER_SOCKET", "/var/run/docker.sock")
dry_run = os.environ.get("DUNE_RESTART_DRY_RUN", "").lower() in ("1", "true", "yes", "on")
action = os.environ.get("DUNE_RESTART_ACTION", "restart")
phase = os.environ.get("DUNE_RESTART_PHASE", action)
host_workspace = os.environ.get("DUNE_RESTART_HOST_WORKSPACE") or os.environ.get("DUNE_ANNOUNCE_HOST_WORKSPACE", "")
compose_image = os.environ.get("DUNE_RESTART_COMPOSE_IMAGE", "docker:27.5.1-cli")
use_host_compose = os.environ.get("DUNE_RESTART_USE_HOST_COMPOSE", "true").lower() in ("1", "true", "yes", "on")
fast_dynamic_start = os.environ.get("DUNE_RESTART_FAST_DYNAMIC_START", "false").lower() in ("1", "true", "yes", "on")
compose_files = [item for item in os.environ.get("COMPOSE_FILES", "compose.yaml:compose.allmaps.yaml").split(":") if item]
env_file = os.environ.get("ENV_FILE", ".env")
watchdog_control_enabled = os.environ.get("DUNE_MAP_WATCHDOG_CONTROL", "true").lower() not in ("0", "false", "no", "off")

def read_map_catalog_cache():
    configured = os.environ.get("DUNE_MAP_CATALOG_FILE", "backups/admin-panel/map-catalog.json")
    candidates = [configured] if os.path.isabs(configured) else [
        configured,
        os.path.join(host_workspace, configured) if host_workspace else "",
        os.path.join("/workspace", configured),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                catalog = json.load(handle)
        except (OSError, ValueError):
            continue
        services = catalog.get("active_services")
        maps = catalog.get("maps")
        if catalog.get("schema_version") != 1 or not isinstance(services, list) or not isinstance(maps, list):
            continue
        if any(not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service) for service in services):
            continue
        return catalog
    return None


map_catalog = read_map_catalog_cache()
SERVICE_LAYER_SERVICES = ["director", "gateway", "text-router", "rmq-auth-shim"]
default_services = list(map_catalog["active_services"]) + SERVICE_LAYER_SERVICES if map_catalog else []


def env_value(key):
    value = os.environ.get(key)
    if value:
        return value
    candidates = [env_file]
    if host_workspace:
        candidates.extend([os.path.join(host_workspace, env_file), os.path.join("/workspace", env_file)])
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    name, candidate_value = stripped.split("=", 1)
                    if name.strip() == key:
                        return candidate_value.strip().strip("\"'")
        except OSError:
            continue
    return ""


stateful_services = {"postgres", "admin-rmq", "game-rmq"}
allow_stateful = os.environ.get("DUNE_RESTART_ALLOW_STATEFUL", "").lower() in ("1", "true", "yes", "on")

services = services_arg.split() if services_arg else []
if target == "all":
    if not map_catalog:
        print("map catalog cache is missing or invalid; run scripts/map_catalog.py on the Compose host before restarting all", file=sys.stderr)
        sys.exit(64)
    services = default_services
if not services:
    print(f"no services mapped for target {target}", file=sys.stderr)
    sys.exit(65)
blocked_stateful = sorted(stateful_services.intersection(services))
if blocked_stateful and not allow_stateful:
    print(
        "refusing to restart stateful services without DUNE_RESTART_ALLOW_STATEFUL=true: "
        + " ".join(blocked_stateful),
        file=sys.stderr,
    )
    sys.exit(66)


def decode_chunked(payload):
    decoded = bytearray()
    rest = payload
    while rest:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            raise ValueError("truncated chunked response")
        size = int(line.split(b";", 1)[0], 16)
        if size == 0:
            return bytes(decoded)
        decoded.extend(rest[:size])
        rest = rest[size + 2:]
    return bytes(decoded)


def request_timeout(path):
    if "/wait" in path:
        return int(os.environ.get("DUNE_RESTART_COMPOSE_TIMEOUT_SECONDS", os.environ.get("DUNE_ADMIN_RESTART_COMMAND_TIMEOUT_SECONDS", "1800")))
    if "/images/create" in path:
        return int(os.environ.get("DUNE_RESTART_IMAGE_PULL_TIMEOUT_SECONDS", os.environ.get("DUNE_RESTART_COMPOSE_TIMEOUT_SECONDS", "1800")))
    if "/stop" in path or "/restart" in path:
        return int(os.environ.get("DUNE_RESTART_DOCKER_STOP_TIMEOUT_SECONDS", "120"))
    return int(os.environ.get("DUNE_RESTART_DOCKER_API_TIMEOUT_SECONDS", "30"))


def docker(method, path, body=None):
    data = b"" if body is None else json.dumps(body).encode()
    request = [
        f"{method} {path} HTTP/1.1",
        "Host: docker",
        "Connection: close",
    ]
    if body is not None:
        request.extend(["Content-Type: application/json", f"Content-Length: {len(data)}"])
    request.append("")
    request.append("")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(request_timeout(path))
    sock.connect(socket_path)
    sock.sendall("\r\n".join(request).encode() + data)
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    sock.close()
    raw = b"".join(chunks)
    header, _, payload = raw.partition(b"\r\n\r\n")
    header_text = header.decode("iso-8859-1")
    status = int(header_text.split(" ", 2)[1])
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip().lower()
    if headers.get("transfer-encoding") == "chunked":
        payload = decode_chunked(payload)
    return status, payload


def remove_container(container_id):
    docker("DELETE", f"/containers/{container_id}?force=true&v=true")


def docker_logs(container_id):
    status, payload = docker("GET", f"/containers/{container_id}/logs?stdout=1&stderr=1")
    if status != 200:
        return f"failed reading helper logs: HTTP {status}"
    if payload and payload[0] in (1, 2) and len(payload) >= 8 and payload[1:4] == b"\x00\x00\x00":
        decoded = bytearray()
        offset = 0
        while offset + 8 <= len(payload):
            size = int.from_bytes(payload[offset + 4:offset + 8], "big")
            offset += 8
            decoded.extend(payload[offset:offset + size])
            offset += size
        payload = bytes(decoded)
    return payload.decode("utf-8", errors="replace").strip()


def split_image_ref(image):
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = image.rsplit(":", 1)
        return name, tag
    return image, "latest"


def ensure_image(image):
    status, _payload = docker("GET", "/images/" + urllib.parse.quote(image, safe="") + "/json")
    if status == 200:
        return
    name, tag = split_image_ref(image)
    path = "/images/create?fromImage=" + urllib.parse.quote(name) + "&tag=" + urllib.parse.quote(tag)
    status, payload = docker("POST", path)
    if status != 200:
        print(f"failed pulling helper image {image}: HTTP {status} {payload[:500]!r}", file=sys.stderr)
        sys.exit(75)
    text = payload.decode("utf-8", errors="replace")
    if '"error"' in text.lower():
        print(f"failed pulling helper image {image}: {text[-1000:]}", file=sys.stderr)
        sys.exit(75)


def run_host_shell(name, shell_command, *, include_host_ssh=False):
    if not host_workspace:
        return {"ok": False, "skipped": True, "warning": "host workspace is not configured"}
    ensure_image(compose_image)
    shell_command = "apk add --no-cache bash iproute2 util-linux sudo jq >/dev/null 2>&1 || true; " + shell_command
    helper_env = [
        f"COMPOSE_PROJECT_NAME={project}",
        "DOCKER_HOST=unix:///var/run/docker.sock",
    ]
    helper_binds = [
        f"{socket_path}:/var/run/docker.sock",
        f"{host_workspace}:{host_workspace}",
        f"{host_workspace}:/workspace",
    ]
    helper_tmpfs = {}
    if include_host_ssh:
        host_ssh_dir = env_value("DUNE_RESTART_HOST_SSH_DIR") or "/home/keith/.ssh"
        host_ssh_dir = os.path.normpath(os.path.expanduser(host_ssh_dir))
        if not os.path.isabs(host_ssh_dir):
            return {"ok": False, "error": f"host router SSH directory must be an absolute host path: {host_ssh_dir}"}
        helper_binds.append(f"{host_ssh_dir}:/mnt/host-ssh:ro")
        helper_binds.append(f"{host_ssh_dir}:{host_ssh_dir}:ro")
        # OpenSSH resolves the helper process home from its passwd entry (root),
        # not the HOME environment passed to `sh -lc`. Copy the read-only host
        # credentials into a root-owned tmpfs so host-key and private-key
        # ownership checks pass without writing credentials into the image.
        helper_tmpfs["/root/.ssh"] = "mode=0700"
        shell_command = (
            "set -e; if [ ! -d /mnt/host-ssh ] || [ -z \"$(find /mnt/host-ssh -mindepth 1 -maxdepth 1 -print -quit)\" ]; then "
            f"echo 'host router SSH directory is empty or unavailable on Docker host: {host_ssh_dir}' >&2; exit 1; fi; "
            "mkdir -p /root/.ssh; cp -R -L /mnt/host-ssh/. /root/.ssh/; "
            "find /root/.ssh -type d -exec chmod 700 {} \\; ; "
            "find /root/.ssh -type f -exec chmod 600 {} \\; ; "
            + shell_command
        )
    host_config = {
        "AutoRemove": False,
        "NetworkMode": "host",
        "UTSMode": "host",
        "CapDrop": ["ALL"],
        "CapAdd": ["DAC_OVERRIDE"],
        "SecurityOpt": ["no-new-privileges:true"],
        "Binds": helper_binds,
    }
    if helper_tmpfs:
        host_config["Tmpfs"] = helper_tmpfs
    body = {
        "Image": compose_image,
        "WorkingDir": host_workspace,
        "Cmd": ["sh", "-lc", shell_command],
        "Env": helper_env,
        "HostConfig": host_config,
        "Labels": {
            "com.snapetech.dune.role": name,
            "com.snapetech.dune.restart_job": os.environ.get("DUNE_RESTART_JOB_ID", ""),
        },
    }
    helper_name = "dune-" + name + "-" + (os.environ.get("DUNE_RESTART_JOB_ID", "manual") or "manual")
    status, payload = docker("POST", "/containers/create?name=" + urllib.parse.quote(helper_name), body)
    if status == 409:
        status, payload = docker("POST", "/containers/create", body)
    if status != 201:
        return {"ok": False, "error": f"failed creating helper: HTTP {status} {payload[:500]!r}"}
    container_id = json.loads(payload.decode())["Id"]
    try:
        status, payload = docker("POST", f"/containers/{container_id}/start")
        if status not in (204, 304):
            return {"ok": False, "error": f"failed starting helper: HTTP {status} {payload[:500]!r}"}
        status, payload = docker("POST", f"/containers/{container_id}/wait")
        if status != 200:
            return {"ok": False, "error": f"failed waiting for helper: HTTP {status} {payload[:500]!r}"}
        result = json.loads(payload.decode() or "{}")
        exit_code = int(result.get("StatusCode", 1))
        logs = docker_logs(container_id)
        return {"ok": exit_code == 0, "returncode": exit_code, "output": logs}
    finally:
        remove_container(container_id)


def map_watchdog_control(verb):
    if not watchdog_control_enabled:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    command = (
        "set -e; "
        "if [ -x /workspace/scripts/map-watchdog-control.sh ]; then "
        f"/workspace/scripts/map-watchdog-control.sh {shlex.quote(verb)} {shlex.quote(env_file)}; "
        "else echo 'map watchdog control skipped: script missing' >&2; fi"
    )
    return run_host_shell("admin-restart-watchdog", command)


def ensure_map_network_capacity():
    if phase not in ("start", "restart") or dry_run:
        return {"ok": True, "skipped": True}
    command = (
        "set -e; apk add --no-cache openssh-client python3 py3-yaml >/dev/null 2>&1; "
        "if [ ! -f /workspace/scripts/ensure-map-network-capacity.py ]; then "
        "echo 'map network capacity helper is missing' >&2; exit 1; fi; "
        f"python3 /workspace/scripts/ensure-map-network-capacity.py {shlex.quote(env_file)}"
    )
    return run_host_shell("admin-map-network-capacity", command, include_host_ssh=True)


def run_host_compose(services):
    if not host_workspace:
        print(
            "DUNE_RESTART_HOST_WORKSPACE or DUNE_ANNOUNCE_HOST_WORKSPACE is required for host Compose recreate",
            file=sys.stderr,
        )
        sys.exit(78)
    ensure_image(compose_image)
    compose_command = ["docker", "compose"]
    for file_name in compose_files:
        compose_command.extend(["-f", file_name])
    selected_profiles = sorted({
        profile
        for map_entry in (map_catalog or {}).get("maps", [])
        if map_entry.get("service") in services
        for profile in map_entry.get("profiles", [])
    })
    for profile in selected_profiles:
        compose_command.extend(["--profile", profile])
    compose_command.extend(["--env-file", env_file, "up", "--pull", "never", "-d"])
    if not fast_dynamic_start:
        compose_command.append("--force-recreate")
    compose_command.append("--no-deps")
    service_words = " ".join(services)

    # A "restart all" fleet recreate previously issued one docker compose up
    # with every configured map service at once. Compose then created/started
    # them essentially simultaneously, and production evidence (Capacity
    # Intelligence recentStarts) showed the resulting thundering herd routinely
    # took 5-13 minutes per map and repeatedly hit the 300s start timeout,
    # instead of the ~90-260s a single guarded start takes. Batch it instead.
    restart_all_batch_size = 0
    if target == "all":
        restart_all_batch_size = int(env_value("DUNE_RESTART_ALL_BATCH_SIZE") or "6")
    restart_all_batch_pause = int(env_value("DUNE_RESTART_ALL_BATCH_PAUSE_SECONDS") or "60")
    if restart_all_batch_size > 0 and len(services) > restart_all_batch_size:
        batches = [services[i:i + restart_all_batch_size] for i in range(0, len(services), restart_all_batch_size)]
    else:
        batches = [services]
    compose_invocations = []
    for batch_index, batch in enumerate(batches):
        batch_command = compose_command + list(batch)
        compose_invocations.append(" ".join(shlex.quote(part) for part in batch_command))
        if batch_index < len(batches) - 1:
            compose_invocations.append(
                "echo "
                + shlex.quote(
                    f"restart-target: batch {batch_index + 1}/{len(batches)} started"
                    f" ({len(batch)} services), pausing {restart_all_batch_pause}s before next batch"
                )
                + f"; sleep {restart_all_batch_pause}"
            )
    compose_command_shell = "; ".join(compose_invocations)
    landsraad_term_length_tuning_enabled = os.environ.get("DUNE_LANDSRAAD_TERM_LENGTH_TUNING_ENABLED") or read_env_value(env_file, "DUNE_LANDSRAAD_TERM_LENGTH_TUNING_ENABLED") or ""
    landsraad_term_alignment_guard_enabled = os.environ.get("DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED") or read_env_value(env_file, "DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED") or "true"
    landsraad_goal_tuning_enabled = os.environ.get("DUNE_LANDSRAAD_GOAL_TUNING_ENABLED") or read_env_value(env_file, "DUNE_LANDSRAAD_GOAL_TUNING_ENABLED") or ""
    landsraad_reveal_watchdog_enabled = os.environ.get("DUNE_LANDSRAAD_REVEAL_WATCHDOG_ENABLED") or read_env_value(env_file, "DUNE_LANDSRAAD_REVEAL_WATCHDOG_ENABLED") or "true"
    hardcore_dd_wipe_enabled = (
        os.environ.get("DUNE_HARDCORE_DD_WEEKLY_WIPE_ENABLED")
        or read_env_value(env_file, "DUNE_HARDCORE_DD_WEEKLY_WIPE_ENABLED")
        or os.environ.get("DUNE_PVP_DD_WEEKLY_WIPE_ENABLED")
        or read_env_value(env_file, "DUNE_PVP_DD_WEEKLY_WIPE_ENABLED")
        or ""
    )
    clear_player_rmq = os.environ.get("DUNE_RESTART_CLEAR_PLAYER_RMQ_SESSIONS") or read_env_value(env_file, "DUNE_RESTART_CLEAR_PLAYER_RMQ_SESSIONS") or ""
    if not clear_player_rmq:
        clear_player_rmq = "true" if target == "all" else "false"
    official_image_repos = " ".join(
        shlex.quote(repo)
        for repo in [
            "seabass-server-rabbitmq",
            "seabass-server-text-router",
            "seabass-server-bg-director",
            "seabass-server-gateway",
            "seabass-server-db-utils",
            "seabass-server",
        ]
    )
    ensure_official_images_shell = (
        "image_tag=$(awk -F= '$1 == \"DUNE_IMAGE_TAG\" {print $2; exit}' "
        + shlex.quote(env_file)
        + " 2>/dev/null || true); "
        "if [ -z \"$image_tag\" ]; then echo 'DUNE_IMAGE_TAG is empty; cannot verify official Dune images before start' >&2; exit 1; fi; "
        "missing_image=0; "
        + "for repo in "
        + official_image_repos
        + "; do image=\"registry.funcom.com/funcom/self-hosting/${repo}:${image_tag}\"; "
        "if ! docker image inspect \"$image\" >/dev/null 2>&1; then echo \"official Dune image is not loaded: $image\" >&2; missing_image=1; fi; "
        "done; "
        "if [ \"$missing_image\" -ne 0 ]; then "
        "if [ -x ./scripts/load-images.sh ]; then echo 'loading official Dune images from Steam package because one or more required images are missing'; "
        + f"./scripts/load-images.sh {shlex.quote(env_file)}; "
        "else echo 'one or more official Dune images are missing and scripts/load-images.sh is unavailable' >&2; exit 1; fi; "
        "fi; "
    )
    if fast_dynamic_start:
        shell_command = (
            "set -e; "
            "apk add --no-cache bash binutils gdb iproute2 util-linux sudo jq >/dev/null; "
            "if [ -x /workspace/scripts/validate-landsraad-coriolis-cycle.sh ]; then "
            f"/workspace/scripts/validate-landsraad-coriolis-cycle.sh {shlex.quote(env_file)}; "
            "fi; "
            + compose_command_shell
            + "; if [ -x /workspace/scripts/seed-gateway-neighbor.sh ]; then "
            "/workspace/scripts/seed-gateway-neighbor.sh || true; "
            "fi; "
            "if [ -x /workspace/scripts/restart-post-start-health.sh ]; then "
            f"ENV_FILE={shlex.quote(env_file)} /workspace/scripts/restart-post-start-health.sh; "
            "elif [ -x /workspace/scripts/verify-rmq-auth-path.sh ]; then "
            "/workspace/scripts/verify-rmq-auth-path.sh; "
            "fi"
        )
    else:
        shell_command = (
        "set -e; "
        + "apk add --no-cache bash binutils gdb iproute2 python3 util-linux sudo jq >/dev/null; "
        + ensure_official_images_shell
        + "if [ -x ./scripts/apply-official-db-patches.sh ]; then "
        + f"./scripts/apply-official-db-patches.sh {shlex.quote(env_file)}; "
        + "fi; "
        + "case " + shlex.quote(landsraad_term_length_tuning_enabled) + " in 1|true|yes|on) "
        + "if [ -x ./scripts/tune-landsraad-term-length.sh ]; then "
        + f"./scripts/tune-landsraad-term-length.sh {shlex.quote(env_file)} --execute; "
        + "else echo 'Landsraad term length tuning enabled but script missing' >&2; exit 1; fi ;; esac; "
        + "case " + shlex.quote(landsraad_term_alignment_guard_enabled) + " in 1|true|yes|on) "
        + "if [ -x ./scripts/validate-landsraad-term-coriolis-alignment.sh ]; then "
        + f"./scripts/validate-landsraad-term-coriolis-alignment.sh {shlex.quote(env_file)}; "
        + "else echo 'Landsraad term Coriolis alignment guard enabled but script missing' >&2; exit 1; fi ;; esac; "
        + "case " + shlex.quote(landsraad_goal_tuning_enabled) + " in 1|true|yes|on) "
        + "if [ -x ./scripts/tune-landsraad-goals.sh ]; then "
        + f"./scripts/tune-landsraad-goals.sh {shlex.quote(env_file)} --execute; "
        + "else echo 'Landsraad goal tuning enabled but script missing' >&2; exit 1; fi ;; esac; "
        + "case " + shlex.quote(landsraad_reveal_watchdog_enabled) + " in 1|true|yes|on) "
        + "if [ -x ./scripts/landsraad-reveal-watchdog.sh ]; then "
        + f"./scripts/landsraad-reveal-watchdog.sh {shlex.quote(env_file)} --execute; "
        + "else echo 'Landsraad reveal watchdog enabled but script missing' >&2; exit 1; fi ;; esac; "
        + "case " + shlex.quote(hardcore_dd_wipe_enabled) + " in 1|true|yes|on) "
        + "case " + shlex.quote(" " + service_words + " ") + " in *' deep-desert-pvp '*) "
        + "if [ -x ./scripts/wipe-hardcore-deep-desert.sh ]; then "
        + f"./scripts/wipe-hardcore-deep-desert.sh {shlex.quote(env_file)} --execute --if-due; "
        + "else echo 'Hardcore DD weekly wipe enabled but script missing' >&2; exit 1; fi ;; esac ;; esac; "
        + "case " + shlex.quote(clear_player_rmq) + " in 1|true|yes|on) "
        + "if [ -x ./scripts/clear-player-rmq-sessions.sh ]; then "
        + f"./scripts/clear-player-rmq-sessions.sh {shlex.quote(env_file)} || true; "
        + "fi ;; esac; "
        + "if [ -x /workspace/scripts/seed-gateway-neighbor.sh ]; then "
        + "/workspace/scripts/seed-gateway-neighbor.sh || true; "
        + "fi; "
        + "if [ -x /workspace/scripts/full-world-partitions.sh ]; then "
        + f"/workspace/scripts/full-world-partitions.sh {shlex.quote(env_file)}; "
        + "fi; "
        + ensure_official_images_shell
        + compose_command_shell
        + "; if [ -x /workspace/scripts/seed-gateway-neighbor.sh ]; then "
        + "/workspace/scripts/seed-gateway-neighbor.sh; "
        + "fi; "
        + "if [ -x /workspace/scripts/restart-post-start-health.sh ]; then "
        + "/workspace/scripts/restart-post-start-health.sh; "
        + "elif [ -x /workspace/scripts/verify-rmq-auth-path.sh ]; then "
        + "/workspace/scripts/verify-rmq-auth-path.sh; "
        + "fi"
    )
    body = {
        "Image": compose_image,
        "WorkingDir": host_workspace,
        "Cmd": ["sh", "-lc", shell_command],
        "Env": [
            f"COMPOSE_PROJECT_NAME={project}",
            "DOCKER_HOST=unix:///var/run/docker.sock",
            f"DUNE_RESTART_TARGET={target}",
            f"DUNE_RESTART_PHASE={phase}",
        ],
        "HostConfig": {
            "AutoRemove": False,
            "NetworkMode": "host",
            "PidMode": "host",
            "Privileged": True,
            "Binds": [
                f"{socket_path}:/var/run/docker.sock",
                f"{host_workspace}:{host_workspace}",
                f"{host_workspace}:/workspace",
            ],
        },
        "Labels": {
            "com.snapetech.dune.role": "admin-restart-compose",
            "com.snapetech.dune.restart_job": os.environ.get("DUNE_RESTART_JOB_ID", ""),
        },
    }
    helper_name = "dune-admin-restart-compose-" + (os.environ.get("DUNE_RESTART_JOB_ID", "manual") or "manual")
    status, payload = docker("POST", "/containers/create?name=" + urllib.parse.quote(helper_name), body)
    if status == 409:
        status, payload = docker("POST", "/containers/create", body)
    if status != 201:
        print(f"failed creating Compose helper: HTTP {status} {payload[:500]!r}", file=sys.stderr)
        sys.exit(75)
    container_id = json.loads(payload.decode())["Id"]
    try:
        status, payload = docker("POST", f"/containers/{container_id}/start")
        if status not in (204, 304):
            print(f"failed starting Compose helper: HTTP {status} {payload[:500]!r}", file=sys.stderr)
            sys.exit(75)
        status, payload = docker("POST", f"/containers/{container_id}/wait")
        if status != 200:
            print(f"failed waiting for Compose helper: HTTP {status} {payload[:500]!r}", file=sys.stderr)
            sys.exit(75)
        result = json.loads(payload.decode() or "{}")
        exit_code = int(result.get("StatusCode", 1))
        logs = docker_logs(container_id)
        if exit_code != 0:
            print(logs, file=sys.stderr)
            sys.exit(exit_code)
        return {"ok": True, "composeImage": compose_image, "hostWorkspace": host_workspace, "command": compose_command, "seedGatewayNeighbor": True, "output": logs}
    finally:
        remove_container(container_id)


def steam_update_enabled():
    return os.environ.get("DUNE_RESTART_CHECK_STEAM_UPDATE", "true").lower() in ("1", "true", "yes", "on")


def read_env_value(path, key):
    candidates = []
    if os.path.isabs(path):
        candidates.append(path)
    if host_workspace:
        candidates.append(os.path.join(host_workspace, path))
        candidates.append(os.path.join("/workspace", path))
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    name, value = stripped.split("=", 1)
                    if name.strip() == key:
                        return value.strip().strip("\"'")
        except OSError:
            continue
    return ""


def run_host_sietches(command):
    if target != "all":
        return {"ok": True, "skipped": True, "reason": "target is not all"}
    enabled = os.environ.get("DUNE_SIETCH_MUTATIONS_ENABLED") or read_env_value(env_file, "DUNE_SIETCH_MUTATIONS_ENABLED")
    if str(enabled).lower() not in ("1", "true", "yes", "on"):
        return {"ok": True, "skipped": True, "reason": "disabled"}
    shell_command = (
        "set -e; "
        "if [ ! -x /workspace/scripts/sietches.sh ]; then "
        "echo 'Sietch helper is enabled but missing' >&2; exit 1; fi; "
        + "/workspace/scripts/sietches.sh "
        + shlex.quote(env_file)
        + " "
        + shlex.quote(command)
        + " --execute"
    )
    return run_host_shell("admin-restart-sietches-" + command, shell_command)


def run_host_update_check():
    if not steam_update_enabled():
        return {"ok": True, "skipped": True, "reason": "DUNE_RESTART_CHECK_STEAM_UPDATE disabled"}
    if not host_workspace:
        return {"ok": True, "skipped": True, "warning": "host workspace is not configured"}
    ensure_image(compose_image)
    steam_dir = read_env_value(env_file, "DUNE_STEAM_SERVER_DIR")
    steamcmd_helper_image = os.environ.get("DUNE_RESTART_STEAMCMD_HELPER_IMAGE") or read_env_value(env_file, "DUNE_RESTART_STEAMCMD_HELPER_IMAGE") or "cm2network/steamcmd:root"
    steamcmd_env_keys = [
        "DUNE_RESTART_STEAMCMD_UPDATE",
        "DUNE_RESTART_STEAM_UPDATE_MODE",
        "DUNE_RESTART_STEAM_CLIENT_TRIGGER",
        "DUNE_RESTART_STEAM_CLIENT_WAIT_SECONDS",
        "DUNE_RESTART_STEAM_CLIENT_MIN_WAIT_SECONDS",
        "DUNE_STEAM_CLIENT_COMMAND",
        "DUNE_RESTART_STEAMCMD_REQUIRED",
        "DUNE_STEAM_APP_ID",
        "DUNE_STEAM_LOGIN",
        "DUNE_OWNED_STEAM_LOGIN",
        "DUNE_STEAM_PASSWORD",
        "DUNE_STEAM_PASSWORD_FILE",
        "DUNE_STEAMCMD_HOME",
        "DUNE_STEAMCMD_COMMAND",
        "DUNE_STEAMCMD_VALIDATE",
        "DUNE_STEAMCMD_TIMEOUT_SECONDS",
    ]
    steamcmd_env = []
    for key in steamcmd_env_keys:
        value = os.environ.get(key)
        if value is None:
            value = read_env_value(env_file, key)
        if value:
            steamcmd_env.extend(["-e", f"{key}={value}"])
    steamcmd_container = ""
    if steam_dir and os.path.isabs(steam_dir) and steamcmd_helper_image:
        steamcmd_home = os.environ.get("DUNE_STEAMCMD_HOME") or read_env_value(env_file, "DUNE_STEAMCMD_HOME") or os.path.expanduser("~/.steamcmd-dune")
        steam_mount = steam_dir
        if os.path.basename(os.path.dirname(steam_dir)) == "common" and os.path.basename(os.path.dirname(os.path.dirname(steam_dir))) == "steamapps":
            steam_mount = os.path.dirname(os.path.dirname(os.path.dirname(steam_dir)))
        docker_run = [
            "docker", "run", "--rm",
            "--network", "host",
            "-v", f"{host_workspace}:{host_workspace}",
            "-v", f"{host_workspace}:/workspace",
            "-v", f"{steam_mount}:{steam_mount}",
            "-v", f"{steamcmd_home}:{steamcmd_home}",
            "-w", host_workspace,
            "-e", f"HOME={steamcmd_home}",
        ]
        docker_run.extend(steamcmd_env)
        docker_run.extend([
            steamcmd_helper_image,
            "bash", "./scripts/update-steam-tool.sh", env_file,
        ])
        steamcmd_container = (
            "steamcmd_uid_gid=$(stat -c %u:%g " + shlex.quote(steam_dir) + "); "
            + " ".join(shlex.quote(part) for part in docker_run[:3])
            + " --user \"$steamcmd_uid_gid\" "
            + " ".join(shlex.quote(part) for part in docker_run[3:])
        )
    steam_client_command = os.environ.get("DUNE_STEAM_CLIENT_COMMAND") or read_env_value(env_file, "DUNE_STEAM_CLIENT_COMMAND") or "steam"
    steam_client_min_wait = os.environ.get("DUNE_RESTART_STEAM_CLIENT_MIN_WAIT_SECONDS") or read_env_value(env_file, "DUNE_RESTART_STEAM_CLIENT_MIN_WAIT_SECONDS") or "30"
    steam_client_trigger = os.environ.get("DUNE_RESTART_STEAM_CLIENT_TRIGGER") or read_env_value(env_file, "DUNE_RESTART_STEAM_CLIENT_TRIGGER") or "true"
    steam_client_host_trigger = (
        "steam_pid=\"$(pgrep -u $(stat -c %u " + shlex.quote(steam_dir or host_workspace) + ") -f '/Steam/.*/steam|/steam( |$)' | head -1 || true)\"; "
        "if [ -n \"$steam_pid\" ] && command -v nsenter >/dev/null 2>&1; then "
        "if [ " + shlex.quote(steam_client_trigger) + " = false ] || [ " + shlex.quote(steam_client_trigger) + " = 0 ]; then "
        "echo 'Host Steam client detected; validation trigger disabled'; "
        "else echo 'Requesting host Steam client validation for app " + shlex.quote(read_env_value(env_file, "DUNE_STEAM_APP_ID") or "4754530") + "'; "
        "steam_uid=\"$(stat -c %u " + shlex.quote(steam_dir or host_workspace) + ")\"; "
        "steam_gid=\"$(stat -c %g " + shlex.quote(steam_dir or host_workspace) + ")\"; "
        "steam_home=\"" + shlex.quote((steam_dir or "").split("/.local/share/Steam/", 1)[0] if "/.local/share/Steam/" in (steam_dir or "") else "") + "\"; "
        "nsenter --target \"$steam_pid\" --mount --uts --ipc --net --pid --setuid \"$steam_uid\" --setgid \"$steam_gid\" "
        "env HOME=\"${steam_home:-/tmp}\" XDG_RUNTIME_DIR=\"/run/user/$steam_uid\" DISPLAY=\"${DISPLAY:-:0}\" "
        + shlex.quote(steam_client_command or "steam") + " "
        + shlex.quote("steam://validate/" + (read_env_value(env_file, "DUNE_STEAM_APP_ID") or "4754530"))
        + " >/dev/null 2>&1 || true; fi; "
        "sleep " + shlex.quote(steam_client_min_wait) + "; "
        "else false; fi"
    )
    image_tars = [
        "images/battlegroup/server-rabbitmq.tar",
        "images/battlegroup/server-text-router.tar",
        "images/battlegroup/server-bg-director.tar",
        "images/battlegroup/server-gateway.tar",
        "images/battlegroup/server-db-utils.tar",
        "images/battlegroup/server.tar",
    ]
    load_tars = image_tars + ["images/prerequisites/igw-postgres.tar"]
    image_repos = [
        "seabass-server-rabbitmq",
        "seabass-server-text-router",
        "seabass-server-bg-director",
        "seabass-server-gateway",
        "seabass-server-db-utils",
        "seabass-server",
    ]
    image_tars_shell = " ".join(shlex.quote(item) for item in image_tars)
    load_tars_shell = " ".join(shlex.quote(item) for item in load_tars)
    image_repos_shell = " ".join(shlex.quote(item) for item in image_repos)
    steam_dir_shell = shlex.quote(steam_dir or "")
    env_file_shell = shlex.quote(env_file)
    inline_package_ingest = (
        "steam_dir=" + steam_dir_shell + "; "
        "env_file=" + env_file_shell + "; "
        "tag_file=$(mktemp); "
        "missing=0; "
        "for rel in " + image_tars_shell + "; do "
        "path=\"$steam_dir/$rel\"; "
        "if [ ! -f \"$path\" ]; then echo \"warn: missing package image tar: $path\" >&2; missing=$((missing + 1)); continue; fi; "
        "tar -xOf \"$path\" manifest.json 2>/dev/null | "
        "tr ',' '\\n' | "
        "sed -n 's/.*registry\\.funcom\\.com\\/funcom\\/self-hosting\\/seabass-server[^:\" ]*:\\([^\"]*\\).*/\\1/p' >> \"$tag_file\" || true; "
        "done; "
        "tags=$(sort -u \"$tag_file\" | sed '/^$/d'); "
        "tag_count=$(printf '%s\\n' \"$tags\" | sed '/^$/d' | wc -l | tr -d ' '); "
        "current_tag=$(awk -F= '$1 == \"DUNE_IMAGE_TAG\" {print $2; exit}' \"$env_file\" 2>/dev/null || true); "
        "echo \"env file: $env_file\"; echo \"Steam server dir: $steam_dir\"; echo \"current DUNE_IMAGE_TAG: ${current_tag:-unset}\"; "
        "app_id=${DUNE_STEAM_APP_ID:-" + shlex.quote(read_env_value(env_file, "DUNE_STEAM_APP_ID") or "4754530") + "}; "
        "appmanifest=\"\"; "
        "if [ -f \"$steam_dir/steamapps/appmanifest_${app_id}.acf\" ]; then appmanifest=\"$steam_dir/steamapps/appmanifest_${app_id}.acf\"; "
        "elif printf '%s' \"$steam_dir\" | grep -q '/steamapps/common/'; then appmanifest=\"${steam_dir%%/common/*}/appmanifest_${app_id}.acf\"; "
        "else dir=\"$steam_dir\"; while [ \"$dir\" != / ] && [ -n \"$dir\" ]; do "
        "if [ \"$(basename \"$dir\")\" = steamapps ]; then appmanifest=\"$dir/appmanifest_${app_id}.acf\"; break; fi; "
        "dir=$(dirname \"$dir\"); done; fi; "
        "installed_buildid=\"\"; target_buildid=\"\"; "
        "loaded_buildid_file=\"$steam_dir/images/battlegroup/.loaded_buildid\"; loaded_buildid=\"\"; "
        "if [ -n \"$appmanifest\" ] && [ -f \"$appmanifest\" ]; then "
        "installed_buildid=$(awk '$1 == \"\\\"buildid\\\"\" {gsub(/\\\"/, \"\", $2); print $2; exit}' \"$appmanifest\" 2>/dev/null || true); "
        "target_buildid=$(awk '$1 == \"\\\"TargetBuildID\\\"\" {gsub(/\\\"/, \"\", $2); print $2; exit}' \"$appmanifest\" 2>/dev/null || true); "
        "fi; "
        "if [ -f \"$loaded_buildid_file\" ]; then loaded_buildid=$(cat \"$loaded_buildid_file\" 2>/dev/null || true); fi; "
        "if [ -n \"$installed_buildid\" ]; then echo \"Steam installed buildid: $installed_buildid\"; fi; "
        "if [ -n \"$target_buildid\" ]; then echo \"Steam target buildid: $target_buildid\"; fi; "
        "if [ -n \"$loaded_buildid\" ]; then echo \"last loaded buildid: $loaded_buildid\"; fi; "
        "if [ -n \"$installed_buildid\" ] && [ -n \"$target_buildid\" ] && [ \"$installed_buildid\" != \"$target_buildid\" ]; then "
        "echo 'status: Steam package install incomplete' >&2; "
        "echo \"installed buildid: $installed_buildid\" >&2; "
        "echo \"target buildid: $target_buildid\" >&2; "
        "echo 'rerun the Steam package update before loading images or restarting maps' >&2; "
        "rm -f \"$tag_file\"; exit 2; "
        "fi; "
        "echo 'package server tags:'; printf '%s\\n' \"$tags\" | sed 's/^/  /'; "
        "if [ \"$missing\" -gt 0 ] || [ \"$tag_count\" -ne 1 ]; then "
        "echo 'Steam package update check could not determine a safe tag; aborting restart before starting old images' >&2; rm -f \"$tag_file\"; exit 2; "
        "fi; "
        "package_tag=$(printf '%s\\n' \"$tags\" | sed '/^$/d' | head -1); "
        "if [ \"$current_tag\" = \"$package_tag\" ]; then "
        "echo 'status: current'; "
        "missing_image=0; "
        "for repo in " + image_repos_shell + "; do "
        "image=\"registry.funcom.com/funcom/self-hosting/${repo}:${current_tag}\"; "
        "if ! docker image inspect \"$image\" >/dev/null 2>&1; then echo \"official Dune image is not loaded: $image\" >&2; missing_image=1; fi; "
        "done; "
        "same_tag_build_changed=0; "
        "if [ -n \"$installed_buildid\" ] && [ \"$installed_buildid\" != \"$loaded_buildid\" ]; then same_tag_build_changed=1; fi; "
        "if [ \"$missing_image\" -ne 0 ] || [ \"$same_tag_build_changed\" -ne 0 ]; then "
        "if [ \"$same_tag_build_changed\" -ne 0 ]; then echo \"loading official Dune images from Steam package because Steam build changed under same Docker tag: ${loaded_buildid:-unset} -> $installed_buildid\"; "
        "else echo 'loading official Dune images from Steam package because one or more required images are missing'; fi; "
        "for rel in " + load_tars_shell + "; do "
        "path=\"$steam_dir/$rel\"; "
        "if [ ! -f \"$path\" ]; then echo \"missing image tar: $path\" >&2; rm -f \"$tag_file\"; exit 1; fi; "
        "docker load -i \"$path\"; "
        "done; "
        "if [ -n \"$installed_buildid\" ]; then mkdir -p \"$(dirname \"$loaded_buildid_file\")\"; printf '%s\\n' \"$installed_buildid\" > \"$loaded_buildid_file\"; fi; "
        "fi; "
        "rm -f \"$tag_file\"; exit 0; fi; "
        "current_build=${current_tag%%-*}; package_build=${package_tag%%-*}; "
        "case \"$current_build:$package_build\" in *[!0-9:]*|:*|*:) ;; *) "
        "if [ \"$package_build\" -lt \"$current_build\" ]; then echo 'status: package older than current DUNE_IMAGE_TAG'; echo \"keeping current tag: $current_tag\"; rm -f \"$tag_file\"; exit 0; fi; "
        ";; esac; "
        "echo 'status: update available'; echo \"next tag: $package_tag\"; "
        "for rel in " + load_tars_shell + "; do "
        "path=\"$steam_dir/$rel\"; "
        "if [ ! -f \"$path\" ]; then echo \"missing image tar: $path\" >&2; rm -f \"$tag_file\"; exit 1; fi; "
        "docker load -i \"$path\"; "
        "done; "
        "if [ -n \"$installed_buildid\" ]; then mkdir -p \"$(dirname \"$loaded_buildid_file\")\"; printf '%s\\n' \"$installed_buildid\" > \"$loaded_buildid_file\"; fi; "
        "python3 ./scripts/update-env-file.py \"$env_file\" --allow-protected-link --quiet --set DUNE_IMAGE_TAG \"$package_tag\"; "
        "echo \"updated $env_file: DUNE_IMAGE_TAG=$package_tag\"; "
        "rm -f \"$tag_file\""
    )
    shell_command = (
        "set -e; "
        "apk add --no-cache bash python3 util-linux >/dev/null 2>&1 || true; "
        "if [ -x ./scripts/update-steam-tool.sh ]; then "
        "steam_mode=${DUNE_RESTART_STEAM_UPDATE_MODE:-" + shlex.quote(read_env_value(env_file, "DUNE_RESTART_STEAM_UPDATE_MODE") or "auto") + "}; "
        "if [ \"$steam_mode\" = none ]; then "
        "echo 'Steam package acquisition disabled; using only the already staged candidate'; "
        "elif [ \"$steam_mode\" = client ]; then "
        + steam_client_host_trigger + " || echo 'Steam client package refresh skipped: no host Steam client visible from helper' >&2; "
        "elif [ \"$steam_mode\" != steamcmd ] && ( " + steam_client_host_trigger + " ); then "
        "true; "
        "elif command -v steamcmd >/dev/null 2>&1; then "
        f"./scripts/update-steam-tool.sh {shlex.quote(env_file)}; "
        + (f"elif [ -n {shlex.quote(steamcmd_container)} ]; then {steamcmd_container}; " if steamcmd_container else "")
        + "else "
        "echo 'SteamCMD package update skipped: steamcmd is unavailable and no helper image is configured' >&2; "
        "fi; "
        "else "
        "echo 'SteamCMD package update skipped: scripts/update-steam-tool.sh is missing or not executable' >&2; "
        "fi; "
        + inline_package_ingest
    )
    binds = [
        f"{socket_path}:/var/run/docker.sock",
        f"{host_workspace}:{host_workspace}",
        f"{host_workspace}:/workspace",
    ]
    if steam_dir and os.path.isabs(steam_dir):
        steam_mount = steam_dir
        if os.path.basename(os.path.dirname(steam_dir)) == "common" and os.path.basename(os.path.dirname(os.path.dirname(steam_dir))) == "steamapps":
            steam_mount = os.path.dirname(os.path.dirname(os.path.dirname(steam_dir)))
        binds.append(f"{steam_mount}:{steam_mount}")
    body = {
        "Image": compose_image,
        "WorkingDir": host_workspace,
        "Cmd": ["sh", "-lc", shell_command],
        "Env": [
            "DOCKER_HOST=unix:///var/run/docker.sock",
        ],
        "HostConfig": {
            "AutoRemove": False,
            "NetworkMode": "host",
            "PidMode": "host",
            "Privileged": True,
            "Binds": binds,
        },
        "Labels": {
            "com.snapetech.dune.role": "admin-restart-steam-update",
            "com.snapetech.dune.restart_job": os.environ.get("DUNE_RESTART_JOB_ID", ""),
        },
    }
    helper_name = "dune-admin-steam-update-" + (os.environ.get("DUNE_RESTART_JOB_ID", "manual") or "manual")
    status, payload = docker("POST", "/containers/create?name=" + urllib.parse.quote(helper_name), body)
    if status == 409:
        status, payload = docker("POST", "/containers/create", body)
    if status != 201:
        print(f"failed creating Steam update helper: HTTP {status} {payload[:500]!r}", file=sys.stderr)
        sys.exit(75)
    container_id = json.loads(payload.decode())["Id"]
    try:
        status, payload = docker("POST", f"/containers/{container_id}/start")
        if status not in (204, 304):
            print(f"failed starting Steam update helper: HTTP {status} {payload[:500]!r}", file=sys.stderr)
            sys.exit(75)
        status, payload = docker("POST", f"/containers/{container_id}/wait")
        if status != 200:
            print(f"failed waiting for Steam update helper: HTTP {status} {payload[:500]!r}", file=sys.stderr)
            sys.exit(75)
        result = json.loads(payload.decode() or "{}")
        exit_code = int(result.get("StatusCode", 1))
        logs = docker_logs(container_id)
        if exit_code != 0:
            print(logs, file=sys.stderr)
            sys.exit(exit_code)
        return {"ok": True, "output": logs}
    finally:
        remove_container(container_id)


if phase == "update":
    if dry_run:
        print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "dryRun": True, "steamUpdate": True}, separators=(",", ":")))
        sys.exit(0)
    result = run_host_update_check() if use_host_compose else {"ok": True, "skipped": True, "warning": "host Compose helper disabled"}
    print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "steamUpdate": True, "result": result}, separators=(",", ":")))
    sys.exit(0)

if phase == "reboot":
    if dry_run:
        print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "dryRun": True}, separators=(",", ":")))
        sys.exit(0)
    request_command = (
        "set -e; apk add --no-cache bash util-linux >/dev/null 2>&1 || true; "
        + "/workspace/scripts/update-reboot-resume.sh request "
        + shlex.quote(env_file) + " "
        + shlex.quote(os.environ.get("DUNE_RESTART_JOB_ID", "manual")) + " "
        + shlex.quote(target) + " "
        + shlex.quote(" ".join(services))
    )
    result = run_host_shell("admin-update-reboot", request_command)
    print(json.dumps({"ok": bool(result.get("ok")), "target": target, "action": action, "phase": phase, "result": result}, separators=(",", ":")))
    sys.exit(0 if result.get("ok") else int(result.get("returncode", 1)))


if phase == "recovery":
    if dry_run:
        print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "dryRun": True}, separators=(",", ":")))
        sys.exit(0)
    recovery_wait = os.environ.get("DUNE_WATCH_RECOVERY_WAIT", "300")
    recovery_timeout = os.environ.get("DUNE_WATCH_RECOVER_TIMEOUT", "900")
    recovery_command = (
        "set -e; "
        "if [ ! -x /workspace/scripts/watch-maps.sh ]; then "
        "echo 'watch-maps recovery script is missing' >&2; exit 1; fi; "
        "if [ -x /workspace/scripts/map-watchdog-control.sh ]; then "
        f"/workspace/scripts/map-watchdog-control.sh pause {shlex.quote(env_file)} || true; "
        "fi; "
        "cleanup() { "
        "if [ -x /workspace/scripts/map-watchdog-control.sh ]; then "
        f"/workspace/scripts/map-watchdog-control.sh resume {shlex.quote(env_file)} || true; "
        "fi; "
        "}; trap cleanup EXIT; "
        f"DUNE_WATCH_STARTUP_GRACE=0 DUNE_WATCH_RECOVERY_WAIT={shlex.quote(recovery_wait)} "
        f"DUNE_WATCH_RECOVER_TIMEOUT={shlex.quote(recovery_timeout)} "
        f"bash /workspace/scripts/watch-maps.sh {shlex.quote(env_file)} --once"
    )
    result = run_host_shell("admin-restart-recovery", recovery_command)
    print(json.dumps({"ok": bool(result.get("ok")), "target": target, "action": action, "phase": phase, "result": result}, separators=(",", ":")))
    sys.exit(0 if result.get("ok") else int(result.get("returncode", 1)))


if phase in ("start", "restart") and use_host_compose:
    if dry_run:
        print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "dryRun": True, "hostCompose": True, "services": services}, separators=(",", ":")))
        sys.exit(0)
    network_result = ensure_map_network_capacity()
    if not network_result.get("ok"):
        print(network_result.get("output") or network_result.get("error") or "map network capacity check failed", file=sys.stderr)
        sys.exit(int(network_result.get("returncode") or 1))
    if not fast_dynamic_start:
        map_watchdog_control("stop")
    result = run_host_compose(services)
    sietch_result = run_host_sietches("reconcile")
    if not sietch_result.get("ok"):
        print(sietch_result.get("output") or sietch_result.get("error") or "Sietch reconcile failed", file=sys.stderr)
        sys.exit(int(sietch_result.get("returncode") or 1))
    if not fast_dynamic_start:
        map_watchdog_control("start")
    print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "hostCompose": True, "affected": services, "result": result, "sietches": sietch_result}, separators=(",", ":")))
    sys.exit(0)


label_filters = [
    f"label=com.docker.compose.project={project}",
]
for service in services:
    label_filters.append(f"label=com.docker.compose.service={service}")

restarted = []
missing = []
if phase in ("start", "restart") and not dry_run:
    network_result = ensure_map_network_capacity()
    if not network_result.get("ok"):
        print(network_result.get("output") or network_result.get("error") or "map network capacity check failed", file=sys.stderr)
        sys.exit(int(network_result.get("returncode") or 1))
if phase in ("shutdown", "stop", "restart") and not dry_run:
    sietch_result = run_host_sietches("stop-managed")
    if not sietch_result.get("ok"):
        print(sietch_result.get("output") or sietch_result.get("error") or "Sietch stop failed", file=sys.stderr)
        sys.exit(int(sietch_result.get("returncode") or 1))
if phase in ("shutdown", "stop", "start", "restart") and not dry_run:
    map_watchdog_control("stop")
for service in services:
    filters = {
        "label": [
            f"com.docker.compose.project={project}",
            f"com.docker.compose.service={service}",
        ]
    }
    query = urllib.parse.urlencode({"all": "true", "filters": json.dumps(filters)})
    status, payload = docker("GET", f"/containers/json?{query}")
    if status != 200:
        print(f"failed listing containers for {service}: HTTP {status}", file=sys.stderr)
        sys.exit(75)
    containers = json.loads(payload.decode() or "[]")
    if not containers:
        missing.append(service)
        continue
    for container in containers:
        container_id = container["Id"]
        if dry_run:
            restarted.append(service)
            continue
        if phase in ("shutdown", "stop"):
            endpoint = "stop?t=30"
        elif phase == "start":
            endpoint = "start"
        else:
            endpoint = "restart?t=30"
        status, payload = docker("POST", f"/containers/{container_id}/{endpoint}")
        if status not in (204, 304):
            print(f"failed {phase} for {service}: HTTP {status} {payload[:200]!r}", file=sys.stderr)
            sys.exit(75)
        restarted.append(service)

if phase in ("start", "restart") and not dry_run:
    sietch_result = run_host_sietches("reconcile")
    if not sietch_result.get("ok"):
        print(sietch_result.get("output") or sietch_result.get("error") or "Sietch reconcile failed", file=sys.stderr)
        sys.exit(int(sietch_result.get("returncode") or 1))
    map_watchdog_control("start")
    if target == "all":
        build_check = run_host_shell(
            "admin-verify-latest-build",
            "set -e; if [ ! -x /workspace/scripts/verify-running-image-tag.sh ]; then "
            "echo 'latest Steam build verifier is missing' >&2; exit 1; fi; "
            f"/workspace/scripts/verify-running-image-tag.sh {shlex.quote(env_file)} --versions-only",
        )
        if not build_check.get("ok"):
            print(build_check.get("output") or build_check.get("error") or "latest Steam build check failed", file=sys.stderr)
            sys.exit(int(build_check.get("returncode") or 1))

print(json.dumps({"ok": True, "target": target, "action": action, "phase": phase, "dryRun": dry_run, "affected": restarted, "missing": missing}, separators=(",", ":")))
PY
  exit $?
fi

if { [ "$phase" = "start" ] || [ "$phase" = "restart" ]; } \
    && [ "${DUNE_RESTART_DRY_RUN:-}" != "true" ] \
    && [ "${DUNE_RESTART_DRY_RUN:-}" != "1" ] \
    && [ -f "$(dirname "$0")/ensure-map-network-capacity.py" ]; then
  python3 "$(dirname "$0")/ensure-map-network-capacity.py" "${ENV_FILE:-.env}"
fi

if [ -x "$(dirname "$0")/compose-files.sh" ]; then
  COMPOSE_FILES="$("$(dirname "$0")/compose-files.sh" "${ENV_FILE:-.env}")"
  export COMPOSE_FILES
fi
compose_files="${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
map_catalog_script="$(dirname -- "$0")/map_catalog.py"
if [ ! -x "$map_catalog_script" ]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$map_catalog_script" >&2
  exit 1
fi
map_services="$("$map_catalog_script" "${ENV_FILE:-.env}" --field services --write-cache)"
map_profiles="$("$map_catalog_script" "${ENV_FILE:-.env}" --field all-profiles)"
set -- docker compose
old_ifs="$IFS"
IFS=:
for file in $compose_files; do
  set -- "$@" -f "$file"
done
IFS="$old_ifs"
set -- "$@" --env-file "${ENV_FILE:-.env}"

if [ "$target" = "all" ]; then
  if [ -z "$map_services" ]; then
    printf 'map catalog returned no workers; refusing to restart all with an empty map roster\n' >&2
    exit 64
  fi
  services="$map_services director gateway text-router rmq-auth-shim"
fi

if [ -z "$services" ]; then
  printf 'no services mapped for target %s\n' "$target" >&2
  exit 65
fi

for profile in $map_profiles; do
  set -- "$@" --profile "$profile"
done

# shellcheck disable=SC2086
case " $services " in
  *" postgres "*|*" admin-rmq "*|*" game-rmq "*)
    if [ "${DUNE_RESTART_ALLOW_STATEFUL:-}" != "true" ] && [ "${DUNE_RESTART_ALLOW_STATEFUL:-}" != "1" ]; then
      printf 'refusing to restart stateful services without DUNE_RESTART_ALLOW_STATEFUL=true: %s\n' "$services" >&2
      exit 66
    fi
    ;;
esac
if [ "${DUNE_RESTART_DRY_RUN:-}" = "true" ] || [ "${DUNE_RESTART_DRY_RUN:-}" = "1" ]; then
  printf '{"ok":true,"target":"%s","action":"%s","phase":"%s","dryRun":true,"services":"%s"}\n' "$target" "$action" "$phase" "$services"
  exit 0
fi
if [ "$phase" = "update" ]; then
  image_tag_before="$(env_file_value DUNE_IMAGE_TAG "${ENV_FILE:-.env}")"
  run_steam_update_check
  image_tag_after="$(env_file_value DUNE_IMAGE_TAG "${ENV_FILE:-.env}")"
  if [ -n "$image_tag_after" ] && [ "$image_tag_after" != "$image_tag_before" ]; then
    printf 'DUNE_STEAM_UPDATE_APPLIED=%s:%s\n' "$image_tag_before" "$image_tag_after"
  fi
  exit 0
fi
if [ "$phase" = "reboot" ]; then
  exec ./scripts/update-reboot-resume.sh request "${ENV_FILE:-.env}" "${DUNE_RESTART_JOB_ID:-manual}" "$target" "$services"
fi
if [ "$phase" = "shutdown" ] || [ "$phase" = "stop" ]; then
  map_watchdog_control stop
  exec "$@" stop -t 30 $services
fi
if [ "$phase" = "start" ]; then
  case "$fast_dynamic_start" in
    1|true|yes|on)
      run_landsraad_coriolis_guard
      # Compose starts the existing stopped container when its config hash is
      # current and recreates it only when configuration actually changed.
      "$@" up --pull never -d --no-deps $services
      seed_gateway_neighbors
      if [ -x ./scripts/restart-post-start-health.sh ]; then
        ./scripts/restart-post-start-health.sh
      elif [ -x ./scripts/verify-rmq-auth-path.sh ]; then
        ./scripts/verify-rmq-auth-path.sh
      fi
      case "${DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED:-true}" in
        1|true|yes|on|TRUE|True|YES|ON)
          # The generic post-start hook can run before a newly started game
          # process exists and still succeed after patching older maps. Retry
          # against only the requested containers and require readback.
          target_logoff_containers=""
          for service in $services; do
            container_name="$("$@" ps --format '{{.Name}}' "$service" 2>/dev/null | head -1)"
            [ -z "$container_name" ] || target_logoff_containers="$target_logoff_containers $container_name"
          done
          target_logoff_containers="${target_logoff_containers# }"
          patched=false
          for _ in $(seq 1 36); do
            if [ -n "$target_logoff_containers" ] \
              && DUNE_LOGOFF_TIMER_CONTAINERS="$target_logoff_containers" ./scripts/patch-logoff-timers-runtime.sh --local \
              && DUNE_LOGOFF_TIMER_CONTAINERS="$target_logoff_containers" ./scripts/patch-logoff-timers-runtime.sh --local --dry-run; then
              patched=true
              break
            fi
            sleep 5
          done
          if [ "$patched" != true ]; then
            echo "required logoff timer runtime patch did not verify for: $target_logoff_containers" >&2
            exit 1
          fi
          ;;
      esac
      exit 0
      ;;
  esac
  ensure_official_images_loaded
  map_watchdog_control stop
  pre_start_hygiene
  seed_gateway_neighbors
  if [ -x ./scripts/full-world-partitions.sh ]; then
    ./scripts/full-world-partitions.sh "${ENV_FILE:-.env}"
  fi
  ensure_official_images_loaded
  "$@" up --pull never -d --force-recreate --no-deps $services
  seed_gateway_neighbors
  if [ -x ./scripts/restart-post-start-health.sh ]; then
    ./scripts/restart-post-start-health.sh
  elif [ -x ./scripts/verify-rmq-auth-path.sh ]; then
    ./scripts/verify-rmq-auth-path.sh
  fi
  map_watchdog_control start
  exit 0
fi
ensure_official_images_loaded
map_watchdog_control stop
pre_start_hygiene
seed_gateway_neighbors
ensure_official_images_loaded
"$@" up --pull never -d --force-recreate --no-deps $services
seed_gateway_neighbors
if [ -x ./scripts/restart-post-start-health.sh ]; then
  ./scripts/restart-post-start-health.sh
elif [ -x ./scripts/verify-rmq-auth-path.sh ]; then
  ./scripts/verify-rmq-auth-path.sh
fi
map_watchdog_control start
