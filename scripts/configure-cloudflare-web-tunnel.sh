#!/usr/bin/env bash
set -euo pipefail

apply=false
if [[ "${1:-}" == "--apply" ]]; then
  apply=true
  shift
fi

env_file="${1:-/etc/snape/cloudflare.env}"
tunnel_id="${CLOUDFLARE_TUNNEL_ID:-1c9aab85-f178-4257-be00-390271752e90}"
zone_name="${CLOUDFLARE_ZONE_NAME:-snape.tech}"
expected_host="${DUNE_PRODUCTION_HOST:-kspls0}"
hostnames=(dune.snape.tech snape.tech www.snape.tech palworld.snape.tech)

if [[ "$(hostname -s)" != "$expected_host" ]]; then
  printf 'error: refusing Cloudflare production configuration on %s; expected %s\n' "$(hostname -s)" "$expected_host" >&2
  exit 1
fi
if [[ ! -r "$env_file" ]]; then
  printf 'error: cannot read %s\n' "$env_file" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$env_file"
: "${CLOUDFLARE_ACCOUNT_ID:?missing CLOUDFLARE_ACCOUNT_ID}"
: "${CLOUDFLARE_API_TOKEN:?missing CLOUDFLARE_API_TOKEN}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/snape-curl-auth.sh"

api="https://api.cloudflare.com/client/v4"
cf() {
  local method="$1" path="$2" payload="${3:-}"
  local args=(-fsS -X "$method" -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json")
  [[ -n "$payload" ]] && args+=(--data "$payload")
  curl "${args[@]}" "${api}${path}"
}

cf GET /user/tokens/verify | jq -e '.success and .result.status == "active"' >/dev/null || {
  printf 'error: Cloudflare API token is not active\n' >&2
  exit 1
}

zone_id="$(cf GET "/zones?name=${zone_name}&account.id=${CLOUDFLARE_ACCOUNT_ID}" | jq -er '.result[0].id')"
current="$(cf GET "/accounts/${CLOUDFLARE_ACCOUNT_ID}/cfd_tunnel/${tunnel_id}/configurations")"
desired='[
  {"hostname":"dune.snape.tech","service":"http://127.0.0.1:80","originRequest":{"httpHostHeader":"dune.snape.tech"}},
  {"hostname":"snape.tech","service":"http://127.0.0.1:80","originRequest":{"httpHostHeader":"snape.tech"}},
  {"hostname":"www.snape.tech","service":"http://127.0.0.1:80","originRequest":{"httpHostHeader":"www.snape.tech"}},
  {"hostname":"palworld.snape.tech","service":"http://127.0.0.1:80","originRequest":{"httpHostHeader":"palworld.snape.tech"}}
]'

payload="$(jq -c --argjson desired "$desired" '
  .result.config as $config
  | ($desired | map(.hostname)) as $managed_names
  | [
      $config.ingress[]
      | select(has("hostname"))
      | select(.hostname as $hostname | ($managed_names | index($hostname) | not))
    ] as $preserved
  | {config: ($config | .ingress = ($preserved + $desired + [{service:"http_status:404"}]))}
' <<<"$current")"

declare -A record_ids
for hostname in "${hostnames[@]}"; do
  records="$(cf GET "/zones/${zone_id}/dns_records?name=${hostname}")"
  count="$(jq -er '.result | length' <<<"$records")"
  if (( count > 1 )); then
    printf 'error: %s has %s DNS records; refusing an ambiguous replacement\n' "$hostname" "$count" >&2
    exit 1
  fi
  record_ids["$hostname"]="$(jq -r '.result[0].id // empty' <<<"$records")"
done

printf 'Tunnel %s will publish:\n' "$tunnel_id"
jq -r '.config.ingress[] | select(has("hostname")) | "  \(.hostname) -> \(.service)"' <<<"$payload"
printf 'DNS target: %s.cfargotunnel.com\n' "$tunnel_id"

if [[ "$apply" != true ]]; then
  printf 'dry run only; rerun with --apply to update remote tunnel configuration and DNS\n'
  exit 0
fi

cf PUT "/accounts/${CLOUDFLARE_ACCOUNT_ID}/cfd_tunnel/${tunnel_id}/configurations" "$payload" | jq -e '.success' >/dev/null

for hostname in "${hostnames[@]}"; do
  dns_payload="$(jq -nc --arg name "$hostname" --arg content "${tunnel_id}.cfargotunnel.com" '{type:"CNAME",name:$name,content:$content,ttl:1,proxied:true,comment:"Managed by configure-cloudflare-web-tunnel.sh"}')"
  if [[ -n "${record_ids[$hostname]}" ]]; then
    cf PUT "/zones/${zone_id}/dns_records/${record_ids[$hostname]}" "$dns_payload" | jq -e '.success' >/dev/null
  else
    cf POST "/zones/${zone_id}/dns_records" "$dns_payload" | jq -e '.success' >/dev/null
  fi
  printf 'updated %s\n' "$hostname"
done

printf 'ok: Cloudflare web tunnel configuration and DNS updated\n'
