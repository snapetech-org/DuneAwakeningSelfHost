#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-}}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

remote="${remote:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}"
image_tag="${DUNE_IMAGE_TAG:-$(read_env DUNE_IMAGE_TAG)}"
postgres_image="${POSTGRES_IMAGE:-${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}}"

if [[ -z "$remote" || -z "$image_tag" ]]; then
  printf 'POSTGRES_REMOTE_REPLICA_HOST and DUNE_IMAGE_TAG are required\n' >&2
  exit 1
fi

images=(
  "$postgres_image"
  "registry.funcom.com/funcom/self-hosting/seabass-server:${image_tag}"
  "registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:${image_tag}"
  "registry.funcom.com/funcom/self-hosting/seabass-server-bg-director:${image_tag}"
  "registry.funcom.com/funcom/self-hosting/seabass-server-text-router:${image_tag}"
  "registry.funcom.com/funcom/self-hosting/seabass-server-db-utils:${image_tag}"
  "registry.funcom.com/funcom/self-hosting/seabass-server-gateway:${image_tag}"
)
director_override="${DUNE_DIRECTOR_IMAGE:-$(read_env DUNE_DIRECTOR_IMAGE)}"
text_router_override="${DUNE_TEXT_ROUTER_IMAGE:-$(read_env DUNE_TEXT_ROUTER_IMAGE)}"
gateway_override="${DUNE_GATEWAY_IMAGE:-$(read_env DUNE_GATEWAY_IMAGE)}"
[[ -z "$director_override" ]] || images+=("$director_override")
[[ -z "$text_router_override" ]] || images+=("$text_router_override")
[[ -z "$gateway_override" ]] || images+=("$gateway_override")

missing=()
for image in "${images[@]}"; do
  if ssh "$remote" "docker image inspect '$image' >/dev/null 2>&1"; then
    printf 'OK remote has %s\n' "$image"
  else
    missing+=("$image")
  fi
done

if [[ "${#missing[@]}" -eq 0 ]]; then
  printf 'all required images already present on %s\n' "$remote"
  exit 0
fi

if [[ "${CONFIRM_SYNC_STANDBY_IMAGES:-}" != "yes" ]]; then
  printf 'Dry run: would copy %s missing images to %s:\n' "${#missing[@]}" "$remote"
  printf '  %s\n' "${missing[@]}"
  printf 'Apply with CONFIRM_SYNC_STANDBY_IMAGES=yes make sync-standby-images ENV_FILE=%s\n' "$env_file"
  exit 0
fi

for image in "${missing[@]}"; do
  printf 'copying %s to %s\n' "$image" "$remote"
  docker image inspect "$image" >/dev/null
  docker save "$image" | ssh "$remote" docker load
done
