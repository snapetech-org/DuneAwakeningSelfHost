#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/live-target-safety-audit.sh [ENV_FILE]

Read-only live audit for the experimental targets we currently depend on:
Deep Desert state, BRT restore canary safety, building-piece/subfief cap
patches, Landsraad/Coriolis guards, rollback backups, and watchdog health.

Defaults:
  DUNE_TARGET_AUDIT_REQUIRED_HOST=kspls0
  DUNE_TARGET_AUDIT_PATCH_PROBE_SERVICES=all
  DUNE_TARGET_AUDIT_PATCH_PROBE_EXCLUDE_SERVICES=
  DUNE_TARGET_AUDIT_BRT_BACKUP_DIR=<latest backups/manual/dd-pre-restore-*>
  DUNE_TARGET_AUDIT_BRT_BACKUP_ID=<manifest brt_backup_id or brt-source-backup-id.txt>
  DUNE_TARGET_AUDIT_ALLOW_ANY_HOST=0
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

env_file="${1:-${ENV_FILE:-.env}}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

if [[ ! -f "$env_file" ]]; then
  printf 'missing env file: %s\n' "$env_file" >&2
  exit 2
fi

required_host="${DUNE_TARGET_AUDIT_REQUIRED_HOST:-kspls0}"
allow_any_host="${DUNE_TARGET_AUDIT_ALLOW_ANY_HOST:-0}"
container_runtime="${CONTAINER_RUNTIME:-docker}"
patch_probe_services="${DUNE_TARGET_AUDIT_PATCH_PROBE_SERVICES:-all}"
patch_probe_exclude_services="${DUNE_TARGET_AUDIT_PATCH_PROBE_EXCLUDE_SERVICES:-}"

failures=0
warnings=0
cleanup_audit_tmp_dir=false
if [[ -n "${DUNE_TARGET_AUDIT_TMP_DIR:-}" ]]; then
  audit_tmp_dir="$DUNE_TARGET_AUDIT_TMP_DIR"
  mkdir -p "$audit_tmp_dir"
else
  audit_tmp_dir="$(mktemp -d -t dune-target-audit.XXXXXX)"
  cleanup_audit_tmp_dir=true
fi
if [[ "$cleanup_audit_tmp_dir" == "true" ]]; then
  trap 'rm -rf "$audit_tmp_dir"' EXIT
fi

tmp_path() {
  printf '%s/%s\n' "$audit_tmp_dir" "$1"
}

section() {
  printf '\n== %s ==\n' "$1"
}

ok() {
  printf 'OK   %s\n' "$*"
}

warn() {
  warnings=$((warnings + 1))
  printf 'WARN %s\n' "$*"
}

fail() {
  failures=$((failures + 1))
  printf 'FAIL %s\n' "$*" >&2
}

indent() {
  sed 's/^/  /'
}

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

env_or_file() {
  local key="$1" default_value="${2:-}" value
  value="${!key:-}"
  if [[ -z "$value" ]]; then
    value="$(read_env "$key")"
  fi
  printf '%s' "${value:-$default_value}"
}

require_env_value() {
  local key="$1" expected="$2" actual
  actual="$(env_or_file "$key")"
  if [[ "$actual" == "$expected" ]]; then
    ok "$key=$actual"
  else
    fail "$key=$actual expected=$expected"
  fi
}

require_env_nonempty() {
  local key="$1" actual
  actual="$(env_or_file "$key")"
  if [[ -n "$actual" ]]; then
    ok "$key is set"
  else
    fail "$key is empty"
  fi
}

host_short="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
if [[ "$allow_any_host" != "1" && -n "$required_host" && "$host_short" != "$required_host" ]]; then
  fail "host=$host_short required=$required_host"
else
  ok "host=$host_short"
fi

compose_files="$("$script_dir/compose-files.sh" "$env_file")"
compose=("$container_runtime" compose)
IFS=':' read -ra compose_file_array <<< "$compose_files"
for compose_file in "${compose_file_array[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")
export COMPOSE_FILES="$compose_files"
map_catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$map_catalog_script" ]]; then
  fail "map catalog helper is missing: $map_catalog_script"
  exit 1
