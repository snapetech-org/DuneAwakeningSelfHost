#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="/etc/systemd/system"
service_template="$repo_root/config/systemd/dune-health-monitor.service"
timer_template="$repo_root/config/systemd/dune-health-monitor.timer"
alert_dropin="$repo_root/config/systemd/dune-health-alert.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  exec sudo -- "$0" "$@"
fi

for file in "$service_template" "$timer_template" "$alert_dropin" "$repo_root/scripts/dune-health-monitor.sh"; do
  [[ -f "$file" ]] || { printf 'required health monitor file is missing: %s\n' "$file" >&2; exit 1; }
done

[[ -r /etc/snape/identity-alerts.env ]] || { printf 'mail configuration is missing: /etc/snape/identity-alerts.env\n' >&2; exit 1; }
[[ -x /usr/local/lib/snape-mail-submit.sh ]] || { printf 'shared mail helper is missing: /usr/local/lib/snape-mail-submit.sh\n' >&2; exit 1; }
[[ -x /usr/local/lib/snape-mail-ledger.py ]] || { printf 'shared mail ledger is missing: /usr/local/lib/snape-mail-ledger.py\n' >&2; exit 1; }

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
repo_path="$(cd "$repo_root" && pwd -P)"
sed \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=$repo_path#" \
  -e "s#^ExecStart=.*#ExecStart=$repo_path/scripts/dune-health-monitor.sh $repo_path/.env#" \
  "$service_template" > "$tmp_dir/dune-health-monitor.service"

install -d -m 0755 "$unit_dir"
install -m 0644 "$tmp_dir/dune-health-monitor.service" "$unit_dir/dune-health-monitor.service"
install -m 0644 "$timer_template" "$unit_dir/dune-health-monitor.timer"
for unit in \
  dune-hotfix-auto-update.service \
  dune-public-ip-monitor.service \
  dune-target-safety-audit.service \
  dune-map-watchdog.service; do
  dropin_dir="$unit_dir/$unit.d"
  install -d -m 0755 "$dropin_dir"
  install -m 0644 "$alert_dropin" "$dropin_dir/90-dune-health-alert.conf"
done

if [[ "$(id -u)" -eq 0 ]]; then
  systemctl daemon-reload
  systemctl enable --now dune-health-monitor.timer
  systemctl start --no-block dune-health-monitor.service
else
  sudo systemctl daemon-reload
  sudo systemctl enable --now dune-health-monitor.timer
  sudo systemctl start --no-block dune-health-monitor.service
fi

systemctl list-timers --all --no-pager dune-health-monitor.timer dune-hotfix-auto-update.timer dune-public-ip-monitor.timer
printf 'installed Dune health monitoring and failure-triggered checks\n'
