#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/reconcile-map-patch-overlays.sh [ENV_FILE] [--execute]

Detect map containers that were created before the all-map patch overlays were
enabled and optionally recreate zero-player stale maps through
scripts/start-map-with-post-hooks.sh.

The script is read-only unless --execute is passed.

Environment:
  DUNE_RECONCILE_EXCLUDE_SERVICES  Comma-separated map services to skip.
  DUNE_RECONCILE_FAIL_ON_DRIFT     In dry-run mode, exit 1 if stale/blocked
                                   services are found. Default: false.
USAGE
}

env_file="${1:-${ENV_FILE:-.env}}"
execute=false
if [[ "${env_file:-}" == "--execute" ]]; then
  env_file="${ENV_FILE:-.env}"
  execute=true
fi
if [[ "${2:-}" == "--execute" ]]; then
  execute=true
elif [[ -n "${2:-}" ]]; then
  usage
  exit 2
fi
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 2
fi

required_host="${DUNE_RECONCILE_REQUIRED_HOST:-kspls0}"
allow_any_host="${DUNE_RECONCILE_ALLOW_ANY_HOST:-0}"
host_short="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
if [[ "$allow_any_host" != "1" && -n "$required_host" && "$host_short" != "$required_host" ]]; then
  printf 'refusing reconcile on %s; expected %s\n' "$host_short" "$required_host" >&2
  exit 1
fi

container_runtime="${CONTAINER_RUNTIME:-docker}"
exclude_services="${DUNE_RECONCILE_EXCLUDE_SERVICES:-}"
fail_on_drift="${DUNE_RECONCILE_FAIL_ON_DRIFT:-false}"
compose_files="$("$script_dir/compose-files.sh" "$env_file")"
export COMPOSE_FILES="$compose_files"
compose=("$container_runtime" compose)
IFS=':' read -ra compose_file_array <<< "$compose_files"
for compose_file in "${compose_file_array[@]}"; do
  [[ -n "$compose_file" ]] && compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")
map_catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$map_catalog_script" ]]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$map_catalog_script" >&2
  exit 1
fi
map_partitions_output="$("$map_catalog_script" "$env_file" --field all-map-partitions --write-cache)"
if [[ -z "$map_partitions_output" ]]; then
  printf 'map catalog returned no workers; refusing overlay reconciliation\n' >&2
  exit 1
fi
mapfile -t MAP_PARTITIONS <<< "$map_partitions_output"
all_map_profiles="$("$map_catalog_script" "$env_file" --field all-profiles)"
for profile in $all_map_profiles; do
  compose+=(--profile "$profile")
done

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "")
      gsub(/^["'\''"]|["'\''"]$/, "")
      print
      exit
    }
  ' "$env_file" 2>/dev/null || true
}

db="${DUNE_GAME_DB_NAME:-$(env_value DUNE_GAME_DB_NAME)}"
db="${db:-${DUNE_DATABASE:-$(env_value DUNE_DATABASE)}}"
db="${db:-${DUNE_DB_NAME:-$(env_value DUNE_DB_NAME)}}"
db="${db:-dune_sb_1_4_0_0}"

psql_at() {
  "${compose[@]}" exec -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 -qAtc "$1"
}

building_piece_mode="${DUNE_BUILDING_PIECE_LIMIT_MODE:-$(env_value DUNE_BUILDING_PIECE_LIMIT_MODE)}"
building_piece_mode="${building_piece_mode:-server-settings}"
building_settings_enabled="${DUNE_BUILDING_PIECE_LIMIT_SETTINGS_ENABLED:-$(env_value DUNE_BUILDING_PIECE_LIMIT_SETTINGS_ENABLED)}"
building_settings_enabled="${building_settings_enabled:-false}"
subfief_enabled="${DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED:-$(env_value DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED)}"
subfief_enabled="${subfief_enabled:-false}"

declare -A EXCLUDED_SERVICES=()
if [[ -n "$exclude_services" ]]; then
  IFS=',' read -ra excluded_service_items <<< "$exclude_services"
  for service in "${excluded_service_items[@]}"; do
    service="${service//[[:space:]]/}"
    [[ -n "$service" ]] || continue
    EXCLUDED_SERVICES["$service"]=1
  done
