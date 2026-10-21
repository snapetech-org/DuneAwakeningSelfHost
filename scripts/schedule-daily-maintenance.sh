#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/schedule-daily-maintenance.sh

Schedules the daily executed, backed-up, announced all-services maintenance
restart through the local DASH admin panel.

Environment:
  DUNE_ENV_FILE                         Env file to read. Default: <repo>/.env
  DUNE_ADMIN_LOCAL_HOST                 Admin bind host. Default: 127.0.0.1
  DUNE_ADMIN_LOCAL_PORT                 Admin port. Default: DUNE_ADMIN_HOST_PORT or 18080
  DUNE_DAILY_RESTART_SCHEDULE_WINDOW    Allowed local HH:MM-HH:MM invocation window. Default: 05:25-05:35
  DUNE_DAILY_RESTART_ALLOW_OUTSIDE_WINDOW
                                        Set true to allow manual runs outside the window.
  DUNE_DAILY_RESTART_TIME               Local maintenance start time. Default: 06:00
  DUNE_DAILY_RESTART_REPEAT_SECONDS     Repeat cadence. Default: 600
  DUNE_DAILY_RESTART_RETRY_SECONDS      Seconds between schedule reconciliation attempts. Default: 60
  DUNE_DAILY_RESTART_RETRY_WINDOW_SECONDS
                                        Maximum reconciliation window. Default: 1500
  DUNE_DAILY_RESTART_REQUIRE_SOFT_DISCONNECT
                                        Require targeted player disconnect before stop.
                                        Default: false
  DUNE_DAILY_RESTART_UPDATE_POLICY       current, certified, or automatic. Default: certified.
                                        Certified candidates are revalidated before disconnect.
  DUNE_DAILY_RESTART_MESSAGE            Announcement text.
USAGE
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

repo_dir="${DUNE_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
source "$repo_dir/scripts/lib/snape-curl-auth.sh"
env_file="${DUNE_ENV_FILE:-$repo_dir/.env}"
admin_host="${DUNE_ADMIN_LOCAL_HOST:-127.0.0.1}"
admin_port="${DUNE_ADMIN_LOCAL_PORT:-}"

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

