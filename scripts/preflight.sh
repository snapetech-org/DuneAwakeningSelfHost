#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
failures=0

ok() {
  printf 'ok: %s\n' "$*"
}

warn() {
  printf 'warn: %s\n' "$*" >&2
}

fail() {
  printf 'fail: %s\n' "$*" >&2
  failures=$((failures + 1))
}

require_command() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1 found"
  else
    fail "$1 is required"
  fi
}

load_env() {
  if [[ ! -f "$env_file" ]]; then
    fail "$env_file does not exist; run ./scripts/populate-local-env.sh"
    return
  fi

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    if [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      printf -v "$key" '%s' "$value"
    fi
  done <"$env_file"

  ok "$env_file loaded"
}

check_env_value() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "$value" ]]; then
    fail "$name is empty"
  else
    ok "$name set"
  fi
}

check_not_default() {
  local name="$1"
  local default="$2"
  local value="${!name:-}"
  if [[ "$value" == "$default" ]]; then
    fail "$name still uses default placeholder"
  fi
}

check_world_identity() {
  case "${WORLD_UNIQUE_NAME:-}" in
    ""|sh-example-dune|example|changeme|change-me)
      fail "WORLD_UNIQUE_NAME still uses an example placeholder"
      ;;
    sh-example-*)
      warn "WORLD_UNIQUE_NAME still uses the generated sh-example-* prefix; keep it only if this is the durable world identity you intend to register"
      ;;
    *)
      ok "WORLD_UNIQUE_NAME is customized"
      ;;
  esac

  warn "After first successful FLS registration, do not rotate WORLD_UNIQUE_NAME; back up .env with state backups"
}

check_fls_environment() {
  local value="${DUNE_FLS_ENV:-retail}"
  case "$value" in
    retail)
      ok "DUNE_FLS_ENV=retail"
      ;;
    beta|test|ptc|staging)
      warn "DUNE_FLS_ENV=$value; use non-retail only with a matching PTC/test server build and token authorization"
      ;;
    *)
      warn "DUNE_FLS_ENV=$value is not a known DASH value; confirm the server build and Funcom token support it"
      ;;
  esac
}

check_server_browser_naming() {
  local detail_row="${DUNE_SERVER_DISPLAY_NAME:-}"
  local world_detail_row="${WORLD_NAME:-}"
  local gateway_registration_row="${PUBLIC_SERVER_NAME:-${WORLD_NAME:-}}"
  local gateway_ini="config/gateway.ini"
  local gateway_parent_row=""

  check_env_value DUNE_SERVER_DISPLAY_NAME

  if [[ -n "$detail_row" && -n "$world_detail_row" && "$detail_row" != "$world_detail_row" ]]; then
    fail "DUNE_SERVER_DISPLAY_NAME must match WORLD_NAME; both feed the server-browser nested/details row"
  fi

  if [[ ! -f "$gateway_ini" ]]; then
    fail "$gateway_ini does not exist"
    return
  fi

  gateway_parent_row="$(awk -F= '
    /^[[:space:]]*display_name[[:space:]]*=/ {
      value=$0
      sub(/^[^=]*=/, "", value)
      sub(/^[[:space:]]*/, "", value)
      sub(/[[:space:]]*$/, "", value)
      print value
    }
  ' "$gateway_ini" | tail -n 1)"

  if [[ -z "$gateway_parent_row" ]]; then
    fail "$gateway_ini [gateway].display_name is empty"
  elif [[ -n "$world_detail_row" && "$gateway_parent_row" != "$world_detail_row" ]]; then
    fail "$gateway_ini [gateway].display_name must match WORLD_NAME; all server-browser title fields must match"
  elif [[ -n "$world_detail_row" && "$gateway_registration_row" != "$world_detail_row" ]]; then
    fail "PUBLIC_SERVER_NAME must match WORLD_NAME; all server-browser title fields must match"
  else
    ok "server-browser title fields are in sync"
  fi
}

