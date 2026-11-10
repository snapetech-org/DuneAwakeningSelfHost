#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

remote="${2:-${POSTGRES_REMOTE_REPLICA_HOST:-$(read_env POSTGRES_REMOTE_REPLICA_HOST)}}"
remote_root="${3:-${POSTGRES_REMOTE_REPLICA_ROOT:-$(read_env POSTGRES_REMOTE_REPLICA_ROOT)}}"
remote_repo="${DUNE_STANDBY_REPO_ROOT:-$PWD}"
db="${DUNE_DATABASE:-$(read_env DUNE_DATABASE)}"; db="${db:-dune_sb_1_4_0_0}"
slot="${POSTGRES_REMOTE_REPLICATION_SLOT:-$(read_env POSTGRES_REMOTE_REPLICATION_SLOT)}"; slot="${slot:-dune_standby_remote}"
image_tag="${DUNE_IMAGE_TAG:-$(read_env DUNE_IMAGE_TAG)}"; image_tag="${image_tag:-1968181-0-shipping}"
max_snapshot_age_hours="${DUNE_STANDBY_MAX_SNAPSHOT_AGE_HOURS:-2}"
status_rc=0

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi
if [[ -z "$remote" ]]; then
  printf 'remote host required: %s ENV_FILE REMOTE [ROOT]\n' "$0" >&2
  exit 1
fi
if [[ -z "$remote_root" ]]; then
  printf 'POSTGRES_REMOTE_REPLICA_ROOT or ROOT is required\n' >&2
  exit 1
fi

compose=(docker compose --env-file "$env_file" -f compose.yaml -f compose.replica.yaml)
game_image="registry.funcom.com/funcom/self-hosting/seabass-server:${image_tag}"
rmq_image="registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:${image_tag}"
director_image="${DUNE_DIRECTOR_IMAGE:-$(read_env DUNE_DIRECTOR_IMAGE)}"
director_image="${director_image:-registry.funcom.com/funcom/self-hosting/seabass-server-bg-director:${image_tag}}"
text_router_image="${DUNE_TEXT_ROUTER_IMAGE:-$(read_env DUNE_TEXT_ROUTER_IMAGE)}"
text_router_image="${text_router_image:-registry.funcom.com/funcom/self-hosting/seabass-server-text-router:${image_tag}}"
db_utils_image="registry.funcom.com/funcom/self-hosting/seabass-server-db-utils:${image_tag}"
postgres_image="${POSTGRES_IMAGE:-${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}}"

printf '== primary replication slot: %s ==\n' "$slot"
"${compose[@]}" exec -T postgres psql -U dune -d "$db" -c "
select slot_name, active, restart_lsn, confirmed_flush_lsn, wal_status
from pg_replication_slots
where slot_name = '$slot';
select application_name, client_addr, state, sync_state, write_lag, flush_lag, replay_lag
from pg_stat_replication
order by application_name, client_addr;
"
slot_active="$("${compose[@]}" exec -T postgres psql -U dune -d "$db" -Atc "select coalesce((select active::text from pg_replication_slots where slot_name = '$slot'), 'missing');" 2>/dev/null || printf 'error')"
case "$slot_active" in
  t|true) printf 'OK remote replication slot is active\n' ;;
  *) printf 'WARN remote replication slot is not active: %s\n' "$slot_active"; status_rc=1 ;;
esac

printf '\n== remote standby recovery: %s ==\n' "$remote"
ssh "$remote" "docker ps --filter name=dune-postgres-replica --format '{{.Names}} {{.Status}}'
docker exec dune-postgres-replica psql -U dune -d '$db' -c \"select pg_is_in_recovery() as standby, now() - pg_last_xact_replay_timestamp() as replay_delay;\""
standby_recovery="$(ssh "$remote" "docker exec dune-postgres-replica psql -U dune -d '$db' -Atc 'select pg_is_in_recovery();'" 2>/dev/null || printf 'error')"
case "$standby_recovery" in
  t|true) printf 'OK remote Postgres is still in recovery\n' ;;
  *) printf 'WARN remote Postgres is not in standby recovery: %s\n' "$standby_recovery"; status_rc=1 ;;
esac

