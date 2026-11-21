# Building and qualifying security images

## Scheduled rebuilds for self-hosters

The **Rebuild security derivatives** GitHub Actions workflow runs daily on public
Linux AMD64 runners and accepts manual dispatches. Each job downloads the current
Go 1.27 patch for Alpine 3.24, verifies the compiler version, builds a candidate,
and scans a Docker archive by its image-config digest. A build or scan failure
fails that component independently. The evidence artifact includes the raw Trivy
HIGH/CRITICAL report when scanning completes. A passing job also publishes a
candidate archive for two days. Fork owners can enable Actions and the scheduled
workflow, or run the same entry point locally:

```sh
scripts/build-public-security-image.sh postgres /path/to/new-output-directory
```

| Component | Rebuild and compatibility checks | Deployment setting |
| --- | --- | --- |
| `postgres` | Official PostgreSQL 17 patch on Alpine 3.24, distribution packages and a source-built gosu. The server and restore binaries must survive the package upgrade byte-for-byte. Check extensions, Python and pg_restore. | `DUNE_POSTGRES_SECURITY_IMAGE`, replica and restore drill |
| `prometheus` | Pinned upstream source and UI bundle, patched Go dependencies, upstream web tests with embedded assets | `DUNE_METRICS_PROMETHEUS_IMAGE` |
| `node-exporter` | Pinned source, unpacked fixtures and collector tests | `DUNE_METRICS_NODE_EXPORTER_IMAGE` |
| `postgres-exporter` | Pinned source and upstream short test suite | `DUNE_METRICS_POSTGRES_EXPORTER_IMAGE` |
| `cadvisor` | Pinned source and base, native libpfm build, refreshed Alpine packages and model/metrics tests | `DUNE_METRICS_CADVISOR_IMAGE` |
| `admin-ingress` | Caddy 2.11.9 with its MaxMind module and patched Go dependencies. Validate the repository Caddyfile. | `DUNE_ADMIN_INGRESS_IMAGE` |

Download the matching `candidate-<component>` and `evidence-<component>`
artifacts from **one successful job** into the same private directory. Run
`sha256sum -c SHA256SUMS` before `docker load -i candidate.tar`. The archive
is saved by immutable ID. Tag the loaded image locally for the intended
service after qualification. Docker engines with different image stores can
report different loaded IDs. Compare the archive config and rootfs diff IDs
and rescan the loaded image before pinning it. Rehearse PostgreSQL backup,
restore and replication. For monitoring, compare targets, series and collector
errors against the previous image. For ingress, compare modules, headers and a
served page. Retain the previous image and recovery evidence.

### Local vendor images and SteamCMD

Use a Linux AMD64 Docker host with Docker BuildKit, Python 3, jq, curl, tar, OpenSSL and Trivy
0.76.2 on PATH. SteamCMD assessment also uses dpkg for Debian version comparisons. Load the official vendor images from the operator's Steam
package. Copy `config/security/local-images.example` to an owner-controlled
inventory and select the actual source for each service. Each row names an
independent build. `{vendor_tag}` reads `DUNE_IMAGE_TAG` from the chosen env
file. Utility and broker versions can differ from the map worker. A source
image must already be loaded. The .NET runtime and distro updates need network
access. The game check also needs GCC, pip and the pinned scanner image used by
`scripts/qualify-game-runtime.py`.

```sh
scripts/refresh-local-security-images.py /etc/dune-security/local-images     /var/lib/dune-security-candidates --env-file .env
```

Each run writes an owner-private timestamped directory with component logs and
exit codes. An individual failure leaves the other builds eligible. Passing
images have a unique staging tag, a scan report and checksums. Game builds use
the existing payload verification, a fresh unique tag and a new private approval.
The batch only stages candidates. The operator selects the exact image through
the existing readiness, backup, canary and rollback procedure.

To schedule it, adapt `config/systemd/dune-security-rebuild.service` to the
checkout path and operator account, install the service and its sibling timer
under `/etc/systemd/system/`, run `systemctl daemon-reload`, and enable
`dune-security-rebuild.timer`. Reserve space for game rootfs comparisons, image
archives and Docker builds. Move approved evidence and rollback images into
protected storage before expiring old staging directories and their unique tags.

