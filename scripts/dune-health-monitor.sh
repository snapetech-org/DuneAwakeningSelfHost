#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/.env}"
if [[ "$env_file" != /* ]]; then env_file="$repo_root/$env_file"; fi
state_dir="${STATE_DIRECTORY:-/var/lib/dune-health-monitor}"
state_file="$state_dir/findings.txt"
mail_helper="${DUNE_MAIL_HELPER:-/usr/local/lib/snape-mail-submit.sh}"

[[ -r "$env_file" ]] || { printf 'Dune environment file is unreadable: %s\n' "$env_file" >&2; exit 1; }
[[ -r "$mail_helper" ]] || { printf 'shared mail helper is unreadable: %s\n' "$mail_helper" >&2; exit 1; }
# shellcheck source=/dev/null
source "$mail_helper"

declare -a findings=()
add_finding() {
  findings+=("$1")
}

timer_next() {
  local timer="$1"
  systemctl list-timers --all --no-legend --no-pager "$timer" 2>/dev/null \
    | awk 'NR == 1 { print $1 }'
}

check_timer() {
  local timer="$1" service="$2" next service_state
  if ! systemctl is-active --quiet "$timer"; then
    add_finding "$timer is not active"
    return
  fi
  # OnUnitInactiveSec schedules after the associated oneshot exits; an empty
  # next-activation field is expected while that service is still running.
  service_state="$(systemctl show "$service" --property=ActiveState --value 2>/dev/null || true)"
  case "$service_state" in
    active|activating|reloading) return ;;
  esac
  next="$(timer_next "$timer")"
  if [[ -z "$next" || "$next" == "-" ]]; then
    add_finding "$timer has no next activation"
  fi
}

check_timer dune-health-monitor.timer dune-health-monitor.service
check_timer dune-hotfix-auto-update.timer dune-hotfix-auto-update.service
check_timer dune-public-ip-monitor.timer dune-public-ip-monitor.service
check_timer dune-target-safety-audit.timer dune-target-safety-audit.service

if ! systemctl is-active --quiet docker.service; then
  add_finding "docker.service is not active"
fi

for service in \
  dune-hotfix-auto-update.service \
  dune-public-ip-monitor.service \
  dune-target-safety-audit.service \
  dune-map-watchdog.service; do
  load_state="$(systemctl show "$service" --property=LoadState --value 2>/dev/null || true)"
  [[ "$load_state" == "loaded" ]] || continue
  if systemctl is-failed --quiet "$service"; then
    result="$(systemctl show "$service" --property=Result --value 2>/dev/null || printf unknown)"
    status="$(systemctl show "$service" --property=ExecMainStatus --value 2>/dev/null || printf unknown)"
    add_finding "$service is failed (result=$result, exit=$status)"
  fi
done

updater_active=false
ip_monitor_active=false
systemctl is-active --quiet dune-hotfix-auto-update.service && updater_active=true
systemctl is-active --quiet dune-public-ip-monitor.service && ip_monitor_active=true

if [[ "$updater_active" != true ]]; then
  set +e
  steam_check="$("$repo_root/scripts/check-steam-update.sh" "$env_file" 2>&1)"
  steam_rc=$?
  set -e
  if (( steam_rc != 0 )); then
    summary="$(sed -n '/^status:/p; /^reload required:/p' <<<"$steam_check" | paste -sd '; ' -)"
    add_finding "Steam server package is stale or unverifiable${summary:+: $summary}"
  fi
fi

if [[ "$updater_active" != true && "$ip_monitor_active" != true ]]; then
  set +e
  status_db="${DUNE_PUBLIC_STATUS_DB_NAME:-}"
  if [[ -z "$status_db" ]]; then
    status_db="$(awk -F= '$1 == "DUNE_PUBLIC_STATUS_DB_NAME" { sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); gsub(/^\"|\"$/, ""); print; exit }' "$env_file" 2>/dev/null)"
  fi
  if [[ -z "$status_db" ]]; then
    status_db="$(awk -F= '$1 == "DUNE_GAME_DB_NAME" { sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); gsub(/^\"|\"$/, ""); print; exit }' "$env_file" 2>/dev/null)"
  fi
  status_db="${status_db:-${DUNE_DATABASE:-dune_sb_1_5_15_0}}"
  farm_status="$(DUNE_PUBLIC_STATUS_DB_NAME="$status_db" "$repo_root/scripts/status.sh" --public-summary 2>&1)"
  farm_rc=$?
  set -e
  if (( farm_rc != 0 )); then
    add_finding "Dune farm health command failed (exit=$farm_rc)"
  fi
  if grep -Fq 'WARN: required always-on partition readiness is incomplete.' <<<"$farm_status"; then
    add_finding "Dune required always-on game partitions are not ready and connected"
  elif ! grep -Fq 'OK: required always-on partitions are ready and connected.' <<<"$farm_status"; then
    add_finding "Dune farm health did not confirm required partitions ready"
  fi
  if grep -Fq 'WARN: FLS publication health is degraded.' <<<"$farm_status"; then
    add_finding "Dune server browser publication is degraded"
  fi
fi

mkdir -p "$state_dir"
chmod 0750 "$state_dir"
current=""
if ((${#findings[@]})); then
  current="$(printf '%s\n' "${findings[@]}" | LC_ALL=C sort -u)"
fi
previous="$(cat "$state_file" 2>/dev/null || true)"

if [[ "$current" == "$previous" ]]; then
  exit 0
fi

if [[ -n "$current" ]]; then
  if [[ -n "$previous" ]]; then
    subject='[Dune] Health alert updated'
    heading='Dune server health findings changed:'
  else
    subject='[Dune] Server health alert'
    heading='Dune server health needs attention:'
  fi
  body="$heading

$current

The Dune health monitor checks the installed Steam build, required game partitions, server-browser publication, and the next scheduled updater/monitor runs. It will send another email if the findings change and a recovery email when they clear."
  snape_mail_send "$subject" "$body"
  tmp="$(mktemp "$state_dir/.findings.XXXXXX")"
  printf '%s\n' "$current" > "$tmp"
  chmod 0640 "$tmp"
  mv -f "$tmp" "$state_file"
elif [[ -n "$previous" ]]; then
  body="Dune server health has recovered. The installed Steam build, required game partitions, server-browser publication, and monitored timer schedules are healthy."
  snape_mail_send '[Dune] Server health recovered' "$body"
  rm -f "$state_file"
fi
