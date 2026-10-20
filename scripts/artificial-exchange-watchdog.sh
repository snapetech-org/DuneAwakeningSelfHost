#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$env_file" != /* ]]; then
  env_file="$repo_root/$env_file"
fi

if [[ ! -f "$env_file" ]]; then
  printf '{"ok":false,"error":"env file missing","path":"%s"}\n' "$env_file" >&2
  exit 1
fi

env_value() {
  local key="$1"
  local default="${2:-}"
  local line value
  line="$(grep -E "^${key}=" "$env_file" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    printf '%s' "$default"
    return
  fi
  value="${line#*=}"
  value="${value%$'\r'}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

json_bool() {
  case "${1,,}" in
    true|1|yes|on) printf true ;;
    *) printf false ;;
  esac
}

install_known_unit() {
  local label="$1"
  local unit="$2"
  case "$label:$unit" in
    buyer:dune-artificial-exchange-bot.service)
      "$repo_root/scripts/install-artificial-exchange-service.sh" "$env_file" "/etc/systemd/system/$unit" buyer >/dev/null 2>&1
      ;;
    populator:dune-artificial-exchange-populator.service)
      "$repo_root/scripts/install-artificial-exchange-service.sh" "$env_file" "/etc/systemd/system/$unit" populator >/dev/null 2>&1
      ;;
    *)
      return 1
      ;;
  esac
}

check_unit() {
  local label="$1"
  local unit="$2"
  local gate="$3"
  local dry_run="$4"
  local enabled active should_run started installed

  should_run="$(json_bool "$gate")"
  started=false
  installed=false
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  active="$(systemctl is-active "$unit" 2>/dev/null || true)"

  if [[ "$label" == buyer && "$should_run" == true && "$active" != active ]]; then
    if ! "$repo_root/scripts/artificial-exchange-bot.py" --check-ready >/dev/null 2>&1; then
      printf '{"label":"%s","unit":"%s","configured":true,"enabled":"%s","active":"%s","installed":false,"started":false,"readinessBlocked":true}' \
        "$label" "$unit" "$enabled" "$active"
      return 0
    fi
  fi

  if [[ "$should_run" == true && "$enabled" != enabled ]]; then
    if [[ "$(json_bool "$dry_run")" == false ]]; then
      install_known_unit "$label" "$unit" || true
    fi
    installed=true
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
  fi

  if [[ "$should_run" == true && "$enabled" == enabled && "$active" != active ]]; then
    if [[ "$(json_bool "$dry_run")" == false ]]; then
      systemctl start "$unit"
    fi
    started=true
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
  fi

  printf '{"label":"%s","unit":"%s","configured":%s,"enabled":"%s","active":"%s","installed":%s,"started":%s}' \
    "$label" "$unit" "$should_run" "$enabled" "$active" "$installed" "$started"
}

buyer_unit="$(env_value DUNE_ARTIFICIAL_EXCHANGE_WATCHDOG_BUYER_UNIT dune-artificial-exchange-bot.service)"
populator_unit="$(env_value DUNE_ARTIFICIAL_EXCHANGE_WATCHDOG_POPULATOR_UNIT dune-artificial-exchange-populator.service)"
watchdog_dry_run="$(env_value DUNE_ARTIFICIAL_EXCHANGE_WATCHDOG_DRY_RUN false)"
buyer_gate="$(env_value DUNE_ARTIFICIAL_EXCHANGE_ENABLED false)"
populator_gate="$(env_value DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED false)"

printf '{"ok":true,"dryRun":%s,"services":[' "$(json_bool "$watchdog_dry_run")"
check_unit buyer "$buyer_unit" "$buyer_gate" "$watchdog_dry_run"
printf ','
check_unit populator "$populator_unit" "$populator_gate" "$watchdog_dry_run"
printf ']}\n'
