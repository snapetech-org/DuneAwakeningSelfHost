#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
COMMAND="status"
EXECUTE=false
CONFIRM=""
PERSIST=false
NIC_ENABLED=false
PRODUCTION_HOST="${DUNE_PRODUCTION_HOST:-kspls0}"
ALLOW_NON_PRODUCTION_HOST=false
ETC_ROOT="${DUNE_HOST_TUNING_ETC_ROOT:-/etc}"
SYSFS_ROOT="${DUNE_HOST_TUNING_SYSFS_ROOT:-/sys}"
PROC_ROOT="${DUNE_HOST_TUNING_PROC_ROOT:-/proc}"
BACKUP_ROOT="${DUNE_HOST_TUNING_BACKUP_ROOT:-$ROOT_DIR/backups/host-tuning}"
SYSCTL_CONF="$ETC_ROOT/sysctl.d/99-dune-selfhost.conf"
SYSTEMD_UNIT="$ETC_ROOT/systemd/system/dune-host-tuning.service"

usage() {
  cat <<'EOF'
Usage:
  scripts/host-tuning.sh [--env-file PATH] status
  scripts/host-tuning.sh [--env-file PATH] plan [--nic]
  sudo scripts/host-tuning.sh [--env-file PATH] apply --execute [--persist] [--nic] \
    --confirm 'APPLY DUNE HOST TUNING'
  scripts/host-tuning.sh --render-sysctl

Apply is hostname-, root-, and confirmation-gated. It preserves current network
maxima when they exceed DASH baselines, backs up replaced files and runtime
values, and never edits Docker daemon.json. --nic expands supported RX/TX rings
and pins NIC IRQs to the generated CPU-affinity background pool when irqbalance
is inactive. --persist installs a boot-time oneshot and enables feature flags.
EOF
}

RENDER_ONLY=false
while (($#)); do
  case "$1" in
    status|plan|apply|runtime) COMMAND="$1" ;;
    --env-file) shift; ENV_FILE="${1:?--env-file requires a path}" ;;
    --execute) EXECUTE=true ;;
    --confirm) shift; CONFIRM="${1:?--confirm requires a phrase}" ;;
    --persist) PERSIST=true ;;
    --nic) NIC_ENABLED=true ;;
    --production-host) shift; PRODUCTION_HOST="${1:?--production-host requires a name}" ;;
    --allow-non-production-host) ALLOW_NON_PRODUCTION_HOST=true ;;
    --render-sysctl) RENDER_ONLY=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$ENV_FILE" != /* ]]; then ENV_FILE="$ROOT_DIR/$ENV_FILE"; fi

read_env() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); value=$0} END {gsub(/^["'\'' ]+|["'\'' ]+$/, "", value); print value}' "$ENV_FILE" 2>/dev/null
}

env_bool() {
  case "${1,,}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac
}

if env_bool "${DUNE_HOST_TUNING_NIC_ENABLED:-$(read_env DUNE_HOST_TUNING_NIC_ENABLED)}"; then
  NIC_ENABLED=true
fi

sysctl_value() {
  sysctl -n "$1" 2>/dev/null || printf '%s\n' "$2"
}

max_value() {
  local current="$1" baseline="$2"
  if [[ "$current" =~ ^[0-9]+$ ]] && ((current > baseline)); then echo "$current"; else echo "$baseline"; fi
}

render_sysctl() {
  local rmem_max wmem_max somaxconn backlog file_max
  rmem_max="$(max_value "$(sysctl_value net.core.rmem_max 0)" 33554432)"
  wmem_max="$(max_value "$(sysctl_value net.core.wmem_max 0)" 33554432)"
  somaxconn="$(max_value "$(sysctl_value net.core.somaxconn 0)" 4096)"
  backlog="$(max_value "$(sysctl_value net.core.netdev_max_backlog 0)" 5000)"
  cat <<EOF
# Managed by DASH scripts/host-tuning.sh.
# Existing network maxima larger than DASH baselines are preserved at generation.
vm.swappiness = ${DUNE_HOST_TUNING_SWAPPINESS:-10}
vm.overcommit_memory = ${DUNE_HOST_TUNING_OVERCOMMIT_MEMORY:-1}
vm.dirty_ratio = ${DUNE_HOST_TUNING_DIRTY_RATIO:-10}
vm.dirty_background_ratio = ${DUNE_HOST_TUNING_DIRTY_BACKGROUND_RATIO:-5}
net.core.rmem_max = $rmem_max
net.core.wmem_max = $wmem_max
net.core.rmem_default = ${DUNE_HOST_TUNING_RMEM_DEFAULT:-8388608}
net.core.wmem_default = ${DUNE_HOST_TUNING_WMEM_DEFAULT:-4194304}
net.core.somaxconn = $somaxconn
net.core.netdev_max_backlog = $backlog
net.core.netdev_budget = ${DUNE_HOST_TUNING_NETDEV_BUDGET:-600}
net.core.netdev_budget_usecs = ${DUNE_HOST_TUNING_NETDEV_BUDGET_USECS:-4000}
EOF
}

