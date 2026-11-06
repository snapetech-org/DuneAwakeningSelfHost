# Backup Strategy

Use layered backups. No single backup mechanism covers every failure mode.

## Layers

| Layer | Protects Against | Does Not Protect Against |
| --- | --- | --- |
| Local stopped-world backup | Bad config edits, failed upgrades, admin mistakes noticed quickly | Host loss, disk loss, old unnoticed corruption |
| Local Postgres streaming replica | Primary Postgres container/volume failure, read-only dump load | Deletes, bad writes, host loss when same disk/host |
| Remote Postgres replica | Host loss, primary disk loss, read-only off-host dumps | Deletes and bad writes that replicate |
| Offsite object/NAS backup | Fire/theft/site loss, rollback to older snapshots | Very recent writes unless schedule is tight |
| Manual pre-change backup | Known restore point before risky work | Problems that predate the backup |

The maintenance restart flow creates an authoritative local maintenance backup before it checks the Steam package for updated Funcom image tarballs and starts services again. That backup includes a Postgres dump, config/env archive, RabbitMQ archives, server saved data, and a manifest. See [`docs/maintenance-updates.md`](maintenance-updates.md).

Manual and scheduled Admin-panel full backups additionally declare the exact
enabled durable-store coverage in `manifest.json` and fail if any required
layer is absent. They share an OS lock with host backups and assured deployments
so a deployment cannot replace the Admin process or change recovery inputs
mid-snapshot. Failure retries, retention, metrics, alerts, and the production
canary are documented in [`automatic-backups.md`](automatic-backups.md).

## Local Backup

Run:

```bash
./scripts/backup-state.sh .env
```

Before a maintenance window, check what identity/config layers will be captured without contacting Docker:

```bash
./scripts/backup-state.sh --dry-run .env
```

Equivalent Make targets:

```bash
make backup-dry-run ENV_FILE=.env
make backup-state ENV_FILE=.env
make verify-backup BACKUP_DIR=backups/<UTC timestamp>
make restore-dry-run ENV_FILE=.env BACKUP_DIR=backups/<UTC timestamp>
make operational-report ENV_FILE=.env
make operational-bundle ENV_FILE=.env
```

Backups are written under:

```text
backups/<UTC timestamp>/
```

The local backup includes:

- Postgres custom-format dump.
- RabbitMQ state archives when brokers are running.
- Server saved-state archive when available.
- A copy of the env file used for the backup.
- `config.tgz` for committed/local config files, excluding TLS key material.
- `config-tls.tgz` for RabbitMQ TLS material under `config/tls/`.
- A transactionally consistent `community-rewards.sqlite3` snapshot when the
  isolated wallet/shop database exists.
- Policy-bound Community Rewards canary receipts inside `operator-evidence.tgz`;
  the matching config archive supplies the HMAC key and the verifier applies
  the canary's strict no-live-data semantics.
- Input-bound Creator/Modding lifecycle receipts in the same archive; both
  backup verifiers dispatch their schema and enforce the signed isolation and
  semantic verdict before accepting the archive.
- Input-bound Public-IP Repair lifecycle receipts in the same archive; both
  verifiers reject tampered/inconsistent evidence and require the signed
  no-live-environment/TLS/systemd/Docker/map/network isolation verdict.
- The private `canary-autopilot.json` retry/attempt history, validated against
  its strict schema at copy time and again by the backup verifier. This file is
  diagnostic scheduler state; the signed receipts in `operator-evidence.tgz`
  remain the proof authority.
- Signed Operator Briefing receipts in `operator-evidence.tgz`. Both backup
  verifiers use the matching Change Intelligence key to recompute signature,
  source fingerprint, score/state/summary, action mapping, receipt digest, and
  timestamp semantics before accepting the mixed evidence archive.
- A transactionally consistent `moderation.sqlite3` snapshot when the isolated
  case/history database exists, including the identity-free aggregate
  population buckets used by the player-impact maintenance planner.
- A transactionally consistent `base-gallery.sqlite3` snapshot when the
  isolated creator/gallery database exists.
- A transactionally consistent `operational-slo.sqlite3` reliability snapshot
  when the SLO ledger exists; verification also checks its incident hash chain.
- A transactionally consistent `capacity-intelligence.sqlite3` snapshot when
  the capacity ledger exists; verification checks its append-only application
  triggers and every receipt hash.
- A transactionally consistent `desired-state.sqlite3` snapshot when the
  attestation ledger exists. Verification uses the matching policy and HMAC key
  in `config.tgz` to recompute every baseline/observation/finding signature and
  event link; SQLite integrity alone is insufficient.
- A transactionally consistent `change-intelligence.sqlite3` snapshot when the
  operational timeline exists. Verification extracts the matching policy/key
  from `config.tgz` and recomputes every event HMAC and chain link.
- A transactionally consistent `alert-inbox.sqlite3` snapshot when the
  Prometheus on-call inbox has initialized. Verification requires SQLite
  integrity and the alerts, transitions, and metadata schema; the source and
  backup stay private at mode `0600`.

