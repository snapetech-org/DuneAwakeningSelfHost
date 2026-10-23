# Desired-State Attestation

DASH continuously proves whether the reviewed repository configuration and the
running Compose project still match an operator-approved baseline. This is a
private, HMAC-authenticated evidence system, not an automatic configuration
reverter. It detects and retains drift without changing files, containers, or
game state.

The default policy is [`config/desired-state.json`](../config/desired-state.json).
The Infrastructure page, authenticated API, CLI, Prometheus endpoint, backup
verifier, restore preflight, and operational SLO all use the same ledger.

## What Is Attested

The default file scope includes:

- `.env`, every `compose*.yaml`, and GitHub workflow files;
- admin-panel and operator scripts;
- `config/`, including INI, policy, TLS, and secret paths;
- deployment packaging; and
- public-site scripts, service units, and container definitions.

Generated Python caches and item icons are excluded. Required paths are emitted
as critical `missing` records instead of silently disappearing. Policy limits
bound the number of files, individual file size, total bytes hashed, observation
retention, and polling cadence. Symlinks are recorded as a hash of their target
text rather than followed outside the workspace.

Each Compose-project container is inspected through the private Docker socket.
The collector obtains only containers with the configured Compose project
label, rechecks that label after inspection to reject inventory races, and
normalizes these properties:

- image reference and immutable image ID;
- entrypoint, command, working directory, and container user;
- restart, privilege, read-only-root, network, capability, security, PID,
  memory, CPU, and CPU-set configuration;
- mount destination/type/write mode;
- attached network names/aliases and explicitly configured static IPAM
  addresses; and
- every environment variable name.

Environment values and host mount sources are stored only as HMAC-SHA256
fingerprints. File contents are stored only as SHA-256, byte count, type, mode,
and criticality metadata. The dashboard/API do not return plaintext file
contents, environment values, host mount sources, or the HMAC key.

Allocator-generated endpoint IDs, dynamic IPs, and MACs are intentionally not
part of the fingerprint: a same-configuration container replacement may change
all three. Explicit Compose/IPAM addresses remain attested, so the fixed map and
control-plane addressing contract still fails on configuration drift.

## States And Findings

The state is one of:

| State | Meaning |
| --- | --- |
| `unsealed` | Observations are running, but no reviewed baseline exists. |
| `attested` | A baseline exists, its ledger verifies, and no drift is open. |
| `drift` | One or more file or container differences remain open. |
| `disabled` | Collection is explicitly disabled. |

Added, removed, and changed subjects become retained findings. A finding keeps
its first/last seen time, criticality, details, acknowledgement, owner, note,
and resolution time. Returning to the approved state resolves it naturally. If
the same drift returns later, it reopens and clears the stale acknowledgement.

Acknowledgement establishes ownership only. It never resolves drift, changes
the SLO signal, or suppresses an alert. A planned SLO maintenance window
suppresses paging through `dash_desired_state_alertable_critical_drift`, but the
observation and finding remain recorded and visible.

## Seal And Reseal Workflow

1. Open **Infrastructure → Desired-State Attestation**.
2. Review every open finding and the policy/runtime/integrity evidence.
3. Correct unintended drift and wait for the next observation. It will resolve
   without a mutation.
4. For an intentional complete-state change, enter a review reason and select
   **Review and reseal current state**.
5. Confirm that the complete current repository and Compose runtime—not only a
   selected finding—should become authoritative.

Sealing requires an authenticated identity with `infrastructure.write`, the
master mutation gate, `DUNE_ADMIN_DESIRED_STATE_MUTATIONS_ENABLED=true`, and
the exact phrase `SEAL DESIRED STATE`. A reason is mandatory. The operation
takes a fresh snapshot, deactivates the prior baseline, appends a signed event,
and immediately observes against the new baseline. Prior baselines are not
deleted and their HMAC signatures remain verifiable.

Acknowledgement uses the same capability/gates and exact phrase
`ACKNOWLEDGE CONFIGURATION DRIFT`. The browser additionally requires a note.

API examples:

```bash
curl -sS -H "X-Admin-Token: $DUNE_ADMIN_TOKEN" \
  http://127.0.0.1:18080/api/ops/desired-state | jq

curl -sS -H "X-Admin-Token: $DUNE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"seal","reason":"reviewed release deployment","confirm":"SEAL DESIRED STATE"}' \
  http://127.0.0.1:18080/api/ops/desired-state | jq

curl -sS -H "X-Admin-Token: $DUNE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"acknowledge","findingId":"drift-...","note":"owned by operations","confirm":"ACKNOWLEDGE CONFIGURATION DRIFT"}' \
  http://127.0.0.1:18080/api/ops/desired-state | jq
```

## Integrity Model

The private SQLite ledger stores immutable baseline documents, signed
observations, individually signed retained findings, and a globally chained
event stream.

- Every baseline signature covers its ID, timestamp, actor, reason, snapshot
  digest, and complete normalized snapshot.
- Every observation signature covers timestamp, baseline, current snapshot
  digest, drift/critical counts, and maintenance state.
- Every finding signature covers its identity, subject/change metadata,
  criticality, first/last/resolved times, acknowledgement/owner/note, and
  normalized drift details. Legitimate lifecycle changes atomically replace
  that signature in the same transaction.
- Every event signature covers its type, timestamp, actor, payload, and the
  preceding event signature.
- SQLite triggers prohibit deletion of baselines/events and prohibit every
  baseline update except the one-way active-to-superseded transition.
