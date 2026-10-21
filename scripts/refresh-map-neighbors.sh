#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s MAP_SERVICE|--all\n' "${0##*/}" >&2
}

if [[ $# -ne 1 || -z "${1:-}" ]]; then
  usage
  exit 2
fi

target_service="$1"
refresh_all=false
if [[ "$target_service" == "--all" ]]; then
  refresh_all=true
  target_service=""
fi
runtime="${CONTAINER_RUNTIME:-docker}"
project="${COMPOSE_PROJECT_NAME:-dune_server}"

run_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

is_map_service() {
  case "$1" in
    postgres|admin-rmq|game-rmq|db-init|director|gateway|text-router|rmq-auth-shim|admin-panel|admin-panel-ingress|admin-chat-commands)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

declare -A container_ip=()
declare -A container_mac=()
declare -A container_pid=()
declare -A container_dev=()
map_containers=()
target_containers=()

while IFS= read -r name; do
  [[ -n "$name" ]] || continue
  service="$($runtime inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$name" 2>/dev/null || true)"
  is_map_service "$service" || continue

  pid="$($runtime inspect -f '{{.State.Pid}}' "$name" 2>/dev/null || true)"
  ip="$($runtime inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name" 2>/dev/null || true)"
  mac="$($runtime inspect -f '{{range .NetworkSettings.Networks}}{{.MacAddress}}{{end}}' "$name" 2>/dev/null || true)"
  [[ -n "$pid" && "$pid" != 0 && -n "$ip" && -n "$mac" ]] || continue
  dev="$(run_root nsenter -t "$pid" -n ip route show default 2>/dev/null \
    | awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')"
  [[ -n "$dev" ]] || continue

  map_containers+=("$name")
  container_ip["$name"]="$ip"
  container_mac["$name"]="$mac"
  container_pid["$name"]="$pid"
  container_dev["$name"]="$dev"
  if [[ "$service" == "$target_service" ]]; then
    target_containers+=("$name")
  fi
done < <("$runtime" ps \
  --filter "label=com.docker.compose.project=$project" \
  --filter status=running \
  --format '{{.Names}}')

if [[ "$refresh_all" == "true" ]]; then
  target_containers=("${map_containers[@]}")
elif [[ "${#target_containers[@]}" -eq 0 ]]; then
  printf 'target map service is not running: %s\n' "$target_service" >&2
  exit 1
fi

if [[ "$refresh_all" == "true" ]]; then
  printf 'refreshing map neighbors: target=all maps=%s\n' "${#map_containers[@]}"
else
  printf 'refreshing map neighbors: target=%s maps=%s\n' "$target_service" "${#map_containers[@]}"
fi
attempted=0
failures=0

seed_pair() {
  local source="$1"
  local target="$2"
  [[ "$source" != "$target" ]] || return 0
  attempted=$((attempted + 1))
  if ! run_root nsenter -t "${container_pid[$source]}" -n ip neigh replace \
      "${container_ip[$target]}" lladdr "${container_mac[$target]}" \
      dev "${container_dev[$source]}" nud permanent; then
    failures=$((failures + 1))
    printf 'warning: neighbor refresh failed: %s -> %s\n' "$source" "$target" >&2
  fi
}

if [[ "$refresh_all" == "true" ]]; then
  for source in "${map_containers[@]}"; do
    for target in "${map_containers[@]}"; do
      seed_pair "$source" "$target"
    done
  done
else
  for target in "${target_containers[@]}"; do
    for source in "${map_containers[@]}"; do
      seed_pair "$source" "$target"
    done
    for other in "${map_containers[@]}"; do
      seed_pair "$target" "$other"
    done
  done
fi

printf 'map neighbor refresh complete: attempted=%s failures=%s\n' "$attempted" "$failures"
(( failures == 0 ))
