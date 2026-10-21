#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/watch-maps.sh ENV_FILE [--once|--status|--dry-run]

Watches fixed-partition map containers and recovers crashed/exited services or
degraded partition registration with scripts/recover-map.sh. It does not start
services that were never launched.

Modes:
  --once      Run one recovery pass, quiet unless recovery is needed.
  --status    Print monitored service/container status and exit.
  --dry-run   Print what would be recovered and exit without changing state.

Environment:
  COMPOSE_FILES              Compose files, colon-separated. Defaults are derived from ENV_FILE and host role.
  CONTAINER_RUNTIME          Container runtime. Default: docker
  DUNE_WATCH_INTERVAL        Seconds between checks. Default: 30
  DUNE_WATCH_RECOVERY_WAIT   Seconds recover-map waits per recovery. Default: 180
  DUNE_WATCH_RECOVER_TIMEOUT Max seconds for recover-map command. Default: wait+180; 0 disables.
  DUNE_WATCH_COOLDOWN        Minimum seconds between recoveries per service. Default: 300
  DUNE_WATCH_STARTUP_GRACE   Seconds to let running maps warm before DB recovery. Default: 300
  DUNE_WATCH_LOCK_DIR        Single-instance lock dir. Default: /tmp/dune-map-watchdog.lock
  DUNE_WATCH_PAUSE_FILE      Pause marker honored in loop mode. Default: /tmp/dune-map-watchdog.paused
  DUNE_WATCH_REQUIRE_READY    Recover running maps with ready=false. Default: false
  DUNE_WATCH_RECOVER_COMMAND Recovery command. Default: scripts/recover-map.sh
  DUNE_WATCH_COMMAND_TIMEOUT Max seconds for Docker/DB probe commands. Default: 20; 0 disables.
  DUNE_WATCH_SEED_NEIGHBORS Seed known Docker bridge neighbor entries. Default: false
  DUNE_WATCH_SEED_COMMAND   Neighbor seed command. Default: scripts/seed-gateway-neighbor.sh
  DUNE_WATCH_SEED_INTERVAL  Seconds between neighbor seeding attempts. Default: 300
  DUNE_WATCH_SEED_TIMEOUT   Max seconds for neighbor seeding. Default: 90
  DUNE_WATCH_CONTROL_PLANE_ENABLED  Probe map-to-Postgres connectivity. Default: true
  DUNE_WATCH_CONTROL_PLANE_INTERVAL Seconds between control-plane probes. Default: 30
  DUNE_WATCH_CONTROL_PLANE_REPAIR_ENABLED Repair bridge/firewall state on probe failure. Default: true
  DUNE_WATCH_CONTROL_PLANE_REPAIR_INTERVAL Minimum seconds between repairs. Default: 300
  DUNE_WATCH_CONTROL_PLANE_SCRIPT  Probe script. Default: scripts/control-plane-health.sh
  DUNE_WATCH_DEGRADED_CONFIRMATIONS Consecutive degraded checks before recovery. Default: 2
  DUNE_WATCH_REQUIRED_READY_PARTITIONS Comma-separated partitions requiring ready=true.
                                  Default: DUNE_CORE_PARTITION_IDS or 1,2
  DUNE_WORLD_PARTITION_COUNT Legacy opt-in for profile-gated maps that declare
                             dune.map.enabled-by-partition-count. Ordinary map
                             services are discovered directly from Compose.
USAGE
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

env_file="$1"
mode="${2:-}"
if [[ -n "$mode" && "$mode" != "--once" && "$mode" != "--status" && "$mode" != "--dry-run" ]]; then
  usage
  exit 2
fi

