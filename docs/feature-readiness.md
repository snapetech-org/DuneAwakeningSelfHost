# Feature Readiness Control Center

DASH exposes one authoritative, secret-safe answer to a question that feature
flags alone cannot answer: which capabilities are disabled, partially enabled,
configured, running, blocked by an external credential, degraded at runtime, or
still waiting for an explicit canary?

The control center is read-only. It does not enable a gate, create a credential,
start a service, run a canary, execute a game command, or change an incident. It
links each finding to the existing guarded operator surface that owns recovery.

## Outcome

Open **Infrastructure → Feature Readiness Control Center**. The matrix combines:

- every gate used by `scripts/enable-feature-parity.sh`;
- credential **presence** without credential values;
- required repository/configuration artifacts and their bounded minimum size;
- current Compose service state;
- feature dependencies;
- explicit runtime probes for the database, governance chain, operational
  evidence stores, autoscaler, metrics collectors, native command transport,
  moderation/community ledgers, authentication, webhooks, public directory,
  public-IP monitor, and backup encryption; and
- a separate canary classification so configured code is never mislabeled as
  end-to-end runtime proof.

The current catalog groups the complete activation set into trust,
infrastructure, retained evidence, adaptive maps, metrics, player/world/content
administration, creator tooling, moderation, community rewards, Discord,
federated login, webhooks, public discovery, multi-Sietch topology, public-IP
repair, encrypted backups, and networkless RabbitMQ recovery proof.

Ecosystem Peer Watch is a distinct Discovery feature. Its runtime probe proves
that the retained SQLite store is valid, the worker started, the authoritative
checked-in peer catalogue matches the last completed observation, and the poll
is fresh. Revision drift and isolated source errors remain explicit operational
findings; they do not masquerade as missing implementation. See
[`peer-watch.md`](peer-watch.md).

Credential Lifecycle is a distinct Trust feature. Its probe evaluates 19
activation-aware sources, private permissions, observed rotation history, and
newest-backup coverage without returning values or fingerprints. External
provider material remains an explicit pending state. See
[`credential-lifecycle.md`](credential-lifecycle.md).

The catalog is [`config/feature-readiness.json`](../config/feature-readiness.json).
Evaluation is implemented by
[`admin/feature_readiness.py`](../admin/feature_readiness.py). Runtime probes and
the authenticated API live in the Admin Panel.

Repository artifacts are checked through the complete read-only
`DUNE_DEPLOYMENT_ASSURANCE_WORKSPACE` mount (normally `/source-workspace` in
the Admin container). Runtime state remains under `/workspace`; this prevents
the deliberately partial runtime mounts from falsely reporting committed
Compose overlays as absent.

Directory verification uses OpenSSL when the executable is available. The
minimal vendor Admin image does not currently ship that executable, so DASH
also includes a strict RFC 8032 Ed25519 verification fallback. It rejects
non-canonical points, non-prime-order points, identity points, out-of-range
scalars, and altered payloads/signatures; signing and key generation remain on
the private host-side renderer.

## States

| State | Exact meaning |
| --- | --- |
| `ready` | Required gates, credentials, artifacts, services, dependencies, and runtime probe pass; any required live canary is proven. |
| `canary-pending` | Implementation is loaded and configured, but the declared operator/provider live canary remains unproven. |
| `disabled` | The optional feature's primary gate is intentionally inactive. This is not an outage. |
| `partial` | At least one gate in the feature group is active, but the required group is incomplete. |
| `blocked` | An active feature is missing a required artifact, local credential, or dependency. |
| `degraded` | Configuration is present, but a required service or explicit runtime probe is unhealthy. |
| `external-blocked` | An active integration still needs an operator-owned external provider credential. |

An `operator-canary-pending` catalog row promotes to `ready` only when its
allowlisted runtime probe explicitly returns `state=canary-proven`. A merely
healthy/configured probe remains pending. RabbitMQ recovery uses this contract:
the implementation can be fully loaded without claiming recovery until both
copied broker states boot successfully and the HMAC-anchored receipt history
verifies.

Community Rewards uses the same evidence rule. Its probe remains pending until
the active policy has a current, semantically valid HMAC receipt from the
disposable link→webhook→wallet→purchase→delivery→engagement→track→ledger
transaction. Policy drift or receipt expiry removes proof without altering live
community or game state; see
[`community-rewards-canary.md`](community-rewards-canary.md).