check_image_tarballs() {
  local server_dir="${DUNE_STEAM_SERVER_DIR:-}"
  local images=(
    "images/battlegroup/server-rabbitmq.tar"
    "images/battlegroup/server-text-router.tar"
    "images/battlegroup/server-bg-director.tar"
    "images/battlegroup/server-gateway.tar"
    "images/battlegroup/server-db-utils.tar"
    "images/battlegroup/server.tar"
    "images/prerequisites/igw-postgres.tar"
  )

  if [[ -z "$server_dir" ]]; then
    fail "DUNE_STEAM_SERVER_DIR is empty"
    return
  fi

  if [[ ! -d "$server_dir" ]]; then
    fail "DUNE_STEAM_SERVER_DIR does not exist: $server_dir"
    return
  fi

  ok "Steam server directory exists"

  for image in "${images[@]}"; do
    if [[ -f "$server_dir/$image" ]]; then
      ok "found $image"
    else
      fail "missing $server_dir/$image"
    fi
  done
}

check_rabbitmq_cert_sans() {
  if [[ -x ./scripts/check-rabbitmq-cert-sans.sh ]]; then
    if ./scripts/check-rabbitmq-cert-sans.sh "$env_file"; then
      ok "RabbitMQ TLS certificate SANs cover expected names"
    else
      warn "RabbitMQ TLS certificate SANs do not cover every expected name; see docs/setup.md before regenerating certs"
    fi
  fi
}

check_compose_bindings() {
  local rendered
  rendered="$(mktemp)"

  if docker compose --env-file "$env_file" config >"$rendered"; then
    ok "Compose config renders"
  else
    fail "Compose config failed to render"
    rm -f "$rendered"
    return
  fi

  if rg -n '0\.0\.0\.0:(15431|15672|15673|5673|31982):' "$rendered" >/dev/null; then
    fail "debug/database/RabbitMQ port appears bound on all interfaces"
  else
    ok "debug/database/RabbitMQ host ports are not bound on all interfaces"
  fi

  rm -f "$rendered"
}

require_command docker
require_command openssl
require_command rg
require_command jq

load_env

check_env_value DUNE_STEAM_SERVER_DIR
check_env_value DUNE_IMAGE_TAG
check_env_value WORLD_NAME
check_env_value WORLD_UNIQUE_NAME
check_env_value WORLD_REGION
check_env_value FLS_SECRET
check_env_value POSTGRES_SUPER_PASSWORD
check_env_value POSTGRES_DUNE_PASSWORD
check_env_value RMQ_HTTP_TOKEN_AUTH_SECRET
check_env_value DUNE_ADMIN_TOKEN
check_env_value EXTERNAL_ADDRESS

check_world_identity
check_fls_environment
check_server_browser_naming

check_not_default POSTGRES_SUPER_PASSWORD change-me-postgres-super
check_not_default POSTGRES_DUNE_PASSWORD change-me-dune-db
check_not_default RMQ_HTTP_TOKEN_AUTH_SECRET change-me-rmq-secret
check_not_default DUNE_ADMIN_TOKEN change-me-admin-token

if [[ "${EXTERNAL_ADDRESS:-}" == "127.0.0.1" ]]; then
  warn "EXTERNAL_ADDRESS is 127.0.0.1; clients outside this host will not be able to connect"
fi

check_image_tarballs

if [[ -x ./scripts/check-steam-update.sh ]]; then
  if ./scripts/check-steam-update.sh "$env_file" >/dev/null; then
    ok "DUNE_IMAGE_TAG matches Steam package"
  else
    warn "DUNE_IMAGE_TAG may not match Steam package; run ./scripts/check-steam-update.sh $env_file"
  fi
fi

check_compose_bindings
check_rabbitmq_cert_sans

if [[ -x ./scripts/check-admin-ingress.sh ]]; then
  if ./scripts/check-admin-ingress.sh "$env_file"; then
    ok "admin ingress probe passed"
  else
    fail "admin ingress probe failed"
  fi
fi

if [[ "$failures" -gt 0 ]]; then
  printf '\npreflight failed with %s issue(s)\n' "$failures" >&2
  exit 1
fi

printf '\npreflight passed\n'