For an isolated alert-inbox recovery, use
`scripts/restore-alert-inbox.sh --execute`; it does not target maps, Postgres,
or RabbitMQ. The `restore-state.sh --alert-inbox` flag below is only for the
complete stopped-world recovery path.
- A consistent `audit-ledger.sqlite3`, `audit-ledger.hmac.key`, and
  `audit-ledger.anchor.json` set when the mutation flight recorder exists. The
  backup retries around concurrent admin events until the copied SQLite chain
  and authenticated head verify together.
- A consistent `credential-lifecycle.sqlite3` and
  `credential-lifecycle.anchor.json` pair whose observation chain and
  authenticated head are verified with the matching
  `config/secrets/credential-lifecycle-hmac.secret` from `config.tgz`. The
  backup retries around concurrent observations until all three agree.
- A paired `change-approvals.sqlite3` and `change-approvals.key` snapshot when
  two-person approval state has been initialized. A partial pair fails backup
  creation and verification.
- `operator-evidence.tgz` when portable signed evidence exists. Collection pins
  the complete bounded selection with open file handles before archiving, so
  concurrent retention cannot remove a selected inode from the backup. A name
  disappearing during acquisition retries the entire selection up to five
  times; it never silently skips a missing capsule or publishes a partial
  archive. Both verifiers
  confine and bound every member, dispatch by schema, recompute incident-plan
  and drill/certification digests or deployment manifest/continuity/health
  semantics, or rederive exact game-update candidate/check/verdict/expiry
  semantics, and verify the outer HMAC with the matching key from this backup
  rather than the current live key. See
  [`incident-response.md`](incident-response.md) and
  [`deployment-assurance.md`](deployment-assurance.md), and
  [`update-readiness.md`](update-readiness.md).
- `manifest.txt` with `WORLD_UNIQUE_NAME`, `DUNE_FLS_ENV`, and `GAME_RMQ_PUBLIC_HOST`.

New CLI backups run with `umask 077`. Admin-panel dump, archive, manifest, and
layer-report artifacts are mode `0600`, their per-run directory is mode `0700`,
and root-running containers, including the Prometheus alert inbox, transfer
ownership to `DUNE_HOST_UID` / `DUNE_HOST_GID`. This keeps scheduled host restore drills readable by the DASH
operator without making database dumps world-readable.

`scripts/backup-state.sh`, panel-created backups, and the complete assured
deployment workflow serialize through `backups/admin-panel/operation.lock`.
Standalone host callers wait up to `DUNE_OPERATION_LOCK_WAIT_SECONDS` (1,800
seconds by default); scheduled panel runs defer to their short retry window
without recording a false backup failure.

Restore with:

```bash
./scripts/restore-state.sh .env backups/<UTC timestamp>
```

Restores are disruptive. Stop game/admin writers first.
RabbitMQ, saved-state, config, and TLS replacement are opt-in:

```bash
./scripts/restore-state.sh --rabbitmq --server-saved --config --tls --community-rewards --moderation --base-gallery --operational-slo --capacity-intelligence --desired-state --change-intelligence --alert-inbox --credential-lifecycle --change-approvals --audit-ledger .env backups/<UTC timestamp>
```

Run `--dry-run` first. If the manifest `WORLD_UNIQUE_NAME` differs from the current `.env`, restore will warn because that value is the durable FLS battlegroup identity.

The equivalent Make target accepts optional restore layer flags:

```bash
make restore-dry-run ENV_FILE=.env BACKUP_DIR=backups/<UTC timestamp> RESTORE_FLAGS='--rabbitmq --server-saved --config --tls --community-rewards --moderation --base-gallery --operational-slo --capacity-intelligence --desired-state --change-intelligence --alert-inbox --credential-lifecycle --change-approvals --audit-ledger'
```

## Offsite and Onsite Sync

Use `scripts/backup-offsite.sh` to create a local backup and then sync `backups/` to another destination.

Dry local-only run:

```bash
DUNE_BACKUP_OFFSITE_MODE=none ./scripts/backup-offsite.sh .env
```

rclone example:

```bash
DUNE_BACKUP_REMOTE_ENV=examples/backup/rclone-offsite.env ./scripts/backup-offsite.sh .env
```

rsync/NAS example:

```bash
DUNE_BACKUP_REMOTE_ENV=examples/backup/rsync-nas.env ./scripts/backup-offsite.sh .env
```

restic example:

```bash
DUNE_BACKUP_REMOTE_ENV=examples/backup/restic.env ./scripts/backup-offsite.sh .env
```

Install an hourly systemd timer:

```bash
./scripts/install-backup-offsite-timer.sh .env examples/backup/rclone-offsite.env
systemctl list-timers dune-backup-offsite.timer --all --no-pager
```

The examples intentionally use placeholder destinations. Configure credentials with each tool's normal private config:

- rclone remotes in the operator user's rclone config.
- SSH keys for rsync.
- `RESTIC_PASSWORD_FILE` or a private secret manager for restic.

Do not put cloud tokens, SSH private keys, restic passwords, or provider credentials in this repo.

## Remote Postgres Replica

For a hot standby on another LAN host:

