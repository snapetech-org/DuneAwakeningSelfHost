#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/push-assured-control-plane.sh --manifest FILE --reason TEXT [--host HOST] [--required-host HOSTNAME] [--remote-workspace DIR] [--remote-env FILE] [--pre-change-backup DIR] [--enable-service-recovery]

Stages exact manifest files over SSH, without overwriting the live tree, then
runs the production host's two-phase assured deployment from that private stage.
EOF
}

manifest=""
reason=""
remote_host="kspls0"
required_host="kspls0"
remote_workspace="/home/keith/Documents/code/DuneAwakeningSelfHost"
remote_env=".env"
pre_change_backup=""
recovery_arg=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) manifest="${2:-}"; shift 2 ;;
    --reason) reason="${2:-}"; shift 2 ;;
    --host) remote_host="${2:-}"; shift 2 ;;
    --required-host) required_host="${2:-}"; shift 2 ;;
    --remote-workspace) remote_workspace="${2:-}"; shift 2 ;;
    --remote-env) remote_env="${2:-}"; shift 2 ;;
    --enable-service-recovery) recovery_arg="--enable-service-recovery"; shift ;;
    --pre-change-backup) pre_change_backup="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -f "$manifest" ]] || { printf 'a readable --manifest is required\n' >&2; exit 2; }
[[ ${#reason} -ge 10 && ${#reason} -le 1000 ]] || { printf -- '--reason must contain 10..1000 characters\n' >&2; exit 2; }
[[ "$remote_host" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || { printf 'invalid remote host\n' >&2; exit 2; }
[[ "$required_host" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || { printf 'invalid required hostname\n' >&2; exit 2; }
[[ "$remote_workspace" == /* && "$remote_workspace" != *$'\n'* ]] || { printf 'remote workspace must be absolute\n' >&2; exit 2; }
[[ "$remote_env" != *$'\n'* ]] || { printf 'invalid remote env path\n' >&2; exit 2; }
[[ "$pre_change_backup" != *$'\n'* ]] || { printf 'invalid pre-change backup path\n' >&2; exit 2; }
if [[ "$remote_env" != /* ]]; then
  remote_env="${remote_workspace%/}/$remote_env"
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python3 scripts/deployment-assurance.py verify --manifest "$manifest" --workspace "$repo_root" >/dev/null
"$repo_root/scripts/deploy-admin-panel.sh" --test-only

stage_id="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
remote_stage="/tmp/dash-assured-stage-${stage_id}"
work="$(mktemp -d -p "${TMPDIR:-/tmp}" dash-assured-push.XXXXXX)"
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

python3 - "$manifest" >"$work/files.list" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
for row in d["files"]: print(row["path"])
PY
for support in \
  admin/admin_panel.py admin/audit_ledger.py admin/change_approvals.py admin/change_intelligence.py \
  admin/community_canary.py admin/community_rewards.py admin/credential_lifecycle.py admin/deployment_assurance.py admin/desired_state.py \
  admin/creator_canary.py admin/public_ip_canary.py admin/canary_autopilot.py admin/operations_briefing.py admin/operations_calendar.py admin/alert_inbox.py admin/peer_watch.py admin/player_life_recovery.py admin/offline_teleport.py admin/addon_admin.py admin/base_creator.py admin/base_retirement.py admin/cosmetics_admin.py admin/gameplay_presets.py \
  docs/ecosystem-feature-parity-audit.md \
  admin/feature_readiness_history.py admin/maintenance_planner.py admin/maintenance_outcomes.py admin/rabbitmq_restore_drill.py admin/restore_drill.py \
  admin/update_readiness.py admin/runtime_security.py admin/host_service_control.py scripts/deployment-assurance.py \
  scripts/dune_restart_api.py scripts/check-current-steam-build.py scripts/check-steam-update.sh scripts/snapshot-audit-ledger.py scripts/install-dune-restart-api.sh scripts/test-dune-restart-api.py scripts/test-host-service-control.py scripts/rollout-runtime-security.py scripts/test-rollout-runtime-security.py \
  config/systemd/dune-restart-api.service config/systemd/dune-restart-api.socket \
  scripts/deploy-admin-panel.sh scripts/push-assured-control-plane.sh scripts/check-admin-ingress.sh scripts/test-cosmetics-admin.py scripts/test-restore-drill.py scripts/test-operational-slo.py scripts/test-capacity-intelligence.py scripts/test-desired-state.py scripts/test-change-intelligence.py scripts/test-deployment-assurance.py scripts/test-admin-panel-safe-surfaces.py scripts/test-docker-api-proxy.py scripts/test-deploy-admin-panel.py \
  scripts/backup_verification_view.py scripts/test-backup-verification-view.py scripts/test-compose-file-protocol.py scripts/assured-control-plane-deploy.sh scripts/lib/snape-curl-auth.sh scripts/secret_env.py scripts/http_url.py scripts/test-security-boundaries.py scripts/backup-state.sh scripts/snapshot_operator_evidence.py scripts/test-snapshot-operator-evidence.py scripts/restore-state.sh scripts/restore-alert-inbox.sh scripts/verify-backup.sh scripts/public-ip-monitor.sh scripts/generate-rabbitmq-cert.sh \
  scripts/check-rabbitmq-cert-sans.sh scripts/restart-target.sh scripts/map_catalog.py scripts/install-public-ip-monitor.sh \
  config/systemd/dune-public-ip-monitor.service config/systemd/dune-public-ip-monitor.timer; do
  grep -Fxq "$support" "$work/files.list" || printf '%s\n' "$support" >>"$work/files.list"
done

required_q="$(printf '%q' "$required_host")"
ssh -o BatchMode=yes "$remote_host" "set -e; test \"\$(hostname -s)\" = $required_q; install -d -m 700 '$remote_stage'"
tar -C "$repo_root" -czf - --files-from="$work/files.list" | ssh -o BatchMode=yes "$remote_host" "tar -C '$remote_stage' -xzf -"
scp -q -o BatchMode=yes "$manifest" "$remote_host:$remote_stage/deployment-manifest.json"

reason_q="$(printf '%q' "$reason")"
stage_q="$(printf '%q' "$remote_stage")"
workspace_q="$(printf '%q' "$remote_workspace")"
env_q="$(printf '%q' "$remote_env")"
pre_change_arg=""
if [[ -n "$pre_change_backup" ]]; then
  pre_change_arg="--pre-change-backup $(printf '%q' "$pre_change_backup")"
fi
ssh -o BatchMode=yes "$remote_host" "set -euo pipefail; trap 'rm -rf $stage_q' EXIT; test \"\$(hostname -s)\" = $required_q; DUNE_PRODUCTION_HOST=$required_q PYTHONPATH=$stage_q/admin $stage_q/scripts/assured-control-plane-deploy.sh --manifest $stage_q/deployment-manifest.json --reason $reason_q --stage $stage_q --workspace $workspace_q $pre_change_arg $recovery_arg $env_q"
