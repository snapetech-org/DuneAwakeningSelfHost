#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
container_runtime="${CONTAINER_RUNTIME:-docker}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi
compose=("$container_runtime" compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")
catalog_script="$script_dir/map_catalog.py"
if [[ ! -x "$catalog_script" ]]; then
  printf 'map catalog helper is missing or not executable: %s\n' "$catalog_script" >&2
  exit 1
fi
map_catalog_json="$("$catalog_script" "$env_file" --write-cache)"
if [[ -z "$map_catalog_json" ]]; then
  printf 'refusing to seed world partitions from an empty map catalog\n' >&2
  exit 1
fi
backup_dir=backups/partition-surgery

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

db="${DUNE_GAME_DB_NAME:-$(read_env DUNE_GAME_DB_NAME)}"
db="${db:-$(read_env DUNE_DB_NAME)}"
db="${db:-dune_sb_1_4_0_0}"

case "$backup_dir" in
  backups/*) ;;
  *)
    printf 'refusing to write partition backup outside ignored backups/: %s\n' "$backup_dir" >&2
    exit 1
    ;;
esac

mkdir -p "$backup_dir"
backup_file="$backup_dir/world-partitions-before-full-world-$(date -u +%Y%m%dT%H%M%SZ).sql"

"${compose[@]}" exec -T postgres pg_dump \
  -U dune \
  -d "$db" \
  --schema=dune \
  --table=dune.world_partition \
  --data-only \
  > "$backup_file"

printf 'wrote backup: %s\n' "$backup_file"

"${compose[@]}" exec -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 -v "map_catalog=$map_catalog_json" <<'SQL'
begin;

create temp table dune_map_catalog_desired on commit drop as
select partition_id, map, dimension_index, label
from jsonb_to_recordset((:'map_catalog'::jsonb) -> 'maps') as entry(
    partition_id integer,
    map text,
    dimension_index integer,
    label text,
    active boolean,
    remove_when_disabled boolean
  )
where active;

create temp table dune_map_catalog_disabled on commit drop as
select partition_id, map, dimension_index
from jsonb_to_recordset((:'map_catalog'::jsonb) -> 'maps') as entry(
  partition_id integer,
  map text,
  dimension_index integer,
  label text,
  active boolean,
  remove_when_disabled boolean
)
where not active and remove_when_disabled;

with
definition as (
  select '{"box": {"max_x": 1, "max_y": 1, "min_x": 0, "min_y": 0}, "type": "box2d_array"}'::jsonb as value
)
insert into dune.world_partition (partition_id, map, dimension_index, partition_definition, label)
select d.partition_id, d.map, d.dimension_index, definition.value, d.label
from dune_map_catalog_desired d
cross join definition
where not exists (
    select 1
    from dune.world_partition wp
    where wp.map = d.map
      and wp.dimension_index = d.dimension_index
  );

update dune.world_partition wp
set label = d.label
from dune_map_catalog_desired d
where wp.partition_id = d.partition_id
  and wp.map = d.map
  and wp.dimension_index = d.dimension_index
  and d.label is not null;

update dune.actors
set partition_id = null
where partition_id in (
  select wp.partition_id
  from dune.world_partition wp
  join dune_map_catalog_disabled d using (partition_id, map, dimension_index)
);

delete from dune.world_partition_reset_seed seed
using dune.world_partition wp, dune_map_catalog_disabled d
where seed.partition_id = wp.partition_id
  and wp.partition_id = d.partition_id
  and wp.map = d.map
  and wp.dimension_index = d.dimension_index;

delete from dune.world_partition wp
using dune_map_catalog_disabled d
where wp.partition_id = d.partition_id
  and wp.map = d.map
  and wp.dimension_index = d.dimension_index;

select setval(
  'dune.world_partition_partition_id_seq',
  greatest(
    (select coalesce(max(partition_id), 1) from dune.world_partition),
    (select last_value from dune.world_partition_partition_id_seq)
  )
);

select dune.update_partition_labels(false);
notify world_partition_update;

commit;

select partition_id, server_id, map, dimension_index, label
from dune.world_partition
order by partition_id;
SQL
