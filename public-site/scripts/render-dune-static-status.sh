#!/usr/bin/env bash
set -euo pipefail

# Generate a public-safe static status block for the Dune page.
# This exposes only coarse player-facing state. It does not publish ports,
# hosts, container names, routes, logs, tokens, or map topology.

if [[ -z "${DUNE_ROOT:-}" ]]; then
  script_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  if [[ -x "$script_repo_root/scripts/status.sh" ]]; then
    DUNE_ROOT="$script_repo_root"
  elif [[ -x /home/keith/Documents/code/DuneAwakeningSelfHost/scripts/status.sh ]]; then
    DUNE_ROOT="/home/keith/Documents/code/DuneAwakeningSelfHost"
  else
    DUNE_ROOT="/opt/DuneAwakeningSelfHost"
  fi
fi
export DUNE_ROOT
INDEX_FILE="${INDEX_FILE:-/srv/dash-public-site/index.html}"
STATUS_FILE="${STATUS_FILE:-$(dirname "$INDEX_FILE")/status.html}"
STATIC_DIR="${STATIC_DIR:-$(dirname "$INDEX_FILE")}"
SOURCE_INDEX_FILE="${SOURCE_INDEX_FILE:-$DUNE_ROOT/public-site/static/index.html}"
STATUS_TIMEOUT_SECONDS="${STATUS_TIMEOUT_SECONDS:-60}"
SYNC_HOST="${SYNC_HOST:-}"
SYNC_STATIC_ROOT="${SYNC_STATIC_ROOT:-/srv/hostapps/ingress/static}"
CONFIGURE_SCRIPT="${CONFIGURE_SCRIPT:-$(dirname "$0")/configure-dune-public-site.sh}"
DRIFT_CHECK_SCRIPT="${DRIFT_CHECK_SCRIPT:-$(dirname "$0")/check-dune-public-site-drift.sh}"

tmp_status="$(mktemp)"
tmp_index="$(mktemp)"
tmp_source_index="$(mktemp)"
tmp_status_html="$(mktemp)"
trap 'rm -f "$tmp_status" "$tmp_index" "$tmp_source_index" "$tmp_status_html"' EXIT

install_output_file() {
  local src="$1"
  local dst="$2"
  if [[ -w "$(dirname "$dst")" ]]; then
    install -m 0644 "$src" "$dst"
  else
    sudo install -m 0644 "$src" "$dst"
  fi
}

ensure_static_dir() {
  if [[ -d "$STATIC_DIR" ]]; then
    return 0
  fi
  if [[ -w "$(dirname "$STATIC_DIR")" ]]; then
    install -d -m 0755 "$STATIC_DIR"
  else
    sudo install -d -m 0755 "$STATIC_DIR"
  fi
}

seed_static_asset_if_empty() {
  local name="$1"
  local src="$DUNE_ROOT/public-site/static/$name"
  local dst="$STATIC_DIR/$name"
  [[ -s "$src" && ! -s "$dst" ]] || return 0
  install_output_file "$src" "$dst"
}

seed_static_assets() {
  ensure_static_dir
  seed_static_asset_if_empty style.css
  seed_static_asset_if_empty app.js
  seed_static_asset_if_empty hagga-pois.json
  seed_static_asset_if_empty hagga-basin.webp
}

require_browser_assets() {
  local name
  for name in style.css app.js; do
    if [[ ! -s "$STATIC_DIR/$name" ]]; then
      echo "missing or empty required public site browser asset: $STATIC_DIR/$name" >&2
      exit 1
    fi
  done
}

asset_version() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print substr($1, 1, 12)}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print substr($1, 1, 12)}'
  else
    cksum "$file" | awk '{print $1}'
  fi
}

stamp_static_asset_versions() {
  local target_index="${1:-$INDEX_FILE}"
  local app_file="$STATIC_DIR/app.js"
  local style_file="$STATIC_DIR/style.css"
  local app_version style_version tmp_versioned
  [[ -f "$target_index" && -f "$app_file" && -f "$style_file" ]] || return 0
  app_version="$(asset_version "$app_file")"
  style_version="$(asset_version "$style_file")"
  tmp_versioned="$(mktemp)"
  sed -E \
    -e "s#href=\"style\.css(\\?v=[^\"]*)?\"#href=\"style.css?v=${style_version}\"#g" \
    -e "s#src=\"app\.js(\\?v=[^\"]*)?\"#src=\"app.js?v=${app_version}\"#g" \
    "$target_index" > "$tmp_versioned"
  if [[ -w "$target_index" ]]; then
    install -m 0644 "$tmp_versioned" "$target_index"
  else
    sudo install -m 0644 "$tmp_versioned" "$target_index"
  fi
  rm -f "$tmp_versioned"
}

