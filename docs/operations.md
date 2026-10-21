# Operations

This page covers state management for the Compose lab. Commands assume the default `.env` file; pass `--env-file` directly if you use a different environment.

Set `CONTAINER_RUNTIME=podman` when testing with Podman-compatible Compose commands:

```bash
CONTAINER_RUNTIME=podman ./scripts/status.sh .env
CONTAINER_RUNTIME=podman ./scripts/capture-routing.sh .env baseline-idle
CONTAINER_RUNTIME=podman ./scripts/backup-state.sh .env
CONTAINER_RUNTIME=podman ./scripts/restore-state.sh .env backups/20260519T150000Z
```

For platform-specific notes, including Windows/macOS operator workstations,
Podman caveats, VM layout, and NAS storage guidance, see `docs/platforms.md`.

## Health Checks

Compose defines health checks for:

- `postgres`
- `admin-rmq`
- `game-rmq`

Check them with:

```bash
docker compose --env-file .env ps
```

The director, gateway, text-router, and game-server containers do not expose a
single generic readiness endpoint. The repository uses explicit control-plane
probes and the FLS publication health check instead of treating a running
container as healthy. A weak health check is worse than no health check because
it can mark a broken routing state as healthy.

For a concise runtime verdict, use:

```bash
./scripts/status.sh .env
```

Normal production scripts derive Docker Compose overlays with
`scripts/compose-files.sh .env`. Do not pin `COMPOSE_FILES` in persistent
service units or `.env` for host role selection; `POSTGRES_REMOTE_REPLICA_HOST`
is the active Postgres owner and causes `compose.failover-standby.yaml` to be
included on the promoted host.

The `health verdict` section evaluates the current lifecycle policy: every
`always-on` or actively demanded partition must have an alive farm row joined
through `world_partition` and an active server id. A stopped `dynamic` or
`disabled` map is policy-compliant, while a required map or a running optional
map that has not registered is unhealthy. The response retains whole-farm
counts for capacity visibility and adds `requiredMaps`, `requiredReadyAlive`,
and per-map `mode`, `expectedOnline`, and `policySatisfied` fields. Some live
builds can leave `farm_state.ready=false` briefly after the game log reports
`Server farm is READY`, and observed builds can retain that false flag. Required
map health therefore gates on `alive` plus `active`; ready-flag coverage remains
visible as advisory telemetry.

For RabbitMQ-specific checks, use:

```bash
./scripts/rmq-health.sh .env
```

For the client-facing game RabbitMQ TLS certificate identity, use:

```bash
./scripts/check-rabbitmq-cert-sans.sh .env
```

The check is read-only. It reports the certificate SANs and warns when the cert does not cover `GAME_RMQ_PUBLIC_HOST`, `game-rmq`, `localhost`, or `127.0.0.1`.

Certificate generation is explicit and guarded:

```bash
./scripts/generate-rabbitmq-cert.sh .env
```

The generator refuses to overwrite existing TLS files unless `--force` is passed. Use `--force` only during planned maintenance after backing up `config/tls/rabbitmq`.

Admin RabbitMQ can have fewer active service-user connections than farm partitions in the 30-map warm-pool layout. Treat it as unhealthy when recent auth/connectivity errors appear or when a failed client transition correlates with missing admin queue consumers.

Fast auth-path check:

```bash
./scripts/seed-gateway-neighbor.sh
./scripts/verify-rmq-auth-path.sh
```

That verifier covers the restart failure mode where maps are alive in the database but dynamic RabbitMQ auth times out because `admin-rmq` cannot reach the local auth shim.

The admin panel Overview and Ops tabs expose the same high-level readiness from a browser. The map table is derived from `world_partition`, `farm_state`, and `active_server_ids`; the network table probes local Postgres plus upstream HTTP reachability for the Dune account portal and public Dune/Funcom sites. Treat those probes as operator signals, not proof that FLS registration or client travel is healthy.

The Infrastructure tab adds project-scoped service/log inspection and control,
scheduled/full backup lifecycle, bounded database operations, and update/repair
workflows. The World tab adds read-only guild, Landsraad, and aggregate storage
views; separately gated Landsraad actions live on Admin Actions. See
[`infrastructure-console.md`](infrastructure-console.md) and
[`world-console.md`](world-console.md). Each mutation path is disabled by
default and requires its documented gate and exact confirmation.

The Ops tab also has restart-announcement and scheduled restart planners. The daily maintenance target is 06:00 local host time, with the host timer firing at 05:30 to create a 30-minute warning window. Executed maintenance uses a stopped-world backup, then checks the current Steam package for updated Funcom image tarballs before recreating services. See [`docs/maintenance-updates.md`](maintenance-updates.md) for the full timeline, update logic, and timer install path.

