#!/usr/bin/env bash
set -euo pipefail
umask 077
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/backup-state.sh [--dry-run] [env-file]

Creates a timestamped local backup under backups/.
Use --dry-run to report planned identity/config/TLS backup layers without
contacting Docker or writing a backup directory.
EOF
}

dry_run=false
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

env_file="${1:-.env}"
container_runtime="${CONTAINER_RUNTIME:-docker}"
compose=("$container_runtime" compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "")
      gsub(/^["'\'']|["'\'']$/, "")
      print
      exit
    }
  ' "$env_file" 2>/dev/null
}

world_unique_name="$(env_value WORLD_UNIQUE_NAME)"
dune_fls_env="$(env_value DUNE_FLS_ENV)"
game_rmq_public_host="$(env_value GAME_RMQ_PUBLIC_HOST)"
community_db="${DUNE_COMMUNITY_REWARDS_HOST_DATABASE:-backups/community-rewards/community.sqlite3}"
community_snapshot=""
[[ -f "$community_db" ]] && community_snapshot="community-rewards.sqlite3"
moderation_db="${DUNE_MODERATION_HOST_DATABASE:-backups/moderation/moderation.sqlite3}"
moderation_snapshot=""
[[ -f "$moderation_db" ]] && moderation_snapshot="moderation.sqlite3"
base_gallery_db="${DUNE_BASE_GALLERY_HOST_DATABASE:-backups/base-gallery/gallery.sqlite3}"
base_gallery_snapshot=""
[[ -f "$base_gallery_db" ]] && base_gallery_snapshot="base-gallery.sqlite3"
slo_db="${DUNE_OPERATIONAL_SLO_HOST_DATABASE:-backups/operational-slo/slo.sqlite3}"
slo_snapshot=""
[[ -f "$slo_db" ]] && slo_snapshot="operational-slo.sqlite3"
capacity_db="${DUNE_CAPACITY_INTELLIGENCE_HOST_DATABASE:-backups/capacity-intelligence/capacity.sqlite3}"
capacity_snapshot=""
[[ -f "$capacity_db" ]] && capacity_snapshot="capacity-intelligence.sqlite3"
desired_state_db="${DUNE_DESIRED_STATE_HOST_DATABASE:-backups/desired-state/desired-state.sqlite3}"
desired_state_snapshot=""
[[ -f "$desired_state_db" ]] && desired_state_snapshot="desired-state.sqlite3"
change_intelligence_db="${DUNE_CHANGE_INTELLIGENCE_HOST_DATABASE:-backups/change-intelligence/change-intelligence.sqlite3}"
change_intelligence_snapshot=""
[[ -f "$change_intelligence_db" ]] && change_intelligence_snapshot="change-intelligence.sqlite3"
feature_readiness_history_db="${DUNE_FEATURE_READINESS_HISTORY_HOST_DATABASE:-backups/feature-readiness/history.sqlite3}"
feature_readiness_history_snapshot=""
[[ -f "$feature_readiness_history_db" ]] && feature_readiness_history_snapshot="feature-readiness-history.sqlite3"
alert_inbox_db="${DUNE_ALERT_INBOX_HOST_DATABASE:-backups/alert-inbox/inbox.sqlite3}"
alert_inbox_snapshot=""
[[ -f "$alert_inbox_db" ]] && alert_inbox_snapshot="alert-inbox.sqlite3"
alert_inbox_enabled="$(env_value DUNE_ALERT_INBOX_ENABLED)"
case "${alert_inbox_enabled,,}" in
  ""|1|true|yes|on) alert_inbox_required=true ;;
  *) alert_inbox_required=false ;;
esac
if [[ "$alert_inbox_required" == true && -z "$alert_inbox_snapshot" && "$dry_run" == false ]]; then
  printf 'enabled alert inbox is unavailable to host backup: %s\n' "$alert_inbox_db" >&2
  exit 1
fi
peer_watch_db="${DUNE_PEER_WATCH_HOST_DATABASE:-backups/peer-watch/watch.sqlite3}"
peer_watch_snapshot=""
[[ -f "$peer_watch_db" ]] && peer_watch_snapshot="peer-watch.sqlite3"
peer_watch_enabled="$(env_value DUNE_PEER_WATCH_ENABLED)"
case "${peer_watch_enabled,,}" in
  ""|1|true|yes|on) peer_watch_required=true ;;
  *) peer_watch_required=false ;;