seed_static_assets
require_browser_assets

if [[ -f "$SOURCE_INDEX_FILE" ]]; then
  install -m 0644 "$SOURCE_INDEX_FILE" "$tmp_source_index"
elif [[ -f "$INDEX_FILE" ]]; then
  install -m 0644 "$INDEX_FILE" "$tmp_source_index"
else
  echo "missing public site index template: $SOURCE_INDEX_FILE" >&2
  exit 1
fi

if [[ -x "$CONFIGURE_SCRIPT" ]]; then
  STATIC_DIR="$STATIC_DIR" INDEX_FILE="$tmp_source_index" "$CONFIGURE_SCRIPT"
fi

stamp_static_asset_versions "$tmp_source_index"

server_class="status-warn"
server_text="Unknown"
world_class="status-warn"
world_text="Unknown"
access_class="status-warn"
access_text="Unknown"
runtime_class="status-unknown"
runtime_text="Unknown"
runtime_label="Since maintenance"
runtime_detail_html="Runtime data unavailable."
status_detail_text="Live status is being checked."
runtime_farm_ready=false

if [[ -d "$DUNE_ROOT" ]] && (cd "$DUNE_ROOT" && timeout "$STATUS_TIMEOUT_SECONDS" ./scripts/status.sh --public-summary) >"$tmp_status" 2>&1; then
  health_line="$(sed -nE 's/^current_ready_alive=([0-9]+) current_alive_active=([0-9]+) active_servers=([0-9]+) partitions=([0-9]+) game_sg_connections=([0-9]+) admin_sg_connections=([0-9]+).*/\1 \2 \3 \4 \5 \6/p' "$tmp_status" | tail -1)"
  read -r current_ready_alive current_alive_active active_servers partitions game_sg_connections admin_sg_connections <<< "${health_line:-0 0 0 0 0 0}"
  core_health_line="$(sed -nE 's/^core_ready_alive=([0-9]+) core_alive_active=([0-9]+) core_active_servers=([0-9]+) core_partitions=([0-9]+).*/\1 \2 \3 \4/p' "$tmp_status" | tail -1)"
  read -r core_ready_alive core_alive_active core_active_servers core_partitions <<< "${core_health_line:-0 0 0 0}"
  fls_healthy=false
  if grep -q '^FLS publication health: healthy' "$tmp_status"; then
    fls_healthy=true
  fi
  if [[ "$core_ready_alive" =~ ^[0-9]+$ && "$core_alive_active" =~ ^[0-9]+$ \
      && "$core_active_servers" =~ ^[0-9]+$ && "$core_partitions" =~ ^[0-9]+$ \
      && "$game_sg_connections" =~ ^[0-9]+$ && "$core_partitions" -gt 0 \
      && "$core_alive_active" -eq "$core_partitions" \
      && "$core_active_servers" -eq "$core_partitions" ]]; then
    server_class="status-ok"
    server_text="Online"
    if [[ "$core_ready_alive" -eq "$core_partitions" ]]; then
      world_class="status-ok"
      world_text="Healthy"
    else
      world_class="status-warn"
      world_text="Warming"
    fi
    if [[ "$core_ready_alive" -eq "$core_partitions" \
        && "$game_sg_connections" -ge "$core_partitions" \
        && "$fls_healthy" == "true" ]]; then
      access_class="status-ok"
      access_text="Available"
      status_detail_text="Core services are ready. Travel destinations start automatically when requested."
    else
      access_class="status-warn"
      access_text="Limited"
      if [[ "$fls_healthy" != "true" && "$core_ready_alive" -eq "$core_partitions" \
          && "$game_sg_connections" -ge "$core_partitions" ]]; then
        status_detail_text="Server-browser publication is catching up. Existing players remain connected."
      else
        status_detail_text="The core world is warming. Player access may take a few minutes."
      fi
    fi
  else
    server_class="status-down"
    server_text="Recovering"
    world_class="status-down"
    world_text="Degraded"
    access_class="status-down"
    access_text="Limited"
    status_detail_text="Required core services are recovering. Retry in a few minutes."
  fi
