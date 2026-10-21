#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-}}"
remote_root="${3:-${POSTGRES_REMOTE_REPLICA_ROOT:-/srv/dune-postgres-replica}}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service_path="${4:-/etc/systemd/system/dune-replica-snapshot.service}"
timer_path="${5:-/etc/systemd/system/dune-replica-snapshot.timer}"
run_user="${DUNE_REPLICA_SNAPSHOT_USER:-$(id -un)}"
run_group="${DUNE_REPLICA_SNAPSHOT_GROUP:-$(id -gn)}"

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

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi
snapshot_root="${DUNE_REPLICA_SNAPSHOT_ROOT:-$(read_env DUNE_REPLICA_SNAPSHOT_ROOT)}"
snapshot_root="${snapshot_root:-${repo_root}/backups/replica-snapshots}"

tmp_service="$(mktemp)"
tmp_timer="$(mktemp)"
exec_start="${repo_root}/scripts/replica-snapshot.sh ${repo_root}/${env_file}"
if [[ -n "$remote" ]]; then
  exec_start+=" ${remote} ${remote_root}"
fi
sed \
  -e "s#^User=.*#User=${run_user}#" \
  -e "s#^Group=.*#Group=${run_group}#" \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=${repo_root}#" \
  -e "s#^RequiresMountsFor=.*#RequiresMountsFor=${snapshot_root}#" \
  -e "s#^Environment=DUNE_REPLICA_SNAPSHOT_ROOT=.*#Environment=DUNE_REPLICA_SNAPSHOT_ROOT=${snapshot_root}#" \
  -e "s#ExecStart=.*#ExecStart=${exec_start}#" \
  "$repo_root/config/systemd/dune-replica-snapshot.service" > "$tmp_service"
cp "$repo_root/config/systemd/dune-replica-snapshot.timer" "$tmp_timer"

sudo install -m 0644 "$tmp_service" "$service_path"
sudo install -m 0644 "$tmp_timer" "$timer_path"
rm -f "$tmp_service" "$tmp_timer"

sudo systemctl daemon-reload
sudo systemctl enable --now "$(basename "$timer_path")"
sudo systemctl reset-failed "$(basename "$service_path")" || true
sudo systemctl list-timers "$(basename "$timer_path")" --no-pager
