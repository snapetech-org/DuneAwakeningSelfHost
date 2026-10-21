#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/brt-dd-live-readiness.sh COMMAND [ENV_FILE] [CONFIRM]

Commands:
  preflight             Read-only checks before the downtime window.
  restart-deep-desert  Restart only the live Deep Desert partition.
  verify-after-restart Read-only DB/container checks after restart.
  verify-brt-stack     Verify overlay, DB shim, rollback plan, self-test, and canary.
  logs                 Print recent high-signal Deep Desert BRT/building logs.
  checklist            Print the next-downtime operator checklist.

The restart command is a production mutation. It refuses to run unless hostname
is kspls0 and CONFIRM is exactly: RESTART DEEP DESERT BRT
USAGE
}

cmd="${1:-}"
env_file="${2:-${ENV_FILE:-.env}}"
confirm="${3:-${CONFIRM:-}}"
db="${DUNE_DB_NAME:-}"
runtime="${CONTAINER_RUNTIME:-docker}"
service="${DUNE_BRT_DD_LIVE_SERVICE:-deep-desert}"
partition_id="${DUNE_BRT_DD_LIVE_PARTITION_ID:-8}"
required_host="${DUNE_BRT_DD_LIVE_HOST:-kspls0}"
confirm_phrase="${DUNE_BRT_DD_LIVE_CONFIRM:-RESTART DEEP DESERT BRT}"
wait_seconds="${DUNE_BRT_DD_RESTART_WAIT_SECONDS:-300}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target_config="config/UserGame.deep-desert-coriolis.ini"
copied_config="/home/dune/server/DuneSandbox/Saved/UserSettings/UserGame.ini"
target_custom_config="config/UserServerCustomSettings.ini"
copied_custom_config="/home/dune/server/DuneSandbox/Saved/UserSettings/ServerCustomSettings.ini"

required_config_patterns=(
  'm_MaxLandclaimSegmentsPerMap=.*DeepDesert'
  'm_MaxLandclaimSegmentsPerMap=.*DeepDesert_1'
  'm_BaseBackupToolMapRestriction=.*DeepDesert'
  'm_BaseBackupToolMapRestriction=.*DeepDesert_1'
)

required_custom_config_patterns=(
  'bIsBuildingRestrictionsEnabled=False'
)

if [[ -z "$cmd" || "$cmd" == "-h" || "$cmd" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "$env_file" ]]; then
  printf 'missing env file: %s\n' "$env_file" >&2
  exit 2
fi

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "", $0)
      print $0
    }
  ' "$env_file" | tail -n 1
}

if [[ -z "$db" ]]; then
  db="$(env_value DUNE_GAME_DB_NAME)"
  db="${db:-dune_sb_1_4_5_0}"
fi

if [[ -z "${WORLD_DATACENTER_ID:-}" ]]; then
  WORLD_DATACENTER_ID="$(env_value WORLD_DATACENTER_ID)"
  if [[ -z "$WORLD_DATACENTER_ID" ]]; then
    WORLD_DATACENTER_ID="$(env_value WORLD_REGION)"
  fi
  export WORLD_DATACENTER_ID
fi

compose_files="$("$script_dir/compose-files.sh" "$env_file")"
compose=("$runtime" compose)
IFS=':' read -ra compose_file_array <<< "$compose_files"
for compose_file in "${compose_file_array[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")

run_compose() {
  "${compose[@]}" "$@"
}

host_name() {
  hostname 2>/dev/null || true
}

host_short() {
  hostname -s 2>/dev/null || host_name
}

host_matches_required() {
  local actual="$1"
  [[ "$actual" == "$required_host" || "$actual" == "$required_host."* ]]
}

assert_live_host_for_mutation() {
  local actual short
  actual="$(host_name)"
  short="$(host_short)"
  if ! host_matches_required "$actual" && ! host_matches_required "$short"; then
    printf 'refusing production mutation: hostname is %s, required %s\n' "${actual:-unknown}" "$required_host" >&2
    printf 'connect to %s and rerun from there\n' "$required_host" >&2
    exit 1
  fi
  if [[ "$confirm" != "$confirm_phrase" ]]; then
    printf 'refusing restart: CONFIRM must be exactly: %s\n' "$confirm_phrase" >&2
    exit 2
  fi
}

