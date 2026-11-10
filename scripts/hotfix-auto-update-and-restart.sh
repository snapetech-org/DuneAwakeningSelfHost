#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: ./scripts/hotfix-auto-update-and-restart.sh [env-file]

Polls the owned Steam package for Dune hotfixes, loads updated Funcom Docker
images, writes DUNE_IMAGE_TAG when needed, and restarts the live farm only when
the package changed. Intended for systemd timer use on kspls0.
USAGE
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

env_file="${1:-.env}"
script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

if [[ "$(hostname)" != "kspls0" ]]; then
  printf 'fail: must run on kspls0; current host is %s\n' "$(hostname)" >&2
  exit 1
fi

if [[ ! -f "$env_file" ]]; then
  printf 'fail: env file not found: %s\n' "$env_file" >&2
  exit 1
fi

lock_file="${DUNE_HOTFIX_UPDATE_LOCK_FILE:-/tmp/dune-hotfix-auto-update.lock}"
exec 9>"$lock_file"
if ! flock -n 9; then
  printf 'another hotfix update run is already active; exiting\n'
  exit 0
fi

export DUNE_RESTART_CHECK_STEAM_UPDATE=false
require_readiness="${DUNE_UPDATE_REQUIRE_READINESS_RECEIPT:-true}"
allow_uncertified_auto_apply="${DUNE_HOTFIX_AUTO_APPLY_WITHOUT_READINESS:-false}"
if [[ "$require_readiness" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])$ ]] \
  && [[ ! "$allow_uncertified_auto_apply" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])$ ]]; then
  "$script_dir/update-steam-tool.sh" "$env_file"
  check_file="$(mktemp)"
  trap 'rm -f "$check_file"' EXIT
  package_check_args=()
  auto_qualify="$(awk -F= '$1 == "DUNE_AUTO_QUALIFY_GAME_IMAGE" {print $2; exit}' "$env_file")"
  if [[ "${DUNE_AUTO_QUALIFY_GAME_IMAGE:-${auto_qualify:-false}}" =~ ^(1|true|yes|on)$ ]]; then
    package_check_args+=(--package-only)
  fi
  set +e
  "$script_dir/check-steam-update.sh" "$env_file" "${package_check_args[@]}" 2>&1 | tee "$check_file"
  check_rc="${PIPESTATUS[0]}"
  set -e
  case "$check_rc" in
    0)
      printf 'Steam candidate is current; readiness policy leaves the live farm unchanged\n'
      ;;
    1)
      if grep -Eq 'status: update available|status: same tag but Steam build changed' "$check_file"; then
        printf 'Steam candidate staged; readiness receipt required before image load or farm restart\n'
      else
        printf 'fail: candidate check returned 1 without a recognized staged update state\n' >&2
        exit 1
      fi
      ;;
    *)
      printf 'fail: staged Steam candidate is incomplete or unreadable\n' >&2
      exit "$check_rc"
      ;;
  esac
  exit 0
fi

"$script_dir/update-owned-steam-build-and-restart.sh" \
  "$env_file" \
  --yes \
  --non-interactive \
  --restart-only-on-update
