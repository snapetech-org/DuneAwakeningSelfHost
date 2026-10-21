#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/wipe-hardcore-deep-desert.sh [ENV_FILE] [--execute] [--if-due] [--force]

Dry-runs, or executes, the weekly Hardcore Deep Desert cleanup for only partition 31
and dimension 1. The cleanup uses the official partition-scoped Coriolis
function for actors/respawns and separately clears only Hardcore-dimension resource
field state. Spice-field metrics/reset are applied when the current database
schema exposes the optional spice-field table and reset function.

Defaults:
  DUNE_HARDCORE_DD_PARTITION_ID=31
  DUNE_HARDCORE_DD_WORLD_MAP=DeepDesert_1
  DUNE_HARDCORE_DD_RESOURCE_MAP=DeepDesert
  DUNE_HARDCORE_DD_SPICE_MAP=DeepDesert
  DUNE_HARDCORE_DD_DIMENSION_INDEX=1
  DUNE_HARDCORE_DD_EXPECTED_LABEL=02 PVE Hardcore
  DUNE_HARDCORE_DD_WEEKLY_WIPE_MARKER=backups/manual/hardcore-dd-weekly-wipe.last
  DUNE_HARDCORE_DD_WEEKLY_WIPE_MIN_INTERVAL_DAYS=6
  DUNE_HARDCORE_DD_WIPE_REQUIRE_HOST=kspls0

Legacy DUNE_PVP_DD_* variables are still accepted for compatibility.
USAGE
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
env_file="${DUNE_ENV_FILE:-.env}"
execute=false
if_due=false
force=false
mark_success=true

while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --execute)
      execute=true
      ;;
    --if-due)
      if_due=true
      ;;
    --force)
      force=true
      ;;
    --no-mark)
      mark_success=false
      ;;
    *)
      env_file="$1"
      ;;
  esac
  shift
done

cd "$repo_root"

if [[ ! -f "$env_file" ]]; then
  printf 'missing env file: %s\n' "$env_file" >&2
  exit 1
fi

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

env_or_file_any() {
  local default_value="$1" key value
  shift
  for key in "$@"; do
    value="${!key:-}"
    if [[ -z "$value" ]]; then
      value="$(read_env "$key")"
    fi
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return 0
    fi
  done
  printf '%s' "$default_value"
}

host_short() {
  local host_ns=""
  if command -v nsenter >/dev/null 2>&1 && [[ -e /proc/1/ns/uts ]]; then
    host_ns="$(nsenter --target 1 --uts hostname -s 2>/dev/null || true)"
  fi
  if [[ -n "$host_ns" ]]; then
    printf '%s' "$host_ns"
  else
    hostname -s 2>/dev/null || hostname
  fi
}

marker_due() {
  local marker="$1" min_days="$2" now last_epoch min_seconds age
  [[ "$force" == true ]] && return 0
  [[ -f "$marker" ]] || return 0
  last_epoch="$(awk -F= '$1 == "epoch" {print $2; exit}' "$marker" 2>/dev/null || true)"
  case "$last_epoch" in
    ''|*[!0-9]*) return 0 ;;
  esac
  now="$(date -u +%s)"
  min_seconds=$((min_days * 86400))
  age=$((now - last_epoch))
  [[ "$age" -ge "$min_seconds" ]]
}

partition_id="$(env_or_file_any 31 DUNE_HARDCORE_DD_PARTITION_ID DUNE_PVP_DD_PARTITION_ID)"
world_map="$(env_or_file_any DeepDesert_1 DUNE_HARDCORE_DD_WORLD_MAP DUNE_PVP_DD_WORLD_MAP)"
resource_map="$(env_or_file_any DeepDesert DUNE_HARDCORE_DD_RESOURCE_MAP DUNE_PVP_DD_RESOURCE_MAP)"
spice_map="$(env_or_file_any DeepDesert DUNE_HARDCORE_DD_SPICE_MAP DUNE_PVP_DD_SPICE_MAP)"
dimension_index="$(env_or_file_any 1 DUNE_HARDCORE_DD_DIMENSION_INDEX DUNE_PVP_DD_DIMENSION_INDEX)"
expected_label="$(env_or_file_any "02 PVE Hardcore" DUNE_HARDCORE_DD_EXPECTED_LABEL DUNE_PVP_DD_EXPECTED_LABEL)"
marker="$(env_or_file_any "backups/manual/hardcore-dd-weekly-wipe.last" DUNE_HARDCORE_DD_WEEKLY_WIPE_MARKER DUNE_PVP_DD_WEEKLY_WIPE_MARKER)"
min_days="$(env_or_file_any 6 DUNE_HARDCORE_DD_WEEKLY_WIPE_MIN_INTERVAL_DAYS DUNE_PVP_DD_WEEKLY_WIPE_MIN_INTERVAL_DAYS)"
required_host="$(env_or_file_any kspls0 DUNE_HARDCORE_DD_WIPE_REQUIRE_HOST DUNE_PVP_DD_WIPE_REQUIRE_HOST)"
db="$(env_or_file DUNE_DATABASE dune_sb_1_4_0_0)"
container_runtime="$(env_or_file CONTAINER_RUNTIME docker)"