The same page ranks exact future maintenance windows from zero-inclusive,
identity-free population observations. Selecting a recommendation fills the
exact-time field; it does not bypass the execution selector, backup, certified
update, disconnect, announcement, or change-governance contracts. Until enough
complete historical windows exist, the planner visibly retains the configured
`06:00` fallback. See
[`player-impact-maintenance.md`](player-impact-maintenance.md).

Inspect Docker storage and preview obsolete Dune image cleanup with:

```bash
make storage-status
make storage-cleanup-dry-run
```

Deletion is limited to obsolete images in known Funcom Dune repositories. It
protects the configured current image, every image referenced by any container,
the pinned Postgres image, unrelated repositories, containers, volumes, data,
and backups. Execution requires `scripts/storage-cleanup.sh cleanup --execute`
plus the exact confirmation documented by the script. Build-cache pruning is a
separate opt-in flag. See [`infrastructure-console.md`](infrastructure-console.md).

For the expanded standing farm, the expected summary is:

```text
current_alive_active=9 active_servers=9 partitions=9
```

For the 30-partition warm pool, the expected summary is:

```text
current_alive_active=30 active_servers=30 partitions=30
```

## Survival Recovery

If Postgres or RabbitMQ restart while `survival` is running, restart the game-server process after dependencies are healthy. The observed live server build can crash fatally on a lost Postgres connection instead of reconnecting cleanly.

Use:

```bash
./scripts/recover-survival.sh .env
```

The helper starts Postgres and both RabbitMQ services, waits for their health checks, starts the service layer, force-recreates only `survival`, waits for registration, and then prints `./scripts/status.sh`.

## Fixed-Partition Map Recovery

If a specific map process crashes with `Local partition is not found`, recover it with the fixed-partition helper instead of immediately force-recreating the container. The failure means the process started with a new server id while its `world_partition` row was still assigned to an old server id that had not aged out of `active_server_ids`.

For example, partition 18 is `heighliner-dungeon`:

```bash
COMPOSE_FILES='compose.yaml:compose.allmaps.yaml' \
  ./scripts/recover-map.sh .env heighliner-dungeon 18
```

The helper marks the old partition owner dead, stops the map service, waits until the old id is no longer active, starts the service, and waits for the partition to become ready/alive/active again.

## Control-Plane Nudges

Use a nudge when the maps are already running and registered locally, but the
browser/FLS view is stale. This is different from map recovery: do not restart
map containers just because the server browser is missing maps.

Good nudge candidates:

- The server parent row appears in the browser, but the nested map list is empty
  or stale.
- `world_partition` has the expected assigned rows and `blocked=false`.
- Director logs show `[ServerState] Received server state` for the expected
  partitions.
- Director, gateway, or RabbitMQ had a temporary connectivity failure and then
  recovered.

First confirm the local state:

```bash
docker compose --env-file .env exec -T postgres psql -U dune -d dune_sb_1_4_0_0 \
  -c "select count(*) filter (where server_id is not null) assigned, count(*) filter (where blocked) blocked, count(*) total from dune.world_partition;"

docker logs --since 5m dune_server-director-1 2>&1 \
  | grep '\[ServerState\] Received server state' \
  | sed -E 's/.*"partitionId":([0-9]+).*/\1/' \
  | sort -n | uniq -c
```

If maps are locally present but FLS declarations are stale, restart only
Director. This causes Director to rebuild RabbitMQ subscriptions and re-declare
the battlegroup/map state to FLS without replacing running map containers:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.allmaps.yaml \
  restart director
```

On standby/failover hosts, include the same override files the stack is
currently using. For example:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.failover-standby.yaml \
  -f compose.allmaps.yaml -f compose.64g-limits.yaml \
  restart director
```

Verify the nudge by watching for successful FLS calls:

```bash
docker logs --since 90s dune_server-director-1 2>&1 \
  | grep -E 'Director_InitializeDirector|Battlegroups_DeclareBattlegroupUpdates|Battlegroups_SendBattlegroupHeartbeat|RMQ unreachable|No database connection'
```

`./scripts/status.sh` also runs `scripts/fls-publication-health.py`. Treat a
degraded FLS publication result as a server-browser outage even when all map
rows are locally green. The check looks for recent successful Director
initialization, heartbeat, population/capacity declarations, Gateway farm
status declaration, and no later FLS errors such as `INVALID_DATA` or
`does not exist or is inactive`.

```bash
compose_files="$(./scripts/compose-files.sh .env)"
./scripts/fls-publication-health.py .env --compose-files "$compose_files"
```

If Director can reach FLS only from host networking, set
`DUNE_DIRECTOR_HOSTNET_ENABLED=true` and keep
`compose.director-hostnet-port.yaml` pointed at a free host-local port. This
adds `compose.director-hostnet-cutover.yaml` plus the port override to the
normal compose-file resolver, so later `director` recreates do not drop the
working network shape.

