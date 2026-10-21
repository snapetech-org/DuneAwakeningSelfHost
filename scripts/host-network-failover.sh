#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
remote_arg="${2:-}"
target_role="${3:-${DUNE_FAILOVER_TARGET_ROLE:-${ROLE:-standby}}}"

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

remote="${remote_arg:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}"
remote_repo="${DUNE_STANDBY_REPO_ROOT:-$PWD}"
primary_host="${DUNE_FAILOVER_PRIMARY_HOST:-$(read_env DUNE_FAILOVER_PRIMARY_HOST)}"
standby_host="${DUNE_FAILOVER_STANDBY_HOST:-$(read_env DUNE_FAILOVER_STANDBY_HOST)}"
public_ip="${DUNE_FAILOVER_PUBLIC_IP:-${DUNE_PUBLIC_IP:-${EXTERNAL_ADDRESS:-$(read_env EXTERNAL_ADDRESS)}}}"
game_rmq_port="${GAME_RMQ_PUBLIC_PORT:-$(read_env GAME_RMQ_PUBLIC_PORT)}"; game_rmq_port="${game_rmq_port:-31982}"
game_udp_range="${GAME_UDP_PORT_RANGE:-$(read_env GAME_UDP_PORT_RANGE)}"; game_udp_range="${game_udp_range:-7777:7871}"
igw_udp_range="${IGW_UDP_PORT_RANGE:-$(read_env IGW_UDP_PORT_RANGE)}"; igw_udp_range="${igw_udp_range:-7888:7982}"
game_udp_start="${game_udp_range%%:*}"
game_udp_end="${game_udp_range##*:}"
igw_udp_start="${igw_udp_range%%:*}"
igw_udp_end="${igw_udp_range##*:}"
primary_ip="${DUNE_FAILOVER_PRIMARY_LAN_IP:-${DUNE_PRIMARY_LAN_IP:-$(read_env DUNE_FAILOVER_PRIMARY_LAN_IP)}}"
standby_ip="${DUNE_FAILOVER_STANDBY_LAN_IP:-${DUNE_STANDBY_LAN_IP:-$(read_env DUNE_FAILOVER_STANDBY_LAN_IP)}}"

if [[ "$remote_arg" == "primary" || "$remote_arg" == "standby" || "$remote_arg" == "current" ]]; then
  target_role="$remote_arg"
  remote="$(read_env POSTGRES_REMOTE_REPLICA_HOST)"
fi

case "$target_role" in
  current)
    target_host="${DUNE_CURRENT_HOST:-$(read_env DUNE_CURRENT_HOST)}"
    target_ip="${DUNE_CURRENT_LAN_IP:-$(read_env DUNE_CURRENT_LAN_IP)}"
    source_host=""
    source_ip=""
    if [[ "$target_host" == "$primary_host" || "$target_ip" == "$primary_ip" ]]; then
      source_host="$standby_host"
      source_ip="$standby_ip"
    else
      source_host="$primary_host"
      source_ip="$primary_ip"
    fi
    ;;
  primary)
    target_host="$primary_host"
    target_ip="$primary_ip"
    source_host="$standby_host"
    source_ip="$standby_ip"
    ;;
  standby)
    target_host="$standby_host"
    target_ip="$standby_ip"
    source_host="$primary_host"
    source_ip="$primary_ip"
    ;;
  *)
    printf 'usage: %s ENV_FILE [REMOTE|primary|standby|current] [primary|standby|current]\n' "$0" >&2
    exit 2
    ;;
esac

if [[ -z "$remote" || -z "$primary_host" || -z "$standby_host" || -z "$public_ip" || -z "$primary_ip" || -z "$standby_ip" || -z "$target_host" || -z "$target_ip" || -z "$source_host" ]]; then
  printf 'POSTGRES_REMOTE_REPLICA_HOST, DUNE_FAILOVER_PRIMARY_HOST, DUNE_FAILOVER_STANDBY_HOST, EXTERNAL_ADDRESS/DUNE_PUBLIC_IP, DUNE_FAILOVER_PRIMARY_LAN_IP, DUNE_FAILOVER_STANDBY_LAN_IP, and current/target host details are required\n' >&2
  exit 1
fi

is_local_host() {
  local host="$1"
  [[ "$host" == "localhost" || "$host" == "$(hostname)" || "$host" == "$(hostname -s)" ]]
}

host_exec() {
  local host="$1"; shift
  if is_local_host "$host"; then
    "$@"
  else
    ssh "$host" "$*"
  fi
}

host_shell() {
  local host="$1" command="$2"
  if is_local_host "$host"; then
    bash -lc "$command"
  else
    ssh "$host" "$command"
  fi
}

