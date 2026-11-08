#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/rebuild-postgres-standby.sh [ENV_FILE] [TARGET_HOST] [TARGET_ROOT]

Run this from the current active Postgres primary. It prepares TARGET_HOST as a
fresh physical standby by replacing TARGET_ROOT/data with pg_basebackup output.
Dry-run is the default. Apply with CONFIRM_REBUILD_POSTGRES_STANDBY=yes.
EOF
}

env_file="${1:-.env}"
target="${2:-}"
target_root="${3:-}"

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

target="${target:-${DUNE_POSTGRES_STANDBY_TARGET_HOST:-$(read_env DUNE_POSTGRES_STANDBY_TARGET_HOST)}}"
target="${target:-${POSTGRES_REMOTE_REPLICA_HOST:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}}"
target="${target:-${DUNE_FAILOVER_STANDBY_HOST:-$(read_env DUNE_FAILOVER_STANDBY_HOST)}}"
target_root="${target_root:-${DUNE_POSTGRES_STANDBY_TARGET_ROOT:-$(read_env DUNE_POSTGRES_STANDBY_TARGET_ROOT)}}"
target_root="${target_root:-${POSTGRES_REMOTE_REPLICA_ROOT:-$(read_env POSTGRES_REMOTE_REPLICA_ROOT)}}"
replication_user="${POSTGRES_REPLICATION_USER:-$(read_env POSTGRES_REPLICATION_USER)}"; replication_user="${replication_user:-dune_replicator}"
replication_password="${POSTGRES_REPLICATION_PASSWORD:-$(read_env POSTGRES_REPLICATION_PASSWORD)}"
replication_slot="${DUNE_POSTGRES_STANDBY_SLOT:-$(read_env DUNE_POSTGRES_STANDBY_SLOT)}"
replication_slot="${replication_slot:-${POSTGRES_REMOTE_REPLICATION_SLOT:-$(read_env POSTGRES_REMOTE_REPLICATION_SLOT)}}"
replication_slot="${replication_slot:-dune_standby_remote}"
primary_host="${POSTGRES_REPLICATION_PRIMARY_HOST:-$(read_env POSTGRES_REPLICATION_PRIMARY_HOST)}"
primary_port="${POSTGRES_REPLICATION_PUBLIC_PORT:-$(read_env POSTGRES_REPLICATION_PUBLIC_PORT)}"; primary_port="${primary_port:-15434}"
image="${POSTGRES_IMAGE:-${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}}"
db="${DUNE_DATABASE:-$(read_env DUNE_DATABASE)}"; db="${db:-dune_sb_1_4_0_0}"
confirm="${CONFIRM_REBUILD_POSTGRES_STANDBY:-no}"
network_mode="${DUNE_POSTGRES_STANDBY_NETWORK_MODE:-$(read_env DUNE_POSTGRES_STANDBY_NETWORK_MODE)}"
network_mode="${network_mode:-bridge}"

if [[ -z "$target" || -z "$target_root" || -z "$replication_password" ]]; then
  printf 'target host, target root, and POSTGRES_REPLICATION_PASSWORD are required\n' >&2
  exit 1
fi
if [[ "$target" == "$(hostname)" || "$target" == "$(hostname -s)" ]]; then
  printf 'refusing to rebuild standby on the local active primary host: %s\n' "$target" >&2
  exit 1
fi
if [[ -z "$primary_host" ]]; then
  primary_host="$(ip -4 route get "$target" | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
fi
if [[ -z "$primary_host" ]]; then
  printf 'POSTGRES_REPLICATION_PRIMARY_HOST could not be derived for target %s\n' "$target" >&2
  exit 1
fi

compose=(docker compose --env-file "$env_file" -f compose.yaml -f compose.replica.yaml)

printf 'current_active_primary=%s\n' "$(hostname -f 2>/dev/null || hostname)"
printf 'target_standby=%s\n' "$target"
printf 'target_root=%s\n' "$target_root"
printf 'replication_slot=%s\n' "$replication_slot"
printf 'primary_endpoint=%s:%s\n' "$primary_host" "$primary_port"
printf 'standby_network_mode=%s\n' "$network_mode"

printf '\n== local primary state ==\n'
"${compose[@]}" exec -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 -c 'select pg_is_in_recovery() as in_recovery;'
in_recovery="$("${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc 'select pg_is_in_recovery();' 2>/dev/null || printf 'error')"
if [[ "$in_recovery" != "f" && "$in_recovery" != "false" ]]; then
  printf 'local Postgres is not primary: pg_is_in_recovery()=%s\n' "$in_recovery" >&2
  exit 1
fi

cat <<EOF

This operation replaces ${target}:${target_root}/data with a new physical
standby cloned from this host. Any stale Postgres data on the target is moved to
a timestamped backup directory first, but it must not be started as a writer.
EOF

