#!/usr/bin/env bash
set -euo pipefail

base_image="${BASE_IMAGE:-postgres@sha256:005c5c37db96c1e6652a79ea31f9886402fba81ac95dc2db897739121dd7f8f2}"
expected_base_id="${EXPECTED_BASE_IMAGE_ID:-sha256:005c5c37db96c1e6652a79ea31f9886402fba81ac95dc2db897739121dd7f8f2}"
go_builder_image="${GO_BUILDER_IMAGE:-golang:1.27.4-alpine3.24@sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586}"
expected_go_builder_id="${EXPECTED_GO_BUILDER_ID:-sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586}"
candidate_image="${CANDIDATE_IMAGE:-snape/dune-postgres:security-17.12-alpine3.24-gosu1.19-go1.27.4-20261114}"
expected_version="${EXPECTED_POSTGRES_VERSION:-17.12}"
gosu_commit="${GOSU_COMMIT:-6456aaa0f3c854d199d0f037f068eb97515b7513}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker pull --platform linux/amd64 "$base_image"
docker pull --platform linux/amd64 "$go_builder_image"
actual_base_id="$(docker image inspect "$base_image" --format '{{.Id}}')"
actual_go_builder_id="$(docker image inspect "$go_builder_image" --format '{{.Id}}')"
[[ "$actual_base_id" == "$expected_base_id" ]] || {
  printf 'base image mismatch: expected %s, got %s\n' "$expected_base_id" "$actual_base_id" >&2
  exit 1
}
[[ "$actual_go_builder_id" == "$expected_go_builder_id" ]] || {
  printf 'Go builder image mismatch: expected %s, got %s\n' "$expected_go_builder_id" "$actual_go_builder_id" >&2
  exit 1
}

DOCKER_BUILDKIT=0 docker build --platform linux/amd64 \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "GO_BUILDER_IMAGE=$go_builder_image" \
  --build-arg "GOSU_COMMIT=$gosu_commit" \
  --build-arg "EXPECTED_POSTGRES_VERSION=$expected_version" \
  --file "$repo_root/images/postgres-security/Dockerfile" \
  --tag "$candidate_image" \
  "$repo_root/images/postgres-security"

actual_version="$(docker run --rm --entrypoint postgres "$candidate_image" --version | awk '{print $3}')"
[[ "$actual_version" == "$expected_version" ]] || {
  printf 'PostgreSQL version mismatch: expected %s, got %s\n' "$expected_version" "$actual_version" >&2
  exit 1
}
docker run --rm --entrypoint sh "$candidate_image" -ec \
  'grep -q "^3\.24\." /etc/alpine-release; test -f "$(pg_config --pkglibdir)/pg_trgm.so"; test -f "$(pg_config --pkglibdir)/pgcrypto.so"; python3 --version; pg_restore --version'

printf 'candidate=%s\nimage_id=%s\npostgres_version=%s\nbase_id=%s\n' \
  "$candidate_image" \
  "$(docker image inspect "$candidate_image" --format '{{.Id}}')" \
  "$actual_version" \
  "$actual_base_id"
printf 'gosu_commit=%s\ngo_builder_id=%s\n' "$gosu_commit" "$actual_go_builder_id"