Admin alerts can include the same signal when
`DUNE_PLAYER_PRESENCE_INFRA_ADMIN_ALERTS_ENABLED=true`; the FLS publication
subcheck is controlled by `DUNE_PLAYER_PRESENCE_FLS_PUBLICATION_HEALTH_ENABLED`.

### HP3 when Funcom FLS identity verification fails

`Pending Connection Failure` is Unreal's general network-failure category. In
the 2026-10-21 production incident, the map log identified the specific failure
as `sbHP3$ Server could not verify player's identity.` during
`VerifyFlsIdentity`. The map's request to
`https://sb-retail.fls.funcom.com/api/Auth_VerifyFlsServerToken` timed out after
connecting to an obsolete pinned IPv4 address whose certificate did not match
the FLS hostname. Removing the stale entries restored current DNS resolution
and successful TLS verification from a representative map.

This check is a server-to-Funcom HTTPS request, so a failure can reject
internal and external players alike. Hairpin routing affects LAN clients that
reach the server through its public address; it does not explain this FLS
identity-verification error.

The previous workaround for a 2026-05-28 DNS-resolution incident pinned
`sb-retail.fls.funcom.com` to two IPv4 addresses. Those addresses became stale.
Use normal DNS for FLS and do not enable static host pins. The old
`compose.fls-ipv4-hosts.yaml` file is now an empty compatibility overlay, so an
older `COMPOSE_FILES` entry cannot reintroduce the pins.

For a live incident, verify the target host before editing its environment or
running-container host files:

```bash
hostname
```

Only continue with a live change when that prints `kspls0`. Remove
`compose.fls-ipv4-hosts.yaml` from `COMPOSE_FILES` in `.env` if it is listed,
then remove existing `sb-retail.fls.funcom.com` lines from running Dune app
containers' `/etc/hosts`. Do not restart maps just to clear those entries.

Check a representative map's resolver and TLS path:

```bash
docker exec dune_server-survival-1 getent ahostsv4 sb-retail.fls.funcom.com
docker exec dune_server-survival-1 curl --disable --noproxy '*' -sS \
  --connect-timeout 5 --max-time 10 -o /dev/null \
  -w 'remote=%{remote_ip} http=%{http_code} tls_verify=%{ssl_verify_result}\n' \
  https://sb-retail.fls.funcom.com/
```

The FLS A record can change. A successful unauthenticated probe returns HTTP
403 with `tls_verify=0`; that confirms DNS, connectivity, and certificate
validation. Check recent map logs for further `VerifyFlsIdentity` failures
afterward.

If the parent server row itself is missing from the browser, nudge `gateway`
instead of Director. This is also the recovery for accidental FLS identity
stealing: if another host starts with the same `.env`/`WORLD_UNIQUE_NAME` and
then stops, it can publish the same battlegroup as inactive. Restarting
`gateway` on the intended live host re-declares the farm as active without
restarting Survival or the map containers:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.allmaps.yaml \
  restart gateway
```

Then verify that Gateway declared the farm and observed the maps:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.allmaps.yaml \
  logs --since 3m gateway \
  | grep -E 'GatewayDeclareFarmStatus|Server .* came up|HTTP Request Error|failed|inactive'
```

On hosts affected by Docker bridge neighbor-learning issues, reseed the
gateway/Postgres neighbors after the restart:

```bash
./scripts/seed-gateway-neighbor.sh
```

If `scripts/fls-publication-health.py` still reports Director population
errors such as `INVALID_DATA` or `does not exist or is inactive` immediately
after the gateway restart, check the live browser before escalating. Gateway
farm declaration is the authoritative fix for the missing parent row; Director
population may lag or continue reporting stale FLS-side state for a short
interval.

If Director cannot reach RabbitMQ, nudge the stuck broker path first, then
Director:

```bash
docker exec dune_server-director-1 sh -lc 'nc -vz -w2 game-rmq 5672; nc -vz -w2 admin-rmq 5672; nc -vz -w2 postgres 5432'
docker compose --env-file .env restart admin-rmq director
```

If a specific map container is exited, dead, missing from `active_server_ids`,
or crashing with `Local partition is not found`, use the fixed-partition map
recovery helper instead of a nudge.

## Map Watchdog

Compose does not currently restart map containers automatically, and a generic `restart: always` policy is not safe for fixed-partition maps because a fast blind restart can reproduce the stale server-id crash loop.

Run the watchdog from an operator shell or a host-level service manager if you want automatic recovery:

```bash
COMPOSE_FILES='compose.yaml:compose.allmaps.yaml' \
  ./scripts/watch-maps.sh .env
```

The watchdog only recovers services that already have containers; it does not start maps that were intentionally never launched. Before each recovery scan it probes the map-to-Postgres path. If that dependency is degraded, it repairs LAN/bridge rules and neighbor entries at a bounded interval, then pauses map restarts until the dependency recovers. This prevents a shared network failure from turning into a fleet-wide restart storm.