time_to_minutes() {
  local value="$1"
  if [[ ! "$value" =~ ^([0-2][0-9]):([0-5][0-9])$ ]]; then
    printf 'invalid schedule time: %s\n' "$value" >&2
    exit 2
  fi
  local hour="${BASH_REMATCH[1]}"
  local minute="${BASH_REMATCH[2]}"
  if (( 10#$hour > 23 )); then
    printf 'invalid schedule hour: %s\n' "$value" >&2
    exit 2
  fi
  printf '%s\n' $((10#$hour * 60 + 10#$minute))
}

check_schedule_window() {
  local allow window start end now_minutes start_minutes end_minutes
  allow="$(env_or_file DUNE_DAILY_RESTART_ALLOW_OUTSIDE_WINDOW)"
  case "${allow,,}" in
    1|true|yes|on) return 0 ;;
  esac
  window="$(env_or_file DUNE_DAILY_RESTART_SCHEDULE_WINDOW)"
  window="${window:-05:25-05:35}"
  if [[ "$window" != *-* ]]; then
    printf 'invalid DUNE_DAILY_RESTART_SCHEDULE_WINDOW: %s\n' "$window" >&2
    exit 2
  fi
  start="${window%-*}"
  end="${window#*-}"
  start_minutes="$(time_to_minutes "$start")"
  end_minutes="$(time_to_minutes "$end")"
  now_minutes=$((10#$(date +%H) * 60 + 10#$(date +%M)))
  if (( start_minutes <= end_minutes )); then
    if (( now_minutes >= start_minutes && now_minutes <= end_minutes )); then
      return 0
    fi
  else
    if (( now_minutes >= start_minutes || now_minutes <= end_minutes )); then
      return 0
    fi
  fi
  printf 'refusing to schedule daily maintenance outside %s local time; now=%s\n' "$window" "$(date +%H:%M)" >&2
  printf 'set DUNE_DAILY_RESTART_ALLOW_OUTSIDE_WINDOW=true for a deliberate manual run\n' >&2
  exit 2
}

check_schedule_window

# Refresh Steam's server package immediately before scheduling the full-farm
# restart. This shares the updater lock so the maintenance job cannot race a
# partially downloaded Tuesday patch and then restart the previously loaded
# Docker build when update readiness is unavailable.
hotfix_lock_file="$(env_or_file DUNE_HOTFIX_UPDATE_LOCK_FILE)"
hotfix_lock_file="${hotfix_lock_file:-/var/lib/dune-hotfix-auto-update/dune-hotfix-auto-update.lock}"
hotfix_lock_dir="$(dirname -- "$hotfix_lock_file")"
if [[ ! -d "$hotfix_lock_dir" ]]; then
  if ! mkdir -p "$hotfix_lock_dir"; then
    printf 'fail: cannot create Steam update lock directory: %s\n' "$hotfix_lock_dir" >&2
    exit 1
  fi
fi
exec 9>"$hotfix_lock_file"
if ! flock -w 1800 9; then
  printf 'fail: timed out waiting for the Steam hotfix updater lock\n' >&2
  exit 1
fi

steamcmd_enabled="$(env_or_file DUNE_RESTART_STEAMCMD_UPDATE)"
case "${steamcmd_enabled:-true}" in
  1|true|yes|on) ;;
  *)
    printf 'fail: cannot verify the latest Steam server build while SteamCMD updates are disabled\n' >&2
    exit 1
    ;;
esac
if ! DUNE_RESTART_STEAM_UPDATE_MODE=steamcmd DUNE_RESTART_STEAMCMD_REQUIRED=true \
    "$repo_dir/scripts/update-steam-tool.sh" "$env_file"; then
  printf 'fail: SteamCMD could not refresh the latest server package; maintenance was not scheduled\n' >&2
  exit 1
fi
set +e
steam_check_output="$("$repo_dir/scripts/check-steam-update.sh" "$env_file" 2>&1)"
steam_check_rc=$?
set -e
printf '%s\n' "$steam_check_output"
steam_candidate_requires_certification=false
case "$steam_check_rc" in
  0)
    ;;
  1)
    if grep -Eq 'status: update available|status: same tag but Steam build changed' <<<"$steam_check_output"; then
      steam_candidate_requires_certification=true
    else
      printf 'fail: Steam build check returned 1 without a recognized update state\n' >&2
      exit 1
    fi
    ;;
  *)
    printf 'fail: latest Steam server build could not be verified; maintenance was not scheduled\n' >&2
    exit "$steam_check_rc"
    ;;
esac

if [[ -z "$admin_port" ]]; then
  admin_port="$(read_env DUNE_ADMIN_HOST_PORT)"
fi
admin_port="${admin_port:-18080}"
maintenance_time="$(env_or_file DUNE_DAILY_RESTART_TIME)"
maintenance_time="${maintenance_time:-06:00}"
time_to_minutes "$maintenance_time" >/dev/null
now_epoch="$(date +%s)"
target_epoch="$(date -d "today ${maintenance_time}" +%s)"
if (( target_epoch <= now_epoch + 30 )); then
  target_epoch="$(date -d "tomorrow ${maintenance_time}" +%s)"
fi
target_iso="$(date -u -d "@${target_epoch}" +%Y-%m-%dT%H:%M:%SZ)"
daily_maintenance_key="$(date -d "@${target_epoch}" +%Y-%m-%d)"
repeat_seconds="$(env_or_file DUNE_DAILY_RESTART_REPEAT_SECONDS)"
repeat_seconds="${repeat_seconds:-600}"
retry_seconds="$(env_or_file DUNE_DAILY_RESTART_RETRY_SECONDS)"
retry_seconds="${retry_seconds:-60}"
retry_window_seconds="$(env_or_file DUNE_DAILY_RESTART_RETRY_WINDOW_SECONDS)"
retry_window_seconds="${retry_window_seconds:-1500}"
if [[ ! "$retry_seconds" =~ ^[1-9][0-9]*$ || "$retry_seconds" -gt 600 ]]; then
  printf 'invalid DUNE_DAILY_RESTART_RETRY_SECONDS: %s\n' "$retry_seconds" >&2
  exit 2
fi
if [[ ! "$retry_window_seconds" =~ ^[1-9][0-9]*$ || "$retry_window_seconds" -gt 3600 ]]; then
  printf 'invalid DUNE_DAILY_RESTART_RETRY_WINDOW_SECONDS: %s\n' "$retry_window_seconds" >&2
  exit 2