- Verification recomputes snapshot SHA-256, every HMAC, the complete event
  chain, required triggers, and SQLite `integrity_check`.

The key must contain at least 64 encoded characters and must not be
group/world-accessible. `scripts/enable-feature-parity.sh` creates a 32-byte
random key as 64 hex characters with mode `0600` when none exists:

```text
config/secrets/desired-state-hmac.secret
```

Loss of that key makes existing signatures unverifiable. Replacing it does not
legitimately repair the old ledger; restore the matching ledger, policy, and
key from one backup set. A copied database with the wrong key fails closed.

The HMAC boundary detects database-only tampering, accidental corruption, and
policy/key/ledger mismatch. It does not defeat an attacker who can both read
the HMAC key and rewrite the ledger/application code, and it is not a substitute
for host access control, off-host backups, signed releases, or Git history.
Attestation reports divergence; it deliberately does not auto-revert a host.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUNE_DESIRED_STATE_ENABLED` | `true` | Starts continuous attestation. |
| `DUNE_ADMIN_DESIRED_STATE_MUTATIONS_ENABLED` | `false` | Enables reviewed seal/acknowledgement actions. |
| `DUNE_DESIRED_STATE_POLICY` | `/workspace/config/desired-state.json` | Container policy path. |
| `DUNE_DESIRED_STATE_DATABASE` | `/workspace/backups/desired-state/desired-state.sqlite3` | Private ledger path. |
| `DUNE_DESIRED_STATE_HMAC_SECRET_FILE` | `/workspace-secret-files/desired-state-hmac.secret` | Private signing key mounted from the protected host secret directory. |
| `DUNE_DESIRED_STATE_POLL_SECONDS` | `60` | Worker cadence, constrained to 15–3600 seconds. |

Host CLI paths can be overridden with `DUNE_DESIRED_STATE_HOST_DATABASE`,
`DUNE_DESIRED_STATE_HOST_POLICY`, and
`DUNE_DESIRED_STATE_HOST_HMAC_SECRET_FILE`.

Policy changes affect the next snapshot and therefore appear as drift because
the policy itself is in the attested file scope. Review the policy change and
the resulting complete snapshot before resealing.

## CLI, Metrics, And Alerts

```bash
make desired-state-status
make desired-state-verify
make desired-state-metrics
```

Equivalent direct commands are `./scripts/desired-state.py status`, `verify`,
and `metrics`. Verification exits nonzero for a missing database, bad SQLite
integrity, a missing append-only trigger, invalid baseline/observation HMAC, or
an invalid event-chain link.

Prometheus scrapes the label-free private endpoint:

```text
GET /metrics/desired-state
```

It exports collector integrity, sealed state, last observation timestamp, open
drift, open critical drift, alertable critical drift, and maintenance state.
Rules alert when the target is unreachable, ledger/HMAC integrity is invalid,
no baseline is sealed, collection is stale for three minutes, or critical
drift remains alertable. No subject,
path, identity, note, digest, or credential is a metric label.

Desired-state attestation is also the eighth default operational SLO. It is
good only when a baseline exists, state is `attested`, and ledger verification
passes. Missing policy, key, database, or collector data fails closed.

## Backup And Recovery

`scripts/backup-state.sh` makes an online SQLite snapshot as
`desired-state.sqlite3`. The matching `config.tgz` carries the policy and HMAC
key. `scripts/verify-backup.sh` extracts those two files into a private
temporary directory and recomputes all HMACs and the event chain; SQLite
integrity alone is not accepted.

Restore the three matching parts together when recovering configuration:

```bash
./scripts/restore-state.sh --dry-run --config --desired-state \
  .env backups/<UTC timestamp>

./scripts/restore-state.sh --config --desired-state \
  .env backups/<UTC timestamp>
```

Without `--config`, `--desired-state` verifies the backup ledger with the
current policy/key before replacing only the ledger. Restore removes stale WAL
and SHM files and writes the database with mode `0600`. Stop the admin-panel
writer before an executed restore, as required by the normal restore runbook.

## Failure Recovery

- **`unsealed`:** inspect the first observation, review the full current state,
  then seal with a meaningful reason. Do not seal merely to silence the alert.
- **collector stale/down:** check admin-panel health, Docker-socket access,
  policy readability, key permissions, and `runtime.lastError`.
- **unexpected file drift:** restore or review the file; the next successful
  poll resolves it automatically.
- **unexpected container drift:** compare image/configuration and recreate only
  through the repository's guarded lifecycle. For live maps, use the required
  post-start-hook wrappers from `AGENTS.md`.
- **invalid ledger:** preserve it for investigation, disable seal actions, and
  compare against the newest backup whose matching HMAC verification passes.
- **lost key:** restore the matching policy, key, and database from one backup
  set. Generating a new key cannot authenticate old evidence.
- **oversized scope:** narrow reviewed include patterns or raise bounded policy
  limits deliberately; both changes themselves require review and reseal.

## Validation

```bash
make test-desired-state
make test-admin-panel-safe-surfaces
make test-operational-slo
make test-operational-borrowing
make validate
```

The tests cover secret/mount redaction, policy bounds, container normalization,
seal/observe/reseal, drift lifecycle, maintenance behavior, append-only
enforcement, baseline/observation/finding/event HMAC tamper detection, backup
consistency, API gates and exact
confirmations, project-label race rejection, SLO fail-closed integration, and
backup/restore behavior.
