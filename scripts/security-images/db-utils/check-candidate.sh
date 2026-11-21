#!/usr/bin/env bash
# Run the broker, command and authentication checks against tracked public fixtures.
set -euo pipefail
image=${1:?expected immutable candidate image}
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
mkdir -p "$fixture"/{admin,scripts,config,backups}
cp admin/{rabbitmq_restore_drill,restore_drill}.py "$fixture/admin/"
cp scripts/{test-docker-api-proxy,docker_api_proxy,test-admin-chat-commands,admin-chat-commands,gm-command-catalog,dune_gm_command,dune_whisper_route,http_url,secret_env,rmq_auth_shim}.py "$fixture/scripts/"
cp -r scripts/vendor "$fixture/scripts/"
cp config/artificial-exchange-prices.csv "$fixture/config/"
cp scripts/security-images/db-utils/check-auth-shim.py "$fixture/scripts/"
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --tmpfs /tmp --tmpfs /code/backups:mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$fixture:/code:ro" -w /code --entrypoint sh "$image" -ec '
    python3 scripts/test-docker-api-proxy.py
    python3 scripts/test-admin-chat-commands.py
    python3 scripts/check-auth-shim.py
  '
# Exercise the rebuilt native driver on loopback with a throwaway CA and database.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
  -addext subjectAltName=DNS:localhost -keyout "$fixture/server.key" -out "$fixture/server.crt" 2>/dev/null
cp scripts/security-images/db-utils/check-postgres.py "$fixture/"
tar -C "$fixture" -cf - server.key server.crt check-postgres.py |
  docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user postgres --tmpfs /fixture:mode=1777 --tmpfs /tmp -w /fixture --entrypoint sh "$image" -ec '
      umask 077
      tar xf -
      chmod 600 server.key
      initdb -D /fixture/data -A trust -U postgres >/dev/null
      pg_ctl -D /fixture/data -l /fixture/server.log -w \
        -o "-k /fixture -h 127.0.0.1 -c ssl=on -c ssl_cert_file=/fixture/server.crt -c ssl_key_file=/fixture/server.key" start
      trap "pg_ctl -D /fixture/data -m fast -w stop" EXIT
      python3 check-postgres.py
    '
