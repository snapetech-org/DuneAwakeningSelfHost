#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
router_arg="${2:-}"
target_ip="${3:-${DUNE_FAILOVER_TARGET_LAN_IP:-${DUNE_CURRENT_LAN_IP:-}}}"

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

if [[ -z "${3:-}" && "$router_arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  target_ip="$router_arg"
  router_arg=""
fi

router="${router_arg:-${DUNE_FAILOVER_ROUTER_SSH:-${DUNE_ROUTER_SSH:-$(read_env DUNE_FAILOVER_ROUTER_SSH)}}}"
primary_ip="${DUNE_FAILOVER_PRIMARY_LAN_IP:-${DUNE_PRIMARY_LAN_IP:-$(read_env DUNE_FAILOVER_PRIMARY_LAN_IP)}}"
standby_ip="${DUNE_FAILOVER_STANDBY_LAN_IP:-${DUNE_STANDBY_LAN_IP:-$(read_env DUNE_FAILOVER_STANDBY_LAN_IP)}}"
current_ip="${DUNE_CURRENT_LAN_IP:-$(read_env DUNE_CURRENT_LAN_IP)}"
target_ip="${target_ip:-${current_ip:-$standby_ip}}"
public_ip="${DUNE_FAILOVER_PUBLIC_IP:-${DUNE_PUBLIC_IP:-${EXTERNAL_ADDRESS:-$(read_env EXTERNAL_ADDRESS)}}}"
game_rmq_port="${GAME_RMQ_PUBLIC_PORT:-$(read_env GAME_RMQ_PUBLIC_PORT)}"; game_rmq_port="${game_rmq_port:-31982}"
game_udp_range="${GAME_UDP_PORT_RANGE:-$(read_env GAME_UDP_PORT_RANGE)}"; game_udp_range="${game_udp_range:-7777:7871}"
igw_udp_range="${IGW_UDP_PORT_RANGE:-$(read_env IGW_UDP_PORT_RANGE)}"; igw_udp_range="${igw_udp_range:-7888:7982}"
backup_dir="${DUNE_ROUTER_BACKUP_DIR:-backups/router-inspection}"
timestamp="$(date -u +%Y%m%dT%H%M%S.%NZ)"
backup_file="${backup_dir}/asuswrt-${timestamp}.txt"

mkdir -p "$backup_dir"

if [[ -z "$router" || -z "$target_ip" || -z "$primary_ip" || -z "$standby_ip" || -z "$public_ip" ]]; then
  printf 'DUNE_FAILOVER_ROUTER_SSH, DUNE_FAILOVER_PRIMARY_LAN_IP, DUNE_FAILOVER_STANDBY_LAN_IP, DUNE_CURRENT_LAN_IP or target IP, and EXTERNAL_ADDRESS/DUNE_PUBLIC_IP are required\n' >&2
  exit 1
fi

current="$(ssh "$router" 'nvram get vts_rulelist')"
{
  printf 'router=%s\n' "$router"
  printf 'timestamp=%s\n' "$timestamp"
  printf 'public_ip=%s\n' "$public_ip"
  printf 'primary_ip=%s\n' "$primary_ip"
  printf 'standby_ip=%s\n' "$standby_ip"
  printf 'vts_rulelist=%s\n' "$current"
  ssh "$router" 'nvram get vts_enable_x; nvram get nat_redirect_enable; nvram get game_vts_rulelist' 2>/dev/null || true
} > "$backup_file"

new="$current"
# Iterate primary, standby, and target — the target pass is what makes re-runs
# idempotent: it catches rules already pointing at target_ip but with a stale
# port range (e.g. 7777:7806 leftover from a narrower historical range) and
# normalizes them to the current env range. Without this, drift between env
# and router silently survives every cutover.
for from_ip in "$primary_ip" "$standby_ip" "$target_ip"; do
  new_dune_game="<duneA1>${game_udp_range}>${target_ip}>>UDP>"
  new_dune_igw="<duneA2>${igw_udp_range}>${target_ip}>>UDP>"
  new_dune_rmq="<DuneRMQ>${game_rmq_port}>${target_ip}>${game_rmq_port}>TCP>"
  new="$(printf '%s' "$new" | sed -E "s#<duneA1>[0-9]+:[0-9]+>${from_ip}>>UDP>#${new_dune_game}#g")"
  new="$(printf '%s' "$new" | sed -E "s#<duneA2>[0-9]+:[0-9]+>${from_ip}>>UDP>#${new_dune_igw}#g")"
  new="$(printf '%s' "$new" | sed -E "s#<DuneRMQ>[0-9]+>${from_ip}>[0-9]+>TCP>#${new_dune_rmq}#g")"
done

missing=()
[[ "$new" == *"<duneA1>${game_udp_range}>${target_ip}>>UDP>"* ]] || missing+=("duneA1 ${game_udp_range}/udp")
[[ "$new" == *"<duneA2>${igw_udp_range}>${target_ip}>>UDP>"* ]] || missing+=("duneA2 ${igw_udp_range}/udp")
[[ "$new" == *"<DuneRMQ>${game_rmq_port}>${target_ip}>${game_rmq_port}>TCP>"* ]] || missing+=("DuneRMQ ${game_rmq_port}/tcp")

printf 'router backup written: %s\n' "$backup_file"
printf 'current vts_rulelist:\n%s\n\n' "$current"
printf 'proposed vts_rulelist:\n%s\n\n' "$new"

if [[ "$current" == "$new" ]]; then
  printf 'WARN no Dune forwarding changes detected. Current rules may already target %s or use unexpected names.\n' "$target_ip"
fi
if [[ "${#missing[@]}" -gt 0 ]]; then
  printf 'ERROR proposed router rulelist is missing expected target rules:\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

if [[ "${CONFIRM_ROUTER_CUTOVER:-}" != "yes" ]]; then
  cat <<EOF
Dry run only. To commit the AsusWRT forwarding change, run:
  CONFIRM_ROUTER_CUTOVER=yes make router-cutover ENV_FILE=${env_file} ROUTER=${router} TARGET=${target_ip}

This rewrites Dune port forwards to ${target_ip} and restarts the router firewall.
EOF
  exit 0
fi

printf 'committing AsusWRT vts_rulelist cutover to %s\n' "$target_ip"
ssh "$router" "nvram set vts_rulelist='$new'; nvram commit; service restart_firewall"
printf 'router cutover committed. New vts_rulelist:\n'
ssh "$router" 'nvram get vts_rulelist'
