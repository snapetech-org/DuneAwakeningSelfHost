#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/.env}"
mode="${2:-}"
if [[ "$env_file" != /* ]]; then env_file="$repo_root/$env_file"; fi
[[ -f "$env_file" ]] || { printf 'env file does not exist: %s\n' "$env_file" >&2; exit 1; }
[[ "$mode" == "--execute" || -z "$mode" ]] || { printf 'Usage: %s [ENV_FILE] [--execute]\n' "$0" >&2; exit 2; }

read_env() { sed -n "s/^${1}=//p" "$env_file" | tail -1; }
required_host="${DUNE_FEATURE_PARITY_ALLOWED_HOST:-$(read_env DUNE_FEATURE_PARITY_ALLOWED_HOST)}"
required_host="${required_host:-kspls0}"
current_host="$(hostname -s)"

keys=(
  DUNE_ADMIN_REQUIRE_TOKEN DUNE_ADMIN_MUTATIONS_ENABLED DUNE_ADMIN_ITEM_GRANTS_ENABLED
  DUNE_ADMIN_CHANGE_CONTRACTS_ENABLED DUNE_ADMIN_CHANGE_CONTRACTS_REQUIRED
  DUNE_ADMIN_GM_COMMANDS_ENABLED DUNE_SERVER_NOTIFICATION_SYSTEM_ENABLED
  DUNE_ADMIN_PLAYER_RUNTIME_MUTATIONS_ENABLED DUNE_ADMIN_PLAYER_LIFE_RECOVERY_ENABLED
  DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED
  DUNE_ADMIN_VEHICLE_MUTATIONS_ENABLED
  DUNE_ADMIN_BOOTSTRAP_MUTATIONS_ENABLED DUNE_ADMIN_CATALOG_ENABLED
  DUNE_ADMIN_TYPED_KNOBS_ENABLED DUNE_ADMIN_EVENT_EXECUTION_ENABLED
  DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED DUNE_ADMIN_CARE_PACKAGES_ENABLED
  DUNE_ADMIN_CARE_PACKAGES_AUTO_ENABLED DUNE_ADMIN_BLUEPRINT_MUTATIONS_ENABLED
  DUNE_ADMIN_AUGMENT_MUTATIONS_ENABLED DUNE_ADMIN_DATABASE_QUERY_ENABLED
  DUNE_ADMIN_DATABASE_WRITE_ENABLED DUNE_ADMIN_DATABASE_ROW_MUTATIONS_ENABLED
  DUNE_ADMIN_DATABASE_PASSWORD_MUTATIONS_ENABLED DUNE_ADMIN_BACKUP_MUTATIONS_ENABLED
  DUNE_ADMIN_BACKUP_RESTORE_ENABLED DUNE_RESTORE_DRILL_ENABLED
  DUNE_ADMIN_RESTORE_DRILL_EXECUTION_ENABLED DUNE_RABBITMQ_RESTORE_DRILL_ENABLED
  DUNE_ADMIN_RABBITMQ_RESTORE_DRILL_EXECUTION_ENABLED DUNE_OPERATIONAL_SLO_ENABLED
  DUNE_ADMIN_OPERATIONAL_SLO_MUTATIONS_ENABLED DUNE_CAPACITY_INTELLIGENCE_ENABLED
  DUNE_CAPACITY_AUTO_APPLY_ENABLED DUNE_MAINTENANCE_PLANNER_ENABLED DUNE_DESIRED_STATE_ENABLED
  DUNE_ADMIN_DESIRED_STATE_MUTATIONS_ENABLED DUNE_CHANGE_INTELLIGENCE_ENABLED
  DUNE_RESPONSE_DRILLS_ENABLED DUNE_DEPLOYMENT_ASSURANCE_ENABLED
  DUNE_CANARY_AUTOPILOT_ENABLED DUNE_OPERATIONS_BRIEFING_ENABLED
  DUNE_FEATURE_READINESS_HISTORY_ENABLED
  DUNE_CREDENTIAL_LIFECYCLE_ENABLED
  DUNE_PEER_WATCH_ENABLED
  DUNE_ADMIN_MEMORY_MUTATIONS_ENABLED
  DUNE_ADMIN_AUTOSCALER_MUTATIONS_ENABLED DUNE_DISCORD_ADAPTER_ENABLED
  DUNE_ADMIN_ADDON_MUTATIONS_ENABLED DUNE_ADMIN_SERVICE_CONTROL_ENABLED
  DUNE_ADMIN_STATEFUL_SERVICE_CONTROL_ENABLED DUNE_ADMIN_UPDATE_MUTATIONS_ENABLED
  DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED
  DUNE_ADMIN_FACTION_MUTATIONS_ENABLED DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED
  DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED DUNE_ADMIN_GUILD_MUTATIONS_ENABLED
  DUNE_ADMIN_MARKER_MUTATIONS_ENABLED DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED
  DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED
  DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED
  DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED
  DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED DUNE_ADMIN_CHARACTER_SWAP_ENABLED
  DUNE_AUTOSCALER_ENABLED DUNE_METRICS_ENABLED DUNE_PUBLIC_IP_MONITOR_ENABLED
  DUNE_SIETCH_MUTATIONS_ENABLED DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED
  DUNE_WEBHOOKS_ENABLED
  DUNE_COMMUNITY_REWARDS_ENABLED DUNE_COMMUNITY_DELIVERY_ENABLED
  DUNE_MODERATION_ENABLED DUNE_MODERATION_ENFORCEMENT_ENABLED
  DUNE_BASE_CREATOR_ENABLED DUNE_ADMIN_BASE_RETIREMENT_MUTATIONS_ENABLED
  DUNE_ADMIN_BASE_COOLDOWN_MUTATIONS_ENABLED
  DUNE_GAMEPLAY_PRESETS_ENABLED DUNE_GAMEPLAY_PRESET_MUTATIONS_ENABLED
  DUNE_COMMAND_CONSOLE_ENABLED
  DUNE_ADMIN_COSMETIC_MUTATIONS_ENABLED
  DUNE_ADMIN_FEDERATED_AUTH_ENABLED
  DUNE_BACKUP_ARCHIVE_ENCRYPTION_ENABLED
)

if [[ -z "$mode" ]]; then
  printf 'plan: enable %s feature gates on %s\n' "${#keys[@]}" "$required_host"
  printf 'plan: autoscaler=adaptive (balanced baseline plus evidence-qualified retention) always-on=survival,overmap retention=900s overrides=arrakeen/harko/deep-desert warm-cap=4 memory-floor=16GiB\n'
  printf 'plan: metrics=enabled public-ip-monitor=enabled/dry-run sietch-gate=enabled\n'
  printf 'no changes made; rerun with --execute\n'
  exit 0
fi

[[ "$current_host" == "$required_host" ]] || {
  printf 'refusing feature activation: hostname=%s required=%s\n' "$current_host" "$required_host" >&2
  exit 77
}

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$repo_root/backups/admin-panel/feature-parity"
mkdir -p "$backup_dir"
backup="$backup_dir/env-before-enable-$stamp"
install -m 600 "$env_file" "$backup"

feature_update=(python3 "$repo_root/scripts/update-env-file.py" "$env_file" --allow-protected-link --quiet)
for key in "${keys[@]}"; do feature_update+=(--set "$key" true); done
set_value() {
  [[ $# -eq 2 && -n "$1" ]] || { printf 'set_value requires a key and value\n' >&2; exit 2; }
  feature_update+=(--set "$1" "$2")
}
if [[ ! -f "$repo_root/config/community-rewards.json" ]]; then
  install -m 600 "$repo_root/config/community-rewards.example.json" "$repo_root/config/community-rewards.json"
else
  python3 "$repo_root/scripts/merge-community-engagement-policy.py" \
    "$repo_root/config/community-rewards.json" \
    "$repo_root/config/community-rewards.example.json" \
    "$repo_root/backups/admin-panel/config-upgrades"
fi
chmod 600 "$repo_root/config/community-rewards.json"
mkdir -p "$repo_root/config/secrets"
chmod 700 "$repo_root/config/secrets"
for provider in vote payment; do
  secret_file="$repo_root/config/secrets/community-${provider}-webhook.secret"
  if [[ ! -s "$secret_file" ]]; then
    command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate community webhook secrets\n' >&2; exit 1; }
    openssl rand -hex 32 > "$secret_file"
    chmod 600 "$secret_file"
  fi
done
session_secret_file="$repo_root/config/secrets/admin-session.secret"
if [[ ! -s "$session_secret_file" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the admin session secret\n' >&2; exit 1; }
  openssl rand -hex 32 > "$session_secret_file"
  chmod 600 "$session_secret_file"
fi
desired_state_secret_file="$repo_root/config/secrets/desired-state-hmac.secret"
if [[ ! -s "$desired_state_secret_file" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the desired-state HMAC secret\n' >&2; exit 1; }
  openssl rand -hex 32 > "$desired_state_secret_file"
  chmod 600 "$desired_state_secret_file"
fi
change_intelligence_secret_file="$repo_root/config/secrets/change-intelligence-hmac.secret"
if [[ ! -s "$change_intelligence_secret_file" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the change-intelligence HMAC secret\n' >&2; exit 1; }
  openssl rand -hex 32 > "$change_intelligence_secret_file"
  chmod 600 "$change_intelligence_secret_file"
fi
feature_readiness_history_secret_file="$repo_root/config/secrets/feature-readiness-history-hmac.secret"
if [[ ! -s "$feature_readiness_history_secret_file" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the feature-readiness history HMAC secret\n' >&2; exit 1; }
  openssl rand -hex 32 > "$feature_readiness_history_secret_file"
  chmod 600 "$feature_readiness_history_secret_file"
fi
credential_lifecycle_secret_file="$repo_root/config/secrets/credential-lifecycle-hmac.secret"
if [[ ! -s "$credential_lifecycle_secret_file" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the credential-lifecycle HMAC secret\n' >&2; exit 1; }
  openssl rand -hex 32 > "$credential_lifecycle_secret_file"
  chmod 600 "$credential_lifecycle_secret_file"
fi
set_value DUNE_FEATURE_PARITY_ALLOWED_HOST "$required_host"
set_value DUNE_HOST_UID "$(id -u)"
set_value DUNE_HOST_GID "$(id -g)"
set_value DUNE_RESTORE_DRILL_HOST_WORKSPACE "$repo_root"
set_value DUNE_RESTORE_DRILL_DOCKER_SOCKET /var/run/docker.sock
set_value DUNE_RESTORE_DRILL_IMAGE registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13
set_value DUNE_RESTORE_DRILL_MAX_BACKUP_AGE_HOURS 36
set_value DUNE_RESTORE_DRILL_MAX_RESTORE_SECONDS 900
set_value DUNE_RESTORE_DRILL_MEMORY_MIB 2048
set_value DUNE_RESTORE_DRILL_PGDATA_MIB 1536
set_value DUNE_RESTORE_DRILL_CPUS 2
set_value DUNE_RESTORE_DRILL_PIDS_LIMIT 128
set_value DUNE_RESTORE_DRILL_RECEIPT_RETENTION 1000
image_tag="$(read_env DUNE_IMAGE_TAG)"
image_tag="${image_tag:-2036754-0-shipping}"
set_value DUNE_RABBITMQ_RESTORE_DRILL_HOST_WORKSPACE "$repo_root"
set_value DUNE_RABBITMQ_RESTORE_DRILL_DOCKER_SOCKET /var/run/docker.sock
set_value DUNE_RABBITMQ_RESTORE_DRILL_IMAGE "registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:${image_tag}"
set_value DUNE_RABBITMQ_RESTORE_DRILL_MAX_BACKUP_AGE_HOURS 36
set_value DUNE_RABBITMQ_RESTORE_DRILL_READINESS_SECONDS 180
set_value DUNE_RABBITMQ_RESTORE_DRILL_MEMORY_MIB 1024
set_value DUNE_RABBITMQ_RESTORE_DRILL_CPUS 1
set_value DUNE_RABBITMQ_RESTORE_DRILL_PIDS_LIMIT 256
set_value DUNE_RABBITMQ_RESTORE_DRILL_RECEIPT_RETENTION 1000
set_value DUNE_OPERATIONAL_SLO_POLICY /workspace/config/operational-slo.json
set_value DUNE_OPERATIONAL_SLO_DATABASE /workspace/backups/operational-slo/slo.sqlite3
set_value DUNE_OPERATIONAL_SLO_POLL_SECONDS 60
set_value DUNE_OPERATIONAL_SLO_BACKUP_MAX_AGE_HOURS 36
set_value DUNE_OPERATIONAL_SLO_RESTORE_PROOF_MAX_AGE_HOURS 48
set_value DUNE_OPERATIONAL_SLO_RABBITMQ_RESTORE_PROOF_MAX_AGE_HOURS 192
set_value DUNE_OPERATIONAL_SLO_MEMORY_FLOOR_GIB 8
set_value DUNE_CAPACITY_INTELLIGENCE_POLICY /workspace/config/capacity-intelligence.json
set_value DUNE_CAPACITY_INTELLIGENCE_DATABASE /workspace/backups/capacity-intelligence/capacity.sqlite3
set_value DUNE_CAPACITY_INTELLIGENCE_POLL_SECONDS 30
set_value DUNE_CAPACITY_AUTO_APPLY_INTERVAL_HOURS 24
set_value DUNE_MAINTENANCE_PLANNER_POLICY /workspace/config/maintenance-planner.json
set_value DUNE_DESIRED_STATE_POLICY /workspace/config/desired-state.json
set_value DUNE_DESIRED_STATE_DATABASE /workspace/backups/desired-state/desired-state.sqlite3
set_value DUNE_DESIRED_STATE_HMAC_SECRET_FILE /workspace/config/secrets/desired-state-hmac.secret
set_value DUNE_DESIRED_STATE_POLL_SECONDS 60
set_value DUNE_CHANGE_INTELLIGENCE_POLICY /workspace/config/change-intelligence.json
set_value DUNE_CHANGE_INTELLIGENCE_DATABASE /workspace/backups/change-intelligence/change-intelligence.sqlite3
set_value DUNE_CHANGE_INTELLIGENCE_HMAC_SECRET_FILE /workspace/config/secrets/change-intelligence-hmac.secret
set_value DUNE_CHANGE_INTELLIGENCE_EVIDENCE_DIR /workspace/backups/operator-evidence
set_value DUNE_CHANGE_INTELLIGENCE_HOST_EVIDENCE_DIR backups/operator-evidence
set_value DUNE_CHANGE_INTELLIGENCE_STATUS_CACHE_SECONDS 10
set_value DUNE_OPERATIONS_BRIEFING_POLL_SECONDS 300
set_value DUNE_OPERATIONS_BRIEFING_REFRESH_HOURS 24
set_value DUNE_OPERATIONS_BRIEFING_MAX_AGE_HOURS 36
set_value DUNE_OPERATIONS_BRIEFING_MIN_INTERVAL_SECONDS 300
set_value DUNE_OPERATIONS_BRIEFING_CHANGE_MIN_INTERVAL_SECONDS 15
set_value DUNE_OPERATIONS_BRIEFING_EVENT_DEBOUNCE_SECONDS 5
set_value DUNE_OPERATIONS_BRIEFING_RETENTION 100
set_value DUNE_FEATURE_READINESS_HISTORY_DATABASE /workspace/backups/feature-readiness/history.sqlite3
set_value DUNE_FEATURE_READINESS_HISTORY_HMAC_SECRET_FILE /workspace/config/secrets/feature-readiness-history-hmac.secret
set_value DUNE_CREDENTIAL_LIFECYCLE_DATABASE /workspace/backups/credential-lifecycle/history.sqlite3
set_value DUNE_CREDENTIAL_LIFECYCLE_HMAC_SECRET_FILE /workspace/config/secrets/credential-lifecycle-hmac.secret
set_value DUNE_CREDENTIAL_LIFECYCLE_ANCHOR_FILE /workspace/backups/credential-lifecycle/history.anchor.json
set_value DUNE_DEPLOYMENT_ASSURANCE_STATE_DIR /workspace/backups/deployment-assurance
set_value DUNE_DEPLOYMENT_ASSURANCE_WORKSPACE /source-workspace
set_value DUNE_DEPLOYMENT_ASSURANCE_PROMETHEUS_URL http://prometheus:9090
set_value DUNE_ADMIN_CHANGE_CONTRACT_TTL_SECONDS 120
set_value DUNE_MODERATION_POLL_SECONDS 15
set_value DUNE_MODERATION_RETENTION_DAYS 90
set_value DUNE_MODERATION_HEATMAP_CELL_SIZE 25000
set_value DUNE_MODERATION_KICK_COOLDOWN_SECONDS 60
set_value DUNE_MODERATION_LOG_SERVICES survival,director,gateway,game-rmq
set_value DUNE_AUTOSCALER_PROFILE adaptive
set_value DUNE_AUTOSCALER_DEFAULT_MODE dynamic
set_value DUNE_AUTOSCALER_ALWAYS_ON_SERVICES survival,overmap
set_value DUNE_AUTOSCALER_SIMULATION_REQUIRED_SERVICES survival
set_value DUNE_AUTOSCALER_IDLE_SECONDS 300
set_value DUNE_AUTOSCALER_DEMAND_TTL_SECONDS 900
set_value DUNE_AUTOSCALER_POLL_SECONDS 3
set_value DUNE_AUTOSCALER_RECONCILE_SECONDS 30
set_value DUNE_AUTOSCALER_FAST_START true
set_value DUNE_ADMIN_METRICS_CACHE_SECONDS 30
set_value DUNE_AUTOSCALER_BALANCED_RETENTION_SECONDS 900
set_value DUNE_AUTOSCALER_BALANCED_RETENTION_BY_SERVICE arrakeen=2700,harko-village=2700,deep-desert=1800
set_value DUNE_AUTOSCALER_BALANCED_MAX_WARM_MAPS 4
set_value DUNE_AUTOSCALER_BALANCED_MIN_AVAILABLE_MEMORY_GIB 16
set_value DUNE_PUBLIC_IP_MONITOR_ALLOWED_HOST "$required_host"
set_value DUNE_PUBLIC_IP_MONITOR_INTERVAL_MINUTES 5
set_value DUNE_PUBLIC_IP_MONITOR_DRY_RUN true
set_value DUNE_SIETCH_ALLOWED_HOST "$required_host"
set_value DUNE_DISCORD_ALLOWED_HOST "$required_host"
admin_host_port="${DUNE_ADMIN_HOST_PORT:-$(read_env DUNE_ADMIN_HOST_PORT)}"
set_value DUNE_DISCORD_ADAPTER_URL "http://127.0.0.1:${admin_host_port:-18080}"

if [[ -z "$(read_env DUNE_SERVER_COMMANDS_AUTH_TOKEN)" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the command token\n' >&2; exit 1; }
  set_value DUNE_SERVER_COMMANDS_AUTH_TOKEN "$(openssl rand -hex 32)"
fi

if [[ -z "$(read_env DUNE_BOT_API_TOKEN)" ]]; then
  command -v openssl >/dev/null 2>&1 || { printf 'openssl is required to generate the Discord adapter token\n' >&2; exit 1; }
  set_value DUNE_BOT_API_TOKEN "$(openssl rand -hex 32)"
fi

"${feature_update[@]}"

printf 'enabled feature parity on %s; env backup=%s; command token=%s; Discord adapter token=%s\n' \
  "$current_host" "$backup" configured configured
printf 'public IP monitor remains dry-run until its first successful check\n'
