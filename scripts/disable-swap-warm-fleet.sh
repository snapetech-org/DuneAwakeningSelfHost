#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/disable-swap-warm-fleet.sh [ENV_FILE] [TARGET_MODE]

Full decommission of the swap-warm idle policy, the counterpart to
scripts/enable-swap-warm-fleet.sh. For every service in DUNE_SWAP_WARM_SERVICES:

  1. restores memory.high to max if currently squeezed
     (scripts/map-memory-squeeze.sh restore);
  2. sets its autoscaler mode to TARGET_MODE (default: dynamic) through the
     existing admin API, handing container lifecycle back to the normal
     stop-on-idle/cold-start-on-demand autoscaler behavior.

This does not stop dune-swap-warm.service itself -- with no services left in
its managed mode, or with DUNE_SWAP_WARM_ENABLED=false, it idles harmlessly.
Stop/disable the unit separately if you want it gone entirely:

  sudo systemctl disable --now dune-swap-warm.service

For a quick, instantly-reversible pause instead of a full decommission, set
DUNE_SWAP_WARM_ENABLED=false in ENV_FILE -- the running daemon re-reads it
every cycle, restores any squeezed maps, and idles without needing a restart
or any mode changes. Re-run this script (or re-enable) to resume.

Environment:
  DUNE_SWAP_WARM_SERVICES   Comma-separated service list. Required.
  DUNE_ADMIN_LOCAL_HOST     Admin bind host. Default: 127.0.0.1
  DUNE_ADMIN_LOCAL_PORT     Admin port. Default: DUNE_ADMIN_HOST_PORT or 18080
  DUNE_SWAP_WARM_LOG        Log file. Default:
                             <repo>/backups/memory-squeeze/disable-swap-warm-fleet.log
USAGE
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/scripts/lib/snape-curl-auth.sh"
cd "$repo_root"
env_file="${1:-${ENV_FILE:-.env}}"
target_mode="${2:-dynamic}"

case "$target_mode" in
  always-on|dynamic|disabled) ;;
  *)
    printf 'TARGET_MODE must be always-on, dynamic, or disabled, got: %s\n' "$target_mode" >&2
    exit 2
    ;;
esac

read_env() {
  local key="$1"
  [[ -f "$env_file" ]] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$env_file"
}

env_or_file() {
  local key="$1"
  local value="${!key:-}"
  if [[ -n "$value" ]]; then
    printf '%s\n' "$value"
  else
    read_env "$key"
  fi
}

admin_host="${DUNE_ADMIN_LOCAL_HOST:-127.0.0.1}"
admin_port="${DUNE_ADMIN_LOCAL_PORT:-}"
if [[ -z "$admin_port" ]]; then
  admin_port="$(read_env DUNE_ADMIN_HOST_PORT)"
fi
admin_port="${admin_port:-18080}"
token="$(read_env DUNE_ADMIN_TOKEN || true)"
services_raw="$(env_or_file DUNE_SWAP_WARM_SERVICES)"
log_file="$(env_or_file DUNE_SWAP_WARM_LOG)"
log_file="${log_file:-$repo_root/backups/memory-squeeze/disable-swap-warm-fleet.log}"

if [[ -z "$services_raw" ]]; then
  printf 'DUNE_SWAP_WARM_SERVICES is required (comma-separated map service names)\n' >&2
  exit 2
fi
if [[ -z "$token" ]]; then
  printf 'DUNE_ADMIN_TOKEN is empty in %s\n' "$env_file" >&2
  exit 1
fi

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$line"
  mkdir -p "$(dirname "$log_file")"
  printf '%s\n' "$line" >> "$log_file"
}

url="http://${admin_host}:${admin_port}/api/ops/autoscaler"
IFS=',' read -r -a services <<< "$services_raw"
failures=0
total="${#services[@]}"
log "decommissioning ${total} service(s) back to mode=${target_mode}"

for svc in "${services[@]}"; do
  svc="$(printf '%s' "$svc" | xargs)"
  [[ -n "$svc" ]] || continue

  restore_output="$(./scripts/map-memory-squeeze.sh restore "$svc" 2>&1)" && log "restore ${svc}: ok" || log "restore ${svc}: skipped or failed (${restore_output##*$'\n'})"

  body="$(python3 -c 'import json,sys; print(json.dumps({"action":"set-mode","service":sys.argv[1],"mode":sys.argv[2],"confirm":"CHANGE AUTOSCALER"}))' "$svc" "$target_mode")"
  contract_request="$(python3 -c 'import json,sys; print(json.dumps({"targetPath":"/api/ops/autoscaler","requestBody":json.loads(sys.argv[1])}))' "$body")"
  contract_args=()
  if contract_response="$(curl --fail-with-body --silent --show-error --max-time 30 -H "Authorization: Bearer $token" -H "Content-Type: application/json" -X POST --data "$contract_request" "http://${admin_host}:${admin_port}/api/security/change-contract" 2>&1)"; then
    read -r contract_required contract_token < <(python3 -c '
import json, sys
response=json.load(sys.stdin)
required=bool(response.get("required") and (response.get("contract") or {}).get("governed"))
token=response.get("token") or ""
print("true" if required else "false", token)
' <<<"$contract_response")
    if [[ "$contract_required" == "true" ]]; then
      contract_args=(-H "X-DASH-Change-Contract: $contract_token")
    fi
  fi

  if response="$(curl --fail-with-body --silent --show-error --max-time 30 -H "Authorization: Bearer $token" -H "Content-Type: application/json" "${contract_args[@]}" -X POST --data "$body" "$url" 2>&1)"; then
    log "set-mode ${svc} -> ${target_mode}: ok"
  else
    log "set-mode ${svc} -> ${target_mode}: FAILED: ${response}"
    failures=$((failures + 1))
  fi
done

if (( failures > 0 )); then
  log "completed with ${failures} failure(s)"
  exit 1
fi
log "completed: ${total} service(s) restored and set to ${target_mode}"
