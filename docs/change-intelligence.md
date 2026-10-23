# Operational Change Intelligence

DASH retains one private, HMAC-authenticated operational timeline and uses it
to answer a practical incident question: **what changed shortly before this
signal failed?** It correlates SLO incidents and desired-state drift with
preceding deployments, settings writes, service actions, restarts, restores,
and capacity decisions.

Correlation is not causation. DASH ranks investigation candidates from time,
declared impact, and shared scope. The dashboard and API explicitly preserve
that distinction and never label a candidate as a root cause.

The versioned policy is
[`config/change-intelligence.json`](../config/change-intelligence.json). The
Infrastructure page, authenticated API, CLI, Prometheus endpoint, backup
verifier, and restore preflight use the same private ledger.

## Why This Exists

Peers provide health, logs, action history, and alerts as separate surfaces.
DASH already had stronger individual evidence—time-weighted SLOs, signed
desired state, restore proof, capacity receipts, and a redacted admin audit
log—but an operator still had to align timestamps manually. Change
Intelligence connects those surfaces without inventing certainty.

For each incident capsule it preserves:

- the signed incident-open event and current open/resolved state;
- preceding change candidates inside the bounded policy window;
- candidate age, declared impact, shared scope, score, and human-readable
  ranking reasons;
- bounded follow-up evidence after the incident; and
- the explicit statement that no causal conclusion was generated.

Every capsule also carries a deterministic response plan compiled from the
versioned policy. It organizes verified evidence, operator review, bounded
diagnostics, and existing guarded recovery surfaces without executing any step.
See [`incident-response.md`](incident-response.md).

Reopened desired-state findings begin a fresh capsule window. Prior resolved
history remains in the append-only event chain.

## Event Sources

Every new admin audit event is recorded directly after it is written to the
private JSONL audit log. This includes system-generated events and authenticated
operator actions such as:

- SLO incident open/resolution;
- desired-state drift open/resolution, seal, and acknowledgement;
- capacity recommendation application;
- service lifecycle actions and scheduled restart execution;
- settings, database, player, economy, world, and other guarded writes;
- update and runtime-repair workflows;
- backup/restore and isolated restore-drill events; and
- security denials and other retained audit events.

On activation and every later start, DASH scans at most the newest
`historyImportLimit` events and the configured startup byte budget from rotated
and current admin audit JSONL files.
It bulk-checks keyed source fingerprints and inserts only missing rows, so the
normal catch-up is idempotent and cheap—including an event that may race first
startup. An audit event whose direct SQLite insertion failed transiently is
recovered on restart instead of being abandoned. The catch-up time is marked in
private SQLite metadata. Future events carry a random `audit-...` ID before
being written to both stores.

After catch-up, DASH reconciles unmatched imported SLO and desired-state opens
against their authoritative SQLite stores. If the source already records an
incident or finding as resolved, Change Intelligence appends a labeled
`authoritative-startup-reconciliation` resolution; it never rewrites or deletes
the historical open. A desired-state baseline seal also emits resolution events
for every finding that the seal closes. These two paths keep capsule state
truthful across upgrades, restarts, and historical audit windows while retaining
the complete chain of what DASH observed and when.

The original JSONL audit remains the webhook/digest compatibility surface.
Change Intelligence is the durable authenticated correlation surface.

## Classification And Ranking

Policy rules use bounded shell-style action patterns. The first matching rule
sets:

- `kind`: `change`, `incident-open`, `incident-resolved`, `evidence`, or
  `observation`;
- `category`: a bounded operator domain such as configuration, lifecycle,
  deployment, recovery, capacity, or reliability; and
- `impact`: `info`, `low`, `medium`, `high`, or `critical`.

An unknown authenticated `POST`, `PUT`, `PATCH`, or `DELETE` fails toward a
generic medium-impact administration change instead of disappearing. Unknown
read events become observations.

For each incident, candidates are restricted to `correlationWindowBeforeSeconds`
before its open time. The score combines:

1. declared impact weight;
2. linear recency within the window; and
3. bounded overlap between normalized scope values.

