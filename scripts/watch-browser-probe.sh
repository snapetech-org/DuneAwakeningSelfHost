#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
seconds="${2:-120}"
mode="${DUNE_PROBE_OUTPUT_MODE:-summary}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

range_for_tcpdump() {
  printf '%s' "$1" | tr ':' '-'
}

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi
if ! command -v tcpdump >/dev/null 2>&1; then
  printf 'tcpdump is required\n' >&2
  exit 1
fi
tcpdump_cmd=(tcpdump)
if [[ "$(id -u)" -ne 0 ]]; then
  tcpdump_cmd=(sudo tcpdump)
fi

game_rmq_public_port="$(read_env GAME_RMQ_PUBLIC_PORT)"
game_rmq_public_port="${game_rmq_public_port:-31982}"
game_rmq_public_http_port="$(read_env GAME_RMQ_PUBLIC_HTTP_PORT)"
game_rmq_public_http_port="${game_rmq_public_http_port:-15673}"
game_udp_range="$(read_env GAME_UDP_PORT_RANGE)"
game_udp_range="${game_udp_range:-7777:7871}"
igw_udp_range="$(read_env IGW_UDP_PORT_RANGE)"
igw_udp_range="${igw_udp_range:-7888:7982}"
game_tcpdump_range="$(range_for_tcpdump "$game_udp_range")"
igw_tcpdump_range="$(range_for_tcpdump "$igw_udp_range")"
tcp_only="${DUNE_PROBE_TCP_ONLY:-false}"
case "${tcp_only,,}" in
  1|true|yes|on)
    port_filter="(tcp port ${game_rmq_public_port}) or (tcp port ${game_rmq_public_http_port})"
    ;;
  *)
    port_filter="(tcp port ${game_rmq_public_port}) or (tcp port ${game_rmq_public_http_port}) or (udp portrange ${game_tcpdump_range}) or (udp portrange ${igw_tcpdump_range})"
    ;;
esac
filter="$port_filter"

client_ip="${DUNE_PROBE_CLIENT_IP:-}"
if [[ -n "$client_ip" ]]; then
  filter="(host ${client_ip}) and (${filter})"
fi

exclude_container_nets="${DUNE_PROBE_EXCLUDE_CONTAINER_NETS:-true}"
case "${exclude_container_nets,,}" in
  1|true|yes|on)
    filter="(${filter}) and not net 172.31.240.0/24 and not net 172.18.0.0/16 and not net 172.19.0.0/16"
    ;;
esac

printf 'watching browser probe traffic for %s seconds\n' "$seconds"
if [[ -n "$client_ip" ]]; then
  printf 'client ip: %s\n' "$client_ip"
fi
printf 'exclude container nets: %s\n' "$exclude_container_nets"
printf 'tcp only: %s\n' "$tcp_only"
printf 'output mode: %s\n' "$mode"
printf 'filter: %s\n\n' "$filter"

if command -v iptables >/dev/null 2>&1; then
  printf '== docker nat counters before ==\n'
  iptables -t nat -L DOCKER -n -v 2>/dev/null | rg "dpt:(${game_rmq_public_port}|${game_rmq_public_http_port}|777[7-9]|778[0-9]|779[0-9]|780[0-9]|7810|788[8-9]|789[0-9]|790[0-9]|791[0-8])" || true
  printf '\n'
fi

case "${mode,,}" in
  raw)
    timeout "$seconds" "${tcpdump_cmd[@]}" -ni any "$filter" || status=$?
    ;;
  summary)
    tmp_capture="$(mktemp)"
    trap 'rm -f "$tmp_capture"' EXIT
    timeout "$seconds" "${tcpdump_cmd[@]}" -l -tt -ni any "$filter" >"$tmp_capture" 2>&1 || status=$?
    if rg -i 'permission denied|you do not have permission|no such device|syntax error' "$tmp_capture" >/dev/null 2>&1; then
      cat "$tmp_capture" >&2
    fi
    printf '== packet summary ==\n'
    awk '
      function endpoint_port(endpoint, parts) {
        gsub(/:$/, "", endpoint)
        split(endpoint, parts, ".")
        return parts[length(parts)]
      }
      {
        ip_idx = 0
        for (i = 1; i <= NF; i++) {
          if ($i == "IP") {
            ip_idx = i
            break
          }
        }
        if (!ip_idx || NF < ip_idx + 3) {
          next
        }
        src=$(ip_idx + 1)
        dst=$(ip_idx + 3)
        sub(/:$/, "", dst)
        sp=endpoint_port(src)
        dp=endpoint_port(dst)
        key=sp " -> " dp
        count[key]++
        if (!first[key]) first[key]=$1
        last[key]=$1
      }
      END {
        for (key in count) {
          printf "%7d  %-18s first=%s last=%s\n", count[key], key, first[key], last[key]
        }
      }
    ' "$tmp_capture" | sort -nr || true
    printf '\n== by local service port ==\n'
    awk -v rmq="$game_rmq_public_port" -v http="$game_rmq_public_http_port" '
      function endpoint_port(endpoint, parts) {
        gsub(/:$/, "", endpoint)
        split(endpoint, parts, ".")
        return parts[length(parts)]
      }
      {
        ip_idx = 0
        for (i = 1; i <= NF; i++) {
          if ($i == "IP") {
            ip_idx = i
            break
          }
        }
        if (!ip_idx || NF < ip_idx + 3) {
          next
        }
        src=$(ip_idx + 1)
        dst=$(ip_idx + 3)
        sp=endpoint_port(src)
        dp=endpoint_port(dst)
        if (sp == rmq || dp == rmq) rmq_count++
        if (sp == http || dp == http) http_count++
      }
      END {
        printf "tcp_%s_packets=%d\n", rmq, rmq_count + 0
        printf "tcp_%s_packets=%d\n", http, http_count + 0
      }
    ' "$tmp_capture" || true
    printf '\n== tcp/http probe lines ==\n'
    rg "(\.${game_rmq_public_http_port}|\.${game_rmq_public_port}|Flags)" "$tmp_capture" | tail -n 80 || true
    ;;
  *)
    printf 'unknown DUNE_PROBE_OUTPUT_MODE: %s\n' "$mode" >&2
    exit 2
    ;;
esac
status="${status:-0}"
if [[ "$status" != "0" && "$status" != "124" ]]; then
  exit "$status"
fi

if command -v iptables >/dev/null 2>&1; then
  printf '\n== docker nat counters after ==\n'
  iptables -t nat -L DOCKER -n -v 2>/dev/null | rg "dpt:(${game_rmq_public_port}|${game_rmq_public_http_port}|777[7-9]|778[0-9]|779[0-9]|780[0-9]|7810|788[8-9]|789[0-9]|790[0-9]|791[0-8])" || true
fi
