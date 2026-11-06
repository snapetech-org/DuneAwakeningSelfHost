# Assured Change Windows

Large retained ledgers keep full timeline collection deferred during Prometheus
scrapes. Readiness still exposes the recent fully verified status from the
bounded status cache, including the signed receipt and current policy checks.
Missing, expired, future-dated or invalid proof reports not ready; a newly
recorded change invalidates the status and metrics caches. The separate small
signed deployment-receipt store remains visible while timeline metrics defer.

Audit-ledger connections use a bounded 1 GiB read-only SQLite page mapping to
avoid per-page filesystem round trips during full verification. This changes
the read path, not the SQLite, HMAC, append-only or authenticated-head checks.
SQLite retains its normal read fallback when mapping is unavailable. Native
read-only qualification verified all 400,996 events in 23.6 seconds.

DASH can promote a reviewed control-plane commit through one two-phase,
fail-closed workflow. The outcome is not merely “the deploy command returned
zero.” A successful receipt proves source provenance, map continuity, current
health/readiness, and recovery evidence under the same private HMAC trust
boundary as Change Intelligence.

Confidence: **high** for the implementation, receipt semantics, backup
verification, and map-continuity invariants covered by tests. Each live receipt
separately records whether those invariants passed for that change window.

## Outcome

An assured deployment proves all of these facts:

1. The staged file set exactly matches one 40- or 64-hex Git commit and a
   canonical SHA-256 manifest.
2. A full pre-change backup passed the normal verifier.
3. Every replaced live source file was preserved in a private rollback archive,
   including a record of paths that did not previously exist.
4. The server captured all configured game-map container IDs, states, and
   process start times before the live source tree changed.
5. Only the staged manifest files were atomically promoted.
6. Admin Panel and ingress passed their existing remote test/health path.
7. Prometheus reloaded its committed rule/configuration files in place.
8. No protected game-map container was recreated.
9. A game-map process already running at window start was not restarted.
10. Every `always-on` map stayed running with the same process start time.
11. Credential lifecycle trust state was initialized and its HMAC chain plus
    authenticated head verified before Desired State review.
12. Desired State was fully reviewed/sealed and had zero open findings.
13. The current fleet-wide response certification semantically covered every
    runbook and recovery contract in the committed policy.
14. Operational SLOs were healthy with zero open incidents.
15. Change Intelligence integrity passed with zero open incidents.
16. Prometheus had scraped readiness value `1`; a merely healthy direct
    collector response is insufficient.
17. A full post-change backup passed the normal verifier.
18. Desired State, Change Intelligence/readiness, and Operational SLO
    collectors produced at least two consecutive healthy samples after the
    admin-only recreation.
19. The receipt says both `recoveryExecuted=false` and
    `gameMutationExecuted=false`.
20. A final full backup created after completion contains the signed receipt.

Any false invariant makes `ready=false`. DASH signs and retains a failed
receipt when it can safely characterize the failure; it does not rewrite the
outcome green. An invalid/missing manifest, private path, missing backup,
tampered window, expired window, or unreadable required source fails before
finalization.

For large retained timelines, the readiness metric can verify the latest
certification event's HMAC, nested receipt digest, and current policy directly.
It continues to report timeline collection as deferred. Unrelated audit writes
cannot erase a valid certification between scrapes; an invalid receipt fails
closed. Deployment still independently requires the complete ledger integrity
check, desired-state attestation, SLO health, and current response readiness.

The convergence gate defaults to two healthy samples five seconds apart and a
300-second deadline. It samples desired-state attestation, change-intelligence
integrity, the current readiness certification, SLO health, and the
Prometheus-scraped readiness metric. Waiting for the scraped metric prevents a
false failed receipt immediately after Admin Panel recreation or a Prometheus
reload. Installations can tune the gate with
`DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_TIMEOUT_SECONDS` (30..1800),
`DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_POLL_SECONDS` (1..60), and
`DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_SAMPLES` (2..10). These settings change
how long the workflow waits; they do not remove or weaken receipt invariants.

## Trust And Safety Boundary

