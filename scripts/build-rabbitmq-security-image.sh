#!/usr/bin/env bash
set -euo pipefail

base_image="${BASE_IMAGE:-registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:2168446-0-shipping}"
expected_base_id="${EXPECTED_BASE_IMAGE_ID:-sha256:395d2d85bb9bd42165421430959a832e2166c7518a31c48932450bbdf4f34f59}"
candidate_image="${CANDIDATE_IMAGE:-snape/dune-rabbitmq:security-2168446}"
expected_version="${EXPECTED_RABBITMQ_VERSION:-3.13.7}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

actual_base_id="$(docker image inspect "$base_image" --format '{{.Id}}')"
[[ "$actual_base_id" == "$expected_base_id" ]] || {
  printf 'base image mismatch: expected %s, got %s\n' "$expected_base_id" "$actual_base_id" >&2
  exit 1
}

DOCKER_BUILDKIT=0 docker build \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "EXPECTED_RABBITMQ_VERSION=$expected_version" \
  --file "$repo_root/images/rabbitmq-security/Dockerfile" \
  --tag "$candidate_image" \
  "$repo_root/images/rabbitmq-security"

mapfile -t base_layers < <(docker image inspect "$base_image" --format '{{range .RootFS.Layers}}{{println .}}{{end}}')
mapfile -t candidate_layers < <(docker image inspect "$candidate_image" --format '{{range .RootFS.Layers}}{{println .}}{{end}}')
for index in "${!base_layers[@]}"; do
  [[ "${candidate_layers[$index]:-}" == "${base_layers[$index]}" ]] || {
    printf 'candidate does not preserve base layer %s\n' "$index" >&2
    exit 1
  }
done

actual_version="$(docker run --rm --entrypoint rabbitmq-diagnostics "$candidate_image" --version)"
[[ "$actual_version" == "$expected_version" ]] || {
  printf 'RabbitMQ version mismatch: expected %s, got %s\n' "$expected_version" "$actual_version" >&2
  exit 1
}

printf 'candidate=%s\nimage_id=%s\nrabbitmq_version=%s\nbase_id=%s\n' \
  "$candidate_image" \
  "$(docker image inspect "$candidate_image" --format '{{.Id}}')" \
  "$actual_version" \
  "$actual_base_id"
