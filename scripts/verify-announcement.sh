#!/bin/sh
set -eu

message="${1:-PAUL ANNOUNCEMENT VERIFY}"
env_file="${ENV_FILE:-.env}"
script_dir="$(dirname "$0")"
if [ -x "$script_dir/compose-files.sh" ]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi
compose_files="${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"

set -- docker compose
# Split colon-delimited file paths without changing the shell's global IFS.
remaining_compose_files="$compose_files"
while [ -n "$remaining_compose_files" ]; do
  file="${remaining_compose_files%%:*}"
  [ -z "$file" ] || set -- "$@" -f "$file"
  case "$remaining_compose_files" in
    *:*) remaining_compose_files="${remaining_compose_files#*:}" ;;
    *) break ;;
  esac
done
set -- "$@" --env-file "$env_file"

exec "$@" exec -T admin-panel /workspace/scripts/announce.sh "$message"
