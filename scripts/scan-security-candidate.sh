#!/usr/bin/env bash
# Export and scan the very archive an operator will load. A failing report is retained.
set -euo pipefail
tag=${1:?usage: scan-security-candidate.sh LOCAL_CANDIDATE OUTPUT_DIRECTORY}
required=${3:?missing required Trivy result type (os-pkgs, gobinary or dotnet-core)}
out=$(realpath "${2:?missing output directory}")
id=$(docker image inspect --format '{{.Id}}' "$tag")
[[ $id =~ ^sha256:[0-9a-f]{64}$ ]]
scripts=$(cd "$(dirname "$0")" && pwd)
cd "$out"
for file in candidate.tar scan.json SHA256SUMS candidate.id candidate.tag debian-tracker.sha256 candidate.openvex.json scan.adjusted.json scan.installed-release.json; do
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
check_report() {
  jq -e --arg config "sha256:$digest" --arg required "$required" '
    .Metadata.ImageID == $config and
    ((.Errors // []) | length == 0) and all(.Results[]; ((.Errors // []) | length == 0)) and
    any(.Results[]; .Class == $required or .Type == $required) and
    (if .Metadata.OS then any(.Results[]; .Class == "os-pkgs") else true end)
  ' "$1" >/dev/null
}
check_report scan.json
report=scan.json
files=(candidate.tar scan.json candidate.id candidate.tag)
# Trivy can report the vendor base release after an in-place Alpine upgrade.
# Cross-check the final filesystem, APK database and repository branch before a
# second scan. Keep both reports and require zero findings in both.
if [[ ${4:-} == db-utils ]]; then
  release=$(docker run --rm --read-only --network none --cap-drop ALL --entrypoint sh "$id" -ec '
    release=$(cat /etc/alpine-release)
    . /usr/lib/os-release
    test "$ID" = alpine
    test "$VERSION_ID" = "$release"
    test "$(apk info -v | grep "^alpine-release-")" = "alpine-release-$release-r0"
    test "$(cat /etc/apk/repositories)" = "$(printf "%s\n" https://dl-cdn.alpinelinux.org/alpine/v3.22/main https://dl-cdn.alpinelinux.org/alpine/v3.22/community)"
    printf "%s\n" "$release"
  ')
  [[ $release =~ ^3\.22\.[0-9]+$ ]]
  if ! jq -e --arg release "$release" '.Metadata.OS.Name == $release' scan.json >/dev/null; then
    jq -e --arg release "$release-r0" '
      .Metadata.OS.Family == "alpine" and (.Metadata.OS.Name | startswith("3.21.")) and
      any(.Results[].Packages[]?; .Name == "alpine-release" and .Version == $release) and
      all(.Results[]; ((.Vulnerabilities // []) | length == 0))
    ' scan.json >/dev/null
    report=scan.installed-release.json
    trivy image --input candidate.tar --distro "alpine/$release" --skip-db-update --scanners vuln \
      --pkg-types os,library --severity HIGH,CRITICAL --ignorefile /dev/null --format json --output "$report" --timeout 30m
    check_report "$report"
    jq -e --arg release "$release" '.Metadata.OS.Name == $release' "$report" >/dev/null
    files+=("$report")
  fi
fi
if [[ ${4:-} == steamcmd ]] && jq -e 'any(.Results[].Vulnerabilities[]?; true)' scan.json >/dev/null; then
  # Assess only previously reviewed source/advisory pairs, using today's Debian
  # tracker and this candidate's installed binary AND source package versions.
  tracker=$(mktemp "$out/.debian-tracker.XXXXXX")
  trap 'rm -f -- "$tracker"' EXIT
  curl -fsSL --retry 2 https://security-tracker.debian.org/tracker/data/json -o "$tracker"
  sha256sum "$tracker" | cut -d' ' -f1 > debian-tracker.sha256
  python3 "$scripts/security-images/steamcmd/assess-debian.py" "$id" scan.json "$tracker" candidate.openvex.json
  report=scan.adjusted.json
  trivy image --input candidate.tar --scanners vuln --pkg-types os,library --severity HIGH,CRITICAL \
    --ignorefile /dev/null --skip-db-update --vex candidate.openvex.json --format json --output "$report" --timeout 30m
  check_report "$report"
  files+=(debian-tracker.sha256 candidate.openvex.json "$report")
fi
jq -e '.Metadata.OS.EOSL != true and all(.Results[]; ((.Vulnerabilities // []) | length == 0))' "$report" >/dev/null || {
  echo "Candidate failed the scan gate. See $out/$report" >&2
  exit 1
}
sha256sum "${files[@]}" > SHA256SUMS
