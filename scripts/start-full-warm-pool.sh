#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/start-full-warm-pool.sh [ENV_FILE] [WAIT_SECONDS]

Starts every active map worker discovered from the merged Compose topology
without recreating already-running stateful services. Persisted
minimum/balanced/custom policies start only always-on maps; full-warm starts all
active maps. Profile-gated maps are opt-in through COMPOSE_PROFILES or their
explicit legacy partition-count label.

Default:
  scripts/start-full-warm-pool.sh .env 600
USAGE
}

if [[ $# -gt 2 ]]; then
  usage
  exit 2
fi

env_file="${1:-.env}"
wait_seconds="${2:-600}"
if [[ ! "$wait_seconds" =~ ^[0-9]+$ ]]; then
  printf 'wait seconds must be numeric: %s\n' "$wait_seconds" >&2
  exit 2
fi

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

batch_size="${DUNE_WARM_POOL_BATCH_SIZE:-0}"
batch_delay="${DUNE_WARM_POOL_BATCH_DELAY:-20}"
if [[ ! "$batch_size" =~ ^[0-9]+$ || ! "$batch_delay" =~ ^[0-9]+$ ]]; then
  printf 'DUNE_WARM_POOL_BATCH_SIZE and DUNE_WARM_POOL_BATCH_DELAY must be numeric\n' >&2
  exit 2
fi
autoscaler_enabled="${DUNE_AUTOSCALER_ENABLED:-$(read_env DUNE_AUTOSCALER_ENABLED)}"
autoscaler_profile="${DUNE_AUTOSCALER_PROFILE:-$(read_env DUNE_AUTOSCALER_PROFILE)}"
autoscaler_profile="${autoscaler_profile:-custom}"
autoscaler_default_mode="${DUNE_AUTOSCALER_DEFAULT_MODE:-$(read_env DUNE_AUTOSCALER_DEFAULT_MODE)}"
autoscaler_default_mode="${autoscaler_default_mode:-always-on}"
autoscaler_always_on="${DUNE_AUTOSCALER_ALWAYS_ON_SERVICES:-$(read_env DUNE_AUTOSCALER_ALWAYS_ON_SERVICES)}"
autoscaler_always_on="${autoscaler_always_on:-survival,overmap}"
autoscaler_simulation_required="${DUNE_AUTOSCALER_SIMULATION_REQUIRED_SERVICES:-$(read_env DUNE_AUTOSCALER_SIMULATION_REQUIRED_SERVICES)}"
autoscaler_simulation_required="${autoscaler_simulation_required:-survival}"
autoscaler_always_on="${autoscaler_always_on},${autoscaler_simulation_required}"
autoscaler_state_file="${DUNE_AUTOSCALER_STATE_FILE:-backups/admin-panel/autoscaler.json}"
if [[ "$autoscaler_state_file" != /* ]]; then
  autoscaler_state_file="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/$autoscaler_state_file"
fi
if [[ -f "$autoscaler_state_file" ]] && command -v python3 >/dev/null 2>&1; then
  mapfile -t autoscaler_state_values < <(python3 - "$autoscaler_state_file" <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)
print("true" if state.get("enabled") else "false")
print(str(state.get("profile") or "custom"))
print(",".join(sorted(service for service, mode in (state.get("modes") or {}).items() if mode == "always-on")))
print("true" if any(mode != "always-on" for mode in (state.get("modes") or {}).values()) else "false")
PY
  )
  if (( ${#autoscaler_state_values[@]} == 4 )); then
    autoscaler_enabled="${autoscaler_state_values[0]}"
    autoscaler_profile="${autoscaler_state_values[1]}"
    autoscaler_always_on="${autoscaler_state_values[2]}"
    autoscaler_state_selective="${autoscaler_state_values[3]}"
  fi
fi
selective_startup=false
if [[ "$autoscaler_enabled" =~ ^(1|true|yes|on)$ ]]; then
  case "$autoscaler_profile" in
    minimum-footprint|balanced|adaptive) selective_startup=true ;;
    full-warm) selective_startup=false ;;
    custom)
      if [[ "${autoscaler_state_selective:-}" == "true" || "$autoscaler_default_mode" == "dynamic" ]]; then
        selective_startup=true
      fi
      ;;
  esac
fi

map_is_always_on() {
  local service="$1"
  local candidate
  IFS=',' read -ra always_on_services <<< "$autoscaler_always_on"
  for candidate in "${always_on_services[@]}"; do
    candidate="${candidate//[[:space:]]/}"
    [[ "$candidate" == "$service" ]] && return 0
  done
  return 1
}

container_runtime="${CONTAINER_RUNTIME:-docker}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$script_dir/ensure-map-network-capacity.py" ]]; then
  python3 "$script_dir/ensure-map-network-capacity.py" "$env_file"
fi
if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi
compose=("$container_runtime" compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")
map_catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$map_catalog_script" ]]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$map_catalog_script" >&2
  exit 1
fi
mapfile -t MAP_PARTITIONS < <("$map_catalog_script" "$env_file" --field map-partitions --write-cache)
mapfile -t INACTIVE_MAP_PARTITIONS < <("$map_catalog_script" "$env_file" --field inactive-map-partitions)
map_profiles_text="$("$map_catalog_script" "$env_file" --field profiles)"
if [[ -n "$map_profiles_text" ]]; then
  read -r -a map_profiles <<< "$map_profiles_text"
  for profile in "${map_profiles[@]}"; do
    compose+=(--profile "$profile")
  done
fi
MAP_SERVICES=()
partition_ids_csv=""
for entry in "${MAP_PARTITIONS[@]}"; do
  service="${entry%%:*}"
  partition_id="${entry##*:}"
  MAP_SERVICES+=("$service")
  if [[ -n "$partition_ids_csv" ]]; then
    partition_ids_csv+=","
  fi
  partition_ids_csv+="$partition_id"
done
partition_count="${#MAP_SERVICES[@]}"
if (( partition_count == 0 )); then
  printf 'map catalog returned no workers; refusing to start an empty map farm\n' >&2
  exit 1
fi
db="${DUNE_GAME_DB_NAME:-$(read_env DUNE_GAME_DB_NAME)}"
db="${db:-dune_sb_1_4_0_0}"

psql_at() {
  "${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc "$1"
}

remove_db_init() {
  local container_id

  container_id="$("${compose[@]}" ps -aq db-init 2>/dev/null || true)"
  if [[ -n "$container_id" ]]; then
    printf 'removing stale one-shot db-init container\n'
    "$container_runtime" rm -f "$container_id" >/dev/null
  fi
}

wait_for_healthy() {
  local service="$1"
  local container_id
  local status

  container_id="$("${compose[@]}" ps -q "$service")"
  if [[ -z "$container_id" ]]; then
    printf 'service is not running: %s\n' "$service" >&2
    return 1
  fi

  for _ in {1..90}; do
    status="$("$container_runtime" inspect \
      --format '{{ if .State.Health }}{{ .State.Health.Status }}{{ else }}running{{ end }}' \
      "$container_id")"
    if [[ "$status" == "healthy" || "$status" == "running" ]]; then
      return 0
    fi
    sleep 2
  done

  printf 'service did not become healthy: %s\n' "$service" >&2
  return 1
}

wait_for_counts() {
  local expected="$1"
  local label="$2"
  local deadline=$((SECONDS + wait_seconds))
  local row
  local alive_active
  local active
  local partitions

  printf 'waiting for %s: expected=%s\n' "$label" "$expected"
  while (( SECONDS < deadline )); do
    row="$(psql_at "
      select
        count(*) filter (where fs.alive and asi.server_id is not null) || ' ' ||
        count(*) filter (where asi.server_id is not null) || ' ' ||
        count(*)
      from dune.world_partition wp
      left join dune.farm_state fs on fs.server_id = wp.server_id
      left join dune.active_server_ids asi on asi.server_id = wp.server_id
      where wp.partition_id in ($partition_ids_csv)
    ")"
    read -r alive_active active partitions <<< "$row"
    if (( alive_active >= expected && active >= expected )) && [[ "$partitions" == "$partition_count" ]]; then
      printf 'ready enough: alive_active=%s active=%s partitions=%s\n' "$alive_active" "$active" "$partitions"
      return 0
    fi
    printf 'still starting: alive_active=%s active=%s partitions=%s\n' "${alive_active:-?}" "${active:-?}" "${partitions:-?}"
    sleep 10
  done

  printf '%s did not reach alive-active/active count %s with %s partitions within %s seconds\n' "$label" "$expected" "$partition_count" "$wait_seconds" >&2
  return 1
}

start_services() {
  local services=("$@")
  local batch=()
  local service

  if (( batch_size <= 0 )); then
    "${compose[@]}" up -d --no-recreate "${services[@]}"
    return
  fi

  for service in "${services[@]}"; do
    batch+=("$service")
    if (( ${#batch[@]} >= batch_size )); then
      "${compose[@]}" up -d --no-recreate "${batch[@]}"
      batch=()
      sleep "$batch_delay"
    fi
  done

  if (( ${#batch[@]} > 0 )); then
    "${compose[@]}" up -d --no-recreate "${batch[@]}"
  fi
}

start_map_group() {
  local selected=()
  local service

  for service in "$@"; do
    if [[ "$selective_startup" == "false" ]] || map_is_always_on "$service"; then
      selected+=("$service")
    fi
  done
  if (( ${#selected[@]} > 0 )); then
    start_services "${selected[@]}"
  fi
}

remove_db_init

printf 'starting stateful dependencies without recreating existing containers\n'
"${compose[@]}" up -d --no-recreate postgres admin-rmq game-rmq
wait_for_healthy postgres
wait_for_healthy admin-rmq
wait_for_healthy game-rmq

printf 'applying official DB patches before map startup\n'
"$script_dir/apply-official-db-patches.sh" "$env_file"

printf 'clearing stale player RabbitMQ sessions before map startup\n'
"$script_dir/clear-player-rmq-sessions.sh" "$env_file"

printf 'refreshing host LAN/firewall reflection for the current docker bridge\n'
"$script_dir/setup-lan-reflection.sh" "$env_file"

printf 'ensuring %s world partitions exist\n' "$partition_count"
COMPOSE_FILES="$COMPOSE_FILES" CONTAINER_RUNTIME="$container_runtime" \
  "$script_dir/full-world-partitions.sh" "$env_file"

printf 'starting service layer without recreating existing containers\n'
"${compose[@]}" up -d --no-recreate \
  rmq-auth-shim text-router gateway director admin-panel admin-panel-ingress admin-chat-commands
if [[ "${DUNE_METRICS_ENABLED:-$(read_env DUNE_METRICS_ENABLED)}" =~ ^(1|true|yes|on)$ ]]; then
  printf 'starting retained metrics services\n'
  "${compose[@]}" up -d --no-recreate prometheus node-exporter cadvisor postgres-exporter
fi
"$script_dir/seed-gateway-neighbor.sh"

printf 'starting %s active map workers from the Compose catalog\n' "$partition_count"
start_map_group "${MAP_SERVICES[@]}"

for entry in "${INACTIVE_MAP_PARTITIONS[@]}"; do
  service="${entry%%:*}"
  profiles_csv="${entry#*:}"
  stop_compose=("${compose[@]}")
  IFS=',' read -ra disabled_profiles <<< "$profiles_csv"
  for profile in "${disabled_profiles[@]}"; do
    [[ -n "$profile" ]] && stop_compose+=(--profile "$profile")
  done
  printf 'stopping inactive profile-gated map worker: %s\n' "$service"
  "${stop_compose[@]}" stop "$service" >/dev/null 2>&1 || true
done
remove_db_init

if [[ "$selective_startup" == "true" ]]; then
  always_on_count=0
  for service in "${MAP_SERVICES[@]}"; do
    map_is_always_on "$service" && always_on_count=$((always_on_count + 1))
  done
  printf 'autoscaler startup active: profile=%s always_on=%s dynamic_maps=%s\n' "$autoscaler_profile" "$autoscaler_always_on" "$((partition_count - always_on_count))"
  wait_for_counts "$always_on_count" 'always-on maps'
else
  wait_for_counts "$partition_count" 'full map farm'
fi

if grep -Eq '^DUNE_SIETCH_MUTATIONS_ENABLED=(1|true|yes|on)$' "$env_file"; then
  printf 'reconciling configured additional Survival_1 Sietches\n'
  "$script_dir/sietches.sh" "$env_file" reconcile --execute
fi

printf 'running guarded post-start health and runtime hooks\n'
COMPOSE_FILES="$COMPOSE_FILES" ENV_FILE="$env_file" \
  "$script_dir/restart-post-start-health.sh"

printf 'final status\n'
COMPOSE_FILES="$COMPOSE_FILES" CONTAINER_RUNTIME="$container_runtime" \
  "$script_dir/status.sh" "$env_file"
