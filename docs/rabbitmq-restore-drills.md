# RabbitMQ Recovery Proof

DASH can prove that both broker backups are recoverable without connecting to,
stopping, restarting, mounting, or modifying either live RabbitMQ broker. The
drill boots copied Mnesia state from a complete full backup under the original
`admin-rmq` and `game-rmq` node identities, validates health and topology, then
destroys every disposable resource.

This complements the PostgreSQL drill in
[`restore-drills.md`](restore-drills.md). Structural archive checks answer
whether bytes can be listed. This drill answers whether the copied broker state
can actually start and expose its retained vhosts, users, queues, exchanges,
bindings, and message counts.

## Run and inspect

From the host checkout:

```bash
./scripts/rabbitmq-restore-drill.py
./scripts/rabbitmq-restore-drill.py --status
```

Select a specific complete backup only when investigating it:

```bash
./scripts/rabbitmq-restore-drill.py \
  --backup-set backups/20260717T033847Z
```

Equivalent Make targets are:

```bash
make rabbitmq-restore-drill
make rabbitmq-restore-drill-status
```

The latest complete supported directory is selected by default. The drill
accepts both backup formats produced by DASH:

- legacy split sets directly beneath `backups/`, containing bounded regular
  files named `manifest.txt`, `config.tgz`, `config-tls.tgz`,
  `rabbitmq-admin.tgz`, and `rabbitmq-game.tgz`; and
- scheduled full backups beneath `backups/admin-panel/maintenance/`, containing
  `manifest.json`, `config-and-env.tgz`, and `rabbitmq.tgz`.

A scheduled manifest must declare a complete full backup with both `config`
and `rabbitmq` captured and required, no missing coverage, and artifact paths
and byte counts that match the bounded regular files. The two broker states
are selected from their fixed `rabbitmq/admin` and `rabbitmq/game` prefixes in
the consolidated archive.

Requested paths must remain under the workspace backup root. Symlinked sets,
missing layers, empty files, and oversized archives fail closed. Automatic
selection compares the oldest required archive/manifest mtime, anchored by
recorded creation timestamps in the manifest and backup name. Directory
modification times are ignored: inspecting or copying an old set must not make
its recovery data appear fresh.

## Isolation contract

The implementation enforces and then inspects this contract for each broker:

- the source archives and live Docker volumes remain read-only and untouched;
- only a private extracted copy of `/var/lib/rabbitmq` is writable;
- `admin-rmq` and `game-rmq` run sequentially, never as a cluster;
- Docker `NetworkMode` is `none` and no network is created;
- no host or container port is published;
- the root filesystem is read-only;
- all Linux capabilities are dropped;
- `no-new-privileges` is enabled;
- a configured non-root UID/GID runs the broker;
- memory, swap, CPU, and PID ceilings are fixed;
- configuration, plugin, passwd/group, and TLS mounts are read-only;
- only stale PID files inside the copied state are removed; and
- the container and private staging tree must be deleted for the run to pass.

The Docker client uses the local Unix socket directly. Creating a container
with an unavailable image fails; the drill has no image-pull operation. The
configured image should match the deployed Funcom RabbitMQ image exactly.

Before extraction, DASH rejects absolute paths, traversal, backslashes,
symlinks, hardlinks, devices, duplicate required configuration members,
undeclared member lengths, more than 20,000 members, individual members above
2 GiB, and total expanded state above 8 GiB. Extraction uses manual bounded
copies instead of `tar.extractall()`.

## Proof collected

For each copied broker, the receipt records:

- source archive SHA-256 and bounded extraction counts;
- original node identity and stale-PID removal result;
- inspected isolation controls and resource ceilings;
- time until `rabbitmq-diagnostics -q check_running` proves the recovered
  `rabbit` application is running (an Erlang-node-only `ping` is insufficient);
- a successful `rabbitmqctl -q status`; and
- counts of vhosts, users, queues, exchanges, bindings, and messages.

