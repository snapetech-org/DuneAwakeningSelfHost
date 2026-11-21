#!/usr/bin/env bash
# Export and scan the very archive an operator will load. A failing report is retained.
set -euo pipefail
tag=${1:?usage: scan-security-candidate.sh LOCAL_CANDIDATE OUTPUT_DIRECTORY}
required=${3:?missing required Trivy result type (os-pkgs, gobinary or dotnet-core)}
out=$(realpath "${2:?missing output directory}")
id=$(docker image inspect --format '{{.Id}}' "$tag")
[[ $id =~ ^sha256:[0-9a-f]{64}$ ]]
cd "$out"
for file in candidate.tar scan.json SHA256SUMS candidate.id candidate.tag; do
  [[ ! -e $file ]] || { echo "Output already exists: $file" >&2; exit 1; }
done
umask 077
printf '%s\n' "$id" > candidate.id
printf '%s\n' "$tag" > candidate.tag
docker save --output candidate.tar "$id"
config=$(tar -xOf candidate.tar manifest.json | jq -er 'select(length == 1) | .[0].Config')
[[ $config =~ ^(blobs/sha256/[a-f0-9]{64}|[a-f0-9]{64}\.json)$ ]]
digest=$(tar -xOf candidate.tar "$config" | sha256sum | cut -d' ' -f1)
trivy image --input candidate.tar --scanners vuln --pkg-types os,library --severity HIGH,CRITICAL \
  --ignorefile /dev/null --format json --output scan.json --timeout 30m
jq -e --arg config "sha256:$digest" --arg required "$required" '
  .Metadata.ImageID == $config and
  ((.Errors // []) | length == 0) and
  any(.Results[]; .Class == $required or .Type == $required) and
  (if .Metadata.OS then any(.Results[]; .Class == "os-pkgs") else true end) and
  all(.Results[]; ((.Errors // []) | length == 0) and ((.Vulnerabilities // []) | length == 0))
' scan.json >/dev/null || { echo "Candidate failed the scan gate. See $out/scan.json" >&2; exit 1; }
sha256sum candidate.tar scan.json candidate.id candidate.tag > SHA256SUMS
