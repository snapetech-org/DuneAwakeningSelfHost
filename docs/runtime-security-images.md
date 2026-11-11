# Qualified game runtime images

## Control-plane package overlays

For the November 2026 Kerberos/libxslt advisory wave, exact-base candidates
can be built with `images/gateway-security/Dockerfile.krb5-2026-11` and
`images/postgres-security/Dockerfile.libxslt-2026-11`. The gateway candidate
pins Alpine 3.24 `krb5-libs` 1.22.3-r0; the database candidate pins Alpine
3.21 `libxslt` 1.1.42-r3 while retaining PostgreSQL 17.4. Scan the immutable
candidate IDs and verify image configuration and base-layer lineage before
any handoff. Alpine 3.21 is past distribution support, so a zero-finding scan
of that database image does not establish complete vulnerability coverage.
Keep candidates separate from live tags until the assured operator workflow
supplies signed readiness, player checks, verified backup, and rollback.

After the guarded November 2026 rollout, the PostgreSQL 17.4 default is
`snape/dune-postgres:security-17.4-alpine321-fc13-libxslt-20261111`. Build it
from the exact vendor base on each host before Compose deployment; the tag is
local and is not a registry fallback. The rollout retained PostgreSQL 17.4,
verified streaming replication and the required map processes, and refreshed
the two isolated lab databases. Keep the previous image available for rollback.

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

The November 2026 package overlays update musl, zlib, and related Alpine
packages. They do not replace the vendor-bundled .NET runtime; its remaining
scanner findings require a qualified vendor republish.

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
