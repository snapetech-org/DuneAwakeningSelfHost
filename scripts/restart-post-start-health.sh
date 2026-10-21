#!/usr/bin/env bash
set -euo pipefail

env_file="${ENV_FILE:-.env}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
restart_target="${DUNE_RESTART_TARGET:-}"
restart_phase="${DUNE_RESTART_PHASE:-}"
if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi
compose_files="${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
project="${COMPOSE_PROJECT_NAME:-dune_server}"
text_router_service="${TEXT_ROUTER_SERVICE:-text-router}"
text_router_container="${TEXT_ROUTER_CONTAINER:-${project}-${text_router_service}-1}"
postgres_container="${POSTGRES_CONTAINER:-${project}-postgres-1}"
seed_script="${DUNE_SEED_NEIGHBOR_SCRIPT:-./scripts/seed-gateway-neighbor.sh}"
verify_script="${DUNE_VERIFY_RMQ_AUTH_PATH_SCRIPT:-./scripts/verify-rmq-auth-path.sh}"
timeout="${DUNE_RESTART_POST_START_TIMEOUT_SECONDS:-180}"
stability_seconds=0
all_map_services=()
if [[ "$restart_target" == "all" && ( "$restart_phase" == "start" || "$restart_phase" == "restart" ) ]]; then
  timeout="${DUNE_RESTART_POST_START_TIMEOUT_SECONDS:-900}"
  stability_seconds="${DUNE_RESTART_POST_START_STABILITY_SECONDS:-120}"
fi
interval="${DUNE_RESTART_POST_START_INTERVAL_SECONDS:-5}"
seed_timeout="${DUNE_SEED_NEIGHBOR_TIMEOUT_SECONDS:-90}"
logoff_patch_enabled="${DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED:-true}"
landsraad_coriolis_guard_enabled="${DUNE_LANDSRAAD_CORIOLIS_GUARD_ENABLED:-true}"
lan_reflection_refresh_enabled="${DUNE_LAN_REFLECTION_REFRESH_AFTER_START:-true}"

compose_cmd=(docker compose)
IFS=':' read -r -a compose_file_array <<< "$compose_files"
for file in "${compose_file_array[@]}"; do
  [[ -n "$file" ]] && compose_cmd+=(-f "$file")
done
compose_cmd+=(--env-file "$env_file")