esac
if [[ "$peer_watch_required" == true && -z "$peer_watch_snapshot" && "$dry_run" == false ]]; then
  printf 'enabled peer watch is unavailable to host backup: %s\n' "$peer_watch_db" >&2
  exit 1
fi
canary_autopilot_state="${DUNE_CANARY_AUTOPILOT_HOST_STATE_FILE:-backups/admin-panel/canary-autopilot.json}"
canary_autopilot_snapshot=""
[[ -f "$canary_autopilot_state" ]] && canary_autopilot_snapshot="canary-autopilot.json"
credential_lifecycle_db="${DUNE_CREDENTIAL_LIFECYCLE_HOST_DATABASE:-backups/credential-lifecycle/history.sqlite3}"
credential_lifecycle_anchor="${DUNE_CREDENTIAL_LIFECYCLE_HOST_ANCHOR:-backups/credential-lifecycle/history.anchor.json}"
credential_lifecycle_key="${DUNE_CREDENTIAL_LIFECYCLE_HOST_HMAC_SECRET_FILE:-config/secrets/credential-lifecycle-hmac.secret}"
credential_lifecycle_artifacts=0
for artifact in "$credential_lifecycle_db" "$credential_lifecycle_anchor"; do
  [[ -f "$artifact" ]] && credential_lifecycle_artifacts=$((credential_lifecycle_artifacts + 1))
done
if [[ "$credential_lifecycle_artifacts" -eq 1 ]]; then
  printf 'credential lifecycle backup requires its SQLite ledger and authenticated head together\n' >&2
  exit 1
fi
credential_lifecycle_snapshot=""
credential_lifecycle_anchor_snapshot=""
if [[ "$credential_lifecycle_artifacts" -eq 2 ]]; then
  credential_lifecycle_snapshot="credential-lifecycle.sqlite3"
  credential_lifecycle_anchor_snapshot="credential-lifecycle.anchor.json"
fi
change_approval_db="${DUNE_ADMIN_DUAL_CONTROL_HOST_DATABASE:-backups/admin-panel/change-approvals.sqlite3}"
change_approval_key="${DUNE_ADMIN_DUAL_CONTROL_HOST_KEY:-backups/admin-panel/change-approvals.key}"
change_approval_artifacts=0
for artifact in "$change_approval_db" "$change_approval_key"; do
  [[ -f "$artifact" ]] && change_approval_artifacts=$((change_approval_artifacts + 1))
done
if [[ "$change_approval_artifacts" -eq 1 ]]; then
  printf 'change approval backup requires its SQLite ledger and HMAC key together\n' >&2
  exit 1
fi
change_approval_snapshot=""
change_approval_key_snapshot=""
if [[ "$change_approval_artifacts" -eq 2 ]]; then
  change_approval_snapshot="change-approvals.sqlite3"
  change_approval_key_snapshot="change-approvals.key"
fi
audit_ledger_db="${DUNE_ADMIN_AUDIT_LEDGER_HOST_DATABASE:-backups/admin-panel/audit-ledger.sqlite3}"
audit_ledger_key="${DUNE_ADMIN_AUDIT_LEDGER_HOST_KEY:-backups/admin-panel/audit-ledger.hmac.key}"
audit_ledger_anchor="${DUNE_ADMIN_AUDIT_LEDGER_HOST_ANCHOR:-backups/admin-panel/audit-ledger.anchor.json}"
audit_ledger_artifacts=0
for artifact in "$audit_ledger_db" "$audit_ledger_key" "$audit_ledger_anchor"; do
  [[ -f "$artifact" ]] && audit_ledger_artifacts=$((audit_ledger_artifacts + 1))
done
if [[ "$audit_ledger_artifacts" -ne 0 && "$audit_ledger_artifacts" -ne 3 ]]; then
  printf 'audit ledger backup requires database, HMAC key, and authenticated anchor together\n' >&2
  exit 1
