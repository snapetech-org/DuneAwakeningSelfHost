#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  printf 'usage: %s BASE_IMAGE_REF CANDIDATE_IMAGE_TAG\n' "$0" >&2
  exit 2
fi

base_ref="$1"
candidate_ref="$2"
[[ "$base_ref" == snape/dune-db-utils:* ]] || {
  echo 'base image must be an existing snape/dune-db-utils image' >&2
  exit 2
}
[[ "$candidate_ref" =~ ^snape/dune-db-utils:[a-z0-9][a-z0-9._-]*$ ]] || {
  echo 'candidate must use a new tag in the snape/dune-db-utils namespace' >&2
  exit 2
}

docker image inspect "$base_ref" >/dev/null
if docker image inspect "$candidate_ref" >/dev/null 2>&1; then
  printf 'candidate tag already exists; refusing to replace it: %s\n' "$candidate_ref" >&2
  exit 1
fi

base_id="$(docker image inspect --format '{{.Id}}' "$base_ref")"
[[ "$base_id" =~ ^sha256:[a-f0-9]{64}$ ]] || {
  echo 'base image did not resolve to an immutable local image ID' >&2
  exit 1
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
base_alias="snape/dune-db-utils:local-base-${base_id#sha256:}"
if docker image inspect "$base_alias" >/dev/null 2>&1; then
  printf 'temporary base alias already exists; inspect it before retrying: %s\n' "$base_alias" >&2
  exit 1
fi
context="$(mktemp -d)"
alias_created=false
cleanup() {
  local status=0
  if [[ "$alias_created" == true ]]; then
    if ! docker image rm --no-prune "$base_alias" >/dev/null; then
      printf 'could not remove temporary base alias: %s\n' "$base_alias" >&2
      status=1
    fi
  fi
  if [[ -d "$context" ]] && ! rmdir -- "$context"; then
    printf 'could not remove empty build context: %s\n' "$context" >&2
    status=1
  fi
  return "$status"
}
trap cleanup EXIT
chmod 0700 "$context"

# BuildKit accepts a local tag as FROM and --pull=false prevents registry fallback.
docker image tag "$base_id" "$base_alias"
alias_created=true

docker build --pull=false --no-cache \
  --file "$script_dir/../images/db-utils-security/Dockerfile.libpq-2026-11" \
  --build-arg "BASE_IMAGE=$base_alias" \
  --build-arg "BASE_IMAGE_ID=$base_id" \
  --tag "$candidate_ref" \
  "$context"

candidate_id="$(docker image inspect --format '{{.Id}}' "$candidate_ref")"
test "$candidate_id" != "$base_id"
test "$(docker image inspect --format '{{.Config.User}}' "$candidate_id")" = \
  "$(docker image inspect --format '{{.Config.User}}' "$base_id")"
test "$(docker image inspect --format '{{.Config.WorkingDir}}' "$candidate_id")" = \
  "$(docker image inspect --format '{{.Config.WorkingDir}}' "$base_id")"
test "$(docker image inspect --format '{{json .Config.Entrypoint}}' "$candidate_id")" = \
  "$(docker image inspect --format '{{json .Config.Entrypoint}}' "$base_id")"
test "$(docker image inspect --format '{{index .Config.Labels "tech.snape.security.base-image-id"}}' "$candidate_id")" = \
  "$base_id"
test "$(docker image inspect --format '{{index .Config.Labels "tech.snape.security.package"}}' "$candidate_id")" = \
  'libpq=17.12-r0'

docker run --rm --network none --entrypoint python "$candidate_id" -c \
  'from pathlib import Path; import re; db=Path("/lib/apk/db/installed").read_text(); match=re.search(r"(?m)^P:libpq\nV:([^\n]+)", db); assert match and match.group(1)=="17.12-r0", match.group(1) if match else "libpq missing"; import psycopg2; print("libpq="+match.group(1)+" psycopg2="+psycopg2.__version__)'

printf 'base_ref=%s\nbase_id=%s\ncandidate_ref=%s\ncandidate_id=%s\n' \
  "$base_ref" "$base_id" "$candidate_ref" "$candidate_id"
