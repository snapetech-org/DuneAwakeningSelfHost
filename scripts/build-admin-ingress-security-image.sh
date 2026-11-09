#!/usr/bin/env bash
set -euo pipefail

container="${DUNE_ADMIN_INGRESS_CONTAINER:-dune_server-admin-panel-ingress-1}"
target="${DUNE_ADMIN_INGRESS_IMAGE:-snape/dune-admin-caddy:security-2.11.9}"
config="${DUNE_ADMIN_INGRESS_CONFIG:-config/admin-ingress.Caddyfile}"

base_id="$(docker inspect "$container" --format '{{.Image}}')"
[[ "$base_id" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "unable to resolve immutable admin ingress base image" >&2
  exit 1
}
running_version="$(docker run --rm --network none --entrypoint caddy "$base_id" version)"
[[ "$running_version" == v2.11.9\ * ]] || {
  echo "qualify the Caddy 2.11.9 application patch before rebuilding this OS overlay" >&2
  exit 1
}
base_tag="snape/dune-admin-caddy-base:${base_id#sha256:}"
base_tag="${base_tag:0:40}"
docker tag "$base_id" "$base_tag"
cleanup() {
  docker image rm "$base_tag" >/dev/null 2>&1 || true
}
trap cleanup EXIT

dockerfile='ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apk upgrade --no-cache --update-cache
'
printf '%s' "$dockerfile" | DOCKER_BUILDKIT=0 docker build --pull=false \
  --build-arg "BASE_IMAGE=$base_tag" \
  --label "tech.snape.security.base-image=$base_id" \
  --label "tech.snape.security.overlay=alpine-package-upgrade" \
  --tag "$target" -

running_version="$(docker run --rm --entrypoint caddy "$base_id" version)"
candidate_version="$(docker run --rm --entrypoint caddy "$target" version)"
[[ -n "$running_version" && "$candidate_version" == "$running_version" ]] || {
  echo "Caddy application identity changed during package overlay build" >&2
  exit 1
}
docker run --rm -v "$PWD/$config:/etc/caddy/Caddyfile:ro" "$target" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker run --rm --entrypoint /bin/sh "$target" -lc \
  'apk list -I brotli-libs 2>/dev/null | grep -Eq "brotli-libs-1\.2\.1-r[0-9]+"'

echo "built $target from $base_id; application identity preserved: $candidate_version"