Once the dependency path is healthy, it recovers containers that are `exited` or `dead`, and checks running maps against Postgres for partition registration. A running map is recovered after two consecutive degraded observations by default, unless it is already exited/dead or an operator invokes `--once`. Recovery delegates to `scripts/recover-map.sh`, so the old partition owner is marked dead and aged out before the service starts again. After the recovered partition is ready/alive/active, `recover-map.sh` runs `scripts/restart-post-start-health.sh`; that reapplies process-local runtime patches such as the instant-logoff timer patch. Do not replace this with a raw `docker compose up -d <map>` on live maps.

If a raw Compose map start or recreate is unavoidable during live repair, finish
with:

```bash
./scripts/restart-post-start-health.sh
./scripts/patch-logoff-timers-runtime.sh --local --dry-run
```

The dry run must show the target containers' logoff timer arrays as `0 0 0 0`.

After a Funcom update the patch offsets are re-derived from the running binary
(`DUNE_LOGOFF_TIMER_AUTO_REMAP_ENABLED`, default true). The remap finds the
backend timer by its function prologue and, if that drifts, by an anchor scan on
the deadline clamp and duration reload. If the client-visible UI timer globals
cannot be found it patches backend-only instead of leaving the build unpatched.
`scripts/update-owned-steam-build-and-restart.sh` runs
`scripts/test-logoff-timer-remap.sh` against the new server binary before the
restart; run it by hand on any extracted `DuneSandboxServer-Linux-Shipping`.
The runtime patch is in-memory, so it is reapplied by the post-start hooks after
every map start.
On 2026-06-02, DD1 lost instant logout because it was manually recreated without
that post-start step; the process came back with `30`/`300` second runtime
timers despite correct INI values.

For intentional manual starts, use the wrapper instead of raw Compose:

```bash
./scripts/start-map-with-post-hooks.sh .env deep-desert
```

Use `scripts/recover-map.sh` instead when the map has stale partition
registration or `Local partition is not found`; the wrapper does not mark old
partition owners dead.

## Director/FLS Publication Watchdog

`scripts/director-watchdog.sh` monitors both the Director container and the
Director/Gateway publication path. Three consecutive failed publication checks
trigger `scripts/recover-publication.sh`, which uses `scripts/restart-target.sh
publication` and the normal post-start health hooks. A five-minute cooldown
prevents repeated publication recovery from becoming a restart loop.

Install the complete host-level auto-healing set on the active server:

```bash
test "$(hostname)" = kspls0
./scripts/install-autoheal-services.sh .env
systemctl --no-pager --full status dune-map-watchdog.service \
  dune-director-watchdog.service dune-lan-reflection.timer
```

The installer renders paths for the current checkout, enables the two
watchdogs, and runs LAN reflection from a one-minute timer. The timer is
intentional: firewall reloads can remove runtime bridge rules while a
`RemainAfterExit` oneshot would still appear active.

## Landsraad Coriolis Guard

Confidence: high.

Standard PvE DD must keep a weekly Coriolis cycle even when Coriolis damage,
shifting sands, cycle-end restart, and DB wipe are disabled. Landsraad derives
its active/suspended window from the Coriolis cycle. Setting
`m_CycleDurationInDays=36524` prevented visible Coriolis rollover, but also
globally suspended Landsraad while the database term still looked active.

Required live values in `config/UserGame.ini` and
`config/UserGame.deep-desert-coriolis.ini`:

```ini
m_bCoriolisAutoSpawnEnabled=False
m_bCoriolisDoesDamage=False
m_bCoriolisTriggerShiftingSands=False
m_CycleDurationInDays=7
m_bShouldRestartServerOnCycleEnd=False
m_bIsDbWipeEnabled=False
```

Run the guard before and after any Coriolis or DD1 map restart work:

```bash
./scripts/validate-landsraad-coriolis-cycle.sh .env
```

`scripts/restart-target.sh`, `scripts/start-map-with-post-hooks.sh`, and
`scripts/restart-post-start-health.sh` run this guard by default. Leave
`DUNE_LANDSRAAD_CORIOLIS_GUARD_ENABLED=true` unless intentionally testing in a
non-production lab.

On hosts affected by the Docker bridge neighbor-learning failure, run the host
systemd watchdog with `DUNE_WATCH_SEED_NEIGHBORS=true`. That makes the watchdog
run `scripts/seed-gateway-neighbor.sh` periodically after recovery scans,
keeping the known control-plane and map-to-RabbitMQ/Postgres neighbor entries
warm without putting seeding ahead of map crash detection. Neighbor seeding is
best-effort, runs no more often than `DUNE_WATCH_SEED_INTERVAL`, default `300`
seconds, and is bounded by `DUNE_WATCH_SEED_TIMEOUT`, default `90` seconds, so
a slow Docker inspection or namespace command cannot stall map recovery
indefinitely.

