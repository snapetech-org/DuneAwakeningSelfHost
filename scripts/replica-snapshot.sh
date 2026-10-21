#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
read_env() {
  local key="$1"
  local value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}}"
db="${DUNE_DATABASE:-$(read_env DUNE_DATABASE)}"
db="${db:-dune_sb_1_4_0_0}"
keep_hours="${DUNE_REPLICA_SNAPSHOT_KEEP_HOURS:-$(read_env DUNE_REPLICA_SNAPSHOT_KEEP_HOURS)}"
keep_hours="${keep_hours:-48}"

# The third argument remains reserved for the former remote-root parameter.
# Dump files now stream from the standby into local storage.
snapshot_root="${4:-${DUNE_REPLICA_SNAPSHOT_ROOT:-$(read_env DUNE_REPLICA_SNAPSHOT_ROOT)}}"
snapshot_root="${snapshot_root:-${repo_root}/backups/replica-snapshots}"

if [[ -z "$remote" ]]; then
  printf 'remote host required: %s ENV_FILE REMOTE_HOST [REMOTE_ROOT] [SNAPSHOT_ROOT]\n' "$0" >&2
  exit 1
fi
if [[ ! "$db" =~ ^[A-Za-z0-9_]+$ ]]; then
  printf 'invalid DUNE_DATABASE: %s\n' "$db" >&2
  exit 2
fi
if [[ ! "$keep_hours" =~ ^[0-9]+$ ]]; then
  printf 'invalid DUNE_REPLICA_SNAPSHOT_KEEP_HOURS: %s\n' "$keep_hours" >&2
  exit 2
fi
keep_hours=$((10#$keep_hours))
if (( keep_hours > 8760 )); then
  printf 'DUNE_REPLICA_SNAPSHOT_KEEP_HOURS exceeds one year: %s\n' "$keep_hours" >&2
  exit 2
fi
keep_minutes=$((keep_hours * 60))

umask 077
mkdir -p -- "$snapshot_root"
chmod 0700 -- "$snapshot_root"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="$snapshot_root/postgres-${db}-${timestamp}.dump"
tmp_file="${dump_file}.partial.$$"
trap 'rm -f -- "$tmp_file"' EXIT

if ! ssh -T "$remote" docker exec dune-postgres-replica pg_dump -U dune -d "$db" -Fc >"$tmp_file"; then
  printf 'replica pg_dump failed; removed partial snapshot\n' >&2
  exit 1
fi
if [[ ! -s "$tmp_file" ]]; then
  printf 'replica pg_dump returned an empty snapshot\n' >&2
  exit 1
fi

chmod 0600 -- "$tmp_file"
mv -- "$tmp_file" "$dump_file"

# Apply the same window across database names so version changes cannot strand
# old dump series indefinitely. Nested archive directories are left untouched.
find "$snapshot_root" -maxdepth 1 -type f -name 'postgres-*.dump' \
  -mmin "+${keep_minutes}" -delete

printf 'snapshot saved locally: %s\n' "$dump_file"
ls -lh -- "$dump_file"
