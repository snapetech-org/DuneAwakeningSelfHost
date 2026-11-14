# Isolated Backup Restore Drills

DASH can prove that its newest PostgreSQL dump is recoverable without stopping
the farm, connecting to the live database, publishing a port, or retaining a
second database. This closes the gap between archive inspection and an actual
restore.

`scripts/verify-backup.sh` remains the fast structural check. A restore drill is
the stronger test: it starts a disposable PostgreSQL container, restores the
archive, exercises the Dune schema, creates another custom-format dump, and
destroys the container.

This document covers PostgreSQL recovery. DASH separately proves both RabbitMQ
Mnesia backups by booting copied broker state in sequential no-network
containers; see [`rabbitmq-restore-drills.md`](rabbitmq-restore-drills.md).

## Quick Start

Run the newest regular `.dump` beneath `backups/`:

```bash
./scripts/backup-restore-drill.py
```

Select an exact confined dump:

```bash
./scripts/backup-restore-drill.py \
  --source backups/admin-panel/maintenance/<backup-id>/<database>.dump
```

Inspect the latest proof without starting anything:

```bash
./scripts/backup-restore-drill.py --status
```

The command exits zero only when both recovery integrity and the configured
recovery-point/recovery-time policy pass. A valid but old backup is reported as
`integrityOk=true`, `policyOk=false`, and `ok=false`; staleness is not hidden as
corruption.

## Isolation Contract

Every drill container is created directly through the local Docker Engine API
with all of these controls:

- `NetworkMode=none`; no Docker network, DNS, published port, or live database
  route exists;
- a read-only root filesystem;
- the configured non-root host operator UID/GID (the Funcom entrypoint is
  verified to initialize under an arbitrary non-root numeric identity);
- every Linux capability dropped and `no-new-privileges` enabled;
- fixed CPU, memory, swap, PID and ephemeral-PGDATA limits;
- the selected dump is copied to a mode-`0400` staging file owned by the
  container UID/GID, SHA-256 checked, mounted read-only at
  `/drill/source.dump`, checked again inside the container, and deleted during
  cleanup; the original dump's permissions are never weakened;
- minimal mode-`0400` passwd/group files give the arbitrary numeric operator
  identity a local name required by PostgreSQL client tools; both are mounted
  read-only and deleted with the staging copy;
- PostgreSQL data, its Unix socket, and `/tmp` held only in bounded tmpfs;
- restart policy `no` and unconditional forced removal in the cleanup path.

After startup, DASH inspects the created container and fails the drill if
Docker did not apply every critical isolation control. It never pulls an image.
The pinned image must already be present, so an unavailable registry cannot
silently change the recovery environment.

The drill never calls the live PostgreSQL service. Receipts explicitly record
`liveDatabaseTouched=false`.

## What Is Proven

The engine performs these stages in order:

1. Resolve a regular `.dump` beneath the workspace `backups/` root. Absolute
   escapes, traversal, and symlink components fail closed.
2. Hash the complete source archive with SHA-256 and record its mtime and size.
3. Remove only stopped or stale containers carrying both DASH's exact restore
   label and name prefix.
4. Start the no-network PostgreSQL 17.12 container and inspect its applied
   isolation.
5. Run `pg_restore --list` on the source.
6. Run `pg_restore --exit-on-error --no-owner --no-privileges` into a fresh
   `drill` database.
7. Require the core Dune tables, native `dune.base_backup_save_from_totem`,
   `dune.get_player_pawn`, `dune.update_death_location`, and
   `dune.admin_move_offline_player_to_partition` functions, zero invalid
   indexes, and zero unvalidated constraints.
8. Read exact counts from actors, player state, world partitions, farm state,
   items, inventories, building instances, and recoverable base backups. Actor
   and partition populations must be nonzero, and player-state rows may not
   exceed actor rows.
9. Inside one transaction, select an Alive pawn, invoke the native life-state
   path to `Dead`, verify the death location materialized, invoke it back to
   `Alive`, verify the death location cleared, and roll the whole transaction
   back. This is the semantic canary for guarded offline recovery; no tested
   state remains even in the disposable clone.
10. Inside a second transaction, select an explicitly Offline player with a
    valid pawn and partition, move the pawn by one X unit through the native
    offline teleport function, verify its persisted map/dimension/partition/
    XYZ plus both Offline predicates, and roll the transaction back.
11. Run `vacuumdb --analyze-only` to read and analyze the restored relations.
12. Create a second custom-format `pg_dump`, list it with `pg_restore`, and
    require a nonempty archive.
13. Remove the container and write a private receipt.

This proves the PostgreSQL layer and Dune database invariants. It does not
claim that RabbitMQ, server-saved files, configuration archives, TLS identity,
or an offsite provider have been restored. Those layers remain covered by
`restore-state.sh`, backup manifests, structural verification, and deliberate
full disaster-recovery exercises.

## Receipts And Evidence

Receipts live under:

```text
backups/admin-panel/restore-drills/<UTC>-<nonce>.json
backups/admin-panel/restore-drills/latest.json
```

The directory is mode `0700`; receipt files and the concurrency lock are mode
`0600`. When invoked from the root-running admin container, ownership is handed
to `DUNE_HOST_UID` / `DUNE_HOST_GID` so the unprivileged host timer and the
dashboard share one lock and receipt chain. Each receipt includes:

- source-relative path, byte count, mtime, age, and SHA-256;
- PostgreSQL image and measured ready/restore/total durations;
- the applied isolation evidence;
- required-schema results, core row counts, database size, index/constraint
  state, rolled-back native player life-state and offline-teleport semantic
  proofs, analyze result, and round-trip archive size;
- cleanup outcome and stale-container removals;
- separate integrity and RPO/RTO policy verdicts;
- the prior receipt hash and its own canonical SHA-256.

`--status` recomputes every displayed receipt hash. `latest.json` is an atomic
copy of the newest receipt, not a symlink. Receipt retention defaults to 1,000;
removing the oldest local entries does not rewrite surviving receipts or their
external chain references.

No database password, admin token, dump contents, Docker environment dump, or
absolute source path is stored in a receipt.

## Dashboard

Infrastructure → Recovery Proof shows the latest receipt, source age, restore
and total duration, hash and isolation verdicts, configured RPO/RTO targets,
resource limits, and recent history.

Read status:

```text
GET /api/ops/restore-drill
```

Queue the newest dump in a background worker:

```json
POST /api/ops/restore-drill
{"confirm":"RUN ISOLATED RESTORE DRILL"}
```

Execution requires all of:

- an identity with `infrastructure.write`;
- the normal master mutation gate;
- `DUNE_RESTORE_DRILL_ENABLED=true`;
- `DUNE_ADMIN_RESTORE_DRILL_EXECUTION_ENABLED=true`;
- the exact confirmation phrase;
- the local Docker socket.
- an absolute `DUNE_RESTORE_DRILL_HOST_WORKSPACE` when queueing from the admin
  container, so Docker receives the host path of the private staging file.

The HTTP request returns `202 Accepted` after queueing. A process lock and a
filesystem lock prevent overlap with dashboard, CLI, or timer runs.

## Automatic Daily Proof

Install the hardened system service and persistent timer as the normal DASH
operator:

```bash
./scripts/install-backup-restore-drill-timer.sh .env
```

The timer runs daily at 04:30 local time with up to 15 minutes of jitter. The
service is restricted to Unix sockets, uses a read-only home/system view, may
write only the workspace backup directory, has no capabilities, and uses the
operator's Docker-socket group. Check it with:

```bash
systemctl list-timers dune-backup-restore-drill.timer --all --no-pager
systemctl status dune-backup-restore-drill.service --no-pager
journalctl -u dune-backup-restore-drill.service -n 200 --no-pager
```

Run one immediately without changing the timer:

```bash
sudo systemctl start dune-backup-restore-drill.service
```

Failover role orchestration treats this as an active-host timer. A standby
should retain replicated/offsite backups but should not independently select
and certify the active workspace's local newest dump.

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `DUNE_RESTORE_DRILL_ENABLED` | `true` | Advertise and permit the restore-drill subsystem. |
| `DUNE_ADMIN_RESTORE_DRILL_EXECUTION_ENABLED` | `false` | Permit browser queueing. The host timer/CLI are independent. |
| `DUNE_RESTORE_DRILL_HOST_WORKSPACE` | required in container | Absolute host checkout path used only to bind the private staging copy. |
| `DUNE_RESTORE_DRILL_DOCKER_SOCKET` | `/var/run/docker.sock` | Local Docker Engine Unix socket. |
| `DUNE_RESTORE_DRILL_IMAGE` | pinned PostgreSQL 17.12 security image | Already-loaded image; never pulled by the drill. |
| `DUNE_RESTORE_DRILL_MAX_BACKUP_AGE_HOURS` | `36` | RPO freshness target. |
| `DUNE_RESTORE_DRILL_MAX_RESTORE_SECONDS` | `900` | `pg_restore` RTO target. |
| `DUNE_RESTORE_DRILL_MEMORY_MIB` | `2048` | Hard container memory and swap limit. |
| `DUNE_RESTORE_DRILL_PGDATA_MIB` | `1536` | Ephemeral PGDATA tmpfs capacity. |
| `DUNE_RESTORE_DRILL_CPUS` | `2` | Docker CPU quota. |
| `DUNE_RESTORE_DRILL_PIDS_LIMIT` | `128` | Process limit. |
| `DUNE_RESTORE_DRILL_RECEIPT_RETENTION` | `1000` | Local receipt count. |

Increase PGDATA before memory if a restored database outgrows tmpfs. Increase
the RTO only after measuring healthy runs; do not make a slow or stale backup
look healthy by changing both the workload and policy at once.

## Failure Recovery

- `no PostgreSQL custom-format dump`: create a full backup and verify its
  configured backup root.
- `host workspace path`: set it to the real host checkout, not `/workspace`
  from inside the admin container.
- `image not found`: load the pinned Funcom prerequisites; the drill will not
  pull an unreviewed replacement.
- readiness failure: inspect the receipt error and service journal; inadequate
  tmpfs or an incompatible image are common causes.
- restore/schema/index failure: preserve the failed receipt and source dump,
  create a fresh stopped-world backup, and compare both before deleting either.
- cleanup failure: remove only the exact `dash-restore-drill-*` container with
  label `com.dash.restore-drill=true`; cleanup failure makes the drill fail.
- policy-only failure: the dump restored successfully but missed RPO or RTO.
  Fix backup cadence or resource capacity instead of treating it as corruption.

## Validation

```bash
make test-restore-drill
make validate
```

The focused suite covers path confinement, symlink rejection, hardening spec,
successful restore stages, Dune validation, round-trip evidence, cleanup,
failure receipts, policy separation, exact stale-container ownership,
concurrency locking, private permissions, and receipt-chain verification.