check_repo_config() {
  local rendered custom_rendered pattern
  if [[ ! -f "$target_config" ]]; then
    printf 'missing target config: %s\n' "$target_config" >&2
    return 1
  fi

  rendered="$(cat "$target_config")"
  for pattern in "${required_config_patterns[@]}"; do
    if ! grep -Eq "$pattern" <<<"$rendered"; then
      printf 'missing repo config pattern in %s: %s\n' "$target_config" "$pattern" >&2
      return 1
    fi
  done
  if [[ ! -f "$target_custom_config" ]]; then
    printf 'missing target custom config: %s\n' "$target_custom_config" >&2
    return 1
  fi
  custom_rendered="$(cat "$target_custom_config")"
  for pattern in "${required_custom_config_patterns[@]}"; do
    if ! grep -Eq "$pattern" <<<"$custom_rendered"; then
      printf 'missing repo custom config pattern in %s: %s\n' "$target_custom_config" "$pattern" >&2
      return 1
    fi
  done
  printf 'repo_config=%s contains DeepDesert and DeepDesert_1 landclaim/BRT map entries and %s contains replicated BRT restriction settings\n' \
    "$target_config" "$target_custom_config"
}

check_compose_surface() {
  local rendered
  run_compose config --quiet
  rendered="$(run_compose config "$service")"

  grep -Fq 'DUNE_USERGAME_CONFIG_PATH: /workspace/config/UserGame.deep-desert-coriolis.ini' <<<"$rendered" \
    || { printf 'compose service %s does not use %s\n' "$service" "$target_config" >&2; return 1; }
  grep -Fq '/Game/Dune/Maps/Arrakis/DeepDesert_1/DeepDesert_1.DeepDesert_1' <<<"$rendered" \
    || { printf 'compose service %s does not launch DeepDesert_1\n' "$service" >&2; return 1; }
  grep -Fq -- '-PartitionIndex=8' <<<"$rendered" \
    || { printf 'compose service %s does not use partition %s\n' "$service" "$partition_id" >&2; return 1; }
  grep -Fq 'DUNE_SERVER_CUSTOM_SETTINGS_PATH: /workspace/config/UserServerCustomSettings.ini' <<<"$rendered" \
    || { printf 'compose service %s does not use %s\n' "$service" "$target_custom_config" >&2; return 1; }

  printf 'compose_service=%s uses DeepDesert_1 partition %s, %s, and %s\n' \
    "$service" "$partition_id" "$target_config" "$target_custom_config"
}

postgres_id() {
  run_compose ps -q postgres 2>/dev/null || true
}

container_id() {
  run_compose ps -q "$service" 2>/dev/null || true
}

psql_select() {
  run_compose exec -T postgres psql -U dune -d "$db" -Atc "$1"
}

verify_db_partition() {
  local mode="${1:-optional}"
  local postgres row
  postgres="$(postgres_id)"
  if [[ -z "$postgres" ]]; then
    if [[ "$mode" == "optional" ]]; then
      printf 'postgres is not running; DB partition check deferred\n'
      return 0
    fi
    printf 'postgres is not running; DB partition check failed\n' >&2
    return 1
  fi

  row="$(psql_select "
    select wp.partition_id || '|' || wp.map || '|' || wp.dimension_index || '|' ||
           coalesce(wp.label, '') || '|' || coalesce(wp.server_id, '') || '|' ||
           coalesce(fs.ready::text, '') || '|' || coalesce(fs.alive::text, '')
    from dune.world_partition wp
    left join dune.farm_state fs on fs.server_id = wp.server_id
    where wp.partition_id = ${partition_id};
  ")"
  printf 'partition_%s=%s\n' "$partition_id" "${row:-missing}"
  [[ "$row" == ${partition_id}\|DeepDesert_1\|* ]]
}

verify_copied_config() {
  local mode="${1:-optional}"
  local id rendered custom_rendered pattern
  id="$(container_id)"
  if [[ -z "$id" ]]; then
    if [[ "$mode" == "optional" ]]; then
      printf '%s is not running; copied config check deferred until after restart\n' "$service"
      return 0
    fi
    printf '%s is not running; copied config check failed\n' "$service" >&2
    return 1
  fi

  rendered="$("$runtime" exec "$id" sh -lc "test -f '$copied_config' && cat '$copied_config'")"
  for pattern in "${required_config_patterns[@]}"; do
    if ! grep -Eq "$pattern" <<<"$rendered"; then
      printf 'missing copied config pattern in %s: %s\n' "$copied_config" "$pattern" >&2
      return 1
    fi
  done
  custom_rendered="$("$runtime" exec "$id" sh -lc "test -f '$copied_custom_config' && cat '$copied_custom_config'")"
  for pattern in "${required_custom_config_patterns[@]}"; do
    if ! grep -Eq "$pattern" <<<"$custom_rendered"; then
      printf 'missing copied custom config pattern in %s: %s\n' "$copied_custom_config" "$pattern" >&2
      return 1
    fi
  done
  printf 'copied_config=%s contains DeepDesert landclaim/BRT map entries and %s contains replicated BRT restriction settings\n' \
    "$copied_config" "$copied_custom_config"
}

