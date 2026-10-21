#!/usr/bin/env bash
set -euo pipefail

# These tests use a fake runtime and exercise map policy, not the live
# control-plane probe. Dedicated control-plane tests cover that path.
export DUNE_WATCH_CONTROL_PLANE_ENABLED=false

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

fake_runtime="$tmp_dir/fake-runtime"
cat > "$fake_runtime" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

log_file="${FAKE_RUNTIME_LOG:?}"
status_file="${FAKE_RUNTIME_STATUS_FILE:?}"
health_file="${FAKE_RUNTIME_HEALTH_FILE:-}"

printf '%s\n' "$*" >> "$log_file"

if [[ "${1:-}" == "compose" ]]; then
  shift
  service=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f|--env-file|--profile)
        shift 2
        ;;
      *)
        break
        ;;
    esac
  done
  while [[ $# -gt 0 ]]; do
    case "$1" in
      config)
        printf '%s\n' '{"services":{"survival":{"command":["-PartitionIndex=1"],"labels":{"dune.map.database-name":"Survival_1"}},"heighliner-dungeon":{"command":["/Game/DLC/B1C3/Maps/MainStory_Chapter3/FallenLight_HighlinerDungeon/Story_HeighlinerDungeon.Story_HeighlinerDungeon","-PartitionIndex=18"]}}}'
        exit 0
        ;;
      ps)
        shift
        if [[ "${1:-}" == "-q" || "${1:-}" == "-aq" ]]; then
          shift
          service="${1:-}"
          if grep -q "^${service}=" "$status_file"; then
            printf 'fake-%s\n' "$service"
          fi
          exit 0
        fi
        ;;
      exec)
        shift
        while [[ "${1:-}" == -* ]]; do
          shift
        done
        service="${1:-}"
        if [[ "$service" == "postgres" ]]; then
          partition_id="$(printf '%s\n' "$*" | sed -nE 's/.*where wp\.partition_id = ([0-9]+).*/\1/p' | head -1)"
          if [[ -n "$health_file" && -n "$partition_id" ]] && grep -q "^${partition_id}=" "$health_file"; then
            grep "^${partition_id}=" "$health_file" | tail -1 | cut -d= -f2
          else
            printf 't t t\n'
          fi
          exit 0
        fi
        ;;
      *)
        shift
        ;;
    esac
  done
fi

if [[ "${1:-}" == "inspect" ]]; then
  container="${@: -1}"
  service="${container#fake-}"
  grep "^${service}=" "$status_file" | tail -1 | cut -d= -f2
  exit 0
fi

printf 'unsupported fake-runtime command: %s\n' "$*" >&2
exit 1
EOF
chmod +x "$fake_runtime"

log_file="$tmp_dir/runtime.log"
status_file="$tmp_dir/status.txt"
env_file="$tmp_dir/test.env"
touch "$log_file" "$env_file"

cat > "$status_file" <<'EOF'
survival=running
heighliner-dungeon=exited
EOF

health_file="$tmp_dir/health.txt"
cat > "$health_file" <<'EOF'
1=t t t
18=f f f
EOF

status_output="$(
  FAKE_RUNTIME_LOG="$log_file" \
  FAKE_RUNTIME_STATUS_FILE="$status_file" \
  FAKE_RUNTIME_HEALTH_FILE="$health_file" \
  CONTAINER_RUNTIME="$fake_runtime" \
  COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
  DUNE_MAP_CATALOG_FILE="$tmp_dir/map-catalog.json" \
  "$repo_root/scripts/watch-maps.sh" "$env_file" --status
)"

if ! grep -q "survival .*status=running.*db=\"t t t\"" <<< "$status_output"; then
  printf 'status output did not report survival running\n' >&2
  exit 1
fi

if ! grep -q "heighliner-dungeon .*status=exited.*db=\"f f f\"" <<< "$status_output"; then
  printf 'status output did not report heighliner-dungeon exited\n' >&2
  exit 1
fi

dry_run_output="$(
  FAKE_RUNTIME_LOG="$log_file" \
  FAKE_RUNTIME_STATUS_FILE="$status_file" \
  FAKE_RUNTIME_HEALTH_FILE="$health_file" \
  CONTAINER_RUNTIME="$fake_runtime" \
  COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
  DUNE_MAP_CATALOG_FILE="$tmp_dir/map-catalog.json" \
  DUNE_WATCH_STARTUP_GRACE=0 \
  "$repo_root/scripts/watch-maps.sh" "$env_file" --dry-run
)"

if ! grep -q "would recover map: service=heighliner-dungeon partition=18 reason=exited" <<< "$dry_run_output"; then
  printf 'dry-run output did not report heighliner-dungeon recovery\n' >&2
  exit 1
fi

if grep -q "recover-map" "$log_file"; then
  printf 'dry-run attempted to invoke recovery\n' >&2
  exit 1
fi

cat > "$status_file" <<'EOF'
survival=running
heighliner-dungeon=running
EOF

cat > "$health_file" <<'EOF'
1=t t t
18=t t f
EOF

degraded_output="$(
  FAKE_RUNTIME_LOG="$log_file" \
  FAKE_RUNTIME_STATUS_FILE="$status_file" \
  FAKE_RUNTIME_HEALTH_FILE="$health_file" \
  CONTAINER_RUNTIME="$fake_runtime" \
  COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
  DUNE_MAP_CATALOG_FILE="$tmp_dir/map-catalog.json" \
  DUNE_WATCH_STARTUP_GRACE=0 \
  "$repo_root/scripts/watch-maps.sh" "$env_file" --dry-run
)"

