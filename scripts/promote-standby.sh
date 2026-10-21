#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}}"
remote_root="${POSTGRES_REMOTE_REPLICA_ROOT:-$(read_env POSTGRES_REMOTE_REPLICA_ROOT)}"
db="${DUNE_DATABASE:-$(read_env DUNE_DATABASE)}"; db="${db:-dune_sb_1_4_0_0}"
remote_repo="${DUNE_STANDBY_REPO_ROOT:-$PWD}"

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi
if [[ -z "$remote" ]]; then
  printf 'remote host required: %s ENV_FILE REMOTE\n' "$0" >&2
  exit 1
fi
if [[ -z "$remote_root" ]]; then
  printf 'POSTGRES_REMOTE_REPLICA_ROOT is required\n' >&2
  exit 1
fi
if [[ "${CONFIRM_PROMOTE_STANDBY:-}" != "yes" ]]; then
  cat >&2 <<'EOF'
Refusing to promote without CONFIRM_PROMOTE_STANDBY=yes.
Stop or isolate all Dune writers on the old primary first, then rerun:
  CONFIRM_PROMOTE_STANDBY=yes make promote-standby ENV_FILE=.env REMOTE=<standby-host>
EOF
  exit 2
fi

compose=(docker compose --env-file "$env_file" -f compose.yaml -f compose.replica.yaml)
control_services=(postgres admin-rmq game-rmq db-init director text-router gateway rmq-auth-shim)

printf 'checking primary-side replay status before promotion\n'
"${compose[@]}" exec -T postgres psql -U dune -d "$db" -c "
select application_name, state, sync_state, write_lag, flush_lag, replay_lag
from pg_stat_replication;
"

printf 'promoting remote standby on %s\n' "$remote"
ssh "$remote" "docker exec dune-postgres-replica psql -U dune -d '$db' -v ON_ERROR_STOP=1 -c 'select pg_promote(wait => true);'
docker exec dune-postgres-replica psql -U dune -d '$db' -c 'select pg_is_in_recovery() as still_in_recovery;'"

printf 'stopping remote replica container before compose adopts promoted data path\n'
ssh "$remote" "docker rm -f dune-postgres-replica >/dev/null 2>&1 || true"

printf 'starting promoted stack on %s using %s/data as postgres volume\n' "$remote" "$remote_root"
ssh "$remote" "cd '$remote_repo'
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml ./scripts/map_catalog.py '$env_file' --field json --write-cache >/dev/null
map_services=\$(COMPOSE_FILES=compose.yaml:compose.allmaps.yaml ./scripts/map_catalog.py '$env_file' --field services)
map_profiles=\$(COMPOSE_FILES=compose.yaml:compose.allmaps.yaml ./scripts/map_catalog.py '$env_file' --field profiles)
profile_args=''
for profile in \$map_profiles; do profile_args=\"\$profile_args --profile \$profile\"; done
POSTGRES_REMOTE_REPLICA_ROOT='$remote_root' docker compose --env-file '$env_file' -f compose.yaml -f compose.failover-standby.yaml up -d ${control_services[*]}
POSTGRES_REMOTE_REPLICA_ROOT='$remote_root' docker compose --env-file '$env_file' -f compose.yaml -f compose.failover-standby.yaml -f compose.allmaps.yaml \$profile_args up -d \$map_services
COMPOSE_FILES=compose.yaml:compose.failover-standby.yaml:compose.allmaps.yaml ./scripts/status.sh '$env_file'"
