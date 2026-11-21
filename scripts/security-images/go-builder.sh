# Resolve the current patch in the qualified Go/Alpine series once per build.
# Set GO_BUILDER_IMAGE to a recorded RepoDigest to reproduce the compiler.
go_image="${GO_BUILDER_IMAGE:-golang:1.27-alpine3.24}"
docker pull --platform linux/amd64 "$go_image" >&2
builder=$(docker image inspect --format '{{index .RepoDigests 0}}' "$go_image")
go_version=$(docker run --rm --network none --entrypoint go "$builder" env GOVERSION)
[[ $go_version =~ ^go1\.27\.([0-9]+)$ && ${BASH_REMATCH[1]} -ge 4 ]] || {
  echo "Unsupported Go compiler: $go_version" >&2
  exit 1
}
printf 'Go compiler %s %s\n' "$go_version" "$builder" >&2
