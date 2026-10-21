# Postgres Streaming Replica

This repo can run optional hot standby Postgres containers for near-realtime
physical streaming replication of the Dune database volume. A same-host standby
is useful for read-only dumps and primary-container failures. A remote standby is
the useful layer for host/disk failure.

This is not a replacement for point-in-time backups. A streaming replica quickly
copies good writes, bad writes, deletes, corruption, and schema migrations. Keep
`scripts/backup-state.sh` or admin-panel dumps for recoverable snapshots.
For broader onsite/offsite backup patterns, see `docs/backup-strategy.md`.

## Enable

Fresh `.env` files created by `scripts/populate-local-env.sh` already get a
random `POSTGRES_REPLICATION_PASSWORD`. If you maintain `.env` by hand, add a
strong replication password:

```sh
POSTGRES_REPLICATION_USER=dune_replicator
POSTGRES_REPLICATION_PASSWORD=<strong unique password>
POSTGRES_REPLICATION_SLOT=dune_standby
POSTGRES_REPLICA_HOST_PORT=15433
```

Before starting Compose, stage protected runtime files from the private `.env`:

```sh
./scripts/protect-compose-secrets.sh
python3 scripts/game-runtime-secrets.py sync-from-env --env-file .env
```

Compose mounts the application password and replication password as read-only
files. The replica wrapper creates a mode-`0600` temporary `.pgpass` under
`/run` and removes both password environment variables before starting Postgres.
The remote installer transfers the replication secret as a protected file;
it does not put the value in Docker arguments or container metadata.

Initialize the primary replication role/HBA rule and start the standby:

```sh
COMPOSE_FILES=compose.yaml:compose.replica.yaml ./scripts/setup-postgres-replica.sh .env
```

The setup script:

- starts the primary `postgres` service if needed;
- creates or updates `POSTGRES_REPLICATION_USER`;
- creates the physical replication slot named by `POSTGRES_REPLICATION_SLOT`;
- appends a `pg_hba.conf` replication rule on the primary and reloads Postgres;
- starts `postgres-replica`, which runs `pg_basebackup` into `data/postgres-replica` on first boot.

## Remote Replica

For host-level redundancy, run the standby on another LAN host. The primary host
can expose a replication-only LAN forwarder without recreating the live Postgres
container:

```sh
POSTGRES_REPLICATION_BIND_ADDRESS=<primary-lan-ip>
POSTGRES_REPLICATION_ALLOWED_ADDRESS=<remote-lan-ip>
POSTGRES_REPLICATION_PRIMARY_HOST=<primary-lan-ip>
POSTGRES_REPLICATION_PUBLIC_PORT=15434
POSTGRES_REMOTE_REPLICATION_SLOT=dune_standby_remote
POSTGRES_REMOTE_REPLICA_HOST=replica.example.lan
POSTGRES_REMOTE_REPLICA_ROOT=/srv/dune-postgres-replica
```

Install the primary-side forwarder:

```sh
./scripts/install-postgres-lan-forwarder.sh .env
```

`POSTGRES_REPLICATION_ALLOWED_ADDRESS` limits the forwarder to the remote replica
host before Postgres authentication is reached. Keep this port on a private LAN
or VPN only.

Install the remote standby. This script copies the Postgres image to the remote
host if missing, creates a dedicated physical replication slot, and runs the
remote container with Docker:

```sh
./scripts/install-remote-postgres-replica.sh .env replica.example.lan /srv/dune-postgres-replica
```

The remote host needs SSH access and Docker. It does not need Docker Compose.
The standby data lives under the remote root's `data/` directory. By default the
remote standby runs on Docker bridge networking and does not bind host TCP 5432.
Set `DUNE_POSTGRES_STANDBY_NETWORK_MODE=host` only on hosts dedicated to the
standby or known not to run a host-level Postgres listener.

After the first run, include the overlay whenever managing the stack:

```sh
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml ps
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml up -d postgres-replica
```

If you use `COMPOSE_FILES` for the all-maps overlay, include all overlays:

```sh
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml:compose.replica.yaml ./scripts/status.sh .env
```

## Check Status

One command checks the current three-layer posture:

```sh
./scripts/backup-layers-status.sh .env replica.example.lan /srv/dune-postgres-replica
```

It reports the remote replication slot, standby recovery state, latest rolling
snapshots, recent full-state backups, and the systemd timer/forwarder state.

On the primary:

```sh
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml exec -T postgres \
  psql -U dune -d dune_sb_1_4_0_0 -c \
  "select application_name,state,sync_state,write_lag,flush_lag,replay_lag from pg_stat_replication;"
```

On the replica:

```sh
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml exec -T postgres-replica \
  psql -U dune -d dune_sb_1_4_0_0 -c \
  "select pg_is_in_recovery(), now() - pg_last_xact_replay_timestamp() as replay_delay;"
```

## Use For Backups

Read-only logical dumps can be taken from the replica without adding dump load to
the primary:

