#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${CARGO_TARGET_DIR:-/tmp/dune-map-asset-index-target}"
manifest="$repo_root/scripts/map-asset-index/Cargo.toml"
output="$repo_root/scripts/map-asset-index/bin/dune-map-asset-index"

cargo build --locked --release --offline --manifest-path "$manifest" --target-dir "$target_dir"
install -D -m 0755 "$target_dir/release/dune-map-asset-index" "$output"
printf 'built map asset indexer: %s\n' "$output"
