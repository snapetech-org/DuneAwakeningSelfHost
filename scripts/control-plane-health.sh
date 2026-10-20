#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/control-plane-health.sh ENV_FILE [OPTIONS]

Checks the control-plane path from running game-map containers to Postgres.
A healthy Postgres container does not prove that game processes can reach it
through the Docker bridge and host forwarding path.

Options:
  --repair        Reapply host LAN/bridge rules and seed container neighbors,
                  then run the probes again.
  --full          Also verify the RabbitMQ auth/text-router path.
  --allow-no-map  Return success when no configured probe map is running.
  --quiet         Suppress successful probe output.

Environment:
  DUNE_CONTROL_PLANE_PROBE_SERVICES  Comma-separated map services. Default:
                                     survival,deep-desert,deep-desert-pvp.
  DUNE_CONTROL_PLANE_DB_HOST         Default: postgres.
  DUNE_CONTROL_PLANE_DB_PORT         Default: 5432.
  DUNE_CONTROL_PLANE_DB_USER         Default: dune.
  DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS  Default: 90.
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

env_file="$1"
shift
repair=false
full=false
allow_no_map=false
quiet=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repair) repair=true ;;
    --full) full=true ;;
    --allow-no-map) allow_no_map=true ;;
    --quiet) quiet=true ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage; exit 2 ;;
  esac
  shift
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime="$(printenv CONTAINER_RUNTIME 2>/dev/null || true)"
[[ -n "$runtime" ]] || runtime=docker

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

