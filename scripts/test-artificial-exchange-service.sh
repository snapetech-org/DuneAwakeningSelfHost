#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

env_file="$tmp_dir/test.env"
unit_file="$tmp_dir/dune-artificial-exchange-bot.service"

cat > "$env_file" <<'EOF'
DUNE_ARTIFICIAL_EXCHANGE_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=true
EOF

"$repo_root/scripts/install-artificial-exchange-service.sh" "$env_file" "$unit_file" >/dev/null

if ! grep -q "^WorkingDirectory=$repo_root$" "$unit_file"; then
  printf 'rendered unit did not use checkout working directory\n' >&2
  exit 1
fi

if ! grep -q "^EnvironmentFile=$env_file$" "$unit_file"; then
  printf 'rendered unit did not use requested env file\n' >&2
  exit 1
fi

if ! grep -q "^ExecStart=$repo_root/scripts/artificial-exchange-bot.py --loop$" "$unit_file"; then
  printf 'rendered unit did not use checkout bot path\n' >&2
  exit 1
fi

if ! grep -q "^ExecStartPre=$repo_root/scripts/build-exchange-catalog.py$" "$unit_file"; then
  printf 'rendered unit did not build catalog before starting bot\n' >&2
  exit 1
fi

if grep -E '^Environment=DUNE_ARTIFICIAL_EXCHANGE_' "$unit_file" | grep -v '^Environment=DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE=true$' >/dev/null; then
  printf 'rendered unit hardcodes artificial Exchange gates instead of using env file\n' >&2
  exit 1
fi

if ! grep -q '^Environment=DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE=true$' "$unit_file"; then
  printf 'rendered unit does not avoid rereading the dotenv file\n' >&2
  exit 1
fi

if ! grep -q '^Environment=PYTHONUNBUFFERED=1$' "$unit_file"; then
  printf 'rendered unit does not force unbuffered Python logging\n' >&2
  exit 1
fi

if ! grep -q '^StartLimitIntervalSec=10min$' "$unit_file" || ! grep -q '^StartLimitBurst=3$' "$unit_file"; then
  printf 'rendered buyer unit does not cap repeated start failures\n' >&2
  exit 1
fi

if ! grep -q '^RestartSec=60s$' "$unit_file"; then
  printf 'rendered buyer unit does not back off after failure\n' >&2
  exit 1
fi

"$repo_root/scripts/install-artificial-exchange-service.sh" "$env_file" "$unit_file" populator >/dev/null
if ! grep -q "^ExecStart=$repo_root/scripts/artificial-exchange-bot.py --populate-loop --expire-seeded$" "$unit_file"; then
  printf 'rendered populator unit did not use populate loop with seeded cleanup\n' >&2
  exit 1
fi

if ! "$repo_root/scripts/install-artificial-exchange-service.sh" "$env_file" "$unit_file" both >/dev/null; then
  printf 'installer should support combined mode\n' >&2
  exit 1
fi
if ! grep -q "^ExecStart=$repo_root/scripts/artificial-exchange-bot.py --loop --populate-loop --expire-seeded$" "$unit_file"; then
  printf 'rendered combined unit did not use buyer and populate loops with seeded cleanup\n' >&2
  exit 1
fi

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$unit_file"
fi

watchdog_service="$tmp_dir/dune-artificial-exchange-watchdog.service"
watchdog_timer="$tmp_dir/dune-artificial-exchange-watchdog.timer"
"$repo_root/scripts/install-artificial-exchange-watchdog-timer.sh" "$env_file" "$watchdog_service" "$watchdog_timer" >/dev/null

if ! grep -q "^WorkingDirectory=$repo_root$" "$watchdog_service"; then
  printf 'rendered watchdog service did not use checkout working directory\n' >&2
  exit 1
fi

if ! grep -q "^EnvironmentFile=$env_file$" "$watchdog_service"; then
  printf 'rendered watchdog service did not use requested env file\n' >&2
  exit 1
fi

if ! grep -q '^Environment=DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE=true$' "$watchdog_service"; then
  printf 'rendered watchdog service does not avoid rereading the dotenv file\n' >&2
  exit 1
fi

if ! grep -q "^ExecStart=$repo_root/scripts/artificial-exchange-watchdog.sh $env_file$" "$watchdog_service"; then
  printf 'rendered watchdog service did not use checkout watchdog path\n' >&2
  exit 1
fi

if ! grep -q '^OnUnitInactiveSec=1min$' "$watchdog_timer"; then
  printf 'rendered watchdog timer does not run every minute\n' >&2
  exit 1
fi

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$watchdog_service" "$watchdog_timer"
fi

printf 'artificial exchange service tests passed\n'