if [[ "$RENDER_ONLY" == true ]]; then render_sysctl; exit 0; fi

detect_nic() {
  local configured="${DUNE_HOST_TUNING_NIC:-$(read_env DUNE_HOST_TUNING_NIC)}"
  [[ -z "$configured" ]] || { echo "$configured"; return; }
  ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}'
}

background_cpuset() {
  local configured="${DUNE_HOST_TUNING_NIC_IRQ_CPUSET:-$(read_env DUNE_HOST_TUNING_NIC_IRQ_CPUSET)}"
  [[ -z "$configured" ]] || { echo "$configured"; return; }
  sed -n 's/^# Background CPUs: //p' "$ROOT_DIR/compose.cpu-affinity.yaml" 2>/dev/null | head -1
}

thp_path="$SYSFS_ROOT/kernel/mm/transparent_hugepage/enabled"
thp_defrag_path="$SYSFS_ROOT/kernel/mm/transparent_hugepage/defrag"
nic="$(detect_nic || true)"
irq_cpuset="$(background_cpuset || true)"

show_status() {
  local irqbalance_state
  irqbalance_state="$(systemctl is-active irqbalance 2>/dev/null || true)"
  irqbalance_state="${irqbalance_state:-unavailable}"
  echo "Host tuning status (hostname=$(hostname))"
  sysctl vm.swappiness vm.overcommit_memory vm.dirty_ratio vm.dirty_background_ratio \
    net.core.rmem_max net.core.wmem_max net.core.rmem_default net.core.wmem_default \
    net.core.somaxconn net.core.netdev_max_backlog net.core.netdev_budget \
    net.core.netdev_budget_usecs 2>/dev/null || true
  printf 'transparent_hugepage=%s\n' "$(cat "$thp_path" 2>/dev/null || echo unavailable)"
  printf 'nic=%s nic_tuning=%s irq_cpuset=%s irqbalance=%s\n' \
    "${nic:-unavailable}" "$NIC_ENABLED" "${irq_cpuset:-unavailable}" \
    "$irqbalance_state"
  if [[ -n "$nic" ]] && command -v ethtool >/dev/null 2>&1; then
    ethtool -g "$nic" 2>/dev/null || true
  fi
  if [[ -n "$nic" && -r "$PROC_ROOT/interrupts" ]]; then
    while read -r irq; do
      [[ -n "$irq" ]] || continue
      printf 'irq_%s_affinity=%s\n' "$irq" "$(cat "$PROC_ROOT/irq/$irq/smp_affinity_list" 2>/dev/null || echo unavailable)"
    done < <(awk -F: -v nic="$nic" '$0 ~ nic {gsub(/ /,"",$1); print $1}' "$PROC_ROOT/interrupts")
  fi
}

