#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${1:-.env}"
if [[ "$env_file" != /* ]]; then env_file="$repo_root/$env_file"; fi
reload_idle="${2:-}"
[[ -z "$reload_idle" || "$reload_idle" == --reload-idle ]] || { printf 'invalid reload option\n' >&2; exit 64; }
enable_recovery="${3:-}"
[[ -z "$enable_recovery" || ( "$reload_idle" == --reload-idle && "$enable_recovery" == --enable-recovery ) ]] || { printf 'invalid recovery activation option\n' >&2; exit 64; }
[[ $# -le 3 ]] || exit 64
service_user="${DUNE_SERVICE_USER:-$(stat -c %U "$repo_root")}"
service_group="${DUNE_SERVICE_GROUP:-$(stat -c %G "$repo_root")}"
service_template="$repo_root/config/systemd/dune-restart-api.service"
socket_template="$repo_root/config/systemd/dune-restart-api.socket"

if [[ ! -f "$env_file" || ! -x "$repo_root/scripts/dune_restart_api.py" ]]; then
  printf 'restart API prerequisites are missing\n' >&2
  exit 66
fi
if ! id -nG "$service_user" | tr ' ' '\n' | grep -qx docker; then
  printf 'restart API service user must belong to the docker group\n' >&2
  exit 77
fi

service_tmp="$(mktemp)"
socket_tmp="$(mktemp)"
trap 'rm -f "$service_tmp" "$socket_tmp"' EXIT
for template in "$service_template" "$socket_template"; do
  [[ -f "$template" ]] || { printf 'unit template is missing: %s\n' "$template" >&2; exit 66; }
done

sed \
  -e "s#DUNE_SERVICE_USER_PLACEHOLDER#$service_user#g" \
  -e "s#DUNE_SERVICE_GROUP_PLACEHOLDER#$service_group#g" \
  -e "s#DUNE_RESTART_API_ROOT_PLACEHOLDER#$repo_root#g" \
  -e "s#DUNE_RESTART_API_ENV_PLACEHOLDER#$env_file#g" \
  -e "s#DUNE_RESTART_API_HOST_PLACEHOLDER#$(hostname -s)#g" \
  "$service_template" > "$service_tmp"
sed \
  -e "s#DUNE_SERVICE_USER_PLACEHOLDER#$service_user#g" \
  -e "s#DUNE_SERVICE_GROUP_PLACEHOLDER#$service_group#g" \
  "$socket_template" > "$socket_tmp"

install_unit() {
  local src="$1" dst="$2"
  if [[ -w /etc/systemd/system ]]; then
    install -m 0644 "$src" "$dst"
  else
    sudo install -m 0644 "$src" "$dst"
  fi
}
install_unit "$service_tmp" /etc/systemd/system/dune-restart-api.service
install_unit "$socket_tmp" /etc/systemd/system/dune-restart-api.socket

if [[ "$(id -u)" -eq 0 ]]; then
  systemctl daemon-reload
  systemctl enable --now dune-restart-api.socket
else
  sudo systemctl daemon-reload
  sudo systemctl enable --now dune-restart-api.socket
fi
systemctl status dune-restart-api.socket --no-pager -n 3 2>/dev/null || sudo systemctl status dune-restart-api.socket --no-pager -n 3

if [[ "$reload_idle" == --reload-idle ]]; then
  python3 - <<'PYTHON'
import pathlib, subprocess
result = subprocess.run(["systemctl", "show", "dune-restart-api.service", "--property=MainPID", "--value"], capture_output=True, text=True, check=True)
pid = int(result.stdout.strip() or "0")
if pid:
    tasks = pathlib.Path(f"/proc/{pid}/task")
    if not tasks.is_dir():
        raise SystemExit("host API process changed during reload admission")
    for path in tasks.glob("*/children"):
        if path.read_text().strip():
            raise SystemExit("host API owns a native command; defer its reload")
PYTHON
  # Approval is a host-only operation. Never expose this through the socket API.
  sudo python3 - "$repo_root" "$env_file" <<'PYTHON'
import importlib.util, pathlib, socket, sys
root, env_file = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path[:0] = [str(root / "admin"), str(root / "scripts")]
import host_service_control
if pathlib.Path("/run/dune-restart-api/restart.sock").exists():
    health = host_service_control.request_socket("/run/dune-restart-api/restart.sock", "GET", "/v1/health", timeout=5)
    if health.get("busy") is not False:
        raise SystemExit("host recovery is busy or its idle state is unknown; defer approval")
spec = importlib.util.spec_from_file_location("host_restart_approval", root / "scripts/dune_restart_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
api.ENV_FILE = env_file
state = pathlib.Path("/var/lib/dune-restart-api")
for record in state.glob("*.json"):
    if host_service_control.JOB_PATTERN.fullmatch(record.stem):
        import json
        if json.loads(record.read_text()).get("state") not in {"completed", "failed", "blocked"}:
            raise SystemExit("unfinished host recovery requires review before approval")
result = host_service_control.approve_configuration(root, env_file, state, api.configured_map_services(), socket.gethostname().split(".")[0])
print("approved existing-container recovery configuration; protocol", result["protocol"])
PYTHON
  # Keep the live listening socket inode: Admin has a bind mount of that inode.
  if [[ -S /run/dune-restart-api/restart.sock && ! -L /run/dune-restart-api/restart.sock ]]; then
    sudo chgrp root /run/dune-restart-api/restart.sock
    sudo chmod 0660 /run/dune-restart-api/restart.sock
  fi
  sudo systemctl restart dune-restart-api.service
  sudo python3 - "$repo_root" "$env_file" "$enable_recovery" <<'PYTHON'
import importlib.util, pathlib, socket, sys
root, env_file = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path[:0] = [str(root / "admin"), str(root / "scripts")]
import host_service_control
result = host_service_control.health("/run/dune-restart-api/restart.sock")
if not result.get("qualified"):
    raise SystemExit("loaded host recovery protocol did not qualify")
print("loaded guarded host recovery protocol qualified without a lifecycle operation")
if sys.argv[3] == "--enable-recovery":
    spec = importlib.util.spec_from_file_location("host_recovery_activation", root / "scripts/dune_restart_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api.ENV_FILE = env_file
    host_service_control.enable_approved_recovery(root, env_file, pathlib.Path("/var/lib/dune-restart-api"), api.configured_map_services(), socket.gethostname().split(".")[0])
    result = host_service_control.health("/run/dune-restart-api/restart.sock")
    if not all(result.get(key) is True for key in ("qualified", "serviceControlReady", "statefulControlReady")):
        raise SystemExit("activated recovery controls did not qualify")
    print("host-approved recovery controls enabled without a lifecycle operation")
PYTHON
fi