fi
audit_ledger_snapshot=""
audit_ledger_key_snapshot=""
audit_ledger_anchor_snapshot=""
if [[ "$audit_ledger_artifacts" -eq 3 ]]; then
  audit_ledger_snapshot="audit-ledger.sqlite3"
  audit_ledger_key_snapshot="audit-ledger.hmac.key"
  audit_ledger_anchor_snapshot="audit-ledger.anchor.json"
fi
operator_evidence_dir="${DUNE_CHANGE_INTELLIGENCE_HOST_EVIDENCE_DIR:-$(env_value DUNE_CHANGE_INTELLIGENCE_HOST_EVIDENCE_DIR)}"
operator_evidence_dir="${operator_evidence_dir:-backups/operator-evidence}"
operator_evidence_count=0
if [[ -d "$operator_evidence_dir" ]]; then
  operator_evidence_count="$(find "$operator_evidence_dir" -maxdepth 1 -type f -name '*.signed.json' -printf . | wc -c)"
fi
operator_evidence_archive=""
[[ "$operator_evidence_count" -gt 0 ]] && operator_evidence_archive="operator-evidence.tgz"
rabbitmq_restore_receipt_dir="${DUNE_RABBITMQ_RESTORE_DRILL_HOST_RECEIPT_DIR:-backups/admin-panel/rabbitmq-restore-drills}"
rabbitmq_restore_receipt_source=""
rabbitmq_restore_receipt_snapshot=""
if [[ -d "$rabbitmq_restore_receipt_dir" ]]; then
  rabbitmq_restore_receipt_source="$(PYTHONPATH="$repo_root/admin" python3 - "$rabbitmq_restore_receipt_dir" <<'PY'
import pathlib
import sys
import rabbitmq_restore_drill

root = pathlib.Path(sys.argv[1])
rows = rabbitmq_restore_drill.list_receipts(root, 100000)
if not rows:
    raise SystemExit(0)
history = rabbitmq_restore_drill.verify_history(root)
if not history.get("ok") or any(not row.get("receiptHashValid") or not row.get("receiptChainValid") for row in rows):
    raise SystemExit("RabbitMQ recovery receipt history is invalid")
path = root / f"{rows[0]['id']}.json"
print(path)
PY
)"
  [[ -z "$rabbitmq_restore_receipt_source" ]] || rabbitmq_restore_receipt_snapshot="rabbitmq-restore-drill.json"
fi
db="${DUNE_GAME_DB_NAME:-$(env_value DUNE_GAME_DB_NAME)}"
db="${db:-${DUNE_DATABASE:-$(env_value DUNE_DATABASE)}}"
db="${db:-${DUNE_DB_NAME:-$(env_value DUNE_DB_NAME)}}"
db="${db:-dune_sb_1_4_0_0}"