Optional maps are not recovered solely on `farm_state.ready=false`, because
some live builds can report `ready=false` after the map log has reached
`Server farm is READY`. The core partitions (by default `1,2`, or the
`DUNE_CORE_PARTITION_IDS` value) require `ready=true` in addition to alive and
active. To make readiness strict for every watched map, set:

```bash
DUNE_WATCH_REQUIRE_READY=true
```

Check what it is monitoring without changing state:

```bash
COMPOSE_FILES='compose.yaml:compose.allmaps.yaml' \
  ./scripts/watch-maps.sh .env --status
```

Validate watchdog status and dry-run behavior without Docker:

```bash
make test-watch-maps
```

To run it under systemd on this host, install a rendered unit for the current checkout:

```bash
./scripts/install-map-watchdog-service.sh .env
sudo systemctl enable --now dune-map-watchdog.service
systemctl status dune-map-watchdog.service
```

The installer updates `WorkingDirectory` and `ExecStart` for the current repo path before copying the unit to `/etc/systemd/system/`.

The full-farm unit also renders `ExecStop` to
`scripts/stop-full-warm-pool.sh`. Systemd stop, reboot, and shutdown therefore
use `restart-target.sh` in its shutdown phase, including the normal ordered
world-stop behavior, instead of depending on Docker's process termination
order. `TimeoutStopSec=1800` gives the clean stop the same bounded window as
startup.

### Dynamic public IPv4 changes

`scripts/public-ip-monitor.sh` compares the public IPv4 with
`EXTERNAL_ADDRESS`; unchanged addresses and DNS names are no-ops. A change is
dry-run-only by default. Execution requires both
`DUNE_PUBLIC_IP_MONITOR_ENABLED=true` and an exact short-hostname match in
`DUNE_PUBLIC_IP_MONITOR_ALLOWED_HOST`.

Before a committed change, the monitor archives `.env` and the RabbitMQ TLS
directory. It generates and verifies replacement TLS material in staging before
updating `EXTERNAL_ADDRESS`, and updates `GAME_RMQ_PUBLIC_HOST` only when that
value followed the old address. It then installs the staged certificate and
invokes the normal target-aware full-farm restart. A failed restart remains in
`restarting` state and is retried on the next timer check even though the
address already matches. Status is stored under
`backups/admin-panel/public-ip-monitor.state`.

Before arming it, run the signed disposable repair proof from **Infrastructure
→ Public-IP Repair Proof**. It executes the real rewrite, OpenSSL SAN rotation,
restart handoff/retry, and timer rendering against temporary state without
opening live configuration, TLS, systemd, Docker, map, or network state. The
Feature Readiness row promotes only when that input-bound receipt is current
and the live monitor is armed. See
[`public-ip-repair-canary.md`](public-ip-repair-canary.md).

```bash
hostname -s
./scripts/public-ip-monitor.sh .env status
./scripts/public-ip-monitor.sh .env check
make test-public-ip-canary
make install-public-ip-monitor ENV_FILE=.env
systemctl status dune-public-ip-monitor.timer
```

Changing the interval requires reinstalling the timer. Disable it with
`systemctl disable --now dune-public-ip-monitor.timer`; uninstalling the timer
does not remove backups or rewrite `.env`.

## Expanded Farm Startup

Start the full standing farm after the core service layer is healthy:

```bash
./scripts/full-world-partitions.sh .env

docker compose --env-file .env up -d \
  survival overmap arrakeen harko-village \
  testing-hephaestus testing-carthag testing-waterfat \
  deep-desert proces-verbal

./scripts/status.sh .env
```

The full-farm partition layout is intentionally smaller than `initialize_partitions_full_battlegroup()`. Funcom's helper creates many extra `Survival_1` and other map dimensions. The Compose layout starts exactly one container for each current travel target, so the local partition script creates exactly those rows.

Capture the known-good state after all nine maps register:

```bash
./scripts/backup-state.sh .env
./scripts/capture-routing.sh .env full-farm-ready
```

For the 30-partition warm pool:

```bash
./scripts/start-full-warm-pool.sh .env
COMPOSE_FILES='compose.yaml:compose.allmaps.yaml' ./scripts/rmq-health.sh .env
```

The helper starts Postgres and both RabbitMQ services first, waits for their
health checks, writes the 30-partition layout, and starts the service layer. It
then reads `backups/admin-panel/autoscaler.json` when present, falling back to
`.env`. Minimum, balanced, and selective custom profiles start only their
always-on maps; full warm starts maps in batches: `survival`/`overmap`,
partitions 3-9, then partitions 10-30. It uses `--no-recreate` for normal
startup so a routine online operation does not replace Postgres under running
game servers. See [`autoscaling-memory.md`](autoscaling-memory.md).