fi
runtime_ready_count="${current_ready_alive:-0}"
runtime_alive_count="${current_alive_active:-0}"
runtime_active_count="${active_servers:-0}"
runtime_partition_count="${partitions:-0}"
if [[ "$runtime_ready_count" =~ ^[0-9]+$ && "$runtime_alive_count" =~ ^[0-9]+$ \
    && "$runtime_active_count" =~ ^[0-9]+$ && "$runtime_partition_count" =~ ^[0-9]+$ \
    && "$runtime_partition_count" -gt 0 && "$runtime_ready_count" -eq "$runtime_partition_count" \
    && "$runtime_alive_count" -eq "$runtime_partition_count" && "$runtime_active_count" -eq "$runtime_partition_count" ]]; then
  runtime_farm_ready=true
fi
export DUNE_PUBLIC_FARM_READY="$runtime_farm_ready"

runtime_eval="$(
  python3 - <<'PY'
import datetime
import html
import json
import os
import shlex
import subprocess
import sys

services = {
    "survival", "overmap", "arrakeen", "harko-village", "testing-hephaestus",
    "testing-carthag", "testing-waterfat", "deep-desert", "proces-verbal",
    "lostharvest-ecolab-a", "lostharvest-ecolab-b", "lostharvest-forgottenlab",
    "art-of-kanly", "dungeon-hephaestus", "dungeon-oldcarthag",
    "faction-outpost-atre", "faction-outpost-hark", "heighliner-dungeon",
    "ecolab-green-089", "ecolab-green-152", "ecolab-green-024",
    "ecolab-green-195", "ecolab-green-136", "overland-m-01", "overland-s-04",
    "overland-s-06", "bandit-fortress", "overland-s-07", "overland-s-08",
    "dungeon-thepit",
}
project = os.environ.get("COMPOSE_PROJECT_NAME", os.environ.get("DOCKER_COMPOSE_PROJECT", "dune_server"))
dune_root = os.environ.get("DUNE_ROOT", "/opt/DuneAwakeningSelfHost")
restart_state_file = os.environ.get(
    "DUNE_PUBLIC_RESTART_STATE_FILE",
    os.path.join(dune_root, "backups", "admin-panel", "restart-jobs.json"),
)

# This optional map appears in the runtime list only while its labeled
# container is running, so no game .env value is needed to report it.
services.add("deep-desert-pvp")