container_runtime="${CONTAINER_RUNTIME:-docker}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi
compose=("$container_runtime" compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")

interval="${DUNE_WATCH_INTERVAL:-30}"
recovery_wait="${DUNE_WATCH_RECOVERY_WAIT:-180}"
recovery_timeout="${DUNE_WATCH_RECOVER_TIMEOUT:-}"
cooldown="${DUNE_WATCH_COOLDOWN:-300}"
startup_grace="${DUNE_WATCH_STARTUP_GRACE:-300}"
lock_dir="${DUNE_WATCH_LOCK_DIR:-/tmp/dune-map-watchdog.lock}"
pause_file="${DUNE_WATCH_PAUSE_FILE:-/tmp/dune-map-watchdog.paused}"
autoscaler_state_file="${DUNE_AUTOSCALER_STATE_FILE:-backups/admin-panel/autoscaler.json}"
require_ready="${DUNE_WATCH_REQUIRE_READY:-false}"
recover_command="${DUNE_WATCH_RECOVER_COMMAND:-$script_dir/recover-map.sh}"
command_timeout="${DUNE_WATCH_COMMAND_TIMEOUT:-20}"
seed_neighbors="${DUNE_WATCH_SEED_NEIGHBORS:-false}"
seed_command="${DUNE_WATCH_SEED_COMMAND:-$script_dir/seed-gateway-neighbor.sh}"
seed_interval="${DUNE_WATCH_SEED_INTERVAL:-300}"
seed_timeout="${DUNE_WATCH_SEED_TIMEOUT:-90}"
control_plane_enabled="${DUNE_WATCH_CONTROL_PLANE_ENABLED:-true}"
control_plane_interval="${DUNE_WATCH_CONTROL_PLANE_INTERVAL:-30}"
control_plane_repair_enabled="${DUNE_WATCH_CONTROL_PLANE_REPAIR_ENABLED:-true}"
control_plane_repair_interval="${DUNE_WATCH_CONTROL_PLANE_REPAIR_INTERVAL:-300}"
control_plane_script="${DUNE_WATCH_CONTROL_PLANE_SCRIPT:-$script_dir/control-plane-health.sh}"
degraded_confirmations="${DUNE_WATCH_DEGRADED_CONFIRMATIONS:-2}"
required_ready_partitions="${DUNE_WATCH_REQUIRED_READY_PARTITIONS:-${DUNE_CORE_PARTITION_IDS:-}}"
required_ready_partitions="${required_ready_partitions:-1,2}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

db="${DUNE_GAME_DB_NAME:-$(read_env DUNE_GAME_DB_NAME)}"
db="${db:-${DUNE_DATABASE:-$(read_env DUNE_DATABASE)}}"
db="${db:-${DUNE_DB_NAME:-$(read_env DUNE_DB_NAME)}}"
db="${db:-dune_sb_1_4_0_0}"

# Pass only the non-secret settings map discovery needs. Docker Compose reads
# the runtime dotenv file only for actual service operations below.
map_partition_count="${DUNE_WORLD_PARTITION_COUNT:-$(read_env DUNE_WORLD_PARTITION_COUNT)}"
export DUNE_WORLD_PARTITION_COUNT="${map_partition_count:-30}"
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-$(read_env COMPOSE_PROFILES)}"

map_catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$map_catalog_script" ]]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$map_catalog_script" >&2
  exit 2
fi
mapfile -t MAP_PARTITIONS < <("$map_catalog_script" "$env_file" --no-env-file-read --field map-partitions --write-cache)
map_profiles_text="$("$map_catalog_script" "$env_file" --no-env-file-read --field profiles)"
if [[ -n "$map_profiles_text" ]]; then
  read -r -a map_profiles <<< "$map_profiles_text"
  for profile in "${map_profiles[@]}"; do
    compose+=(--profile "$profile")
  done
