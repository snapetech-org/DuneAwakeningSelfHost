#!/usr/bin/env bash
# Build only; production promotion requires independent scan and metric checks.
set -euo pipefail
root="${1:?usage: build-cadvisor-security.sh NEW_SCRATCH_DIRECTORY [CANDIDATE_TAG]}"
target="${2:-snape/dune-cadvisor:security-0.60.6-20261112}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ ! -e "$root" ]]
mkdir "$root"
source "$(dirname "${BASH_SOURCE[0]}")/security-images/go-builder.sh"
root="$(realpath "$root")"
git clone --depth 1 --branch v0.60.6 https://github.com/google/cadvisor.git "$root/source"
[[ "$(git -C "$root/source" rev-parse HEAD)" == 5bf5d43ac6f60d7ee36a13b4d5b4a78ad2d0abc7 ]]
docker build --no-cache --pull=false --build-arg "GO_BUILDER_IMAGE=$builder" --iidfile "$root/builder.id" \
  -f "$script_dir/security-images/cadvisor-builder.Dockerfile" "$root/source"
docker run --rm --cpus=2 --memory=6g --cap-drop ALL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges -e GOPATH=/go -e GOMAXPROCS=2 \
  -e GOFLAGS='-p=2 -buildvcs=false' -e GOTOOLCHAIN=local -e CGO_ENABLED=1 \
  -v "$root/source:/src" \
  -w /src \
  "$(cat "$root/builder.id")" sh -ec '
    for directory in lib . cmd; do
      (cd "$directory"; go get golang.org/x/net@v0.62.0 github.com/klauspost/compress@v1.18.7 google.golang.org/grpc@v1.83.2)
    done
    (cd lib; go test -short ./metrics/... ./model/...)
    go test -short ./info/...
    cd cmd
    go mod tidy
    go build -tags=libpfm,netgo -trimpath -ldflags "-s -w -X github.com/google/cadvisor/lib/version.Version=v0.60.6 -X github.com/google/cadvisor/lib/version.Revision=5bf5d43ac6f60d7ee36a13b4d5b4a78ad2d0abc7" -o /src/cadvisor .
  '
git -C "$root/source" diff -- '*.mod' '*.sum' > "$root/dependency.patch"
docker build --no-cache --pull=false -t "$target" -f - "$root/source" <<DOCKERFILE
FROM ghcr.io/google/cadvisor@sha256:b8e7d1093144fd088f425ff003d75a4aa405de075db78dae3bc563730b1bd07a
RUN apk --no-cache upgrade
COPY cadvisor /usr/bin/cadvisor
DOCKERFILE
docker image inspect "$target" > "$root/image.json"
echo "Built $target; scan and runtime-qualify its immutable image before promotion."