The workflow is production-host-gated to `DUNE_PRODUCTION_HOST` (`kspls0` by
default). The target host verifies `hostname` before any deployment action.

The local push helper stages files under a random mode-`0700` `/tmp` directory.
It does not overwrite the live tree over `rsync` or `scp`. The production
workflow validates the staged tree, captures pre-change evidence, creates both
recovery layers, starts the HMAC-authenticated window, and only then atomically
applies each manifest file.

Manifest paths:

- must be bounded repository-relative paths;
- cannot contain `..`, absolute paths, symlinks, or unsafe path components;
- cannot select `.env`, `config/secrets`, `.git`, `data`, `captures`, or
  `backups`;
- are capped at 256 files, 10 MiB per file, and 100 MiB total; and
- must match the exact committed Git blob before staging and again inside the
  remote stage.

After promotion, Admin independently re-hashes the same files through the
dedicated read-only `/source-workspace` bind. This is separate from the normal
read-write `/workspace` state/config submounts. Private and mutable paths remain
rejected by manifest validation even though the trusted Admin container already
holds the Docker socket and its normal backup/config authority.

The host workflow contains no raw `docker compose`, `docker restart`, or map
lifecycle call. It invokes `scripts/deploy-admin-panel.sh`, which recreates only
`admin-panel` and `admin-panel-ingress`. It runs the Landsraad/Coriolis validator
before and after deployment. Normal Desired State sealing, readiness
certification, backup verification, RBAC, exact confirmations, and audit paths
remain authoritative.

Dynamic maps may legitimately start from player demand or stop after their idle
retention expires during a window. That does not fail assurance when the same
container identity is retained. A container recreation always fails. A process
that was running before and remains running after must preserve its exact
`StartedAt`. Every `always-on` service must remain running continuously.

## Generate A Commit-Bound Manifest

Commit the complete change first, then generate the manifest from that exact
commit. By default, the file set is the committed diff from its first parent:

```bash
python3 scripts/deployment-assurance.py manifest \
  --commit HEAD \
  --reason 'Deploy reviewed control-plane release' \
  --output /tmp/dash-deployment-manifest.json

python3 scripts/deployment-assurance.py verify \
  --manifest /tmp/dash-deployment-manifest.json
```

Use repeated `--file` only for a deliberately narrower deploy. The staged
assurance runner plus the complete native backup-verifier dependency closure,
including the Admin entrypoint that dispatches signed evidence schemas, are
always added, even when unchanged, so no unmanifested helper code executes on
production. This lets a verifier/schema migration authenticate the
pre-change backup with exact commit-bound code before that code is promoted,
without weakening or bypassing recovery verification. Generation reads each blob
from the exact commit and refuses when the current workspace file differs. The
mode-`0600` document contains:

```json
{
  "schemaVersion": "dune-deployment-manifest/v1",
  "commit": "<exact commit>",
  "reason": "...",
  "files": [{"path": "admin/admin_panel.py", "sha256": "...", "bytes": 123}],
  "manifestSha256": "..."
}
```

## Push And Promote

When CPU affinity is enabled, review the host-generated overlay before deploying.
It must cover `docker-api-broker`, `admin-panel`, and `admin-panel-ingress`, as
well as running map services. Regenerate it with the native topology helper,
preserve existing CPU pool overrides, and review changed assignments before
applying. Keep this host-specific generated file outside Git; the normal
coverage and application checks remain mandatory.

Run from the reviewed source checkout:

```bash
scripts/push-assured-control-plane.sh \
  --manifest /tmp/dash-deployment-manifest.json \
  --reason 'Deploy reviewed control-plane release' \
  --host kspls0
```

The push helper first runs the complete native Admin test suite in the reviewed
checkout through `scripts/deploy-admin-panel.sh --test-only`. A failed test
prevents staging and production mutation. The deployment helper and the native
test files are manifest-bound so production never runs stale tests against a
newer Admin entrypoint. Production repeats its normal deployment checks. Ingress readiness requires two
consecutive successful native checks within a bounded startup window (300
seconds by default; `DUNE_ADMIN_DEPLOY_INGRESS_READY_SECONDS` permits 30–300).
Each check still requires a 401 response from unauthenticated detailed status
and successful direct/LAN health probes. This accommodates initial retained-
history verification without accepting a failed or unavailable auth response.
The five-minute default covers measured cold-start ledger verification under
storage load; the earlier 90-second window expired before authentication probes
recovered. A shorter configured window still fails closed at its own deadline.