case "$partition_id:$dimension_index:$min_days" in
  *[!0-9:]*|:*|*:) printf 'partition, dimension, and min-days must be numeric\n' >&2; exit 64 ;;
esac

if [[ "$execute" == true && -n "$required_host" ]]; then
  current_host="$(host_short)"
  if [[ "$current_host" != "$required_host" ]]; then
    printf 'refusing Hardcore DD wipe on host %s; required host is %s\n' "$current_host" "$required_host" >&2
    exit 78
  fi
fi

if [[ "$if_due" == true ]] && ! marker_due "$marker" "$min_days"; then
  printf 'Hardcore DD weekly wipe not due; marker=%s minDays=%s\n' "$marker" "$min_days"
  exit 0
fi

compose_files="$("$script_dir/compose-files.sh" "$env_file")"
compose=("$container_runtime" compose --env-file "$env_file")
IFS=':' read -ra files <<< "$compose_files"
for file in "${files[@]}"; do
  compose+=(-f "$file")
done

psql_base=(
  "${compose[@]}" exec -T postgres
  psql -U dune -d "$db" -v ON_ERROR_STOP=1
  -v "partition_id=$partition_id"
  -v "world_map=$world_map"
  -v "resource_map=$resource_map"
  -v "spice_map=$spice_map"
  -v "dimension_index=$dimension_index"
  -v "expected_label=$expected_label"
)

if [[ "$execute" == true ]]; then
  printf 'Executing Hardcore DD cleanup: partition=%s worldMap=%s resourceMap=%s spiceMap=%s dimension=%s\n' \
    "$partition_id" "$world_map" "$resource_map" "$spice_map" "$dimension_index"
  "${psql_base[@]}" <<'SQL'
\pset pager off
\pset tuples_only off
BEGIN;
SELECT set_config('pvp_dd.partition_id', :'partition_id', false);
SELECT set_config('pvp_dd.world_map', :'world_map', false);
SELECT set_config('pvp_dd.resource_map', :'resource_map', false);
SELECT set_config('pvp_dd.spice_map', :'spice_map', false);
SELECT set_config('pvp_dd.dimension_index', :'dimension_index', false);
SELECT set_config('pvp_dd.expected_label', :'expected_label', false);
SELECT to_regclass('dune.spicefield_types') IS NOT NULL AS spicefield_types_present,
       to_regprocedure('dune.reset_global_spice_field_state(text,integer)') IS NOT NULL AS spice_reset_function_present
\gset

DO $$
DECLARE
  wp record;
BEGIN
  SELECT *
    INTO wp
    FROM dune.world_partition
    WHERE partition_id = current_setting('pvp_dd.partition_id')::bigint
    FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'missing Hardcore DD partition %', current_setting('pvp_dd.partition_id');
  END IF;
  IF wp.map <> current_setting('pvp_dd.world_map') THEN
    RAISE EXCEPTION 'partition % map mismatch: got %, expected %',
      wp.partition_id, wp.map, current_setting('pvp_dd.world_map');
  END IF;
  IF wp.dimension_index <> current_setting('pvp_dd.dimension_index')::integer THEN
    RAISE EXCEPTION 'partition % dimension mismatch: got %, expected %',
      wp.partition_id, wp.dimension_index, current_setting('pvp_dd.dimension_index');
  END IF;
  IF current_setting('pvp_dd.expected_label') <> ''
     AND coalesce(wp.label, '') <> current_setting('pvp_dd.expected_label') THEN
    RAISE EXCEPTION 'partition % label mismatch: got %, expected %',
      wp.partition_id, coalesce(wp.label, ''), current_setting('pvp_dd.expected_label');
  END IF;
END $$;

