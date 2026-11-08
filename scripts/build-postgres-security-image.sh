#!/usr/bin/env bash
set -euo pipefail

base_image="${BASE_IMAGE:-snape/dune-postgres:vendor-17.4-alpine-fc13-local}"
expected_base_id="${EXPECTED_BASE_IMAGE_ID:-sha256:3a6efbd2d908f0acdafca6f4a6ffe5a3eb77d4ec634d6f9359bfcb0a36538531}"
candidate_image="${CANDIDATE_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}"
expected_version="${EXPECTED_POSTGRES_VERSION:-17.4}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

actual_base_id="$(docker image inspect "$base_image" --format '{{.Id}}')"
[[ "$actual_base_id" == "$expected_base_id" ]] || {
  printf 'base image mismatch: expected %s, got %s\n' "$expected_base_id" "$actual_base_id" >&2
  exit 1
}

DOCKER_BUILDKIT=0 docker build \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "EXPECTED_POSTGRES_VERSION=$expected_version" \
  --file "$repo_root/images/postgres-security/Dockerfile" \
  --tag "$candidate_image" \
  "$repo_root/images/postgres-security"

mapfile -t base_layers < <(docker image inspect "$base_image" --format '{{range .RootFS.Layers}}{{println .}}{{end}}')
mapfile -t candidate_layers < <(docker image inspect "$candidate_image" --format '{{range .RootFS.Layers}}{{println .}}{{end}}')
for index in "${!base_layers[@]}"; do
  [[ "${candidate_layers[$index]:-}" == "${base_layers[$index]}" ]] || {
    printf 'candidate does not preserve base layer %s\n' "$index" >&2
    exit 1
  }
done

actual_version="$(docker run --rm "$candidate_image" postgres --version | awk '{print $3}')"
[[ "$actual_version" == "$expected_version" ]] || {
  printf 'PostgreSQL version mismatch: expected %s, got %s\n' "$expected_version" "$actual_version" >&2
  exit 1
}

printf 'candidate=%s\nimage_id=%s\npostgres_version=%s\nbase_id=%s\n' \
  "$candidate_image" \
  "$(docker image inspect "$candidate_image" --format '{{.Id}}')" \
  "$actual_version" \
  "$actual_base_id"