The score orders review; it has no probability interpretation. A highly ranked
change can be unrelated, and a true external cause can have no corresponding
change event. Operators must verify with the linked SLO, desired-state, logs,
backup, deployment, and runtime evidence.

Follow-up evidence ends at the incident resolution or
`correlationWindowAfterSeconds`, whichever occurs first. It is context for
response/recovery, not a success claim.

## Privacy And Bounds

The change ledger does not copy arbitrary audit payloads verbatim.

- keys containing password, secret, token, cookie, authorization, private-key,
  or credential markers become `<redacted>`;
- player, character, account, FLS, peer/client, target, and subject identifiers
  become keyed HMAC pseudonyms;
- absolute host/filesystem paths become keyed path HMACs, while API/metric/
  health routes remain readable;
- URL user/password components and bearer values in free text are removed;
- depth, dictionary keys, list entries, text length, serialized payload bytes,
  action/source syntax, incident-key syntax, query results, and UI rows are
  bounded; and
- Prometheus metrics contain no action, identity, path, scope, candidate,
  incident, note, or digest labels.

Named admin actors remain readable because accountability is an operator
requirement; local RBAC already constrains their syntax. Game/player identities
do not.

## Integrity Model

Each event contains a random ledger ID, source fingerprint, occurred/ingested
times, action, kind/category/impact, outcome, actor, source, incident key,
change flag, normalized scope/payload, and the prior signature. HMAC-SHA256
covers that complete document. Each signature becomes the next event's chain
link.

SQLite triggers reject every event update and deletion. Verification checks:

- SQLite `integrity_check`;
- both append-only triggers;
- every event HMAC; and
- every previous-signature link from genesis through the newest event.

The HMAC key must contain at least 64 encoded characters and must be mode
`0600` or stricter. `scripts/enable-feature-parity.sh` creates a random 32-byte
key as 64 hex characters when absent:

```text
config/secrets/change-intelligence-hmac.secret
```

The boundary detects database-only tampering, corruption, and mismatched
policy/key/ledger recovery. An actor able to read the key and replace both code
and database can forge evidence; retain off-host backups and signed release/Git
history as independent controls.

The ledger is intentionally append-only. `maxEvents` is a large hard bound,
not silent retention. On reaching it, ingestion fails visibly and requires a
reviewed archive/rotation design; DASH does not delete evidence behind the
operator's back.

## Dashboard And API

Open **Infrastructure → Change Intelligence**. The panel shows ledger
verification, retained event count, open incident count, incidents with
candidate changes, import errors, incident/candidate summaries, and recent
changes. Select an incident to load its complete bounded evidence capsule or
download a portable signed copy.

Authenticated read routes:

```text
GET /api/ops/change-intelligence
GET /api/ops/change-intelligence/capsule?incidentKey=slo:<id>
GET /api/ops/change-intelligence/capsule?incidentKey=desired:<id>
GET /api/ops/change-intelligence/capsule?incidentKey=slo:<id>&signed=true
```

They require only normal authenticated `read` capability. There is no browser
route for fabricating arbitrary evidence. Explicit drill and policy-wide
certification routes can append only their server-derived bounded receipts;
the client cannot submit diagnostic results, runbook results, receipt IDs, or
digests.

### Portable signed capsules

The `signed=true` form freezes the same privacy-bounded capsule into schema 2
and HMAC-authenticates the complete export. The signed payload includes:

- its generation time and incident key;
- the HMAC algorithm and a SHA-256 fingerprint identifying the required key;
- the verified ledger event count and exact head signature at export time;
- SQLite, append-only-trigger, and event-chain verification results; and
- the complete bounded incident, candidate, and follow-up evidence capsule.
- the deterministic response plan, its policy/input digests, and its own
  complete-plan digest.

The outer signature covers every field except the signature itself. Verification
rejects missing or extra top-level fields, a different incident key, a changed
causality contract, malformed head/signature values, a mismatched key
fingerprint, and any nested payload modification. The browser-generated file
contains no HMAC key and receives the same pseudonymization, redaction, and row
bounds as the live API.

