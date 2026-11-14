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
  --no-cache \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "EXPECTED_POSTGRES_VERSION=$expected_version" \
  --file "$repo_root/images/postgres-security/Dockerfile" \
  --tag "$candidate_image" \
  "$repo_root/images/postgres-security"

python3 - "$base_image" "$candidate_image" <<'PY'
import json
import subprocess
import sys


def layers(image):
    raw = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{json .RootFS.Layers}}"],
        text=True,
    )
    return json.loads(raw)


base_layers, candidate_layers = map(layers, sys.argv[1:])
if candidate_layers[: len(base_layers)] != base_layers:
    raise SystemExit("candidate does not preserve the exact base layer prefix")
print(
    f"verified {len(base_layers)} unchanged base layers and "
    f"{len(candidate_layers) - len(base_layers)} candidate layer(s)"
)
PY

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
