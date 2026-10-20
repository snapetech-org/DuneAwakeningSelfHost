#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

fake_runtime="$tmp_dir/fake-runtime"
status_file="$tmp_dir/status.txt"
env_file="$tmp_dir/test.env"
touch "$env_file"

cat > "$fake_runtime" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

status_file="$(printenv FAKE_STATUS_FILE)"

value_for() {
  awk -F= -v key="$1" '$1 == key {print $2; exit}' "$status_file"
}

case "$1" in
  ps)
    shift
    service=
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --filter)
          shift
          [[ "$1" == label=com.docker.compose.service=* ]] && service="${1##*=}"
          shift
          ;;
        *)
          shift
          ;;
      esac
    done
    [[ "$(value_for "$service")" == "running" ]] && printf 'fake-%s\n' "$service"
    exit 0
    ;;
  inspect)
    shift
    shift
    shift
    container="$1"
    if [[ "$container" == fake-* ]]; then
      service="$(printf '%s' "$container" | sed 's/^fake-//')"
      [[ "$(value_for "$service")" == "running" ]] && printf 'true\n' || printf 'false\n'
    elif [[ "$container" == dune_server-postgres-1 ]]; then
      [[ "$(value_for postgres)" == "running" ]] && printf 'true\n' || printf 'false\n'
    else
      printf 'false\n'
    fi
    exit 0
    ;;
  exec)
    container="$2"
    if [[ "$container" == dune_server-postgres-1 ]]; then
      [[ "$(value_for postgres)" == "running" ]]
      exit
    fi
    service="$(printf '%s' "$container" | sed 's/^fake-//')"
    key="$service"'_db'
    [[ "$(value_for "$key")" == "ok" ]]
    exit
    ;;
  *)
    printf 'unsupported fake runtime command: %s\n' "$*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$fake_runtime"

run_check() {
  set +e
  CONTAINER_RUNTIME="$fake_runtime" \
  FAKE_STATUS_FILE="$status_file" \
  COMPOSE_FILES=compose.yaml \
  COMPOSE_PROJECT_NAME=dune_server \
    "$repo_root/scripts/control-plane-health.sh" "$env_file" --quiet "$@" \
    >"$tmp_dir/stdout" 2>"$tmp_dir/stderr"
  last_rc=$?
  set -e
}

cat > "$status_file" <<'EOF'
postgres=running
survival=running
survival_db=ok
EOF
run_check
[[ "$last_rc" -eq 0 ]] || {
  printf 'healthy control-plane probe returned %s\n' "$last_rc" >&2
  cat "$tmp_dir/stderr" >&2
  exit 1
}

sed -i 's/^survival_db=.*/survival_db=failed/' "$status_file"
run_check
[[ "$last_rc" -eq 1 ]] || {
  printf 'unreachable map probe returned %s instead of 1\n' "$last_rc" >&2
  cat "$tmp_dir/stdout" >&2
  cat "$tmp_dir/stderr" >&2
  exit 1
}

sed -i '/^survival=/d;/^survival_db=/d' "$status_file"
run_check
[[ "$last_rc" -eq 2 ]] || {
  printf 'no-map probe returned %s instead of 2\n' "$last_rc" >&2
  exit 1
}

run_check --allow-no-map
[[ "$last_rc" -eq 0 ]] || {
  printf 'allow-no-map probe returned %s\n' "$last_rc" >&2
  exit 1
}

sed -i 's/^postgres=.*/postgres=stopped/' "$status_file"
run_check --allow-no-map
[[ "$last_rc" -eq 1 ]] || {
  printf 'dead Postgres with allow-no-map returned %s instead of 1\n' "$last_rc" >&2
  exit 1
}

printf 'control-plane health tests passed\n'