The helper transfers the exact manifest files, including the assurance runner
and backup-verifier closure required to operate from staging. Every executable
support file is manifest-bound and promoted from the same commit; no loose
helper is accepted from the stage.

Backup and attestation validation can take minutes on busy storage. Start,
finish, credential trust initialization, Desired State reads/sealing, and
incident-readiness reads/certification use the same bounded
`DUNE_DEPLOYMENT_ASSURANCE_VERIFICATION_TIMEOUT_SECONDS` budget
(default 1800 seconds; permitted range 60–3600). This changes the transport wait,
not the validation or health gates. Start-response recovery also requires the
exact manifest digest, commit, and reason before adopting a durable window.
Shorter fixed waits previously abandoned a Desired State seal while its retained
history checks were still running. The shared bound prevents that premature
failure without accepting an incomplete seal or certification.
The private Admin ingress permits a 65-minute response window on the named
verification endpoints, covering the client's maximum one-hour budget with
transport margin. Ordinary routes retain a five-minute upstream wait. Request
header/body read limits, idle timeout, body-size limit, allowed methods, and
application authorization remain enforced. The server write deadline also
allows the longer responses; increasing only the client's timeout is insufficient.
Older status responses omit the manifest digest. The client then reads only
public window metadata through the running Admin verifier, which authenticates
the existing record with its configured HMAC key. Keys and signatures stay in
the runtime, and a failed verification cannot resume a deployment. Current
status responses include the digest directly.
If the client exhausts its recovery budget, check for a late matching window
before retrying; cancel that exact unused window if no promotion occurred.

Start and finish operations are durable even when a long verification closes
the HTTP response before the client receives JSON. The runner polls the signed
window/receipt store, requires one exact commit-and-reason match, and resumes
only that window. It never opens a replacement window merely because response
transport failed.

### Verifier-schema migration bridge

Normal deployments must let the workflow create its own pre-change backup. A
verifier-schema migration has one bootstrap problem: the running Admin process
may reject historical evidence that the reviewed staged verifier knows how to
authenticate. For that case only, create and independently verify a fresh
recovery-complete backup, preserve the original full evidence backup, and pass
the confined bridge set explicitly:

```bash
scripts/push-assured-control-plane.sh \
  --manifest /tmp/dash-deployment-manifest.json \
  --reason 'Migrate the signed evidence verifier' \
  --host kspls0 \
  --pre-change-backup backups/<bridge-id>
```

The runner resolves the path beneath `workspace/backups`, rejects escapes and
missing directories, and re-verifies it with the exact manifest-bound staged
verifier. The running Admin process still performs its own admission check.
Only the pre-change recovery layer may use the bridge; post-change and final
receipt backups are always newly created and must pass the complete promoted
verifier. This option does not skip source rollback, map continuity, health,
evidence, or final-backup gates.

On an already staged production host, the lower-level entry point is:

```bash
PYTHONPATH=/tmp/dash-stage/admin \
  /tmp/dash-stage/scripts/assured-control-plane-deploy.sh \
  --manifest /tmp/dash-stage/deployment-manifest.json \
  --reason 'Deploy reviewed control-plane release' \
  --stage /tmp/dash-stage \
  --workspace /home/keith/Documents/code/DuneAwakeningSelfHost \
  .env
```

The host runner creates three verified backup sets:

- pre-change recovery state referenced by the open window;
- post-change recovery state referenced by the signed receipt; and
- final state containing the completed signed receipt in
  `operator-evidence.tgz`.