The helper also seeds required Docker bridge neighbor entries before and after
the control-plane services start. This is a site-specific guard for the current
`dune_server_default` bridge, where recreated containers have sometimes failed
to learn existing peers or kept stale permanent neighbor entries. The gateway
keeps `172.31.240.40` and MAC `02:42:ac:1f:f0:28`; `admin-chat-commands` is
pinned to `172.31.240.41` so it cannot take the gateway address during a
recreate. The seeder derives Postgres and live container MACs with
`docker inspect`, pre-seeds Postgres with the gateway's static MAC before
gateway starts, then refreshes both directions while gateway is still in its
startup sleep. Run the same step manually after force-recreating `gateway`,
`director`, `game-rmq`, `rmq-auth-shim`, or `text-router`:

```bash
./scripts/seed-gateway-neighbor.sh
```

Known working baseline as of May 19, 2026:

```text
gateway declares Snapetech PVE Friendly Server (www.snape.tech) to FLS
gateway reaches postgres:5432
game-rmq reaches rmq-auth-shim:8080 and text-router:8080
director reaches game-rmq:5672, admin-rmq:5672, and postgres:5432
director heartbeat to FLS succeeds
current_alive_active=31 active_servers=31 partitions=31
```

The server-browser title appears in separate parent and nested rows.
`config/gateway.ini` `[gateway].display_name`, `PUBLIC_SERVER_NAME`,
`WORLD_NAME`, and `DUNE_SERVER_DISPLAY_NAME` must all contain the exact same
approved title. To refresh the server-browser rows without disconnecting
existing map sessions, recreate only `gateway`, then reseed the gateway/Postgres
bridge neighbor entries:

```bash
docker compose --env-file .env up -d --force-recreate --no-deps gateway
./scripts/seed-gateway-neighbor.sh
```

Known brittle area: Docker bridge peer discovery on this host. Do not remove
the neighbor-seeding step until a maintenance-window network rebuild proves
that newly recreated control-plane containers can connect without it.

### Additional Survival Sietches

The static warm pool keeps the official travel targets stable. Additional
`Survival_1` dimensions are managed by `scripts/sietches.sh` and an overlay
generated under `backups/admin-panel`. The configuration and overlay are mode
`0600` because per-Sietch passwords are local secrets.

`set-active` clones the first Survival partition definition for missing
dimensions and blocks dimensions above the requested active count; it does not
delete their world rows. Each committed topology change creates a custom-format
Postgres backup first. Additional processes have isolated saved-data volumes,
fixed Docker addresses, and deterministic port pairs. All-farm restart,
shutdown, full-pool start, and systemd `ExecStop` include these managed
containers. The admin panel's Docker-socket fallback now performs the same
`stop-managed` / `reconcile` lifecycle through its short-lived host helper;
it no longer loses generated Sietches merely because the panel image lacks a
Docker CLI. The helper reads `DUNE_GAME_DB_NAME` from the active env file for
both topology writes and backups instead of assuming a database named `dune`.

```bash
./scripts/sietches.sh .env list
./scripts/sietches.sh .env set-active 4
./scripts/sietches.sh .env set-active 4 --execute
./scripts/sietches.sh .env set-settings 32 "Sietch Alpha" "secret" --execute
./scripts/sietches.sh .env reconcile --execute
```

Settings changes refuse a partition with connected players. Decreasing the
active count also refuses to remove a managed container with connected players.
The supported ceiling is 64 Survival dimensions. Forward `udp/8001-8063` and
`udp/8101-8163` when those dimensions must be publicly reachable.

For unattended operation, install the complete auto-healing set after startup:

```bash
test "$(hostname)" = kspls0
./scripts/install-autoheal-services.sh .env
```

For unattended backup sync, configure one of the examples under
`examples/backup/`, test `scripts/backup-offsite.sh` manually, then install:

```bash
./scripts/install-backup-offsite-timer.sh .env examples/backup/rclone-offsite.env
```

Verify backup readability after changing the backup path:

```bash
./scripts/verify-backup.sh backups/<backup-id>
```

## Network Ports

For a single `Survival_1` layout, forward:

```text
7777/udp
```

For the expanded standing farm, forward:

```text
7777-7785/udp
```

For the 30-partition warm pool, forward:

```text
7777-7806/udp
7888-7917/udp
```

`7888-7918/udp` are the IGW ports paired with the 31 warm-pool game ports. If your deployment relies on them for live-client routing or server-browser checks, forward them to the Dune host. Do not remove IGW forwards from a working deployment without packet-capture evidence and a router backup.

