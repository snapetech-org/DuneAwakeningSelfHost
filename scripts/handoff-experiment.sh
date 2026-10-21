#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/handoff-experiment.sh [ENV_FILE] standby|primary [--apply]

Capture evidence for a quick-hiccup handoff experiment. Dry-run by default.
The apply mode performs the same live cutover orchestration as failover, but
writes before/after evidence under captures/handoff/<timestamp>.
EOF
}

env_file="${1:-.env}"
role="${2:-standby}"
apply="${3:-}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi
if [[ "$role" != "standby" && "$role" != "primary" ]]; then
  usage
  exit 2
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
capture_dir="${DUNE_HANDOFF_CAPTURE_DIR:-captures/handoff/$stamp}"
public_ip="${DUNE_FAILOVER_PUBLIC_IP:-${DUNE_PUBLIC_IP:-${EXTERNAL_ADDRESS:-$(read_env EXTERNAL_ADDRESS)}}}"
game_rmq_port="${GAME_RMQ_PUBLIC_PORT:-$(read_env GAME_RMQ_PUBLIC_PORT)}"; game_rmq_port="${game_rmq_port:-31982}"
game_udp_range="${GAME_UDP_PORT_RANGE:-$(read_env GAME_UDP_PORT_RANGE)}"; game_udp_range="${game_udp_range:-7777:7871}"
igw_udp_range="${IGW_UDP_PORT_RANGE:-$(read_env IGW_UDP_PORT_RANGE)}"; igw_udp_range="${igw_udp_range:-7888:7982}"
capture_seconds="${DUNE_HANDOFF_CAPTURE_SECONDS:-90}"
compose_files="${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"

mkdir -p "$capture_dir"

run_capture() {
  local label="$1"
  {
    printf 'timestamp=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'role=%s apply=%s public_ip=%s rmq_port=%s game_udp_range=%s igw_udp_range=%s\n' "$role" "${apply:-dry-run}" "$public_ip" "$game_rmq_port" "$game_udp_range" "$igw_udp_range"
    printf '\n== cutover network status ==\n'
    make cutover-network-status "ENV_FILE=$env_file" || true
    printf '\n== stack status ==\n'
    COMPOSE_FILES="$compose_files" ./scripts/status.sh "$env_file" || true
    printf '\n== rabbitmq health ==\n'
    COMPOSE_FILES="$compose_files" ./scripts/rmq-health.sh "$env_file" || true
    printf '\n== network sockets ==\n'
    ./scripts/watch-network.sh "$env_file" || true
  } >"$capture_dir/${label}.txt" 2>&1
}

preflight_handoff() {
  local rc=0
  printf '== handoff preflight ==\n' >"$capture_dir/preflight.txt"
  {
    printf 'timestamp=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    ./scripts/handoff-ready.sh "$env_file" "$role" || rc=1
  } >>"$capture_dir/preflight.txt" 2>&1
  return "$rc"
}

printf 'handoff_capture_dir=%s\n' "$capture_dir"
preflight_rc=0
preflight_handoff || preflight_rc=1
run_capture before

cat >"$capture_dir/operator-notes.md" <<EOF
# Handoff Experiment ${stamp}

Confidence target: determine whether a connected client survives, auto-reconnects,
or requires manual reconnect during a host handoff.

Record:
- client state before cutover;
- exact time APPLY starts;
- whether the client rubber-bands, loads, disconnects to menu, or silently resumes;
- time until chat/movement/map travel works again;
- whether public status and website recover.
EOF

if [[ "$apply" != "--apply" ]]; then
  cat <<EOF
Dry run only. Evidence captured in ${capture_dir}.
To run a live quick-hiccup experiment:
  CONFIRM_HANDOFF_EXPERIMENT=yes make handoff-experiment ENV_FILE=${env_file} ROLE=${role} APPLY=--apply
EOF
  exit 0
fi

if [[ "$preflight_rc" -ne 0 && "${DUNE_HANDOFF_ALLOW_PREFLIGHT_WARNINGS:-}" != "yes" ]]; then
  printf 'refusing live handoff experiment because preflight failed; see %s/preflight.txt\n' "$capture_dir" >&2
  printf 'set DUNE_HANDOFF_ALLOW_PREFLIGHT_WARNINGS=yes only for an intentional emergency experiment\n' >&2
  exit 1
fi

if [[ "${CONFIRM_HANDOFF_EXPERIMENT:-}" != "yes" ]]; then
  printf 'refusing live handoff experiment without CONFIRM_HANDOFF_EXPERIMENT=yes\n' >&2
  exit 1
fi

printf 'starting live handoff experiment; capture window is %ss\n' "$capture_seconds"
if command -v tcpdump >/dev/null 2>&1 && [[ -n "$public_ip" ]]; then
  sudo -n timeout "$capture_seconds" tcpdump -ni any "host $public_ip and (tcp port $game_rmq_port or udp)" -w "$capture_dir/public-traffic.pcap" >/dev/null 2>&1 &
  tcpdump_pid=$!
else
  tcpdump_pid=""
fi

./scripts/failover-orchestrate.sh "$env_file" "$role" --apply | tee "$capture_dir/failover-apply.log"
run_capture after

if [[ -n "${tcpdump_pid:-}" ]]; then
  wait "$tcpdump_pid" || true
fi

./scripts/summarize-handoff-experiment.sh "$capture_dir" >/dev/null || true
printf 'handoff experiment complete. Evidence: %s\n' "$capture_dir"