if [[ "$dry_run" == true ]]; then
  printf 'backup dry run OK\n'
  printf 'env_file=%s\n' "$env_file"
  printf 'env_copy=%s\n' "$(basename "$env_file")"
  printf 'config_archive=config.tgz\n'
  if [[ -f "$community_db" ]]; then
    printf 'community_rewards_snapshot=community-rewards.sqlite3\n'
  else
    printf 'community_rewards_snapshot=<missing %s>\n' "$community_db"
  fi
  if [[ -f "$moderation_db" ]]; then
    printf 'moderation_snapshot=moderation.sqlite3\n'
  else
    printf 'moderation_snapshot=<missing %s>\n' "$moderation_db"
  fi
  if [[ -f "$base_gallery_db" ]]; then
    printf 'base_gallery_snapshot=base-gallery.sqlite3\n'
  else
    printf 'base_gallery_snapshot=<missing %s>\n' "$base_gallery_db"
  fi
  if [[ -f "$slo_db" ]]; then
    printf 'operational_slo_snapshot=operational-slo.sqlite3\n'
  else
    printf 'operational_slo_snapshot=<missing %s>\n' "$slo_db"
  fi
  if [[ -f "$capacity_db" ]]; then
    printf 'capacity_intelligence_snapshot=capacity-intelligence.sqlite3\n'
  else
    printf 'capacity_intelligence_snapshot=<missing %s>\n' "$capacity_db"
  fi
  if [[ -f "$desired_state_db" ]]; then
    printf 'desired_state_snapshot=desired-state.sqlite3\n'
  else
    printf 'desired_state_snapshot=<missing %s>\n' "$desired_state_db"
  fi
  if [[ -f "$change_intelligence_db" ]]; then
    printf 'change_intelligence_snapshot=change-intelligence.sqlite3\n'
  else
    printf 'change_intelligence_snapshot=<missing %s>\n' "$change_intelligence_db"
  fi
  if [[ -f "$feature_readiness_history_db" ]]; then
    printf 'feature_readiness_history_snapshot=feature-readiness-history.sqlite3\n'
  else
    printf 'feature_readiness_history_snapshot=<missing %s>\n' "$feature_readiness_history_db"
  fi
  if [[ -f "$alert_inbox_db" ]]; then
    printf 'alert_inbox_snapshot=alert-inbox.sqlite3\n'
  elif [[ "$alert_inbox_required" == true ]]; then
    printf 'alert_inbox_snapshot=<required but unavailable %s>\n' "$alert_inbox_db"
  else
    printf 'alert_inbox_snapshot=<not initialized>\n'
  fi
  if [[ -f "$peer_watch_db" ]]; then
    printf 'peer_watch_snapshot=peer-watch.sqlite3\n'
  elif [[ "$peer_watch_required" == true ]]; then
    printf 'peer_watch_snapshot=<required but unavailable %s>\n' "$peer_watch_db"
  else
    printf 'peer_watch_snapshot=<not initialized>\n'
  fi
  if [[ -f "$canary_autopilot_state" ]]; then
    printf 'canary_autopilot_snapshot=canary-autopilot.json\n'
  else
    printf 'canary_autopilot_snapshot=<not initialized>\n'
  fi
  if [[ "$credential_lifecycle_artifacts" -eq 2 ]]; then
    printf 'credential_lifecycle_snapshot=credential-lifecycle.sqlite3\n'
    printf 'credential_lifecycle_anchor=credential-lifecycle.anchor.json\n'
  else
    printf 'credential_lifecycle_snapshot=<not initialized>\n'
  fi
  if [[ "$change_approval_artifacts" -eq 2 ]]; then
    printf 'change_approval_snapshot=change-approvals.sqlite3\n'
    printf 'change_approval_key=change-approvals.key\n'
  else
    printf 'change_approval_snapshot=<not initialized>\n'
  fi
  if [[ "$audit_ledger_artifacts" -eq 3 ]]; then
    printf 'audit_ledger_snapshot=audit-ledger.sqlite3\n'
    printf 'audit_ledger_key=audit-ledger.hmac.key\n'
    printf 'audit_ledger_anchor=audit-ledger.anchor.json\n'
  else
    printf 'audit_ledger_snapshot=<not initialized>\n'
  fi
  if [[ "$operator_evidence_count" -gt 0 ]]; then
    printf 'operator_evidence_archive=operator-evidence.tgz\n'
    printf 'operator_evidence_files=%s\n' "$operator_evidence_count"
  else
    printf 'operator_evidence_archive=<no signed capsules under %s>\n' "$operator_evidence_dir"
  fi
  if [[ -n "$rabbitmq_restore_receipt_source" ]]; then
    printf 'rabbitmq_restore_receipt=rabbitmq-restore-drill.json\n'
  else
    printf 'rabbitmq_restore_receipt=<no receipt under %s>\n' "$rabbitmq_restore_receipt_dir"
  fi
  if [[ -d config/tls ]]; then
    printf 'config_tls_archive=config-tls.tgz\n'
  else
    printf 'config_tls_archive=<missing config/tls>\n'
  fi
  printf 'world_unique_name=%s\n' "${world_unique_name:-}"
  printf 'dune_fls_env=%s\n' "${dune_fls_env:-retail}"
  printf 'game_rmq_public_host=%s\n' "${game_rmq_public_host:-}"
  exit 0
fi

if ! command -v "$container_runtime" >/dev/null 2>&1; then
  printf '%s is required\n' "$container_runtime" >&2
  exit 1
fi