Ledger verification, head selection, incident state, candidate ranking, and
follow-up selection all run in one SQLite read transaction. A concurrent writer
can append normally under WAL, but that later event cannot leak into only part
of the export; the capsule consistently represents one database snapshot.

A valid portable signature proves that the holder of the corresponding DASH key
froze this exact capsule from a ledger whose chain verified at that head. It does
not prove causality, prove that no later events exist, or replace the independent
backup containing the source ledger and key. Treat exports as private operator
evidence even though credentials and player identities are removed.
Offline verification also recomputes the nested response-plan digest and every
incident-drill/policy-certification receipt digest. It reports
`responsePlanValid` and `readinessReceiptsValid`.
Authentic schema-1 exports created before response plans remain verifiable and
report `legacyWithoutResponsePlan=true` with `responsePlanValid=null`. A schema-1
artifact that claims to contain a response plan is rejected instead of silently
assigning new semantics to the old format.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUNE_CHANGE_INTELLIGENCE_ENABLED` | `true` | Enables history import and direct future ingestion. |
| `DUNE_CHANGE_INTELLIGENCE_POLICY` | `/workspace/config/change-intelligence.json` | Classification, impact, correlation, and bounds. |
| `DUNE_CHANGE_INTELLIGENCE_DATABASE` | `/workspace/backups/change-intelligence/change-intelligence.sqlite3` | Private append-only ledger. |
| `DUNE_CHANGE_INTELLIGENCE_HMAC_SECRET_FILE` | `/workspace-secret-files/change-intelligence-hmac.secret` | Private authentication key mounted from the protected host secret directory. |
| `DUNE_CHANGE_INTELLIGENCE_EVIDENCE_DIR` | `/workspace/backups/operator-evidence` | Container path for private portable signed incident and deployment evidence. |
| `DUNE_CHANGE_INTELLIGENCE_STATUS_CACHE_SECONDS` | `60` | Single-flight public-status reuse window preventing concurrent API/readiness/metrics scans from rebuilding incident correlation views. Valid range is 1–300 seconds. |
| `DUNE_CHANGE_INTELLIGENCE_HISTORY_IMPORT_MAX_BYTES` | `67108864` | Maximum rotated/current audit JSONL bytes read during startup catch-up; bounded to 1 MiB–512 MiB. |
| `DUNE_CHANGE_INTELLIGENCE_HOST_EVIDENCE_DIR` | `backups/operator-evidence` | Mixed signed-evidence host path archived by full backups. |
| `DUNE_RESPONSE_DRILLS_ENABLED` | `true` | Enables explicit fixed-diagnostic incident rehearsals and fleet-wide policy certification. |

Assured deployment receipts reuse this evidence directory and HMAC key. Their
independent semantic schema is documented in
[`deployment-assurance.md`](deployment-assurance.md).

The in-process store also retains a verified integrity head while the database,
WAL, policy, key, and parent-directory identity and metadata remain unchanged.
A normal append extends that known-good HMAC head after its SQLite transaction;
an unexpected artifact change discards the cache and forces the complete event
walk. New processes, backup copies, portable signed capsule export, and offline
verification start without this cache and independently verify the entire
chain. Assured health uses the bounded verified public status already checked
during convergence instead of starting a redundant 100k-row scan that could
starve its own Prometheus scrape.

Host CLI overrides are `DUNE_CHANGE_INTELLIGENCE_HOST_DATABASE`,
`DUNE_CHANGE_INTELLIGENCE_HOST_POLICY`, and
`DUNE_CHANGE_INTELLIGENCE_HOST_HMAC_SECRET_FILE`.

Policy bounds:

| Key | Default | Valid range |
| --- | ---: | ---: |
| `maxEvents` | 1,000,000 | 1,000–10,000,000 |
| `maxPayloadBytes` | 32,768 | 1,024–1,048,576 |
| `correlationWindowBeforeSeconds` | 3,600 | 60–86,400 |
| `correlationWindowAfterSeconds` | 1,800 | 60–86,400 |
| `statusEventLimit` | 200 | 10–1,000 |
| `candidateLimit` | 20 | 1–100 |
| `capsuleEvidenceLimit` | 200 | 10–1,000 |
| `historyImportLimit` | 10,000 | 0–100,000 |

## CLI, Metrics, And Alerts

```bash
make change-intelligence-status
make change-intelligence-verify
make change-intelligence-metrics

