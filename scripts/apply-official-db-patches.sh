#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: ./scripts/apply-official-db-patches.sh [env-file]

Applies missing official Funcom database upgrade patches from the currently
configured seabass-server image before map startup.
USAGE
}

env_file="${1:-.env}"
case "$env_file" in
  -h|--help)
    usage
    exit 0
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ ! -f "$env_file" ]]; then
  printf 'fail: env file not found: %s\n' "$env_file" >&2
  exit 1
fi

get_env() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "")
      gsub(/^["'\'']|["'\'']$/, "")
      print
      exit
    }
  ' "$env_file" 2>/dev/null
}

set_env_value() {
  local key="$1"
  local value="$2"
  python3 "$repo_root/scripts/update-env-file.py" "$env_file" --allow-protected-link --quiet --set "$key" "$value"
}

database_exists() {
  local database="$1"
  "${compose[@]}" exec -T postgres psql -U dune -d postgres -Atc \
    "select 1 from pg_database where datname = '$database';" 2>/dev/null | grep -qx 1
}

validate_database_name() {
  local database="$1"
  if ! [[ "$database" =~ ^[A-Za-z0-9_]+$ ]]; then
    printf 'fail: unsafe database name: %s\n' "$database" >&2
    exit 1
  fi
}

infer_image_database_name() {
  local image="$1"
  local branch
  local template
  branch="$(docker run --rm --entrypoint sh "$image" -lc \
    "for p in /home/dune/server/DuneSandbox/Config/perforce-keywords.json /root/DuneSandbox/Config/perforce-keywords.json; do [ -f \"\$p\" ] && sed -n 's#.*//seabass/\\(sb-[^/]*\\)/.*#\\1#p' \"\$p\" && exit 0; done" \
    | head -n 1 | tr -d '[:space:]')"
  [[ -n "$branch" ]] || return 1

  branch="${branch//-/_}"
  branch="${branch//./_}"
  template="$(docker run --rm --entrypoint sh "$image" -lc \
    "for p in /home/dune/server/DuneSandbox/Config/DedicatedServerGame.ini /root/DuneSandbox/Config/DedicatedServerGame.ini; do [ -f \"\$p\" ] && awk -F= '/^[[:space:]]*DatabaseName[[:space:]]*=/{print \$2; exit}' \"\$p\" && exit 0; done" \
    | head -n 1 | tr -d '[:space:]')"
  template="${template:-dune%_BRANCH%}"
  template="${template//%_BRANCH%/_$branch}"
  template="${template//%BRANCH%/$branch}"
  printf '%s\n' "$template"
}

clone_database_if_missing() {
  local source_db="$1"
  local target_db="$2"
  local timestamp
  local backup_dir
  local dump_file

  if database_exists "$target_db"; then
    return 0
  fi
  if [[ "$source_db" == "$target_db" ]]; then
    printf 'fail: active database %s does not exist and no source branch database is available\n' "$target_db" >&2
    exit 1
  fi
  if ! database_exists "$source_db"; then
    printf 'fail: source database %s does not exist; cannot create %s\n' "$source_db" "$target_db" >&2
    exit 1
  fi

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="backups/${timestamp}"
  mkdir -p "$backup_dir"
  dump_file="${backup_dir}/postgres-${source_db}-pre-${target_db}-clone.dump"
  printf 'active DB %s is missing; cloning %s to %s\n' "$target_db" "$source_db" "$target_db"
  printf 'writing branch-clone backup: %s\n' "$dump_file"
  "${compose[@]}" exec -T postgres pg_dump -U dune -d "$source_db" -Fc >"$dump_file"
  "${compose[@]}" exec -T postgres createdb -U dune -O dune "$target_db"
  "${compose[@]}" exec -T postgres pg_restore -U dune -d "$target_db" --no-owner --role=dune <"$dump_file"
}

image_tag="${DUNE_IMAGE_TAG:-$(get_env DUNE_IMAGE_TAG)}"
if [[ -z "$image_tag" ]]; then
  printf 'fail: DUNE_IMAGE_TAG is empty\n' >&2
  exit 1