Live-client login also asks FLS for a game RabbitMQ address before it starts the gameplay UDP leg. `GAME_RMQ_PUBLIC_HOST` and `GAME_RMQ_PUBLIC_PORT` control the AMQP address Gateway reports to FLS. `GAME_RMQ_PUBLIC_HTTP_PORT`, default `15673/tcp`, controls the HTTP address Gateway reports to FLS, while `GAME_RMQ_HTTP_BIND_ADDRESS` controls the host bind for RabbitMQ management and defaults to `127.0.0.1`. For a publicly reachable live self-hosted server, forward `GAME_RMQ_PUBLIC_PORT` TCP, default `31982/tcp`, to the host. Do not forward Postgres, RabbitMQ management, or admin panel ports unless a targeted browser-ping experiment explicitly requires it.

For server-browser ping work, use the non-mutating diagnostics first:

```bash
make browser-ping-proof ENV_FILE=.env
make browser-ping-diagnostics ENV_FILE=.env
make client-browser-ping-verifier ENV_FILE=.env
make client-browser-ping-verifier-external ENV_FILE=.env
```

`browser-ping-proof` runs the server-side diagnostics plus the external client
verifier. `browser-ping-diagnostics` checks the server-side publication path,
including the latest FLS battlegroup declaration and whether the starting
partition is advertised as `EXTERNAL_ADDRESS:GamePort`.
`client-browser-ping-verifier` checks the local client host's public/LAN ICMP,
AMQP, and RMQ HTTP reachability and inspects the Dune client log for fresh
browser/RMQ evidence. The `-external` target also runs public Globalping probes
against the advertised host to prove WAN ICMP reachability independently of LAN
reflection. To wait for the next in-game browser refresh without packet
capture:

```bash
make watch-client-browser-ping-log ENV_FILE=.env SECONDS=300
```

`watch-browser-probe` is a packet-capture helper and should only be used when
that evidence is explicitly wanted.

### Internal Clients and LAN Reflection

For a public server, keep `EXTERNAL_ADDRESS` set to the public WAN address even
for internal players. Internal players joining through the server listing will
still be handed that public address by the Dune/FLS path.

Do not make changing `EXTERNAL_ADDRESS` part of normal operations. Instead,
choose one LAN reflection mode and document it for the site:

- router UDP hairpin/NAT reflection;
- router static route for the public `/32` to the Dune host;
- per-client route for the public `/32` to the Dune host.

See `docs/lan-reflection.md` for the standard setup and validation commands.

## Artificial Exchange Buyer

Use `scripts/build-exchange-catalog.py` to build the reviewed local catalog and
`scripts/artificial-exchange-bot.py` for dry-run scans. Real purchases require
the explicit artificial Exchange gates and confirmation phrase. Settlement
auto-claim remains disabled; use the bot's `--settlement-report` mode to inspect
completed orders. See `docs/artificial-exchange.md`.

Install the long-running service with:

```bash
python3 scripts/artificial-exchange-bot.py --check-ready
make install-artificial-exchange-buyer-service ENV_FILE=.env
```

The Exchange populator is separate from the buyer. Leave
`dune-artificial-exchange-populator.service` stopped unless you are deliberately
seeding operator-owned NPC listings. Current populator policy requires
dune.exchange price evidence and tier 2+ rows; see `docs/artificial-exchange.md`
before starting it.

## Backup

Use the backup helper for routine snapshots:

```bash
./scripts/backup-state.sh .env
```

It writes:

- `backups/<timestamp>/postgres-dune_sb_1_4_0_0.dump`
- `backups/<timestamp>/rabbitmq-admin.tgz`
- `backups/<timestamp>/rabbitmq-game.tgz`
- `backups/<timestamp>/server-saved.tgz`
- `backups/<timestamp>/manifest.txt`

The helper intentionally writes only under `backups/`, which is ignored by git. Restore also refuses backup directories outside `backups/` so local artifacts do not drift into publishable paths.

Host backups serialize with Admin-panel manual/scheduled backups and the entire
assured deployment workflow through `backups/admin-panel/operation.lock`.
Standalone callers wait up to `DUNE_OPERATION_LOCK_WAIT_SECONDS`; scheduled
panel runs record a deferral and retry without counting lock contention as a
backup failure. Executing browser maintenance owns that same lock across
preflight, disconnect, stop, backup, update, start, recovery, and online proof;
a busy lock defers before player impact. Configure coverage-checked schedules,
retries, retention, and monitoring as described in
[`automatic-backups.md`](automatic-backups.md), and review cross-scheduler
admission in [`operations-calendar.md`](operations-calendar.md).

The Operations page also provides the durable Prometheus on-call inbox.
Successful source polls alone advance or resolve alert state; failed polls
retain the last active set. Repeated state is deduplicated, re-fires create a
new generation, and authenticated acknowledgement records ownership without
silencing Prometheus. Only transitions enter signed webhook delivery. See
[`alert-inbox.md`](alert-inbox.md).

Manual Postgres dump:

```bash
docker compose --env-file .env exec -T postgres \
  pg_dump -U dune -d dune_sb_1_4_0_0 -Fc \
  > backups/dune-$(date -u +%Y%m%dT%H%M%SZ).dump
```