fi
map_partitions_output="$("$map_catalog_script" "$env_file" --field map-partitions --write-cache)"
if [[ -z "$map_partitions_output" ]]; then
  fail "map catalog returned no workers"
  exit 1
fi
mapfile -t MAP_PARTITIONS <<< "$map_partitions_output"
map_profiles="$("$map_catalog_script" "$env_file" --field profiles)"
for profile in $map_profiles; do
  compose+=(--profile "$profile")
done

db="$(env_or_file DUNE_GAME_DB_NAME)"
db="${db:-$(env_or_file DUNE_DATABASE)}"
db="${db:-$(env_or_file DUNE_DB_NAME dune_sb_1_4_0_0)}"

psql_cmd() {
  "${compose[@]}" exec -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 -P pager=off "$@"
}

psql_at() {
  psql_cmd -qAtc "$1"
}

latest_backup_dir() {
  find backups/manual -maxdepth 1 -type d -name 'dd-pre-restore-*' 2>/dev/null \
    ! -exec test -e '{}/INCOMPLETE.txt' ';' -print \
    | sort \
    | tail -1
}

manifest_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

container_id() {
  "${compose[@]}" ps -q "$1" 2>/dev/null || true
}

MAP_SERVICES=()
for item in "${MAP_PARTITIONS[@]}"; do
  MAP_SERVICES+=("${item%%:*}")
done

declare -A REQUIRED_MAPS=()
autoscaler_enabled="$(env_or_file DUNE_AUTOSCALER_ENABLED false)"
if [[ ! "$autoscaler_enabled" =~ ^(1|true|yes|on)$ ]]; then
  for service in "${MAP_SERVICES[@]}"; do REQUIRED_MAPS["$service"]=1; done
