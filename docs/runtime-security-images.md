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