Vhost names are bounded in memory for per-vhost queries. User, queue, exchange,
and binding inventories are reduced to counts inside the disposable container;
queue totals include message counts. Only bounded numeric results return to
the host, so large valid inventories do not require a larger output limit.
Names are never written to the receipt, Admin API, Prometheus, or audit events. Failed container logs
are also withheld; only their SHA-256 is returned for correlation.

The run passes only when both brokers recover, every inspected isolation
control matches the requested container specification, cleanup succeeds, and
the source backup age is within policy. `liveRabbitMQTouched=false` and
`networkCreated=false` are explicit receipt fields.

## Private receipts and tamper detection

Receipts live under:

```text
backups/admin-panel/rabbitmq-restore-drills/<UTC>-<nonce>.json
backups/admin-panel/rabbitmq-restore-drills/latest.json
backups/admin-panel/rabbitmq-restore-drills/head.anchor.json
```

Files are mode `0600`; the directory is mode `0700`. Each receipt hashes its
complete canonical content and names its predecessor. The authenticated head
anchor binds the newest and oldest retained hashes, retained count, and the
oldest predecessor. A private 32-byte key in the same directory HMACs the
anchor. This detects content edits, receipt insertion/relinking, middle
deletion, newest deletion, and oldest-tail truncation. If history verification
fails, DASH refuses to append another receipt.

Retention pruning updates the authenticated retained-range binding only after
a successful append. The default retains 1,000 receipts. Full host and browser
maintenance backups copy the newest self-verifying receipt as
`rabbitmq-restore-drill.json`; both the shell and minimal-image native backup
verifiers validate it. The full local receipt directory remains the authority
for complete chain and anchor verification.

Receipts contain no RabbitMQ cookie, password, token, TLS private-key bytes,
topology names, raw logs, player data, or live volume path.

## Admin API and dashboard

The authenticated read endpoint is:

```text
GET /api/ops/rabbitmq-restore-drill
```

It returns configuration readiness, runtime state, policy/resource limits,
the enforced isolation contract, latest private receipt, recent history, and
authenticated-chain status.

Queueing from **Infrastructure → RabbitMQ Recovery Proof** uses:

```text
POST /api/ops/rabbitmq-restore-drill
```

It requires all of the following:

- the `infrastructure.write` capability;
- `DUNE_ADMIN_MUTATIONS_ENABLED=true`;
- `DUNE_RABBITMQ_RESTORE_DRILL_ENABLED=true`;
- `DUNE_ADMIN_RABBITMQ_RESTORE_DRILL_EXECUTION_ENABLED=true`; and
- exact confirmation `RUN NETWORKLESS RABBITMQ RESTORE DRILL`.

The request returns `202 Accepted`; a background worker performs the bounded
run. CLI, timer, and browser paths share a nonblocking filesystem lock. A
second run is refused while one is active.