preflight() {
  printf 'host=%s required_live_host=%s\n' "$(host_name)" "$required_host"
  if ! host_matches_required "$(host_short)"; then
    printf 'read_only=true; restart command will refuse here\n'
  fi
  check_repo_config
  check_compose_surface
  verify_db_partition optional
  verify_copied_config optional
}

verify_after_restart() {
  check_repo_config
  check_compose_surface
  verify_db_partition required
  verify_copied_config required
}

verify_overlay() {
  local id host_pak container_pak host_sha container_sha
  host_pak="${DUNE_BRT_DD_OVERLAY_PAK:-backups/operations/brt-dd-overlay/pakchunk9999-LinuxServer.pak}"
  container_pak="${DUNE_BRT_DD_CONTAINER_OVERLAY_PAK:-/home/dune/server/DuneSandbox/Content/Paks/pakchunk9999-LinuxServer.pak}"

  "$script_dir/verify-brt-dd-overlay-pak.sh" "$host_pak"
  host_sha="$(sha256sum "$host_pak" | awk '{print $1}')"

  id="$(container_id)"
  if [[ -z "$id" ]]; then
    printf '%s is not running; live overlay checksum check failed\n' "$service" >&2
    return 1
  fi
  container_sha="$("$runtime" exec "$id" sha256sum "$container_pak" | awk '{print $1}')"
  printf 'overlay_host_sha256=%s\n' "$host_sha"
  printf 'overlay_container_sha256=%s\n' "$container_sha"
  [[ "$host_sha" == "$container_sha" ]] || {
    printf 'overlay checksum mismatch: host=%s container=%s\n' "$host_sha" "$container_sha" >&2
    return 1
  }
}

verify_brt_stack() {
  printf 'host=%s required_live_host=%s db=%s service=%s partition=%s\n' "$(host_name)" "$required_host" "$db" "$service" "$partition_id"
  verify_db_partition required
  verify_copied_config required
  verify_overlay
  "$script_dir/brt-dd-db-metadata-map-shim.sh" status "$env_file"
  "$script_dir/brt-dd-db-metadata-map-shim.sh" rollback-plan "$env_file"
  "$script_dir/brt-dd-db-metadata-map-shim.sh" self-test "$env_file"
  "$script_dir/brt-dd-live-canary.sh" arm "$env_file"
  "$script_dir/brt-dd-live-canary.sh" collect "$env_file"
}

restart_deep_desert() {
  assert_live_host_for_mutation
  printf 'pausing map watchdog before %s restart\n' "$service"
  "$script_dir/map-watchdog-control.sh" pause "$env_file" || true
  trap '"$script_dir/map-watchdog-control.sh" resume "$env_file" || true' EXIT

  "$script_dir/recover-map.sh" "$env_file" "$service" "$partition_id" "$wait_seconds"
  verify_after_restart
}

show_logs() {
  run_compose logs --since="${DUNE_BRT_DD_LIVE_LOG_SINCE:-45m}" --tail="${DUNE_BRT_DD_LIVE_LOG_TAIL:-1600}" "$service" 2>&1 \
    | rg -n "BaseBackup|BuildingBlueprintBackupTool|CanBePlaced|Landclaim|BuildableMapRegion|BuildingRestriction|DeepDesert|Server farm is READY|READY|error|failed|Exception" -i \
    || true
}

print_checklist() {
  cat <<USAGE
Next downtime BRT/DD rollout:

1. On kspls0, confirm the host:
   hostname

2. Run read-only preflight:
   make brt-dd-live-preflight ENV_FILE=.env

3. During downtime, restart only Deep Desert:
   make brt-dd-live-restart ENV_FILE=.env CONFIRM='${confirm_phrase}'

4. Verify the restarted service copied the updated config:
   make brt-dd-live-verify ENV_FILE=.env

5. Have a trusted tester try BRT restore in Deep Desert away from borders,
   POIs, resource fields, and blocking volumes.

6. While they test, watch high-signal logs:
   make brt-dd-live-logs ENV_FILE=.env

Rollback is to restore the previous ${target_config}
m_MaxLandclaimSegmentsPerMap and m_BaseBackupToolMapRestriction lines, then rerun step 3.
USAGE
}

case "$cmd" in
  preflight)
    preflight
    ;;
  restart-deep-desert)
    restart_deep_desert
    ;;
  verify-after-restart)
    verify_after_restart
    ;;
  verify-brt-stack)
    verify_brt_stack
    ;;
  logs)
    show_logs
    ;;
  checklist)
    print_checklist
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
