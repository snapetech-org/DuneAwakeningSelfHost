#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
target="${2:-}"
target_root="${3:-}"
seal_file="${4:-}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

read_seal() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$seal_file" 2>/dev/null | tail -1)"
  printf '%s' "$value"
}

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

target="${target:-${DUNE_POSTGRES_STANDBY_TARGET_HOST:-$(read_env DUNE_POSTGRES_STANDBY_TARGET_HOST)}}"
target="${target:-${DUNE_FAILOVER_PRIMARY_HOST:-$(read_env DUNE_FAILOVER_PRIMARY_HOST)}}"
target_root="${target_root:-${DUNE_POSTGRES_STANDBY_TARGET_ROOT:-$(read_env DUNE_POSTGRES_STANDBY_TARGET_ROOT)}}"
target_root="${target_root:-${POSTGRES_REMOTE_REPLICA_ROOT:-$(read_env POSTGRES_REMOTE_REPLICA_ROOT)}}"
seal_file="${seal_file:-${DUNE_POSTGRES_FAILOVER_SEAL_FILE:-$(read_env DUNE_POSTGRES_FAILOVER_SEAL_FILE)}}"
db="${DUNE_DATABASE:-$(read_env DUNE_DATABASE)}"; db="${db:-dune_sb_1_4_0_0}"
postgres_password="${POSTGRES_DUNE_PASSWORD:-$(read_env POSTGRES_DUNE_PASSWORD)}"
source_host="${POSTGRES_REPLICATION_PRIMARY_HOST:-$(read_env POSTGRES_REPLICATION_PRIMARY_HOST)}"
source_port="${POSTGRES_REPLICATION_PUBLIC_PORT:-$(read_env POSTGRES_REPLICATION_PUBLIC_PORT)}"; source_port="${source_port:-15434}"
image="${POSTGRES_IMAGE:-${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}}"
status_rc=0

if [[ -z "$target" || -z "$target_root" || -z "$seal_file" ]]; then
  printf 'target host, target root, and seal file are required\n' >&2
  exit 1
fi
if [[ ! -f "$seal_file" ]]; then
  printf 'seal file not found: %s\n' "$seal_file" >&2
  exit 1
fi
if [[ -z "$source_host" ]]; then
  source_host="$(ip -4 route get "$target" | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
fi

sealed_system_id="$(read_seal system_identifier)"
sealed_timeline="$(read_seal primary_timeline)"
sealed_lsn="$(read_seal primary_current_wal_lsn)"
compose=(docker compose --env-file "$env_file" -f compose.yaml -f compose.replica.yaml)

printf '== current primary identity ==\n'
current_system_id="$("${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc 'select system_identifier from pg_control_system();')"
current_in_recovery="$("${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc 'select pg_is_in_recovery();')"
printf 'system_identifier=%s in_recovery=%s\n' "$current_system_id" "$current_in_recovery"
if [[ "$current_in_recovery" != "f" && "$current_in_recovery" != "false" ]]; then
  printf 'FAIL current local Postgres is not writable primary\n' >&2
  status_rc=1
fi
if [[ "$current_system_id" != "$sealed_system_id" ]]; then
  printf 'FAIL current primary system identifier differs from seal\n' >&2
  status_rc=1
fi

printf '\n== target old-primary control data: %s ==\n' "$target"
control="$(ssh "$target" "docker run --rm -v '$target_root/data:/var/lib/postgresql/data:ro' '$image' pg_controldata /var/lib/postgresql/data" 2>/dev/null || true)"
if [[ -z "$control" ]]; then
  printf 'FAIL unable to read target pg_controldata from %s:%s/data\n' "$target" "$target_root" >&2
  exit 1
fi
printf '%s\n' "$control" | sed -n '1,18p'
target_system_id="$(printf '%s\n' "$control" | awk -F: '/Database system identifier/ {gsub(/^[ \t]+/, "", $2); print $2}')"
target_state="$(printf '%s\n' "$control" | awk -F: '/Database cluster state/ {gsub(/^[ \t]+/, "", $2); print $2}')"
target_timeline="$(printf '%s\n' "$control" | awk -F: 'index($1, "Latest checkpoint") && index($1, "TimeLineID") && !index($1, "PrevTimeLineID") {gsub(/^[ \t]+/, "", $2); print $2}')"
target_checkpoint_lsn="$(printf '%s\n' "$control" | awk -F: '/Latest checkpoint location/ {gsub(/^[ \t]+/, "", $2); print $2}')"

if [[ "$target_system_id" != "$sealed_system_id" ]]; then
  printf 'FAIL target system identifier differs from seal\n' >&2
  status_rc=1
fi
if [[ "$target_state" == "in production" ]]; then
  printf 'FAIL target data directory appears to be in production\n' >&2
  status_rc=1
fi
if [[ "$target_timeline" -gt "$sealed_timeline" ]]; then
  printf 'FAIL target timeline %s is newer than sealed timeline %s\n' "$target_timeline" "$sealed_timeline" >&2
  status_rc=1
fi

target_minus_seal="$("${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc "select pg_wal_lsn_diff('$target_checkpoint_lsn'::pg_lsn, '$sealed_lsn'::pg_lsn);")"
printf 'target_checkpoint_lsn=%s sealed_lsn=%s target_minus_seal_bytes=%s\n' "$target_checkpoint_lsn" "$sealed_lsn" "$target_minus_seal"
if [[ "$target_minus_seal" =~ ^[0-9] && "$target_minus_seal" != "0" ]]; then
  printf 'FAIL target checkpoint advanced past sealed LSN; cannot prove no divergent writes\n' >&2
  status_rc=1
fi

printf '\n== pg_rewind dry-run evidence ==\n'
if [[ "$target_state" == in\ archive\ recovery ]]; then
  printf 'OK target is already running as a standby in archive recovery; pg_rewind dry-run is not needed\n'
elif [[ -n "$postgres_password" ]]; then
  rewind_output="$(ssh "$target" "timeout 30 docker run --rm -e PGPASSWORD='$postgres_password' -v '$target_root/data:/var/lib/postgresql/data' '$image' gosu postgres pg_rewind --dry-run --target-pgdata=/var/lib/postgresql/data --source-server='host=$source_host port=$source_port user=dune dbname=$db'" 2>&1 || true)"
  printf '%s\n' "$rewind_output"
  if printf '%s\n' "$rewind_output" | grep -Eiq 'servers diverged|Done|no rewind required|source and target cluster are on the same timeline'; then
    printf 'OK pg_rewind dry-run completed far enough to prove common lineage/rewindability\n'
  else
    printf 'WARN pg_rewind dry-run did not produce a recognized success marker\n' >&2
    status_rc=1
  fi
else
  printf 'WARN POSTGRES_DUNE_PASSWORD missing; skipping pg_rewind dry-run\n' >&2
  status_rc=1
fi

if [[ "$status_rc" -eq 0 ]]; then
  printf '\nOK no divergent old-primary writes detected by seal/control-data/rewind checks\n'
else
  printf '\nFAIL cutback proof did not pass; use rebuild-postgres-standby instead of blind reverse\n' >&2
fi
exit "$status_rc"
