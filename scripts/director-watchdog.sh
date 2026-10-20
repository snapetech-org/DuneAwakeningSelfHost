#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="$(printf '%s' "$value" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  printf '%s' "$value"
}

setting() {
  local key="$1" value
  value="$(printenv "$key" 2>/dev/null || true)"
  [[ -n "$value" ]] || value="$(read_env "$key")"
  printf '%s' "$value"
}

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi

interval="$(setting DUNE_DIRECTOR_WATCHDOG_INTERVAL)"
interval="${interval:-15}"
publication_enabled="$(setting DUNE_DIRECTOR_WATCHDOG_PUBLICATION_ENABLED)"
publication_enabled="${publication_enabled:-true}"
publication_interval="$(setting DUNE_DIRECTOR_WATCHDOG_PUBLICATION_INTERVAL)"
publication_interval="${publication_interval:-60}"
publication_failure_threshold="$(setting DUNE_DIRECTOR_WATCHDOG_PUBLICATION_FAILURE_THRESHOLD)"
publication_failure_threshold="${publication_failure_threshold:-3}"
recovery_cooldown="$(setting DUNE_DIRECTOR_WATCHDOG_RECOVERY_COOLDOWN)"
recovery_cooldown="${recovery_cooldown:-300}"
recovery_timeout="$(setting DUNE_DIRECTOR_WATCHDOG_RECOVERY_TIMEOUT)"
recovery_timeout="${recovery_timeout:-600}"
director="$(setting DUNE_DIRECTOR_CONTAINER)"
director="${director:-dune_server-director-1}"
rmq="$(setting DUNE_DIRECTOR_RMQ_CONTAINER)"
rmq="${rmq:-dune_server-game-rmq-1}"
lock_file="$(setting DUNE_DIRECTOR_WATCHDOG_LOCK_FILE)"
lock_file="${lock_file:-/tmp/dune-director-watchdog.lock}"
runtime="$(printenv CONTAINER_RUNTIME 2>/dev/null || printf docker)"

# Child health probes run repeatedly. Resolve only their non-secret settings
# once here so they do not reopen the credential-bearing Compose dotenv file
# on every probe.
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(setting COMPOSE_PROJECT_NAME)}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-dune_server}"
DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS="${DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS:-$(setting DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS)}"
export DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS="${DUNE_CONTROL_PLANE_REPAIR_TIMEOUT_SECONDS:-90}"
DUNE_CONTROL_PLANE_DB_HOST="${DUNE_CONTROL_PLANE_DB_HOST:-$(setting DUNE_CONTROL_PLANE_DB_HOST)}"
export DUNE_CONTROL_PLANE_DB_HOST="${DUNE_CONTROL_PLANE_DB_HOST:-postgres}"
DUNE_CONTROL_PLANE_DB_PORT="${DUNE_CONTROL_PLANE_DB_PORT:-$(setting DUNE_CONTROL_PLANE_DB_PORT)}"
export DUNE_CONTROL_PLANE_DB_PORT="${DUNE_CONTROL_PLANE_DB_PORT:-5432}"
DUNE_CONTROL_PLANE_DB_USER="${DUNE_CONTROL_PLANE_DB_USER:-$(setting DUNE_CONTROL_PLANE_DB_USER)}"
export DUNE_CONTROL_PLANE_DB_USER="${DUNE_CONTROL_PLANE_DB_USER:-dune}"
DUNE_CONTROL_PLANE_PROBE_SERVICES="${DUNE_CONTROL_PLANE_PROBE_SERVICES:-$(setting DUNE_CONTROL_PLANE_PROBE_SERVICES)}"
export DUNE_CONTROL_PLANE_PROBE_SERVICES="${DUNE_CONTROL_PLANE_PROBE_SERVICES:-survival,deep-desert,deep-desert-pvp}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-$(setting POSTGRES_CONTAINER)}"
export POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-dune_server-postgres-1}"

