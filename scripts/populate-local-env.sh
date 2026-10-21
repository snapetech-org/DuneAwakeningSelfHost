#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
tls_dir="config/tls/rabbitmq"

if [[ -f "$env_file" ]]; then
  echo "$env_file already exists; leaving it unchanged" >&2
else
  cp .env.example "$env_file"
  postgres_super_password="$(openssl rand -hex 24)"
  postgres_dune_password="$(openssl rand -hex 24)"
  postgres_replication_password="$(openssl rand -hex 32)"
  rmq_secret="$(openssl rand -hex 64)"
  world_suffix="$(openssl rand -hex 3)"

  python3 ./scripts/update-env-file.py "$env_file" --allow-protected-link --quiet \
    --set POSTGRES_SUPER_PASSWORD "$postgres_super_password" \
    --set POSTGRES_DUNE_PASSWORD "$postgres_dune_password" \
    --set POSTGRES_REPLICATION_PASSWORD "$postgres_replication_password" \
    --set RMQ_HTTP_TOKEN_AUTH_SECRET "$rmq_secret" \
    --set WORLD_UNIQUE_NAME "sh-example-$world_suffix"
fi

mkdir -p "$tls_dir"

if [[ ! -f "$tls_dir/ca.crt" || ! -f "$tls_dir/server.crt" || ! -f "$tls_dir/server.key" ]]; then
  ./scripts/generate-rabbitmq-cert.sh "$env_file"
fi

echo "Populated $env_file and $tls_dir"
echo "Next: edit $env_file and set FLS_SECRET, EXTERNAL_ADDRESS, and any world metadata."