If the optional streaming replica is enabled, prefer taking routine logical dumps from the read-only standby:

```bash
mkdir -p backups
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml exec -T postgres-replica \
  pg_dump -U dune -d dune_sb_1_4_0_0 -Fc \
  > backups/dune-replica-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Check replica health before relying on it:

```bash
docker compose --env-file .env -f compose.yaml -f compose.replica.yaml exec -T postgres \
  psql -U dune -d dune_sb_1_4_0_0 -c \
  "select application_name,state,sync_state,write_lag,flush_lag,replay_lag from pg_stat_replication;"
```

See `docs/postgres-replication.md` for setup, lag checks, and failure handling. Streaming replication is not point-in-time recovery; bad writes and deletes replicate immediately.

For the remote standby layout, use the snapshot helper instead of pulling dumps back to the primary host:

```bash
./scripts/replica-snapshot.sh .env replica.example.lan /srv/dune-postgres-replica
./scripts/install-replica-snapshot-timer.sh .env replica.example.lan /srv/dune-postgres-replica
```

This streams rolling dumps from the remote replica to the local
`backups/replica-snapshots` path. The installed timer runs as the installing
local user by default so SSH uses that user's keys and host aliases. The
snapshot path is required before the timer runs, which lets it use the mounted
backup filesystem. Use `DUNE_REPLICA_SNAPSHOT_USER` and
`DUNE_REPLICA_SNAPSHOT_GROUP` only for a dedicated service account with working
remote SSH credentials. Use it alongside daily offline full-state backups.

Check all backup layers:

```bash
./scripts/backup-layers-status.sh .env replica.example.lan /srv/dune-postgres-replica
```

Archive RabbitMQ state from inside the running containers:

```bash
docker compose --env-file .env exec -T admin-rmq \
  tar -czf - -C /var/lib/rabbitmq . \
  > backups/rabbitmq-admin-$(date -u +%Y%m%dT%H%M%SZ).tgz

docker compose --env-file .env exec -T game-rmq \
  tar -czf - -C /var/lib/rabbitmq . \
  > backups/rabbitmq-game-$(date -u +%Y%m%dT%H%M%SZ).tgz
```

Archive server saved state:

```bash
tar -czf backups/server-saved-$(date -u +%Y%m%dT%H%M%SZ).tgz data/server-saved
```

Keep `backups/` local. Database dumps can contain player, world, and account-linked state.

## Restore

Restore is disruptive. It stops services that write state before replacing database contents, and optional RabbitMQ/server-saved restores replace local state directories. Run a dry run first and do not perform a real restore while players are online.

Stop services that write state:

```bash
docker compose --env-file .env down
```

Use the restore helper for Postgres:

```bash
./scripts/restore-state.sh --dry-run .env backups/YYYYMMDDTHHMMSSZ
./scripts/restore-state.sh .env backups/YYYYMMDDTHHMMSSZ
```

Restore RabbitMQ and saved state only when you intentionally want to replace those local directories:

```bash
./scripts/restore-state.sh --dry-run --rabbitmq --server-saved .env backups/YYYYMMDDTHHMMSSZ
./scripts/restore-state.sh --rabbitmq --server-saved .env backups/YYYYMMDDTHHMMSSZ
```

Manual restore:

```bash
docker compose --env-file .env up -d postgres
docker compose --env-file .env exec -T postgres \
  pg_restore -U dune -d dune_sb_1_4_0_0 --clean --if-exists \
  < backups/dune-YYYYMMDDTHHMMSSZ.dump
```

Then start the rest of the stack:

```bash
docker compose --env-file .env up -d admin-rmq game-rmq rmq-auth-shim text-router gateway director
```

## Upgrade Checklist

Before changing `DUNE_IMAGE_TAG`:

- Capture `docker compose --env-file .env ps`.
- Run `./scripts/status.sh .env` and save the output locally.
- Back up Postgres, RabbitMQ, and `data/server-saved`.
- Record the old `DUNE_IMAGE_TAG`.
- Run `./scripts/check-steam-update.sh .env` to compare `.env` with the current Steam package.
- Load the new Funcom image tarballs with `./scripts/load-images.sh .env`.
- Update the env pin with `./scripts/check-steam-update.sh .env --write-env`, or edit `DUNE_IMAGE_TAG` manually if multiple tags are reported.
- Run `docker compose --env-file .env config --quiet`.
- Start only `postgres`, `admin-rmq`, and `game-rmq`.
- Run `docker compose --env-file .env run --rm db-init`.
- Start the service layer and check `./scripts/status.sh .env`.
- Attempt Hagga Basin login before testing cross-map routing.

Rollback means restoring the old image tag and restoring state from the backups taken before the upgrade. Do not mix a downgraded image tag with post-upgrade database state unless you have verified the schema did not change.