./scripts/change-intelligence.py capsule --incident-key 'slo:<id>'
./scripts/change-intelligence.py plan --incident-key 'slo:<id>'

# Atomic mode-0600 export tied to the current verified ledger head.
./scripts/change-intelligence.py export-capsule \
  --incident-key 'slo:<id>' \
  --output backups/operator-evidence/slo-incident.signed.json

# Offline verification needs only the export and its original private key;
# the source SQLite database and classification policy are not required.
./scripts/change-intelligence.py verify-capsule \
  --capsule-file backups/operator-evidence/slo-incident.signed.json \
  --secret-file config/secrets/change-intelligence-hmac.secret
```

The label-free private endpoint is:

```text
GET /metrics/change-intelligence
```

It exposes ledger verification, total events, open incidents, open incidents
with at least one candidate change, last-event time, latest response-drill
readiness/time, latest fleet-certification readiness/time, ready/total runbooks,
coverage ratio, ready/total shared diagnostics, and ready/total recovery
contracts. The series are label-free and expose no identities, incident keys,
commands, runbook names, gates, or digests. Certification readiness is `1` only
when the outcome passed, its nested receipt digest verifies, and its retained
policy digest matches the currently loaded response policy.

Prometheus alerts when the target is unreachable, integrity verification fails,
an open incident has preceding changes requiring review, the last incident
drill is failed/stale, or the policy has no current passing fleet-wide
certification. Candidate correlation remains an investigation signal, not a
root-cause verdict.

Ledger verification is also the ninth default operational SLO and is not
maintenance-excludable. Planned work cannot hide lost operational evidence.

## Backup And Recovery

Every full backup includes an online SQLite snapshot as
`change-intelligence.sqlite3` when the ledger exists. The matching `config.tgz`
contains the policy and HMAC key. Both the host shell verifier and the minimal
admin-image verifier extract those exact matching files into a private
temporary directory and recompute the entire event chain.

Restore preflight refuses a structurally valid database with the wrong key:

```bash
./scripts/restore-state.sh --dry-run --config --change-intelligence \
  .env backups/<UTC timestamp>

./scripts/restore-state.sh --config --change-intelligence \
  .env backups/<UTC timestamp>
```

Without `--config`, the backup must verify with the current policy/key before
only the ledger is restored. Executed restore removes stale WAL/SHM files and
writes mode `0600`. Stop the admin writer through the normal restore workflow.

Loss of the key cannot be repaired by generating a new one. Restore the policy,
key, and database from one verified backup set.

## Failure Handling

- **collector down:** check admin health, feature flag, policy path, database
  permissions, and key presence/mode.
- **import errors:** inspect `runtime.importErrors`; malformed legacy JSONL is
  skipped and counted, never converted into valid evidence.
- **integrity invalid:** preserve the current database, stop relying on its
  correlations, and compare with the newest matching verified backup.
- **no candidates:** investigate external/runtime causes and logs. The absence
  of a recorded change is not evidence that nothing changed outside DASH.
- **too many candidates:** narrow the policy window only after reviewing normal
  change frequency; do not tune scores to force a preferred explanation.
- **max events reached:** preserve and archive the complete ledger. Do not
  delete rows or remove append-only triggers.
- **wrong incident selected:** incident keys are bounded to `slo:`, `desired:`,
  or internal `event:` forms and SQL remains parameterized.

## Validation

```bash
make test-change-intelligence
make test-admin-panel-safe-surfaces
make test-operational-borrowing
make validate
```

Tests cover policy/key/file modes, credential/identity/path redaction, payload
bounds, classification fallbacks, temporal ranking, non-causal language,
resolution/reopen handling, idempotent history import, append-only enforcement,
HMAC tamper detection, portable signed export/offline verification, metrics
privacy, fixed-diagnostic deduplication, policy-wide runbook/recovery coverage,
nested readiness-receipt verification, native/minimal backup verification,
restore preflight, API authentication/confirmation, and dashboard wiring.