Before it creates or validates the pre-change set, the runner acquires
`backups/admin-panel/operation.lock` and holds it through final verification.
Panel backups and standalone `scripts/backup-state.sh` use the same inode.
Scheduled backup runs and due browser maintenance therefore defer instead of
snapshotting or restarting through a changing deployment; maintenance defers
before player disconnect or service control. Standalone host backups wait up to
`DUNE_OPERATION_LOCK_WAIT_SECONDS`. Nested backups in this workflow inherit the
lock and do not deadlock by reacquiring it. See
[`automatic-backups.md`](automatic-backups.md) and
[`operations-calendar.md`](operations-calendar.md).

The manifest-bound control plane also includes `admin/alert_inbox.py`; the
post-recreation convergence proof requires its Prometheus series and the final
verified backup contains its SQLite history. See
[`alert-inbox.md`](alert-inbox.md).

Each required backup gets at most three attempts. A live file changing during
tar creation, failed dump, failed archive, or failed verifier remains a failed
attempt; the workflow never suppresses the error or relabels a partial set.

The runner requires consecutive healthy samples before finalization. The
final API sample remains authoritative: if a health gate changes afterward,
the API returns `waiting-for-health`, names the failed gates, and leaves the
signed change window open. The runner retries within the bounded convergence
timeout. Source-manifest and map-continuity failures are not retried; they
still produce a signed failed receipt for operator review.

It also creates a mode-`0600` source rollback archive under
`backups/deployments/`. The archive has `rollback-manifest.json` plus every live
file that existed before promotion. Restore source through a separately
reviewed maintenance action; assurance never auto-rolls back a running server.

## API

Read status and bounded receipt summaries:

```text
GET /api/ops/deployment-assurance
```

Two-phase writes require `infrastructure.write` and exact confirmations:

```text
POST /api/ops/deployment-assurance
{
  "action":"start",
  "commit":"<exact commit>",
  "reason":"...",
  "manifest":{"schemaVersion":"dune-deployment-manifest/v1","files":[...]},
  "preChangeBackupPath":"<relative backup set>",
  "sourceRollbackArchive":"deployments/<archive>.tgz",
  "sourceRollbackSha256":"<64 hex>",
  "staged":true,
  "confirm":"START ASSURED CHANGE WINDOW"
}

POST /api/ops/deployment-assurance
{"action":"finish","windowId":"deployment-window-...","backupPath":"<relative backup set>","confirm":"FINALIZE ASSURED CHANGE WINDOW"}

POST /api/ops/deployment-assurance
{"action":"cancel","windowId":"deployment-window-...","reason":"...","confirm":"CANCEL ASSURED CHANGE WINDOW"}
```

Clients cannot submit container snapshots, health results, readiness outcomes,
or receipt digests. The server derives those values. Open-window JSON is
HMAC-authenticated, mode `0600`, expires after six hours, and cannot be reused
after completion/cancellation. When Admin Panel runs as root, it transfers the
mode-`0700` state/evidence directories and mode-`0600` files to
`DUNE_HOST_UID/GID`, allowing the production host workflow to create and remove
its self-reload lock without broadening permissions.

## Signed Receipt And Backup Verification

Completed artifacts use schema `dune-deployment-assurance/v1` and live under:

```text
backups/operator-evidence/deployment-assurance-<id>.signed.json
```

The inner `receiptSha256` covers the complete normalized receipt excluding only
that digest. Verification then enforces semantic agreement between:

- file rows and `manifestSha256`;
- per-service continuity rows and their aggregate booleans;
- health values and matching invariants;
- backup verification and `backupVerified`;
- every invariant and top-level `ready`;
- recovery/game-mutation flags; and
- receipt identity, commit, window, timestamps, and source rollback proof.

The outer HMAC covers generation time, schema, key fingerprint, and complete
receipt. Both native/admin and host backup verifiers dispatch mixed
`operator-evidence.tgz` members by schema, so incident capsules and deployment
receipts coexist. One invalid artifact fails the whole backup verification.

## Dashboard, Metrics, And Alerts

Open **Infrastructure → Assured Change Windows**. The panel shows the latest
commit/outcome, source/map/readiness/backup proof, open windows, receipt history,
and the exact protected/strict service policy. It exposes no token or HMAC key.

The existing label-free Change Intelligence metrics endpoint adds:

