#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
config="$tmp/sietches.json"
overlay="$tmp/compose.sietches.generated.yaml"
fixture_root="$tmp/repo"
mkdir -p "$fixture_root/scripts"
cp "$repo_root/scripts/sietches.sh" "$fixture_root/scripts/"
cp "$repo_root/compose.yaml" "$repo_root/compose.allmaps.yaml" "$repo_root/.env.example" "$fixture_root/"
printf '%s\n' '{"partitions":{"32":{"display_name":"Sietch Alpha","password":"TestPassword"}}}' > "$config"

DUNE_SIETCH_TEST_ROWS=$'32\t1\tSietch Alpha' \
DUNE_SIETCH_CONFIG_FILE="$config" DUNE_SIETCH_OVERLAY_FILE="$overlay" \
  "$fixture_root/scripts/sietches.sh" "$fixture_root/.env.example" render >/dev/null

grep -q '^  sietch-32:$' "$overlay"
grep -q -- '-PartitionIndex=32' "$overlay"
grep -q '8001:8001/udp' "$overlay"
grep -q '8101:8101/udp' "$overlay"
grep -q '172.31.240.129' "$overlay"
! grep -q 'TestPassword' "$overlay"
grep -q 'target: dune_sietch_login_password' "$overlay"
[[ "$(cat "$fixture_root/config/secrets/sietch-login-32")" == TestPassword ]]
[[ "$(stat -c %a "$fixture_root/config/secrets/sietch-login-32")" == "600" ]]
[[ "$(stat -c %a "$overlay")" == "600" ]]
docker compose --env-file "$fixture_root/.env.example" -f "$fixture_root/compose.yaml" -f "$fixture_root/compose.allmaps.yaml" -f "$overlay" config --quiet

output="$($repo_root/scripts/sietches.sh "$repo_root/.env.example" set-active 3)"
grep -q 'plan: set Survival_1 active dimensions to 3' <<< "$output"
printf 'Sietch tests passed\n'