if [[ -z "${COMPOSE_FILES:-}" && -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  printf '%s' "$value" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

env_or_file() {
  local key="$1" value
  value="$(printenv "$key" 2>/dev/null || true)"
  [[ -n "$value" ]] || value="$(read_env "$key")"
  printf '%s' "$value"
}

log() {
  [[ "$quiet" == true ]] || printf '%s\n' "$*"
}

repair_timeout="$(env_or_file DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS)"
[[ -n "$repair_timeout" ]] || repair_timeout=90
if [[ ! "$repair_timeout" =~ ^[0-9]+$ ]]; then
  printf 'DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS must be numeric\n' >&2
  exit 2
fi

run_bounded() {
  local timeout_seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5s "${timeout_seconds}s" "$@"
  else
    "$@"
  fi
}

db_host="$(env_or_file DUNE_CONTROL_PLANE_DB_HOST)"
[[ -n "$db_host" ]] || db_host=postgres
db_port="$(env_or_file DUNE_CONTROL_PLANE_DB_PORT)"
[[ -n "$db_port" ]] || db_port=5432
db_user="$(env_or_file DUNE_CONTROL_PLANE_DB_USER)"
[[ -n "$db_user" ]] || db_user=dune
probe_services="$(env_or_file DUNE_CONTROL_PLANE_PROBE_SERVICES)"
[[ -n "$probe_services" ]] || probe_services=survival,deep-desert,deep-desert-pvp

compose_ps_q() {
  "$runtime" ps -q \
    --filter status=running \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME:-dune_server}" \
    --filter "label=com.docker.compose.service=$1" 2>/dev/null | head -1
}

container_running() {
  local container_id="$1"
  [[ "$("$runtime" inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null || true)" == true ]]
}

probe_container_postgres() {
  local container_id="$1"
  "$runtime" exec "$container_id" sh -lc '
    if command -v pg_isready >/dev/null 2>&1; then
      pg_isready -h "$1" -p "$2" -U "$3" >/dev/null 2>&1
    elif command -v nc >/dev/null 2>&1; then
      nc -z -w 3 "$1" "$2" >/dev/null 2>&1
    else
      exit 127
    fi
  ' sh "$db_host" "$db_port" "$db_user"
}

probe_map_services() {
  local service container_id
  local running_count=0
  local failed_count=0

  IFS=',' read -ra probe_service_array <<< "$probe_services"
  for service in "${probe_service_array[@]}"; do
    [[ -n "$service" ]] || continue
    container_id="$(compose_ps_q "$service" || true)"
    if [[ -z "$container_id" ]] || ! container_running "$container_id"; then
      log "control-plane probe skipped: service=$service is not running"
      continue
    fi

    running_count=$((running_count + 1))
    if probe_container_postgres "$container_id"; then
      log "control-plane probe OK: service=$service can reach $db_host:$db_port"
    else
      failed_count=$((failed_count + 1))
      printf 'control-plane probe FAILED: service=%s cannot reach %s:%s\n' "$service" "$db_host" "$db_port" >&2
    fi
  done

  if (( running_count == 0 )); then
    return 2
  fi
  (( failed_count == 0 ))
}

probe_postgres_container() {
  local postgres_container
  postgres_container="$(env_or_file POSTGRES_CONTAINER)"
  [[ -n "$postgres_container" ]] || postgres_container=dune_server-postgres-1
  if [[ "$("$runtime" inspect -f '{{.State.Running}}' "$postgres_container" 2>/dev/null || true)" != true ]]; then
    printf 'control-plane probe FAILED: Postgres container is not running: %s\n' "$postgres_container" >&2
    return 1
  fi
  "$runtime" exec "$postgres_container" sh -lc 'pg_isready -U postgres >/dev/null 2>&1 || pg_isready >/dev/null 2>&1'
}

repair_network() {
  local rc=0
  if [[ -x "$script_dir/setup-lan-reflection.sh" ]]; then
    log 'control-plane repair: refreshing LAN/bridge firewall rules'
    if ! run_bounded "$repair_timeout" "$script_dir/setup-lan-reflection.sh" "$env_file"; then
      printf 'control-plane repair warning: setup-lan-reflection.sh failed\n' >&2
      rc=1
    fi
  fi

  if [[ -x "$script_dir/seed-gateway-neighbor.sh" ]]; then
    log 'control-plane repair: refreshing container neighbor entries'
    if command -v timeout >/dev/null 2>&1; then
      if ! run_bounded "$repair_timeout" env CONTAINER_RUNTIME="$runtime" "$script_dir/seed-gateway-neighbor.sh"; then
        printf 'control-plane repair warning: neighbor seeding failed or timed out\n' >&2
        rc=1
      fi
    elif ! CONTAINER_RUNTIME="$runtime" "$script_dir/seed-gateway-neighbor.sh"; then
      printf 'control-plane repair warning: neighbor seeding failed\n' >&2
      rc=1
    fi
  fi
  return "$rc"
}

verify_rmq_path() {
  [[ -x "$script_dir/verify-rmq-auth-path.sh" ]] || return 0
  ENV_FILE="$env_file" "$script_dir/verify-rmq-auth-path.sh"
}

run_probes() {
  local postgres_rc=0
  local map_rc=0
  probe_postgres_container || postgres_rc=$?
  probe_map_services || map_rc=$?
  if (( postgres_rc != 0 )); then
    return 1
  fi
  if (( map_rc == 2 )); then
    if [[ "$allow_no_map" == true ]]; then
      log 'control-plane probe: no configured map probe service is running'
      map_rc=0
    else
      printf 'control-plane probe inconclusive: no configured map probe service is running\n' >&2
      return 2
    fi
  fi
  if (( map_rc != 0 )); then
    return 1
  fi
  if [[ "$full" == true ]]; then
    verify_rmq_path
  fi
  return 0
}

if run_probes; then
  log 'control-plane health: healthy'
  exit 0
else
  initial_rc=$?
fi

if [[ "$repair" != true ]]; then
  exit "$initial_rc"
fi

repair_network || true
if run_probes; then
  log 'control-plane health: recovered after network repair'
  exit 0
else
  final_rc=$?
fi
printf 'control-plane health: degraded after repair attempt\n' >&2
exit "$final_rc"
