#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="${DASH_PANEL_ROOT:-/opt/dash/current}"
state_root="${DASH_PANEL_STATE_ROOT:-/var/lib/dash}"
env_file="${DASH_PANEL_ENV_FILE:-$state_root/.env}"
expected_hostname="${DASH_PANEL_EXPECTED_HOSTNAME:-}"
dry_run="${DASH_PANEL_COMMAND_DRY_RUN:-false}"
audit_file="${DASH_PANEL_AUDIT_FILE:-$state_root/panel-command-audit.log}"

usage() {
  cat <<'EOF'
Allowed commands:
  help
  status
  bootstrap-check
  backup
  farm-start
  farm-stop
  map-start SERVICE
  map-stop SERVICE
  map-restart SERVICE
EOF
}

if [[ -n "$expected_hostname" && "$(hostname)" != "$expected_hostname" ]]; then
  printf 'refusing command on hostname %s; expected %s\n' "$(hostname)" "$expected_hostname" >&2
  exit 77
fi
[[ -d "$repo_root" && -f "$env_file" ]] || {
  printf 'DASH release or environment is missing\n' >&2
  exit 1
}

if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  [[ "$SSH_ORIGINAL_COMMAND" =~ ^[a-z0-9-]+([[:space:]][a-z0-9-]+)?$ ]] || {
    printf 'command contains rejected characters\n' >&2
    exit 64
  }
  read -r -a argv <<< "$SSH_ORIGINAL_COMMAND"
else
  argv=("$@")
fi
(( ${#argv[@]} >= 1 && ${#argv[@]} <= 2 )) || { usage >&2; exit 64; }

map_service="${argv[1]:-}"
if [[ -n "$map_service" ]]; then
  map_catalog_script="$repo_root/scripts/map_catalog.py"
  map_compose_files="$("$repo_root/scripts/compose-files.sh" "$env_file")"
  all_map_services="$(
    COMPOSE_FILES="$map_compose_files" \
    DUNE_MAP_CATALOG_FILE="$repo_root/backups/admin-panel/map-catalog.json" \
      "$map_catalog_script" "$env_file" --field all-services --write-cache
  )"
  case " $all_map_services " in
    *" $map_service "*) ;;
    *) printf 'unknown map service: %s\n' "$map_service" >&2; exit 64 ;;
  esac
fi

command_name="${argv[0]}"
planned=()
case "$command_name" in
  help)
    (( ${#argv[@]} == 1 )) || { usage >&2; exit 64; }
    usage
    exit 0
    ;;
  status)
    (( ${#argv[@]} == 1 )) || { usage >&2; exit 64; }
    planned=("$repo_root/scripts/status.sh" "$env_file")
    ;;
  bootstrap-check)
    (( ${#argv[@]} == 1 )) || { usage >&2; exit 64; }
    planned=("$repo_root/scripts/bootstrap-checklist.sh" "$env_file")
    ;;
  backup)
    (( ${#argv[@]} == 1 )) || { usage >&2; exit 64; }
    planned=("$repo_root/scripts/backup-state.sh" "$env_file")
    ;;
  farm-start)
    (( ${#argv[@]} == 1 )) || { usage >&2; exit 64; }
    planned=("$repo_root/scripts/start-full-warm-pool.sh" "$env_file")
    ;;
  farm-stop)
    (( ${#argv[@]} == 1 )) || { usage >&2; exit 64; }
    planned=("$repo_root/scripts/stop-full-warm-pool.sh" "$env_file")
    ;;
  map-start|map-stop|map-restart)
    (( ${#argv[@]} == 2 )) || { usage >&2; exit 64; }
    planned=(env "ENV_FILE=$env_file" "DUNE_RESTART_SERVICES=$map_service")
    case "$command_name" in
      map-start) planned+=(DUNE_RESTART_ACTION=restart DUNE_RESTART_PHASE=start) ;;
      map-stop) planned+=(DUNE_RESTART_ACTION=shutdown DUNE_RESTART_PHASE=stop) ;;
      map-restart) planned+=(DUNE_RESTART_ACTION=restart DUNE_RESTART_PHASE=restart) ;;
    esac
    planned+=("$repo_root/scripts/restart-target.sh" "$map_service")
    ;;
  *) printf 'unknown command: %s\n' "$command_name" >&2; usage >&2; exit 64 ;;
esac

if [[ "$dry_run" =~ ^(1|true|yes|on)$ ]]; then
  printf 'dry-run:'
  printf ' %q' "${planned[@]}"
  printf '\n'
  exit 0
fi

mkdir -p -- "$(dirname -- "$audit_file")"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rc=0
trap 'rc=$?; printf "%s command=%s target=%s result=%s\n" "$started" "$command_name" "${map_service:-none}" "$rc" >> "$audit_file"; exit "$rc"' EXIT
cd "$repo_root"
"${planned[@]}"
