#!/usr/bin/env bash
# Reproduce the qualified exporter without changing any running service.
set -euo pipefail

root="${1:?usage: build-postgres-exporter-security.sh NEW_SCRATCH_DIRECTORY}"
target="${2:-snape/dune-postgres-exporter:security-0.20.1-20261109}"
revision=867fbcac31cd18c143e244190ea9168cca069827
runtime=quay.io/prometheuscommunity/postgres-exporter@sha256:ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e

[[ "$(docker info --format '{{.Architecture}}')" == x86_64 ]] || {
  echo 'This qualification targets native Linux amd64.' >&2; exit 1;
}
[[ ! -e "$root" ]] || { echo 'Use a new task scratch directory.' >&2; exit 1; }
mkdir "$root"
source "$(dirname "${BASH_SOURCE[0]}")/security-images/go-builder.sh"
root="$(realpath "$root")"
git clone --depth 1 --branch v0.20.1 https://github.com/prometheus-community/postgres_exporter.git "$root/source"
[[ "$(git -C "$root/source" rev-parse HEAD)" == "$revision" ]]
docker pull "$runtime"

# DAC_OVERRIDE permits writes to the task checkout owned by the invoking user.
# No host credentials, Docker socket, host PID namespace or host root are mounted.
docker run --rm --cpus=2 --memory=4g --cap-drop ALL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  -e GOMAXPROCS=2 -e GOFLAGS='-p=2 -buildvcs=false' -e GOTOOLCHAIN=local -e CGO_ENABLED=0 \
  -v "$root/source:/src" \
  -w /src "$builder" sh -ec '
    go get golang.org/x/net@v0.62.0
    go test -short ./...
    go build -trimpath -ldflags "-s -w -X github.com/prometheus/common/version.Version=0.20.1 -X github.com/prometheus/common/version.Revision=867fbcac31cd18c143e244190ea9168cca069827" -o postgres_exporter ./cmd/postgres_exporter
  '
git -C "$root/source" diff -- go.mod go.sum > "$root/dependency.patch"
docker build --no-cache --pull=false -t "$target" -f - "$root/source" <<EOF
FROM $runtime
COPY postgres_exporter /bin/postgres_exporter
EOF
docker image inspect "$target" > "$root/image.json"
echo "Built $target; scan and runtime-qualify its immutable image ID before promotion. Evidence: $root"
