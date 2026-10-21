#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: ./scripts/update-owned-steam-build-and-restart.sh [env-file] [options]

Interactively updates the Dune: Awakening Self-Hosted Server Steam tool with an
owned Steam account, loads the shipped Docker images, updates DUNE_IMAGE_TAG,
backs up state, and restarts the full farm through restart-target.sh.

Options:
  --login USER       Steam account name to use. Default: DUNE_OWNED_STEAM_LOGIN
                     or ksnape when DUNE_STEAM_LOGIN is anonymous.
  --no-restart      Stop after updating/loading images and writing .env.
  --restart-only-on-update
                    Do not restart when the Steam package already matches the
                    loaded Docker images.
  --non-interactive Run SteamCMD without a TTY. Requires cached credentials or
                    DUNE_STEAM_PASSWORD.
  --check-only      Validate host/env/script resolution and exit before SteamCMD.
  --yes             Do not ask for the final restart confirmation.
  -h, --help        Show this help.

This script intentionally targets Steam app 4754530, the self-hosted server
tool. It refuses to run on any host other than kspls0.
USAGE
}

die() {
  printf 'fail: %s\n' "$*" >&2
  exit 1
}

env_file=".env"
steam_login="${DUNE_OWNED_STEAM_LOGIN:-}"
restart=true
restart_only_on_update=false
non_interactive=false
assume_yes=false
check_only=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --login)
      steam_login="${2:?missing user after --login}"
      shift 2
      ;;
    --no-restart)
      restart=false
      shift
      ;;
    --restart-only-on-update)
      restart_only_on_update=true
      shift
      ;;
    --non-interactive)
      non_interactive=true
      shift
      ;;
    --check-only)
      check_only=true
      shift
      ;;
    --yes)
      assume_yes=true
      shift
      ;;
    --*)
      usage >&2
      die "unknown option: $1"
      ;;
    *)
      env_file="$1"
      shift
      ;;
  esac
done

script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

[[ -f "$env_file" ]] || die "env file not found: $env_file"
[[ "$(hostname)" == "kspls0" ]] || die "must run on kspls0; current host is $(hostname)"
if [[ "$check_only" != "true" && "$non_interactive" != "true" ]]; then
  [[ -t 0 && -t 1 ]] || die "interactive Steam login requires a TTY; run with ssh -t kspls0"
fi