Creator/Modding also uses the evidence rule. Its probe binds the active
implementation modules, both catalogs, and all three `UserGame` targets, then requires a current signed
disposable lifecycle covering base export/gallery, retirement guards, preset
apply/rollback, Landsraad protection, cosmetic planning, and confined addon
install/removal. Input drift or expiry returns the row to `canary-pending`; see
[`creator-modding-canary.md`](creator-modding-canary.md).

Public-IP Repair uses the same rule plus an explicit live arming condition. Its
signed canary runs the real environment rewrite, OpenSSL SAN rotation, restart
handoff/retry, and timer rendering in disposable state. The row remains
`canary-pending` when the proof is missing/stale/input-drifted or the monitor is
still dry-run. Invalid signed evidence degrades it. It reaches `ready` only with
a current passing proof and `DUNE_PUBLIC_IP_MONITOR_DRY_RUN=false`; see
[`public-ip-repair-canary.md`](public-ip-repair-canary.md).

`overall=attention` means at least one active feature is `partial`, `blocked`,
`degraded`, or `external-blocked`. Disabled optional integrations and explicitly
visible pending canaries do not masquerade as runtime failures.

Readiness is evidence-specific. For example, a `ready` native-command row proves
the gates, private command token, Admin RMQ service, and already promoted
runtime transport. It does not claim that every possible player command was
re-run against a live player during the current request. Where a disposable or
provider-backed live canary is still required, the row remains
`canary-pending` or `external-blocked`.

## API

The authenticated read endpoint is:

```text
GET /api/ops/feature-readiness
GET /api/ops/feature-readiness?refresh=1
GET /api/ops/feature-readiness/history?limit=100
```

The normal response is cached for 30 seconds to keep dashboard loads and
Prometheus scrapes cheap. `refresh=1` recomputes service inventory, artifact
metadata, credential presence, and runtime probes immediately. The TTL is
bounded to 5-300 seconds with:

```env
DUNE_FEATURE_READINESS_CACHE_TTL_SECONDS=30
```

Each feature row contains its group, documentation, remediation surface,
individual gate booleans, credential-presence booleans, file sizes, service
states, dependency states, runtime-probe result, and canary contract. It never
contains an environment value, secret-file content, OAuth secret, Discord bot
token, database password, Admin token, or native-command token.

The response explicitly returns `secretValuesReturned=false`.

## Tamper-Evident Transition History

Point-in-time readiness cannot answer whether a deployment introduced a
regression, whether an operator actually recovered it, or whether somebody
rewrote the evidence afterward. When
`DUNE_FEATURE_READINESS_HISTORY_ENABLED=true`, every newly observed feature
state vector is canonicalized and compared with the preceding vector. Exact
duplicates create no row. A real change is classified as `regression`,
`improvement`, `mixed`, or neutral `change`; the first vector is `baseline`.

Each append-only SQLite event contains only feature IDs, readiness states,
active booleans, summary counts, the exact per-feature transitions, observation
time, source, and the active assured-deployment commit when available. It does
not retain gate values, environment values, credential contents, player data,
host paths, or probe error text. The event HMAC covers its sequence, complete
canonical snapshot, transition set, deployment correlation, and previous HMAC.
The store verifies the full chain before appending, and SQLite triggers reject
updates and deletes.

Configuration defaults are:

```env
DUNE_FEATURE_READINESS_HISTORY_ENABLED=true
DUNE_FEATURE_READINESS_HISTORY_DATABASE=/workspace/backups/feature-readiness/history.sqlite3
DUNE_FEATURE_READINESS_HISTORY_HMAC_SECRET_FILE=/workspace-secret-files/feature-readiness-history-hmac.secret
```

The parity activator generates the 32-byte secret with mode `0600`. The store
can also initialize it atomically for a clean default deployment. Keep the
database and key together: a replacement key does not repair or re-authorize an
existing chain.

The Infrastructure card shows transition sequence, time, deployment commit,
classification, ready/problem counts, and exact state changes. The dedicated
authenticated history endpoint returns at most 1,000 newest events. Query
limits do not weaken verification: the complete chain is always checked.

Full backups take a SQLite online snapshot and include the matching HMAC key in
the private config archive. `scripts/verify-backup.sh` extracts only that
bounded member into a private temporary directory and verifies the complete
snapshot chain before accepting the backup.

## Metrics

The existing private `/metrics/change-intelligence` scrape now also exports:

```text
dash_feature_readiness_ok
dash_feature_readiness_total
dash_feature_readiness_active
dash_feature_readiness_active_problems
dash_feature_readiness_ready
dash_feature_readiness_canary_pending
dash_feature_readiness_disabled
dash_feature_readiness_partial
dash_feature_readiness_blocked
dash_feature_readiness_degraded
dash_feature_readiness_external_blocked
dash_feature_readiness_history_valid
dash_feature_readiness_history_events_total
dash_feature_readiness_history_regressions_total
dash_feature_readiness_history_improvements_total
dash_feature_readiness_history_head_sequence
dash_feature_readiness_history_last_regression_timestamp_seconds
```

The `canary-autopilot` catalog row additionally proves that the automatic
isolated-proof worker is loaded, its strict scheduler state and target evidence
collectors are valid, at least one target is active, and no active proof remains
due. It does not substitute scheduler state for the Community, Creator/Modding,
or public-IP signed receipt verifier. See
[`canary-autopilot.md`](canary-autopilot.md).

The `operations-briefing` row proves that the change-aware synthesis worker is
running and its latest HMAC receipt remains valid, age-current, and bound to the
current categorical source fingerprint. Initial startup is explicitly
`collector-starting`; a retained stale, tampered, or input-drifted receipt fails
the runtime probe. The briefing consumes Feature Readiness but its probe reads
only cached receipt/runtime state, preventing recursive evaluation. See
[`operations-briefing.md`](operations-briefing.md).

The catalog includes `player-impact-maintenance`. Its runtime probe verifies
that the shared moderation database accepted aggregate observations and that
the planner can produce a bounded recommendation. `policy-fallback-learning`
is a ready runtime state with low evidence confidence, not a false measured
claim; the Operations page exposes the distinction.

Metrics are deliberately label-free. Feature IDs, credential names, service
names, operator identities, paths, and failure text remain in the authenticated
API rather than becoming public/high-cardinality labels.

`DashFeatureReadinessCollectorInvalid` alerts when evaluation returns no
catalog, and `DashFeatureReadinessActiveProblems` alerts when an active feature
remains partial, blocked, degraded, or externally blocked for five minutes.
`DashFeatureReadinessHistoryInvalid` alerts on SQLite/HMAC failure, and
`DashFeatureReadinessRegression` reports a newly observed regression without
putting feature IDs into Prometheus labels.

## Catalog Contract

Every feature declares:

- stable `id`, title, group, description, and documentation path;
- one `primaryGate` and the complete required `gates` set;
- credential environment keys whose values are checked only for presence;
- confined relative regular-file requirements and minimum byte counts;
- required Compose service names;
- an allowlisted runtime `probe` identifier;
- other catalog feature IDs that must be `ready` first;
- one canary state; and
- the existing surface and concise recovery instruction.

Catalog loading fails closed on unknown fields, duplicate/invalid IDs, invalid
environment or service names, absolute/traversing paths, invalid byte bounds,
unknown dependencies, self-dependencies, or unsupported canary states.
Dependency cycles are also refused.

The repository test extracts the `keys=(...)` block from
`scripts/enable-feature-parity.sh` and requires every activated gate to appear
in at least one catalog feature. Adding a new parity gate without readiness
coverage therefore breaks validation instead of silently creating another
untracked toggle.

## Recovery Workflow

1. Refresh the matrix once to rule out the bounded cache.
2. Read the exact failing gate, artifact, service, dependency, or probe.
3. Open the row's documented remediation surface.
4. Use that surface's existing capability, feature gate, exact confirmation,
   backup, hostname, post-start hook, and change-contract controls.
5. Recheck the matrix and the subsystem's own detailed status.

If transition integrity fails, stop recording, preserve both files, and restore
the newest backup whose `feature-readiness-history.sqlite3` verifies against
the HMAC key inside its matching config archive. Never delete a bad row, edit a
classification, or rotate the key in place. A deliberate clean-slate history
requires archiving the old database/key pair together and initializing a new
pair with an explicit operational record outside this ledger.

Do not turn a disabled optional external integration into a false outage. Do
not treat `canary-pending` as failure or as proof. Do not repair a failed
evidence ledger by generating a new key; follow its backup-specific recovery
contract.

## Validation

Run the focused tests with:

```bash
make test-feature-readiness
make test-feature-readiness-history
make test-operations-briefing
make test-admin-panel-safe-surfaces
```

The tests cover every state, dependency failure, secret non-disclosure,
catalog confinement/validation, canonical state snapshots, deduplication,
regression/improvement/mixed classification, append-only enforcement, tamper
failure, label-free metrics, documentation links, and complete parity-activator
gate coverage. `make validate` includes all three suites.

Production activation requires only an assured Admin/control-plane deployment.
The feature itself has no mutation gate and does not require or perform a game
map lifecycle action.