if [[ "$confirm" != "yes" ]]; then
  cat <<EOF

Dry run only. To apply:
  CONFIRM_REBUILD_POSTGRES_STANDBY=yes make rebuild-postgres-standby ENV_FILE=${env_file} TARGET=${target} ROOT=${target_root}
EOF
  exit 0
fi

secret_tmp="$(mktemp)"
chmod 0600 "$secret_tmp"
printf '%s' "$replication_password" > "$secret_tmp"
trap 'rm -f "$secret_tmp"' EXIT
remote_secret_dir="$target_root/secrets"
remote_secret_path="$remote_secret_dir/postgres-replication-password"
remote_secret_tmp="$remote_secret_path.tmp.$$"

printf '\n== ensuring primary replication role/slot/HBA ==\n'
POSTGRES_REPLICATION_PASSWORD="$replication_password" \
  "${compose[@]}" exec -e POSTGRES_REPLICATION_PASSWORD -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 \
  -v repl_user="$replication_user" \
  -v repl_slot="$replication_slot" <<'SQL'
\getenv repl_password POSTGRES_REPLICATION_PASSWORD
select format('create role %I with replication login password %L', :'repl_user', :'repl_password')
where not exists (select 1 from pg_roles where rolname = :'repl_user') \gexec
select format('alter role %I with replication login password %L', :'repl_user', :'repl_password') \gexec
select pg_drop_replication_slot(:'repl_slot')
where exists (
  select 1 from pg_replication_slots
  where slot_name = :'repl_slot' and active = false
);
select pg_create_physical_replication_slot(:'repl_slot')
where not exists (select 1 from pg_replication_slots where slot_name = :'repl_slot');
SQL

"${compose[@]}" exec -T postgres sh -lc "
set -eu
hba=/var/lib/postgresql/data/pg_hba.conf
rule='host replication ${replication_user} all scram-sha-256'
grep -qxF \"\$rule\" \"\$hba\" || printf '\n%s\n' \"\$rule\" >> \"\$hba\"
psql -U dune -d \"$db\" -c 'select pg_reload_conf();'
"

printf '\n== preparing target standby host ==\n'
ssh "$target" "mkdir -p '$target_root/scripts' '$target_root/snapshots' '$remote_secret_dir' && chmod 0700 '$remote_secret_dir'"
scp "$secret_tmp" "$target:$remote_secret_tmp" >/dev/null
ssh "$target" "chmod 0600 '$remote_secret_tmp' && mv '$remote_secret_tmp' '$remote_secret_path'"
scp scripts/postgres-replica-entrypoint.sh "$target:$target_root/scripts/postgres-replica-entrypoint.sh" >/dev/null
ssh "$target" "chmod +x '$target_root/scripts/postgres-replica-entrypoint.sh'"

if ! ssh "$target" "docker image inspect '$image' >/dev/null 2>&1"; then
  printf 'copying postgres image to %s\n' "$target"
  docker save "$image" | ssh "$target" docker load
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
network_arg=""
if [[ "$network_mode" != "bridge" ]]; then
  network_arg="--network $network_mode"
fi
ssh "$target" "set -eu
docker rm -f dune-postgres-replica >/dev/null 2>&1 || true
if [ -d '$target_root/data' ]; then
  mv '$target_root/data' '$target_root/data.rebuild-backup.$stamp'
fi
mkdir -p '$target_root/data'
docker run -d --name dune-postgres-replica --restart unless-stopped \
  $network_arg \
  -v '$remote_secret_path:/run/secrets/dune_postgres_replication_password:ro' \
  -e POSTGRES_REPLICATION_USER='$replication_user' \
  -e POSTGRES_REPLICATION_PASSWORD_FILE=/run/secrets/dune_postgres_replication_password \
  -e POSTGRES_REPLICATION_SLOT='$replication_slot' \
  -e POSTGRES_PRIMARY_HOST='$primary_host' \
  -e POSTGRES_PRIMARY_PORT='$primary_port' \
  -v '$target_root/data:/var/lib/postgresql/data' \
  -v '$target_root/scripts/postgres-replica-entrypoint.sh:/workspace/scripts/postgres-replica-entrypoint.sh:ro' \
  '$image' /workspace/scripts/postgres-replica-entrypoint.sh"

unset replication_password
rm -f "$secret_tmp"
trap - EXIT

printf '\nnew standby started on %s. Replication status:\n' "$target"
"${compose[@]}" exec -T postgres psql -U dune -d "$db" -c "
select slot_name, active, restart_lsn, wal_status from pg_replication_slots where slot_name = '$replication_slot';
select application_name, client_addr, state, sync_state, write_lag, flush_lag, replay_lag from pg_stat_replication;
"
