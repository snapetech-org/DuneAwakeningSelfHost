#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  install-release.sh install --ref COMMIT --sha256 SHA256 [--archive FILE | --url HTTPS_URL]
      [--prefix DIR] [--state-root DIR] [--activate] [--dry-run]
  install-release.sh status [--prefix DIR] [--state-root DIR]
  install-release.sh rollback --confirm 'ROLL BACK DASH RELEASE' [--prefix DIR] [--dry-run]

The install path never starts or restarts DASH. A ref must be a full 40-hex Git
commit and the archive checksum is mandatory. The default URL is the official
snapetech GitHub source archive for that exact commit. Official GitHub Release
asset URLs are accepted when their checksum and embedded commit metadata match.
EOF
}

action="${1:-}"
[[ -n "$action" ]] || { usage >&2; exit 2; }
shift

prefix="${DASH_RELEASE_PREFIX:-/opt/dash}"
state_root="${DASH_STATE_ROOT:-/var/lib/dash}"
ref=""
expected_sha=""
archive=""
url=""
activate=false
dry_run=false
confirm=""

while (($#)); do
  case "$1" in
    --prefix) prefix="${2:?missing --prefix value}"; shift 2 ;;
    --state-root) state_root="${2:?missing --state-root value}"; shift 2 ;;
    --ref) ref="${2:?missing --ref value}"; shift 2 ;;
    --sha256) expected_sha="${2:?missing --sha256 value}"; shift 2 ;;
    --archive) archive="${2:?missing --archive value}"; shift 2 ;;
    --url) url="${2:?missing --url value}"; shift 2 ;;
    --activate) activate=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    --confirm) confirm="${2:?missing --confirm value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$prefix" == /* && "$state_root" == /* ]] || { printf 'prefix and state root must be absolute paths\n' >&2; exit 2; }
[[ "$prefix" != / && "$state_root" != / ]] || { printf 'refusing root filesystem as prefix/state root\n' >&2; exit 2; }

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

link_target() {
  local path="$1"
  [[ -L "$path" ]] && readlink -f -- "$path" || true
}

atomic_link() {
  local target="$1" link="$2" temporary
  temporary="${link}.new.$$"
  ln -s -- "$target" "$temporary"
  mv -Tf -- "$temporary" "$link"
}

status() {
  local current previous
  current="$(link_target "$prefix/current")"
  previous="$(link_target "$prefix/previous")"
  printf 'prefix=%s\nstate_root=%s\ncurrent=%s\nprevious=%s\n' "$prefix" "$state_root" "${current:-none}" "${previous:-none}"
  if [[ -n "$current" && -f "$current/.dash-release.json" ]]; then
    cat "$current/.dash-release.json"
  fi
}

case "$action" in
  status)
    status
    exit 0
    ;;
  rollback)
    [[ "$confirm" == "ROLL BACK DASH RELEASE" ]] || { printf 'confirmation required: ROLL BACK DASH RELEASE\n' >&2; exit 77; }
    current="$(link_target "$prefix/current")"
    previous="$(link_target "$prefix/previous")"
    [[ -n "$current" && -d "$current" ]] || { printf 'current release is missing\n' >&2; exit 1; }
    [[ -n "$previous" && -d "$previous" ]] || { printf 'previous release is missing\n' >&2; exit 1; }
    printf 'plan: current %s -> %s; no services will be restarted\n' "$current" "$previous"
    if [[ "$dry_run" == true ]]; then exit 0; fi
    atomic_link "$previous" "$prefix/current"
    atomic_link "$current" "$prefix/previous"
    status
    exit 0
    ;;
  install) ;;
  *) usage >&2; exit 2 ;;
esac

[[ "$ref" =~ ^[0-9a-f]{40}$ ]] || { printf -- '--ref must be a full lowercase 40-hex commit\n' >&2; exit 2; }
[[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || { printf -- '--sha256 must be a full lowercase SHA-256\n' >&2; exit 2; }
[[ -z "$archive" || -z "$url" ]] || { printf 'use only one of --archive or --url\n' >&2; exit 2; }
url="${url:-https://github.com/snapetech/DuneAwakeningSelfHost/archive/${ref}.tar.gz}"
if [[ -z "$archive" ]]; then
  case "$url" in
    https://github.com/snapetech/DuneAwakeningSelfHost/*|https://codeload.github.com/snapetech/DuneAwakeningSelfHost/*) ;;
    *) printf 'remote archive URL must use the official snapetech GitHub repository over HTTPS\n' >&2; exit 77 ;;
  esac
fi

release_dir="$prefix/releases/$ref"
printf 'plan: verify commit=%s sha256=%s\n' "$ref" "$expected_sha"
printf 'plan: stage=%s state=%s activate=%s\n' "$release_dir" "$state_root" "$activate"
if [[ "$dry_run" == true ]]; then exit 0; fi

install -d -m 0755 "$prefix" "$prefix/releases"
install -d -m 0700 "$state_root" "$state_root/backups" "$state_root/data" "$state_root/config-overrides" "$state_root/config-secrets" "$state_root/config-tls"
stage="$(mktemp -d "$prefix/.release-stage.XXXXXX")"
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT

download="$stage/source.tar.gz"
if [[ -n "$archive" ]]; then
  [[ -f "$archive" ]] || { printf 'archive not found: %s\n' "$archive" >&2; exit 1; }
  cp -- "$archive" "$download"
else
  command -v curl >/dev/null || { printf 'curl is required\n' >&2; exit 1; }
  curl --proto '=https' --tlsv1.2 --location --fail --no-progress-meter --output "$download" "$url"
fi
actual_sha="$(sha256sum "$download" | awk '{print $1}')"
[[ "$actual_sha" == "$expected_sha" ]] || { printf 'archive checksum mismatch: expected=%s actual=%s\n' "$expected_sha" "$actual_sha" >&2; exit 1; }

archive_root="$(python3 - "$download" <<'PY'
import pathlib,sys,tarfile
path=pathlib.Path(sys.argv[1])
roots=set()
with tarfile.open(path,'r:gz') as archive:
    members=archive.getmembers()
    if not members or len(members)>20000:
        raise SystemExit('archive member count is empty or exceeds 20000')
    total=0
    for member in members:
        pure=pathlib.PurePosixPath(member.name)
        if pure.is_absolute() or '..' in pure.parts or not pure.parts:
            raise SystemExit(f'unsafe archive path: {member.name}')
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f'links/devices are not accepted in release archives: {member.name}')
        roots.add(pure.parts[0])
        total += max(0, member.size)
        if total > 512*1024*1024:
            raise SystemExit('archive uncompressed size exceeds 512 MiB')
if len(roots)!=1:
    raise SystemExit('release archive must contain exactly one top-level directory')
print(next(iter(roots)))
PY
)"
tar -xzf "$download" --directory "$stage" --no-same-owner --no-same-permissions
extracted="$stage/$archive_root"
[[ -f "$extracted/compose.yaml" && -f "$extracted/.env.example" && -d "$extracted/scripts" ]] || {
  printf 'archive is not a DASH source release\n' >&2; exit 1;
}

release_version=""
if [[ -f "$extracted/RELEASE-METADATA.json" ]]; then
  release_version="$(python3 - "$extracted/RELEASE-METADATA.json" "$ref" <<'PY'
import json,re,sys
path, expected_commit = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    value=json.load(handle)
if value.get("schemaVersion") != "dash-release-metadata/v1":
    raise SystemExit("release metadata schema mismatch")
if value.get("commit") != expected_commit:
    raise SystemExit("release metadata commit does not match --ref")
version=value.get("version")
tag=value.get("tag")
if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version):
    raise SystemExit("release metadata version is invalid")
if tag != "v" + version:
    raise SystemExit("release metadata tag/version mismatch")
platform=value.get("platform") or {}
if platform.get("serverOperatingSystem") != "linux" or platform.get("architecture") != "x86_64" or platform.get("requiredCpuFeature") != "avx2":
    raise SystemExit("release metadata platform contract mismatch")
contents=value.get("contents") or {}
if contents.get("funcomArtifactsIncluded") is not False or contents.get("runtimeStateIncluded") is not False or contents.get("secretsIncluded") is not False:
    raise SystemExit("release metadata private/proprietary exclusion contract mismatch")
print(version)
PY
)"
fi

if [[ -e "$release_dir" ]]; then
  recorded="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha256",""))' "$release_dir/.dash-release.json" 2>/dev/null || true)"
  [[ "$recorded" == "$expected_sha" ]] || { printf 'existing release directory does not match requested checksum\n' >&2; exit 1; }
else
  mv -- "$extracted" "$release_dir"
fi

if [[ ! -f "$state_root/.env" ]]; then
  install -m 0600 "$release_dir/.env.example" "$state_root/.env"
fi

for name in data backups; do
  rm -rf -- "$release_dir/$name"
  ln -s -- "$state_root/$name" "$release_dir/$name"
done
for name in tls secrets; do
  rm -rf -- "$release_dir/config/$name"
  ln -s -- "$state_root/config-$name" "$release_dir/config/$name"
done
rm -f -- "$release_dir/.env"
ln -s -- "$state_root/.env" "$release_dir/.env"

mutable_configs=(
  UserEngine.ini UserEngine.deep-desert.ini UserEngine.deep-desert-pvp.ini
  UserGame.ini UserGame.deep-desert-coriolis.ini UserGame.deep-desert-pvp.ini
  UserServerCustomSettings.ini UserServerCustomSettings.survival.ini
  UserServerCustomSettings.deep-desert-pvp.ini
  director.ini gateway.ini rabbitmq-admin.conf rabbitmq-game.conf admin-ingress.Caddyfile
)
for name in "${mutable_configs[@]}"; do
  source="$release_dir/config/$name"
  shared="$state_root/config-overrides/$name"
  if [[ -f "$source" || -f "$shared" ]]; then
    if [[ ! -f "$shared" ]]; then install -m 0600 "$source" "$shared"; fi
    rm -f -- "$source"
    ln -s -- "$shared" "$source"
  fi
done

installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
release_version_json="null"
if [[ -n "$release_version" ]]; then release_version_json="$(json_string "$release_version")"; fi
printf '%s\n' \
  "{\"version\":1,\"releaseVersion\":${release_version_json},\"commit\":$(json_string "$ref"),\"sha256\":$(json_string "$expected_sha"),\"source\":$(json_string "${archive:-$url}"),\"installedAt\":$(json_string "$installed_at")}" \
  > "$release_dir/.dash-release.json.tmp"
chmod 0644 "$release_dir/.dash-release.json.tmp"
mv -f -- "$release_dir/.dash-release.json.tmp" "$release_dir/.dash-release.json"

if [[ "$activate" == true ]]; then
  current="$(link_target "$prefix/current")"
  if [[ -n "$current" && "$current" != "$release_dir" ]]; then atomic_link "$current" "$prefix/previous"; fi
  atomic_link "$release_dir" "$prefix/current"
fi
status