printf '\n== remote snapshot freshness ==\n'
ssh "$remote" "latest=\$(find '$remote_root/snapshots' -maxdepth 1 -type f -name 'postgres-${db}-*.dump' -printf '%T@ %TY-%Tm-%TdT%TH:%TM:%TSZ %s %f\n' 2>/dev/null | sort -n | tail -1)
if [ -z \"\$latest\" ]; then
  echo 'WARN: no remote snapshots found'
  exit 0
fi
echo \"\$latest\"
age=\$(( \$(date +%s) - \${latest%%.*} ))
echo \"latest_snapshot_age_seconds=\$age max_expected_seconds=$((max_snapshot_age_hours * 3600))\"
if [ \"\$age\" -gt $((max_snapshot_age_hours * 3600)) ]; then
  echo 'WARN: latest remote snapshot is older than expected'
fi"
snapshot_age="$(ssh "$remote" "latest=\$(find '$remote_root/snapshots' -maxdepth 1 -type f -name 'postgres-${db}-*.dump' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
if [ -z \"\$latest\" ]; then
  echo missing
else
  echo \$(( \$(date +%s) - \${latest%%.*} ))
fi" 2>/dev/null || printf 'error')"
case "$snapshot_age" in
  ''|missing|error)
    printf 'WARN latest remote snapshot age unavailable: %s\n' "${snapshot_age:-empty}"
    status_rc=1
    ;;
  *)
    if [[ "$snapshot_age" -le $((max_snapshot_age_hours * 3600)) ]]; then
      printf 'OK latest remote snapshot is within %s hours\n' "$max_snapshot_age_hours"
    else
      status_rc=1
    fi
    ;;
esac

printf '\n== mirrored file surfaces on remote ==\n'
ssh "$remote" "cd '$remote_repo' 2>/dev/null || { echo 'WARN: repo path missing: $remote_repo'; exit 0; }
for path in .env compose.yaml compose.allmaps.yaml compose.failover-standby.yaml config data/server-saved config/tls/rabbitmq; do
  if [ -e \"\$path\" ]; then
    printf 'OK %s\n' \"\$path\"
  else
    printf 'WARN missing %s\n' \"\$path\"
  fi
done"

printf '\n== config checksum comparison ==\n'
local_manifest="$(mktemp)"
remote_manifest="$(mktemp)"
trap 'rm -f "$local_manifest" "$remote_manifest"' EXIT
find .env compose.yaml compose.allmaps.yaml compose.failover-standby.yaml config \
  -type f \
  -not -path 'config/tls/rabbitmq.backup.*/*' \
  -not -path 'config/tls/rabbitmq-staged/*' \
  -not -path 'config/tls/rabbitmq-staged.backup.*/*' \
  -not -path 'config/tls/*/*.key' \
  -print0 2>/dev/null \
  | sort -z \
  | xargs -0 sha256sum > "$local_manifest"
if ssh "$remote" "cd '$remote_repo' && find .env compose.yaml compose.allmaps.yaml compose.failover-standby.yaml config \
  -type f \
  -not -path 'config/tls/rabbitmq.backup.*/*' \
  -not -path 'config/tls/rabbitmq-staged/*' \
  -not -path 'config/tls/rabbitmq-staged.backup.*/*' \
  -not -path 'config/tls/*/*.key' \
  -print0 2>/dev/null \
  | sort -z \
  | xargs -0 sha256sum" > "$remote_manifest"; then
  if diff -u "$local_manifest" "$remote_manifest"; then
    printf 'OK config checksums match, excluding private key files\n'
  else
    printf 'WARN config checksums differ, excluding private key files\n'
    status_rc=1
  fi
else
  printf 'WARN unable to calculate remote config checksums\n'
  status_rc=1
fi

printf '\n== required images on remote ==\n'
ssh "$remote" "for image in '$postgres_image' '$game_image' '$rmq_image' '$director_image' '$text_router_image' '$db_utils_image'; do
  if docker image inspect \"\$image\" >/dev/null 2>&1; then
    printf 'OK %s\n' \"\$image\"
  else
    printf 'WARN missing %s\n' \"\$image\"
  fi
done"
missing_images="$(ssh "$remote" "for image in '$postgres_image' '$game_image' '$rmq_image' '$director_image' '$text_router_image' '$db_utils_image'; do docker image inspect \"\$image\" >/dev/null 2>&1 || echo \"\$image\"; done" 2>/dev/null || printf 'image-check-error')"
if [[ -n "$missing_images" ]]; then
  printf 'WARN one or more required images are missing on remote\n'
  status_rc=1
else
  printf 'OK all required images are present on remote\n'
fi

if [[ "${DUNE_STANDBY_SKIP_RMQ_TLS_CHECK:-}" == "true" ]]; then
  printf '\n== rabbitmq TLS SAN check ==\n'
  printf 'SKIP RabbitMQ TLS certificate check requested by DUNE_STANDBY_SKIP_RMQ_TLS_CHECK\n'
elif [[ -x ./scripts/check-rabbitmq-cert-sans.sh ]]; then
  printf '\n== rabbitmq TLS SAN check ==\n'
  if ./scripts/check-rabbitmq-cert-sans.sh "$env_file"; then
    printf 'OK RabbitMQ TLS certificate covers expected names\n'
  else
    printf 'WARN RabbitMQ TLS certificate SAN check failed\n'
    status_rc=1
  fi
fi

exit "$status_rc"
