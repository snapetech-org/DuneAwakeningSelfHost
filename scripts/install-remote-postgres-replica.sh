#!/usr/bin/env bash
set -euo pipefail

secret_tmp=""
cleanup() {
  if [[ -n "$secret_tmp" ]]; then
    rm -f "$secret_tmp"
  fi
}
trap cleanup EXIT

env_file="${1:-.env}"
remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-}}"
remote_root="${3:-${POSTGRES_REMOTE_REPLICA_ROOT:-/srv/dune-postgres-replica}}"
image="${POSTGRES_IMAGE:-${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}}"
db="${DUNE_DATABASE:-dune_sb_1_4_0_0}"
remote_port="$(awk -F= '/^POSTGRES_REMOTE_REPLICA_HOST_PORT=/{print $2}' "$env_file" 2>/dev/null | tail -1 || true)"
network_mode="$(awk -F= '/^DUNE_POSTGRES_STANDBY_NETWORK_MODE=/{print $2}' "$env_file" 2>/dev/null | tail -1 || true)"
network_mode="${DUNE_POSTGRES_STANDBY_NETWORK_MODE:-${network_mode:-bridge}}"

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi
if [[ -z "$remote" ]]; then
  printf 'remote host required: %s ENV_FILE REMOTE_HOST [REMOTE_ROOT]\n' "$0" >&2
  exit 1
fi

replication_user="$(awk -F= '/^POSTGRES_REPLICATION_USER=/{print $2}' "$env_file" | tail -1)"
replication_user="${replication_user:-dune_replicator}"
replication_password="$(awk -F= '/^POSTGRES_REPLICATION_PASSWORD=/{print $2}' "$env_file" | tail -1)"
replication_slot="$(awk -F= '/^POSTGRES_REMOTE_REPLICATION_SLOT=/{print $2}' "$env_file" | tail -1)"
replication_slot="${replication_slot:-dune_standby_remote}"
primary_host="$(awk -F= '/^POSTGRES_REPLICATION_PRIMARY_HOST=/{print $2}' "$env_file" | tail -1)"
primary_port="$(awk -F= '/^POSTGRES_REPLICATION_PUBLIC_PORT=/{print $2}' "$env_file" | tail -1)"
primary_port="${primary_port:-15434}"

if [[ -z "$primary_host" ]]; then
  primary_host="$(ip -4 route get "$remote" | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
fi
if [[ -z "$replication_password" || -z "$primary_host" ]]; then
  printf 'POSTGRES_REPLICATION_PASSWORD and primary host are required\n' >&2
  exit 1
fi

secret_tmp="$(mktemp)"
chmod 0600 "$secret_tmp"
printf '%s' "$replication_password" > "$secret_tmp"
remote_secret_dir="$remote_root/secrets"
remote_secret_path="$remote_secret_dir/postgres-replication-password"
remote_secret_tmp="$remote_secret_path.tmp.$$"

compose=(docker compose --env-file "$env_file" -f compose.yaml -f compose.replica.yaml)

printf 'ensuring primary replication role/slot/HBA for remote standby\n'
POSTGRES_REPLICATION_PASSWORD="$replication_password" \
  "${compose[@]}" exec -e POSTGRES_REPLICATION_PASSWORD -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 \
  -v repl_user="$replication_user" \
  -v repl_slot="$replication_slot" <<'SQL'
\getenv repl_password POSTGRES_REPLICATION_PASSWORD
select format('create role %I with replication login password %L', :'repl_user', :'repl_password')
where not exists (select 1 from pg_roles where rolname = :'repl_user') \gexec
select format('alter role %I with replication login password %L', :'repl_user', :'repl_password') \gexec
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

printf 'preparing remote directory on %s\n' "$remote"
ssh "$remote" "mkdir -p '$remote_root/scripts' '$remote_root/data' '$remote_root/snapshots' '$remote_secret_dir' && chmod 0700 '$remote_secret_dir'"
scp "$secret_tmp" "$remote:$remote_secret_tmp" >/dev/null
ssh "$remote" "chmod 0600 '$remote_secret_tmp' && mv '$remote_secret_tmp' '$remote_secret_path'"
scp scripts/postgres-replica-entrypoint.sh "$remote:$remote_root/scripts/postgres-replica-entrypoint.sh" >/dev/null
ssh "$remote" "chmod +x '$remote_root/scripts/postgres-replica-entrypoint.sh'"

if ! ssh "$remote" "docker image inspect '$image' >/dev/null 2>&1"; then
  printf 'copying postgres image to %s\n' "$remote"
  docker save "$image" | ssh "$remote" docker load
fi

port_args=""
if [[ -n "$remote_port" ]]; then
  port_args="-p 127.0.0.1:${remote_port}:5432"
fi
network_arg=""
if [[ "$network_mode" != "bridge" ]]; then
  network_arg="--network $network_mode"
  port_args=""
fi

printf 'starting remote replica container\n'
ssh "$remote" "docker rm -f dune-postgres-replica >/dev/null 2>&1 || true
docker run -d --name dune-postgres-replica --restart unless-stopped \
  $network_arg \
  $port_args \
  -v '$remote_secret_path:/run/secrets/dune_postgres_replication_password:ro' \
  -e POSTGRES_REPLICATION_USER='$replication_user' \
  -e POSTGRES_REPLICATION_PASSWORD_FILE=/run/secrets/dune_postgres_replication_password \
  -e POSTGRES_REPLICATION_SLOT='$replication_slot' \
  -e POSTGRES_PRIMARY_HOST='$primary_host' \
  -e POSTGRES_PRIMARY_PORT='$primary_port' \
  -v '$remote_root/data:/var/lib/postgresql/data' \
  -v '$remote_root/scripts/postgres-replica-entrypoint.sh:/workspace/scripts/postgres-replica-entrypoint.sh:ro' \
  '$image' /workspace/scripts/postgres-replica-entrypoint.sh"

unset replication_password
rm -f "$secret_tmp"
secret_tmp=""

printf 'remote replica started on %s. Primary status:\n' "$remote"
"${compose[@]}" exec -T postgres psql -U dune -d "$db" -c "
select slot_name, active, restart_lsn from pg_replication_slots where slot_name in ('$replication_slot');
select application_name, client_addr, state, sync_state, write_lag, flush_lag, replay_lag from pg_stat_replication;
"