```bash
./scripts/install-postgres-lan-forwarder.sh .env
./scripts/install-remote-postgres-replica.sh .env replica.example.lan /srv/dune-postgres-replica
./scripts/install-replica-snapshot-timer.sh .env replica.example.lan /srv/dune-postgres-replica
```

The snapshot timer runs as the installing local user by default so SSH uses that
user's keys and host aliases. Set `DUNE_REPLICA_SNAPSHOT_USER` and
`DUNE_REPLICA_SNAPSHOT_GROUP` before installation only when another local
account owns the remote replica SSH credentials.

Check the layers:

```bash
./scripts/backup-layers-status.sh .env replica.example.lan /srv/dune-postgres-replica
```

See [`docs/postgres-replication.md`](docs/postgres-replication.md).

## Suggested Schedules

Small/private host:

- Local backup before upgrades and config changes.
- Daily offsite sync.
- Daily automated PostgreSQL restore proof, weekly networkless RabbitMQ
  recovery proof, plus periodic full-layer recovery exercise.

Public 30-map host:

- Maintenance backup before scheduled restart, followed by the Steam package image-tag check.
- Hourly offsite sync or restic backup.
- Remote Postgres replica with hourly snapshot.
- Daily automated PostgreSQL restore proof, weekly networkless RabbitMQ
  recovery proof, plus periodic full-layer recovery exercise.
- Alert when newest local/offsite backup is older than 24 hours.

## Restore Testing

A backup that has never been restored is only a guess. The structural verifier
is fast, but it does not prove that PostgreSQL can restore the archive:

```bash
./scripts/verify-backup.sh backups/<id>
```

Run the real isolated database rehearsal against the newest dump:

```bash
./scripts/backup-restore-drill.py
./scripts/backup-restore-drill.py --status
```

Install the daily persistent timer:

```bash
./scripts/install-backup-restore-drill-timer.sh .env
```

The rehearsal uses a no-network, read-only-root, capability-free disposable
PostgreSQL container with bounded CPU, memory, PIDs, and tmpfs. It restores the
archive, verifies Dune tables/functions/indexes/constraints and core row reads,
analyzes it, creates and lists a second custom-format dump, removes the
container, and records a private hash-chained RPO/RTO receipt. It never
connects to the live database. See [`restore-drills.md`](restore-drills.md).

Prove both copied RabbitMQ Mnesia layers start under their original node
identities without networking or live mounts:

```bash
./scripts/rabbitmq-restore-drill.py
./scripts/rabbitmq-restore-drill.py --status
./scripts/install-rabbitmq-restore-drill-timer.sh .env
```

The drill runs the admin and game brokers sequentially with fixed resources,
verifies health and name-free topology counts, inspects Docker isolation,
requires cleanup, and records HMAC-anchored private receipts. Full backups copy
the latest self-verifying receipt and both backup verifiers validate it. See
[`rabbitmq-restore-drills.md`](rabbitmq-restore-drills.md).

The private admin panel Infrastructure page lists backup sets below `backups/`
and invokes the same `scripts/verify-backup.sh` check. It also provides gated
create/download/import/quarantine-delete and dry-run/execute restore workflows.
Browser restores still invoke the same `scripts/restore-state.sh`, create a
verified pre-restore set, require zero online players, and keep layer selection
explicit. See [`infrastructure-console.md`](infrastructure-console.md).

The Infrastructure page can also schedule the same verified full backup by
local start time and interval, with bounded retention. The scheduler records
its next run before starting work to prevent duplicate execution after a slow
backup, and retention only removes paths that the scheduler itself recorded
under `backups/admin-panel/maintenance`. This complements rather than replaces
offsite sync and replica snapshots.

Recipient-encrypted portable archives and encrypted-only rclone/rsync staging
are documented in [`backup-encryption.md`](backup-encryption.md). Restic remains
the encrypted repository path; the OpenPGP path adds an independently portable
artifact with exact fingerprint selection and a ciphertext receipt.

For maintenance backups:

```bash
backup=backups/admin-panel/maintenance/<id>
./scripts/verify-backup.sh "$backup"
```

For offsite backups, periodically restore to a temporary directory from the remote provider and run the same checks there.

The automated drill proves the PostgreSQL layer. Continue periodic full-layer
exercises for RabbitMQ, server-saved data, configuration, TLS identity, and an
artifact downloaded from the actual offsite provider.

The mutation flight recorder is a three-artifact recovery unit:
`backups/admin-panel/audit-ledger.sqlite3`, `audit-ledger.hmac.key`, and
`audit-ledger.anchor.json`. Restore those files from the same backup. The HMAC
chain cannot be verified with a mismatched key, and the authenticated anchor
deliberately rejects a database whose tail differs. See
[`audit-ledger.md`](audit-ledger.md).

## Retention

Reasonable starting points:

- Keep local backups for 3-7 days.
- Keep offsite daily snapshots for 14-30 days.
- Keep weekly snapshots for 2-3 months.
- Keep monthly snapshots only if storage cost is acceptable.

Tune retention to player count, upgrade frequency, storage cost, and how long it usually takes you to notice data damage.