remove_public_ip() {
  local host="$1"
  local command
  command="ip -o addr show | awk -v ip='$public_ip/32' '\$0 ~ ip {print \$2}' | sort -u | while read -r dev; do [ -n \"\$dev\" ] && sudo -n ip addr del '$public_ip/32' dev \"\$dev\" 2>/dev/null || true; done"
  host_shell "$host" "$command"
}

# Strip the host-self OUTPUT REDIRECT rules that setup-lan-reflection.sh
# installs while a host owns the public IP. Leaving them in place on the OLD
# active host silently hijacks any local-to-public-IP probe (nc, curl,
# diagnostic capture) into 127.0.0.1, where nothing useful is listening
# after cutover. Idempotent: missing rules are skipped.
remove_self_redirects() {
  local host="$1"
  local command
  command="set +e
for proto_args in '-p tcp --dport $game_rmq_port' '-p udp --dport $game_udp_range' '-p udp --dport $igw_udp_range'; do
  while sudo -n iptables -t nat -C OUTPUT -d '$public_ip/32' \$proto_args -j REDIRECT 2>/dev/null; do
    sudo -n iptables -t nat -D OUTPUT -d '$public_ip/32' \$proto_args -j REDIRECT
  done
  while sudo -n iptables -t nat -C OUTPUT -d '$public_ip/32' \$proto_args -j DNAT --to-destination 127.0.0.1 2>/dev/null; do
    sudo -n iptables -t nat -D OUTPUT -d '$public_ip/32' \$proto_args -j DNAT --to-destination 127.0.0.1
  done
done
true"
  host_shell "$host" "$command"
}

printf 'target_role=%s target_host=%s target_ip=%s source_host=%s source_ip=%s\n\n' "$target_role" "$target_host" "$target_ip" "$source_host" "$source_ip"

printf '== local host network snapshot ==\n'
ip -brief addr | sed -n '1,80p'
ip route | sed -n '1,80p'
if command -v nft >/dev/null 2>&1; then
  sudo -n nft list ruleset 2>/dev/null | rg -n "$public_ip|$game_rmq_port|$game_udp_start|$game_udp_end|$igw_udp_start|$igw_udp_end|172\\.31\\.240" || true
fi

printf '\n== remote host network snapshot: %s ==\n' "$remote"
ssh "$remote" "ip -brief addr | sed -n '1,80p'
ip route | sed -n '1,80p'
if command -v nft >/dev/null 2>&1; then sudo -n nft list ruleset 2>/dev/null | grep -E '$public_ip|$game_rmq_port|$game_udp_start|$game_udp_end|$igw_udp_start|$igw_udp_end|172\\.31\\.240' || true; fi"

printf '\n== verdict ==\n'
if host_shell "$target_host" "ip -brief addr | grep -q '$public_ip/32'" 2>/dev/null; then
  printf 'OK target %s already owns %s/32\n' "$target_host" "$public_ip"
else
  printf 'WARN target %s does not own %s/32 yet\n' "$target_host" "$public_ip"
fi

if [[ "${CONFIRM_HOST_NETWORK_FAILOVER:-}" != "yes" ]]; then
  cat <<EOF
Dry run only. To apply host-side failover networking for ${target_role}, run:
  CONFIRM_HOST_NETWORK_FAILOVER=yes make host-network-failover ENV_FILE=${env_file} ROLE=${target_role}

This removes ${public_ip}/32 from ${source_host} and runs
scripts/setup-lan-reflection.sh on ${target_host}; it adds ${public_ip}/32,
rp_filter relaxations, Dune bridge MASQUERADE, and local OUTPUT redirects for
${game_rmq_port}/tcp, ${game_udp_range}/udp, and ${igw_udp_range}/udp.
EOF
  exit 0
fi

printf '\n== removing public address from old owner: %s ==\n' "$source_host"
remove_public_ip "$source_host"

printf '\n== stripping stale OUTPUT REDIRECTs from old owner: %s ==\n' "$source_host"
remove_self_redirects "$source_host"

printf '\n== applying host network ownership on %s ==\n' "$target_host"
if is_local_host "$target_host"; then
  DUNE_PUBLIC_IP="$public_ip" DUNE_PRIMARY_LAN_IP="$primary_ip" DUNE_STANDBY_LAN_IP="$standby_ip" ./scripts/setup-lan-reflection.sh "$env_file"
else
  ssh "$target_host" "cd '$remote_repo' && DUNE_PUBLIC_IP='$public_ip' DUNE_PRIMARY_LAN_IP='$primary_ip' DUNE_STANDBY_LAN_IP='$standby_ip' ./scripts/setup-lan-reflection.sh '$env_file'"
fi
host_shell "$target_host" "ip -brief addr | grep '$public_ip/32' && ip route get '$public_ip' || true"