| Local component | Patch applied and qualification |
| --- | --- |
| `game` | Vendor game payload, native libraries and embedded Python compatibility are checked against the exact base. `--fresh` rebuilds even when an earlier approval exists. |
| `director`, `text-router` | Extract the vendor .NET bundle, replace only managed framework assets from the official Linux musl ASP.NET 10.0 patch image, retain application DLLs and configuration, upgrade Alpine and smoke-test the original entrypoint. The recipe rejects unknown framework layouts and downgrades. Verify FLS, farm registration and actual routing in a canary. |
| `gateway` | Refresh Alpine packages, enforce the Kerberos and libpq security floors, and import the native PostgreSQL driver. Verify neighbor seeding after a guarded restart. |
| `db-utils` | Move the known Alpine 3.21 utility base to 3.22 and rebuild CPython 3.12.16 from checksummed source. Retain vendor site packages, update pip's bundled packaging code and the PostgreSQL 17 libraries. The candidate runs the broker, auth-shim and admin-command suites plus a network-isolated PostgreSQL TLS fixture. |
| `rabbitmq` | Apply distro fixes on the selected vendor base and verify the original RabbitMQ version. For an operator-side restore proof with both brokers on one exact deployed image, run `scripts/dune-rabbitmq-restore-current-image.py --workspace "$PWD" --project dune_server`. |
| `steamcmd` | From an already loaded Debian trixie SteamCMD root image, prune unused curl/Kerberos dependencies, refresh selected Debian packages from sid and run `steamcmd.sh +quit` as UID 1000. Set `DUNE_RESTART_STEAMCMD_HELPER_IMAGE` after qualification. |

After qualification, set `DUNE_DB_UTILS_SECURITY_IMAGE` for the DB utilities
and `DUNE_ADMIN_CHAT_SECURITY_IMAGE` for the separately versioned chat image.
Set `DUNE_RABBITMQ_SECURITY_IMAGE` for both brokers. For brokered restore
drills using a new derivative, set the drill reference and its independent
broker approval to the same loaded SHA-256 image ID:

| Drill reference | Operator-controlled broker approval |
| --- | --- |
| `DUNE_RESTORE_DRILL_IMAGE` | `DUNE_BROKER_POSTGRES_DRILL_IMAGE_ID` |
| `DUNE_RABBITMQ_RESTORE_DRILL_IMAGE` | `DUNE_BROKER_RABBITMQ_DRILL_IMAGE_ID` |

Deploy those pins through the guarded control-plane procedure so Admin and its
broker receive the reviewed configuration. Keep each role's previous pin for
rollback. The broker checks both new-container requests and existing drill
containers against the role-specific approval.

The November SteamCMD rehearsal recorded **47 raw HIGH findings** and zero
applicable HIGH findings after review against exact Debian package versions.
Each rebuild retains its raw report. For the previously reviewed advisories
it checks each binary package, architecture, epoch and source version inside the
candidate against Debian's current sid tracker. Only an exact match to the
current, resolved sid source package with a known fixed version produces an
OpenVEX statement. The second scan uses the same archive and cached database.
The candidate keeps the tracker hash, OpenVEX and adjusted report alongside the
raw report. Any remaining HIGH/CRITICAL finding withholds the checksum receipt.
A new advisory or an end-of-life OS can therefore block the next candidate.
For example, the November 21 rebuild matched 47 reviewed Debian package findings
and held the image for three additional OpenSSL package findings for
CVE-2026-124709.

Trivy 0.76.2 may identify the original Alpine 3.21 layer in a DB utility image
after its upgrade. The utility gate checks the installed release package,
`os-release`, `alpine-release`, repository branch and scanned APK inventory.
If they agree on 3.22, it scans the same archive with that verified release and
retains both reports. Both must have zero HIGH/CRITICAL findings.

## Dune utility PostgreSQL client library update

The Dune utility image bases for vendor builds `2162777` and `2173753` use
Alpine 3.21 and carry PostgreSQL `17.11-r0` packages. The November 2026
PostgreSQL security release is available as Alpine `17.12-r0`. Build each
candidate from the exact local base image ID. The helper resolves the source
tag once, creates a temporary local alias for that immutable ID because
BuildKit does not accept a bare image ID in `FROM`, and disables image pulls.
It refuses to overwrite candidate tags and preserves the base image's user,
working directory, and entrypoint. It upgrades and verifies
`postgresql17`, `postgresql17-client`, and `libpq` together because Trivy
reports PostgreSQL CVEs against both the server and client packages.

