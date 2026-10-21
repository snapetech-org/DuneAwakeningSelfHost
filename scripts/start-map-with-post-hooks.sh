#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/start-map-with-post-hooks.sh ENV_FILE SERVICE [SERVICE...]

Manual live-map start/recreate wrapper. Prefer scripts/recover-map.sh when a
fixed-partition map has a stale server id or degraded partition registration.
This wrapper exists for intentional manual starts where raw docker compose up
would otherwise skip post-start hooks.
USAGE
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

env_file="$1"
shift
services=("$@")
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
seed_timeout="${DUNE_SEED_NEIGHBOR_TIMEOUT_SECONDS:-90}"

cd "$repo_root"

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 2
fi

if [[ -f "$script_dir/ensure-map-network-capacity.py" ]]; then
  python3 "$script_dir/ensure-map-network-capacity.py" "$env_file"
fi

if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi

compose=(docker compose)
IFS=':' read -r -a compose_files <<< "${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
for compose_file in "${compose_files[@]}"; do
  [[ -n "$compose_file" ]] && compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")
map_catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$map_catalog_script" ]]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$map_catalog_script" >&2
  exit 1
fi
all_map_services="$("$map_catalog_script" "$env_file" --field all-services --write-cache)"
for service in "${services[@]}"; do
  case " $all_map_services " in
    *" $service "*) ;;
    *) printf 'unknown Compose map worker: %s\n' "$service" >&2; exit 2 ;;
  esac
done
all_map_profiles="$("$map_catalog_script" "$env_file" --field all-profiles)"
for profile in $all_map_profiles; do
  compose+=(--profile "$profile")
done

seed_neighbors() {
  if [[ ! -x "$script_dir/seed-gateway-neighbor.sh" ]]; then
    return
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5s "${seed_timeout}s" "$script_dir/seed-gateway-neighbor.sh" || true
  else
    "$script_dir/seed-gateway-neighbor.sh" || true
  fi
}

case "${DUNE_REQUIRE_PRODUCTION_HOST_FOR_MANUAL_MAP_START:-true}" in
  1|true|yes|on|TRUE|True|YES|ON)
    current_host="$(hostname)"
    required_host="${DUNE_PRODUCTION_HOSTNAME:-kspls0}"
    if [[ "$current_host" != "$required_host" ]]; then
      printf 'refusing manual live-map start on %s; expected %s\n' "$current_host" "$required_host" >&2
      exit 1
    fi
    ;;
esac

seed_neighbors

case "${DUNE_LANDSRAAD_CORIOLIS_GUARD_ENABLED:-true}" in
  1|true|yes|on|TRUE|True|YES|ON)
    if [[ -x "$script_dir/validate-landsraad-coriolis-cycle.sh" ]]; then
      "$script_dir/validate-landsraad-coriolis-cycle.sh" "$env_file"
    fi
    ;;
esac

printf 'starting/recreating map services with post hooks: %s\n' "${services[*]}"
"${compose[@]}" up --pull never -d --force-recreate --no-deps "${services[@]}"

seed_neighbors

if [[ -x "$script_dir/restart-post-start-health.sh" ]]; then
  ENV_FILE="$env_file" "$script_dir/restart-post-start-health.sh"
elif [[ -x "$script_dir/verify-rmq-auth-path.sh" ]]; then
  ENV_FILE="$env_file" "$script_dir/verify-rmq-auth-path.sh"
fi

case "${DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED:-true}" in
  1|true|yes|on|TRUE|True|YES|ON)
    if [[ -x "$script_dir/patch-logoff-timers-runtime.sh" ]]; then
      "$script_dir/patch-logoff-timers-runtime.sh" --local --dry-run
    fi
    ;;
esac

"${compose[@]}" ps "${services[@]}"