fi
if (( ${#MAP_PARTITIONS[@]} == 0 )); then
  printf 'map catalog returned no map workers; refusing to monitor an empty roster\n' >&2
  exit 2
fi

if [[ ! "$interval" =~ ^[0-9]+$ || ! "$recovery_wait" =~ ^[0-9]+$ || ! "$cooldown" =~ ^[0-9]+$ || ! "$startup_grace" =~ ^[0-9]+$ || ! "$command_timeout" =~ ^[0-9]+$ || ! "$seed_interval" =~ ^[0-9]+$ || ! "$seed_timeout" =~ ^[0-9]+$ || ! "$control_plane_interval" =~ ^[0-9]+$ || ! "$control_plane_repair_interval" =~ ^[0-9]+$ || ! "$degraded_confirmations" =~ ^[1-9][0-9]*$ ]]; then
  printf 'DUNE_WATCH_INTERVAL, DUNE_WATCH_RECOVERY_WAIT, DUNE_WATCH_COOLDOWN, DUNE_WATCH_STARTUP_GRACE, DUNE_WATCH_COMMAND_TIMEOUT, DUNE_WATCH_SEED_INTERVAL, DUNE_WATCH_SEED_TIMEOUT, DUNE_WATCH_CONTROL_PLANE_INTERVAL, DUNE_WATCH_CONTROL_PLANE_REPAIR_INTERVAL, and DUNE_WATCH_DEGRADED_CONFIRMATIONS must be numeric\n' >&2
  exit 2
fi
if [[ -z "$recovery_timeout" ]]; then
  recovery_timeout=$((recovery_wait + 180))
fi
if [[ ! "$recovery_timeout" =~ ^[0-9]+$ ]]; then
  printf 'DUNE_WATCH_RECOVER_TIMEOUT must be numeric\n' >&2
  exit 2
fi

if [[ -z "$mode" ]]; then
  if command -v flock >/dev/null 2>&1; then
    lock_file="${lock_dir%/}.flock"
    exec 9>"$lock_file"
    if ! flock -n 9; then
      printf 'another map watchdog appears to be running: %s\n' "$lock_file" >&2
      exit 1
    fi
    # Remove the legacy mkdir lock. Kernel-managed flock state cannot remain
    # orphaned when systemd or the OOM killer terminates the watchdog.
    rmdir "$lock_dir" 2>/dev/null || true
  else
    if ! mkdir "$lock_dir" 2>/dev/null; then
      printf 'another map watchdog appears to be running: %s\n' "$lock_dir" >&2
      exit 1
    fi
    trap 'rmdir "$lock_dir"' EXIT
  fi
fi

declare -A LAST_RECOVERY=()
declare -A DEGRADED_COUNTS=()
last_seed=0
last_control_plane_check=0
last_control_plane_repair=0
control_plane_blocked=false

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

run_bounded() {
  if (( command_timeout > 0 )) && command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5s "${command_timeout}s" "$@"
  else
    "$@"
  fi
}

run_recovery_command() {
  if (( recovery_timeout > 0 )) && command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=10s "${recovery_timeout}s" "$@"
  else
    "$@"
  fi
}

control_plane_preflight() {
  local now rc repair_rc

  case "$control_plane_enabled" in
    1|true|yes|on|TRUE|True|YES|ON) ;;
    *) return 0 ;;
  esac
  case "$mode" in
    --status|--dry-run) return 0 ;;
  esac
  [[ -x "$control_plane_script" ]] || return 0

  now="$(date +%s)"
  if (( last_control_plane_check > 0 && now - last_control_plane_check < control_plane_interval )); then
    [[ "$control_plane_blocked" != true ]]
    return
  fi
  last_control_plane_check="$now"

  set +e
  "$control_plane_script" "$env_file" --allow-no-map --quiet
  rc=$?
  set -e
  if (( rc == 0 )); then
    if [[ "$control_plane_blocked" == true ]]; then
      log 'control-plane preflight recovered; resuming map health checks'
    fi
    control_plane_blocked=false
    return 0
  fi

  if [[ "$control_plane_repair_enabled" =~ ^(1|true|yes|on|TRUE|True|YES|ON)$ ]] \
      && (( last_control_plane_repair == 0 || now - last_control_plane_repair >= control_plane_repair_interval )); then
    last_control_plane_repair="$now"
    log 'control-plane preflight failed; attempting network repair'
    set +e
    "$control_plane_script" "$env_file" --allow-no-map --repair --quiet
    repair_rc=$?
    set -e
    if (( repair_rc == 0 )); then
      control_plane_blocked=false
      log 'control-plane network repair succeeded; resuming map health checks'
      return 0
    fi
  fi

  if [[ "$control_plane_blocked" != true ]]; then
    log 'control-plane remains degraded; pausing map restarts until the dependency path recovers'
  fi
  control_plane_blocked=true
  return 1
}

partition_requires_ready() {
  local partition_id="$1"
  local ready_partition
  if [[ "$require_ready" =~ ^(1|true|yes|on|TRUE|True|YES|ON)$ ]]; then
    return 0
  fi
  for ready_partition in $(printf '%s' "$required_ready_partitions" | tr ',' ' '); do
    [[ "$ready_partition" == "$partition_id" ]] && return 0
  done
  return 1
}

