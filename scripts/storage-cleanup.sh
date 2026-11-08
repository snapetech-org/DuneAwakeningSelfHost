#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
COMMAND="status"
EXECUTE=false
INCLUDE_BUILD_CACHE=false
CONFIRM=""

usage() {
  cat <<'EOF'
Usage:
  scripts/storage-cleanup.sh [--env-file PATH] status
  scripts/storage-cleanup.sh [--env-file PATH] cleanup [--dry-run]
  scripts/storage-cleanup.sh [--env-file PATH] cleanup --execute \
    --confirm 'REMOVE OBSOLETE DUNE IMAGES' [--include-build-cache]

Safety contract:
  - cleanup is a dry-run unless --execute and the exact confirmation are given;
  - only known Funcom Dune self-host image repositories are candidates;
  - images referenced by any running or stopped container are protected;
  - the DUNE_IMAGE_TAG selected by the env file is protected;
  - containers, volumes, databases, game data, and backups are never removed;
  - build cache is untouched unless --include-build-cache is explicitly used.
EOF
}

while (($#)); do
  case "$1" in
    status|cleanup) COMMAND="$1" ;;
    --env-file)
      shift
      [[ $# -gt 0 ]] || { echo "--env-file requires a path" >&2; exit 2; }
      ENV_FILE="$1"
      ;;
    --dry-run) EXECUTE=false ;;
    --execute) EXECUTE=true ;;
    --include-build-cache) INCLUDE_BUILD_CACHE=true ;;
    --confirm)
      shift
      [[ $# -gt 0 ]] || { echo "--confirm requires a phrase" >&2; exit 2; }
      CONFIRM="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon is not reachable" >&2; exit 1; }

image_tag=""
if [[ -r "$ENV_FILE" ]]; then
  image_tag="$(awk -F= '/^[[:space:]]*DUNE_IMAGE_TAG[[:space:]]*=/ {value=$0; sub(/^[^=]*=/, "", value); gsub(/^[[:space:]]+|[[:space:]]+$/, "", value); print value; exit}' "$ENV_FILE")"
  image_tag="${image_tag#\"}"
  image_tag="${image_tag%\"}"
  image_tag="${image_tag#\'}"
  image_tag="${image_tag%\'}"
fi

known_repository() {
  case "$1" in
    registry.funcom.com/funcom/self-hosting/igw-postgres|\
    registry.funcom.com/funcom/self-hosting/seabass-server|\
    registry.funcom.com/funcom/self-hosting/seabass-server-bg-director|\
    registry.funcom.com/funcom/self-hosting/seabass-server-db-utils|\
    registry.funcom.com/funcom/self-hosting/seabass-server-gateway|\
    registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq|\
    registry.funcom.com/funcom/self-hosting/seabass-server-text-router) return 0 ;;
    *) return 1 ;;
  esac
}

current_refs() {
  local repo
  if [[ -n "$image_tag" ]]; then
    for repo in \
      registry.funcom.com/funcom/self-hosting/seabass-server \
      registry.funcom.com/funcom/self-hosting/seabass-server-bg-director \
      registry.funcom.com/funcom/self-hosting/seabass-server-db-utils \
      registry.funcom.com/funcom/self-hosting/seabass-server-gateway \
      registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq \
      registry.funcom.com/funcom/self-hosting/seabass-server-text-router
    do
      printf '%s:%s\n' "$repo" "$image_tag"
    done
  fi
  printf '%s\n' \
    'registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13' \
    "${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}"
}

protected_image_ids() {
  local container ref
  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true
  done < <(docker container ls -aq)
  while IFS= read -r ref; do
    [[ -n "$ref" ]] || continue
    docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null || true
  done < <(current_refs)
}

obsolete_images() {
  local protected_file repo tag id
  protected_file="$(mktemp)"
  protected_image_ids | sort -u >"$protected_file"
  while IFS='|' read -r repo tag id; do
    known_repository "$repo" || continue
    grep -qxF "$id" "$protected_file" && continue
    printf '%s|%s:%s\n' "$id" "$repo" "$tag"
  done < <(docker image ls --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}')
  rm -f "$protected_file"
}

show_candidates() {
  local row count=0
  while IFS= read -r row; do
    [[ -n "$row" ]] || continue
    count=$((count + 1))
    printf 'WOULD REMOVE %s (%s)\n' "${row#*|}" "${row%%|*}"
  done < <(obsolete_images)
  [[ $count -gt 0 ]] || echo "No obsolete Dune image candidates."
}

if [[ "$COMMAND" == "status" ]]; then
  echo "=== Docker storage ==="
  docker system df
  echo
  echo "=== Protected release ==="
  printf 'DUNE_IMAGE_TAG=%s\n' "${image_tag:-unset}"
  echo
  echo "=== Safe cleanup preview ==="
  show_candidates
  exit 0
fi

if [[ "$EXECUTE" != true ]]; then
  echo "Dry-run only. No images, cache, containers, volumes, data, or backups were changed."
  show_candidates
  exit 0
fi

[[ "$CONFIRM" == "REMOVE OBSOLETE DUNE IMAGES" ]] || {
  echo "Execution requires --confirm 'REMOVE OBSOLETE DUNE IMAGES'" >&2
  exit 2
}

removed=0
while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  id="${row%%|*}"
  ref="${row#*|}"
  if docker image rm "$id" >/dev/null; then
    echo "REMOVED $ref"
    removed=$((removed + 1))
  else
    echo "SKIPPED $ref (Docker reports it is still in use)" >&2
  fi
done < <(obsolete_images)
[[ $removed -gt 0 ]] || echo "No obsolete Dune images were removed."

if [[ "$INCLUDE_BUILD_CACHE" == true ]]; then
  echo "Pruning shared Docker build cache older than seven days."
  docker builder prune --force --filter until=168h
fi
