#!/usr/bin/env bash
# Build on the amd64 Docker host holding the source image. Never read the deployment .env.
set -euo pipefail
component=${1:?usage: build-local-security-image.sh COMPONENT LOADED_BASE NEW_OUTPUT_DIRECTORY}
base=${2:?missing loaded base image}
output=$(realpath -m "${3:?missing new output directory}")
cd "$(dirname "$0")/.."
umask 077
mkdir "$output"
docker image inspect "$base" | jq -e '
  .[0] | select(.Os == "linux" and .Architecture == "amd64") |
  {id:.Id, layers:.RootFS.Layers, workingDir:.Config.WorkingDir, entrypoint:.Config.Entrypoint,
   cmd:.Config.Cmd, user:(.Config.User // "")}
' > "$output/base.json"
id=$(jq -er .id "$output/base.json")
run_id=$(cat /proc/sys/kernel/random/uuid)
alias="dune-local-base:$run_id"
docker tag "$id" "$alias"
trap 'docker image rm --no-prune "$alias" >/dev/null' EXIT
args=(--build-arg "BASE_IMAGE=$alias" --label "tech.snape.security.base-image-id=$id")
ctx="scripts/security-images/$component"
required=os-pkgs
case "$component" in
  director|text-router)
    required=dotnet-core
    ctx=scripts/security-images/dotnet
    if [[ $component == director ]]; then
      app=Director
      namespace=dune-bg-director
      app_dir=/Tools/Battlegroups/Director/BattlegroupDirector
    else
      app=TextRouter
      namespace=dune-text-router
      app_dir=/Tools/Battlegroups/TextRouter/TextRouter
    fi
    jq -e --arg dir "$app_dir" --arg app "./$app" '.workingDir == $dir and .entrypoint == [$app]' "$output/base.json" >/dev/null
    runtime=${ASPNET_IMAGE:-mcr.microsoft.com/dotnet/aspnet:10.0-alpine3.24}
    docker pull --platform linux/amd64 "$runtime"
    runtime=$(docker image inspect --format '{{index .RepoDigests 0}}' "$runtime")
    patch=$(docker run --rm --network none --entrypoint dotnet "$runtime" --list-runtimes | awk '$1 == "Microsoft.AspNetCore.App" {print $2}')
    [[ $patch =~ ^10\.0\.([0-9]+)$ && ${BASH_REMATCH[1]} -ge 15 ]]
    args+=(--build-arg "ASPNET_IMAGE=$runtime" --build-arg "DOTNET_PATCH=$patch"
           --build-arg "APP_DIR=$app_dir" --build-arg "APP=$app")
    printf '%s\n' "$runtime" > "$output/runtime.digest"
    ;;
  rabbitmq)
    namespace=dune-rabbitmq
    ctx=images/rabbitmq-security
    version=$(docker run --rm --network none --entrypoint rabbitmq-diagnostics "$id" --version)
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
    args+=(--build-arg "EXPECTED_RABBITMQ_VERSION=$version")
    ;;
  steamcmd|gateway|db-utils) namespace="dune-$component" ;;
  *) echo "Unknown component: $component" >&2; exit 2 ;;
esac
tag="snape/$namespace:security-$run_id"
docker build --no-cache --pull=false "${args[@]}" -t "$tag" "$ctx"
image=$(docker image inspect --format '{{.Id}}' "$tag")
printf '%s %s\n' "$image" "$tag" > "$output/build-image.txt"
docker image inspect "$image" | jq -e --slurpfile base "$output/base.json" '
  def user: if . == null or . == "" or . == "0" then "root" else . end;
  .[0] | .RootFS.Layers as $layers | $base[0].layers as $source |
  ($source | length) > 0 and $layers[:($source | length)] == $source and
  .Config.Entrypoint == $base[0].entrypoint and .Config.WorkingDir == $base[0].workingDir and
  .Config.Cmd == $base[0].cmd and (.Config.User | user) == ($base[0].user | user)
' >/dev/null
case "$component" in
  director|text-router) docker run --rm --network none "$image" --version ;;
  db-utils) scripts/security-images/db-utils/check-candidate.sh "$image" ;;
  steamcmd) timeout 180 docker run --rm --user 1000:1001 --entrypoint /home/steam/steamcmd/steamcmd.sh "$image" +quit ;;
esac
scripts/scan-security-candidate.sh "$tag" "$output" "$required" "$component"