fi
require_soft_disconnect="$(env_or_file DUNE_DAILY_RESTART_REQUIRE_SOFT_DISCONNECT)"
require_soft_disconnect="${require_soft_disconnect:-false}"
message="$(env_or_file DUNE_DAILY_RESTART_MESSAGE)"
message="${message:-Daily maintenance restart at 6:00 AM. Please get to a safe place.}"
token="$(read_env DUNE_ADMIN_TOKEN || true)"
require_token="$(read_env DUNE_ADMIN_REQUIRE_TOKEN || true)"
require_readiness="$(read_env DUNE_UPDATE_REQUIRE_READINESS_RECEIPT || true)"
require_readiness="${require_readiness:-true}"
update_policy="$(env_or_file DUNE_DAILY_RESTART_UPDATE_POLICY)"
update_policy="${update_policy:-certified}"
case "$update_policy" in
  current|certified|automatic) ;;
  *)
    printf 'invalid DUNE_DAILY_RESTART_UPDATE_POLICY: %s\n' "$update_policy" >&2
    exit 2
    ;;
esac

if [[ -z "$token" ]]; then
  case "${require_token,,}" in
    1|true|yes|on)
      printf 'DUNE_ADMIN_REQUIRE_TOKEN is enabled but DUNE_ADMIN_TOKEN is empty\n' >&2
      exit 1
      ;;
  esac
fi
if [[ "$update_policy" == automatic ]]; then
  case "${require_readiness,,}" in
    1|true|yes|on)
      printf 'automatic daily updates are blocked while DUNE_UPDATE_REQUIRE_READINESS_RECEIPT=true\n' >&2
      exit 2
      ;;
  esac
fi

auth_args=()
if [[ -n "$token" ]]; then
  auth_args=(-H "Authorization: Bearer $token")
fi
restart_url="http://${admin_host}:${admin_port}/api/ops/restart"

# Check before warning players, then let Admin revalidate immediately before
# any disconnect or stop. An uncertified candidate never prevents the useful
# daily restart; that job is pinned to the currently loaded build instead.
effective_update_policy="$update_policy"
if [[ "$update_policy" == certified ]]; then
  readiness_json=""
  if readiness_json="$(curl -fsS --max-time 180 "${auth_args[@]}" "http://${admin_host}:${admin_port}/api/ops/update-readiness?refresh=true" 2>/dev/null)"; then
    parsed_update_policy=""
    if parsed_update_policy="$(python3 -c '
import json,sys
d=json.load(sys.stdin); e=d.get("evaluation") or {}; c=e.get("candidate") or {}
if c.get("status") == "current" and not c.get("updateRequired"):
    print("current")
elif c.get("updateRequired") and d.get("applyReady") and d.get("currentReceiptReady"):
    print("certified")
else:
    print("current")
' <<<"$readiness_json")"; then
      effective_update_policy="$parsed_update_policy"
    else
      effective_update_policy="current"
      printf 'update readiness response was invalid; daily restart is pinned to the current build\n' >&2
    fi
  else
    effective_update_policy="current"
    printf 'update readiness preflight unavailable; daily restart is pinned to the current build\n' >&2
  fi
  if [[ "$effective_update_policy" == current ]]; then
    printf 'no certified staged update will be applied; daily restart is pinned to the current build\n' >&2
  fi
fi

if [[ "$steam_candidate_requires_certification" == true \
    && "$effective_update_policy" != certified \
    && "$effective_update_policy" != automatic ]]; then
  printf 'fail: a newer Steam server build is staged but not certified; refusing to schedule a current-build restart\n' >&2
  exit 1
fi

body="$(
  python3 - "$target_iso" "$daily_maintenance_key" "$repeat_seconds" "$require_soft_disconnect" "$message" "$effective_update_policy" <<'PY'
import json
import sys

run_at, daily_key, repeat_seconds, require_soft_disconnect, message, update_policy = sys.argv[1:7]
require_soft_disconnect = require_soft_disconnect.strip().lower() in ("1", "true", "yes", "on")
print(json.dumps({
    "target": "all",
    "action": "restart",
    "runAt": run_at,
    "repeat_seconds": int(repeat_seconds),
    "announcement_cadence": [
        {"remaining_seconds": 5 * 60, "interval_seconds": 60},
        {"remaining_seconds": 30 * 60, "interval_seconds": 5 * 60},
    ],
    "message": message,
    "announce": True,
    "execute": True,
    "backup": True,
    "require_soft_disconnect": require_soft_disconnect,
    "update_policy": update_policy,
    "automatic_daily_maintenance": True,
    "daily_maintenance_key": daily_key,
}))
PY
)"

