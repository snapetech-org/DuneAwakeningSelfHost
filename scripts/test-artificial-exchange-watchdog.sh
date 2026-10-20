#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

mkdir -p "$tmp_dir/bin"
env_file="$tmp_dir/watchdog.env"
started_file="$tmp_dir/systemctl-started"

cat > "$env_file" <<'EOF'
DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true
DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=false
DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=false
DUNE_ARTIFICIAL_EXCHANGE_WATCHDOG_DRY_RUN=false
EOF

cat > "$tmp_dir/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  is-enabled) printf 'enabled\n' ;;
  is-active) printf 'inactive\n' ;;
  start) printf 'started\n' > "$STARTED_FILE" ;;
  *) exit 2 ;;
esac
EOF

cat > "$tmp_dir/bin/python3" <<'EOF'
#!/usr/bin/env bash
[[ "${FAKE_READY:-false}" == true ]]
EOF

cat > "$tmp_dir/bin/grep" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  if [[ "$arg" == "${WATCHDOG_ENV_FILE:-}" ]]; then
    : > "$WATCHDOG_ENV_READ_FILE"
  fi
done
exec /usr/bin/grep "$@"
EOF

chmod +x "$tmp_dir/bin/systemctl" "$tmp_dir/bin/python3" "$tmp_dir/bin/grep"

blocked_output="$(PATH="$tmp_dir/bin:/usr/bin:/bin" STARTED_FILE="$started_file" FAKE_READY=false \
  WATCHDOG_ENV_FILE="$env_file" WATCHDOG_ENV_READ_FILE="$tmp_dir/env-read" \
  DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE=true \
  DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=false \
  DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=false DUNE_ARTIFICIAL_EXCHANGE_WATCHDOG_DRY_RUN=false \
  "$repo_root/scripts/artificial-exchange-watchdog.sh" "$env_file")"
if ! grep -q '"readinessBlocked":true' <<<"$blocked_output"; then
  printf 'watchdog did not report the failed readiness gate\n' >&2
  exit 1
fi
if [[ -e "$started_file" ]]; then
  printf 'watchdog started the buyer despite failed readiness\n' >&2
  exit 1
fi

ready_output="$(PATH="$tmp_dir/bin:/usr/bin:/bin" STARTED_FILE="$started_file" FAKE_READY=true \
  WATCHDOG_ENV_FILE="$env_file" WATCHDOG_ENV_READ_FILE="$tmp_dir/env-read" \
  DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE=true \
  DUNE_ARTIFICIAL_EXCHANGE_ENABLED=true DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN=false \
  DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED=false DUNE_ARTIFICIAL_EXCHANGE_WATCHDOG_DRY_RUN=false \
  "$repo_root/scripts/artificial-exchange-watchdog.sh" "$env_file")"
if ! grep -q '"started":true' <<<"$ready_output" || [[ ! -e "$started_file" ]]; then
  printf 'watchdog did not start the buyer after readiness passed\n' >&2
  exit 1
fi
if [[ -e "$tmp_dir/env-read" ]]; then
  printf 'watchdog reread the dotenv file despite inherited systemd environment\n' >&2
  exit 1
fi

printf 'artificial exchange watchdog tests passed\n'
