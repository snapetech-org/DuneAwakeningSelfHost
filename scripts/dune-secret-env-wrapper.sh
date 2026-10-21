#!/bin/sh
set -eu

profile=${1:-}
if [ -z "$profile" ]; then
  printf '%s\n' 'missing secret-wrapper profile' >&2
  exit 64
fi
shift

load_secret() {
  secret_name=$1
  secret_path=$2
  if [ ! -r "$secret_path" ]; then
    printf 'required secret file is unreadable: %s\n' "$secret_path" >&2
    exit 78
  fi
  secret_value=$(cat "$secret_path") || {
    printf 'failed to read required secret file: %s\n' "$secret_path" >&2
    exit 78
  }
  if [ -z "$secret_value" ]; then
    printf 'required secret file is empty: %s\n' "$secret_path" >&2
    exit 78
  fi
  export "$secret_name=$secret_value"
  unset secret_value secret_name secret_path
}

case "$profile" in
  director)
    load_secret Database__password /run/secrets/dune_postgres_password
    load_secret FuncomLiveServices__ServiceAuthToken /run/secrets/dune_fls_token
    load_secret RMQ_HTTP_TOKEN_AUTH_SECRET /run/secrets/dune_rmq_http_token_auth_secret
    exec ./Director "$@"
    ;;
  text-router)
    load_secret Database__password /run/secrets/dune_postgres_password
    load_secret DuneDatabaseInterfacePSQL_DatabasePassword /run/secrets/dune_postgres_password
    load_secret FuncomLiveServices__ServiceAuthToken /run/secrets/dune_fls_token
    load_secret RMQ_HTTP_TOKEN_AUTH_SECRET /run/secrets/dune_rmq_http_token_auth_secret
    exec ./TextRouter "$@"
    ;;
  gateway)
    load_secret DuneDatabaseInterfacePSQL_DatabasePassword /run/secrets/dune_postgres_password
    load_secret FuncomLiveServices__ServiceAuthToken /run/secrets/dune_fls_token
    load_secret gateway_farm_api_key /run/secrets/dune_fls_token
    load_secret RMQ_HTTP_TOKEN_AUTH_SECRET /run/secrets/dune_rmq_http_token_auth_secret
    exec "$@"
    ;;
  rabbitmq)
    load_secret RMQ_HTTP_TOKEN_AUTH_SECRET /run/secrets/dune_rmq_http_token_auth_secret
    exec docker-entrypoint.sh "$@"
    ;;
  *)
    printf 'unsupported secret-wrapper profile: %s\n' "$profile" >&2
    exit 64
    ;;
esac