is_running() {
  local container="$1"
  [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" == "true" ]]
}

seed_neighbors() {
  if [[ -x "$seed_script" ]]; then
    if command -v timeout >/dev/null 2>&1; then
      timeout --kill-after=5s "${seed_timeout}s" "$seed_script" || true
    else
      "$seed_script" || true
    fi
  fi
}

refresh_map_neighbors() {
  if [[ ! -x "$script_dir/refresh-map-neighbors.sh" ]]; then
    return 0
  fi
  if ! CONTAINER_RUNTIME=docker COMPOSE_PROJECT_NAME="$project" \
      "$script_dir/refresh-map-neighbors.sh" --all; then
    printf 'warning: map-to-map neighbor refresh failed after restart\n' >&2
  fi
}

run_post_start_hooks() {
  if [[ "$landsraad_coriolis_guard_enabled" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ && -x "$script_dir/validate-landsraad-coriolis-cycle.sh" ]]; then
    "$script_dir/validate-landsraad-coriolis-cycle.sh" "$env_file"
  fi
  if [[ -x "$script_dir/brt-dd-next-downtime.sh" ]]; then
    "$script_dir/brt-dd-next-downtime.sh" apply-pending "$env_file"
  fi
  if [[ "$logoff_patch_enabled" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ && -x "$script_dir/patch-logoff-timers-runtime.sh" ]]; then
    "$script_dir/patch-logoff-timers-runtime.sh" --local \
      || printf 'warning: logoff timer runtime patch failed after restart\n' >&2
  fi
}

postgres_accepts_connections() {
  is_running "$postgres_container" || return 1
  docker exec "$postgres_container" sh -lc 'pg_isready -U "${POSTGRES_USER:-postgres}" >/dev/null 2>&1 || pg_isready >/dev/null 2>&1'
}

full_farm_ready() {
  local status_output current_line core_line current_ready current_alive active_servers partitions
  local game_connections admin_connections core_ready core_alive core_active core_total map_total
  [[ -x "$script_dir/status.sh" ]] || return 1
  status_output="$("$script_dir/status.sh" "$env_file" --public-summary 2>/dev/null)" || return 1
  current_line="$(printf '%s\n' "$status_output" | sed -nE 's/^current_ready_alive=([0-9]+) current_alive_active=([0-9]+) active_servers=([0-9]+) partitions=([0-9]+) game_sg_connections=([0-9]+) admin_sg_connections=([0-9]+)$/\1 \2 \3 \4 \5 \6/p' | tail -1)"
  core_line="$(printf '%s\n' "$status_output" | sed -nE 's/^core_ready_alive=([0-9]+) core_alive_active=([0-9]+) core_active_servers=([0-9]+) core_partitions=([0-9]+)$/\1 \2 \3 \4/p' | tail -1)"
  [[ -n "$current_line" && -n "$core_line" ]] || return 1
  read -r current_ready current_alive active_servers partitions game_connections admin_connections <<< "$current_line"
  read -r core_ready core_alive core_active core_total <<< "$core_line"
  map_total="${#all_map_services[@]}"
  [[ "$map_total" =~ ^[1-9][0-9]*$ && "$partitions" == "$map_total" \
    && "$current_ready" == "$map_total" && "$current_alive" == "$map_total" \
    && "$active_servers" == "$map_total" && "$game_connections" -ge "$map_total" \
    && "$core_total" =~ ^[1-9][0-9]*$ && "$core_ready" == "$core_total" \
    && "$core_alive" == "$core_total" && "$core_active" == "$core_total" ]]
}

if [[ "$restart_target" == "all" && ( "$restart_phase" == "start" || "$restart_phase" == "restart" ) \
    && ! -x "$script_dir/status.sh" ]]; then
  printf 'full-farm start requires scripts/status.sh for core readiness verification\n' >&2
  exit 1
fi
if [[ "$restart_target" == "all" && ( "$restart_phase" == "start" || "$restart_phase" == "restart" ) ]]; then
  map_catalog_script="$script_dir/map_catalog.py"
  if [[ ! -x "$map_catalog_script" ]]; then
    printf 'full-farm start requires scripts/map_catalog.py to verify all map workers\n' >&2
    exit 1
  fi
  map_services_output="$("$map_catalog_script" "$env_file" --field services --write-cache)" || {
    printf 'full-farm start could not read the active map-service roster\n' >&2
    exit 1
  }
  read -r -a all_map_services <<< "$map_services_output"
  if (( ${#all_map_services[@]} == 0 )); then
    printf 'full-farm start found an empty active map-service roster\n' >&2
    exit 1
  fi
fi

all_map_containers_running() {
  local running_services service
  running_services="$(docker ps --filter "label=com.docker.compose.project=$project" \
    --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null)" || return 1
  for service in "${all_map_services[@]}"; do
    grep -Fqx -- "$service" <<< "$running_services" || return 1
  done
}

deadline=$((SECONDS + timeout))
stable_since=0

seed_neighbors

# Refresh nftables DNAT-forward accepts after the Compose network exists. New
# deployments use a stable bridge name, while this also repairs legacy networks.
if [[ "$lan_reflection_refresh_enabled" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ \
      && -x "$script_dir/setup-lan-reflection.sh" ]]; then
  "$script_dir/setup-lan-reflection.sh" "$env_file" \
    || printf 'warning: LAN reflection/firewall refresh failed after restart\n' >&2
fi

while ! postgres_accepts_connections; do
  if (( SECONDS >= deadline )); then
    printf 'postgres was not connection-ready after %ss\n' "$timeout" >&2
    exit 1
  fi
  sleep "$interval"
done

text_router_recreated=false
while (( SECONDS < deadline )); do
  seed_neighbors

  if ! is_running "$text_router_container"; then
    printf 'text-router is not running after start; recreating it after postgres readiness\n' >&2
    "${compose_cmd[@]}" up -d --force-recreate --no-deps "$text_router_service"
    text_router_recreated=true
    sleep "$interval"
    continue
  fi

  if [[ -x "$verify_script" ]] && "$verify_script"; then
    if [[ "$restart_target" == "all" && ( "$restart_phase" == "start" || "$restart_phase" == "restart" ) ]]; then
      if ! full_farm_ready || ! all_map_containers_running; then
        stable_since=0
        sleep "$interval"
        continue
      fi
      if (( stable_since == 0 )); then
        stable_since=$SECONDS
      fi
      if (( SECONDS - stable_since < stability_seconds )); then
        sleep "$interval"
        continue
      fi
    fi
    refresh_map_neighbors
    run_post_start_hooks
    if [[ "$restart_target" == "all" && ( "$restart_phase" == "start" || "$restart_phase" == "restart" ) ]]; then
      build_check_script="$script_dir/verify-running-image-tag.sh"
      if [[ ! -x "$build_check_script" ]]; then
        printf 'latest Steam build check is required for a full-farm start but the verifier is missing\n' >&2
        exit 1
      fi
      "$build_check_script" "$env_file" --versions-only
    fi
    if [[ "$text_router_recreated" == "true" ]]; then
      printf 'post-start recovery recreated text-router and verified RMQ/auth routing\n'
    else
      printf 'post-start RMQ/auth routing verified\n'
    fi
    exit 0
  fi

  sleep "$interval"
done

if [[ "$restart_target" == "all" && ( "$restart_phase" == "start" || "$restart_phase" == "restart" ) ]]; then
  printf 'full-farm post-start readiness did not converge after %ss\n' "$timeout" >&2
else
  printf 'post-start health checks did not pass after %ss\n' "$timeout" >&2
fi
if [[ -x "$verify_script" ]]; then
  "$verify_script" || true
fi
exit 1
