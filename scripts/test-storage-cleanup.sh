#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
LOG_FILE="$TMP_DIR/docker.log"
ENV_FILE="$TMP_DIR/test.env"
printf 'DUNE_IMAGE_TAG=current-tag\n' >"$ENV_FILE"

docker() {
  printf '%s\n' "$*" >>"$LOG_FILE"
  case "$1 ${2:-} ${3:-}" in
    "info  ") return 0 ;;
    "system df ") echo 'TYPE TOTAL ACTIVE SIZE RECLAIMABLE'; return 0 ;;
    "container ls -aq") echo container-one; return 0 ;;
    "inspect --format {{.Image}}") echo sha256:running; return 0 ;;
    "image inspect --format")
      case "${5:-}" in
        *:current-tag) echo sha256:current ;;
        *igw-postgres:17.4-alpine-fc-13|*dune-postgres:security-17.4-alpine321-fc13) echo sha256:postgres ;;
      esac
      return 0
      ;;
    "image ls --no-trunc")
      printf '%s\n' \
        'registry.funcom.com/funcom/self-hosting/seabass-server|current-tag|sha256:current' \
        'registry.funcom.com/funcom/self-hosting/seabass-server|old-tag|sha256:old' \
        'registry.funcom.com/funcom/self-hosting/seabass-server-gateway|running-tag|sha256:running' \
        'registry.funcom.com/funcom/self-hosting/igw-postgres|17.4-alpine-fc-13|sha256:postgres' \
        'example.invalid/unrelated|old|sha256:unrelated'
      return 0
      ;;
    "image rm sha256:old") return 0 ;;
    "builder prune --force") return 0 ;;
  esac
  echo "unexpected docker invocation: $*" >&2
  return 1
}
export -f docker
export LOG_FILE

preview="$($ROOT_DIR/scripts/storage-cleanup.sh --env-file "$ENV_FILE" cleanup --dry-run)"
grep -q 'WOULD REMOVE.*old-tag' <<<"$preview"
! grep -q 'current-tag' <<<"$preview"
! grep -q 'running-tag' <<<"$preview"
! grep -q 'unrelated' <<<"$preview"
! grep -q 'igw-postgres' <<<"$preview"
! grep -q '^image rm ' "$LOG_FILE"

EMPTY_ENV_FILE="$TMP_DIR/empty.env"
: >"$EMPTY_ENV_FILE"
preview_without_release="$($ROOT_DIR/scripts/storage-cleanup.sh --env-file "$EMPTY_ENV_FILE" cleanup --dry-run)"
! grep -q 'igw-postgres' <<<"$preview_without_release"

if "$ROOT_DIR/scripts/storage-cleanup.sh" --env-file "$ENV_FILE" cleanup --execute --confirm WRONG >/dev/null 2>&1; then
  echo "cleanup accepted the wrong confirmation" >&2
  exit 1
fi

execute="$($ROOT_DIR/scripts/storage-cleanup.sh --env-file "$ENV_FILE" cleanup --execute --confirm 'REMOVE OBSOLETE DUNE IMAGES')"
grep -q 'REMOVED.*old-tag' <<<"$execute"
grep -q '^image rm sha256:old$' "$LOG_FILE"
! grep -q '^image rm sha256:current$' "$LOG_FILE"
! grep -q '^image rm sha256:running$' "$LOG_FILE"

echo "storage cleanup tests passed"