```text
dash_deployment_assurance_collector_up
dash_deployment_assurance_latest_ready
dash_deployment_assurance_last_completion_timestamp_seconds
dash_deployment_assurance_open_windows
dash_deployment_assurance_overdue_windows
```

Prometheus alerts on invalid state/evidence, no passing latest receipt, a
receipt older than seven days, or an expired open window. No commit, path,
operator, service, backup, or digest is used as a metric label.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUNE_DEPLOYMENT_ASSURANCE_ENABLED` | `true` | Enables API, dashboard, receipts, and metrics. |
| `DUNE_DEPLOYMENT_ASSURANCE_STATE_DIR` | `/workspace/backups/deployment-assurance` | Private HMAC-authenticated window state. |
| `DUNE_DEPLOYMENT_ASSURANCE_WORKSPACE` | `/source-workspace` | Complete read-only source tree used for independent post-apply hashing. |
| `DUNE_DEPLOYMENT_ASSURANCE_PROMETHEUS_URL` | `http://prometheus:9090` | Internal query endpoint proving readiness was scraped. |

Deployment receipts intentionally reuse the Change Intelligence HMAC secret and
operator-evidence directory. Key recovery therefore follows the existing
matching-key Change Intelligence backup contract.

## Failure Handling

- **manifest generation rejects a file:** commit it first and ensure the
  workspace byte-for-byte matches that commit. Never edit the manifest digest.
- **stage verification fails:** discard the stage and transfer it again; no live
  file has changed.
- **start fails:** verify the pre-change backup, rollback archive, HMAC key,
  Docker socket, and manifest bounds. No source apply occurs before success.
- **workflow aborts after start:** the shell trap attempts an explicit
  cancellation. If the admin endpoint is unavailable, the window expires and
  the overdue alert remains visible.
- **source/map/health invariant fails:** retain the signed failed receipt and
  diagnose the exact false fields. Do not manually flip them.
- **health convergence times out:** inspect the last printed collector sample.
  The window is cancelled before finalization, so a transient collector does
  not become a signed failed receipt.
- **desired state is not attested:** review every finding; the host workflow
  seals the complete snapshot only through the existing exact-confirmation
  route.
- **response readiness is incomplete:** repair diagnostics or the named capability/gate
  contract separately. Assurance never enables a gate.
- **backup fails:** the window cannot be ready. Preserve both pre-change layers
  while investigating.
- **rollback required:** verify the source archive and pre-change backup, plan
  the rollback separately, and run another assured window for the rollback
  commit. Automatic rollback could compound a partial external failure and is
  deliberately not inferred.

## Validation

```bash
make test-deployment-assurance
make test-change-intelligence
make test-admin-panel-safe-surfaces
make test-operational-borrowing
docker compose --env-file .env.example config --quiet
make validate
```

Coverage includes manifest/path/size bounds, exact Git-blob binding, stage
verification, source rollback creation, atomic apply, HMAC window tampering,
expiry/cancellation, dynamic-map starts, protected container recreation,
running/strict process restart, health/backup failure, nested/outer tampering,
mixed signed-evidence archives, RBAC/confirmation, dashboard wiring, metrics,
and production-host/map-lifecycle guards.

### Local verification read views

Backup verification stages the exact recovery point into a private local
`/var/tmp` directory before running the existing native checks. This avoids
random SQLite reads on busy archive storage. Copies are bounded to 4,096 entries
and 8 GiB, require an additional 1 GiB of free space, reject links and special
files, and preserve the recovery-point name. Source identities must stay stable
through copying and validation. Copied files are hashed while copying and again
after verification; a checker cannot silently repair recovery data and claim it
passed. SQLite reader indexes and empty WAL files are transient; nonempty WAL
files remain part of the checked recovery data.

The temporary view is removed after the native verifier returns. The authoritative
backup remains in its archive location. Every existing integrity, HMAC, manifest,
and recovery-evidence check remains mandatory. A live qualification of the retained
1.8 GiB SQLite recovery set passed the complete native verifier in 82 seconds;
that qualifies this read path and does not claim a production image rollout.