\echo before
WITH target AS (
  SELECT current_setting('pvp_dd.partition_id')::bigint AS partition_id,
         current_setting('pvp_dd.world_map') AS world_map,
         current_setting('pvp_dd.resource_map') AS resource_map,
         current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
)
SELECT metric, count
FROM (
  SELECT 'actors_matching_partition' AS metric, count(*)::bigint AS count
    FROM dune.actors a, target t
    WHERE dune.server_info_match(
      a,
      row(t.world_map, t.partition_id, t.dimension_index)::dune.serverinfo
    )
  UNION ALL
  SELECT 'respawns_matching_dimension', count(*)::bigint
    FROM dune.player_respawn_locations r, target t
    WHERE r.map = t.world_map AND r.dimension = t.dimension_index
  UNION ALL
  SELECT 'resourcefield_state_dimension', count(*)::bigint
    FROM dune.resourcefield_state r, target t
    WHERE r.map = t.resource_map AND r.dimension_index = t.dimension_index
) counts
ORDER BY metric;

\if :spicefield_types_present
WITH target AS (
  SELECT current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
)
SELECT 'spicefield_types_dimension' AS metric, count(*)::bigint AS count
FROM dune.spicefield_types s, target t
WHERE s.map_name = t.spice_map AND s.dimension_index = t.dimension_index;
\else
\echo spicefield_types is absent; spice-field metrics skipped
\endif

WITH target AS (
  SELECT current_setting('pvp_dd.partition_id')::bigint AS partition_id,
         current_setting('pvp_dd.world_map') AS world_map,
         current_setting('pvp_dd.resource_map') AS resource_map,
         current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
),
partition_cleanup AS (
  SELECT dune.coriolis_cleanup_partition(
    row(t.world_map, t.partition_id, t.dimension_index)::dune.serverinfo,
    row(true, true, ARRAY[]::text[], false, NULL::text[])::dune.coriolismapinfo
  )
  FROM target t
),
deleted_resourcefields AS (
  DELETE FROM dune.resourcefield_state r
  USING target t
  WHERE r.map = t.resource_map
    AND r.dimension_index = t.dimension_index
  RETURNING 1
)
SELECT mutation, count
FROM (
  SELECT 'partition_cleanup_called' AS mutation, count(*)::bigint AS count
    FROM partition_cleanup
  UNION ALL
  SELECT 'resourcefield_state_deleted', count(*)::bigint
    FROM deleted_resourcefields
) mutations
ORDER BY mutation;

\if :spice_reset_function_present
SELECT dune.reset_global_spice_field_state(
  current_setting('pvp_dd.spice_map'),
  current_setting('pvp_dd.dimension_index')::integer
);
\else
\echo reset_global_spice_field_state is absent; spice-field reset skipped
\endif

\echo after
WITH target AS (
  SELECT current_setting('pvp_dd.partition_id')::bigint AS partition_id,
         current_setting('pvp_dd.world_map') AS world_map,
         current_setting('pvp_dd.resource_map') AS resource_map,
         current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
)
SELECT metric, count
FROM (
  SELECT 'actors_matching_partition' AS metric, count(*)::bigint AS count
    FROM dune.actors a, target t
    WHERE dune.server_info_match(
      a,
      row(t.world_map, t.partition_id, t.dimension_index)::dune.serverinfo
    )
  UNION ALL
  SELECT 'respawns_matching_dimension', count(*)::bigint
    FROM dune.player_respawn_locations r, target t
    WHERE r.map = t.world_map AND r.dimension = t.dimension_index
  UNION ALL
  SELECT 'resourcefield_state_dimension', count(*)::bigint
    FROM dune.resourcefield_state r, target t
    WHERE r.map = t.resource_map AND r.dimension_index = t.dimension_index
) counts
ORDER BY metric;

\if :spicefield_types_present
WITH target AS (
  SELECT current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
)
SELECT 'spicefield_current_active' AS metric,
       coalesce(sum(current_globally_active), 0)::bigint AS count
FROM dune.spicefield_types s, target t
WHERE s.map_name = t.spice_map AND s.dimension_index = t.dimension_index
UNION ALL
SELECT 'spicefield_current_primed',
       coalesce(sum(current_globally_primed), 0)::bigint
FROM dune.spicefield_types s, target t
WHERE s.map_name = t.spice_map AND s.dimension_index = t.dimension_index;
\else
\echo spicefield_types is absent; spice-field post-cleanup metrics skipped
\endif

