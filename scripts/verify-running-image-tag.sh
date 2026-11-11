#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file=".env"
versions_only=false
for arg in "$@"; do
  case "$arg" in
    --versions-only) versions_only=true ;;
    -h|--help)
      printf 'Usage: %s [env-file] [--versions-only]\n' "$0"
      exit 0
      ;;
    *) env_file="$arg" ;;
  esac
done

if [[ ! -f "$env_file" ]]; then
  printf 'fail: env file not found: %s\n' "$env_file" >&2
  exit 2
fi

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "")
      gsub(/^["'"'"']|["'"'"']$/, "")
      print
      exit
    }
  ' "$env_file" 2>/dev/null
}

expected_tag="$(env_value DUNE_IMAGE_TAG)"
project="$(env_value COMPOSE_PROJECT_NAME)"
project="${project:-dune_server}"

if [[ -z "$expected_tag" ]]; then
  printf 'fail: DUNE_IMAGE_TAG is empty in %s\n' "$env_file" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  printf 'fail: docker is required to verify running image tags\n' >&2
  exit 2
fi

# A current running worker is not enough to skip recovery: the maintenance
# path also needs the full current image set locally so the watchdog can
# recreate a worker without reaching the upstream registry.
missing_images=0
for repo in \
  seabass-server-rabbitmq \
  seabass-server-text-router \
  seabass-server-bg-director \
  seabass-server-gateway \
  seabass-server-db-utils \
  seabass-server
do
  image="registry.funcom.com/funcom/self-hosting/${repo}:${expected_tag}"
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    printf 'official Dune image missing locally: %s\n' "$image"
    missing_images=1
  fi
done
if [[ "$missing_images" -ne 0 ]]; then
  printf 'running image convergence: missing official images expected_tag=%s\n' "$expected_tag"
  exit 1
fi

game_override="${DUNE_GAME_IMAGE-$(env_value DUNE_GAME_IMAGE)}"
runtime_image_id=""
if [[ -n "$game_override" ]]; then
  runtime_check="$(python3 "$script_dir/runtime-security.py" "$env_file" --expected-tag "$expected_tag")"
  runtime_image_id="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("imageId", ""))' "$runtime_check")"
fi

# Control-plane overlays are qualified for one vendor build and one immutable
# local image ID. A later Steam build must not inherit an old overlay.
declare -A control_image control_id
for service in director text-router gateway; do
  if [[ "$service" == director ]]; then
    prefix=DUNE_DIRECTOR
  elif [[ "$service" == text-router ]]; then
    prefix=DUNE_TEXT_ROUTER
  else
    prefix=DUNE_GATEWAY
  fi
  image_key="${prefix}_IMAGE"
  id_key="${prefix}_IMAGE_ID"
  vendor_key="${prefix}_VENDOR_TAG"
  override="${!image_key:-$(env_value "$image_key")}"
  pinned_id="${!id_key:-$(env_value "$id_key")}"
  vendor_tag="${!vendor_key:-$(env_value "$vendor_key")}"
  if [[ -z "$override$pinned_id$vendor_tag" ]]; then
    continue
  fi
  if [[ -z "$override" || ! "$pinned_id" =~ ^sha256:[0-9a-f]{64}$ || "$vendor_tag" != "$expected_tag" ]]; then
    printf 'invalid %s security image pin for vendor tag %s\n' "$service" "$expected_tag" >&2
    exit 2
  fi
  if [[ "$service" == director && "$override" != snape/dune-bg-director:* ]] \
    || [[ "$service" == text-router && "$override" != snape/dune-text-router:* ]] \
    || [[ "$service" == gateway && "$override" != snape/dune-gateway:* ]]; then
    printf 'unapproved %s security image reference\n' "$service" >&2
    exit 2
  fi
  loaded_id="$(docker image inspect --format '{{.Id}}' "$override" 2>/dev/null || true)"
  if [[ "$loaded_id" != "$pinned_id" ]]; then
    printf '%s security image is missing or differs from its pinned ID\n' "$service" >&2
    exit 1
  fi
  control_image["$service"]="$override"
  control_id["$service"]="$pinned_id"
done

