# Map asset index helper

This offline source utility indexes cooked map paths from package metadata. It
is separate from live server deployment and does not restart maps or mutate
server databases.

Builds use the pinned retoc Git revision in Cargo.toml and the transitive Git
revisions recorded in Cargo.lock. The previous manifest depended on absolute
paths in one machine's Cargo cache; those paths are not required now. The
original jmap, repak and ser-hex source revisions are preserved in the lockfile.
Always use the checked-in lockfile for verification:

```sh
cargo test --locked --manifest-path scripts/map-asset-index/Cargo.toml
cargo clippy --locked --manifest-path scripts/map-asset-index/Cargo.toml --all-targets -- -D warnings
cargo audit --file scripts/map-asset-index/Cargo.lock
```

The security refresh updates rustls, certificate validation/root bundles, zlib,
and Zstandard within compatible release lines. Three map-path regression tests
pass, Clippy passes, and the refreshed dependency audit has zero vulnerabilities
or warnings. These checks do not qualify proprietary game assets or promote a
new production game image.
