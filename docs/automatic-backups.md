# Automatic Full Backups

DASH can create the same coverage-checked recovery set from the Infrastructure
page manually or on a retained schedule. The scheduler is an admin-control-plane
worker; it does not stop, start, recreate, or restart game-map containers.

## Recovery contract

Every panel-created full backup has a `manifest.json` and a
`dash-full-backup-coverage/v1` declaration. Coverage is derived from the
features and durable sources enabled when the snapshot starts. A run is not
successful unless every required artifact was captured and the resulting set
passes `scripts/verify-backup.sh` or the equivalent native verifier in the
minimal Admin image.

The always-required layers are the PostgreSQL custom-format dump and combined
config/environment archive. Server saved state and RabbitMQ are required when
their mounted sources exist. Enabled durable control-plane stores are also
required, including:

- Community Rewards, moderation, Base Gallery, Prometheus alert-inbox, and
  ecosystem peer-watch SQLite state;
- Operational SLO, Capacity Intelligence, Desired State, and Change
  Intelligence ledgers;
- Feature Readiness history and its config-bound HMAC key;
- Credential Lifecycle database/authenticated-head pair;
- change-approval database/key pair when four-eyes control is enabled;
- audit-ledger database/key/authenticated-head set;
- Canary Autopilot scheduler state when initialized; and
- signed operator evidence plus the latest RabbitMQ recovery receipt when
  available.

SQLite sources use the online backup API rather than copying live WAL files.
Multi-artifact authenticated ledgers are verified as a copied set. The Admin
container transfers private artifacts to `DUNE_HOST_UID`/`DUNE_HOST_GID` with
directory mode `0700` and file mode `0600`, so host-side restore tooling can
read them without widening permissions.

The configuration archive stores private recovery files as regular files under
`config/secrets`. In the Admin container, a host-only `config/secrets` symlink
can be unavailable while the dedicated `/workspace-secret-files` mount is
readable. The backup uses that mounted source and replaces the unavailable link
in the archive with a private directory and file contents. Nested links and
special files are rejected; the selected private tree is limited to 4,096
members and 16 MiB. File rotation during capture fails the attempt. Archives
start with mode `0600`, including failed partial attempts, and existing archive
files are never overwritten. The normal copied-ledger and recovery-material
verification remains mandatory.

## Collision prevention

Panel backups, executing browser maintenance, `scripts/backup-state.sh`, and
`scripts/assured-control-plane-deploy.sh` coordinate through:

```text
backups/admin-panel/operation.lock
```

The lock is an OS `flock` on the same bind-mounted inode for host and container
processes. An assured deployment owns it for the complete workflow, including
pre-change, post-change, and final evidence backups. Its nested
`backup-state.sh` calls inherit the lock instead of reacquiring it. A standalone
host backup waits up to `DUNE_OPERATION_LOCK_WAIT_SECONDS` (default 1,800
seconds). The panel uses a nonblocking acquisition: a scheduled run records a
deferral, moves `nextRun` to the configured retry window, and does not count the
collision as a failed backup.

Executing maintenance owns the same lock across preflight, player disconnect,
stop, verified backup, update, restart, recovery, and online proof. A due job
that finds the lock occupied returns to its scheduled state and retries after a
bounded delay without disconnecting players or recording a false maintenance
failure. The conflict-aware Operations Calendar separately rejects predictable
backup/maintenance overlap before the job is stored; see
[`operations-calendar.md`](operations-calendar.md).

Do not configure different lock paths for host and container execution. Doing
so removes the serialization guarantee.

## Schedule and retry behavior

Configure the schedule from **Infrastructure → Automatic Full Backups**:

| Setting | Bounds | Meaning |
| --- | --- | --- |
| First run local time | `HH:MM` | Initial local wall-clock run |
| Interval hours | 1–744 | Normal interval after a successful run |
| Failure/lock retry minutes | 1–1,440 | Short retry after failure or lock deferral |
| Verification attempts | 1–5 | Rechecks one immutable new snapshot; it does not recreate the backup between attempts |
| Retention days | 0–3,650 | Deletes only successful paths recorded by this scheduler; zero is unlimited |

The persisted schedule is
`backups/admin-panel/backup-schedule.json`. A successful run clears
`consecutiveFailures`, records `lastSuccess`, and schedules the normal interval.
A coverage, creation, or verification failure retains its exception type,
backup path when known, coverage gaps, and the final 8 KiB of verifier stdout
and stderr; it then schedules the short retry. Lock deferrals are tracked
separately and never overwrite the last backup result.

`DUNE_BACKUP_VERIFY_RETRY_SECONDS` controls the delay between verification
attempts and defaults to one second. Retries are intended for transient
filesystem/consumer races; the coverage declaration prevents retries from
turning an incomplete set into a passing full backup.

## Operator surfaces

`GET /api/ops/backups` returns schedule configuration, ISO timestamps, runtime
state, last result, failure count, and deferrals. The Infrastructure page shows
verified/failed/awaiting-first-run state and the retained diagnostic document.
Schedule changes require the existing exact confirmation and backup mutation
gate.

The Change Intelligence metrics endpoint exports label-free series:

```text
dash_backup_schedule_collector_up
dash_backup_schedule_enabled
dash_backup_schedule_worker_running
dash_backup_schedule_active
dash_backup_schedule_last_run_ok
dash_backup_schedule_last_run_timestamp_seconds
dash_backup_schedule_last_success_timestamp_seconds
dash_backup_schedule_next_run_timestamp_seconds
dash_backup_schedule_overdue_seconds
dash_backup_schedule_consecutive_failures
dash_backup_schedule_deferrals_total
```

Prometheus alerts on unreadable scheduler state, a stopped enabled worker, a
failed latest run, and a run overdue beyond the retry window. The signed
Operator Briefing includes automatic backup reliability as an independent
critical source; schedule changes and completed attempts invalidate the prior
briefing and wake its coalescing worker. These two low-frequency backup events
force a replacement signed receipt even when the categorical health state did
not change, so timestamps and the next-run detail cannot remain stale behind a
still-`verified` source fingerprint.

## Troubleshooting

1. Inspect `lastResult.verification.stderr` and `coverage.missing` in the
   Infrastructure page or API.
2. Check whether a deployment owns the operation lock. A lock collision should
   appear as a deferral, not a failure.
3. Run `scripts/verify-backup.sh backups/<set>` from the host. Panel-created
   audit artifacts must be owned by the configured host operator and remain
   mode `0600`.
4. Repair only the named source or verifier. Preserve the failed backup for
   evidence; the scheduler will retry on its short interval.
5. Use the isolated PostgreSQL and RabbitMQ restore drills to prove recovery.
   Structural verification alone is not a restore rehearsal.

## Validation

```bash
bash -n scripts/backup-state.sh scripts/assured-control-plane-deploy.sh scripts/verify-backup.sh
python3 scripts/test-admin-panel-safe-surfaces.py
python3 scripts/test-deployment-assurance.py
make validate
```

For a production canary, verify `hostname` is `kspls0`, create one due
scheduled run through the guarded Admin path, verify its coverage declaration
and backup set from both the container and host, then confirm the metrics and
signed briefing have recovered. No map lifecycle command is part of this test.