Before the first successful canary, Feature Readiness reports
`rabbitmq-recovery-proof` as `canary-pending`. A current valid passing receipt
and authenticated history promote it to `ready`. A failed run or invalid
history reports `degraded`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUNE_RABBITMQ_RESTORE_DRILL_ENABLED` | `true` | Load status, proof, metrics, and readiness integration. |
| `DUNE_ADMIN_RABBITMQ_RESTORE_DRILL_EXECUTION_ENABLED` | `false` | Separately permit browser queueing. CLI/timer execution is independent. |
| `DUNE_RABBITMQ_RESTORE_DRILL_HOST_WORKSPACE` | required in Admin container | Absolute host checkout path used for private bind mounts. |
| `DUNE_RABBITMQ_RESTORE_DRILL_DOCKER_SOCKET` | `/var/run/docker.sock` | Local Docker Unix socket. |
| `DUNE_RABBITMQ_RESTORE_DRILL_IMAGE` | current Funcom RabbitMQ image | Exact already-loaded image; never pulled by the drill. |
| `DUNE_RABBITMQ_RESTORE_DRILL_MAX_BACKUP_AGE_HOURS` | `36` | Recovery-point freshness target. |
| `DUNE_RABBITMQ_RESTORE_DRILL_READINESS_SECONDS` | `180` | Startup budget for each broker. |
| `DUNE_RABBITMQ_RESTORE_DRILL_MEMORY_MIB` | `1024` | Memory and swap ceiling per sequential broker. |
| `DUNE_RABBITMQ_RESTORE_DRILL_CPUS` | `1` | CPU quota per broker. |
| `DUNE_RABBITMQ_RESTORE_DRILL_PIDS_LIMIT` | `256` | Process ceiling per broker. |
| `DUNE_RABBITMQ_RESTORE_DRILL_RECEIPT_RETENTION` | `1000` | Private receipt count retained locally. |

`scripts/enable-feature-parity.sh .env --execute` enables both feature gates,
sets the host workspace and Docker socket, derives the RabbitMQ image from
`DUNE_IMAGE_TAG`, and applies the bounded defaults atomically.

## Scheduling

Install the hardened weekly timer:

```bash
./scripts/install-rabbitmq-restore-drill-timer.sh .env
```

The installer verifies that the non-root operator can access the Docker socket,
creates the private receipt directory, resolves the exact image from `.env`,
installs the service and timer, and enables it. The service has a strict system
view, a read-only checkout, a writable `backups/` exception, no capabilities,
`NoNewPrivileges`, private temporary storage, and only `AF_UNIX` sockets.

Inspect or run it with:

```bash
systemctl list-timers dune-rabbitmq-restore-drill.timer --all --no-pager
systemctl status dune-rabbitmq-restore-drill.service --no-pager
journalctl -u dune-rabbitmq-restore-drill.service -n 200 --no-pager
sudo systemctl start dune-rabbitmq-restore-drill.service
```

The weekly schedule is Sunday at 05:30 with up to 30 minutes of randomized
delay. `Persistent=true` catches a missed run after the host returns.

## Metrics and alerts

The private DASH metrics endpoint exports label-free values:

```text
dash_rabbitmq_restore_drill_enabled
dash_rabbitmq_restore_drill_configured
dash_rabbitmq_restore_drill_receipt_present
dash_rabbitmq_restore_drill_ok
dash_rabbitmq_restore_drill_integrity_ok
dash_rabbitmq_restore_drill_running
dash_rabbitmq_restore_drill_backup_age_seconds
dash_rabbitmq_restore_drill_last_finished_timestamp_seconds
```

Prometheus alerts when configuration is invalid, the latest proof or
authenticated history fails, or no run has completed within eight days.

The same authenticated proof is a non-maintenance-excludable Operational SLO
objective and a mandatory game-update readiness check. A missing, stale,
tampered, non-isolated, or failed dual-broker receipt therefore blocks update
certification instead of leaving recovery coverage as advisory telemetry.

## Failure recovery

- **No complete backup:** run `scripts/backup-state.sh .env`, verify the new
  set, then repeat. The drill does not synthesize missing RabbitMQ layers.
- **Wrong node identity:** keep the receipt and inspect how the backup was
  produced. Do not rename Mnesia node directories.
- **Image unavailable:** load the exact deployed image and correct
  `DUNE_RABBITMQ_RESTORE_DRILL_IMAGE`; do not substitute an arbitrary RabbitMQ
  version for proof.
- **Readiness timeout:** compare the returned log SHA with private Docker/system
  logs, increase the bounded startup budget only with evidence, and rerun.
- **History/anchor invalid:** preserve the entire private receipt directory and
  restore it from a known-good backup. Appends intentionally stop.
- **Stale owned container:** a later run removes only labeled
  `dash-rmq-restore-drill-*` containers that are stopped or older than six
  hours. Unrelated containers are never selected.
- **Cleanup failure:** remove only the exact labeled disposable container or
  `.stage-*` directory named by the private run evidence, then repeat. Cleanup
  failure makes the proof fail.

Run the focused regression suite with:

```bash
make test-rabbitmq-restore-drill
```
