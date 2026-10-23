# Credential Lifecycle Control Center

## Purpose

DASH centralizes first-party credential posture without returning credential
values, raw hashes, or keyed fingerprints to a browser, API client, metric, log,
or audit event. The control answers five separate questions:

1. Is a credential required by the currently active feature gates?
2. Is its configured source present, non-placeholder, sufficiently long, and
   private?
3. Which bounded first-party consumers depend on it?
4. Has its material changed since DASH began observing it, and is a scheduled
   rotation due?
5. Does the newest full backup contain the source and the matching
   tamper-evident observation history?

This is posture evidence, not a secret viewer and not a password vault.

## Operator surface

Open **Security → Credential Lifecycle Control Center**. The table reports:

- the catalog ID and human-readable title;
- category and declared consumers;
- whether current feature gates require the credential;
- the environment-key or private-file reference, never its value;
- source permission state;
- minimum-material result without reporting its actual length;
- rotation policy, observed age, and maximum age where scheduled;
- newest-full-backup coverage; and
- exact secret-safe findings.

The authenticated API is:

```text
GET /api/ops/credential-lifecycle
GET /api/ops/credential-lifecycle?refresh=true
```

The refresh form bypasses the bounded Feature Readiness cache. Both responses
set `secretValuesReturned=false` and `materialFingerprintsReturned=false`.

## Catalog and activation semantics

The strict versioned catalog is
[`config/credential-lifecycle.json`](../config/credential-lifecycle.json).
Unknown fields, duplicate IDs, unsafe paths, unbounded material requirements,
invalid gates, and incomplete backup contracts fail closed.

Each entry declares:

- `source`: an environment value, a confined private file, or an environment
  value with a private-file override;
- `requiredWhen`: `always`, `allGates`, or `anyGates` activation rules;
- `minimumBytes` and exact known placeholder values;
- `rotationPolicy` and an optional `maximumAgeDays`;
- the complete first-party consumer list;
- an `env-copy`, `config-member`, `direct-artifact`, `env-or-config`, or `none`
  backup contract; and
- its authoritative rotation/recovery runbook.

Disabled optional integrations remain visible as `not-required`. Missing
operator-generated material is a problem. Missing OAuth/Discord material that
only the external provider can issue is `external-pending`; it remains visible
without misclassifying DASH's implementation as broken.

The shipped catalog covers 19 credential contracts:

- FLS, PostgreSQL bootstrap/application, and RabbitMQ data-plane material;
- the owner recovery token and native game-command token;
- Discord adapter and provider bot tokens;
- Desired State, Change Intelligence, Feature Readiness, credential lifecycle,
  mutation audit, and two-person approval HMAC keys;
- federated session and provider client secrets;
- vote/payment webhook HMAC secrets; and
- the stable public-directory Ed25519 identity key.

## Source safety

Environment credentials inherit the `.env` file's permission verdict. File
credentials must resolve inside the DASH workspace, must be regular files, and
must not be symlinks. Group/world permission bits make a configured source
unsafe. An absolute `/workspace/...` container path is mapped back to the same
workspace root before confinement checks.

The evaluator reads material only to perform these in-process checks:

- non-empty;
- exact placeholder rejection;
- minimum byte count; and
- keyed change detection.

It does not report actual length, content, hashes, HMACs, prefixes, suffixes, or
entropy guesses. Minimum length is not presented as proof of entropy.

## Rotation history and age semantics

State is stored under:

```text
backups/credential-lifecycle/history.sqlite3
backups/credential-lifecycle/history.anchor.json
config/secrets/credential-lifecycle-hmac.secret
```

The HMAC key is at least 32 bytes and mode `0600`. When Admin runs as root in
the container, it applies `DUNE_HOST_UID`/`DUNE_HOST_GID` to the key, database,
WAL/SHM companions, anchor, and their private directories so the host-side
backup verifier can read the recovery unit without weakening permissions. For
each valid configured credential, DASH derives an HMAC fingerprint with domain separation, the
credential ID, and the private master key. Fingerprints never leave the store.
This prevents an offline dictionary test against a copied database unless the
separate HMAC key is also compromised.

The first sighting appends a `baseline` event. A different keyed fingerprint
appends a `rotation` event. Repeated observations are deduplicated. Every event
includes the preceding event HMAC, forming one global append-only chain across
all credentials. SQLite triggers reject update and delete operations. Every
read verifies database integrity and the complete chain before accepting or
recording more observations. A separately authenticated head anchor binds the
current sequence and event HMAC, so clean deletion of the newest valid events
also fails verification. The database and anchor are one recovery unit.

`observedAgeDays` begins at the first baseline. It is not a claim about when a
pre-existing secret was originally issued. After DASH observes a material
change, it is the observed rotation age, bounded by the polling/refresh
interval. The UI labels this evidence explicitly.

Rotation policies have different meanings:

- `scheduled`: rotate before `maximumAgeDays`.
- `provider-managed`: rotate in the external provider and update DASH as one
  coordinated operation.
- `retain-with-ledger`: keep the key with its dependent history. Rotation
  requires a documented ledger transition or loses verification continuity.