else
  required_csv="$(env_or_file DUNE_AUTOSCALER_ALWAYS_ON_SERVICES survival,overmap),$(env_or_file DUNE_AUTOSCALER_SIMULATION_REQUIRED_SERVICES survival)"
  IFS=',' read -ra required_services <<< "$required_csv"
  for service in "${required_services[@]}"; do
    service="${service//[[:space:]]/}"
    [[ -n "$service" ]] && REQUIRED_MAPS["$service"]=1
  done
  autoscaler_state_file="$(env_or_file DUNE_AUTOSCALER_STATE_FILE backups/admin-panel/autoscaler.json)"
  if [[ "$autoscaler_state_file" != /* ]]; then autoscaler_state_file="$repo_root/$autoscaler_state_file"; fi
  if [[ -f "$autoscaler_state_file" ]]; then
    while IFS= read -r service; do [[ -n "$service" ]] && REQUIRED_MAPS["$service"]=1; done < <(
      python3 - "$autoscaler_state_file" <<'PY'
import json, sys
try:
    state=json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)
for service, mode in (state.get("modes") or {}).items():
    if mode == "always-on":
        print(service)
PY
    )
  fi
fi

required_partition_csv=""
for item in "${MAP_PARTITIONS[@]}"; do
  service="${item%%:*}"
  partition="${item##*:}"
  if [[ -n "${REQUIRED_MAPS[$service]:-}" ]]; then
    required_partition_csv+="${required_partition_csv:+,}$partition"
  fi
done
required_partition_csv="${required_partition_csv:-0}"

map_required() {
  [[ -n "${REQUIRED_MAPS[$1]:-}" ]]
}

declare -A PATCH_PROBE_EXCLUDE=()
if [[ -n "$patch_probe_exclude_services" ]]; then
  IFS=',' read -ra excluded_probe_services <<< "$patch_probe_exclude_services"
  for service in "${excluded_probe_services[@]}"; do
    service="${service//[[:space:]]/}"
    [[ -n "$service" ]] || continue
    PATCH_PROBE_EXCLUDE["$service"]=1
  done
fi

patch_probe_service_list() {
  local service
  if [[ "${patch_probe_services//[[:space:]]/}" == "all" ]]; then
    for service in "${MAP_SERVICES[@]}"; do
      [[ -n "${PATCH_PROBE_EXCLUDE[$service]:-}" ]] && continue
      printf '%s\n' "$service"
    done
    return
  fi

  IFS=',' read -ra requested_probe_services <<< "$patch_probe_services"
  for service in "${requested_probe_services[@]}"; do
    service="${service//[[:space:]]/}"
    [[ -n "$service" ]] || continue
    [[ -n "${PATCH_PROBE_EXCLUDE[$service]:-}" ]] && continue
    printf '%s\n' "$service"
  done
}

service_health() {
  local service="$1" cid status health
  cid="$(container_id "$service")"
  if [[ -z "$cid" ]]; then
    fail "$service container missing"
    return
  fi
  status="$("$container_runtime" inspect "$cid" --format '{{ .State.Status }}' 2>/dev/null || true)"
  health="$("$container_runtime" inspect "$cid" --format '{{ if .State.Health }}{{ .State.Health.Status }}{{ end }}' 2>/dev/null || true)"
  if [[ "$status" == "running" && ( -z "$health" || "$health" == "healthy" ) ]]; then
    ok "$service status=$status health=${health:-none}"
  else
    fail "$service status=${status:-unknown} health=${health:-none}"
  fi
}

section "Compose And Services"
printf 'compose_files=%s\n' "$compose_files" | indent
compose_config_out="$(tmp_path live-target-compose-config.out)"
if "${compose[@]}" config --quiet >"$compose_config_out" 2>&1; then
  ok "compose config renders"
else
  fail "compose config failed"
  cat "$compose_config_out" | indent
fi

for unit in dune-map-watchdog.service dune-player-presence-announcer.service render-dune-static-status.timer; do
  if systemctl --quiet is-active "$unit"; then
    ok "$unit active"
  else
    fail "$unit inactive"
  fi
done
service_health admin-chat-commands

section "Landsraad And Deep Desert Config"
coriolis_out="$(tmp_path live-target-coriolis.out)"
if "$script_dir/validate-landsraad-coriolis-cycle.sh" "$env_file" >"$coriolis_out" 2>&1; then
  ok "Landsraad Coriolis weekly/destructive guard"
else
  fail "Landsraad Coriolis weekly/destructive guard"
fi
cat "$coriolis_out" | indent

if [[ "$(env_or_file DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED true)" == "true" ]]; then
  term_align_out="$(tmp_path live-target-term-align.out)"
  if "$script_dir/validate-landsraad-term-coriolis-alignment.sh" "$env_file" >"$term_align_out" 2>&1; then
    ok "Landsraad active-term Coriolis alignment"
  else
    fail "Landsraad active-term Coriolis alignment"
  fi
  cat "$term_align_out" | indent
else
  warn "DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED is not true"
fi

brt_readiness_out="$(tmp_path live-target-brt-readiness.out)"
if "$script_dir/brt-dd-live-readiness.sh" preflight "$env_file" >"$brt_readiness_out" 2>&1; then
  ok "DD/BRT repo, compose, and copied config readiness"
else
  fail "DD/BRT repo, compose, and copied config readiness"
fi
cat "$brt_readiness_out" | tail -20 | indent

section "Database Health"
db_health="$(psql_at "
select
  (select count(*) from dune.world_partition) || '|' ||
  (select count(*) from dune.world_partition wp join dune.active_server_ids asi on asi.server_id=wp.server_id where wp.partition_id in ($required_partition_csv)) || '|' ||
  (select count(*)
   from dune.world_partition wp
   join dune.farm_state fs on fs.server_id=wp.server_id
   join dune.active_server_ids asi on asi.server_id=wp.server_id
   where fs.alive and wp.partition_id in ($required_partition_csv));
")"
IFS='|' read -r partition_count active_count ready_alive_count <<< "$db_health"
required_count="${#REQUIRED_MAPS[@]}"
if [[ "$required_count" == "$active_count" && "$required_count" == "$ready_alive_count" ]]; then
  ok "lifecycle-required farm alive/active ${ready_alive_count}/${required_count}; configured partitions=$partition_count"
else
  fail "farm health required=$required_count partitions=$partition_count active=$active_count ready_alive=$ready_alive_count"
fi

dd_rows="$(psql_at "
select
  wp.partition_id || '|' || wp.map || '|' || wp.dimension_index || '|' ||
  coalesce(wp.label, '') || '|' || coalesce(fs.connected_players, 0) || '|' ||
  coalesce(fs.ready, false) || '|' || coalesce(fs.alive, false) || '|' ||
  (asi.server_id is not null)
from dune.world_partition wp
left join dune.farm_state fs on fs.server_id=wp.server_id
left join dune.active_server_ids asi on asi.server_id=wp.server_id
where wp.partition_id in (8, 31)
order by wp.partition_id;
")"
printf '%s\n' "$dd_rows" | indent
if grep -q '^8|DeepDesert_1|0|' <<< "$dd_rows" && grep -q '^31|DeepDesert_1|1|' <<< "$dd_rows"; then
  ok "DD1/DD2 partition identity"
else
  fail "DD1/DD2 partition identity mismatch"
fi
while IFS='|' read -r partition map dimension label players ready alive active; do
  [[ -n "$partition" ]] || continue
  service="deep-desert"; [[ "$partition" == 31 ]] && service="deep-desert-pvp"
  if map_required "$service"; then
    if [[ "$alive" =~ ^(t|true)$ && "$active" =~ ^(t|true)$ ]]; then
      ok "$service lifecycle-required and alive/active (ready flag=${ready:-false})"
    else
      fail "$service lifecycle-required but not ready/alive/active"
    fi
  else
    ok "$service is on-demand; stopped state is policy-compliant"
  fi
done <<< "$dd_rows"

section "BRT Restore Canary"
require_env_value DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ENABLED true
require_env_value DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_DRY_RUN false
require_env_value DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_COPY_SOURCE_ENABLED true
require_env_nonempty DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ALLOWED_PLAYER_IDS
require_env_value DUNE_CHAT_COMMAND_DD1_RESTORE_MAX_DD_CONNECTED_PLAYERS 1

backup_dir="${DUNE_TARGET_AUDIT_BRT_BACKUP_DIR:-$(latest_backup_dir)}"
if [[ -n "$backup_dir" && -d "$backup_dir" ]]; then
  ok "rollback backup dir=$backup_dir"
  verify_backup_out="$(tmp_path live-target-verify-backup.out)"
  if "$script_dir/verify-backup.sh" "$backup_dir" >"$verify_backup_out" 2>&1; then
    ok "rollback backup structural verification"
  else
    fail "rollback backup structural verification"
  fi
  cat "$verify_backup_out" | tail -12 | indent
else
  fail "no complete dd-pre-restore backup directory found"
fi

brt_backup_id="${DUNE_TARGET_AUDIT_BRT_BACKUP_ID:-}"
player_id="$(env_or_file DUNE_TARGET_AUDIT_RESTORE_PLAYER_ID "$(env_or_file DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ALLOWED_PLAYER_IDS)")"
player_id="${player_id%%,*}"
if [[ -z "$brt_backup_id" && -n "$backup_dir" ]]; then
  brt_backup_id="$(manifest_value "$backup_dir/manifest.txt" brt_backup_id)"
fi
if [[ -z "$brt_backup_id" && -n "$backup_dir" && -f "$backup_dir/brt-source-backup-id.txt" ]]; then
  brt_backup_id="$(tr -dc '0-9' < "$backup_dir/brt-source-backup-id.txt")"
fi
brt_available_count="unknown"
if [[ -z "$brt_backup_id" && -n "$player_id" ]]; then
  brt_list_out="$(tmp_path live-target-brt-list.json)"
  if "$script_dir/dd1-brt-emulator.py" list-backups --player-id "$player_id" >"$brt_list_out" 2>&1; then
    read -r brt_available_count discovered_backup_id < <(python3 - "$brt_list_out" <<'PY'
import json, sys
document=json.load(open(sys.argv[1], encoding="utf-8"))
rows=document.get("backups") or []
ids=[str(row.get("id")) for row in rows if row.get("id") is not None]
print(len(rows), ids[0] if len(ids) == 1 else "")
PY
    )
    if [[ -n "$discovered_backup_id" ]]; then
      brt_backup_id="$discovered_backup_id"
      ok "resolved the only available BRT backup for restore player $player_id"
    fi
  else
    fail "could not enumerate BRT backups for restore player $player_id"
    cat "$brt_list_out" | indent
  fi
fi

if [[ -n "$brt_backup_id" ]]; then
  brt_inspect_out="$(tmp_path live-target-brt-inspect.json)"
  if "$script_dir/dd1-brt-emulator.py" inspect-backup --backup-id "$brt_backup_id" >"$brt_inspect_out" 2>&1; then
    ok "BRT backup $brt_backup_id inspectable"
  else
    fail "BRT backup $brt_backup_id inspect failed"
  fi
  cat "$brt_inspect_out" | indent
else
  if [[ "$brt_available_count" == 0 ]]; then
    warn "restore player $player_id has no BRT backup yet; restore correctly remains unavailable until &DD1_backup creates one"
  elif [[ "$brt_available_count" =~ ^[0-9]+$ && "$brt_available_count" -gt 1 ]]; then
    fail "$brt_available_count BRT backups exist; set DUNE_TARGET_AUDIT_BRT_BACKUP_ID to select the canary fixture"
  else
    fail "BRT backup id not resolved"
  fi
fi

if [[ -n "$player_id" ]]; then
  player_row="$(psql_at "
    select id || '|' || map || '|' || partition_id || '|' || dimension_index
    from dune.actors
    where id=${player_id};
  " || true)"
  printf 'restore_player=%s\n' "${player_row:-missing}" | indent
  if [[ "$player_row" == ${player_id}\|DeepDesert\|8\|0 || "$player_row" == ${player_id}\|DeepDesert_1\|8\|0 ]]; then
    ok "restore player is currently in DD1"
  else
    warn "restore player is not in DD1 yet; chat confirm will refuse until they are at the DD1 target"
  fi
fi

section "Building Piece And Subfief Caps"
building_mode="$(env_or_file DUNE_BUILDING_PIECE_LIMIT_MODE server-settings)"
building_settings_enabled="$(env_or_file DUNE_BUILDING_PIECE_LIMIT_SETTINGS_ENABLED false)"
if [[ "$building_mode" == "server-settings" && "$building_settings_enabled" == "true" ]]; then
  building_multiplier="$(env_or_file DUNE_BUILDING_PIECE_LIMIT_MULTIPLIER 0)"
  if [[ "$building_multiplier" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] &&
      awk -v multiplier="$building_multiplier" 'BEGIN { exit !(multiplier > 0) }'; then
    ok "DUNE_BUILDING_PIECE_LIMIT_SETTINGS_ENABLED=true multiplier=$building_multiplier"
  else
    fail "DUNE_BUILDING_PIECE_LIMIT_MULTIPLIER=$building_multiplier is not positive"
  fi
elif [[ "$building_mode" == "pak" ]]; then
  require_env_value DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED true
  building_limit="$(env_or_file DUNE_BUILDING_PIECE_LIMIT 0)"
  if [[ "$building_limit" =~ ^[0-9]+$ && "$building_limit" -gt 5000 ]]; then
    ok "legacy DUNE_BUILDING_PIECE_LIMIT=$building_limit"
  else
    fail "DUNE_BUILDING_PIECE_LIMIT=$building_limit expected >5000 for pak mode"
  fi
else
  fail "unsupported building-piece configuration: mode=$building_mode settings_enabled=$building_settings_enabled"
fi
subfief_enabled="$(env_or_file DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED false)"
if [[ "$subfief_enabled" == "true" ]]; then
  require_env_value DUNE_SUBFIEF_CAP_BINARY_TARGET subfief
  subfief_cap="$(env_or_file DUNE_SUBFIEF_CAP 0)"
  if [[ "$subfief_cap" =~ ^[0-9]+$ && "$subfief_cap" -gt 3 ]]; then
    ok "DUNE_SUBFIEF_CAP=$subfief_cap"
  else
    fail "DUNE_SUBFIEF_CAP=$subfief_cap expected >3"
  fi
else
  warn "DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED is disabled; current-image signature refresh is still pending"
fi

if [[ "$building_mode" == "pak" ]] && grep -q 'compose.building-piece-limit.yaml' <<< "$compose_files"; then
  ok "legacy building-piece compose overlay selected"
elif [[ "$building_mode" == "server-settings" ]] && ! grep -q 'compose.building-piece-limit.yaml' <<< "$compose_files"; then
  ok "supported building-piece server-settings path selected"
else
  fail "building-piece compose selection does not match mode=$building_mode"
fi

mapfile -t probe_services < <(patch_probe_service_list)
ok "patch probe service scope=${patch_probe_services}; services=${#probe_services[@]}"
if [[ -n "$patch_probe_exclude_services" ]]; then
  ok "patch probe excluded services=${patch_probe_exclude_services}"
fi
for service in "${probe_services[@]}"; do
  cid="$(container_id "$service")"
  if [[ -z "$cid" ]]; then
    if map_required "$service"; then
      fail "patch probe skipped; lifecycle-required service $service is not running"
    else
      ok "patch probe skipped; on-demand service $service is intentionally stopped"
    fi
    continue
  fi
  building_out="$(tmp_path "live-target-${service}-building.out")"
  if [[ "$building_mode" == "server-settings" ]]; then
    if "$container_runtime" exec "$cid" sh -lc '
        test -f /home/dune/server/DuneSandbox/Saved/UserSettings/ServerCustomSettings.ini
        awk -F= "/^BuildingPieceLimitMultiplier=/{if (\$2 > 0) found=1} END {exit !found}" \
          /home/dune/server/DuneSandbox/Saved/UserSettings/ServerCustomSettings.ini
      ' >"$building_out" 2>&1; then
      ok "$service supported BuildingPieceLimitMultiplier present"
    else
      fail "$service supported BuildingPieceLimitMultiplier missing or invalid"
    fi
  elif "$container_runtime" exec "$cid" sh -lc '
      oodle="${DUNE_OODLE_LIBRARY:-/tmp/oodle/liboodle-data-shared.so}"
      if [ ! -f "$oodle" ]; then
        oodle=/workspace/backups/operator-oodle/liboodle-data-shared.so
      fi
      python3 /workspace/scripts/patch-building-piece-limit-pak.py \
        --pak /home/dune/server/DuneSandbox/Content/Paks/pakchunk0-LinuxServer.pak \
        --oodle "$oodle" \
        --limit "${DUNE_BUILDING_PIECE_LIMIT:-10000}" \
        --dry-run
    ' >"$building_out" 2>&1; then
    if grep -q 'already-patched' "$building_out"; then
      ok "$service building-piece pak already patched"
    else
      fail "$service building-piece pak dry-run did not report already-patched"
    fi
  else
    fail "$service building-piece pak dry-run failed"
  fi
  cat "$building_out" | tail -4 | indent

  if [[ "$subfief_enabled" != "true" ]]; then
    continue
  fi
  subfief_out="$(tmp_path "live-target-${service}-subfief.out")"
  if "$container_runtime" exec "$cid" sh -lc '
      python3 /workspace/scripts/patch-subfief-cap-binary.py \
        --binary /home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping \
        --target "${DUNE_SUBFIEF_CAP_BINARY_TARGET:-all}" \
        --new-cap "${DUNE_SUBFIEF_CAP:-6}" \
        --dry-run
    ' >"$subfief_out" 2>&1; then
    if grep -q 'all selected targets already patched' "$subfief_out"; then
      ok "$service subfief binary cap already patched"
    elif grep -q 'DRY RUN: would patch' "$subfief_out"; then
      fail "$service subfief binary cap would still patch"
    else
      ok "$service subfief binary cap dry-run completed"
    fi
  else
    fail "$service subfief/building binary cap dry-run failed"
  fi
  cat "$subfief_out" | tail -8 | indent
done

section "Hardcore DD Cleanup Guard"
hardcore_dd_out="$(tmp_path live-target-hardcore-dd.out)"
if "$script_dir/wipe-hardcore-deep-desert.sh" "$env_file" >"$hardcore_dd_out" 2>&1; then
  ok "Hardcore DD cleanup dry-run"
else
  fail "Hardcore DD cleanup dry-run"
fi
cat "$hardcore_dd_out" | tail -30 | indent

section "Watchdog Status"
watchdog_status_out="$(tmp_path live-target-watchdog-status.out)"
if "$script_dir/watch-maps.sh" "$env_file" --status >"$watchdog_status_out" 2>&1; then
  ok "watch-maps status"
else
  fail "watch-maps status"
fi
rg 'deep-desert|deep-desert-pvp|status=exited|status=dead|status=unknown|db="[^t]' "$watchdog_status_out" | indent || true

printf '\nsummary: failures=%s warnings=%s\n' "$failures" "$warnings"
if (( failures > 0 )); then
  exit 1
fi
