#!/usr/bin/env bash
# Reproduce the qualified Prometheus without changing any running service.
set -euo pipefail

root="${1:?usage: build-prometheus-security.sh NEW_SCRATCH_DIRECTORY}"
target="${2:-snape/dune-prometheus:security-3.16.0-20261109}"
revision=e2e28adeee2c5883d000fc82aca6008cfbf7e96d
runtime=quay.io/prometheus/prometheus@sha256:9332054523223e2ddc18d8101799d54a4a8d823b030c53216f063a6c117773cd

[[ "$(docker info --format '{{.Architecture}}')" == x86_64 ]] || {
  echo 'This qualification targets native Linux amd64.' >&2; exit 1;
}
[[ ! -e "$root" ]] || { echo 'Use a new task scratch directory.' >&2; exit 1; }
mkdir "$root"
source "$(dirname "${BASH_SOURCE[0]}")/security-images/go-builder.sh"
root="$(realpath "$root")"
git clone --depth 1 --branch v3.16.0 https://github.com/prometheus/prometheus.git "$root/source"
[[ "$(git -C "$root/source" rev-parse HEAD)" == "$revision" ]]
docker pull "$runtime"

(
  cd "$root/source"
  curl -fsSL --retry 2 -o ../ui.tar.gz https://github.com/prometheus/prometheus/releases/download/v3.16.0/prometheus-web-ui-3.16.0.tar.gz
  printf '%s  %s\n' 000a45231f6dd22a5703218aad3ee11da125c8a44d1824a27670416c07531001 ../ui.tar.gz | sha256sum -c -
  tar -xzf ../ui.tar.gz -C web/ui
  bash scripts/compress_assets.sh
)
# DAC_OVERRIDE permits writes to the task checkout owned by the invoking user.
# No host credentials, Docker socket, host PID namespace or host root are mounted.
docker run --rm --cpus=2 --memory=6g --cap-drop ALL --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  -e GOMAXPROCS=2 -e GOFLAGS='-p=2 -buildvcs=false' -e GOTOOLCHAIN=local -e CGO_ENABLED=0 \
  -v "$root/source:/src" \
  -w /src "$builder" sh -ec '
    go get golang.org/x/net@v0.62.0
    go test -tags netgo,builtinassets ./web/...
    go build -tags netgo,builtinassets -trimpath -ldflags "-s -w -X github.com/prometheus/common/version.Version=3.16.0 -X github.com/prometheus/common/version.Revision=e2e28adeee2c5883d000fc82aca6008cfbf7e96d" -o prometheus ./cmd/prometheus
    go build -tags netgo,builtinassets -trimpath -ldflags "-s -w -X github.com/prometheus/common/version.Version=3.16.0 -X github.com/prometheus/common/version.Revision=e2e28adeee2c5883d000fc82aca6008cfbf7e96d" -o promtool ./cmd/promtool
  '
git -C "$root/source" diff -- go.mod go.sum > "$root/dependency.patch"
docker build --no-cache --pull=false -t "$target" -f - "$root/source" <<EOF
FROM $runtime
COPY prometheus /bin/prometheus
COPY promtool /bin/promtool
EOF
docker image inspect "$target" > "$root/image.json"
echo "Built $target; scan and runtime-qualify its immutable image ID before promotion. Evidence: $root"