job_matches_state() {
  local state_json="$1"
  python3 -c '
import json, sys
target = float(sys.argv[1])
daily_key = sys.argv[2]
tolerance = float(sys.argv[3])
state = json.load(sys.stdin)
for job in state.get("jobs", []):
    if job.get("status") not in ("scheduled", "executing", "awaiting_reboot"):
        continue
    if job.get("action") != "restart" or job.get("target") != "all":
        continue
    if not job.get("execute") or not job.get("backup"):
        continue
    try:
        run_at = float(job.get("runAt"))
    except (TypeError, ValueError):
        continue
    if job.get("automaticDailyMaintenanceKey") == daily_key or abs(run_at - target) <= tolerance:
        print(json.dumps({"id": job.get("id"), "status": job.get("status"), "runAt": run_at}))
        raise SystemExit(0)
raise SystemExit(1)
' "$target_epoch" "$daily_maintenance_key" "120" <<<"$state_json"
}

verify_scheduled_job() {
  local state_json
  if ! state_json="$(curl --fail-with-body --silent --show-error --max-time 30 "${auth_args[@]}" "$restart_url" 2>/dev/null)"; then
    return 1
  fi
  if job_summary="$(job_matches_state "$state_json" 2>/dev/null)"; then
    printf 'daily maintenance job is persisted: %s\n' "$job_summary"
    return 0
  fi
  return 1
}

schedule_once() {
  local contract_request contract_response contract_required contract_token response
  if verify_scheduled_job; then
    return 0
  fi

  contract_request="$(python3 - "$body" <<'PY'
import json
import sys
print(json.dumps({"targetPath": "/api/ops/restart", "requestBody": json.loads(sys.argv[1])}))
PY
)"
  if ! contract_response="$(curl --fail-with-body --silent --show-error --max-time 60 -H "Content-Type: application/json" -X POST --data "$contract_request" "${auth_args[@]}" "http://${admin_host}:${admin_port}/api/security/change-contract" 2>&1)"; then
    printf 'daily maintenance change-contract preflight failed: %s\n' "$contract_response" >&2
    return 1
  fi
  read -r contract_required contract_token < <(python3 -c '
import json, sys
response=json.load(sys.stdin)
required=bool(response.get("required") and (response.get("contract") or {}).get("governed"))
token=response.get("token") or ""
if required and not token:
    raise SystemExit("change contract is required but the preflight returned no token")
print("true" if required else "false", token)
' <<<"$contract_response")
  args=(-fsS -H "Content-Type: application/json" -X POST --data "$body" "${auth_args[@]}")
  if [[ "$contract_required" == "true" ]]; then
    args+=(-H "X-DASH-Change-Contract: $contract_token")
  fi
  if ! response="$(curl --fail-with-body --silent --show-error --max-time 180 "${args[@]}" "$restart_url" 2>&1)"; then
    printf 'daily maintenance schedule POST failed: %s\n' "$response" >&2
  fi
  if verify_scheduled_job; then
    return 0
  fi
  return 1
}

retry_started="$(date +%s)"
retry_deadline=$((retry_started + retry_window_seconds))
target_deadline=$((target_epoch - 60))
if (( target_deadline < retry_deadline )); then
  retry_deadline="$target_deadline"
fi
if (( retry_deadline <= retry_started )); then
  retry_deadline=$((retry_started + 60))
fi

while true; do
  if schedule_once; then
    printf '\n'
    printf 'daily maintenance update policy: %s\n' "$effective_update_policy"
    exit 0
  fi
  now_epoch="$(date +%s)"
  if (( now_epoch >= retry_deadline )); then
    printf 'daily maintenance job was not persisted before the retry deadline; no silent skip is allowed\n' >&2
    exit 1
  fi
  sleep_for=$retry_seconds
  remaining=$((retry_deadline - now_epoch))
  if (( sleep_for > remaining )); then
    sleep_for="$remaining"
  fi
  sleep "$sleep_for"
done