if [[ ! "$interval" =~ ^[1-9][0-9]*$ || ! "$publication_interval" =~ ^[1-9][0-9]*$ \
    || ! "$publication_failure_threshold" =~ ^[1-9][0-9]*$ \
    || ! "$recovery_cooldown" =~ ^[0-9]+$ || ! "$recovery_timeout" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Director watchdog intervals, threshold, cooldown, and timeout must be positive integers\n' >&2
  exit 2
fi

if command -v flock >/dev/null 2>&1; then
  exec 9>"$lock_file"
  flock -n 9 || exit 0
fi

purge_login_queue() {
  "$runtime" exec "$rmq" rabbitmqctl purge_queue loginRequests >/dev/null 2>&1 || true
}

run_bounded() {
  local timeout_seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5s "${timeout_seconds}s" "$@"
  else
    "$@"
  fi
}

publication_health_script="$script_dir/fls-publication-health.py"
control_plane_script="$script_dir/control-plane-health.sh"
publication_recovery_script="$script_dir/recover-publication.sh"
last_publication_check=0
publication_failures=0
last_recovery=0

recover_publication() {
  local now="$1"
  if (( last_recovery > 0 && now - last_recovery < recovery_cooldown )); then
    printf '%s publication recovery suppressed by cooldown (%ss remaining)\n' \
      "$(date -u +%FT%TZ)" "$((recovery_cooldown - (now - last_recovery)))" >&2
    return 1
  fi
  if [[ ! -x "$publication_recovery_script" ]]; then
    printf '%s publication recovery helper is missing: %s\n' \
      "$(date -u +%FT%TZ)" "$publication_recovery_script" >&2
    return 1
  fi
  last_recovery="$now"
  printf '%s recovering Director/Gateway publication path\n' "$(date -u +%FT%TZ)" >&2
  if run_bounded "$recovery_timeout" "$publication_recovery_script" "$env_file"; then
    publication_failures=0
    printf '%s Director/Gateway publication recovery completed\n' "$(date -u +%FT%TZ)" >&2
    return 0
  fi
  printf '%s Director/Gateway publication recovery failed; watchdog will retry after cooldown\n' \
    "$(date -u +%FT%TZ)" >&2
  return 1
}

publication_is_healthy() {
  [[ -x "$publication_health_script" ]] || return 1
  if [[ -x "$control_plane_script" ]]; then
    if ! run_bounded 90 "$control_plane_script" "$env_file" --allow-no-map --quiet; then
      run_bounded 90 "$control_plane_script" "$env_file" --allow-no-map --repair --quiet || return 1
    fi
  fi
  run_bounded 90 python3 "$publication_health_script" "$env_file" \
    --compose-files "${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}" \
    --json >/dev/null
}

check_publication() {
  local now="$1" rc
  case "$publication_enabled" in
    1|true|yes|on|TRUE|True|YES|ON) ;;
    *) return 0 ;;
  esac
  if (( last_publication_check > 0 && now - last_publication_check < publication_interval )); then
    return 0
  fi
  last_publication_check="$now"

  set +e
  publication_is_healthy
  rc=$?
  set -e
  if (( rc == 0 )); then
    if (( publication_failures > 0 )); then
      printf '%s FLS publication health recovered\n' "$(date -u +%FT%TZ)" >&2
    fi
    publication_failures=0
    return 0
  fi

  publication_failures=$((publication_failures + 1))
  printf '%s FLS publication health failed (%s/%s)\n' \
    "$(date -u +%FT%TZ)" "$publication_failures" "$publication_failure_threshold" >&2
  if (( publication_failures >= publication_failure_threshold )); then
    recover_publication "$now" || true
    publication_failures=0
  fi
}

while :; do
  now="$(date +%s)"
  state="$("$runtime" inspect -f '{{.State.Status}}' "$director" 2>/dev/null || printf missing)"
  if [[ "$state" != running ]]; then
    printf '%s Director state=%s; purging loginRequests before controlled recovery\n' \
      "$(date -u +%FT%TZ)" "$state" >&2
    purge_login_queue
    recover_publication "$now" || true
  else
    check_publication "$now" || true
  fi
  sleep "$interval"
done