if ! grep -q "would recover map: service=heighliner-dungeon partition=18 reason=not_active" <<< "$degraded_output"; then
  printf 'dry-run output did not report degraded heighliner-dungeon recovery\n' >&2
  exit 1
fi

recover_command="$tmp_dir/recover-map.sh"
cat > "$recover_command" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_RECOVERY_LOG:?}"
exit 42
EOF
chmod +x "$recover_command"
recovery_log="$tmp_dir/recovery.log"

failed_recovery_output="$(
  FAKE_RUNTIME_LOG="$log_file" \
  FAKE_RUNTIME_STATUS_FILE="$status_file" \
  FAKE_RUNTIME_HEALTH_FILE="$health_file" \
  FAKE_RECOVERY_LOG="$recovery_log" \
  CONTAINER_RUNTIME="$fake_runtime" \
  COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
  DUNE_MAP_CATALOG_FILE="$tmp_dir/map-catalog.json" \
  DUNE_WATCH_STARTUP_GRACE=0 \
  DUNE_WATCH_RECOVER_COMMAND="$recover_command" \
  "$repo_root/scripts/watch-maps.sh" "$env_file" --dry-run
)"

if grep -q "map recovery failed" <<< "$failed_recovery_output"; then
  printf 'dry-run should not invoke the recovery command\n' >&2
  exit 1
fi

set +e
FAKE_RUNTIME_LOG="$log_file" \
FAKE_RUNTIME_STATUS_FILE="$status_file" \
FAKE_RUNTIME_HEALTH_FILE="$health_file" \
FAKE_RECOVERY_LOG="$recovery_log" \
CONTAINER_RUNTIME="$fake_runtime" \
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
DUNE_WATCH_STARTUP_GRACE=0 \
DUNE_WATCH_RECOVER_COMMAND="$recover_command" \
  "$repo_root/scripts/watch-maps.sh" "$env_file" --once > "$tmp_dir/failed-once.out"
failed_once_rc=$?
set -e

if [[ "$failed_once_rc" -eq 0 ]]; then
  printf 'one-shot recovery failure should return non-zero\n' >&2
  exit 1
fi

if ! grep -q "map recovery failed: service=heighliner-dungeon partition=18 reason=not_active" "$tmp_dir/failed-once.out"; then
  printf 'one-shot recovery failure was not logged\n' >&2
  exit 1
fi

seed_command="$tmp_dir/seed-neighbors.sh"
cat > "$seed_command" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'seeded\n' >> "${FAKE_SEED_LOG:?}"
EOF
chmod +x "$seed_command"
seed_log="$tmp_dir/seed.log"

cat > "$status_file" <<'EOF'
survival=running
heighliner-dungeon=running
EOF

cat > "$health_file" <<'EOF'
1=t t t
18=t t t
EOF

FAKE_RUNTIME_LOG="$log_file" \
FAKE_RUNTIME_STATUS_FILE="$status_file" \
FAKE_RUNTIME_HEALTH_FILE="$health_file" \
FAKE_RECOVERY_LOG="$recovery_log" \
FAKE_SEED_LOG="$seed_log" \
CONTAINER_RUNTIME="$fake_runtime" \
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
DUNE_MAP_CATALOG_FILE="$tmp_dir/map-catalog.json" \
DUNE_WATCH_STARTUP_GRACE=0 \
DUNE_WATCH_RECOVER_COMMAND="$recover_command" \
DUNE_WATCH_SEED_NEIGHBORS=true \
DUNE_WATCH_SEED_COMMAND="$seed_command" \
  "$repo_root/scripts/watch-maps.sh" "$env_file" --once > "$tmp_dir/seeded-once.out" || true

if ! grep -q '^seeded$' "$seed_log"; then
  printf 'one-shot watchdog did not run neighbor seeding\n' >&2
  exit 1
fi

cat > "$seed_command" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'start\n' >> "${FAKE_SEED_LOG:?}"
sleep 5
printf 'done\n' >> "${FAKE_SEED_LOG:?}"
EOF
chmod +x "$seed_command"
: > "$seed_log"

timeout_output="$(
  FAKE_RUNTIME_LOG="$log_file" \
  FAKE_RUNTIME_STATUS_FILE="$status_file" \
  FAKE_RUNTIME_HEALTH_FILE="$health_file" \
  FAKE_RECOVERY_LOG="$recovery_log" \
  FAKE_SEED_LOG="$seed_log" \
  CONTAINER_RUNTIME="$fake_runtime" \
  COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
  DUNE_WATCH_STARTUP_GRACE=0 \
  DUNE_WATCH_RECOVER_COMMAND="$recover_command" \
  DUNE_WATCH_SEED_NEIGHBORS=true \
  DUNE_WATCH_SEED_COMMAND="$seed_command" \
  DUNE_WATCH_SEED_TIMEOUT=1 \
    "$repo_root/scripts/watch-maps.sh" "$env_file" --once
)"

if ! grep -q "neighbor seeding failed or timed out" <<< "$timeout_output"; then
  printf 'one-shot watchdog did not log neighbor seeding timeout\n' >&2
  exit 1
fi

if grep -q '^done$' "$seed_log"; then
  printf 'neighbor seeding timeout did not stop the seed command\n' >&2
  exit 1
fi

printf 'watch-maps tests passed\n'
