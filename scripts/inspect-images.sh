#!/usr/bin/env bash
set -euo pipefail

tag="${DUNE_IMAGE_TAG:-1963158-0-shipping}"

images=(
  "registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:$tag"
  "registry.funcom.com/funcom/self-hosting/seabass-server-bg-director:$tag"
  "registry.funcom.com/funcom/self-hosting/seabass-server-gateway:$tag"
  "registry.funcom.com/funcom/self-hosting/seabass-server-text-router:$tag"
  "registry.funcom.com/funcom/self-hosting/seabass-server-db-utils:$tag"
  "registry.funcom.com/funcom/self-hosting/seabass-server:$tag"
  "${DUNE_POSTGRES_SECURITY_IMAGE:-snape/dune-postgres:security-17.4-alpine321-fc13}"
)

docker image inspect "${images[@]}" |
  jq '.[] | {
    tag: .RepoTags[0],
    user: .Config.User,
    entrypoint: .Config.Entrypoint,
    cmd: .Config.Cmd,
    workdir: .Config.WorkingDir,
    env: .Config.Env,
    exposed: .Config.ExposedPorts,
    volumes: .Config.Volumes
  }'