```sh
mkdir -p backups
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml exec -T postgres-replica \
  pg_dump -U dune -d dune_sb_1_4_0_0 -Fc \
  > backups/dune-replica-$(date -u +%Y%m%dT%H%M%SZ).dump
```

The existing `scripts/backup-state.sh` still dumps from the primary. Use the
manual replica dump above when you specifically want to avoid primary dump load.
RabbitMQ and `data/server-saved` archives are not covered by Postgres streaming
replication.

For remote hourly snapshots from the remote standby:

```sh
./scripts/replica-snapshot.sh .env replica.example.lan /srv/dune-postgres-replica
./scripts/install-replica-snapshot-timer.sh .env replica.example.lan /srv/dune-postgres-replica
```

The helper streams the custom-format dump over SSH and stores it locally under
`backups/replica-snapshots`. Put that path on the intended backup filesystem;
`DUNE_REPLICA_SNAPSHOT_ROOT` can select another local path. The timer requires
the filesystem containing the snapshot path before it runs. It keeps
`DUNE_REPLICA_SNAPSHOT_KEEP_HOURS` hours of top-level dumps (48 by default),
across database names, so a database-version change cannot strand old dumps on
the replica host. Nested archive directories are preserved.

The installed oneshot service runs as the installing local user by default so
SSH uses that user's configured keys and host aliases. Override with
`DUNE_REPLICA_SNAPSHOT_USER` and `DUNE_REPLICA_SNAPSHOT_GROUP` only when a
dedicated local service account owns the remote replica SSH credentials.

Admin-triggered restart and shutdown maintenance also account for these layers.
The stopped-world local maintenance backup remains the authoritative restore
point. Its `postgres-layers.json` records primary streaming-replication status,
and, when `POSTGRES_REMOTE_REPLICA_HOST` is set, attempts a remote standby
snapshot through `scripts/replica-snapshot.sh`. Set
`DUNE_ADMIN_MAINTENANCE_REPLICA_SNAPSHOT_ENABLED=false` if maintenance jobs
should only record replica status and leave remote snapshots to the systemd
timer.

## Restore And Failover

Routine restore still uses `scripts/restore-state.sh` and is disruptive. After
restoring the primary from a dump, rebuild the standby so it matches the new
timeline and contents:

```sh
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml stop postgres-replica
rm -rf data/postgres-replica
COMPOSE_FILES=compose.yaml:compose.replica.yaml ./scripts/setup-postgres-replica.sh .env
```

For the remote standby layout, rebuild the remote replica from the restored
active primary instead of letting it continue on the old timeline:

```sh
make rebuild-postgres-standby ENV_FILE=.env TARGET="$POSTGRES_REMOTE_REPLICA_HOST" ROOT="$POSTGRES_REMOTE_REPLICA_ROOT"
CONFIRM_REBUILD_POSTGRES_STANDBY=yes make rebuild-postgres-standby ENV_FILE=.env TARGET="$POSTGRES_REMOTE_REPLICA_HOST" ROOT="$POSTGRES_REMOTE_REPLICA_ROOT"
make standby-status ENV_FILE=.env
```

The rebuild helper moves the old remote data directory aside. It defaults to
Docker bridge networking so a standby host with another Postgres listener on
host TCP 5432 can still run `dune-postgres-replica`.

Promotion is only for disaster recovery. If you promote the replica while the
original primary can still accept writes, you can split-brain the database. A
full failover runbook should stop all game/admin writers first, promote the
replica, repoint services to the promoted database, and rebuild a new standby
from the promoted primary.

Postgres cutback is not automatic. Confidence high: once a standby is promoted,
the old primary is not a valid reverse standby until it is rebuilt or otherwise
rewound onto the promoted timeline. This repo provides a confirmation-gated
rebuild helper for the common safe path:

```sh
make postgres-cutback-proof ENV_FILE=.env TARGET=<old-primary-host> ROOT=<standby-root> SEAL_FILE=<seal-file>
make rebuild-postgres-standby ENV_FILE=.env TARGET=<old-primary-host> ROOT=<standby-root>
CONFIRM_REBUILD_POSTGRES_STANDBY=yes make rebuild-postgres-standby ENV_FILE=.env TARGET=<old-primary-host> ROOT=<standby-root>
```

Run these from the current active primary. `postgres-cutback-proof` verifies the
pre-promotion seal against target control data and rewind evidence. The rebuild
helper moves the target's existing standby data aside and starts a fresh
`dune-postgres-replica` cloned with `pg_basebackup`.

For the primary-to-standby warm-standby procedure, including file mirroring,
promotion, router/NAT cutover, systemd role services, and live-handoff
investigation scope, see `docs/primary-standby-failover.md`.

## Failure Notes

- If the replica is stopped for too long, the physical replication slot can hold
  WAL on the primary. Monitor disk usage under `data/postgres/pg_wal`.
- If the replica falls unrecoverably behind, stop it, remove
  `data/postgres-replica`, then rerun `scripts/setup-postgres-replica.sh`.
- Promotion is a disaster-recovery operation. Do not promote the replica while
  the original primary can still accept writes unless you are intentionally
  failing over.
