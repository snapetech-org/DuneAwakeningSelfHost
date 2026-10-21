#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/recreate-rabbitmq-tls-stack.sh [ENV_FILE]

Recreate RabbitMQ and Dune services that must reload client-facing RabbitMQ TLS
material. Dry-run by default. Apply with CONFIRM_RECREATE_RMQ_TLS_STACK=yes.
EOF
}

env_file="${1:-.env}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_files="${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
map_catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$map_catalog_script" ]]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$map_catalog_script" >&2
  exit 1
fi
map_services="$(COMPOSE_FILES="$compose_files" "$map_catalog_script" "$env_file" --field services --write-cache)"
map_profiles="$(COMPOSE_FILES="$compose_files" "$map_catalog_script" "$env_file" --field profiles)"
default_services="game-rmq gateway director text-router $map_services"
services="${DUNE_RMQ_TLS_RECREATE_SERVICES:-$(read_env DUNE_RMQ_TLS_RECREATE_SERVICES)}"
services="${services:-$default_services}"
if [[ -z "$services" ]]; then
  printf 'DUNE_RMQ_TLS_RECREATE_SERVICES is required\n' >&2
  exit 1
fi

compose=(docker compose --env-file "$env_file")
IFS=':' read -ra compose_file_array <<< "$compose_files"
for compose_file in "${compose_file_array[@]}"; do
  compose+=(-f "$compose_file")
done
for profile in $map_profiles; do
  compose+=(--profile "$profile")
done

split_services() {
  rmq_requested=false
  dependent_services=()
  local service
  for service in $services; do
    if [[ "$service" == "game-rmq" ]]; then
      rmq_requested=true
    else
      dependent_services+=("$service")
    fi
  done
}

wait_for_game_rmq() {
  local timeout="${DUNE_RMQ_TLS_RECREATE_WAIT_SECONDS:-180}"
  local deadline=$((SECONDS + timeout))
  local container_id=""

  printf 'waiting for game-rmq broker ping, timeout_seconds=%s\n' "$timeout"
  while (( SECONDS < deadline )); do
    container_id="$("${compose[@]}" ps -q game-rmq 2>/dev/null || true)"
    if [[ -n "$container_id" ]] && docker exec "$container_id" rabbitmq-diagnostics -q ping >/dev/null 2>&1; then
      printf 'OK game-rmq broker ping succeeded\n'
      return 0
    fi
    sleep 2
  done

  printf 'ERROR game-rmq did not answer broker ping within %s seconds\n' "$timeout" >&2
  return 1
}

split_services

printf 'services_to_recreate=%s\n' "$services"
if [[ "$rmq_requested" == true ]]; then
  printf 'recreate_phase_1=game-rmq\n'
fi
if [[ "${#dependent_services[@]}" -gt 0 ]]; then
  printf 'recreate_phase_2=%s\n' "${dependent_services[*]}"
fi
printf '\n== current RabbitMQ TLS SAN ==\n'
tls_rc=0
./scripts/check-rabbitmq-cert-sans.sh "$env_file" || tls_rc=1

if [[ "${CONFIRM_RECREATE_RMQ_TLS_STACK:-}" != "yes" ]]; then
  cat <<EOF
Dry run only. To recreate services during maintenance:
  CONFIRM_RECREATE_RMQ_TLS_STACK=yes make rabbitmq-cert-recreate-stack ENV_FILE=${env_file}
EOF
  exit 0
fi

if [[ "$tls_rc" -ne 0 && "${DUNE_ALLOW_INVALID_RMQ_TLS_RECREATE:-}" != "yes" ]]; then
  printf 'refusing recreate because RabbitMQ TLS SAN check failed\n' >&2
  printf 'install staged TLS first or set DUNE_ALLOW_INVALID_RMQ_TLS_RECREATE=yes for an intentional emergency\n' >&2
  exit 1
fi

printf '\n== recreating RabbitMQ TLS-dependent services ==\n'
if [[ "$rmq_requested" == true ]]; then
  printf '== phase 1: game-rmq ==\n'
  "${compose[@]}" up -d --force-recreate game-rmq
  wait_for_game_rmq
fi

if [[ "${#dependent_services[@]}" -gt 0 ]]; then
  if [[ "$rmq_requested" == false ]]; then
    wait_for_game_rmq
  fi
  printf '\n== phase 2: RabbitMQ clients ==\n'
  "${compose[@]}" up -d --force-recreate "${dependent_services[@]}"
fi

printf '\n== post-recreate status ==\n'
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml ./scripts/status.sh "$env_file"
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml ./scripts/rmq-health.sh "$env_file"