env_value() {
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

app_id="$(env_value DUNE_STEAM_APP_ID)"
app_id="${app_id:-4754530}"
[[ "$app_id" == "4754530" ]] || die "refusing to update app $app_id; expected self-hosted server app 4754530"

steam_dir="$(env_value DUNE_STEAM_SERVER_DIR)"
[[ -n "$steam_dir" ]] || die "DUNE_STEAM_SERVER_DIR is empty in $env_file"
case "$steam_dir" in
  /*) ;;
  *) die "DUNE_STEAM_SERVER_DIR must be absolute: $steam_dir" ;;
esac

helper_image="$(env_value DUNE_RESTART_STEAMCMD_HELPER_IMAGE)"
helper_image="${helper_image:-cm2network/steamcmd:root}"
force_platform="$(env_value DUNE_STEAM_FORCE_PLATFORM)"
force_platform="${force_platform:-linux}"
configured_login="$(env_value DUNE_STEAM_LOGIN)"
if [[ -z "$steam_login" ]]; then
  if [[ -n "$configured_login" && "$configured_login" != "anonymous" ]]; then
    steam_login="$configured_login"
  else
    steam_login="ksnape"
  fi
fi
[[ "$steam_login" != "anonymous" ]] || die "anonymous SteamCMD no longer has access to app 4754530; use --login USER"

steamcmd_home="${DUNE_STEAMCMD_HOME:-$HOME/.steamcmd-dune}"
mkdir -p "$steamcmd_home" "$steam_dir"

printf 'host: %s\n' "$(hostname)"
printf 'env file: %s\n' "$env_file"
printf 'Steam app id: %s (Dune: Awakening Self-Hosted Server)\n' "$app_id"
printf 'Steam tool dir: %s\n' "$steam_dir"
printf 'SteamCMD home: %s\n' "$steamcmd_home"
printf 'Steam login: %s\n' "$steam_login"
printf 'Steam dependency app ids: 1070560 (scout), 1391110 (soldier)\n'

if ! docker image inspect "$helper_image" >/dev/null 2>&1; then
  if [[ "$check_only" == "true" ]]; then
    die "SteamCMD helper image is missing: $helper_image"
  fi
  printf 'pulling SteamCMD helper image: %s\n' "$helper_image"
  docker pull "$helper_image"
fi

if [[ "$check_only" == "true" ]]; then
  printf 'check-only OK\n'
  exit 0
fi

uid_gid="$(id -u):$(id -g)"
steam_password="${DUNE_STEAM_PASSWORD:-$(env_value DUNE_STEAM_PASSWORD)}"
password_file="${DUNE_STEAM_PASSWORD_FILE:-$(env_value DUNE_STEAM_PASSWORD_FILE)}"
if [[ -z "$steam_password" && -n "$password_file" ]]; then
  [[ -r "$password_file" ]] || die "DUNE_STEAM_PASSWORD_FILE is not readable: $password_file"
  steam_password="$(<"$password_file")"
  steam_password="${steam_password%$'\n'}"
fi
login_args=(+login "$steam_login")
if [[ -n "$steam_password" ]]; then
  login_args=(+login "$steam_login" "$steam_password")
  printf 'using DUNE_STEAM_PASSWORD\n'
fi

cat <<EOF

SteamCMD will prompt for the password and Steam Guard code if needed.
After login succeeds it will update Steam runtime dependencies first, then:
  app_update 4754530 validate

EOF

dependencies=(1070560 1391110)
content_log="$steamcmd_home/Steam/logs/content_log.txt"

has_dependency() {
  local needle="$1"
  local dep
  for dep in "${dependencies[@]}"; do
    [[ "$dep" == "$needle" ]] && return 0
  done
  return 1
}

run_steamcmd_update() {
  local update_args=()
  local docker_flags=(--rm)
  local dep
  if [[ "$non_interactive" != "true" ]]; then
    docker_flags+=(-it)
  fi
  if [[ -n "$password_file" ]]; then
    docker_flags+=( -v "$password_file:$password_file:ro" )
  fi
  for dep in "${dependencies[@]}"; do
    update_args+=(+app_update "$dep" validate)
  done
  update_args+=(+app_update "$app_id" validate)

  docker run "${docker_flags[@]}" \
    --network host \
    --user "$uid_gid" \
    -v "$steamcmd_home:$steamcmd_home" \
    -v "$steam_dir:$steam_dir" \
    -e "HOME=$steamcmd_home" \
    "$helper_image" \
    /home/steam/steamcmd/steamcmd.sh \
    "+@sSteamCmdForcePlatformType" "$force_platform" \
    +force_install_dir "$steam_dir" \
    "${login_args[@]}" \
    "${update_args[@]}" \
    +quit
}

steamcmd_rc=1
for attempt in 1 2 3 4 5; do
  printf '\nSteamCMD update attempt %s with dependencies: %s\n' "$attempt" "${dependencies[*]}"
  set +e
  run_steamcmd_update
  steamcmd_rc=$?
  set -e
  if [[ "$steamcmd_rc" -eq 0 ]]; then
    break
  fi

  mapfile -t missing_required < <(
    grep -Eo 'missing required app [0-9]+' "$content_log" 2>/dev/null |
      awk '{print $4}' |
      awk '!seen[$0]++'
  )
  added=false
  for missing_app in "${missing_required[@]}"; do
    if [[ "$missing_app" == "$app_id" ]]; then
      continue
    fi
    if ! has_dependency "$missing_app"; then
      printf 'SteamCMD reported missing required app %s; adding it and retrying\n' "$missing_app"
      dependencies+=("$missing_app")
      added=true
    fi
  done

  if [[ "$added" != "true" ]]; then
    die "SteamCMD update failed with rc=$steamcmd_rc and no new missing required app was found"
  fi
done

if [[ "$steamcmd_rc" -ne 0 ]]; then
  worker_enabled="${DUNE_STEAM_UPDATE_WORKER_ENABLED:-$(env_value DUNE_STEAM_UPDATE_WORKER_ENABLED)}"
  if [[ "$app_id" == "4754530" && "$worker_enabled" =~ ^(1|true|yes|on)$ && -x "$script_dir/steam-update-worker-fallback.sh" ]]; then
    "$script_dir/steam-update-worker-fallback.sh" "$env_file"
    steamcmd_rc=0
  else
    die "SteamCMD update failed after dependency retries"
  fi
fi

manifest="$steam_dir/steamapps/appmanifest_${app_id}.acf"
[[ -f "$manifest" ]] || die "Steam appmanifest missing after update: $manifest"
grep -q '"appid"[[:space:]]*"4754530"' "$manifest" || die "appmanifest is not app 4754530: $manifest"
grep -q '"name"[[:space:]]*"Dune: Awakening Self-Hosted Server"' "$manifest" || die "appmanifest is not the self-hosted server tool: $manifest"

check_file="$(mktemp)"
cleanup() {
  rm -f "$check_file"
}
trap cleanup EXIT

printf '\nchecking Steam package tag against .env\n'
set +e
./scripts/check-steam-update.sh "$env_file" 2>&1 | tee "$check_file"
check_rc="${PIPESTATUS[0]}"
set -e

updated=false
case "$check_rc" in
  0)
    printf 'package tag already matches DUNE_IMAGE_TAG\n'
    ;;
  1)
    if grep -q 'status: update available' "$check_file"; then
      printf '\nloading Funcom Docker images from Steam package\n'
      ./scripts/load-images.sh "$env_file"
      printf '\nwriting DUNE_IMAGE_TAG to %s\n' "$env_file"
      ./scripts/check-steam-update.sh "$env_file" --write-env
    elif grep -q 'status: same tag but Steam build changed' "$check_file"; then
      printf '\nreloading Funcom Docker images for changed Steam build under existing tag\n'
      ./scripts/load-images.sh "$env_file"
    else
      die "check-steam-update exited 1 without an update/reload status"
    fi
    updated=true
    ;;
  *)
    die "Steam package is still incomplete or unreadable; not loading images or restarting"
    ;;
esac

printf '\nverifying final Steam/package state\n'
./scripts/check-steam-update.sh "$env_file"

if [[ "$restart_only_on_update" == "true" && "$updated" != "true" ]]; then
  printf '\nverifying running containers use DUNE_IMAGE_TAG before skipping restart\n'
  set +e
  ./scripts/verify-running-image-tag.sh "$env_file"
  running_tag_rc=$?
  set -e
  case "$running_tag_rc" in
    0)
      printf '\nSteam package and running containers are current; restart skipped by --restart-only-on-update\n'
      exit 0
      ;;
    1)
      printf '\nrunning containers are stale; forcing convergence restart\n'
      updated=true
      ;;
    *)
      die "could not verify running container image convergence"
      ;;
  esac
fi

printf '\nensuring active database branch for current image\n'
./scripts/apply-official-db-patches.sh "$env_file"

COMPOSE_FILES="${COMPOSE_FILES:-$(./scripts/compose-files.sh "$env_file")}"
export COMPOSE_FILES

compose_cmd=(docker compose)
IFS=':' read -r -a compose_file_array <<< "$COMPOSE_FILES"
for compose_file in "${compose_file_array[@]}"; do
  [[ -n "$compose_file" ]] && compose_cmd+=(-f "$compose_file")
done
compose_cmd+=(--env-file "$env_file")

printf '\nvalidating compose config\n'
"${compose_cmd[@]}" config --quiet

printf '\nvalidating Landsraad/Coriolis guard before restart\n'
./scripts/validate-landsraad-coriolis-cycle.sh "$env_file"

printf '\nwriting pre-restart backup\n'
./scripts/backup-state.sh "$env_file"

printf '\nchecking logoff timer auto-remap against the new server binary (offline)\n'
remap_check_dir="$(mktemp -d)"
remap_check_tag="$(grep -E '^DUNE_IMAGE_TAG=' "$env_file" | cut -d= -f2- | head -n1)"
remap_check_cid="$(docker create "registry.funcom.com/funcom/self-hosting/seabass-server:$remap_check_tag" 2>/dev/null || true)"
if [[ -n "$remap_check_cid" ]] \
  && docker cp "$remap_check_cid:/home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping" "$remap_check_dir/server" >/dev/null 2>&1 \
  && ./scripts/test-logoff-timer-remap.sh "$remap_check_dir/server"; then
  :
else
  printf 'warning: offline logoff timer remap check failed or was skipped; the runtime patch may not apply after restart\n' >&2
fi
[[ -n "$remap_check_cid" ]] && docker rm "$remap_check_cid" >/dev/null 2>&1 || true
rm -rf "$remap_check_dir"

if [[ "$restart" != "true" ]]; then
  printf '\nimage update complete; restart skipped by --no-restart\n'
  exit 0
fi

if [[ "$assume_yes" != "true" ]]; then
  printf '\nAbout to restart the full live farm through scripts/restart-target.sh all.\n'
  read -r -p 'Type restart to continue: ' answer
  [[ "$answer" == "restart" ]] || die "restart cancelled"
fi

printf '\nrestarting full farm through restart-target.sh all\n'
ENV_FILE="$env_file" \
COMPOSE_FILES="$COMPOSE_FILES" \
DUNE_RESTART_CHECK_STEAM_UPDATE=false \
./scripts/restart-target.sh all

printf '\nvalidating Landsraad/Coriolis guard after restart\n'
./scripts/validate-landsraad-coriolis-cycle.sh "$env_file"

printf '\nchecking live farm status\n'
./scripts/status.sh "$env_file"

printf '\nchecking FLS publication health\n'
./scripts/fls-publication-health.py "$env_file" --compose-files "$COMPOSE_FILES"

printf '\nchecking logoff timer runtime patch state\n'
if ! ./scripts/patch-logoff-timers-runtime.sh --local --dry-run; then
  printf 'warning: logoff timer dry-run failed after auto-remap/validation; inspect the new build manually before applying (offline check: scripts/test-logoff-timer-remap.sh)\n' >&2
fi

printf '\nupdate and restart flow complete\n'
