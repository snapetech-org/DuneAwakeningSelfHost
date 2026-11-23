#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/install-autoheal-services.sh [ENV_FILE] [UNIT_DIR]

Installs and enables the production auto-healing services:
  dune-map-watchdog.service
  dune-director-watchdog.service
  dune-lan-reflection.service
  dune-lan-reflection.timer

The LAN reflection service is intentionally run by a timer because it is
idempotent and firewall reloads can remove runtime bridge rules while systemd
still considers a RemainAfterExit service active.
USAGE
}

if [[ $# -gt 2 ]]; then
  usage
  exit 2
fi

env_file="${1:-.env}"
unit_dir="${2:-/etc/systemd/system}"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_path="$env_file"
if [[ "$env_path" != /* ]]; then
  env_path="$repo_root/$env_path"
fi

if [[ ! -f "$env_path" ]]; then
  printf 'env file does not exist: %s\n' "$env_path" >&2
  exit 1
fi

for template in \
  dune-map-watchdog.service \
  dune-director-watchdog.service \
  dune-lan-reflection.service \
  dune-lan-reflection.timer
do
  if [[ ! -f "$repo_root/config/systemd/$template" ]]; then
    printf 'unit template does not exist: %s\n' "$template" >&2
    exit 1
  fi
done

run_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_rendered() {
  local template="$1"
  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN

  case "$template" in
    dune-map-watchdog.service)
      sed \
        -e "s#^WorkingDirectory=.*#WorkingDirectory=$repo_root#" \
        -e "s#^ExecStart=.*#ExecStart=$repo_root/scripts/watch-maps.sh $env_path#" \
        "$repo_root/config/systemd/$template" > "$tmp"
      ;;
    dune-director-watchdog.service)
      sed \
        -e "s#^WorkingDirectory=.*#WorkingDirectory=$repo_root#" \
        -e "s#^ExecStart=.*#ExecStart=$repo_root/scripts/director-watchdog.sh $env_path#" \
        "$repo_root/config/systemd/$template" > "$tmp"
      ;;
    dune-lan-reflection.service)
      sed \
        -e "s#^WorkingDirectory=.*#WorkingDirectory=$repo_root#" \
        -e "s#^ExecStart=.*#ExecStart=$repo_root/scripts/setup-lan-reflection.sh $env_path --seed-changed-neighbors#" \
        "$repo_root/config/systemd/$template" > "$tmp"
      ;;
    *)
      cp "$repo_root/config/systemd/$template" "$tmp"
      ;;
  esac

  run_root install -m 0644 "$tmp" "$unit_dir/$template"
  rm -f "$tmp"
  trap - RETURN
}

for template in \
  dune-map-watchdog.service \
  dune-director-watchdog.service \
  dune-lan-reflection.service \
  dune-lan-reflection.timer
do
  install_rendered "$template"
done

if command -v systemctl >/dev/null 2>&1; then
  run_root systemctl daemon-reload
  # The timer owns this oneshot. Stop and disable an older standalone
  # service instance so its stale active state cannot suppress timer runs.
  run_root systemctl disable dune-lan-reflection.service 2>/dev/null || true
  run_root systemctl stop dune-lan-reflection.service 2>/dev/null || true
  run_root systemctl enable --now dune-map-watchdog.service
  run_root systemctl enable --now dune-director-watchdog.service
  run_root systemctl enable --now dune-lan-reflection.timer
  run_root systemctl start dune-lan-reflection.service
fi

printf 'installed auto-heal services from %s\n' "$repo_root"