- `stable-public-identity`: retain the signing key unless compromised; routine
  rotation breaks the public trust identity.

## Findings and remediation

| Finding | Meaning | Action |
| --- | --- | --- |
| `missing` | An active first-party feature requires material that is absent. | Use the linked subsystem runbook to create or install it, then restart only its documented consumers. |
| `external-credential-pending` | An active integration awaits provider-issued material. | Create the provider application/token and run its documented canary. |
| `placeholder` | The source still contains an exact shipped placeholder. | Generate new high-entropy material and replace it through the guarded setting or runbook. |
| `short-material` | The byte count is below the catalog minimum. | Replace it; do not pad a weak value. |
| `insecure-source-permissions` | `.env` or the private file is not a private regular source. | Remove symlinks and set the owner-only mode, normally `0600`. |
| `backup-uncovered` | The newest full backup lacks the configured source/artifact. | Create and verify a new full backup after the source exists. |
| `rotation-due-soon` | Observed age exceeds 80% of the scheduled limit. | Schedule a coordinated rotation and recovery proof. |
| `rotation-overdue` | Observed age exceeds the catalog limit. | Rotate now using the linked runbook and verify every consumer. |
| `invalid-source` | The catalog path escapes the workspace, is a symlink, or cannot be safely read. | Correct the path and ownership; never broaden filesystem access. |

After any rotation:

1. verify the intended target host;
2. follow the credential's linked subsystem runbook;
3. restart/reload only documented consumers;
4. refresh the Credential Lifecycle API and confirm one new rotation event;
5. create a full backup;
6. run `scripts/verify-backup.sh backups/<timestamp>`; and
7. confirm `backupCovered=true` and no new readiness regression.

## Backup and restore contract

`scripts/backup-state.sh` now snapshots the credential observation SQLite
database and its authenticated head together while `config.tgz` carries their
exact matching HMAC key. The backup retries around concurrent observations
until the copied database/anchor pair verifies. The verifier extracts the key
into a private temporary file and validates the full event chain and anchored
head. A partial pair or mismatched key fails backup creation and verification.

The same full backup now preserves the two-person change-approval database and
its HMAC key as an inseparable pair:

```text
change-approvals.sqlite3
change-approvals.key
```

This closes a previous recovery gap: approval requests and their authenticated
event history can no longer be silently omitted while the rest of the trust
plane is considered backed up. A partial pair makes backup creation or
verification fail.

Restore only the required layers while the Admin Panel is stopped:

```bash
scripts/restore-state.sh --dry-run \
  --config --credential-lifecycle --change-approvals \
  .env backups/<timestamp>

scripts/restore-state.sh \
  --config --credential-lifecycle --change-approvals \
  .env backups/<timestamp>
```

`--credential-lifecycle` verifies the snapshot against the current HMAC key,
or the backup's key when `--config` is selected, before installation.
`--change-approvals` always restores and verifies its database/key pair
together. The normal restart path must be used after recovery.

## Metrics and alerts

The existing authenticated `dash-change-intelligence` scrape includes
label-free credential metrics:

```text
dash_credential_lifecycle_enabled
dash_credential_lifecycle_ok
dash_credential_lifecycle_total
dash_credential_lifecycle_required
dash_credential_lifecycle_problems
dash_credential_lifecycle_missing
dash_credential_lifecycle_insecure_permissions
dash_credential_lifecycle_backup_uncovered
dash_credential_lifecycle_rotation_overdue
dash_credential_lifecycle_rotation_due_soon
dash_credential_lifecycle_history_valid
dash_credential_lifecycle_rotations_total
```

No credential ID, path, value, fingerprint, or consumer is used as a metric
label. Alert rules cover invalid history, required missing material, unsafe
permissions, newest-backup gaps, and overdue rotations.

## Feature Readiness integration

`DUNE_CREDENTIAL_LIFECYCLE_ENABLED=true` is part of the normal parity
activator. Feature Readiness requires the strict catalog, evaluator module,
Admin Panel service, and runtime posture probe. The activator creates the HMAC
key with mode `0600` and configures:

```text
DUNE_CREDENTIAL_LIFECYCLE_DATABASE=/workspace/backups/credential-lifecycle/history.sqlite3
DUNE_CREDENTIAL_LIFECYCLE_HMAC_SECRET_FILE=/workspace-secret-files/credential-lifecycle-hmac.secret
```

The first refresh establishes baselines. Create a new full backup afterward so
the first observation history and matching key are recoverable together.

## Validation

Run the focused suite with:

```bash
PYTHONWARNINGS=error::ResourceWarning \
  PYTHONPATH=admin python3 scripts/test-credential-lifecycle.py

bash -n scripts/backup-state.sh scripts/verify-backup.sh \
  scripts/restore-state.sh scripts/enable-feature-parity.sh
```

The repository-wide gate additionally validates the API/UI, strict catalog,
Feature Readiness gate coverage, label-free metrics, alert rules, backup
verification, and existing operational safety invariants:

```bash
make validate
```
