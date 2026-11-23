# Control-Plane and Fleet Auto-Healing

Confidence in the immediate cause is high: the Docker bridge/control-plane
network path failed while the Postgres process itself remained healthy. The
exact event that removed or invalidated the bridge rules or neighbor state is
unknown.

## Failure chain

The incident followed this sequence:

1. Game-map processes timed out connecting to Postgres and retained stale
   invalid database handles.
2. `pg_isready` inside a map failed even though the Postgres container was
   healthy.
3. Reapplying LAN reflection/firewall rules and seeding bridge neighbors
   restored packet flow.
4. The existing map processes still needed controlled process restarts to
   discard their stale handles.
5. Director/Gateway FLS publication was downstream of the same control plane,
   so browser visibility and capacity publication could lag until the control
   plane and map fleet were healthy.

A database restart is therefore not the default fix for this failure class.
The first split is host/container network reachability versus database process
health.

## Recovery layers

The repository now has four bounded layers:

- `scripts/control-plane-health.sh` probes Postgres from running map
  containers and probes the Postgres container itself. `--repair` reapplies
  LAN/bridge rules and neighbor entries, then probes again.
- `scripts/watch-maps.sh` runs the dependency preflight before map recovery.
  It pauses all map restarts while the shared path is degraded, and requires
  two consecutive degraded observations before recovering a running map.
  Exited/dead maps and explicit `--once` operations remain immediate.
- `scripts/director-watchdog.sh` checks Director/Gateway FLS publication.
  It uses the shared DASH operation lock so it cannot recover services during
  a deliberately stopped maintenance or backup window.
  Three failed checks trigger `scripts/recover-publication.sh`, which uses
  `scripts/restart-target.sh publication` and the normal post-start hooks.
- `dune-lan-reflection.timer` reruns the idempotent host network repair every
  minute. This repairs runtime rules lost after firewall or Docker network
  changes while avoiding a permanently-active oneshot service.

The Director recovery has a five-minute cooldown. The map watchdog and
network repair intervals are independently configurable so a shared outage
does not become a restart storm.

## Install on the active host

Run this only on the production host:

```bash
test "$(hostname)" = kspls0
./scripts/install-autoheal-services.sh .env
```

The installer renders paths for the current checkout, reloads systemd, and
enables:

- `dune-map-watchdog.service`
- `dune-director-watchdog.service`
- `dune-lan-reflection.timer`

It also runs one immediate LAN reflection pass. Keep only one host-side
watchdog instance; the watchdog scripts use lock files.

If an old ad-hoc Director watchdog is running, stop that exact process after
the managed unit is active. Do not stop unrelated processes.

## Verify

```bash
test "$(hostname)" = kspls0
./scripts/control-plane-health.sh .env --full --quiet
./scripts/status.sh .env
systemctl is-active dune-map-watchdog.service dune-director-watchdog.service
systemctl is-active dune-lan-reflection.timer
systemctl list-timers --all dune-lan-reflection.timer
```

The normal production verdict should show all required maps alive/active,
the FLS publication window healthy, and Director/Gateway auth checks passing.
The runtime logoff-timer dry run remains a separate build-specific check:

```bash
./scripts/patch-logoff-timers-runtime.sh --local --dry-run
```

If that check reports no UI timer candidate for the current build, do not
invent offsets. Record it as a separate residual and wait for a build-specific
mapping update.

## Rollback

Disable only the managed auto-healing units if they cause an unrelated issue:

```bash
sudo systemctl disable --now dune-director-watchdog.service
sudo systemctl disable --now dune-map-watchdog.service
sudo systemctl disable --now dune-lan-reflection.timer
```

This does not remove Docker data or alter world state. Re-enable the units
after the underlying condition is understood.

## Configuration

The main controls are in `.env` or the rendered systemd environment:

- `DUNE_WATCH_CONTROL_PLANE_INTERVAL` — probe interval, default 30 seconds.
- `DUNE_WATCH_CONTROL_PLANE_REPAIR_INTERVAL` — minimum repair interval,
  default 300 seconds.
- `DUNE_WATCH_DEGRADED_CONFIRMATIONS` — running-map confirmation count,
  default 2.
- `DUNE_WATCH_REQUIRED_READY_PARTITIONS` — partitions that must report
  `ready=true`; default is the configured core IDs or `1,2`.
- `DUNE_DIRECTOR_WATCHDOG_PUBLICATION_INTERVAL` — FLS check interval,
  default 60 seconds.
- `DUNE_DIRECTOR_WATCHDOG_PUBLICATION_FAILURE_THRESHOLD` — failed checks
  before publication recovery, default 3.
- `DUNE_DIRECTOR_WATCHDOG_RECOVERY_COOLDOWN` — publication recovery cooldown,
  default 300 seconds.

Do not disable the control-plane preflight to make a degraded map appear
healthy. If it is noisy, fix the bridge/firewall/neighbor condition or adjust
the interval after collecting evidence.