fi

server_image="${DUNE_DB_PATCH_SOURCE_IMAGE:-registry.funcom.com/funcom/self-hosting/seabass-server:$image_tag}"
patch_root="/home/dune/server/DuneSandbox/Database/Upgrade"

if ! command -v docker >/dev/null 2>&1; then
  printf 'fail: docker is required to read official DB patches from %s\n' "$server_image" >&2
  exit 127
fi

if ! docker image inspect "$server_image" >/dev/null 2>&1; then
  printf 'fail: official server image is not loaded: %s\n' "$server_image" >&2
  exit 1
fi

if [[ -x ./scripts/compose-files.sh ]]; then
  COMPOSE_FILES="$(./scripts/compose-files.sh "$env_file")"
  export COMPOSE_FILES
fi

compose=(docker compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")

inferred_db_name="$(infer_image_database_name "$server_image" || true)"
configured_db_name="${DUNE_GAME_DB_NAME:-$(get_env DUNE_GAME_DB_NAME)}"
configured_db_name="${configured_db_name:-${DUNE_DATABASE:-$(get_env DUNE_DATABASE)}}"
configured_db_name="${configured_db_name:-${DUNE_DB_NAME:-$(get_env DUNE_DB_NAME)}}"
db_name="${inferred_db_name:-$configured_db_name}"
db_name="${db_name:-dune_sb_1_4_0_0}"
validate_database_name "$db_name"

source_db="${DUNE_GAME_DB_CLONE_SOURCE:-$(get_env DUNE_GAME_DB_CLONE_SOURCE)}"
source_db="${source_db:-${DUNE_PREVIOUS_GAME_DB_NAME:-$(get_env DUNE_PREVIOUS_GAME_DB_NAME)}}"
source_db="${source_db:-$configured_db_name}"
source_db="${source_db:-dune_sb_1_4_0_0}"
validate_database_name "$source_db"

clone_database_if_missing "$source_db" "$db_name"

case "${DUNE_WRITE_ACTIVE_DB_ENV:-$(get_env DUNE_WRITE_ACTIVE_DB_ENV)}" in
  0|false|no|off) ;;
  *)
    set_env_value DUNE_GAME_DB_NAME "$db_name"
    set_env_value DUNE_DATABASE "$db_name"
    set_env_value DUNE_DB_NAME "$db_name"
    ;;
esac

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

docker run --rm --entrypoint cat "$server_image" "$patch_root/__order.txt" \
  | sed -E '/^[[:space:]]*($|#)/d; s/[[:space:]]+$//' >"$tmp_dir/order"

"${compose[@]}" exec -T postgres psql -U dune -d "$db_name" -Atc \
  "select name from dune.applied_patches order by name;" >"$tmp_dir/applied"

missing=()
while IFS= read -r patch; do
  [[ -n "$patch" ]] || continue
  if ! [[ "$patch" =~ ^[A-Za-z0-9_-]+$ ]]; then
    printf 'fail: unsafe official patch name in __order.txt: %s\n' "$patch" >&2
    exit 1
  fi
  if ! grep -Fxq "$patch" "$tmp_dir/applied"; then
    missing+=("$patch")
  fi
done <"$tmp_dir/order"

if [[ "${#missing[@]}" -eq 0 ]]; then
  printf 'official DB patches current for %s\n' "$image_tag"
  exit 0
fi

printf 'applying %s official DB patch(es) for %s\n' "${#missing[@]}" "$image_tag"
for patch in "${missing[@]}"; do
  printf 'applying official DB patch: %s\n' "$patch"
  docker run --rm --entrypoint cat "$server_image" "$patch_root/$patch.sql" \
    | "${compose[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U dune -d "$db_name" \
        -c "set search_path to dune, public;" -f -
  "${compose[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U dune -d "$db_name" \
    -c "insert into dune.applied_patches(name, date) values ('$patch', now()) on conflict (name) do nothing;"
done

printf 'official DB patch application complete\n'
