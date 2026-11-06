# Qualified game runtime images

The default game image remains the vendor image selected by `DUNE_IMAGE_TAG`.
An operator can opt into a locally qualified runtime with `DUNE_GAME_IMAGE`
and `DUNE_RUNTIME_SECURITY_APPROVAL_FILE`. This changes only game workers;
RabbitMQ and the remaining control-plane images retain their own references.

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