fi

container_id() {
  "${compose[@]}" ps -q "$1" 2>/dev/null || true
}

container_env_has() {
  local cid="$1" expected="$2"
  "$container_runtime" inspect "$cid" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep -Fxq "$expected"
}

container_mount_has() {
  local cid="$1" destination="$2"
  "$container_runtime" inspect "$cid" --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
    | grep -Fxq "$destination"
}

connected_players() {
  local partition_id="$1"
  psql_at "
    select coalesce(fs.connected_players, 0)
    from dune.world_partition wp
    left join dune.farm_state fs on fs.server_id = wp.server_id
    where wp.partition_id = ${partition_id};
  "
}

printf 'host=%s mode=%s compose_files=%s\n' "$host_short" "$([[ "$execute" == true ]] && printf execute || printf dry-run)" "$compose_files"
if [[ -n "$exclude_services" ]]; then
  printf 'excluded_services=%s\n' "$exclude_services"
fi
printf '%-28s %9s %7s %-10s %s\n' service partition players state reason

recreate_services=()
blocked_services=()
excluded_count=0

for item in "${MAP_PARTITIONS[@]}"; do
  service="${item%%:*}"
  partition_id="${item##*:}"
  if [[ -n "${EXCLUDED_SERVICES[$service]:-}" ]]; then
    excluded_count=$((excluded_count + 1))
    printf '%-28s %9s %7s %-10s %s\n' "$service" "$partition_id" "-" "excluded" "-"
    continue
  fi
  cid="$(container_id "$service")"
  players="$(connected_players "$partition_id")"
  players="${players:-0}"
  reason=()
  if [[ -z "$cid" ]]; then
    reason+=("missing-container")
  else
    if [[ "$building_piece_mode" == "server-settings" && "$building_settings_enabled" == "true" ]]; then
      container_env_has "$cid" "DUNE_BUILDING_PIECE_LIMIT_SETTINGS_ENABLED=true" || reason+=("missing-building-settings-env")
    elif [[ "$building_piece_mode" == "pak" ]]; then
      container_env_has "$cid" "DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED=true" || reason+=("missing-building-pak-env")
      container_mount_has "$cid" "/tmp/oodle/liboodle-data-shared.so" || reason+=("missing-oodle-mount")
    else
      reason+=("invalid-building-mode")
    fi
    if [[ "$subfief_enabled" == "true" ]]; then
      container_env_has "$cid" "DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED=true" || reason+=("missing-subfief-env")
      container_env_has "$cid" "DUNE_SUBFIEF_CAP_BINARY_TARGET=subfief" || reason+=("missing-subfief-target")
    fi
  fi

  if ((${#reason[@]} == 0)); then
    state=ok
    reason_text="-"
  elif [[ "$players" =~ ^[0-9]+$ && "$players" -gt 0 ]]; then
    state=blocked
    reason_text="${reason[*]}"
    blocked_services+=("$service")
  else
    state=stale
    reason_text="${reason[*]}"
    recreate_services+=("$service")
  fi
  printf '%-28s %9s %7s %-10s %s\n' "$service" "$partition_id" "$players" "$state" "$reason_text"
done

printf '\nsummary: stale_zero_player=%s blocked_active=%s excluded=%s\n' "${#recreate_services[@]}" "${#blocked_services[@]}" "$excluded_count"

if [[ "$execute" != true ]]; then
  if [[ "$fail_on_drift" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])$ ]] &&
      (( ${#recreate_services[@]} > 0 || ${#blocked_services[@]} > 0 )); then
    printf 'drift detected in dry-run mode\n' >&2
    exit 1
  fi
  exit 0
fi

if ((${#recreate_services[@]} > 0)); then
  printf 'recreating zero-player stale services: %s\n' "${recreate_services[*]}"
  "$script_dir/start-map-with-post-hooks.sh" "$env_file" "${recreate_services[@]}"
fi

if ((${#blocked_services[@]} > 0)); then
  printf 'blocked active stale services: %s\n' "${blocked_services[*]}" >&2
  exit 1
fi

printf 'reconcile complete\n'
