#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-${DUNE_ENV_FILE:-.env}}"
default_files="${DUNE_DEFAULT_COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
"$script_dir/protect-compose-secrets.sh" "$repo_root"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

file_exists() {
  [[ -f "$1" ]]
}

add_file() {
  local file="$1" existing
  [[ -n "$file" ]] || return 0
  for existing in "${resolved_files[@]}"; do
    [[ "$existing" == "$file" ]] && return 0
  done
  resolved_files+=("$file")
}

host_matches_local() {
  local host="$1" local_short local_full
  [[ -n "$host" ]] || return 1
  if [[ "$host" == "${DUNE_CURRENT_HOST:-$(read_env DUNE_CURRENT_HOST)}" ]]; then
    return 0
  fi
  local_short="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
  local_full="$(hostname 2>/dev/null || true)"
  [[ "$host" == "$local_short" || "$host" == "$local_full" || "$host" == "localhost" ]]
}

postgres_uses_remote_root() {
  local remote_root="$1" container_runtime="${CONTAINER_RUNTIME:-docker}" mount
  [[ -n "$remote_root" ]] || return 1
  mount="$("$container_runtime" inspect dune_server-postgres-1 \
    --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null || true)"
  [[ "$mount" == "$remote_root/data" || "$mount" == "$remote_root"/data/* ]]
}

compose_files="${COMPOSE_FILES:-$(read_env COMPOSE_FILES)}"
compose_files="${compose_files:-$default_files}"

resolved_files=()
IFS=':' read -ra requested_files <<< "$compose_files"
for compose_file in "${requested_files[@]}"; do
  add_file "$compose_file"
done

map_template="${DUNE_WORLD_TEMPLATE_PATH:-$(read_env DUNE_WORLD_TEMPLATE_PATH)}"
map_template="${map_template:-/home/keith/dune-steamcmd-server/scripts/setup/templates/world-template.yaml}"
if [[ "$map_template" != /* ]]; then map_template="$repo_root/$map_template"; fi
map_discovery_enabled="${DUNE_MAP_AUTO_DISCOVERY_ENABLED:-$(read_env DUNE_MAP_AUTO_DISCOVERY_ENABLED)}"
map_discovery_enabled="${map_discovery_enabled:-auto}"
if [[ "$map_discovery_enabled" == "auto" ]]; then
  if [[ -f "$map_template" ]]; then
    map_discovery_enabled=true
  else
    map_discovery_enabled=false
  fi
fi
case "$map_discovery_enabled" in
  true)
    if [[ ! -f "$map_template" ]]; then
      printf 'automatic map discovery is enabled but the world template is missing: %s\n' "$map_template" >&2
      exit 1
    fi
    map_overlay="${DUNE_MAP_WORKER_OVERLAY_FILE:-$(read_env DUNE_MAP_WORKER_OVERLAY_FILE)}"
    map_overlay="${map_overlay:-$repo_root/backups/admin-panel/compose.auto-maps.yaml}"
    if [[ "$map_overlay" != /* ]]; then map_overlay="$repo_root/$map_overlay"; fi
    map_game_port_range="$(read_env GAME_UDP_PORT_RANGE)"
    map_igw_port_range="$(read_env IGW_UDP_PORT_RANGE)"
    map_game_port_range="${map_game_port_range:-${GAME_UDP_PORT_RANGE:-}}"
    map_igw_port_range="${map_igw_port_range:-${IGW_UDP_PORT_RANGE:-}}"
    GAME_UDP_PORT_RANGE="$map_game_port_range" \
    IGW_UDP_PORT_RANGE="$map_igw_port_range" \
    python3 "$script_dir/sync-map-workers.py" "$env_file" \
      --root "$repo_root" \
      --world-template "$map_template" \
      --compose-files "$compose_files" \
      --output "$map_overlay" >&2
    add_file "$map_overlay"
    ;;
  false) ;;
  *)
    printf 'DUNE_MAP_AUTO_DISCOVERY_ENABLED must be true, false, or auto; got: %s\n' "$map_discovery_enabled" >&2
    exit 2
    ;;
esac

postgres_remote_host="${POSTGRES_REMOTE_REPLICA_HOST:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}"
postgres_remote_root="${POSTGRES_REMOTE_REPLICA_ROOT:-$(read_env POSTGRES_REMOTE_REPLICA_ROOT)}"
if file_exists compose.failover-standby.yaml; then
  if host_matches_local "$postgres_remote_host" || postgres_uses_remote_root "$postgres_remote_root"; then
    add_file compose.failover-standby.yaml
  fi
fi

building_piece_enabled="${DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED:-$(read_env DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED)}"
building_piece_mode="${DUNE_BUILDING_PIECE_LIMIT_MODE:-$(read_env DUNE_BUILDING_PIECE_LIMIT_MODE)}"
building_piece_mode="${building_piece_mode:-server-settings}"
if [[ "$building_piece_enabled" == "true" && "$building_piece_mode" == "pak" ]] && file_exists compose.building-piece-limit.yaml; then
  add_file compose.building-piece-limit.yaml
fi

lua_mods_enabled="${DUNE_PROBE_LOADER_LUA_MODS_ENABLED:-$(read_env DUNE_PROBE_LOADER_LUA_MODS_ENABLED)}"
if [[ "$lua_mods_enabled" == "true" ]] && file_exists compose.ue4ss-building-piece-limit.yaml; then
  add_file compose.ue4ss-building-piece-limit.yaml
fi

landsraad_vendor_gate_enabled="${DUNE_LANDSRAAD_VENDOR_FACTION_GATE_PATCH_ENABLED:-$(read_env DUNE_LANDSRAAD_VENDOR_FACTION_GATE_PATCH_ENABLED)}"
if [[ "$landsraad_vendor_gate_enabled" == "true" ]] && file_exists compose.landsraad-vendor-faction-gate.yaml; then
  add_file compose.landsraad-vendor-faction-gate.yaml
fi

brt_dd_invalid_map_enabled="${DUNE_BRT_DD_INVALID_MAP_BINARY_PATCH_ENABLED:-$(read_env DUNE_BRT_DD_INVALID_MAP_BINARY_PATCH_ENABLED)}"
brt_dd_action_gate_enabled="${DUNE_BRT_DD_ACTION_GATE_BINARY_PATCH_ENABLED:-$(read_env DUNE_BRT_DD_ACTION_GATE_BINARY_PATCH_ENABLED)}"
brt_dd_buildable_region_enabled="${DUNE_BRT_DD_BUILDABLE_MAP_REGION_PATCH_ENABLED:-$(read_env DUNE_BRT_DD_BUILDABLE_MAP_REGION_PATCH_ENABLED)}"
brt_dd_narrow_tool_state_enabled="${DUNE_BRT_DD_NARROW_TOOL_STATE_BINARY_PATCH_ENABLED:-$(read_env DUNE_BRT_DD_NARROW_TOOL_STATE_BINARY_PATCH_ENABLED)}"
brt_dd_tool_enable_enabled="${DUNE_BRT_DD_TOOL_ENABLE_BINARY_PATCH_ENABLED:-$(read_env DUNE_BRT_DD_TOOL_ENABLE_BINARY_PATCH_ENABLED)}"
if [[ "$brt_dd_invalid_map_enabled" == "true" || "$brt_dd_action_gate_enabled" == "true" || "$brt_dd_buildable_region_enabled" == "true" || "$brt_dd_narrow_tool_state_enabled" == "true" || "$brt_dd_tool_enable_enabled" == "true" ]]; then
  if [[ "$brt_dd_tool_enable_enabled" == "true" ]] && file_exists compose.brt-dd-invalid-map.yaml; then
    add_file compose.brt-dd-invalid-map.yaml
  elif file_exists compose.brt-dd-tool-selected.yaml; then
    add_file compose.brt-dd-tool-selected.yaml
  elif file_exists compose.brt-dd-invalid-map.yaml; then
    add_file compose.brt-dd-invalid-map.yaml
  fi
fi

fls_ipv4_hosts_enabled="${DUNE_FLS_IPV4_HOSTS_ENABLED:-$(read_env DUNE_FLS_IPV4_HOSTS_ENABLED)}"
fls_ipv4_hosts_enabled="${fls_ipv4_hosts_enabled:-true}"
if [[ "$fls_ipv4_hosts_enabled" == "true" ]] && file_exists compose.fls-ipv4-hosts.yaml; then
  add_file compose.fls-ipv4-hosts.yaml
fi

director_hostnet_enabled="${DUNE_DIRECTOR_HOSTNET_ENABLED:-$(read_env DUNE_DIRECTOR_HOSTNET_ENABLED)}"
if [[ "$director_hostnet_enabled" == "true" ]]; then
  if file_exists compose.director-hostnet-cutover.yaml; then
    add_file compose.director-hostnet-cutover.yaml
  fi
  director_hostnet_port_file="${DUNE_DIRECTOR_HOSTNET_PORT_COMPOSE_FILE:-$(read_env DUNE_DIRECTOR_HOSTNET_PORT_COMPOSE_FILE)}"
  director_hostnet_port_file="${director_hostnet_port_file:-compose.director-hostnet-port.yaml}"
  if file_exists "$director_hostnet_port_file"; then
    add_file "$director_hostnet_port_file"
  fi
fi

host_limits_file="${DUNE_HOST_LIMITS_COMPOSE_FILE:-$(read_env DUNE_HOST_LIMITS_COMPOSE_FILE)}"
if [[ -n "$host_limits_file" ]] && file_exists "$host_limits_file"; then
  add_file "$host_limits_file"
fi

metrics_enabled="${DUNE_METRICS_ENABLED:-$(read_env DUNE_METRICS_ENABLED)}"
if [[ "$metrics_enabled" == "true" ]] && file_exists compose.metrics.yaml; then
  add_file compose.metrics.yaml
fi

cpu_affinity_enabled="${DUNE_CPU_AFFINITY_ENABLED:-$(read_env DUNE_CPU_AFFINITY_ENABLED)}"
cpu_affinity_file="${DUNE_CPU_AFFINITY_COMPOSE_FILE:-$(read_env DUNE_CPU_AFFINITY_COMPOSE_FILE)}"
cpu_affinity_file="${cpu_affinity_file:-compose.cpu-affinity.yaml}"
if [[ "$cpu_affinity_enabled" == "true" ]] && file_exists "$cpu_affinity_file"; then
  add_file "$cpu_affinity_file"
fi

(
  IFS=:
  printf '%s\n' "${resolved_files[*]}"
)