def parse_started(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None

def fmt(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

def parse_epoch(value):
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)

def maintenance_restart_state():
    try:
        with open(restart_state_file, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, None
    completed = []
    active = []
    for job in state.get("jobs", []):
        status = job.get("status")
        if job.get("action") != "restart" or not job.get("execute"):
            continue
        if status in ("executed", "failed", "completed_with_warnings"):
            executed = parse_epoch(job.get("executedAt"))
            if executed:
                completed.append((executed, job))
        elif status in ("scheduled", "executing", "awaiting_reboot"):
            run_at = parse_epoch(job.get("runAt"))
            if run_at:
                active.append((run_at, job))
    latest = max(completed, key=lambda item: item[0]) if completed else None
    upcoming = min(active, key=lambda item: item[0]) if active else None
    return latest, upcoming

try:
    ps = subprocess.check_output(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.service"}}',
        ],
        text=True,
        stderr=subprocess.DEVNULL,
    )
except Exception:
    sys.exit(0)

now = datetime.datetime.now(datetime.timezone.utc)
farm_ready = os.environ.get("DUNE_PUBLIC_FARM_READY", "false").lower() == "true"
rows = []
restart_count = 0
for line in ps.splitlines():
    if not line.strip():
        continue
    parts = line.split("\t", 1)
    if len(parts) != 2 or parts[1] not in services:
        continue
    try:
        detail = json.loads(subprocess.check_output(["docker", "inspect", parts[0]], text=True, stderr=subprocess.DEVNULL))[0]
    except Exception:
        continue
    state = detail.get("State") or {}
    started = parse_started(state.get("StartedAt"))
    if state.get("Status") != "running" or not started:
        continue
    restart_count += int(detail.get("RestartCount") or 0)
    rows.append((parts[1], started))

if not rows:
    sys.exit(0)

newest = max(started for _, started in rows)
oldest = min(started for _, started in rows)
last_restart = int((now - newest).total_seconds())
oldest_uptime = int((now - oldest).total_seconds())
maintenance, scheduled_maintenance = maintenance_restart_state()
runtime_label = "Since restart"
runtime_text = fmt(last_restart)
runtime_class = "status-ok"
detail = (
    f"<strong>Warm maps</strong> {len(rows)}<br>"
    f"<strong>Map policy</strong> destinations start on demand<br>"
    f"<strong>Most recent restart</strong> {html.escape(newest.strftime('%Y-%m-%d %H:%M UTC'))}<br>"
    f"<strong>Oldest map uptime</strong> {html.escape(fmt(oldest_uptime))}<br>"
    f"<strong>Container restarts</strong> {restart_count}"
)
if scheduled_maintenance:
    run_at, job = scheduled_maintenance
    backup_state = "requested" if job.get("backup") else "not requested"
    job_status = str(job.get("status") or "scheduled")
    if job_status == "executing":
        runtime_label = "Maintenance in progress"
        runtime_text = "running"
    elif job_status == "awaiting_reboot":
        runtime_label = "Maintenance awaiting reboot"
        runtime_text = "awaiting reboot"
    elif run_at > now:
        remaining = int((run_at - now).total_seconds())
        runtime_label = "Maintenance scheduled"
        runtime_text = f"in {fmt(remaining)}"
    else:
        overdue = int((now - run_at).total_seconds())
        runtime_label = "Maintenance overdue"
        runtime_text = f"overdue by {fmt(overdue)}"
    runtime_class = "status-warn"
    schedule_label = "Scheduled maintenance" if run_at <= now else "Next maintenance"
    status_label = {
        "scheduled": "scheduled",
        "executing": "executing",
        "awaiting_reboot": "awaiting reboot",
    }.get(job_status, job_status)
    detail = (
        f"<strong>{schedule_label}</strong> {html.escape(run_at.strftime('%Y-%m-%d %H:%M UTC'))}<br>"
        f"<strong>Status</strong> {html.escape(status_label)}<br>"
        f"<strong>Target</strong> {html.escape(str(job.get('targetLabel') or job.get('target') or 'restart'))}<br>"
        f"<strong>Backup</strong> {html.escape(backup_state)} before restart<br>"
        f"<strong>Most recent container restart</strong> {html.escape(newest.strftime('%Y-%m-%d %H:%M UTC'))}<br>"
        f"<strong>Oldest map uptime</strong> {html.escape(fmt(oldest_uptime))}"
    )
    if maintenance:
        executed, previous_job = maintenance
        previous_status = str(previous_job.get("status") or "unknown")
        previous_status_label = {
            "executed": "completed",
            "failed": "failed",
            "completed_with_warnings": "completed with warnings",
        }.get(previous_status, previous_status)
        detail += (
            f"<br><strong>Last maintenance attempt</strong> {html.escape(executed.strftime('%Y-%m-%d %H:%M UTC'))}"
            f"<br><strong>Last result</strong> {html.escape(previous_status_label)}"
        )
elif maintenance:
    executed, job = maintenance
    age = int((now - executed).total_seconds())
    backup_state = "requested" if job.get("backup") else "not requested"
    status = str(job.get("status") or "unknown")
    execution_restart_gap = abs((executed - newest).total_seconds())
    recovered_after_readiness_timeout = (
        status == "failed"
        and farm_ready
        and execution_restart_gap <= 30 * 60
    )
    status_label = {
        "executed": "completed",
        "failed": "failed",
        "completed_with_warnings": "completed with warnings",
    }.get(status, status)
    if recovered_after_readiness_timeout:
        maintenance_heading = "Last maintenance"
        status_label = "recovered after readiness timeout"
        runtime_label = "Since maintenance"
        runtime_class = "status-ok"
    else:
        maintenance_heading = "Last maintenance attempt" if status == "failed" else "Last maintenance"
        runtime_label = "Maintenance failed" if status == "failed" else "Since maintenance"
    runtime_text = fmt(age)
    if (status in ("failed", "completed_with_warnings") and not recovered_after_readiness_timeout) or age > 36 * 3600:
        runtime_class = "status-warn"
    detail = (
        f"<strong>{maintenance_heading}</strong> {html.escape(executed.strftime('%Y-%m-%d %H:%M UTC'))}<br>"
        f"<strong>Status</strong> {html.escape(status_label)}<br>"
        f"<strong>Target</strong> {html.escape(str(job.get('targetLabel') or job.get('target') or 'restart'))}<br>"
        f"<strong>Backup</strong> {html.escape(backup_state)}<br>"
        f"<strong>Most recent container restart</strong> {html.escape(newest.strftime('%Y-%m-%d %H:%M UTC'))}<br>"
        f"<strong>Oldest map uptime</strong> {html.escape(fmt(oldest_uptime))}"
    )
    if recovered_after_readiness_timeout:
        detail += "<br><strong>Recovery</strong> Farm reached ready/active after the maintenance readiness timeout."
    if age > 36 * 3600:
        detail += "<br><strong>Schedule</strong> stale"
values = {
    "runtime_class": runtime_class,
    "runtime_label": runtime_label,
    "runtime_text": runtime_text,
    "runtime_detail_html": detail,
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
)" || runtime_eval=""
if [[ -n "$runtime_eval" ]]; then
  eval "$runtime_eval"
fi

checked_at="$(date -u '+%Y-%m-%d %H:%M UTC')"

status_block="$(cat <<EOF
<section class="status-card">
<h2>Server Status</h2>
<dl class="status-list">
<dt><span class="status-dot ${server_class}"></span>Server</dt><dd>${server_text}</dd>
<dt><span class="status-dot ${world_class}"></span>World health</dt><dd>${world_text}</dd>
<dt><span class="status-dot ${access_class}"></span>Player access</dt><dd>${access_text}</dd>
<dt><span class="status-dot ${runtime_class}"></span>${runtime_label}</dt><dd><span class="status-help" tabindex="0">${runtime_text}<span class="status-popover" role="dialog" aria-label="Uptime details">${runtime_detail_html}</span></span></dd>
</dl>
<p class="status-detail">${status_detail_text}</p>
<p class="status-updated">Last checked ${checked_at}.</p>
<p class="status-support"><span>Support this server</span><a href="https://www.paypal.com/donate/?business=donations%40snape.tech" target="_blank" rel="noopener noreferrer">PayPal</a><a href="https://ko-fi.com/snapetech" target="_blank" rel="noopener noreferrer">Ko-fi</a></p>
</section>
EOF
)"

