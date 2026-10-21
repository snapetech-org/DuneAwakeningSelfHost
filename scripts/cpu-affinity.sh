#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
OVERLAY="$ROOT_DIR/compose.cpu-affinity.yaml"
COMMAND="status"
EXECUTE=false
CONFIRM=""
PRODUCTION_HOST="${DUNE_PRODUCTION_HOST:-kspls0}"
ALLOW_NON_PRODUCTION_HOST=false
PROJECT="${COMPOSE_PROJECT_NAME:-dune_server}"
BACKUP_ROOT="${DUNE_CPU_AFFINITY_BACKUP_ROOT:-$ROOT_DIR/backups/cpu-affinity}"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
PERSIST=false

usage() {
  cat <<'EOF'
Usage:
  scripts/cpu-affinity.sh [--env-file PATH] [--overlay PATH] status
  scripts/cpu-affinity.sh [--env-file PATH] [--overlay PATH] apply [--dry-run]
  scripts/cpu-affinity.sh [--env-file PATH] [--overlay PATH] apply --execute \
    --confirm 'APPLY DUNE CPU AFFINITY' [--persist]
  scripts/cpu-affinity.sh clear --execute --confirm 'CLEAR DUNE CPU AFFINITY' [--persist]

The Compose overlay controls newly created containers. This helper previews or
updates already-running project containers without restarting them. Execution
is hostname- and confirmation-gated and records before/target CPU sets under
backups/cpu-affinity/.
EOF
}

while (($#)); do
  case "$1" in
    status|apply|clear) COMMAND="$1" ;;
    --env-file) shift; ENV_FILE="${1:?--env-file requires a path}" ;;
    --overlay) shift; OVERLAY="${1:?--overlay requires a path}" ;;
    --project) shift; PROJECT="${1:?--project requires a name}" ;;
    --dry-run) EXECUTE=false ;;
    --execute) EXECUTE=true ;;
    --confirm) shift; CONFIRM="${1:?--confirm requires a phrase}" ;;
    --production-host) shift; PRODUCTION_HOST="${1:?--production-host requires a name}" ;;
    --allow-non-production-host) ALLOW_NON_PRODUCTION_HOST=true ;;
    --persist) PERSIST=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ -r "$OVERLAY" ]] || { echo "CPU affinity overlay not found: $OVERLAY" >&2; echo "Run scripts/generate-cpu-affinity.py first." >&2; exit 1; }
command -v "$CONTAINER_RUNTIME" >/dev/null 2>&1 || { echo "$CONTAINER_RUNTIME is required" >&2; exit 1; }

declare -A TARGETS=()
current_service=""
while IFS= read -r line; do
  if [[ "$line" =~ ^[[:space:]][[:space:]]([a-zA-Z0-9_.-]+):[[:space:]]*$ ]]; then
    current_service="${BASH_REMATCH[1]}"
  elif [[ -n "$current_service" && "$line" =~ ^[[:space:]]+cpuset:[[:space:]]+\"?([0-9,-]+)\"?[[:space:]]*$ ]]; then
    TARGETS[$current_service]="${BASH_REMATCH[1]}"
  fi
done <"$OVERLAY"
((${#TARGETS[@]})) || { echo "no service cpusets found in $OVERLAY" >&2; exit 1; }

mapfile -t CONTAINERS < <("$CONTAINER_RUNTIME" ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}}')
if ((${#CONTAINERS[@]} == 0)); then
  echo "No running containers found for Compose project '$PROJECT'."
  exit 0
fi

rows=()
missing_services=()
for container in "${CONTAINERS[@]}"; do
  [[ -n "$container" ]] || continue
  service="$("$CONTAINER_RUNTIME" inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$container")"
  name="$("$CONTAINER_RUNTIME" inspect -f '{{.Name}}' "$container" | sed 's#^/##')"
  current="$("$CONTAINER_RUNTIME" inspect -f '{{.HostConfig.CpusetCpus}}' "$container")"
  target="${TARGETS[$service]:-}"
  if [[ -z "$target" ]]; then
    missing_services+=("$name|$service")
    continue
  fi
  rows+=("$container|$name|$service|$current|$target")
done

if ((${#missing_services[@]})); then
  echo "Running Compose services are missing from $OVERLAY; refusing to continue:" >&2
  for row in "${missing_services[@]}"; do
    IFS='|' read -r name service <<<"$row"
    printf '  %s service=%s\n' "$name" "$service" >&2
  done
  exit 1
fi

printf 'container\tservice\tcurrent\ttarget\taction\n'
for row in "${rows[@]}"; do
  IFS='|' read -r container name service current target <<<"$row"
  action="unchanged"
  if [[ "$COMMAND" == "clear" ]]; then
    [[ -n "$current" ]] && action="clear"
  elif [[ "$current" != "$target" ]]; then
    action="apply"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$service" "${current:-all}" "$target" "$action"
done

[[ "$COMMAND" != "status" ]] || exit 0
if [[ "$EXECUTE" != true ]]; then
  echo "Dry-run only. No running container CPU set was changed."
  exit 0
fi

required_confirm="APPLY DUNE CPU AFFINITY"
[[ "$COMMAND" != "clear" ]] || required_confirm="CLEAR DUNE CPU AFFINITY"
[[ "$CONFIRM" == "$required_confirm" ]] || { echo "Execution requires --confirm '$required_confirm'" >&2; exit 2; }
current_host="$(hostname)"
if [[ "$ALLOW_NON_PRODUCTION_HOST" != true && "$current_host" != "$PRODUCTION_HOST" ]]; then
  echo "refusing CPU-affinity mutation on '$current_host'; expected '$PRODUCTION_HOST'" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$BACKUP_ROOT/$timestamp"
mkdir -p "$backup_dir"
printf 'container\tname\tservice\tbefore\ttarget\n' >"$backup_dir/container-cpusets.tsv"
for row in "${rows[@]}"; do
  IFS='|' read -r container name service current target <<<"$row"
  printf '%s\t%s\t%s\t%s\t%s\n' "$container" "$name" "$service" "$current" "$target" \
    >>"$backup_dir/container-cpusets.tsv"
done
cp "$OVERLAY" "$backup_dir/compose.cpu-affinity.yaml"
if [[ "$PERSIST" == true ]]; then
  [[ -r "$ENV_FILE" ]] || { echo "env file not found for --persist: $ENV_FILE" >&2; exit 1; }
  install -m 600 "$ENV_FILE" "$backup_dir/env-before"
fi

changed=0
for row in "${rows[@]}"; do
  IFS='|' read -r container name service current target <<<"$row"
  wanted="$target"
  [[ "$COMMAND" != "clear" ]] || wanted=""
  [[ "$current" != "$wanted" ]] || continue
  "$CONTAINER_RUNTIME" update --cpuset-cpus "$wanted" "$container" >/dev/null
  changed=$((changed + 1))
done

if [[ "$PERSIST" == true ]]; then
  enabled=true
  [[ "$COMMAND" != "clear" ]] || enabled=false
  python3 "$ROOT_DIR/scripts/update-env-file.py" "$ENV_FILE" --allow-protected-link --quiet \
    --set DUNE_CPU_AFFINITY_ENABLED "$enabled"
fi
printf 'created_utc=%s\nhostname=%s\nproject=%s\ncommand=%s\nchanged=%s\n' \
  "$timestamp" "$current_host" "$PROJECT" "$COMMAND" "$changed" >"$backup_dir/manifest.txt"
echo "$COMMAND complete: $changed container(s) updated without restart; persisted=$PERSIST; recovery record: $backup_dir"
