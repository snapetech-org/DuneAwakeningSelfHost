#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-${DUNE_ENV_FILE:-.env}}"
container_runtime="${CONTAINER_RUNTIME:-docker}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/snape-curl-auth.sh"
if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

compose=("$container_runtime" compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")

read_env() {
  sed -n "s/^$1=//p" "$env_file" | tail -1
}

truthy() {
  [[ "${1:-}" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ ]]
}

maintenance_marker_host="$(read_env DUNE_AUTOSCALER_MAINTENANCE_MARKER || true)"
case "$maintenance_marker_host" in
  /workspace/*) maintenance_marker_host="$script_dir/../${maintenance_marker_host#/workspace/}" ;;
  "") maintenance_marker_host="$script_dir/../backups/admin-panel/autoscaler-maintenance.lock" ;;
esac
maintenance_marker_created=false

cleanup() {
  local status=$?
  if [[ "$maintenance_marker_created" == true ]]; then
    rm -f -- "$maintenance_marker_host"
  fi
  exit "$status"
}
trap cleanup EXIT

wait_for_autoscaler_idle() {
  local token port url deadline state
  token="$(read_env DUNE_ADMIN_TOKEN || true)"
  port="$(read_env DUNE_ADMIN_HOST_PORT || true)"
  port="${port:-18080}"
  url="http://127.0.0.1:${port}/api/ops/autoscaler"
  deadline=$((SECONDS + ${DUNE_ADMIN_DEPLOY_AUTOSCALER_DRAIN_SECONDS:-300}))

  while (( SECONDS < deadline )); do
    if ! "$container_runtime" inspect dune_server-admin-panel-1 >/dev/null 2>&1 \
       || [[ "$("$container_runtime" inspect -f '{{.State.Running}}' dune_server-admin-panel-1 2>/dev/null || true)" != true ]]; then
      printf 'admin panel is not running; no in-process autoscaler action can be active\n'
      return 0
    fi
    state="$(curl -fsS --max-time 10 -H "Authorization: Bearer $token" "$url" 2>/dev/null \
      | python3 -c 'import json,sys
try:
    value=json.load(sys.stdin)
except Exception:
    print("unknown")
else:
    print("busy" if value.get("running") else "idle")
' || true)"
    if [[ "$state" == idle ]]; then
      printf 'autoscaler is idle with control-plane maintenance marker held\n'
      return 0
    fi
    sleep 1
  done
  printf 'autoscaler did not drain before admin deploy within %ss\n' "${DUNE_ADMIN_DEPLOY_AUTOSCALER_DRAIN_SECONDS:-300}" >&2
  return 1
}

repair_running_map_runtime_patches() {
  local enabled deadline targets container
  enabled="$(read_env DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED || true)"
  truthy "${enabled:-true}" || return 0
  [[ -x "$script_dir/patch-logoff-timers-runtime.sh" ]] || return 0
  deadline=$((SECONDS + ${DUNE_ADMIN_DEPLOY_RUNTIME_PATCH_SECONDS:-180}))

  while (( SECONDS < deadline )); do
    targets=""
    for container in dune_server-survival-1 dune_server-deep-desert-1 dune_server-deep-desert-pvp-1; do
      if "$container_runtime" top "$container" 2>/dev/null | grep -q 'DuneSandboxServer-Linux-Shipping'; then
        targets="${targets:+$targets }$container"
      fi
    done
    if [[ -z "$targets" ]]; then
      printf 'no active logoff-patch map processes require post-deploy repair\n'
      return 0
    fi
    if DUNE_LOGOFF_TIMER_CONTAINERS="$targets" "$script_dir/patch-logoff-timers-runtime.sh" --local \
      && DUNE_LOGOFF_TIMER_CONTAINERS="$targets" "$script_dir/patch-logoff-timers-runtime.sh" --local --dry-run; then
      printf 'verified runtime logoff patch after admin deploy: %s\n' "$targets"
      return 0
    fi
    sleep 5
  done
  printf 'runtime logoff patch did not verify after admin deploy within %ss\n' "${DUNE_ADMIN_DEPLOY_RUNTIME_PATCH_SECONDS:-180}" >&2
  return 1
}

python3 -m py_compile admin/*.py scripts/backup-restore-drill.py scripts/operational-slo.py scripts/capacity-intelligence.py scripts/desired-state.py scripts/change-intelligence.py scripts/deployment-assurance.py scripts/build-cosmetic-catalog.py scripts/admin-chat-commands.py scripts/player-presence-announcer.py
python3 -m unittest scripts/test-cosmetics-admin.py
python3 scripts/test-restore-drill.py
python3 scripts/test-operational-slo.py
python3 scripts/test-capacity-intelligence.py
python3 scripts/test-desired-state.py
python3 scripts/test-change-intelligence.py
python3 scripts/test-deployment-assurance.py
python3 scripts/test-admin-panel-safe-surfaces.py
python3 scripts/test-docker-api-proxy.py
python3 scripts/test-deploy-admin-panel.py

"${compose[@]}" up -d --no-recreate postgres

deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
  status="$("${compose[@]}" ps --format json postgres 2>/dev/null | python3 -c 'import json,sys
text=sys.stdin.read().strip()
if not text:
    print("")
    raise SystemExit
try:
    rows=[json.loads(line) for line in text.splitlines() if line.strip()]
except Exception:
    print("")
    raise SystemExit
print((rows[0].get("Health") or rows[0].get("State") or "").lower() if rows else "")
' || true)"
  if [[ "$status" == *healthy* || "$status" == *running* ]]; then
    break
  fi
  sleep 3
done

mkdir -p -- "$(dirname -- "$maintenance_marker_host")"
printf 'pid=%s started=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$maintenance_marker_host"
maintenance_marker_created=true
wait_for_autoscaler_idle

"${compose[@]}" up -d --no-deps --force-recreate docker-api-broker
broker_id="$("${compose[@]}" ps -q docker-api-broker)"
deadline=$((SECONDS + 60))
broker_status=""
while (( SECONDS < deadline )); do
  broker_status="$("$container_runtime" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$broker_id" 2>/dev/null || true)"
  if [[ "$broker_status" == healthy ]]; then
    break
  fi
  sleep 1
done
if [[ "$broker_status" != healthy ]]; then
  printf 'Docker API broker did not become healthy before admin-panel deploy\n' >&2
  exit 1
fi

"${compose[@]}" up -d --no-deps --force-recreate admin-panel admin-panel-ingress

deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
  status="$("${compose[@]}" ps --format json admin-panel 2>/dev/null | python3 -c 'import json,sys
text=sys.stdin.read().strip()
if not text:
    print("")
    raise SystemExit
try:
    rows=[json.loads(line) for line in text.splitlines() if line.strip()]
except Exception:
    print("")
    raise SystemExit
print((rows[0].get("Health") or rows[0].get("State") or "").lower() if rows else "")
' || true)"
  if [[ "$status" == *healthy* || "$status" == *running* ]]; then
    break
  fi
  sleep 3
done

affinity_enabled="$(sed -n 's/^DUNE_CPU_AFFINITY_ENABLED=//p' "$env_file" | tail -1)"
if [[ "$affinity_enabled" == "true" && -x "$script_dir/cpu-affinity.sh" ]]; then
  "$script_dir/cpu-affinity.sh" --env-file "$env_file" apply --execute \
    --confirm "APPLY DUNE CPU AFFINITY" --persist
fi

./scripts/check-admin-ingress.sh "$env_file"
repair_running_map_runtime_patches
printf 'OK: deployed admin-panel using %s\n' "$env_file"