The local base images are not published to a registry, so the package-only
image build runs on a trusted Docker host that already has each base. Repository
validation and source scans use the compatible public GitHub-hosted runner.
Keep candidates separate from live references until the exact image IDs pass a
Trivy scan with zero HIGH/CRITICAL findings and no scan errors, `python`
imports `psycopg2` successfully, the Dune utility safety suite passes, and a
fresh rollback backup is verified. Promote only the matching utility services
with `--no-deps`; preserve all game worker container IDs and images.

Example candidate builds:

```sh
scripts/build-db-utils-libpq-security-image.sh \
  snape/dune-db-utils:python-security-2162777 \
  snape/dune-db-utils:python-security-2162777-postgresql-17.12
scripts/build-db-utils-libpq-security-image.sh \
  snape/dune-db-utils:python-security-2173753 \
  snape/dune-db-utils:python-security-2173753-postgresql-17.12
```

The Compose defaults reference these version-bound candidates. The build helper
does not start or recreate containers; scan and qualify each exact image before
using it.

## Control-plane package overlays

For the November 2026 package advisory wave, exact-base candidates can be
built with `images/gateway-security/Dockerfile.krb5-2026-11` and
`images/postgres-security/Dockerfile.libxslt-2026-11`. The gateway overlay pins
Alpine 3.24 `krb5-libs` 1.22.3-r0 and `libpq` 18.7-r0. An exact scan of the
running gateway image found 38 HIGH records in `libpq` 18.6-r0; Trivy identifies
18.7-r0 as fixed. The database candidate pins Alpine 3.21 `libxslt` 1.1.42-r3
while retaining PostgreSQL 17.4. Scan immutable candidate IDs and verify image
configuration and base-layer lineage before any handoff. Alpine 3.21 is past
distribution support, so a zero-finding scan of that database image does not
establish complete vulnerability coverage. Keep candidates separate from live
tags until the assured operator workflow supplies signed readiness, player
checks, verified backup, and rollback.

The package-only PostgreSQL 17.4 / Alpine 3.21 candidate was rejected: the
server remained at 17.4 and Alpine 3.21 is out of support. A first candidate
from the pinned official PostgreSQL 17.12 / Alpine 3.24 base also failed the
exact-image gate with 1 CRITICAL and 25 HIGH findings in the bundled `gosu`
binary, which used Go 1.24.6.

The current candidate rebuilds upstream `gosu` 1.19 commit
`6456aaa0f3c854d199d0f037f068eb97515b7513` with pinned Go 1.27.4, while
retaining the official PostgreSQL 17.12 / Alpine 3.24 AMD64 base and adding
Python 3 for the container healthcheck. Exact image ID
`sha256:a9516f0a1b50486ac3b457e9d64e4aef1ac0d737e4f28c9bd738d7a30aea4e5f`
passed Trivy 0.76.2 with zero HIGH and zero CRITICAL findings and no scan
errors. Offline startup, `pg_trgm`, `pgcrypto`, and `pg_restore` checks passed.
A fresh production logical dump restored in the isolated networkless rehearsal;
schema, native player-recovery contract, round-trip archive, and integrity
checks passed without touching the live database. The full production backup at
`backups/20261114T170334Z` was freshly verified, including the database dump,
archives, SQLite databases and HMAC chains, operator evidence capsules, manifest,
and protected environment copy. Production promotion remains operator-only and
requires fresh four zero-player gates, a streaming standby, and exact-image
rollback proof immediately before the change. The PostgreSQL 17.12 change is
being delivered through the existing pull request on compatible public
GitHub-hosted runners.

The PostgreSQL 17.4 / Alpine 3.21 overlay above is retained only as historical
rollback documentation. Alpine 3.21 is out of support. The current tag is
`snape/dune-postgres:security-17.12-alpine3.24-gosu1.19-go1.27.4-20261114`.
PostgreSQL 17.12 is a same-major update; retain the previous local image and
exact ID for rollback. Do not use `apk upgrade` to change PostgreSQL server
binaries: the server version must come from the pinned base image.

The director and text router can use locally built package overlays for a
specific vendor build. Build each overlay from the exact vendor image selected
by `DUNE_IMAGE_TAG`, then scan and test it before changing the private `.env`.
Set all three fields per service; the image ID is the full `docker image
inspect --format '{{.Id}}'` value:

