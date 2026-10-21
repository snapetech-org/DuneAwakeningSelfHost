#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/assured-control-plane-deploy.sh --manifest FILE --reason TEXT [--stage DIR] [--workspace DIR] [--pre-change-backup DIR] [env-file]

Runs a production-host-only two-phase control-plane deployment. The workflow:
  1. verifies the exact commit/source manifest and a fresh pre-change backup;
  2. captures every game-map container plus strict always-on process continuity;
  3. deploys only admin-panel/admin-panel-ingress through the normal tested path;
  4. validates and seals reviewed desired state, then certifies all runbooks;
  5. creates and verifies a post-change backup;
  6. finalizes an HMAC-signed assurance receipt; and
  7. creates a second verified backup containing that signed receipt.

It never calls a game-map start, stop, restart, or raw Compose map lifecycle.
EOF
}

manifest=""
reason=""
env_file=".env"
stage=""
workspace=""
pre_change_backup=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) manifest="${2:-}"; shift 2 ;;
    --reason) reason="${2:-}"; shift 2 ;;
    --stage) stage="${2:-}"; shift 2 ;;
    --workspace) workspace="${2:-}"; shift 2 ;;
    --pre-change-backup) pre_change_backup="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    *) env_file="$1"; shift ;;
  esac
done

[[ -n "$manifest" && -f "$manifest" ]] || { printf 'a readable --manifest is required\n' >&2; exit 2; }
[[ ${#reason} -ge 10 && ${#reason} -le 1000 ]] || { printf -- '--reason must contain 10..1000 characters\n' >&2; exit 2; }
[[ -f "$env_file" ]] || { printf 'env file not found: %s\n' "$env_file" >&2; exit 2; }

required_host="${DUNE_PRODUCTION_HOST:-kspls0}"
actual_host="$(hostname -s 2>/dev/null || hostname)"
[[ "$actual_host" == "$required_host" ]] || { printf 'refusing assured production deployment on %s; required host is %s\n' "$actual_host" "$required_host" >&2; exit 1; }

code_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$code_root/scripts/lib/snape-curl-auth.sh"
workspace="${workspace:-$code_root}"
workspace="$(cd -- "$workspace" && pwd)"
if [[ -n "$stage" ]]; then
  stage="$(cd -- "$stage" && pwd)"
fi
cd "$workspace"

read_env() {
  awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print}' "$env_file" | tail -1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

command -v flock >/dev/null 2>&1 || { printf 'flock is required for assured deployment serialization\n' >&2; exit 1; }
operation_lock="${DUNE_OPERATION_LOCK_FILE:-backups/admin-panel/operation.lock}"
operation_lock_wait="${DUNE_OPERATION_LOCK_WAIT_SECONDS:-$(read_env DUNE_OPERATION_LOCK_WAIT_SECONDS)}"
operation_lock_wait="${operation_lock_wait:-1800}"
[[ "$operation_lock_wait" =~ ^[0-9]+$ ]] || { printf 'DUNE_OPERATION_LOCK_WAIT_SECONDS must be a non-negative integer\n' >&2; exit 1; }
mkdir -p "$(dirname -- "$operation_lock")"
exec 8>"$operation_lock"
if ! flock -w "$operation_lock_wait" 8; then
  printf 'timed out waiting for the shared backup/deployment operation lock: %s\n' "$operation_lock" >&2
  exit 1
fi
export DUNE_OPERATION_LOCK_HELD=true

token="$(read_env DUNE_ADMIN_TOKEN)"
[[ -n "$token" ]] || { printf 'DUNE_ADMIN_TOKEN is required\n' >&2; exit 1; }
admin_port="$(read_env DUNE_ADMIN_HOST_PORT)"
admin_port="${admin_port:-18080}"
admin_url="http://127.0.0.1:${admin_port}"
prometheus_port="$(read_env DUNE_METRICS_PROMETHEUS_PORT)"
prometheus_port="${prometheus_port:-19090}"
prometheus_url="http://127.0.0.1:${prometheus_port}"
work="$(mktemp -d -p "${TMPDIR:-/tmp}" dash-assured-deploy.XXXXXX)"
chmod 700 "$work"
window_id=""
finished=false
apply_lock=""

cleanup() {
  local status=$?
  [[ -z "$apply_lock" ]] || rm -f "$apply_lock" || true
  if [[ $status -ne 0 && -n "$window_id" && "$finished" != true ]]; then
    python3 - "$window_id" "$reason" >"$work/cancel.json" <<'PY'
import json,sys
print(json.dumps({"action":"cancel","windowId":sys.argv[1],"reason":"Workflow aborted: " + sys.argv[2],"confirm":"CANCEL ASSURED CHANGE WINDOW"}))
PY
    curl -fsS --max-time 15 -H "Authorization: Bearer $token" -H 'Content-Type: application/json' --data-binary "@$work/cancel.json" "$admin_url/api/ops/deployment-assurance" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

api_get() {
  curl -fsS --max-time "${2:-30}" -H "Authorization: Bearer $token" "$admin_url$1"
}

api_post_file() {
  curl -fsS --max-time "${2:-300}" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' --data-binary "@$1" "$admin_url/api/ops/deployment-assurance"
}

recover_start_response() {
  local deadline=$((SECONDS + 300))
  while (( SECONDS <= deadline )); do
    if api_get /api/ops/deployment-assurance 60 >"$work/deployment-status.json" \
      && python3 - "$work/deployment-status.json" "$manifest" "$reason" >"$work/start-response.json" <<'PY'
import json, sys
status=json.load(open(sys.argv[1], encoding="utf-8"))
manifest=json.load(open(sys.argv[2], encoding="utf-8"))
matches=[row for row in status.get("openWindows") or [] if row.get("commit") == manifest.get("commit") and row.get("reason") == sys.argv[3]]
if len(matches) != 1:
    raise SystemExit(1)
print(json.dumps(matches[0]))
PY
    then
      printf 'recovered durable assured change window after empty/failed start response\n' >&2
      return 0
    fi
    sleep 5
  done
  return 1
}

verified_backup() {
  local output backup attempt verifier="./scripts/verify-backup.sh"
  if [[ -n "$stage" && -x "$stage/scripts/verify-backup.sh" ]]; then
    verifier="$stage/scripts/verify-backup.sh"
  fi
  for attempt in 1 2 3; do
    if output="$(./scripts/backup-state.sh "$env_file" 2>&1)"; then
      printf '%s\n' "$output"
      backup="$(printf '%s\n' "$output" | sed -n 's/^backup complete: //p' | tail -1)"
      if [[ -n "$backup" && -d "$backup" ]] && "$verifier" "$backup"; then
        printf '%s\n' "${backup#backups/}"
        return 0
      fi
    else
      printf 'backup attempt %s/3 failed:\n%s\n' "$attempt" "$output" >&2
    fi
    sleep 2
  done
  printf 'could not create and verify a complete backup after 3 attempts\n' >&2
  return 1
}

verified_existing_backup() {
  local requested="$1" resolved relative verifier="./scripts/verify-backup.sh"
  [[ -n "$requested" && "$requested" != *$'\n'* ]] || {
    printf 'pre-change backup path is invalid\n' >&2
    return 1
  }
  if [[ "$requested" == /* ]]; then
    resolved="$requested"
  else
    resolved="$workspace/$requested"
  fi
  resolved="$(realpath -e -- "$resolved")"
  [[ -d "$resolved" && "$resolved" == "$workspace/backups/"* ]] || {
    printf 'pre-change backup must be an existing directory beneath workspace/backups\n' >&2
    return 1
  }
  if [[ -n "$stage" && -x "$stage/scripts/verify-backup.sh" ]]; then
    verifier="$stage/scripts/verify-backup.sh"
  fi
  "$verifier" "$resolved"
  relative="${resolved#"$workspace/backups/"}"
  printf '%s\n' "$relative"
}

wait_for_assurance_health() {
  local timeout interval required_samples deadline consecutive=0
  timeout="$(read_env DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_TIMEOUT_SECONDS)"
  interval="$(read_env DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_POLL_SECONDS)"
  required_samples="$(read_env DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_SAMPLES)"
  timeout="${timeout:-300}"
  interval="${interval:-5}"
  required_samples="${required_samples:-2}"
  [[ "$timeout" =~ ^[0-9]+$ && "$interval" =~ ^[0-9]+$ && "$required_samples" =~ ^[0-9]+$ ]] || {
    printf 'deployment assurance convergence settings must be positive integers\n' >&2
    return 1
  }
  (( timeout >= 30 && timeout <= 1800 && interval >= 1 && interval <= 60 && required_samples >= 2 && required_samples <= 10 )) || {
    printf 'deployment assurance convergence settings are outside safe bounds\n' >&2
    return 1
  }
  deadline=$((SECONDS + timeout))
  while (( SECONDS <= deadline )); do
    if api_get /api/ops/desired-state 30 >"$work/convergence-desired.json" \
      && api_get /api/ops/change-intelligence 30 >"$work/convergence-change.json" \
      && api_get /api/ops/slo 30 >"$work/convergence-slo.json" \
      && curl -fsS --max-time 30 --get \
        --data-urlencode 'query=dash_incident_readiness_certification_ready' \
        "$prometheus_url/api/v1/query" >"$work/convergence-prometheus.json"; then
      if python3 - "$work/convergence-desired.json" "$work/convergence-change.json" "$work/convergence-slo.json" "$work/convergence-prometheus.json" >"$work/convergence.json" <<'PY'
import json, sys
desired, change, slo, prometheus = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
certification = change.get("readinessCertification") or {}
prometheus_results = ((prometheus.get("data") or {}).get("result") or []) if prometheus.get("status") == "success" else []
checks = {
    "desiredStateAttested": desired.get("state") == "attested" and not (desired.get("openFindings") or []) and bool((desired.get("integrity") or {}).get("ok")),
    "changeIntegrity": bool((change.get("integrity") or {}).get("ok")) and not (change.get("openIncidents") or []),
    "readinessCurrent": bool(certification.get("currentReady") and certification.get("policyCurrent") and (certification.get("receiptVerification") or {}).get("ok")),
    "sloHealthy": slo.get("overall") == "healthy" and not (slo.get("openIncidents") or []) and bool((slo.get("integrity") or {}).get("ok")),
    "prometheusReadiness": any(float((row.get("value") or [None, "0"])[1]) == 1.0 for row in prometheus_results),
}
print(json.dumps({"ready": all(checks.values()), "checks": checks}, sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 1)
PY
      then
        consecutive=$((consecutive + 1))
        printf 'assurance health convergence: %s/%s consecutive healthy samples\n' "$consecutive" "$required_samples"
        if (( consecutive >= required_samples )); then
          return 0
        fi
      else
        consecutive=0
        printf 'waiting for assurance health convergence: %s\n' "$(cat "$work/convergence.json")" >&2
      fi
    else
      consecutive=0
      printf 'waiting for assurance health convergence: health endpoint unavailable\n' >&2
    fi
    sleep "$interval"
  done
  printf 'deployment assurance health did not converge within %s seconds; last sample: %s\n' "$timeout" "$(cat "$work/convergence.json" 2>/dev/null || printf unavailable)" >&2
  return 1
}

finalize_assurance_when_healthy() {
  local timeout interval deadline state failed_health
  timeout="$(read_env DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_TIMEOUT_SECONDS)"
  interval="$(read_env DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_POLL_SECONDS)"
  timeout="${timeout:-300}"
  interval="${interval:-5}"
  deadline=$((SECONDS + timeout))
  while (( SECONDS <= deadline )); do
    if ! api_post_file "$work/finish.json" 600 >"$work/finish-response.json"; then
      if api_get /api/ops/deployment-assurance 60 >"$work/deployment-status.json" \
        && python3 - "$work/deployment-status.json" "$window_id" "$reason" >"$work/finish-response.json" <<'PY'
import json, sys
status=json.load(open(sys.argv[1], encoding="utf-8"))
window_id, reason=sys.argv[2:4]
window=next((row for row in status.get("windows") or [] if row.get("id") == window_id), None)
latest=status.get("latest") or {}
if not window or window.get("status") != "completed" or not latest.get("ready") or latest.get("reason") != reason:
    raise SystemExit(1)
receipt={
    "id": latest.get("id"), "commit": latest.get("commit"), "reason": latest.get("reason"),
    "ready": latest.get("ready"), "receiptSha256": latest.get("receiptSha256"),
    "invariants": latest.get("invariants") or {},
}
print(json.dumps({"finalized": True, "document": {"receipt": receipt}, "verification": latest.get("verification") or {}}))
PY
      then
        printf 'recovered finalized assurance receipt after empty/failed finish response\n' >&2
        return 0
      fi
      sleep "$interval"
      continue
    fi
    state="$(python3 - "$work/finish-response.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
if isinstance((d.get("document") or {}).get("receipt"), dict):
    print("finalized")
elif d.get("finalized") is False and d.get("state") == "waiting-for-health":
    print("waiting")
else:
    raise SystemExit("deployment assurance finish returned an invalid response")
PY
)"
    if [[ "$state" == "finalized" ]]; then
      return 0
    fi
    failed_health="$(python3 - "$work/finish-response.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
print(", ".join(d.get("failedHealth") or ["unknown health gate"]))
PY
)"
    printf 'deployment assurance finalization deferred: %s\n' "$failed_health" >&2
    sleep "$interval"
  done
  printf 'deployment assurance finalization remained unhealthy for %s seconds\n' "$timeout" >&2
  return 1
}

if [[ -n "$stage" ]]; then
  python3 "$code_root/scripts/deployment-assurance.py" verify --manifest "$manifest" --workspace "$stage" >"$work/manifest-verification.json"
else
  python3 "$code_root/scripts/deployment-assurance.py" verify --manifest "$manifest" --workspace "$workspace" >"$work/manifest-verification.json"
fi
./scripts/validate-landsraad-coriolis-cycle.sh "$env_file"

if [[ -n "$pre_change_backup" ]]; then
  pre_backup_output="$(verified_existing_backup "$pre_change_backup")"
else
  pre_backup_output="$(verified_backup)"
fi
printf '%s\n' "$pre_backup_output"
pre_backup="$(printf '%s\n' "$pre_backup_output" | tail -1)"

mkdir -p backups/deployments
chmod 700 backups/deployments
rollback_archive="backups/deployments/assured-source-before-$(date -u +%Y%m%dT%H%M%SZ).tgz"
python3 "$code_root/scripts/deployment-assurance.py" archive --manifest "$manifest" --workspace "$workspace" --output "$rollback_archive" >"$work/rollback.json"
rollback_sha="$(python3 - "$work/rollback.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); print(d["sha256"])
PY
)"
rollback_relative="${rollback_archive#backups/}"

python3 - "$manifest" "$reason" "$pre_backup" "$rollback_relative" "$rollback_sha" "$([[ -n "$stage" ]] && printf true || printf false)" >"$work/start.json" <<'PY'
import json,sys
document=json.load(open(sys.argv[1],encoding="utf-8"))
print(json.dumps({"action":"start","commit":document["commit"],"reason":sys.argv[2],"manifest":document,"preChangeBackupPath":sys.argv[3],"sourceRollbackArchive":sys.argv[4],"sourceRollbackSha256":sys.argv[5],"staged":sys.argv[6]=="true","confirm":"START ASSURED CHANGE WINDOW"}))
PY
if ! api_post_file "$work/start.json" 300 >"$work/start-response.json" \
  || ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$work/start-response.json"; then
  recover_start_response
fi
window_id="$(python3 - "$work/start-response.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); value=d.get("id","")
if not value.startswith("deployment-window-"): raise SystemExit("assurance start did not return a window id")
print(value)
PY
)"
printf 'assured change window: %s\n' "$window_id"

if [[ -n "$stage" ]]; then
  install -d -m 700 backups/deployment-assurance
  apply_lock="$workspace/backups/deployment-assurance/apply.lock"
  (umask 077; printf '%s\n' "$window_id" >"$apply_lock")
  python3 "$code_root/scripts/deployment-assurance.py" apply --manifest "$manifest" --source "$stage" --workspace "$workspace" >"$work/apply.json"
  rm -f "$apply_lock"
  apply_lock=""
fi
./scripts/deploy-admin-panel.sh "$env_file"
prometheus_id="$(docker ps -q --filter "label=com.docker.compose.project=dune_server" --filter "label=com.docker.compose.service=prometheus" | head -1)"
if [[ -n "$prometheus_id" ]]; then
  docker kill --signal HUP "$prometheus_id" >/dev/null
fi
./scripts/validate-landsraad-coriolis-cycle.sh "$env_file"

# Initialize and verify the credential observation trust state before Desired
# State is reviewed. Otherwise the first readiness certification could create
# the HMAC key/database/anchor after sealing and immediately cause source drift.
api_get '/api/ops/credential-lifecycle?refresh=true' 60 >"$work/credential-lifecycle.json"
python3 - "$work/credential-lifecycle.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8")); history=d.get("history") or {}
checks={
    "enabled": bool(d.get("enabled")),
    "hmacKeyConfigured": bool(d.get("hmacKeyConfigured")),
    "authenticatedHeadConfigured": bool(d.get("authenticatedHeadConfigured")),
    "historyValid": bool(history.get("ok")),
}
if not all(checks.values()): raise SystemExit("credential lifecycle trust-state initialization failed: " + json.dumps(checks,sort_keys=True))
print(f"credential lifecycle initialized: {history.get('events',0)} events; authenticated head valid")
PY

api_get /api/ops/desired-state 60 >"$work/desired.json"
desired_state="$(python3 - "$work/desired.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); print(d.get("state", "unknown"))
PY
)"
if [[ "$desired_state" != "attested" ]]; then
  python3 - "$reason" >"$work/seal.json" <<'PY'
import json,sys
print(json.dumps({"action":"seal","reason":"Assured deployment: " + sys.argv[1],"confirm":"SEAL DESIRED STATE"}))
PY
  curl -fsS --max-time 120 -H "Authorization: Bearer $token" -H 'Content-Type: application/json' --data-binary "@$work/seal.json" "$admin_url/api/ops/desired-state" >"$work/seal-response.json"
fi

api_get /api/ops/change-intelligence 60 >"$work/change.json"
python3 - "$work/change.json" >"$work/certify.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); policy=d["policy"]["response"]["policySha256"]
print(json.dumps({"policySha256":policy,"confirm":"CERTIFY INCIDENT RESPONSE READINESS"}))
PY
curl -fsS --max-time 300 -H "Authorization: Bearer $token" -H 'Content-Type: application/json' --data-binary "@$work/certify.json" "$admin_url/api/ops/change-intelligence/certify" >"$work/certify-response.json"
python3 - "$work/certify-response.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); s=d.get("summary") or {}
if not d.get("ready") or s.get("runbooksReady") != s.get("runbooksTotal"): raise SystemExit("fleet-wide readiness certification failed")
print(f"readiness certification: {s['runbooksReady']}/{s['runbooksTotal']} runbooks, {s['diagnosticsReady']}/{s['diagnosticsTotal']} diagnostics, {s['recoveryContractsReady']}/{s['recoveryContractsTotal']} recovery contracts")
PY

post_backup_output="$(verified_backup)"
printf '%s\n' "$post_backup_output"
post_backup="$(printf '%s\n' "$post_backup_output" | tail -1)"
wait_for_assurance_health
python3 - "$window_id" "$post_backup" >"$work/finish.json" <<'PY'
import json,sys
print(json.dumps({"action":"finish","windowId":sys.argv[1],"backupPath":sys.argv[2],"confirm":"FINALIZE ASSURED CHANGE WINDOW"}))
PY
finalize_assurance_when_healthy
python3 - "$work/finish-response.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); receipt=(d.get("document") or {}).get("receipt") or {}; verification=d.get("verification") or {}
if not receipt.get("ready") or not verification.get("ok"): raise SystemExit("deployment assurance receipt failed")
failed=[key for key,value in (receipt.get("invariants") or {}).items() if not value]
if failed: raise SystemExit("deployment assurance invariants failed: " + ", ".join(failed))
print(json.dumps({"receiptId":receipt["id"],"commit":receipt["commit"],"ready":receipt["ready"],"receiptSha256":receipt["receiptSha256"],"evidencePath":d.get("evidencePath"),"invariants":receipt["invariants"]},indent=2,sort_keys=True))
PY
finished=true

final_backup_output="$(verified_backup)"
printf '%s\n' "$final_backup_output"
final_backup="$(printf '%s\n' "$final_backup_output" | tail -1)"

api_get /metrics/change-intelligence 30 | grep -q '^dash_deployment_assurance_latest_ready 1$'
printf 'OK: assured control-plane deployment complete; final evidence backup=%s\n' "$final_backup"