container_status() {
  local service="$1"
  local container_id
  local status

  if ! container_id="$(run_bounded "${compose[@]}" ps -aq "$service" 2>/dev/null)"; then
    printf 'unknown'
    return
  fi
  if [[ -z "$container_id" ]]; then
    printf 'missing'
    return
  fi

  if ! status="$(run_bounded "$container_runtime" inspect --format '{{ .State.Status }}' "$container_id" 2>/dev/null)"; then
    printf 'unknown'
    return
  fi
  printf '%s' "${status:-unknown}"
}

container_age_seconds() {
  local service="$1"
  local container_id
  local started_at
  local started_epoch
  local now

  container_id="$(run_bounded "${compose[@]}" ps -aq "$service" 2>/dev/null || true)"
  if [[ -z "$container_id" ]]; then
    printf '0'
    return
  fi

  started_at="$(run_bounded "$container_runtime" inspect --format '{{ .State.StartedAt }}' "$container_id" 2>/dev/null || true)"
  if [[ -z "$started_at" || "$started_at" == "0001-01-01T00:00:00Z" ]]; then
    printf '0'
    return
  fi

  started_epoch="$(date -d "$started_at" +%s 2>/dev/null || printf '0')"
  now="$(date +%s)"
  if (( started_epoch <= 0 || now < started_epoch )); then
    printf '0'
    return
  fi

  printf '%s' "$((now - started_epoch))"
}

partition_health() {
  local partition_id="$1"

  run_bounded "${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc "
    select
      coalesce(fs.ready, false) || ' ' ||
      coalesce(fs.alive, false) || ' ' ||
      (asi.server_id is not null)
    from dune.world_partition wp
    left join dune.farm_state fs on fs.server_id = wp.server_id
    left join dune.active_server_ids asi on asi.server_id = wp.server_id
    where wp.partition_id = ${partition_id};
  " 2>/dev/null || printf 'unknown unknown unknown'
}

partition_degraded_reason() {
  local partition_id="$1"
  local ready
  local alive
  local active

  read -r ready alive active <<< "$(partition_health "$partition_id")"

  if [[ "$alive" == "unknown" || "$active" == "unknown" ]]; then
    printf 'health_unknown'
    return
  fi

  if [[ "$alive" != "t" && "$alive" != "true" ]]; then
    printf 'not_alive'
    return
  fi

  if [[ "$active" != "t" && "$active" != "true" ]]; then
    printf 'not_active'
    return
  fi

  if partition_requires_ready "$partition_id" && [[ "$ready" != "t" && "$ready" != "true" ]]; then
    printf 'not_ready'
    return
  fi

  printf 'ok'
}

recover_service() {
  local service="$1"
  local partition_id="$2"
  local reason="${3:-crashed}"
  local now
  local last

  now="$(date +%s)"
  last="${LAST_RECOVERY[$service]:-0}"
  if (( now - last < cooldown )); then
    log "skip recovery during cooldown: service=$service partition=$partition_id"
    return
  fi

  if [[ "$mode" == "--dry-run" || "$mode" == "--once" ]]; then
    DEGRADED_COUNTS[$service]=0
  else
    case "$reason" in
    exited|dead|crashed)
      DEGRADED_COUNTS[$service]=0
      ;;
    *)
      degraded_count="${DEGRADED_COUNTS[$service]:-0}"
      degraded_count=$((degraded_count + 1))
      DEGRADED_COUNTS[$service]="$degraded_count"
      if (( degraded_count < degraded_confirmations )); then
        log "waiting for degraded confirmation: service=$service partition=$partition_id reason=$reason count=$degraded_count/$degraded_confirmations"
        return
      fi
      DEGRADED_COUNTS[$service]=0
      ;;
    esac
  fi

  LAST_RECOVERY[$service]="$now"
  if [[ "$mode" == "--dry-run" ]]; then
    log "would recover map: service=$service partition=$partition_id reason=$reason"
    return
  fi

  log "recovering map: service=$service partition=$partition_id reason=$reason"
  if ! COMPOSE_FILES="$COMPOSE_FILES" CONTAINER_RUNTIME="$container_runtime" \
    run_recovery_command "$recover_command" "$env_file" "$service" "$partition_id" "$recovery_wait"; then
    log "map recovery failed: service=$service partition=$partition_id reason=$reason"
    if [[ "$mode" == "--once" ]]; then
      return 1
    fi
  fi
}

