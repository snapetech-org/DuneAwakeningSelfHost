#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/restore-state.sh [--dry-run] [--rabbitmq] [--server-saved] [--config] [--tls] [--community-rewards] [--moderation] [--base-gallery] [--operational-slo] [--capacity-intelligence] [--desired-state] [--change-intelligence] [--alert-inbox] [--peer-watch] [--credential-lifecycle] [--change-approvals] [--audit-ledger] [env-file] <backup-dir>

Restores the Postgres dump from a backup created by scripts/backup-state.sh.
RabbitMQ and server saved-state archives are restored only when their flags are
provided because they replace local data directories.
Config and TLS archives are also opt-in because they can replace the current
world identity, secrets, local tuning, and RabbitMQ certificate material.

Examples:
  ./scripts/restore-state.sh --dry-run .env backups/20260519T150000Z
  ./scripts/restore-state.sh .env backups/20260519T150000Z
  ./scripts/restore-state.sh --rabbitmq --server-saved --config --tls --community-rewards --moderation --base-gallery --operational-slo --capacity-intelligence --desired-state --change-intelligence --alert-inbox --peer-watch --credential-lifecycle --change-approvals --audit-ledger .env backups/20260519T150000Z
EOF
}

dry_run=false
restore_rabbitmq=false
restore_server_saved=false
restore_config=false
restore_tls=false
restore_community_rewards=false
restore_moderation=false
restore_base_gallery=false
restore_operational_slo=false
restore_capacity_intelligence=false
restore_desired_state=false
restore_change_intelligence=false
restore_alert_inbox=false
restore_peer_watch=false
restore_credential_lifecycle=false
restore_change_approvals=false
restore_audit_ledger=false

while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --rabbitmq)
      restore_rabbitmq=true
      shift
      ;;
    --server-saved)
      restore_server_saved=true
      shift
      ;;
    --config)
      restore_config=true
      shift
      ;;
    --tls)
      restore_tls=true
      shift
      ;;
    --community-rewards)
      restore_community_rewards=true
      shift
      ;;
    --moderation)
      restore_moderation=true
      shift
      ;;
    --base-gallery)
      restore_base_gallery=true
      shift
      ;;
    --operational-slo)
      restore_operational_slo=true
      shift
      ;;
    --capacity-intelligence)
      restore_capacity_intelligence=true
      shift
      ;;
    --desired-state)
      restore_desired_state=true
      shift
      ;;
    --change-intelligence)
      restore_change_intelligence=true
      shift
      ;;
    --alert-inbox)
      restore_alert_inbox=true
      shift
      ;;
    --peer-watch)
      restore_peer_watch=true
      shift
      ;;
    --credential-lifecycle)
      restore_credential_lifecycle=true
      shift
      ;;
    --change-approvals)
      restore_change_approvals=true
      shift
      ;;
    --audit-ledger)
      restore_audit_ledger=true
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

env_file=".env"
backup_dir="${1:-}"

if [[ "${2:-}" != "" ]]; then
  env_file="$1"
  backup_dir="$2"
fi

