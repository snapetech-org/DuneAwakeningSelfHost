#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${1:-.env}"
if [[ "$env_file" != /* ]]; then env_file="$repo_root/$env_file"; fi
service_user="${DUNE_SERVICE_USER:-$(stat -c %U "$repo_root")}"
service_group="${DUNE_SERVICE_GROUP:-$(stat -c %G "$repo_root")}"
service_template="$repo_root/config/systemd/dune-restart-api.service"
socket_template="$repo_root/config/systemd/dune-restart-api.socket"

if [[ ! -f "$env_file" || ! -x "$repo_root/scripts/dune_restart_api.py" ]]; then
  printf 'restart API prerequisites are missing\n' >&2
  exit 66
fi
if ! id -nG "$service_user" | tr ' ' '\n' | grep -qx docker; then
  printf 'restart API service user must belong to the docker group\n' >&2
  exit 77
fi

service_tmp="$(mktemp)"
socket_tmp="$(mktemp)"
trap 'rm -f "$service_tmp" "$socket_tmp"' EXIT
for template in "$service_template" "$socket_template"; do
  [[ -f "$template" ]] || { printf 'unit template is missing: %s\n' "$template" >&2; exit 66; }
done

sed \
  -e "s#DUNE_SERVICE_USER_PLACEHOLDER#$service_user#g" \
  -e "s#DUNE_SERVICE_GROUP_PLACEHOLDER#$service_group#g" \
  -e "s#DUNE_RESTART_API_ROOT_PLACEHOLDER#$repo_root#g" \
  -e "s#DUNE_RESTART_API_ENV_PLACEHOLDER#$env_file#g" \
  "$service_template" > "$service_tmp"
sed \
  -e "s#DUNE_SERVICE_USER_PLACEHOLDER#$service_user#g" \
  -e "s#DUNE_SERVICE_GROUP_PLACEHOLDER#$service_group#g" \
  "$socket_template" > "$socket_tmp"

install_unit() {
  local src="$1" dst="$2"
  if [[ -w /etc/systemd/system ]]; then
    install -m 0644 "$src" "$dst"
  else
    sudo install -m 0644 "$src" "$dst"
  fi
}
install_unit "$service_tmp" /etc/systemd/system/dune-restart-api.service
install_unit "$socket_tmp" /etc/systemd/system/dune-restart-api.socket

if [[ "$(id -u)" -eq 0 ]]; then
  systemctl daemon-reload
  systemctl enable --now dune-restart-api.socket
else
  sudo systemctl daemon-reload
  sudo systemctl enable --now dune-restart-api.socket
fi
systemctl status dune-restart-api.socket --no-pager -n 3 2>/dev/null || sudo systemctl status dune-restart-api.socket --no-pager -n 3
