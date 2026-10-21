#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

external_address="${EXTERNAL_ADDRESS:-$(read_env EXTERNAL_ADDRESS)}"
game_rmq_public_host="${GAME_RMQ_PUBLIC_HOST:-$(read_env GAME_RMQ_PUBLIC_HOST)}"
game_rmq_public_host="${game_rmq_public_host:-$external_address}"
game_rmq_port="${GAME_RMQ_PUBLIC_PORT:-$(read_env GAME_RMQ_PUBLIC_PORT)}"; game_rmq_port="${game_rmq_port:-31982}"
game_udp_range="${GAME_UDP_PORT_RANGE:-$(read_env GAME_UDP_PORT_RANGE)}"; game_udp_range="${game_udp_range:-7777:7871}"
igw_udp_range="${IGW_UDP_PORT_RANGE:-$(read_env IGW_UDP_PORT_RANGE)}"; igw_udp_range="${igw_udp_range:-7888:7982}"

printf '== cutover env ==\n'
printf 'EXTERNAL_ADDRESS=%s\n' "${external_address:-unset}"
printf 'GAME_RMQ_PUBLIC_HOST=%s\n' "${game_rmq_public_host:-unset}"
printf 'GAME_RMQ_PUBLIC_PORT=%s\n' "$game_rmq_port"
printf 'required router forwards: %s/tcp, %s/udp, %s/udp\n' "$game_rmq_port" "$game_udp_range" "$igw_udp_range"

printf '\n== local compose health ==\n'
COMPOSE_FILES="${COMPOSE_FILES:-compose.yaml:compose.failover-standby.yaml:compose.allmaps.yaml}" ./scripts/status.sh "$env_file" | sed -n '/== health verdict ==/,/== database state ==/p'

printf '\n== public TCP probe ==\n'
if [[ -n "$game_rmq_public_host" ]]; then
  timeout 5 bash -c "cat < /dev/null > /dev/tcp/$game_rmq_public_host/$game_rmq_port" \
    && printf 'OK tcp %s:%s reachable\n' "$game_rmq_public_host" "$game_rmq_port" \
    || printf 'WARN tcp %s:%s not reachable from this host\n' "$game_rmq_public_host" "$game_rmq_port"
else
  printf 'WARN GAME_RMQ_PUBLIC_HOST is unset; use the public IP or DNS name after NAT flip\n'
fi