if [[ -z "$backup_dir" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

if [[ ! -d "$backup_dir" ]]; then
  printf 'backup dir not found: %s\n' "$backup_dir" >&2
  exit 1
fi

case "$backup_dir" in
  backups/*) ;;
  *)
    printf 'refusing to restore from outside ignored backups/: %s\n' "$backup_dir" >&2
    exit 1
    ;;
esac

container_runtime="${CONTAINER_RUNTIME:-docker}"
if ! command -v "$container_runtime" >/dev/null 2>&1; then
  printf '%s is required\n' "$container_runtime" >&2
  exit 1
fi

db=dune_sb_1_4_0_0
dump_file="${backup_dir}/postgres-${db}.dump"
compose=("$container_runtime" compose --env-file "$env_file")

manifest_value() {
  local key="$1"
  [[ -f "${backup_dir}/manifest.txt" ]] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${backup_dir}/manifest.txt"
}

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

if [[ ! -f "$dump_file" ]]; then
  printf 'postgres dump not found: %s\n' "$dump_file" >&2
  exit 1
fi

if [[ "$restore_rabbitmq" == true ]]; then
  for archive in "${backup_dir}/rabbitmq-admin.tgz" "${backup_dir}/rabbitmq-game.tgz"; do
    if [[ ! -f "$archive" ]]; then
      printf 'rabbitmq archive not found: %s\n' "$archive" >&2
      exit 1
    fi
  done
fi

if [[ "$restore_server_saved" == true ]]; then
  archive="${backup_dir}/server-saved.tgz"
  if [[ ! -f "$archive" ]]; then
    printf 'server saved archive not found: %s\n' "$archive" >&2
    exit 1
  fi
fi

if [[ "$restore_config" == true && ! -f "${backup_dir}/config.tgz" ]]; then
  printf 'config archive not found: %s\n' "${backup_dir}/config.tgz" >&2
  exit 1
fi

if [[ "$restore_tls" == true && ! -f "${backup_dir}/config-tls.tgz" ]]; then
  printf 'TLS archive not found: %s\n' "${backup_dir}/config-tls.tgz" >&2
  exit 1
fi

if [[ "$restore_community_rewards" == true && ! -f "${backup_dir}/community-rewards.sqlite3" ]]; then
  printf 'community rewards snapshot not found: %s\n' "${backup_dir}/community-rewards.sqlite3" >&2
  exit 1
fi

if [[ "$restore_moderation" == true && ! -f "${backup_dir}/moderation.sqlite3" ]]; then
  printf 'moderation snapshot not found: %s\n' "${backup_dir}/moderation.sqlite3" >&2
  exit 1
fi
if [[ "$restore_base_gallery" == true && ! -f "${backup_dir}/base-gallery.sqlite3" ]]; then
  printf 'base gallery snapshot not found: %s\n' "${backup_dir}/base-gallery.sqlite3" >&2
  exit 1
fi
if [[ "$restore_operational_slo" == true && ! -f "${backup_dir}/operational-slo.sqlite3" ]]; then
  printf 'operational SLO snapshot not found: %s\n' "${backup_dir}/operational-slo.sqlite3" >&2
  exit 1
fi
if [[ "$restore_capacity_intelligence" == true && ! -f "${backup_dir}/capacity-intelligence.sqlite3" ]]; then
  printf 'capacity intelligence snapshot not found: %s\n' "${backup_dir}/capacity-intelligence.sqlite3" >&2
  exit 1
fi
if [[ "$restore_desired_state" == true && ! -f "${backup_dir}/desired-state.sqlite3" ]]; then
  printf 'desired-state snapshot not found: %s\n' "${backup_dir}/desired-state.sqlite3" >&2
  exit 1
fi
if [[ "$restore_change_intelligence" == true && ! -f "${backup_dir}/change-intelligence.sqlite3" ]]; then
  printf 'change-intelligence snapshot not found: %s\n' "${backup_dir}/change-intelligence.sqlite3" >&2
  exit 1
fi
if [[ "$restore_alert_inbox" == true ]]; then
  if [[ ! -f "${backup_dir}/alert-inbox.sqlite3" ]]; then
    printf 'alert-inbox snapshot not found: %s\n' "${backup_dir}/alert-inbox.sqlite3" >&2
    exit 1
  fi
  PYTHONPATH=admin python3 - "${backup_dir}/alert-inbox.sqlite3" <<'PY'
import pathlib
import sqlite3
import sys
import alert_inbox
path=pathlib.Path(sys.argv[1])
db=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
try:
    integrity=db.execute("pragma integrity_check").fetchone()[0]
    tables={row[0] for row in db.execute("select name from sqlite_master where type='table'")}
finally:
    db.close()
if integrity != "ok" or not {"alerts","transitions","metadata"} <= tables or alert_inbox.SCHEMA != "dash-alert-inbox/v1":
    raise SystemExit("alert-inbox snapshot verification failed")
PY
fi
if [[ "$restore_peer_watch" == true ]]; then
  if [[ ! -f "${backup_dir}/peer-watch.sqlite3" ]]; then
    printf 'peer-watch snapshot not found: %s\n' "${backup_dir}/peer-watch.sqlite3" >&2
    exit 1
  fi
  PYTHONPATH=admin python3 - "${backup_dir}/peer-watch.sqlite3" <<'PY'
import pathlib
import sys
import peer_watch
path=pathlib.Path(sys.argv[1])
if not peer_watch.verify_database(path).get("ok"):
    raise SystemExit("peer-watch snapshot verification failed")
PY
fi
if [[ "$restore_credential_lifecycle" == true ]]; then
  for artifact in credential-lifecycle.sqlite3 credential-lifecycle.anchor.json; do
    if [[ ! -f "${backup_dir}/${artifact}" ]]; then
      printf 'credential-lifecycle backup artifact not found: %s\n' "${backup_dir}/${artifact}" >&2
      exit 1
    fi
  done
fi
if [[ "$restore_change_approvals" == true ]]; then
  for artifact in change-approvals.sqlite3 change-approvals.key; do
    if [[ ! -f "${backup_dir}/${artifact}" ]]; then
      printf 'change-approval backup artifact not found: %s\n' "${backup_dir}/${artifact}" >&2
      exit 1
    fi
  done
  PYTHONPATH=admin python3 - "${backup_dir}/change-approvals.sqlite3" "${backup_dir}/change-approvals.key" <<'PY'
import sys
import change_approvals
result = change_approvals.Store(sys.argv[1], key_path=sys.argv[2]).verify()
if not result.get("ok"):
    raise SystemExit(f"change-approval HMAC verification failed: {result}")
PY
fi
if [[ "$restore_audit_ledger" == true ]]; then
  for artifact in audit-ledger.sqlite3 audit-ledger.hmac.key audit-ledger.anchor.json; do
    if [[ ! -f "${backup_dir}/${artifact}" ]]; then
      printf 'audit ledger backup artifact not found: %s\n' "${backup_dir}/${artifact}" >&2
      exit 1
    fi
  done
  PYTHONPATH=admin python3 - "${backup_dir}/audit-ledger.sqlite3" "${backup_dir}/audit-ledger.hmac.key" "${backup_dir}/audit-ledger.anchor.json" <<'PY'
import sys
import audit_ledger
result = audit_ledger.Store(sys.argv[1], key_path=sys.argv[2], anchor_path=sys.argv[3]).verify()
if not result.get("ok"):
    raise SystemExit(f"audit ledger HMAC/head verification failed: {result}")
PY
fi

if [[ "$restore_desired_state" == true ]]; then
  desired_policy="config/desired-state.json"
  desired_secret="config/secrets/desired-state-hmac.secret"
  desired_verify_tmp=""
  if [[ "$restore_config" == true ]]; then
    desired_verify_tmp="$(mktemp -d)"
    trap 'rm -rf "${desired_verify_tmp:-}"' EXIT
    mkdir -p "$desired_verify_tmp/config/secrets"
    tar -xOf "${backup_dir}/config.tgz" config/desired-state.json > "$desired_verify_tmp/config/desired-state.json"
    tar -xOf "${backup_dir}/config.tgz" config/secrets/desired-state-hmac.secret > "$desired_verify_tmp/config/secrets/desired-state-hmac.secret"
    chmod 600 "$desired_verify_tmp/config/secrets/desired-state-hmac.secret"
    desired_policy="$desired_verify_tmp/config/desired-state.json"
    desired_secret="$desired_verify_tmp/config/secrets/desired-state-hmac.secret"
  fi
  PYTHONPATH=admin python3 - "${backup_dir}/desired-state.sqlite3" "$desired_policy" "$desired_secret" <<'PY'
import sys
import desired_state
result=desired_state.Store(sys.argv[1],sys.argv[2],sys.argv[3]).verify()
if not result.get("ok"):
    raise SystemExit(f"desired-state HMAC verification failed: {result}")
PY
fi

if [[ "$restore_change_intelligence" == true ]]; then
  change_policy="config/change-intelligence.json"
  change_secret="config/secrets/change-intelligence-hmac.secret"
  change_verify_tmp=""
  if [[ "$restore_config" == true ]]; then
    change_verify_tmp="$(mktemp -d)"
    trap 'rm -rf "${desired_verify_tmp:-}" "${change_verify_tmp:-}"' EXIT
    mkdir -p "$change_verify_tmp/config/secrets"
    tar -xOf "${backup_dir}/config.tgz" config/change-intelligence.json > "$change_verify_tmp/config/change-intelligence.json"
    tar -xOf "${backup_dir}/config.tgz" config/secrets/change-intelligence-hmac.secret > "$change_verify_tmp/config/secrets/change-intelligence-hmac.secret"
    chmod 600 "$change_verify_tmp/config/secrets/change-intelligence-hmac.secret"
    change_policy="$change_verify_tmp/config/change-intelligence.json"
    change_secret="$change_verify_tmp/config/secrets/change-intelligence-hmac.secret"
  fi
  PYTHONPATH=admin python3 - "${backup_dir}/change-intelligence.sqlite3" "$change_policy" "$change_secret" <<'PY'
import sys
import change_intelligence
result=change_intelligence.Store(sys.argv[1],sys.argv[2],sys.argv[3]).verify()
if not result.get("ok"):
    raise SystemExit(f"change-intelligence HMAC verification failed: {result}")
PY
fi

if [[ "$restore_credential_lifecycle" == true ]]; then
  credential_secret="config/secrets/credential-lifecycle-hmac.secret"
  credential_verify_tmp=""
  if [[ "$restore_config" == true ]]; then
    credential_verify_tmp="$(mktemp -d)"
    trap 'rm -rf "${desired_verify_tmp:-}" "${change_verify_tmp:-}" "${credential_verify_tmp:-}"' EXIT
    tar -xOf "${backup_dir}/config.tgz" config/secrets/credential-lifecycle-hmac.secret > "$credential_verify_tmp/credential-lifecycle-hmac.secret"
    chmod 600 "$credential_verify_tmp/credential-lifecycle-hmac.secret"
    credential_secret="$credential_verify_tmp/credential-lifecycle-hmac.secret"
  fi
  PYTHONPATH=admin python3 - "${backup_dir}/credential-lifecycle.sqlite3" "$credential_secret" "${backup_dir}/credential-lifecycle.anchor.json" <<'PY'
import sys
import credential_lifecycle
result=credential_lifecycle.verify_database(sys.argv[1],sys.argv[2],sys.argv[3])
if not result.get("ok"):
    raise SystemExit(f"credential lifecycle HMAC/head verification failed: {result}")
PY
fi

backup_world="$(manifest_value world_unique_name)"
current_world="$(env_value WORLD_UNIQUE_NAME)"
if [[ -n "$backup_world" && -n "$current_world" && "$backup_world" != "$current_world" ]]; then
  printf 'warn: backup WORLD_UNIQUE_NAME=%s differs from current %s\n' "$backup_world" "$current_world" >&2
  printf 'warn: restoring this backup under a different world identity can change FLS battlegroup behavior\n' >&2
fi

if [[ "$dry_run" == true ]]; then
  printf 'restore dry run OK\n'
  printf 'env_file=%s\n' "$env_file"
  printf 'backup_dir=%s\n' "$backup_dir"
  printf 'postgres_dump=%s\n' "$dump_file"
  printf 'restore_rabbitmq=%s\n' "$restore_rabbitmq"
  printf 'restore_server_saved=%s\n' "$restore_server_saved"
  printf 'restore_config=%s\n' "$restore_config"
  printf 'restore_tls=%s\n' "$restore_tls"
  printf 'restore_community_rewards=%s\n' "$restore_community_rewards"
  printf 'restore_moderation=%s\n' "$restore_moderation"
  printf 'restore_base_gallery=%s\n' "$restore_base_gallery"
  printf 'restore_operational_slo=%s\n' "$restore_operational_slo"
  printf 'restore_capacity_intelligence=%s\n' "$restore_capacity_intelligence"
  printf 'restore_desired_state=%s\n' "$restore_desired_state"
  printf 'restore_change_intelligence=%s\n' "$restore_change_intelligence"
  printf 'restore_alert_inbox=%s\n' "$restore_alert_inbox"
  printf 'restore_credential_lifecycle=%s\n' "$restore_credential_lifecycle"
  printf 'restore_change_approvals=%s\n' "$restore_change_approvals"
  printf 'restore_audit_ledger=%s\n' "$restore_audit_ledger"
  printf 'backup_world_unique_name=%s\n' "${backup_world:-}"
  printf 'current_world_unique_name=%s\n' "${current_world:-}"
  exit 0
fi

printf 'stopping write services\n'
"${compose[@]}" stop survival director gateway text-router rmq-auth-shim admin-rmq game-rmq admin-panel || true

if [[ "$restore_community_rewards" == true ]]; then
  printf 'restoring isolated community rewards state from %s\n' "${backup_dir}/community-rewards.sqlite3"
  mkdir -p backups/community-rewards
  rm -f backups/community-rewards/community.sqlite3-wal backups/community-rewards/community.sqlite3-shm
  install -m 600 "${backup_dir}/community-rewards.sqlite3" backups/community-rewards/community.sqlite3
fi

if [[ "$restore_moderation" == true ]]; then
  printf 'restoring isolated moderation state from %s\n' "${backup_dir}/moderation.sqlite3"
  mkdir -p backups/moderation
  rm -f backups/moderation/moderation.sqlite3-wal backups/moderation/moderation.sqlite3-shm
  install -m 600 "${backup_dir}/moderation.sqlite3" backups/moderation/moderation.sqlite3
fi
if [[ "$restore_base_gallery" == true ]]; then
  printf 'restoring isolated base gallery state from %s\n' "${backup_dir}/base-gallery.sqlite3"
  mkdir -p backups/base-gallery
  rm -f backups/base-gallery/gallery.sqlite3-wal backups/base-gallery/gallery.sqlite3-shm
  install -m 600 "${backup_dir}/base-gallery.sqlite3" backups/base-gallery/gallery.sqlite3
fi
if [[ "$restore_operational_slo" == true ]]; then
  printf 'restoring isolated operational SLO state from %s\n' "${backup_dir}/operational-slo.sqlite3"
  mkdir -p backups/operational-slo
  rm -f backups/operational-slo/slo.sqlite3-wal backups/operational-slo/slo.sqlite3-shm
  install -m 600 "${backup_dir}/operational-slo.sqlite3" backups/operational-slo/slo.sqlite3
fi
if [[ "$restore_capacity_intelligence" == true ]]; then
  printf 'restoring isolated capacity intelligence state from %s\n' "${backup_dir}/capacity-intelligence.sqlite3"
  mkdir -p backups/capacity-intelligence
  rm -f backups/capacity-intelligence/capacity.sqlite3-wal backups/capacity-intelligence/capacity.sqlite3-shm
  install -m 600 "${backup_dir}/capacity-intelligence.sqlite3" backups/capacity-intelligence/capacity.sqlite3
fi
if [[ "$restore_desired_state" == true ]]; then
  printf 'restoring HMAC-verified desired-state ledger from %s\n' "${backup_dir}/desired-state.sqlite3"
  mkdir -p backups/desired-state
  rm -f backups/desired-state/desired-state.sqlite3-wal backups/desired-state/desired-state.sqlite3-shm
  install -m 600 "${backup_dir}/desired-state.sqlite3" backups/desired-state/desired-state.sqlite3
fi
if [[ "$restore_change_intelligence" == true ]]; then
  printf 'restoring HMAC-verified change-intelligence ledger from %s\n' "${backup_dir}/change-intelligence.sqlite3"
  mkdir -p backups/change-intelligence
  rm -f backups/change-intelligence/change-intelligence.sqlite3-wal backups/change-intelligence/change-intelligence.sqlite3-shm
  install -m 600 "${backup_dir}/change-intelligence.sqlite3" backups/change-intelligence/change-intelligence.sqlite3
fi
if [[ "$restore_alert_inbox" == true ]]; then
  alert_inbox_target="${DUNE_ALERT_INBOX_HOST_DATABASE:-backups/alert-inbox/inbox.sqlite3}"
  printf 'restoring verified Prometheus alert inbox from %s\n' "${backup_dir}/alert-inbox.sqlite3"
  mkdir -p "$(dirname "$alert_inbox_target")"
  chmod 700 "$(dirname "$alert_inbox_target")"
  rm -f "${alert_inbox_target}-wal" "${alert_inbox_target}-shm"
  install -m 600 "${backup_dir}/alert-inbox.sqlite3" "$alert_inbox_target"
fi
if [[ "$restore_peer_watch" == true ]]; then
  peer_watch_target="${DUNE_PEER_WATCH_HOST_DATABASE:-backups/peer-watch/watch.sqlite3}"
  printf 'restoring verified ecosystem peer-watch state from %s\n' "${backup_dir}/peer-watch.sqlite3"
  mkdir -p "$(dirname "$peer_watch_target")"
  chmod 700 "$(dirname "$peer_watch_target")"
  rm -f "${peer_watch_target}-wal" "${peer_watch_target}-shm"
  install -m 600 "${backup_dir}/peer-watch.sqlite3" "$peer_watch_target"
fi
if [[ "$restore_credential_lifecycle" == true ]]; then
  printf 'restoring HMAC-verified credential lifecycle ledger and authenticated head from %s\n' "$backup_dir"
  mkdir -p backups/credential-lifecycle
  chmod 700 backups/credential-lifecycle
  rm -f backups/credential-lifecycle/history.sqlite3-wal backups/credential-lifecycle/history.sqlite3-shm
  install -m 600 "${backup_dir}/credential-lifecycle.sqlite3" backups/credential-lifecycle/history.sqlite3
  install -m 600 "${backup_dir}/credential-lifecycle.anchor.json" backups/credential-lifecycle/history.anchor.json
fi
if [[ "$restore_change_approvals" == true ]]; then
  printf 'restoring HMAC-verified change-approval ledger and key from %s\n' "$backup_dir"
  mkdir -p backups/admin-panel
  chmod 700 backups/admin-panel
  rm -f backups/admin-panel/change-approvals.sqlite3-wal backups/admin-panel/change-approvals.sqlite3-shm
  install -m 600 "${backup_dir}/change-approvals.sqlite3" backups/admin-panel/change-approvals.sqlite3
  install -m 600 "${backup_dir}/change-approvals.key" backups/admin-panel/change-approvals.key
fi
if [[ "$restore_audit_ledger" == true ]]; then
  printf 'restoring HMAC-verified admin audit ledger and authenticated head from %s\n' "$backup_dir"
  mkdir -p backups/admin-panel
  chmod 700 backups/admin-panel
  rm -f backups/admin-panel/audit-ledger.sqlite3-wal backups/admin-panel/audit-ledger.sqlite3-shm
  install -m 600 "${backup_dir}/audit-ledger.sqlite3" backups/admin-panel/audit-ledger.sqlite3
  install -m 600 "${backup_dir}/audit-ledger.hmac.key" backups/admin-panel/audit-ledger.hmac.key
  install -m 600 "${backup_dir}/audit-ledger.anchor.json" backups/admin-panel/audit-ledger.anchor.json
fi

if [[ "$restore_config" == true ]]; then
  printf 'restoring config files from %s\n' "${backup_dir}/config.tgz"
  # The full backup contains protected secret files, while this workspace keeps
  # config/secrets as a validated link outside the repository. Preserve that
  # boundary when extracting the archived directory; tar otherwise replaces
  # the directory symlink by default.
  "$script_dir/protect-compose-secrets.sh" "$repo_root" >/dev/null
  tar --keep-directory-symlink -xzf "${backup_dir}/config.tgz"
  "$script_dir/protect-compose-secrets.sh" "$repo_root" >/dev/null
fi

if [[ "$restore_tls" == true ]]; then
  printf 'restoring TLS material from %s\n' "${backup_dir}/config-tls.tgz"
  rm -rf config/tls
  tar -xzf "${backup_dir}/config-tls.tgz"
fi

if [[ "$restore_rabbitmq" == true ]]; then
  printf 'restoring RabbitMQ state from %s\n' "$backup_dir"
  rm -rf data/rabbitmq
  mkdir -p data/rabbitmq/admin data/rabbitmq/game
  tar -xzf "${backup_dir}/rabbitmq-admin.tgz" -C data/rabbitmq/admin
  tar -xzf "${backup_dir}/rabbitmq-game.tgz" -C data/rabbitmq/game
fi

if [[ "$restore_server_saved" == true ]]; then
  archive="${backup_dir}/server-saved.tgz"
  if [[ ! -f "$archive" ]]; then
    printf 'server saved archive not found: %s\n' "$archive" >&2
    exit 1
  fi
  printf 'restoring server saved state from %s\n' "$archive"
  rm -rf data/server-saved
  mkdir -p data/server-saved
  tar -xzf "$archive" -C data/server-saved
fi

printf 'starting postgres\n'
"${compose[@]}" up -d postgres

printf 'restoring postgres from %s\n' "$dump_file"
"${compose[@]}" exec -T postgres \
  pg_restore -U dune -d "$db" --clean --if-exists \
  < "$dump_file"

printf 'restore complete. Start remaining services when ready:\n'
printf '  %s compose --env-file %s up -d admin-rmq game-rmq rmq-auth-shim text-router gateway director admin-panel admin-panel-ingress\n' "$container_runtime" "$env_file"
