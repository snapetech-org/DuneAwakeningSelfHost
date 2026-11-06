#!/usr/bin/env bash
set -euo pipefail
script_path="${BASH_SOURCE[0]}"
if [[ "$script_path" == */* ]]; then
  script_source_dir="${script_path%/*}"
else
  script_source_dir="."
fi
script_dir="$(cd -- "$script_source_dir" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/verify-backup.sh BACKUP_DIR

Checks that a DASH backup directory has readable dump/archive files.
This is a structural verification only; it does not perform a full restore.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

backup_dir="${1:-}"
if [[ -z "$backup_dir" || ! -d "$backup_dir" ]]; then
  printf 'backup directory required\n' >&2
  usage >&2
  exit 1
fi

# The original validators run against a byte-identical private local copy.
# This avoids random SQLite reads on slow archive storage and keeps the
# authoritative recovery point untouched. The wrapper checks source stability.
if [[ "${DUNE_BACKUP_VERIFY_VIEW_ACTIVE:-false}" != true ]]; then
  exec python3 "$script_dir/backup_verification_view.py" --script "$script_dir/verify-backup.sh" "$backup_dir"
fi

ok=true

check_dump() {
  local dump="$1"
  if ! command -v pg_restore >/dev/null 2>&1; then
    printf 'SKIP pg_restore not available for %s\n' "$dump"
    return 0
  fi
  if pg_restore --list "$dump" >/dev/null; then
    printf 'OK dump %s\n' "$dump"
  else
    printf 'FAIL dump %s\n' "$dump" >&2
    ok=false
  fi
}

check_tgz() {
  local archive="$1"
  if tar -tzf "$archive" >/dev/null; then
    printf 'OK archive %s\n' "$archive"
  else
    printf 'FAIL archive %s\n' "$archive" >&2
    ok=false
  fi
}

shopt -s nullglob
dumps=("$backup_dir"/*.dump)
archives=("$backup_dir"/*.tgz)
if [[ -d "$backup_dir"/maintenance ]]; then
  dumps+=("$backup_dir"/maintenance/*/*.dump)
  archives+=("$backup_dir"/maintenance/*/*.tgz)
fi

if [[ "${#dumps[@]}" -eq 0 ]]; then
  printf 'FAIL no Postgres dump files found under %s\n' "$backup_dir" >&2
  ok=false
fi

for dump in "${dumps[@]}"; do
  check_dump "$dump"
done
for archive in "${archives[@]}"; do
  check_tgz "$archive"
done

if [[ -f "$backup_dir/canary-autopilot.json" ]]; then
  if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/canary-autopilot.json" <<'PY'
import json
import pathlib
import sys
import canary_autopilot
path = pathlib.Path(sys.argv[1])
if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 4 * 1024 * 1024:
    raise SystemExit(1)
canary_autopilot.validate_state(json.loads(path.read_text(encoding="utf-8")))
PY
  then
    printf 'OK canary autopilot scheduler state %s\n' "$backup_dir/canary-autopilot.json"
  else
    printf 'FAIL canary autopilot scheduler state %s\n' "$backup_dir/canary-autopilot.json" >&2
    ok=false
  fi
else
  printf 'WARN no canary-autopilot.json found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/community-rewards.sqlite3" ]]; then
  if python3 - "$backup_dir/community-rewards.sqlite3" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    result = connection.execute("pragma integrity_check").fetchone()[0]
finally:
    connection.close()
raise SystemExit(0 if result == "ok" else 1)
PY
  then
    printf 'OK community rewards SQLite snapshot %s\n' "$backup_dir/community-rewards.sqlite3"
  else
    printf 'FAIL community rewards SQLite snapshot %s\n' "$backup_dir/community-rewards.sqlite3" >&2
    ok=false
  fi
else
  printf 'WARN no community-rewards.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/moderation.sqlite3" ]]; then
  if python3 - "$backup_dir/moderation.sqlite3" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    result = connection.execute("pragma integrity_check").fetchone()[0]
finally:
    connection.close()
raise SystemExit(0 if result == "ok" else 1)
PY
  then
    printf 'OK moderation SQLite snapshot %s\n' "$backup_dir/moderation.sqlite3"
  else
    printf 'FAIL moderation SQLite snapshot %s\n' "$backup_dir/moderation.sqlite3" >&2
    ok=false
  fi
else
  printf 'WARN no moderation.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/base-gallery.sqlite3" ]]; then
  if python3 - "$backup_dir/base-gallery.sqlite3" <<'PY'
import sqlite3
import sys
connection=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
try: result=connection.execute("pragma integrity_check").fetchone()[0]
finally: connection.close()
raise SystemExit(0 if result=="ok" else 1)
PY
  then
    printf 'OK base gallery SQLite snapshot %s\n' "$backup_dir/base-gallery.sqlite3"
  else
    printf 'FAIL base gallery SQLite snapshot %s\n' "$backup_dir/base-gallery.sqlite3" >&2
    ok=false
  fi
else
  printf 'WARN no base-gallery.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/operational-slo.sqlite3" ]]; then
  if python3 - "$backup_dir/operational-slo.sqlite3" <<'PY'
import hashlib,json,sqlite3,sys
connection=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
connection.row_factory=sqlite3.Row
try:
    if connection.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit(1)
    rows=connection.execute("select * from incident_events order by sequence").fetchall()
finally: connection.close()
previous=None
for row in rows:
    document={"incidentId":row["incident_id"],"objectiveId":row["objective_id"],"eventType":row["event_type"],"createdAt":row["created_at"],"actor":row["actor"],"note":row["note"],"payload":json.loads(row["payload_json"]),"previousHash":row["previous_hash"]}
    expected=hashlib.sha256(json.dumps(document,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    if row["previous_hash"]!=previous or row["event_hash"]!=expected: raise SystemExit(1)
    previous=row["event_hash"]
PY
  then
    printf 'OK operational SLO SQLite snapshot and incident hash chain %s\n' "$backup_dir/operational-slo.sqlite3"
  else
    printf 'FAIL operational SLO SQLite snapshot or incident hash chain %s\n' "$backup_dir/operational-slo.sqlite3" >&2
    ok=false
  fi
else
  printf 'WARN no operational-slo.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/capacity-intelligence.sqlite3" ]]; then
  if python3 - "$backup_dir/capacity-intelligence.sqlite3" <<'PY'
import hashlib,json,sqlite3,sys
connection=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
connection.row_factory=sqlite3.Row
try:
    if connection.execute("pragma integrity_check").fetchone()[0]!="ok": raise SystemExit(1)
    triggers={row[0] for row in connection.execute("select name from sqlite_master where type='trigger'")}
    if not {"capacity_applications_no_update","capacity_applications_no_delete"}.issubset(triggers): raise SystemExit(1)
    rows=connection.execute("select applied_at,actor,source,changes_json,sha256 from applications order by applied_at,id").fetchall()
finally: connection.close()
for row in rows:
    document={"appliedAt":row["applied_at"],"actor":row["actor"],"source":row["source"],"changes":json.loads(row["changes_json"])}
    expected=hashlib.sha256(json.dumps(document,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
    if row["sha256"]!=expected: raise SystemExit(1)
PY
  then
    printf 'OK capacity intelligence SQLite snapshot and application receipts %s\n' "$backup_dir/capacity-intelligence.sqlite3"
  else
    printf 'FAIL capacity intelligence SQLite snapshot or application receipts %s\n' "$backup_dir/capacity-intelligence.sqlite3" >&2
    ok=false
  fi
else
  printf 'WARN no capacity-intelligence.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/desired-state.sqlite3" ]]; then
  desired_archive=""
  for candidate in "$backup_dir/config.tgz" "$backup_dir/config-and-env.tgz"; do
    if [[ -f "$candidate" ]]; then desired_archive="$candidate"; break; fi
  done
  if [[ -z "$desired_archive" ]]; then
    printf 'FAIL desired-state verification requires the matching config archive and HMAC key\n' >&2
    ok=false
  else
    if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/desired-state.sqlite3" "$desired_archive" <<'PY'
import os
import pathlib
import sys
import tarfile
import tempfile
import desired_state

database, archive = sys.argv[1:]
try:
    with tarfile.open(archive, "r:gz") as source, tempfile.TemporaryDirectory(prefix="dash-desired-verify-") as directory:
        root = pathlib.Path(directory)
        paths = {}
        for name, maximum in (("config/desired-state.json", 1024 * 1024), ("config/secrets/desired-state-hmac.secret", 16 * 1024)):
            member = source.getmember(name)
            if not member.isfile() or member.size <= 0 or member.size > maximum:
                raise ValueError(f"invalid backup member: {name}")
            handle = source.extractfile(member)
            if handle is None:
                raise ValueError(f"unreadable backup member: {name}")
            value = handle.read(maximum + 1)
            if len(value) > maximum:
                raise ValueError(f"oversized backup member: {name}")
            path = root / pathlib.PurePosixPath(name).name
            path.write_bytes(value)
            os.chmod(path, 0o600)
            paths[name] = path
        result = desired_state.Store(database, paths["config/desired-state.json"], paths["config/secrets/desired-state-hmac.secret"]).verify()
        raise SystemExit(0 if result.get("ok") else 1)
except (OSError, KeyError, ValueError, tarfile.TarError):
    raise SystemExit(1)
PY
    then
      printf 'OK desired-state SQLite snapshot, baseline/observation/finding HMACs, and event chain %s\n' "$backup_dir/desired-state.sqlite3"
    else
      printf 'FAIL desired-state SQLite snapshot or matching policy/HMAC attestations %s\n' "$backup_dir/desired-state.sqlite3" >&2
      ok=false
    fi
  fi
else
  printf 'WARN no desired-state.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/change-intelligence.sqlite3" ]]; then
  change_archive=""
  for candidate in "$backup_dir/config.tgz" "$backup_dir/config-and-env.tgz"; do
    if [[ -f "$candidate" ]]; then change_archive="$candidate"; break; fi
  done
  if [[ -z "$change_archive" ]]; then
    printf 'FAIL change-intelligence verification requires the matching config archive and HMAC key\n' >&2
    ok=false
  else
    if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/change-intelligence.sqlite3" "$change_archive" <<'PY'
import os
import pathlib
import sys
import tarfile
import tempfile
import change_intelligence

database, archive = sys.argv[1:]
try:
    with tarfile.open(archive, "r:gz") as source, tempfile.TemporaryDirectory(prefix="dash-change-verify-") as directory:
        root = pathlib.Path(directory)
        paths = {}
        for name, maximum in (("config/change-intelligence.json", 1024 * 1024), ("config/secrets/change-intelligence-hmac.secret", 16 * 1024)):
            member = source.getmember(name)
            if not member.isfile() or member.size <= 0 or member.size > maximum:
                raise ValueError(f"invalid backup member: {name}")
            handle = source.extractfile(member)
            if handle is None:
                raise ValueError(f"unreadable backup member: {name}")
            value = handle.read(maximum + 1)
            if len(value) > maximum:
                raise ValueError(f"oversized backup member: {name}")
            path = root / pathlib.PurePosixPath(name).name
            path.write_bytes(value)
            os.chmod(path, 0o600)
            paths[name] = path
        result = change_intelligence.Store(database, paths["config/change-intelligence.json"], paths["config/secrets/change-intelligence-hmac.secret"]).verify()
        raise SystemExit(0 if result.get("ok") else 1)
except (OSError, KeyError, ValueError, tarfile.TarError):
    raise SystemExit(1)
PY
    then
      printf 'OK change-intelligence SQLite snapshot and HMAC event chain %s\n' "$backup_dir/change-intelligence.sqlite3"
    else
      printf 'FAIL change-intelligence SQLite snapshot or matching policy/HMAC event chain %s\n' "$backup_dir/change-intelligence.sqlite3" >&2
      ok=false
    fi
  fi
else
  printf 'WARN no change-intelligence.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/feature-readiness-history.sqlite3" ]]; then
  readiness_archive=""
  for candidate in "$backup_dir/config.tgz" "$backup_dir/config-and-env.tgz"; do
    if [[ -f "$candidate" ]]; then readiness_archive="$candidate"; break; fi
  done
  if [[ -z "$readiness_archive" ]]; then
    printf 'FAIL feature-readiness history verification requires the matching config archive and HMAC key\n' >&2
    ok=false
  else
    if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/feature-readiness-history.sqlite3" "$readiness_archive" <<'PY'
import os
import pathlib
import sys
import tarfile
import tempfile
import feature_readiness_history

database, archive = sys.argv[1:]
try:
    with tarfile.open(archive, "r:gz") as source, tempfile.TemporaryDirectory(prefix="dash-readiness-history-verify-") as directory:
        member = source.getmember("config/secrets/feature-readiness-history-hmac.secret")
        if not member.isfile() or member.size < 32 or member.size > 16 * 1024:
            raise ValueError("invalid feature-readiness history HMAC key backup member")
        handle = source.extractfile(member)
        if handle is None:
            raise ValueError("unreadable feature-readiness history HMAC key backup member")
        value = handle.read(16 * 1024 + 1)
        if len(value) > 16 * 1024:
            raise ValueError("oversized feature-readiness history HMAC key backup member")
        secret = pathlib.Path(directory) / "feature-readiness-history-hmac.secret"
        secret.write_bytes(value)
        os.chmod(secret, 0o600)
        result = feature_readiness_history.verify_database(database, secret)
        raise SystemExit(0 if result.get("ok") else 1)
except (OSError, KeyError, ValueError, tarfile.TarError):
    raise SystemExit(1)
PY
    then
      printf 'OK feature-readiness history SQLite snapshot and HMAC transition chain %s\n' "$backup_dir/feature-readiness-history.sqlite3"
    else
      printf 'FAIL feature-readiness history SQLite snapshot or matching HMAC chain %s\n' "$backup_dir/feature-readiness-history.sqlite3" >&2
      ok=false
    fi
  fi
else
  printf 'WARN no feature-readiness-history.sqlite3 found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/alert-inbox.sqlite3" ]]; then
  if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/alert-inbox.sqlite3" <<'PY'
import pathlib
import sqlite3
import sys
import alert_inbox

database = pathlib.Path(sys.argv[1])
try:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    integrity = connection.execute("pragma integrity_check").fetchone()[0]
    tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    connection.close()
    required = {"alerts", "transitions", "metadata"}
    raise SystemExit(0 if integrity == "ok" and required <= tables and alert_inbox.SCHEMA == "dash-alert-inbox/v1" else 1)
except (OSError, sqlite3.Error):
    raise SystemExit(1)
PY
  then
    printf 'OK alert-inbox SQLite snapshot and schema %s\n' "$backup_dir/alert-inbox.sqlite3"
  else
    printf 'FAIL alert-inbox SQLite snapshot or schema %s\n' "$backup_dir/alert-inbox.sqlite3" >&2
    ok=false
  fi
else
  if [[ -f "$backup_dir/manifest.txt" ]] && rg -q '^alert_inbox_required=true$' "$backup_dir/manifest.txt"; then
    printf 'FAIL enabled alert-inbox snapshot is missing from %s\n' "$backup_dir" >&2
    ok=false
  else
    printf 'WARN no alert-inbox.sqlite3 found in %s\n' "$backup_dir"
  fi
fi

if [[ -f "$backup_dir/peer-watch.sqlite3" ]]; then
  if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/peer-watch.sqlite3" <<'PY'
import pathlib
import sys
import peer_watch

database = pathlib.Path(sys.argv[1])
try:
    raise SystemExit(0 if peer_watch.verify_database(database).get("ok") else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
  then
    printf 'OK peer-watch SQLite snapshot and schema %s\n' "$backup_dir/peer-watch.sqlite3"
  else
    printf 'FAIL peer-watch SQLite snapshot or schema %s\n' "$backup_dir/peer-watch.sqlite3" >&2
    ok=false
  fi
else
  if [[ -f "$backup_dir/manifest.txt" ]] && rg -q '^peer_watch_required=true$' "$backup_dir/manifest.txt"; then
    printf 'FAIL enabled peer-watch snapshot is missing from %s\n' "$backup_dir" >&2
    ok=false
  else
    printf 'WARN no peer-watch.sqlite3 found in %s\n' "$backup_dir"
  fi
fi

credential_lifecycle_artifacts=0
for artifact in credential-lifecycle.sqlite3 credential-lifecycle.anchor.json; do
  [[ -f "$backup_dir/$artifact" ]] && credential_lifecycle_artifacts=$((credential_lifecycle_artifacts + 1))
done
if [[ "$credential_lifecycle_artifacts" -eq 2 ]]; then
  credential_archive=""
  for candidate in "$backup_dir/config.tgz" "$backup_dir/config-and-env.tgz"; do
    if [[ -f "$candidate" ]]; then credential_archive="$candidate"; break; fi
  done
  if [[ -z "$credential_archive" ]]; then
    printf 'FAIL credential-lifecycle verification requires the matching config archive and HMAC key\n' >&2
    ok=false
  else
    if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/credential-lifecycle.sqlite3" "$backup_dir/credential-lifecycle.anchor.json" "$credential_archive" <<'PY'
import os
import pathlib
import sys
import tarfile
import tempfile
import credential_lifecycle

database, anchor, archive = sys.argv[1:]
try:
    with tarfile.open(archive, "r:gz") as source, tempfile.TemporaryDirectory(prefix="dash-credential-lifecycle-verify-") as directory:
        member = source.getmember("config/secrets/credential-lifecycle-hmac.secret")
        if not member.isfile() or member.size < 32 or member.size > 16 * 1024:
            raise ValueError("invalid credential lifecycle HMAC key backup member")
        handle = source.extractfile(member)
        if handle is None:
            raise ValueError("unreadable credential lifecycle HMAC key backup member")
        value = handle.read(16 * 1024 + 1)
        if len(value) > 16 * 1024:
            raise ValueError("oversized credential lifecycle HMAC key backup member")
        secret = pathlib.Path(directory) / "credential-lifecycle-hmac.secret"
        secret.write_bytes(value)
        os.chmod(secret, 0o600)
        result = credential_lifecycle.verify_database(database, secret, anchor)
        raise SystemExit(0 if result.get("ok") else 1)
except (OSError, KeyError, ValueError, tarfile.TarError):
    raise SystemExit(1)
PY
    then
      printf 'OK credential-lifecycle SQLite snapshot, HMAC observation chain, and authenticated head %s\n' "$backup_dir/credential-lifecycle.sqlite3"
    else
      printf 'FAIL credential-lifecycle SQLite snapshot, matching HMAC chain, or authenticated head %s\n' "$backup_dir/credential-lifecycle.sqlite3" >&2
      ok=false
    fi
  fi
elif [[ "$credential_lifecycle_artifacts" -eq 1 ]]; then
  printf 'FAIL credential-lifecycle backup requires its SQLite ledger and authenticated head together\n' >&2
  ok=false
else
  printf 'WARN no credential-lifecycle.sqlite3 found in %s\n' "$backup_dir"
fi

change_approval_artifacts=0
for artifact in change-approvals.sqlite3 change-approvals.key; do
  [[ -f "$backup_dir/$artifact" ]] && change_approval_artifacts=$((change_approval_artifacts + 1))
done
if [[ "$change_approval_artifacts" -eq 2 ]]; then
  if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/change-approvals.sqlite3" "$backup_dir/change-approvals.key" <<'PY'
import sys
import change_approvals

result = change_approvals.Store(sys.argv[1], key_path=sys.argv[2]).verify()
raise SystemExit(0 if result.get("ok") else 1)
PY
  then
    printf 'OK change-approval SQLite snapshot and HMAC event chain %s\n' "$backup_dir/change-approvals.sqlite3"
  else
    printf 'FAIL change-approval SQLite snapshot or HMAC event chain %s\n' "$backup_dir/change-approvals.sqlite3" >&2
    ok=false
  fi
elif [[ "$change_approval_artifacts" -eq 0 ]]; then
  printf 'WARN no change-approval snapshot found in %s\n' "$backup_dir"
else
  printf 'FAIL change-approval backup must contain its SQLite ledger and HMAC key together\n' >&2
  ok=false
fi

audit_ledger_artifacts=0
for artifact in audit-ledger.sqlite3 audit-ledger.hmac.key audit-ledger.anchor.json; do
  [[ -f "$backup_dir/$artifact" ]] && audit_ledger_artifacts=$((audit_ledger_artifacts + 1))
done
if [[ "$audit_ledger_artifacts" -eq 3 ]]; then
  if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/audit-ledger.sqlite3" "$backup_dir/audit-ledger.hmac.key" "$backup_dir/audit-ledger.anchor.json" <<'PY'
import sys
import audit_ledger

result = audit_ledger.Store(sys.argv[1], key_path=sys.argv[2], anchor_path=sys.argv[3]).verify()
raise SystemExit(0 if result.get("ok") else 1)
PY
  then
    printf 'OK audit ledger SQLite snapshot, HMAC event chain, and authenticated head %s\n' "$backup_dir/audit-ledger.sqlite3"
  else
    printf 'FAIL audit ledger SQLite snapshot, HMAC event chain, or authenticated head %s\n' "$backup_dir/audit-ledger.sqlite3" >&2
    ok=false
  fi
elif [[ "$audit_ledger_artifacts" -eq 0 ]]; then
  printf 'WARN no audit-ledger snapshot found in %s\n' "$backup_dir"
else
  printf 'FAIL audit ledger backup must contain database, HMAC key, and authenticated anchor together\n' >&2
  ok=false
fi

if [[ -f "$backup_dir/operator-evidence.tgz" ]]; then
  evidence_config_archive=""
  for candidate in "$backup_dir/config.tgz" "$backup_dir/config-and-env.tgz"; do
    if [[ -f "$candidate" ]]; then evidence_config_archive="$candidate"; break; fi
  done
  if [[ -z "$evidence_config_archive" ]]; then
    printf 'FAIL signed operator evidence verification requires the matching config archive and HMAC key\n' >&2
    ok=false
  else
    if evidence_count="$(PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/operator-evidence.tgz" "$evidence_config_archive" <<'PY'
import json
import os
import pathlib
import re
import sys
import tarfile
import tempfile
import change_intelligence
import community_canary
import creator_canary
import public_ip_canary
import operations_briefing
import deployment_assurance
import maintenance_outcomes
import update_readiness

evidence_archive, config_archive = sys.argv[1:]
try:
    with tarfile.open(config_archive, "r:gz") as config, tempfile.TemporaryDirectory(prefix="dash-evidence-verify-") as directory:
        member = config.getmember("config/secrets/change-intelligence-hmac.secret")
        if not member.isfile() or member.size <= 0 or member.size > 16 * 1024:
            raise ValueError("invalid change-intelligence key backup member")
        handle = config.extractfile(member)
        if handle is None:
            raise ValueError("unreadable change-intelligence key backup member")
        secret_path = pathlib.Path(directory) / "change-intelligence-hmac.secret"
        secret_path.write_bytes(handle.read(16 * 1024 + 1))
        os.chmod(secret_path, 0o600)
        secret = change_intelligence.read_secret(secret_path)
        with tarfile.open(evidence_archive, "r:gz") as evidence:
            members = evidence.getmembers()
            if not 1 <= len(members) <= 1000:
                raise ValueError("operator evidence archive must contain 1..1000 files")
            total = 0
            for item in members:
                if not item.isfile() or not re.fullmatch(r"operator-evidence/[A-Za-z0-9_.-]+\.signed\.json", item.name) or not 1 <= item.size <= 10 * 1024 * 1024:
                    raise ValueError(f"invalid operator evidence member: {item.name}")
                total += item.size
                if total > 100 * 1024 * 1024:
                    raise ValueError("operator evidence archive exceeds 100 MiB")
                extracted = evidence.extractfile(item)
                if extracted is None:
                    raise ValueError(f"unreadable operator evidence member: {item.name}")
                document = json.loads(extracted.read(10 * 1024 * 1024 + 1))
                schema = document.get("schemaVersion")
                result = (
                    deployment_assurance.verify_signed_document(document, secret)
                    if schema == deployment_assurance.SIGNED_SCHEMA
                    else update_readiness.verify_signed_document(document, secret)
                    if schema in update_readiness.SCHEMAS
                    else maintenance_outcomes.verify_signed_document(document, secret)
                    if schema == maintenance_outcomes.SCHEMA
                    else community_canary.verify_signed_document(document, secret)
                    if schema == community_canary.SCHEMA
                    else creator_canary.verify_signed_document(document, secret)
                    if schema == creator_canary.SCHEMA
                    else public_ip_canary.verify_signed_document(document, secret)
                    if schema == public_ip_canary.SCHEMA
                    else operations_briefing.verify_signed_document(document, secret)
                    if schema == operations_briefing.SCHEMA
                    else change_intelligence.verify_signed_capsule(document, secret)
                )
                if not result.get("ok"):
                    raise ValueError(f"invalid signed operator evidence {item.name}: {result.get('error')}")
            print(len(members))
except (OSError, KeyError, ValueError, json.JSONDecodeError, tarfile.TarError):
    raise SystemExit(1)
PY
    )"; then
      printf 'OK %s portable signed operator evidence capsule(s) %s\n' "$evidence_count" "$backup_dir/operator-evidence.tgz"
    else
      printf 'FAIL portable signed operator evidence capsules or matching HMAC key %s\n' "$backup_dir/operator-evidence.tgz" >&2
      ok=false
    fi
  fi
else
  printf 'WARN no operator-evidence.tgz found in %s\n' "$backup_dir"
fi

rabbitmq_receipt_expected=""
if [[ -f "$backup_dir/manifest.txt" ]]; then
  while IFS= read -r manifest_line || [[ -n "$manifest_line" ]]; do
    if [[ "$manifest_line" == rabbitmq_restore_receipt=* ]]; then
      rabbitmq_receipt_expected="${manifest_line#rabbitmq_restore_receipt=}"
    fi
  done < "$backup_dir/manifest.txt"
fi
if [[ -f "$backup_dir/rabbitmq-restore-drill.json" ]]; then
  if PYTHONPATH="$repo_root/admin" python3 - "$backup_dir/rabbitmq-restore-drill.json" <<'PY'
import json
import pathlib
import sys
import rabbitmq_restore_drill

path = pathlib.Path(sys.argv[1])
if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 2 * 1024 * 1024:
    raise SystemExit(1)
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if rabbitmq_restore_drill.verify_receipt_document(payload) else 1)
PY
  then
    printf 'OK portable RabbitMQ recovery receipt %s\n' "$backup_dir/rabbitmq-restore-drill.json"
  else
    printf 'FAIL portable RabbitMQ recovery receipt %s\n' "$backup_dir/rabbitmq-restore-drill.json" >&2
    ok=false
  fi
elif [[ -n "$rabbitmq_receipt_expected" ]]; then
  printf 'FAIL manifest declares a RabbitMQ recovery receipt but the artifact is missing\n' >&2
  ok=false
else
  printf 'WARN no RabbitMQ recovery receipt found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/manifest.json" ]]; then
  if python3 - "$backup_dir/manifest.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
coverage = document.get("coverage")
if coverage is not None:
    required = coverage.get("required") or []
    captured = coverage.get("captured") or []
    missing = coverage.get("missing") or []
    if (
        coverage.get("schemaVersion") != "dash-full-backup-coverage/v1"
        or not coverage.get("complete") or missing or set(required) != set(captured)
        or any(name not in (document.get("artifacts") or {}) for name in required)
    ):
        raise SystemExit(1)
    print(f"OK full-backup coverage {len(required)}/{len(required)} required artifacts")
PY
  then
    printf 'OK manifest %s\n' "$backup_dir/manifest.json"
  else
    printf 'FAIL manifest or full-backup coverage declaration %s\n' "$backup_dir/manifest.json" >&2
    ok=false
  fi
elif [[ -f "$backup_dir/manifest.txt" ]]; then
  printf 'OK manifest %s\n' "$backup_dir/manifest.txt"
  if rg -q '^world_unique_name=.' "$backup_dir/manifest.txt"; then
    printf 'OK manifest world identity present\n'
  else
    printf 'WARN manifest missing world_unique_name\n'
  fi
else
  printf 'WARN no manifest found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/config.tgz" ]]; then
  check_tgz "$backup_dir/config.tgz"
else
  printf 'WARN no config.tgz found in %s\n' "$backup_dir"
fi

if [[ -f "$backup_dir/config-tls.tgz" ]]; then
  check_tgz "$backup_dir/config-tls.tgz"
else
  printf 'WARN no config-tls.tgz found in %s\n' "$backup_dir"
fi

if find "$backup_dir" -maxdepth 1 -type f \( -name '.env' -o -name '*.env' \) | grep -q .; then
  printf 'OK env copy present\n'
else
  printf 'WARN no env copy found in %s\n' "$backup_dir"
fi

if [[ "$ok" == true ]]; then
  printf 'backup verification complete: OK\n'
else
  printf 'backup verification complete: FAILED\n' >&2
  exit 1
fi
