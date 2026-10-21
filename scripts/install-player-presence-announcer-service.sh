#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-.env}"
run_user="${DUNE_PLAYER_PRESENCE_SERVICE_USER:-$(id -un)}"
service_name="dune-player-presence-announcer.service"
unit_dst="/etc/systemd/system/$service_name"
runtime_env_file="$root/config/secrets/player-presence.env"

if [[ ! -f "$root/$env_file" ]]; then
  printf 'env file not found: %s\n' "$root/$env_file" >&2
  exit 66
fi

python3 "$root/scripts/sync-player-presence-env.py" \
  --env-file "$env_file" \
  --output "$runtime_env_file"

tmp_unit="$(mktemp)"
trap 'rm -f "$tmp_unit"' EXIT

cat > "$tmp_unit" <<EOF
[Unit]
Description=Dune player join/leave announcer
Documentation=file://$root/docs/admin-bot.md
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=$run_user
WorkingDirectory=$root
EnvironmentFile=$runtime_env_file
Environment=DUNE_ADMIN_BOT_ENV_FILE=/dev/null
Environment=DUNE_PLAYER_PRESENCE_ENV_FILE=/dev/null
Environment=DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND=$root/scripts/announce.sh
Environment=DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_COMMAND=$root/scripts/announce.sh
Environment=DUNE_PLAYER_PRESENCE_COMPOSE_PROJECT=${DUNE_PLAYER_PRESENCE_COMPOSE_PROJECT:-dune_server}
ExecStart=$root/scripts/player-presence-announcer.py --loop
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "$tmp_unit" "$unit_dst"
sudo systemctl daemon-reload
sudo systemctl enable "$service_name"
if sudo systemctl is-active --quiet "$service_name"; then
  sudo systemctl restart "$service_name"
else
  sudo systemctl start "$service_name"
fi
sudo systemctl show "$service_name" --property=ActiveState --property=SubState --property=NRestarts