if [[ "${DUNE_OPERATION_LOCK_HELD:-false}" != true ]]; then
  if ! command -v flock >/dev/null 2>&1; then
    printf 'flock is required to serialize backups with assured deployments\n' >&2
    exit 1
  fi
  operation_lock="${DUNE_OPERATION_LOCK_FILE:-backups/admin-panel/operation.lock}"
  operation_lock_wait="${DUNE_OPERATION_LOCK_WAIT_SECONDS:-$(env_value DUNE_OPERATION_LOCK_WAIT_SECONDS)}"
  operation_lock_wait="${operation_lock_wait:-1800}"
  [[ "$operation_lock_wait" =~ ^[0-9]+$ ]] || {
    printf 'DUNE_OPERATION_LOCK_WAIT_SECONDS must be a non-negative integer\n' >&2
    exit 1
  }
  mkdir -p "$(dirname -- "$operation_lock")"
  exec 8>"$operation_lock"
  if ! flock -w "$operation_lock_wait" 8; then
    printf 'timed out waiting for the shared backup/deployment operation lock: %s\n' "$operation_lock" >&2
    exit 1
  fi
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="backups/${timestamp}"

case "$backup_dir" in
  backups/*) ;;
  *)
    printf 'refusing to write backup outside ignored backups/: %s\n' "$backup_dir" >&2
    exit 1
    ;;
esac

mkdir -p "$backup_dir"

printf 'writing backup: %s\n' "$backup_dir"

env_base="$(basename "$env_file")"
cp "$env_file" "${backup_dir}/${env_base}"
config_tar_options=(--exclude='config/tls' --exclude='config/tls/**')
if [[ -L config/secrets ]]; then
  # Secret sources are deliberately kept outside the workspace, but a full
  # recovery backup must carry the HMAC keys needed to verify its signed state.
  # Validate the protected links and refuse to dereference any other config
  # symlink so the archive cannot unexpectedly walk outside the project.
  "$script_dir/protect-compose-secrets.sh" "$repo_root" >/dev/null
  config_symlinks="$(find config -type l -print)"
  if [[ "$config_symlinks" != "config/secrets" ]]; then
    printf 'refusing to dereference unexpected config symlink(s): %s\n' "$config_symlinks" >&2
    exit 1
  fi
  config_tar_options+=(--dereference)
elif [[ ! -d config/secrets ]]; then
  printf 'config secrets directory is unavailable: config/secrets\n' >&2
  exit 1
fi
tar "${config_tar_options[@]}" -czf "${backup_dir}/config.tgz" config
chmod 0600 "${backup_dir}/config.tgz"
if [[ -d config/tls ]]; then
  tar -czf "${backup_dir}/config-tls.tgz" config/tls
fi

if [[ -f "$community_db" ]]; then
  python3 - "$community_db" "${backup_dir}/community-rewards.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0] != "ok":
        raise SystemExit("community rewards snapshot failed integrity_check")
finally:
    target.close()
    source.close()
PY
  chmod 600 "${backup_dir}/community-rewards.sqlite3"
fi

if [[ -f "$moderation_db" ]]; then
  python3 - "$moderation_db" "${backup_dir}/moderation.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0] != "ok":
        raise SystemExit("moderation snapshot failed integrity_check")
finally:
    target.close()
    source.close()
PY
  chmod 600 "${backup_dir}/moderation.sqlite3"
fi

if [[ -f "$base_gallery_db" ]]; then
  python3 - "$base_gallery_db" "${backup_dir}/base-gallery.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("base gallery snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/base-gallery.sqlite3"
fi

if [[ -f "$slo_db" ]]; then
  python3 - "$slo_db" "${backup_dir}/operational-slo.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("operational SLO snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/operational-slo.sqlite3"
fi

if [[ -f "$capacity_db" ]]; then
  python3 - "$capacity_db" "${backup_dir}/capacity-intelligence.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("capacity intelligence snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/capacity-intelligence.sqlite3"
fi

if [[ -f "$desired_state_db" ]]; then
  python3 - "$desired_state_db" "${backup_dir}/desired-state.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("desired-state snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/desired-state.sqlite3"
fi

if [[ -f "$change_intelligence_db" ]]; then
  python3 - "$change_intelligence_db" "${backup_dir}/change-intelligence.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("change-intelligence snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/change-intelligence.sqlite3"
fi

if [[ -f "$feature_readiness_history_db" ]]; then
  python3 - "$feature_readiness_history_db" "${backup_dir}/feature-readiness-history.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("feature-readiness history snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/feature-readiness-history.sqlite3"
fi

if [[ -f "$alert_inbox_db" ]]; then
  python3 - "$alert_inbox_db" "${backup_dir}/alert-inbox.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("alert inbox snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/alert-inbox.sqlite3"
fi

if [[ -f "$peer_watch_db" ]]; then
  python3 - "$peer_watch_db" "${backup_dir}/peer-watch.sqlite3" <<'PY'
import sqlite3
import sys
source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
target=sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("peer-watch snapshot failed integrity_check")
finally:
    target.close(); source.close()
PY
  chmod 600 "${backup_dir}/peer-watch.sqlite3"
fi

if [[ -f "$canary_autopilot_state" ]]; then
  PYTHONPATH="$repo_root/admin" python3 - "$canary_autopilot_state" "${backup_dir}/canary-autopilot.json" <<'PY'
import json
import os
import pathlib
import sys
import canary_autopilot
source, target = map(pathlib.Path, sys.argv[1:])
if source.is_symlink() or not source.is_file() or not 1 <= source.stat().st_size <= 4 * 1024 * 1024:
    raise SystemExit("canary autopilot source state is invalid")
raw = source.read_bytes()
canary_autopilot.validate_state(json.loads(raw.decode("utf-8")))
target.write_bytes(raw)
os.chmod(target, 0o600)
PY
fi

if [[ "$credential_lifecycle_artifacts" -eq 2 ]]; then
  PYTHONPATH=admin python3 - "$credential_lifecycle_db" "$credential_lifecycle_anchor" "$credential_lifecycle_key" "${backup_dir}/credential-lifecycle.sqlite3" "${backup_dir}/credential-lifecycle.anchor.json" <<'PY'
import pathlib
import shutil
import sqlite3
import sys
import time
import credential_lifecycle
source_db, source_anchor, key, target_db, target_anchor = map(pathlib.Path, sys.argv[1:])
for attempt in range(5):
    target_db.unlink(missing_ok=True)
    source=sqlite3.connect(f"file:{source_db}?mode=ro",uri=True)
    target=sqlite3.connect(target_db)
    try:
        source.backup(target)
        if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise RuntimeError("credential lifecycle snapshot failed integrity_check")
    finally:
        target.close(); source.close()
    shutil.copyfile(source_anchor, target_anchor)
    target_db.chmod(0o600); target_anchor.chmod(0o600)
    try:
        credential_lifecycle.verify_database(target_db, key, target_anchor)
        break
    except (OSError, ValueError, sqlite3.Error):
        if attempt == 4: raise
        time.sleep(0.05)
PY
  chmod 600 "${backup_dir}/credential-lifecycle.sqlite3" "${backup_dir}/credential-lifecycle.anchor.json"
fi

if [[ "$change_approval_artifacts" -eq 2 ]]; then
  python3 - "$change_approval_db" "$change_approval_key" "${backup_dir}/change-approvals.sqlite3" "${backup_dir}/change-approvals.key" <<'PY'
import pathlib
import shutil
import sqlite3
import sys
source_db, source_key, target_db, target_key = map(pathlib.Path, sys.argv[1:])
source=sqlite3.connect(f"file:{source_db}?mode=ro",uri=True)
target=sqlite3.connect(target_db)
try:
    source.backup(target)
    if target.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit("change approval snapshot failed integrity_check")
finally:
    target.close(); source.close()
shutil.copyfile(source_key, target_key)
PY
  chmod 600 "${backup_dir}/change-approvals.sqlite3" "${backup_dir}/change-approvals.key"
fi

if [[ "$audit_ledger_artifacts" -eq 3 ]]; then
  python3 scripts/snapshot-audit-ledger.py \
    "$audit_ledger_db" "$audit_ledger_key" "$audit_ledger_anchor" "$backup_dir"
fi

if [[ "$operator_evidence_count" -gt 0 ]]; then
  python3 - "$operator_evidence_dir" "${backup_dir}/operator-evidence.tgz" <<'PY'
import pathlib
import re
import tarfile
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
files = sorted(path for path in source.glob("*.signed.json") if path.is_file() and not path.is_symlink())
if not 1 <= len(files) <= 1000:
    raise SystemExit("operator evidence backup requires 1..1000 regular signed capsules")
total = 0
with tarfile.open(target, "w:gz") as archive:
    for path in files:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.signed\.json", path.name):
            raise SystemExit(f"operator evidence capsule has unsafe backup name: {path.name}")
        size = path.stat().st_size
        if not 1 <= size <= 10 * 1024 * 1024:
            raise SystemExit(f"operator evidence capsule has invalid size: {path}")
        total += size
        if total > 100 * 1024 * 1024:
            raise SystemExit("operator evidence capsules exceed the 100 MiB backup bound")
        archive.add(path, arcname=f"operator-evidence/{path.name}", recursive=False)
PY
  chmod 600 "${backup_dir}/operator-evidence.tgz"
fi

if [[ -n "$rabbitmq_restore_receipt_source" ]]; then
  install -m 600 "$rabbitmq_restore_receipt_source" "${backup_dir}/rabbitmq-restore-drill.json"
fi

"${compose[@]}" exec -T postgres \
  pg_dump -U dune -d "$db" -Fc \
  > "${backup_dir}/postgres-${db}.dump"

if "${compose[@]}" ps --services --filter status=running | grep -qx admin-rmq; then
  "${compose[@]}" exec -T admin-rmq tar -czf - -C /var/lib/rabbitmq . \
    > "${backup_dir}/rabbitmq-admin.tgz"
fi

if "${compose[@]}" ps --services --filter status=running | grep -qx game-rmq; then
  "${compose[@]}" exec -T game-rmq tar -czf - -C /var/lib/rabbitmq . \
    > "${backup_dir}/rabbitmq-game.tgz"
fi

if "${compose[@]}" ps --services --filter status=running | grep -qx survival; then
  "${compose[@]}" exec -T survival tar -czf - -C /home/dune/server/DuneSandbox/Saved . \
    > "${backup_dir}/server-saved.tgz"
elif [[ -d data/server-saved ]]; then
  tar -czf "${backup_dir}/server-saved.tgz" data/server-saved
fi

cat > "${backup_dir}/manifest.txt" <<EOF
created_utc=${timestamp}
env_file=${env_file}
env_archive=${env_base}
container_runtime=${container_runtime}
compose_files=${COMPOSE_FILES:-compose.yaml}
database=${db}
postgres_dump=postgres-${db}.dump
rabbitmq_admin_archive=rabbitmq-admin.tgz
rabbitmq_game_archive=rabbitmq-game.tgz
server_saved_archive=server-saved.tgz
config_archive=config.tgz
config_tls_archive=config-tls.tgz
community_rewards_snapshot=${community_snapshot}
moderation_snapshot=${moderation_snapshot}
base_gallery_snapshot=${base_gallery_snapshot}
operational_slo_snapshot=${slo_snapshot}
capacity_intelligence_snapshot=${capacity_snapshot}
desired_state_snapshot=${desired_state_snapshot}
change_intelligence_snapshot=${change_intelligence_snapshot}
feature_readiness_history_snapshot=${feature_readiness_history_snapshot}
alert_inbox_snapshot=${alert_inbox_snapshot}
alert_inbox_required=${alert_inbox_required}
peer_watch_snapshot=${peer_watch_snapshot}
peer_watch_required=${peer_watch_required}
canary_autopilot_snapshot=${canary_autopilot_snapshot}
credential_lifecycle_snapshot=${credential_lifecycle_snapshot}
credential_lifecycle_anchor=${credential_lifecycle_anchor_snapshot}
change_approval_snapshot=${change_approval_snapshot}
change_approval_key=${change_approval_key_snapshot}
audit_ledger_snapshot=${audit_ledger_snapshot}
audit_ledger_key=${audit_ledger_key_snapshot}
audit_ledger_anchor=${audit_ledger_anchor_snapshot}
operator_evidence_archive=${operator_evidence_archive}
operator_evidence_files=${operator_evidence_count}
rabbitmq_restore_receipt=${rabbitmq_restore_receipt_snapshot}
world_unique_name=${world_unique_name}
dune_fls_env=${dune_fls_env:-retail}
game_rmq_public_host=${game_rmq_public_host}
EOF

printf 'backup complete: %s\n' "$backup_dir"
