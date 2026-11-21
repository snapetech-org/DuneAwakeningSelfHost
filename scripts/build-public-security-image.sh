#!/usr/bin/env bash
# Build one redistributable image using a unique staging tag.
set -euo pipefail
component=${1:?usage: build-public-security-image.sh COMPONENT NEW_OUTPUT_DIRECTORY}
output=$(realpath -m "${2:?missing new output directory}")
cd "$(dirname "$0")/.."
umask 077
mkdir "$output"
tag="dune-candidate/$component:$(cat /proc/sys/kernel/random/uuid)"
case "$component" in
  prometheus|postgres-exporter|node-exporter|cadvisor)
    "scripts/build-$component-security.sh" "$output/build" "$tag"
    ;;
  admin-ingress)
    source scripts/security-images/go-builder.sh
    docker build --no-cache --build-arg "GO_BUILDER_IMAGE=$builder" \
      -t "$tag" scripts/security-images/admin-ingress
    docker run --rm --network none --entrypoint caddy "$tag" list-modules | grep -Fx http.matchers.maxmind_geolocation
    docker run --rm --network none -v "$PWD/config/admin-ingress.Caddyfile:/config-check:ro" \
      --entrypoint caddy "$tag" validate --config /config-check --adapter caddyfile
    ;;
  postgres)
    source scripts/security-images/go-builder.sh
    # Use upstream PostgreSQL 17 patch releases on the qualified Alpine series.
    pg_image=postgres:17-alpine3.24
    docker pull --platform linux/amd64 "$pg_image"
    pg_ref=$(docker image inspect --format '{{index .RepoDigests 0}}' "$pg_image")
    version=$(docker run --rm --network none --entrypoint postgres "$pg_ref" --version | awk '{print $3}')
    [[ $version =~ ^17\.([0-9]+)$ && ${BASH_REMATCH[1]} -ge 12 ]]
    BASE_IMAGE="$pg_ref" EXPECTED_BASE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$pg_ref")" \
      GO_BUILDER_IMAGE="$builder" EXPECTED_GO_BUILDER_ID="$(docker image inspect --format '{{.Id}}' "$builder")" \
      EXPECTED_POSTGRES_VERSION="$version" CANDIDATE_IMAGE="$tag" scripts/build-postgres-security-image.sh
    ;;
  *) echo "Unknown public component: $component" >&2; exit 2 ;;
esac
scripts/scan-security-candidate.sh "$tag" "$output"
