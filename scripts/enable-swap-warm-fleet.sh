#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/enable-swap-warm-fleet.sh [ENV_FILE]

One-time setup for the swap-warm idle policy: flips every service in
DUNE_SWAP_WARM_SERVICES from autoscaler mode "dynamic" to "always-on" through
the existing admin API, so the core autoscaler keeps them running instead of
stopping them on idle. scripts/swap-warm-daemon.sh then takes over the
orthogonal job of squeezing/restoring their memory while idle/active.

Safe to re-run; set-mode is idempotent. Requires DUNE_ADMIN_TOKEN and
DUNE_ADMIN_AUTOSCALER_MUTATIONS_ENABLED=true in ENV_FILE.

Batches the mode flips (default 4 services, 90s pause between batches) so the
autoscaler does not try to cold-boot the whole newly-always-on set at once --
that would reproduce the exact thundering-herd problem this policy exists to
avoid. Set batch size to 0 to disable batching (not recommended for a large
service list).

Environment:
  DUNE_SWAP_WARM_SERVICES   Comma-separated service list. Required.
  DUNE_ADMIN_LOCAL_HOST     Admin bind host. Default: 127.0.0.1
  DUNE_ADMIN_LOCAL_PORT     Admin port. Default: DUNE_ADMIN_HOST_PORT or 18080
  DUNE_SWAP_WARM_ENABLE_BATCH_SIZE            Services per batch. Default: 4
  DUNE_SWAP_WARM_ENABLE_BATCH_PAUSE_SECONDS   Pause between batches. Default: 180.
                                               Live rollout evidence: with a 90s
                                               pause, later batches' requests
                                               landed while earlier batches were
                                               still cold-booting under load,
                                               pushing admin API responses past
                                               a 30s client timeout (requests
                                               still succeeded server-side; only
                                               the client-side timing was
                                               misleading). 180s comfortably
                                               exceeds observed single-map
                                               cold-start time under load.
  DUNE_SWAP_WARM_LOG        Log file. Default:
                             <repo>/backups/memory-squeeze/enable-swap-warm-fleet.log
USAGE
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/scripts/lib/snape-curl-auth.sh"
cd "$repo_root"
env_file="${1:-${ENV_FILE:-.env}}"

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
log_file="${log_file:-$repo_root/backups/memory-squeeze/enable-swap-warm-fleet.log}"

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

batch_size="$(env_or_file DUNE_SWAP_WARM_ENABLE_BATCH_SIZE)"
batch_size="${batch_size:-4}"
batch_pause="$(env_or_file DUNE_SWAP_WARM_ENABLE_BATCH_PAUSE_SECONDS)"
batch_pause="${batch_pause:-180}"

url="http://${admin_host}:${admin_port}/api/ops/autoscaler"
IFS=',' read -r -a services <<< "$services_raw"
failures=0
count_in_batch=0
total="${#services[@]}"
log "enabling ${total} service(s) in batches of ${batch_size:-unbatched}, ${batch_pause}s apart"
index=0
for svc in "${services[@]}"; do
  svc="$(printf '%s' "$svc" | xargs)"
  [[ -n "$svc" ]] || continue
  index=$((index + 1))
  body="$(python3 -c 'import json,sys; print(json.dumps({"action":"set-mode","service":sys.argv[1],"mode":"always-on","confirm":"CHANGE AUTOSCALER"}))' "$svc")"
  contract_args=()
  contract_request="$(python3 -c 'import json,sys; print(json.dumps({"targetPath":"/api/ops/autoscaler","requestBody":json.loads(sys.argv[1])}))' "$body")"
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
  else
    log "[${index}/${total}] change-contract preflight failed for ${svc}: ${contract_response}"
  fi
  if response="$(curl --fail-with-body --silent --show-error --max-time 30 -H "Authorization: Bearer $token" -H "Content-Type: application/json" "${contract_args[@]}" -X POST --data "$body" "$url" 2>&1)"; then
    mode_now="$(printf '%s' "$response" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print((d.get("modes") or {}).get(sys.argv[1], "unknown"))
except Exception:
    print("unparsed")' "$svc" 2>/dev/null || echo unparsed)"
    log "[${index}/${total}] set-mode ${svc} -> always-on: ok (modes[${svc}]=${mode_now})"
  else
    log "[${index}/${total}] set-mode ${svc} -> always-on: FAILED: ${response}"
    failures=$((failures + 1))
  fi
  count_in_batch=$((count_in_batch + 1))
  if (( batch_size > 0 )) && (( count_in_batch >= batch_size )) && (( index < total )); then
    log "batch of ${batch_size} done, pausing ${batch_pause}s before continuing so the autoscaler doesn't cold-boot them all at once"
    sleep "$batch_pause"
    count_in_batch=0
  fi
done

if (( failures > 0 )); then
  log "completed with ${failures} failure(s)"
  exit 1
fi
log "completed: ${total} service(s) set to always-on"