status_marked_block="$(cat <<EOF
<!-- STATUS_BEGIN -->
<div id="server-status">
${status_block}
</div>
<!-- STATUS_END -->
EOF
)"

printf '%s\n' "$status_block" > "$tmp_status_html"

awk -v block="$status_marked_block" '
  /<!-- STATUS_BEGIN -->/ {
    print block
    skipping = 1
    next
  }
  /<!-- STATUS_END -->/ {
    skipping = 0
    next
  }
  !skipping { print }
' "$tmp_source_index" > "$tmp_index"

install_output_file "$tmp_status_html" "$STATUS_FILE"
install_output_file "$tmp_index" "$INDEX_FILE"

snapshot_script="$(dirname "$0")/render-dune-public-snapshot.py"
if [[ -x "$snapshot_script" ]]; then
  if [[ -w "$STATIC_DIR" ]]; then
    DUNE_ROOT="$DUNE_ROOT" STATIC_DIR="$STATIC_DIR" "$snapshot_script" || true
  else
    tmp_static="$(mktemp -d)"
    trap 'rm -f "$tmp_status" "$tmp_index" "$tmp_source_index" "$tmp_status_html"; rm -rf "$tmp_static"' EXIT
    DUNE_ROOT="$DUNE_ROOT" STATIC_DIR="$tmp_static" "$snapshot_script" || true
    find "$tmp_static" -maxdepth 1 -type f \( \
      -name '*.json' -o \
      -name '*.svg' -o \
      -name '*.webp' \
    \) -print0 |
      while IFS= read -r -d '' artifact; do
        sudo install -m 0644 "$artifact" "$STATIC_DIR/$(basename "$artifact")"
      done
  fi
fi

if [[ -x "$DRIFT_CHECK_SCRIPT" ]]; then
  STATIC_DIR="$STATIC_DIR" INDEX_FILE="$INDEX_FILE" STATUS_FILE="$STATUS_FILE" "$DRIFT_CHECK_SCRIPT" "$STATIC_DIR"
fi

if [[ -n "$SYNC_HOST" ]]; then
  tar --warning=no-file-changed --ignore-failed-read -C "$(dirname "$(dirname "$INDEX_FILE")")" -cf - "$(basename "$(dirname "$INDEX_FILE")")" |
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$SYNC_HOST" "sudo mkdir -p '$SYNC_STATIC_ROOT' && sudo tar -C '$SYNC_STATIC_ROOT' -xf -"
fi