COMMIT;
SQL
  if [[ "$mark_success" == true ]]; then
    mkdir -p "$(dirname -- "$marker")"
    {
      printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      printf 'epoch=%s\n' "$(date -u +%s)"
      printf 'partition_id=%s\n' "$partition_id"
      printf 'world_map=%s\n' "$world_map"
      printf 'resource_map=%s\n' "$resource_map"
      printf 'spice_map=%s\n' "$spice_map"
      printf 'dimension_index=%s\n' "$dimension_index"
    } > "$marker"
    printf 'Hardcore DD weekly wipe marker updated: %s\n' "$marker"
  fi
else
  printf 'Dry-run Hardcore DD cleanup: partition=%s worldMap=%s resourceMap=%s spiceMap=%s dimension=%s\n' \
    "$partition_id" "$world_map" "$resource_map" "$spice_map" "$dimension_index"
  "${psql_base[@]}" <<'SQL'
\pset pager off
SELECT set_config('pvp_dd.partition_id', :'partition_id', false);
SELECT set_config('pvp_dd.world_map', :'world_map', false);
SELECT set_config('pvp_dd.resource_map', :'resource_map', false);
SELECT set_config('pvp_dd.spice_map', :'spice_map', false);
SELECT set_config('pvp_dd.dimension_index', :'dimension_index', false);
SELECT set_config('pvp_dd.expected_label', :'expected_label', false);
SELECT to_regclass('dune.spicefield_types') IS NOT NULL AS spicefield_types_present
\gset

DO $$
DECLARE
  wp record;
BEGIN
  SELECT *
    INTO wp
    FROM dune.world_partition
    WHERE partition_id = current_setting('pvp_dd.partition_id')::bigint;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'missing Hardcore DD partition %', current_setting('pvp_dd.partition_id');
  END IF;
  IF wp.map <> current_setting('pvp_dd.world_map') THEN
    RAISE EXCEPTION 'partition % map mismatch: got %, expected %',
      wp.partition_id, wp.map, current_setting('pvp_dd.world_map');
  END IF;
  IF wp.dimension_index <> current_setting('pvp_dd.dimension_index')::integer THEN
    RAISE EXCEPTION 'partition % dimension mismatch: got %, expected %',
      wp.partition_id, wp.dimension_index, current_setting('pvp_dd.dimension_index');
  END IF;
  IF current_setting('pvp_dd.expected_label') <> ''
     AND coalesce(wp.label, '') <> current_setting('pvp_dd.expected_label') THEN
    RAISE EXCEPTION 'partition % label mismatch: got %, expected %',
      wp.partition_id, coalesce(wp.label, ''), current_setting('pvp_dd.expected_label');
  END IF;
END $$;

WITH target AS (
  SELECT current_setting('pvp_dd.partition_id')::bigint AS partition_id,
         current_setting('pvp_dd.world_map') AS world_map,
         current_setting('pvp_dd.resource_map') AS resource_map,
         current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
)
SELECT metric, count
FROM (
  SELECT 'actors_matching_partition' AS metric, count(*)::bigint AS count
    FROM dune.actors a, target t
    WHERE dune.server_info_match(
      a,
      row(t.world_map, t.partition_id, t.dimension_index)::dune.serverinfo
    )
  UNION ALL
  SELECT 'respawns_matching_dimension', count(*)::bigint
    FROM dune.player_respawn_locations r, target t
    WHERE r.map = t.world_map AND r.dimension = t.dimension_index
  UNION ALL
  SELECT 'resourcefield_state_dimension', count(*)::bigint
    FROM dune.resourcefield_state r, target t
    WHERE r.map = t.resource_map AND r.dimension_index = t.dimension_index
) counts
ORDER BY metric;

\if :spicefield_types_present
WITH target AS (
  SELECT current_setting('pvp_dd.spice_map') AS spice_map,
         current_setting('pvp_dd.dimension_index')::integer AS dimension_index
)
SELECT 'spicefield_current_active' AS metric,
       coalesce(sum(current_globally_active), 0)::bigint AS count
FROM dune.spicefield_types s, target t
WHERE s.map_name = t.spice_map AND s.dimension_index = t.dimension_index
UNION ALL
SELECT 'spicefield_current_primed',
       coalesce(sum(current_globally_primed), 0)::bigint
FROM dune.spicefield_types s, target t
WHERE s.map_name = t.spice_map AND s.dimension_index = t.dimension_index;
\else
\echo spicefield_types is absent; spice-field metrics skipped
\endif
SQL
fi
