#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
compose_cmd="${COMPOSE:-docker compose}"

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

ini_value() {
  local file="$1" section="$2" key="$3"
  awk -F= -v section="$section" -v key="$key" '
    /^[[:space:]]*\[/ {
      current=$0
      gsub(/^[[:space:]]*\[/, "", current)
      gsub(/\][[:space:]]*$/, "", current)
      next
    }
    current == section && $1 ~ "^[[:space:]]*" key "[[:space:]]*$" {
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      print
      found=1
    }
    END { if (!found) exit 0 }
  ' "$file" 2>/dev/null | tail -1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

redact_sensitive() {
  sed -E \
    -e 's/(ServiceAuthToken=)[^",[:space:]]+/\1<redacted>/g' \
    -e 's/(ServerCommandsAuthToken=)[^",[:space:]]*/\1<redacted>/g' \
    -e 's/(DatabasePassword=)[^",[:space:]]+/\1<redacted>/g' \
    -e 's/(GameRmqSecret["=: ][ ":]*)[^",[:space:]}]+/\1<redacted>/g' \
    -e 's/(RMQ_HTTP_TOKEN_AUTH_SECRET=)[^",[:space:]]+/\1<redacted>/g' \
    -e 's/(FLS_SECRET[=])[^",[:space:]]+/\1<redacted>/g' \
    -e 's/(FuncomLiveServices__ServiceAuthToken=)[^",[:space:]]+/\1<redacted>/g' \
    -e 's/(RMQ_HTTP_TOKEN_AUTH_SECRET: )[[:alnum:]_.-]+/\1<redacted>/g' \
    -e 's/(FuncomLiveServices__ServiceAuthToken: )[[:alnum:]_.-]+/\1<redacted>/g'
}

section() {
  printf '\n== %s ==\n' "$1"
}

if [[ ! -f "$env_file" ]]; then
  printf 'env file not found: %s\n' "$env_file" >&2
  exit 1
fi

world_unique_name="$(read_env WORLD_UNIQUE_NAME)"
world_name="$(read_env WORLD_NAME)"
world_region="$(read_env WORLD_REGION)"
world_datacenter_id="$(read_env WORLD_DATACENTER_ID)"
external_address="$(read_env EXTERNAL_ADDRESS)"
game_rmq_public_host="$(read_env GAME_RMQ_PUBLIC_HOST)"
game_rmq_public_host="${game_rmq_public_host:-$external_address}"
game_rmq_public_port="$(read_env GAME_RMQ_PUBLIC_PORT)"
game_rmq_public_port="${game_rmq_public_port:-31982}"
game_rmq_public_http_port="$(read_env GAME_RMQ_PUBLIC_HTTP_PORT)"
game_rmq_public_http_port="${game_rmq_public_http_port:-15673}"
game_rmq_http_bind_address="$(read_env GAME_RMQ_HTTP_BIND_ADDRESS)"
game_rmq_http_bind_address="${game_rmq_http_bind_address:-127.0.0.1}"
dune_fls_env="$(read_env DUNE_FLS_ENV)"
dune_database="$(read_env DUNE_GAME_DB_NAME)"
dune_database="${dune_database:-$(read_env DUNE_DB_NAME)}"
dune_database="${DUNE_BROWSER_PING_DATABASE:-$dune_database}"
dune_database="${dune_database:-dune_sb_1_4_0_0}"
game_udp_range="$(read_env GAME_UDP_PORT_RANGE)"
game_udp_range="${game_udp_range:-7777:7871}"
igw_udp_range="$(read_env IGW_UDP_PORT_RANGE)"
igw_udp_range="${igw_udp_range:-7888:7982}"

section "env public identity"
printf 'WORLD_UNIQUE_NAME=%s\n' "${world_unique_name:-unset}"
printf 'WORLD_NAME=%s\n' "${world_name:-unset}"
printf 'WORLD_REGION=%s\n' "${world_region:-unset}"
printf 'WORLD_DATACENTER_ID=%s\n' "${world_datacenter_id:-unset}"
printf 'EXTERNAL_ADDRESS=%s\n' "${external_address:-unset}"
printf 'GAME_RMQ_PUBLIC_HOST=%s\n' "${game_rmq_public_host:-unset}"
printf 'GAME_RMQ_PUBLIC_PORT=%s\n' "$game_rmq_public_port"
printf 'GAME_RMQ_PUBLIC_HTTP_PORT=%s\n' "$game_rmq_public_http_port"
printf 'GAME_RMQ_HTTP_BIND_ADDRESS=%s\n' "$game_rmq_http_bind_address"
printf 'DUNE_FLS_ENV=%s\n' "${dune_fls_env:-unset}"
printf 'DUNE_GAME_DATABASE=%s\n' "$dune_database"
printf 'GAME_UDP_PORT_RANGE=%s\n' "$game_udp_range"
printf 'IGW_UDP_PORT_RANGE=%s\n' "$igw_udp_range"

section "gateway.ini identity"
if [[ -f config/gateway.ini ]]; then
  printf 'OnlineSubsystem.ServerName=%s\n' "$(ini_value config/gateway.ini OnlineSubsystem ServerName || true)"
  printf 'OnlineSubsystem.DatacenterId=%s\n' "$(ini_value config/gateway.ini OnlineSubsystem DatacenterId || true)"
  printf 'gateway.display_name=%s\n' "$(ini_value config/gateway.ini gateway display_name || true)"
else
  printf 'WARN missing config/gateway.ini\n'
fi

section "rendered compose signals"
if have docker; then
  if $compose_cmd --env-file "$env_file" config >/tmp/dune-browser-ping-compose.$$ 2>/tmp/dune-browser-ping-compose.err.$$; then
    rg -n 'RMQGameHostname|RMQGamePort|RMQGameHttpPort|EXTERNAL_ADDRESS|ExternalAddress|HOST_DATACENTER|OPT_SERVERNAME|OPT_DISPLAY_NAME|BATTLEGROUP|GAME_RMQ|7777|7888|31982|15673' /tmp/dune-browser-ping-compose.$$ || true
  else
    printf 'WARN docker compose config failed:\n'
    sed -n '1,80p' /tmp/dune-browser-ping-compose.err.$$
  fi
  rm -f /tmp/dune-browser-ping-compose.$$ /tmp/dune-browser-ping-compose.err.$$
else
  printf 'WARN docker not found\n'
fi

section "running container reality"
if have docker && have jq; then
  ids=()
  for service in gateway director text-router game-rmq survival; do
    id="$($compose_cmd --env-file "$env_file" ps -q "$service" 2>/dev/null || true)"
    [[ -n "$id" ]] && ids+=("$id")
  done
  if ((${#ids[@]})); then
    docker inspect "${ids[@]}" 2>/dev/null | jq -r '
      .[] |
      "\n## " + .Name,
      ((.Config.Env // [])[]? | select(test("WORLD_|OPT_|BATTLEGROUP|HOST_DATACENTER|FuncomLiveServices|RMQ|GAME_RMQ|EXTERNAL|FLS"))),
      "cmd=" + ((.Config.Cmd // []) | tostring)
    ' | redact_sensitive
  else
    printf 'WARN no target containers found through compose ps\n'
  fi
else
  printf 'WARN docker and jq are required for running-container inspection\n'
fi

section "rabbitmq certificate"
if [[ -x scripts/check-rabbitmq-cert-sans.sh ]]; then
  scripts/check-rabbitmq-cert-sans.sh "$env_file" || true
else
  printf 'WARN scripts/check-rabbitmq-cert-sans.sh not executable\n'
fi

section "local listeners"
if have ss; then
  printf '%s\n' "-- tcp ${game_rmq_public_port} --"
  ss -ltnup 2>/dev/null | rg "(:|\\*)${game_rmq_public_port}\\b" || true
  printf '%s\n' "-- tcp ${game_rmq_public_http_port} --"
  ss -ltnup 2>/dev/null | rg "(:|\\*)${game_rmq_public_http_port}\\b" || true
  printf '%s\n' "-- udp game/igw ranges --"
  ss -lunp 2>/dev/null | awk -v game="$game_udp_range" -v igw="$igw_udp_range" '
    BEGIN {
      split(game, g, ":"); split(igw, i, ":")
      gs=g[1]+0; ge=g[2]+0; is=i[1]+0; ie=i[2]+0
    }
    {
      port=$5
      sub(/^.*:/, "", port)
      p=port+0
      if ((p >= gs && p <= ge) || (p >= is && p <= ie)) print
    }'
else
  printf 'WARN ss not found\n'
fi

section "icmp browser-ping path"
if have ping; then
  if [[ -n "$game_rmq_public_host" ]]; then
    ping -c "${DUNE_BROWSER_PING_ICMP_COUNT:-3}" -W "${DUNE_BROWSER_PING_ICMP_TIMEOUT:-2}" "$game_rmq_public_host" 2>&1 \
      | awk -v host="$game_rmq_public_host" '
          /packets transmitted/ { summary=$0 }
          /^rtt / { rtt=$0 }
          END {
            if (summary ~ / 0% packet loss/) {
              printf "OK public host ICMP %s reachable", host
              if (rtt != "") printf " (%s)", rtt
              printf "\n"
            } else if (summary != "") {
              printf "WARN public host ICMP %s did not cleanly respond: %s\n", host, summary
            } else {
              printf "WARN public host ICMP %s produced no summary\n", host
            }
          }'
  fi
else
  printf 'WARN ping command not found; ICMP browser-ping path not checked\n'
fi
router="${DUNE_FAILOVER_ROUTER_SSH:-${DUNE_ROUTER_SSH:-$(read_env DUNE_FAILOVER_ROUTER_SSH)}}"
if [[ -n "$router" ]] && have ssh; then
  ssh "$router" 'printf "router_misc_ping_x=%s\n" "$(nvram get misc_ping_x 2>/dev/null)"; iptables -S INPUT 2>/dev/null | grep -i icmp || true; iptables -S INPUT_PING 2>/dev/null || true' \
    || printf 'WARN unable to inspect router ICMP state via %s\n' "$router"
fi

section "docker nat counters"
if have iptables; then
  iptables -t nat -L DOCKER -n -v 2>/dev/null | rg "dpt:(${game_rmq_public_port}|${game_rmq_public_http_port}|777[7-9]|778[0-9]|779[0-9]|780[0-9]|7810|788[8-9]|789[0-9]|790[0-9]|791[0-8])" || true
elif have nft; then
  nft list ruleset 2>/dev/null | rg "(${game_rmq_public_port}|${game_rmq_public_http_port}|7777|7810|7888|7918)" || true
else
  printf 'WARN neither iptables nor nft found\n'
fi

section "db advertised addresses"
if have docker; then
  if $compose_cmd --env-file "$env_file" ps -q postgres >/dev/null 2>&1; then
    $compose_cmd --env-file "$env_file" exec -T postgres psql -U dune -d "$dune_database" -c \
      "with fs as (
         select fs.*, wp.partition_id as current_partition_id
           from dune.farm_state fs
           left join dune.world_partition wp on wp.server_id = fs.server_id
       )
       select count(*) as farm_rows,
              count(*) filter (where current_partition_id is not null) as current_rows,
              count(*) filter (where ready and alive) as ready_alive_rows,
              count(*) filter (where game_addr::text like '${external_address}/%') as public_game_rows,
              count(*) filter (where igw_addr::text like '${external_address}/%') as public_igw_rows,
              count(*) filter (where igw_addr::text ~ '^(10|172\.(1[6-9]|2[0-9]|3[0-1])|192\.168)\.') as private_igw_rows
         from fs;
       select map, count(*) as rows,
              count(*) filter (where ready and alive) as ready_alive,
              count(*) filter (where igw_addr::text ~ '^(10|172\.(1[6-9]|2[0-9]|3[0-1])|192\.168)\.') as private_igw_rows
         from dune.farm_state
        group by map
       having count(*) filter (where igw_addr::text ~ '^(10|172\.(1[6-9]|2[0-9]|3[0-1])|192\.168)\.') > 0
        order by private_igw_rows desc, map
        limit 20;" \
      2>/dev/null || printf 'WARN farm_state query failed\n'
  else
    printf 'WARN postgres service is not available through compose ps\n'
  fi
else
  printf 'WARN docker not found\n'
fi

section "browser ping verdict"
if have docker; then
  if $compose_cmd --env-file "$env_file" ps -q postgres >/dev/null 2>&1; then
    $compose_cmd --env-file "$env_file" exec -T postgres psql -U dune -d "$dune_database" -At -F $'\t' -c \
      "select
         count(*) filter (where fs.ready and fs.alive),
         count(*) filter (where fs.game_addr::text like '${external_address}/%'),
         count(*) filter (where fs.igw_addr::text ~ '^(10|172\.(1[6-9]|2[0-9]|3[0-1])|192\.168)\.'),
         count(*)
       from dune.world_partition wp
       join dune.farm_state fs on fs.server_id = wp.server_id;" 2>/dev/null \
      | awk -F '\t' -v external="$external_address" '
        NF == 4 {
          printf "current_ready_alive=%s current_public_game_addr=%s current_private_igw_addr=%s current_total=%s\n", $1, $2, $3, $4
          if ($2 != $4) {
            printf "WARN not every farm_state game_addr advertises EXTERNAL_ADDRESS=%s\n", external
          }
          if ($3 != $4) {
            printf "WARN not every current farm_state igw_addr is private; public IGW rewrites can break internal server-to-server routing.\n"
          }
        }' || printf 'WARN farm_state verdict query failed\n'
    $compose_cmd --env-file "$env_file" exec -T postgres psql -U dune -d "$dune_database" -At -F $'\t' -c \
      "select count(*)
         from pg_trigger t
         join pg_class c on c.oid = t.tgrelid
         join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'dune'
          and c.relname = 'farm_state'
          and t.tgname = 'force_public_igw_addr'
          and t.tgenabled = 'O';" 2>/dev/null \
      | awk '$1 != 0 { print "WARN farm_state public IGW trigger is installed/enabled; this can break internal server-to-server routing." }' \
      || printf 'WARN farm_state trigger check failed\n'
  else
    printf 'WARN postgres service is not available through compose ps\n'
  fi
else
  printf 'WARN docker not found\n'
fi
if have ss; then
  if ss -ltn 2>/dev/null | awk -v port="$game_rmq_public_http_port" '
      $4 ~ ":" port "$" {
        if ($4 ~ /(^|[^0-9])127\.0\.0\.1:/ || $4 ~ /^\[::1\]:/) local_only=1
        if ($4 !~ /(^|[^0-9])127\.0\.0\.1:/ && $4 !~ /^\[::1\]:/) public_bind=1
      }
      END {
        if (local_only && !public_bind) exit 42
        exit 0
      }'; then
    :
  else
    printf 'WARN Gateway declares GAME_RMQ_PUBLIC_HTTP_PORT=%s to FLS, but the host listener is localhost-only. If the browser ping path probes GameRmqHttpAddress, ping will be blank until this endpoint is deliberately exposed or no longer advertised as public.\n' "$game_rmq_public_http_port"
  fi
fi

section "latest fls battlegroup declaration"
if have docker && have python3; then
  fls_decl_log="/tmp/dune-browser-ping-fls-declaration.$$"
  $compose_cmd --env-file "$env_file" logs --since=2h director >"$fls_decl_log" 2>/dev/null || true
  python3 - "$external_address" "$fls_decl_log" <<'PY' || true
import json
import re
import sys

external = sys.argv[1]
log_path = sys.argv[2]
latest = None
with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
    for line in handle:
        if "Battlegroups_DeclareBattlegroupUpdates" not in line or "Arguments:" not in line:
            continue
        if 'Arguments: "' not in line:
            continue
        raw = line.split('Arguments: "', 1)[1].rstrip()
        if raw.endswith('"'):
            raw = raw[:-1]
        try:
            payload = json.loads(raw)
        except Exception:
            try:
                decoded = json.loads(f'"{raw}"')
                payload = json.loads(decoded)
            except Exception:
                continue
        latest = payload

if not latest:
    print("WARN no Battlegroups_DeclareBattlegroupUpdates payload found in recent director logs")
    sys.exit(0)

region = latest.get("RegionId") or latest.get("Region") or "unset"
battlegroup = latest.get("BattlegroupId") or "unset"
up = latest.get("UpDeclarationsByPartitionId") or {}
heartbeats = latest.get("HeartbeatUpdatesByPartitionId") or {}
settings = latest.get("SettingsUpdatesByPartitionId") or {}
down = latest.get("DownDeclarationsByPartitionId") or {}
print(f"RegionId={region}")
print(f"BattlegroupId={battlegroup}")
print(f"up_declarations={len(up)} heartbeat_updates={len(heartbeats)} settings_updates={len(settings)} down_declarations={len(down)}")
for partition_id, declaration in sorted(up.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0])):
    address = declaration.get("GameAddress") or "unset"
    port = declaration.get("GamePort")
    map_name = declaration.get("MapName") or "unset"
    starting = declaration.get("IsStartingMap")
    server_id = declaration.get("ServerId") or "unset"
    display_name = declaration.get("DisplayName") or ""
    status = "OK" if address == external and port else "WARN"
    print(f"{status} partition={partition_id} map={map_name} server_id={server_id} game={address}:{port} starting={starting} display={display_name[:120]}")
PY
  rm -f "$fls_decl_log"
else
  printf 'WARN docker and python3 are required for FLS declaration inspection\n'
fi

section "recent fls-ish logs"
if have docker; then
  $compose_cmd --env-file "$env_file" logs --since=30m gateway director text-router 2>/dev/null \
    | rg -i 'fls|heartbeat|battlegroup|gateway|server browser|population|ping|datacenter|external|rmq|declare|register|update|error|fail|timeout' \
    | redact_sensitive \
    | tail -n 300 \
    || true
else
  printf 'WARN docker not found\n'
fi
