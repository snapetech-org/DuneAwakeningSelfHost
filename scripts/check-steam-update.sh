#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/check-steam-update.sh [env-file] [--write-env]

Compare DUNE_IMAGE_TAG with the image tags shipped in the local Steam-installed
Dune: Awakening Self-Hosted Server package. By default this is read-only.

Options:
  --write-env   Update DUNE_IMAGE_TAG in the env file when exactly one package
                server tag is found.
EOF
}

env_file=".env"
write_env=false

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    --write-env)
      write_env=true
      ;;
    *)
      env_file="$arg"
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

get_env() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub(/^[^=]*=/, "")
      gsub(/^["'\'']|["'\'']$/, "")
      print
      exit
    }
  ' "$env_file" 2>/dev/null
}

env_or_file() {
  local key="$1"
  local value="${!key:-}"
  if [[ -n "$value" ]]; then
    printf '%s\n' "$value"
  else
    get_env "$key"
  fi
}

steam_appmanifest() {
  local dir="$steam_dir"
  if [[ -f "$steam_dir/steamapps/appmanifest_${app_id}.acf" ]]; then
    printf '%s/steamapps/appmanifest_%s.acf\n' "$steam_dir" "$app_id"
    return 0
  fi
  if [[ "$steam_dir" == */steamapps/common/* ]]; then
    printf '%s/appmanifest_%s.acf\n' "${steam_dir%%/common/*}" "$app_id"
    return 0
  fi
  while [[ "$dir" != "/" && -n "$dir" ]]; do
    if [[ "$(basename "$dir")" == "steamapps" ]]; then
      printf '%s/appmanifest_%s.acf\n' "$dir" "$app_id"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
}

manifest_value() {
  local file="$1"
  local key="$2"
  awk -v key="$key" '$1 == "\"" key "\"" {gsub(/"/, "", $2); print $2; exit}' "$file" 2>/dev/null
}

image_tars=(
  "images/battlegroup/server-rabbitmq.tar"
  "images/battlegroup/server-text-router.tar"
  "images/battlegroup/server-bg-director.tar"
  "images/battlegroup/server-gateway.tar"
  "images/battlegroup/server-db-utils.tar"
  "images/battlegroup/server.tar"
)

if [[ ! -f "$env_file" ]]; then
  echo "fail: env file not found: $env_file" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "fail: jq is required" >&2
  exit 1
fi

steam_dir="$(env_or_file DUNE_STEAM_SERVER_DIR)"
current_tag="$(env_or_file DUNE_IMAGE_TAG)"
app_id="$(env_or_file DUNE_STEAM_APP_ID)"
app_id="${app_id:-4754530}"

if [[ -z "$steam_dir" ]]; then
  echo "fail: DUNE_STEAM_SERVER_DIR is empty" >&2
  exit 1
fi

if [[ ! -d "$steam_dir" ]]; then
  echo "fail: DUNE_STEAM_SERVER_DIR does not exist: $steam_dir" >&2
  exit 1
fi

tmp_tags="$(mktemp)"
trap 'rm -f "$tmp_tags"' EXIT

missing=0
for rel in "${image_tars[@]}"; do
  path="$steam_dir/$rel"
  if [[ ! -f "$path" ]]; then
    echo "warn: missing package image tar: $path" >&2
    missing=$((missing + 1))
    continue
  fi

  if ! tar -xOf "$path" manifest.json 2>/dev/null |
    jq -r '.[]?.RepoTags[]? | select(startswith("registry.funcom.com/funcom/self-hosting/seabass-server")) | split(":")[-1]' >>"$tmp_tags"; then
    echo "warn: could not read Docker manifest from $path" >&2
  fi
done

mapfile -t package_tags < <(sort -u "$tmp_tags")

appmanifest="$(steam_appmanifest || true)"
installed_buildid=""
target_buildid=""
loaded_buildid=""
loaded_buildid_file="$steam_dir/images/battlegroup/.loaded_buildid"
if [[ -n "$appmanifest" && -f "$appmanifest" ]]; then
  installed_buildid="$(manifest_value "$appmanifest" buildid)"
  target_buildid="$(manifest_value "$appmanifest" TargetBuildID)"
fi
if [[ -f "$loaded_buildid_file" ]]; then
  loaded_buildid="$(<"$loaded_buildid_file")"
fi

printf 'env file: %s\n' "$env_file"
printf 'Steam server dir: %s\n' "$steam_dir"
printf 'current DUNE_IMAGE_TAG: %s\n' "${current_tag:-unset}"
if [[ -n "$installed_buildid" ]]; then
  printf 'Steam installed buildid: %s\n' "$installed_buildid"
fi
if [[ -n "$target_buildid" ]]; then
  printf 'Steam target buildid: %s\n' "$target_buildid"
fi
if [[ -n "$loaded_buildid" ]]; then
  printf 'last loaded buildid: %s\n' "$loaded_buildid"
fi

if [[ "${#package_tags[@]}" -eq 0 ]]; then
  echo "package server tags: none found"
  echo "status: unable to determine package tag"
  exit 2
fi

printf 'package server tags:\n'
printf '  %s\n' "${package_tags[@]}"

if [[ "$missing" -gt 0 ]]; then
  echo "status: incomplete Steam package image set"
  exit 2
fi

if [[ "${#package_tags[@]}" -gt 1 ]]; then
  echo "status: multiple server tags found; update DUNE_IMAGE_TAG manually"
  exit 2
fi

package_tag="${package_tags[0]}"

if [[ -z "$installed_buildid" || -z "$target_buildid" ]]; then
  echo "status: unable to verify Steam's installed and target build IDs"
  exit 2
fi

if [[ -n "$installed_buildid" && -n "$target_buildid" && "$installed_buildid" != "$target_buildid" ]]; then
  echo "status: Steam package install incomplete"
  echo "installed buildid: $installed_buildid"
  echo "target buildid: $target_buildid"
  echo "rerun the Steam package update before loading images or restarting maps"
  exit 2
fi

if [[ "$current_tag" == "$package_tag" ]]; then
  if [[ -z "$loaded_buildid" || "$installed_buildid" != "$loaded_buildid" ]]; then
    echo "status: same tag but Steam build changed"
    echo "reload required: $package_tag buildid $installed_buildid (last loaded: ${loaded_buildid:-missing})"
    exit 1
  fi
  echo "status: current"
  exit 0
fi

current_build="${current_tag%%-*}"
package_build="${package_tag%%-*}"
if [[ "$current_build" =~ ^[0-9]+$ && "$package_build" =~ ^[0-9]+$ && "$package_build" -lt "$current_build" ]]; then
  echo "status: package older than current DUNE_IMAGE_TAG"
  echo "keeping current tag: $current_tag"
  exit 0
fi

echo "status: update available"
echo "next tag: $package_tag"
echo "next steps:"
echo "  ./scripts/load-images.sh $env_file"
echo "  ./scripts/check-steam-update.sh $env_file --write-env"
echo "  docker compose --env-file $env_file config --quiet"
echo "  ./scripts/backup-state.sh $env_file"

if [[ "$write_env" == true ]]; then
  python3 "$repo_root/scripts/update-env-file.py" "$env_file" --allow-protected-link --quiet \
    --set DUNE_IMAGE_TAG "$package_tag"
  echo "updated $env_file: DUNE_IMAGE_TAG=$package_tag"
  exit 0
fi

exit 1