checked=0
mismatches=0
while IFS=$'\t' read -r container_id service; do
  [[ -n "$container_id" ]] || continue
  image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
  if [[ "$service" == director || "$service" == text-router || "$service" == gateway ]]; then
    if [[ -n "${control_image[$service]:-}" ]]; then
      checked=$((checked + 1))
      actual_id="$(docker inspect --format '{{.Image}}' "$container_id")"
      if [[ "$image" != "${control_image[$service]}" || "$actual_id" != "${control_id[$service]}" ]]; then
        printf 'control-plane security image mismatch: service=%s\n' "$service" >&2
        mismatches=$((mismatches + 1))
      fi
      continue
    fi
    if [[ "$image" == snape/dune-* ]]; then
      printf 'unapproved control-plane security image: service=%s\n' "$service" >&2
      checked=$((checked + 1))
      mismatches=$((mismatches + 1))
      continue
    fi
  fi
  if [[ -n "$runtime_image_id" && "$image" == "$game_override" ]]; then
    checked=$((checked + 1))
    actual_id="$(docker inspect --format '{{.Image}}' "$container_id")"
    if [[ "$actual_id" != "$runtime_image_id" ]]; then
      printf 'runtime security image mismatch: service=%s\n' "$service" >&2
      mismatches=$((mismatches + 1))
    fi
    continue
  fi
  repository="${image%:*}"
  tag="${image##*:}"

  case "$repository:$service" in
    snape/dune-server:*)
      printf 'unapproved runtime security image: service=%s\n' "$service" >&2
      checked=$((checked + 1))
      mismatches=$((mismatches + 1))
      continue
      ;;
    registry.funcom.com/funcom/self-hosting/seabass-server:*) ;;
    registry.funcom.com/funcom/self-hosting/seabass-server-bg-director:director) ;;
    registry.funcom.com/funcom/self-hosting/seabass-server-gateway:gateway) ;;
    registry.funcom.com/funcom/self-hosting/seabass-server-text-router:text-router) ;;
    registry.funcom.com/funcom/self-hosting/seabass-server-db-utils:rmq-auth-shim) ;;
    *) continue ;;
  esac

  checked=$((checked + 1))
  if [[ "$tag" != "$expected_tag" ]]; then
    printf 'running image mismatch: service=%s container=%s running=%s expected_tag=%s\n' \
      "$service" "$container_id" "$image" "$expected_tag"
    mismatches=$((mismatches + 1))
  fi
done < <(
  docker ps \
    --filter "label=com.docker.compose.project=$project" \
    --format '{{.ID}}\t{{.Label "com.docker.compose.service"}}'
)

if [[ "$checked" -eq 0 ]]; then
  printf 'running image mismatch: no live map/control-plane containers found for project=%s\n' "$project"
  exit 1
fi
if [[ "$mismatches" -ne 0 ]]; then
  printf 'running image convergence: mismatch checked=%s mismatches=%s expected_tag=%s\n' \
    "$checked" "$mismatches" "$expected_tag"
  exit 1
fi

build_check_output=""
set +e
build_check_output="$("$script_dir/check-steam-update.sh" "$env_file" 2>&1)"
build_check_rc=$?
set -e
if [[ "$build_check_rc" -ne 0 ]]; then
  printf '%s\n' "$build_check_output" >&2
  printf 'running image convergence: Steam build check failed; the running farm may not use the latest server build\n' >&2
  exit "$build_check_rc"
fi
printf 'latest Steam build check: passed\n'

if [[ "$versions_only" == true ]]; then
  printf 'running image convergence: current checked=%s expected_tag=%s\n' "$checked" "$expected_tag"
  exit 0
fi

# Require the always-on core partitions to be ready and connected before
# treating the farm as current. A partially running farm can have one matching
# image and still be offline after maintenance.
if ! status_output="$("$script_dir/status.sh" "$env_file" --public-summary 2>&1)"; then
  printf 'running image convergence: core readiness probe failed; restart required\n'
  exit 1
fi

core_line="$(printf '%s\n' "$status_output" | sed -nE \
  's/^core_ready_alive=([0-9]+) core_alive_active=([0-9]+) core_active_servers=([0-9]+) core_partitions=([0-9]+)$/\1 \2 \3 \4/p' | tail -1)"
game_connections="$(printf '%s\n' "$status_output" | sed -nE \
  's/^current_ready_alive=.* game_sg_connections=([0-9]+) admin_sg_connections=.*$/\1/p' | tail -1)"
core_ready=0
core_alive=0
core_active=0
core_total=0
if [[ -n "$core_line" ]]; then
  read -r core_ready core_alive core_active core_total <<< "$core_line"
fi
game_connections="${game_connections:-0}"
if [[ ! "$core_ready" =~ ^[0-9]+$ || ! "$core_alive" =~ ^[0-9]+$ \
    || ! "$core_active" =~ ^[0-9]+$ || ! "$core_total" =~ ^[0-9]+$ \
    || ! "$game_connections" =~ ^[0-9]+$ || "$core_total" -eq 0 \
    || "$core_ready" -ne "$core_total" || "$core_alive" -ne "$core_total" \
    || "$core_active" -ne "$core_total" || "$game_connections" -lt "$core_total" ]]; then
  printf 'running image convergence: required core readiness incomplete ready=%s alive=%s active=%s total=%s game_sg=%s\n' \
    "$core_ready" "$core_alive" "$core_active" "$core_total" "$game_connections"
  exit 1
fi

printf 'running image convergence: current checked=%s expected_tag=%s core_ready=%s/%s game_sg=%s\n' \
  "$checked" "$expected_tag" "$core_ready" "$core_total" "$game_connections"