```dotenv
DUNE_DIRECTOR_IMAGE=snape/dune-bg-director:security-<vendor-build>
DUNE_DIRECTOR_IMAGE_ID=sha256:<64 lowercase hexadecimal characters>
DUNE_DIRECTOR_VENDOR_TAG=<same value as DUNE_IMAGE_TAG>
DUNE_TEXT_ROUTER_IMAGE=snape/dune-text-router:security-<vendor-build>
DUNE_TEXT_ROUTER_IMAGE_ID=sha256:<64 lowercase hexadecimal characters>
DUNE_TEXT_ROUTER_VENDOR_TAG=<same value as DUNE_IMAGE_TAG>
DUNE_GATEWAY_IMAGE=snape/dune-gateway:security-<vendor-build>
DUNE_GATEWAY_IMAGE_ID=sha256:<64 lowercase hexadecimal characters>
DUNE_GATEWAY_VENDOR_TAG=<same value as DUNE_IMAGE_TAG>
```

The running-image verifier rejects an incomplete pin, a changed loaded image,
an unapproved local image, or an overlay from an older vendor build. Keep the
vendor images locally for recovery. The standby image sync includes configured
overlays. A new vendor build requires new overlays and scans; do not carry an
old override forward.

Deploy the committed Compose and verifier changes through the assured
control-plane source workflow. Then update the private `.env` and recreate
only the services with changed overlays through the guarded host maintenance path.
Preserve the previous image IDs and configuration for rollback, run the native
gateway neighbor seed after the control-plane restart, and verify FLS, farm
registration, and the exact loaded image IDs. Do not recreate game map workers.

The November Director and Text Router derivatives also replaced the bundled .NET 10.0.12 framework with 10.0.15. Build the current shared-runtime derivative using the local inventory above.

The default game image remains the vendor image selected by `DUNE_IMAGE_TAG`.
An operator can opt into a locally qualified runtime with `DUNE_GAME_IMAGE`
and `DUNE_RUNTIME_SECURITY_APPROVAL_FILE`. This changes only game workers;
RabbitMQ and the remaining control-plane images retain their own references.

## Automatic local qualification

Set `DUNE_AUTO_QUALIFY_GAME_IMAGE=true` in the protected production `.env` to
qualify a fresh runtime for every loaded vendor image. The hotfix updater first
checks the Steam package without applying the old image pin, loads the vendor
image, then runs `scripts/qualify-game-runtime.py` on `kspls0`. The builder uses
the exact vendor image ID as its base, upgrades a hash-pinned wheel in the
embedded Python site-packages, and removes build/development packages. It
compares the entire game payload against the vendor image, allows changes only
to embedded Python packaging, checks native linking and embedded Python, then
scans a saved image tar with pinned Trivy. It writes private evidence and an
approval only after all checks pass. The updater verifies that approval before
changing the image override and entering the guarded backup/restart path.

The output is version-bound under
`backups/admin-panel/runtime-security-auto/auto-<vendor-tag>-<vendor-id>/`.
The builder is idempotent for an unchanged approved image. If qualification
fails, the updater clears the custom override and uses the official vendor
image; it records a per-image failure stamp and retries after
`DUNE_AUTO_QUALIFY_RETRY_SECONDS` (default 21600). The saved report and approval
remain private. Run the builder directly to stage and inspect an approval
without changing the live environment:

```sh
python3 scripts/qualify-game-runtime.py .env
```

The generated approval uses schema 2. Its `imageId` is Docker's loaded image
ID; `scanImageId` is the image-config digest inside the exact tar passed to
Trivy. The verifier binds both identities to the hash-pinned evidence. Schema 1
manual approvals remain accepted.

Use a named local image in the reserved `snape/dune-server:` namespace. Never
retag the vendor image. Keep the approval and its evidence inside the workspace,
for example under the private `backups/admin-panel/runtime-security/` directory.
Approval and evidence must be regular files, not symlinks. Set both optional
settings in the private deployment environment after qualification:

```dotenv
DUNE_GAME_IMAGE=snape/dune-server:qualified-runtime
DUNE_RUNTIME_SECURITY_APPROVAL_FILE=backups/admin-panel/runtime-security/approval.json
```

The approval is a JSON object with this structure (replace all placeholders):

