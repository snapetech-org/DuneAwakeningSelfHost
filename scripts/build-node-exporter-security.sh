#!/usr/bin/env bash
# Reproduce the qualified exporter without changing any running service.
set -euo pipefail

root="${1:?usage: build-node-exporter-security.sh NEW_SCRATCH_DIRECTORY}"
target="${2:-snape/dune-node-exporter:security-1.12.1-20261109}"
revision=6044da783597cc3b57aef7580ddcdcff58a4ee99
runtime=quay.io/prometheus/node-exporter@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0

[[ "$(docker info --format '{{.Architecture}}')" == x86_64 ]] || {
  echo 'This qualification targets native Linux amd64.' >&2; exit 1;
}
[[ ! -e "$root" ]] || { echo 'Use a new task scratch directory.' >&2; exit 1; }
mkdir "$root"
source "$(dirname "${BASH_SOURCE[0]}")/security-images/go-builder.sh"
root="$(realpath "$root")"
git clone --depth 1 --branch v1.12.1 https://github.com/prometheus/node_exporter.git "$root/source"
[[ "$(git -C "$root/source" rev-parse HEAD)" == "$revision" ]]
docker pull "$runtime"

(
  cd "$root/source"
  ./ttar -C collector/fixtures -x -f collector/fixtures/sys.ttar
  ./ttar -C collector/fixtures -x -f collector/fixtures/udev.ttar
)
# DAC_OVERRIDE permits writes to the task checkout owned by the invoking user.
# No host credentials, Docker socket, host PID namespace or host root are mounted.
docker run --rm --cpus=2 --memory=4g --cap-drop ALL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  -e GOMAXPROCS=2 -e GOFLAGS='-p=2 -buildvcs=false' -e GOTOOLCHAIN=local -e CGO_ENABLED=0 \
  -v "$root/source:/src" \
  -w /src "$builder" sh -ec '
    go get golang.org/x/net@v0.62.0
    go test -short ./collector
    go build -trimpath -ldflags "-s -w -X github.com/prometheus/common/version.Version=1.12.1 -X github.com/prometheus/common/version.Revision=6044da783597cc3b57aef7580ddcdcff58a4ee99" -o node_exporter .
  '
git -C "$root/source" diff -- go.mod go.sum > "$root/dependency.patch"
docker build --no-cache --pull=false -t "$target" -f - "$root/source" <<EOF
FROM $runtime
COPY node_exporter /bin/node_exporter
EOF
docker image inspect "$target" > "$root/image.json"
echo "Built $target; scan and runtime-qualify its immutable image ID before promotion. Evidence: $root"
