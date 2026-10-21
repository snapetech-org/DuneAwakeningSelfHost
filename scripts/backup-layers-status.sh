#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-}}"
# Keep the legacy third argument accepted for CLI compatibility.
remote_root="${3:-${POSTGRES_REMOTE_REPLICA_ROOT:-/srv/dune-postgres-replica}}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db="${DUNE_DATABASE:-$(awk -F= '/^DUNE_DATABASE=/{sub(/^[^=]*=/, ""); print}' "$env_file" 2>/dev/null | tail -1)}"
db="${db%\"}"
db="${db#\"}"
db="${db%\'}"
db="${db#\'}"
db="${db:-dune_sb_1_4_0_0}"
slot="$(awk -F= '/^POSTGRES_REMOTE_REPLICATION_SLOT=/{print $2}' "$env_file" | tail -1)"
slot="${slot:-dune_standby_remote}"
snapshot_root="${DUNE_REPLICA_SNAPSHOT_ROOT:-$(awk -F= '/^DUNE_REPLICA_SNAPSHOT_ROOT=/{sub(/^[^=]*=/, ""); print}' "$env_file" 2>/dev/null | tail -1)}"
snapshot_root="${snapshot_root%\"}"
snapshot_root="${snapshot_root#\"}"
snapshot_root="${snapshot_root%\'}"
snapshot_root="${snapshot_root#\'}"
snapshot_root="${snapshot_root:-${repo_root}/backups/replica-snapshots}"

if [[ -z "$remote" ]]; then
  printf 'remote host required: %s ENV_FILE REMOTE_HOST [REMOTE_ROOT]\n' "$0" >&2
  exit 1
fi

compose=(docker compose --env-file "$env_file" -f compose.yaml -f compose.replica.yaml)

printf '== layer 1: remote streaming replication ==\n'
"${compose[@]}" exec -T postgres psql -U dune -d "$db" -c "
select slot_name, active, restart_lsn, wal_status
from pg_replication_slots
where slot_name = '$slot';
select application_name, client_addr, state, sync_state, write_lag, flush_lag, replay_lag
from pg_stat_replication;
"

printf '\n== remote standby ==\n'
ssh "$remote" "docker ps --filter name=dune-postgres-replica --format '{{.Names}} {{.Status}}'
docker exec dune-postgres-replica psql -U dune -d '$db' -c \"select pg_is_in_recovery() as standby, now() - pg_last_xact_replay_timestamp() as replay_delay;\""

printf '\n== layer 2: rolling replica snapshots on local storage ==\n'
find "$snapshot_root" -maxdepth 2 -type f -name 'postgres-*.dump' -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' 2>/dev/null | sort | tail -10 || true

printf '\n== layer 3: local full-state backups ==\n'
find "$repo_root/backups" -maxdepth 4 \( -name "postgres-${db}.dump" -o -name "manifest.txt" -o -name "manifest.json" \) -printf '%TY-%Tm-%Td %TH:%TM %p\n' 2>/dev/null | sort | tail -20 || true

printf '\n== timers/services ==\n'
systemctl is-active dune-postgres-replication-forwarder.service dune-replica-snapshot.timer || true
systemctl list-timers dune-replica-snapshot.timer --no-pager || true