apply_nic_runtime() {
  [[ "$NIC_ENABLED" == true && -n "$nic" ]] || return 0
  if command -v ethtool >/dev/null 2>&1; then
    read -r max_rx max_tx < <(ethtool -g "$nic" 2>/dev/null | awk '
      /^Pre-set maximums:/ {section=1; next}
      /^Current hardware settings:/ {section=0}
      section && /^RX:[[:space:]]+[0-9]+/ && !rx {rx=$2}
      section && /^TX:[[:space:]]+[0-9]+/ && !tx {tx=$2}
      END {print rx+0, tx+0}')
    if ((max_rx > 0 && max_tx > 0)); then ethtool -G "$nic" rx "$max_rx" tx "$max_tx"; fi
  fi
  if [[ -z "$irq_cpuset" ]]; then
    echo "NIC IRQ CPU set unavailable; skipping IRQ pinning" >&2
    return 0
  fi
  if systemctl is-active --quiet irqbalance 2>/dev/null; then
    echo "irqbalance is active; skipping persistent NIC IRQ pinning" >&2
    return 0
  fi
  [[ -r "$PROC_ROOT/interrupts" ]] || return 0
  while read -r irq; do
    [[ -w "$PROC_ROOT/irq/$irq/smp_affinity_list" ]] || continue
    printf '%s\n' "$irq_cpuset" >"$PROC_ROOT/irq/$irq/smp_affinity_list"
  done < <(awk -F: -v nic="$nic" '$0 ~ nic {gsub(/ /,"",$1); print $1}' "$PROC_ROOT/interrupts")
}

apply_runtime() {
  [[ -r "$SYSCTL_CONF" ]] && sysctl -p "$SYSCTL_CONF"
  thp_mode="${DUNE_HOST_TUNING_THP_MODE:-$(read_env DUNE_HOST_TUNING_THP_MODE)}"
  thp_mode="${thp_mode:-never}"
  [[ ! -w "$thp_path" ]] || printf '%s\n' "$thp_mode" >"$thp_path"
  [[ ! -w "$thp_defrag_path" ]] || printf '%s\n' "$thp_mode" >"$thp_defrag_path"
  apply_nic_runtime
}

if [[ "$COMMAND" == "status" ]]; then show_status; exit 0; fi
if [[ "$COMMAND" == "runtime" ]]; then apply_runtime; exit 0; fi

echo "Planned sysctl configuration:"
render_sysctl
planned_thp="${DUNE_HOST_TUNING_THP_MODE:-$(read_env DUNE_HOST_TUNING_THP_MODE)}"
printf 'transparent_hugepage_target=%s\n' "${planned_thp:-never}"
printf 'nic=%s nic_tuning=%s irq_cpuset=%s\n' "${nic:-unavailable}" "$NIC_ENABLED" "${irq_cpuset:-unavailable}"
if [[ "$COMMAND" == "plan" || "$EXECUTE" != true ]]; then
  echo "Dry-run only. No host setting was changed."
  exit 0
fi

[[ "$CONFIRM" == "APPLY DUNE HOST TUNING" ]] || { echo "Execution requires --confirm 'APPLY DUNE HOST TUNING'" >&2; exit 2; }
current_host="$(hostname)"
if [[ "$ALLOW_NON_PRODUCTION_HOST" != true && "$current_host" != "$PRODUCTION_HOST" ]]; then
  echo "refusing host tuning on '$current_host'; expected '$PRODUCTION_HOST'" >&2
  exit 1
fi
if [[ "${DUNE_HOST_TUNING_TEST_MODE:-false}" != true && "$(id -u)" != 0 ]]; then
  echo "host tuning apply must run as root" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$BACKUP_ROOT/$timestamp"
mkdir -p "$backup_dir" "$(dirname "$SYSCTL_CONF")" "$(dirname "$SYSTEMD_UNIT")"
[[ ! -e "$SYSCTL_CONF" ]] || cp -a "$SYSCTL_CONF" "$backup_dir/sysctl-conf.before"
[[ ! -e "$SYSTEMD_UNIT" ]] || cp -a "$SYSTEMD_UNIT" "$backup_dir/systemd-unit.before"
[[ ! -r "$ENV_FILE" ]] || install -m 600 "$ENV_FILE" "$backup_dir/env.before"
show_status >"$backup_dir/status.before.txt"
render_sysctl >"$SYSCTL_CONF"

if [[ "$PERSIST" == true ]]; then
  python3 "$ROOT_DIR/scripts/update-env-file.py" "$ENV_FILE" --allow-protected-link --quiet \
    --set DUNE_HOST_TUNING_ENABLED true \
    --set DUNE_HOST_TUNING_NIC_ENABLED "$NIC_ENABLED" \
    --set DUNE_HOST_TUNING_NIC "$nic" \
    --set DUNE_HOST_TUNING_NIC_IRQ_CPUSET "$irq_cpuset"
  cat >"$SYSTEMD_UNIT" <<EOF
[Unit]
Description=DASH host sysctl, THP, NIC ring and IRQ tuning
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=$ROOT_DIR/scripts/host-tuning.sh --env-file $ENV_FILE runtime
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
fi

apply_runtime
if [[ "$PERSIST" == true ]]; then
  systemctl daemon-reload
  systemctl enable --now dune-host-tuning.service >/dev/null
fi
show_status >"$backup_dir/status.after.txt"
printf 'created_utc=%s\nhostname=%s\nnic=%s\nirq_cpuset=%s\npersisted=%s\n' \
  "$timestamp" "$current_host" "$nic" "$irq_cpuset" "$PERSIST" >"$backup_dir/manifest.txt"
echo "Host tuning applied; persisted=$PERSIST; recovery record: $backup_dir"