seed_network_neighbors() {
  if [[ "$seed_neighbors" != "true" ]]; then
    return
  fi

  if [[ ! -x "$seed_command" ]]; then
    log "skip neighbor seeding; command is not executable: $seed_command"
    return
  fi

  if (( seed_timeout > 0 )) && command -v timeout >/dev/null 2>&1; then
    set +e
    timeout --kill-after=5s "${seed_timeout}s" env CONTAINER_RUNTIME="$container_runtime" "$seed_command" >/dev/null
    rc=$?
    set -e
    if (( rc != 0 )); then
      log "neighbor seeding failed or timed out: command=$seed_command rc=$rc timeout=${seed_timeout}s"
    fi
    return
  fi

  if ! CONTAINER_RUNTIME="$container_runtime" "$seed_command" >/dev/null; then
    log "neighbor seeding failed: command=$seed_command"
  fi
}

maybe_seed_network_neighbors() {
  local now

  if [[ "$mode" == "--status" || "$mode" == "--dry-run" ]]; then
    return
  fi

  if [[ "$mode" == "--once" ]]; then
    seed_network_neighbors
    return
  fi

  now="$(date +%s)"
  if (( last_seed > 0 && seed_interval > 0 && now - last_seed < seed_interval )); then
    return
  fi
  last_seed="$now"
  seed_network_neighbors
}

wait_if_paused() {
  if [[ -n "$mode" ]]; then
    return
  fi

  while [[ -e "$pause_file" ]]; do
    log "map watchdog paused: pause_file=$pause_file"
    sleep "$interval"
  done
}

autoscaler_manages_stopped_service() {
  local service="$1"
  [[ -f "$autoscaler_state_file" ]] || return 1
  if command -v jq >/dev/null 2>&1; then
    jq -e --arg service "$service" '
      .enabled == true and ((.modes[$service] // "always-on") == "dynamic" or (.modes[$service] // "always-on") == "disabled")
    ' "$autoscaler_state_file" >/dev/null 2>&1
    return
  fi
  return 1
}

check_once() {
  if ! control_plane_preflight; then
    return 0
  fi

  local item
  local service
  local partition_id
  local status
  local health
  local reason

  for item in "${MAP_PARTITIONS[@]}"; do
    service="${item%%:*}"
    partition_id="${item##*:}"
    status="$(container_status "$service")"

    if [[ "$mode" == "--status" ]]; then
      health="$(partition_health "$partition_id")"
      printf '%-32s partition=%-2s status=%-10s db=\"%s\"\n' "$service" "$partition_id" "$status" "$health"
      continue
    fi

    case "$status" in
      running)
        if (( $(container_age_seconds "$service") < startup_grace )); then
          log "skip DB recovery during startup grace: service=$service partition=$partition_id"
          continue
        fi
        reason="$(partition_degraded_reason "$partition_id")"
        case "$reason" in
          ok)
            ;;
          health_unknown)
            log "skip recovery because partition health is unavailable: service=$service partition=$partition_id"
            ;;
          *)
            recover_service "$service" "$partition_id" "$reason"
            ;;
        esac
        ;;
      created|restarting|removing|paused)
        ;;
      unknown)
        log "skip recovery because container status is unavailable: service=$service partition=$partition_id"
        ;;
      exited|dead)
        if autoscaler_manages_stopped_service "$service"; then
          log "skip recovery for autoscaler-managed stopped map: service=$service partition=$partition_id mode=$status"
        else
          recover_service "$service" "$partition_id" "$status"
        fi
        ;;
      missing)
        ;;
      *)
        log "unknown container status: service=$service status=$status"
        ;;
    esac
  done
}

while true; do
  wait_if_paused
  check_once
  maybe_seed_network_neighbors
  if [[ "$mode" == "--once" || "$mode" == "--status" || "$mode" == "--dry-run" ]]; then
    exit 0
  fi
  sleep "$interval"
done