```json
{
  "schemaVersion": 1,
  "vendorTag": "<same value as DUNE_IMAGE_TAG>",
  "image": "snape/dune-server:qualified-runtime",
  "imageId": "sha256:<64 lowercase hexadecimal characters>",
  "vendorImageId": "sha256:<64 lowercase hexadecimal characters>",
  "scan": {"critical": 0, "high": 0, "errors": 0},
  "gameBinaryAssetsAndInterpreterUnchanged": true,
  "qualification": {"path": "backups/admin-panel/runtime-security/qualification.json", "sha256": "<SHA-256>"},
  "scanReport": {"path": "backups/admin-panel/runtime-security/scan.json", "sha256": "<SHA-256>"}
}
```

The qualification report must identify `candidate_image` by immutable image ID
and set `game_binary_assets_and_embedded_interpreter_unchanged`,
`only_python_packaging_files_changed`, `native_binary_linker_passed`, and
`image_runtime_configuration_unchanged` to true. It must also record a true
`embedded_python_<version>_compatibility_passed` result. These are receipts
from the actual payload comparison, native loader check, and embedded Python
compatibility test, not a substitute for those tests.

The pinned Trivy JSON report must identify the same image, include operating
system metadata and `os-pkgs` coverage, and contain no high/critical findings or
scan errors. Qualification is bounded to 8 MiB per artifact and 128 KiB for the
approval. The preflight compares both loaded Docker image IDs with the approval:

```bash
python3 scripts/runtime-security.py .env
```

Compose resolution, running-image verification, the Steam package updater, and
Admin update readiness reject an unapproved replacement. A later vendor version
requires a new build, qualification, scan, and approval; an old runtime image
cannot silently pin old game content while `DUNE_IMAGE_TAG` advances. The
running-image check also rejects a previously local runtime after its approval
has been disabled. Restore the vendor image through the guarded rollout before
removing the override.

Game-update readiness receipts bind the qualified runtime image ID, vendor
image ID, and canonical approval digest. Switching to another runtime or changing
its approval invalidates an earlier receipt, including a vendor-only receipt.

Deploy control-plane changes through the commit-bound assurance workflow first.
The image approval does not replace existing recovery proofs, player/recent-session
checks, readiness gates, Coriolis validation, or post-start hooks. Keep protected
runtime-patch settings unchanged. Roll one empty worker first, verify health and
loaded build, then proceed through the remaining empty workers. Preserve the
old image, configuration, and verified state backup for rollback. An approval
alone never starts or restarts a worker.

## Native operator canary

After enabling and approving the runtime in the private environment, run a new
assured control-plane deployment so Admin collects the same immutable candidate.
Certify game-update readiness for that candidate before touching a worker.
Create `/var/lib/dune-runtime-rollout` as a mode-0700 directory owned by the
configuration owner, outside Admin's writable mounts.

The operator-only CLI defaults to admission checks without a lifecycle action:

```sh
python3 scripts/rollout-runtime-security.py --service deep-desert \
  --reason 'Qualify the approved runtime on one empty worker'
```

Add `--execute` for the reviewed canary. It requires the current host-approved
rendered configuration, exact runtime-bound signed readiness, four zero current
and recent player counts, unchanged environment and approval, and loaded image
identities. It holds the shared maintenance lock, creates and verifies a fresh
backup, repeats admission, and recreates only the selected approved game worker
without pulling an image or touching dependencies. A live worker must publish
its native ready log and active/alive farm registration, then pass native
post-start hooks and a second health check. An inactive worker stays inactive.

On failure, image-only rollback repeats player and identity checks and preserves
state. Unknown players or an unexpected container block rollback for operator
review; the CLI never restores a live database automatically. Recreated IDs are
reapproved through the native existing-container recovery policy. Each attempt
retains a private HMAC-signed job record. No HTTP route gains image-promotion
or approval authority. Stop after a failed canary and retain its evidence.

After successful native verification, the CLI attests the exact image transition
through a restricted desired-state seal. The store verifies its signed baseline
and compares the fresh complete snapshot inside the baseline transaction. The
selected existing container's image ID, image reference, and derived fingerprint
may change. Two reviewed legacy BRT environment names may migrate to their
explicit binary-patch names only when both signed values are exactly false. File changes, other environment changes, enabled or changed flag values, mounts,
commands, resource settings, or another container block attestation. A blocked attestation retains
the healthy candidate and reports the drift; it does not roll the worker back
to conceal an unrelated change. No HTTP route starts or replaces a game worker.
