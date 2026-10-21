# Maintenance Updates

This page documents the daily restart, backup, candidate-certified Steam-package update, and return-online flow.

For operator-initiated browser updates, DASH adds a candidate-bound signed
readiness gate before this existing workflow. It verifies recovery, current
PostgreSQL and RabbitMQ recovery proofs, Compose/Coriolis/post-start hooks, Desired State, SLO/change
integrity, response readiness, and the latest assured deployment without
executing an update. The browser stages Steam separately, certifies that exact
local candidate, then disables further acquisition during apply so the build
cannot change inside the restart workflow. See
[`update-readiness.md`](update-readiness.md).

The hotfix timer also stages-only by default when
`DUNE_UPDATE_REQUIRE_READINESS_RECEIPT=true`. It does not load/restart until an
operator certifies and applies the candidate in Infrastructure. The legacy
fully automatic behavior requires the explicit
`DUNE_HOTFIX_AUTO_APPLY_WITHOUT_READINESS=true` opt-out.

Daily maintenance uses the same trust boundary. The default `certified` policy
checks readiness before warning players and revalidates the exact staged
candidate immediately before any disconnect or stop. When no update is staged,
or a staged candidate lacks a current passing receipt, the useful daily restart
continues on the currently loaded build without acquiring or ingesting package
content. Candidate drift therefore cannot turn a scheduled current-build
restart into an unreviewed update.

## Why 06:00

The daily maintenance target is 06:00 local host time. The timer schedules the job at 05:30 so players receive a 30-minute warning window before the restart begins.

`06:00` remains the unattended timer default and the comparison/fallback
baseline. The Operations page can instead schedule an exact future timestamp
recommended by the player-impact planner. Exact-time warnings begin at the
configured lead time rather than immediately when a job is created. See
[`player-impact-maintenance.md`](player-impact-maintenance.md).

The 06:00 target is deliberately after Funcom's nightly maintenance window. If Steam receives a self-hosted server hotfix during Funcom maintenance, the local Steam package has time to update before DASH stops the world, takes the stopped-world backup, loads the new image tarballs, and starts the farm again.

## Default Timeline

```text
05:30  dune-daily-maintenance-schedule.timer fires
05:30  scripts/schedule-daily-maintenance.sh reconciles/posts the restart job and verifies persistence
05:30  staged candidate receives an early readiness check; uncertified falls back to current
05:30  first in-game warning is sent immediately
05:30-05:55  warnings repeat every 5 minutes
05:55-06:00  warnings repeat every 1 minute
06:00  final "starting now" warning is sent
06:00  exact staged candidate and signed receipt are revalidated
06:00  affected online players are soft-disconnected if the disconnect gates are enabled
06:00  selected services are stopped
06:00  maintenance backup is written and independently verified
06:00  certified package images are loaded, or update is skipped on the current build
06:00  if a newer Steam build was installed, kspls0 reboots and arms boot-time resume
06:00  official DB upgrade patches are applied
06:00  operator DB patch markers and stale player RabbitMQ sessions are cleaned
06:00  selected services are recreated
06:00+ post-start health checks and farm readiness wait run
```

The configured defaults live in `.env`:

```env
DUNE_RESTART_CHECK_STEAM_UPDATE=true
DUNE_REBOOT_AFTER_STEAM_UPDATE=false
DUNE_UPDATE_REBOOT_HOST=kspls0
DUNE_RESTART_STEAM_UPDATE_MODE=auto
DUNE_RESTART_STEAM_CLIENT_TRIGGER=true
DUNE_RESTART_STEAM_CLIENT_WAIT_SECONDS=900
DUNE_RESTART_STEAM_CLIENT_MIN_WAIT_SECONDS=30
DUNE_STEAM_CLIENT_COMMAND=steam
DUNE_RESTART_STEAMCMD_UPDATE=true
DUNE_RESTART_STEAMCMD_REQUIRED=false
DUNE_RESTART_STEAMCMD_HELPER_IMAGE=cm2network/steamcmd:root
DUNE_STEAM_APP_ID=4754530
DUNE_STEAM_LOGIN=anonymous
DUNE_STEAM_PASSWORD=
DUNE_STEAMCMD_COMMAND=steamcmd
DUNE_STEAMCMD_VALIDATE=true
DUNE_STEAMCMD_TIMEOUT_SECONDS=1800
DUNE_DAILY_RESTART_SCHEDULE_WINDOW=05:25-05:35
DUNE_DAILY_RESTART_ALLOW_OUTSIDE_WINDOW=false
DUNE_DAILY_RESTART_TIME=06:00
DUNE_DAILY_RESTART_REPEAT_SECONDS=600
DUNE_DAILY_RESTART_RETRY_SECONDS=60
DUNE_DAILY_RESTART_RETRY_WINDOW_SECONDS=1500
DUNE_DAILY_RESTART_MESSAGE=Daily maintenance restart at 6:00 AM. Please get to a safe place.
DUNE_DAILY_RESTART_UPDATE_POLICY=certified
DUNE_RESTART_CLEAR_PLAYER_RMQ_SESSIONS=true
```

## Certified Maintenance Policy

`DUNE_DAILY_RESTART_UPDATE_POLICY` has three explicit modes:

| Policy | Acquisition during the job | Update behavior |
| --- | --- | --- |
| `current` | Never | Restart only the currently loaded build. |
| `certified` | Never | Apply an already staged candidate only when its current signed readiness receipt passes immediately before shutdown; otherwise schedule a current-build restart. |
| `automatic` | Legacy acquisition path | Refresh and ingest during maintenance. Rejected while `DUNE_UPDATE_REQUIRE_READINESS_RECEIPT=true`. |

The scheduler's first readiness check avoids announcing an update that cannot
run. The server-side second check is authoritative and happens before player
disconnect, map stop, or backup. The stopped-world backup must then pass the
normal mixed-evidence verifier before any candidate can be applied. Targeted map restarts are always `current`;
only an all-farm job can change the build. Jobs persisted by an older DASH
release have no policy field and are treated as `current`.

For certified apply, Admin passes
`DUNE_RESTART_STEAM_UPDATE_MODE=none` into the exact restart job. Both the host
and Docker-socket execution paths handle `none` before any Steam client or
SteamCMD branch, so this mode cannot trigger a desktop client, acquire a
different build, or race the signed candidate.

## Install The Timer

After the admin panel restart path works manually, install the host timer:

```bash
./scripts/install-daily-maintenance-timer.sh .env
systemctl list-timers dune-daily-maintenance-schedule.timer --all --no-pager
```

The installer renders:

- `config/systemd/dune-daily-maintenance-schedule.service`
- `config/systemd/dune-daily-maintenance-schedule.timer`

The timer runs at `05:30:00` and creates an admin-panel restart job with an
explicit `runAt` of 06:00 local time. The scheduler retries and reconciles the
persisted job until the retry deadline, so a slow panel or lost HTTP response
cannot silently skip the morning maintenance. The job carries a date key and
is idempotent across timer retries.

The timer is persistent. If the host is down at 05:30, systemd runs the
scheduler after boot; it targets the next safe 06:00 local window instead of
creating an immediate late restart.

Before it creates the daily job, the scheduler takes the hotfix updater lock,
refreshes the Steam server package, and checks the installed, target, and
image-loaded build IDs against the package tag. If a newer package is staged
but the readiness receipt is unavailable or failing, it leaves the existing
farm untouched and does not queue a current-build restart. All-farm starts
repeat the build and running-image check after Compose starts; a stale or
unverifiable build makes the start phase fail with the check output attached.

To enable a host reboot only when maintenance actually installs a newer Steam
build, first install the boot-time resume service on `kspls0`:

```bash
hostname  # must print kspls0
./scripts/install-update-reboot-resume-service.sh .env
systemctl status dune-update-reboot-resume.service --no-pager
```

Then set:

```env
DUNE_REBOOT_AFTER_STEAM_UPDATE=true
DUNE_UPDATE_REBOOT_HOST=kspls0
```

The feature is disabled by default. Do not enable it until the resume service
is installed and enabled. A changed Docker image tag and a changed Steam
build ID under the same Docker tag both count as an update. A normal daily
maintenance run with an already-current Steam build does not reboot the host.

`scripts/schedule-daily-maintenance.sh` also refuses to schedule outside `DUNE_DAILY_RESTART_SCHEDULE_WINDOW` by default. This is a second guard for manual runs, timer mistakes, and stale installed units. For a deliberate manual test, set:

```bash
DUNE_DAILY_RESTART_ALLOW_OUTSIDE_WINDOW=true ./scripts/schedule-daily-maintenance.sh
```

The scheduler sends `DUNE_ADMIN_TOKEN` as a Bearer credential whenever the
token is configured, even when `DUNE_ADMIN_REQUIRE_TOKEN=false`. This supports
route-level access control and owner audit attribution on otherwise trusted
private deployments. If `DUNE_ADMIN_REQUIRE_TOKEN=true` and the token is empty,
the scheduler fails before making the request.

## Restart Flow

Executed restart jobs use this sequence:

```text
candidate/receipt preflight -> soft-disconnect -> stop -> backup -> verify -> staged-only update -> start -> online wait -> signed outcome
```

With update-triggered reboot enabled and a new build detected, the sequence is:

```text
candidate/receipt preflight -> soft-disconnect -> stop -> backup -> verify -> staged-only update -> durable checkpoint -> host reboot
-> boot-time start -> post-start hooks and farm readiness verification
```

The checkpoint is `backups/admin-panel/update-reboot-resume.env`. The admin
job is persisted as `awaiting_reboot`, so the restarted admin panel does not
repeat the stop, backup, or update phases. The resume service removes the
checkpoint only after the normal `restart-target.sh` start phase succeeds.

Executed shutdown jobs use this sequence:

```text
candidate/receipt preflight -> soft-disconnect -> stop -> backup -> verify -> staged-only update -> signed outcome
```

Shutdown jobs leave services offline after the backup and update check.

If the stop phase fails, no backup, update check, or start is attempted. If
backup creation or verification fails during a restart, DASH suppresses the
update, restores the current build, verifies readiness, and reports the job as
failed even when service recovery succeeds. For an intentional shutdown, the
target remains offline. Every execution produces a signed stage-by-stage
outcome; see [`maintenance-intelligence.md`](maintenance-intelligence.md).

For all-farm jobs using the `current` policy, preflight now refuses to stop the
farm unless the downloaded Steam package, loaded build ID, and configured
image tag agree. The `certified` policy remains the path for applying a staged
new build after its backup and readiness gates pass.

## Restart Batching

The `start` phase for `target=all` (a full-fleet restart, including the daily
job above) previously recreated every configured map service in one
`docker compose up -d` invocation. On a deployment with a large map catalog
this created a thundering herd: Compose starts every container essentially
simultaneously, and on `kspls0` this was measured pushing individual map
ready-times to the Capacity Intelligence collector's 300-second start timeout
-- some recorded as outright timeouts, one pair over 13 minutes -- versus the
~90-260 seconds a single guarded demand start takes. That evidence (`source:
observed` entries in `scripts/capacity-intelligence.py status` clustered at
one `startedAt` timestamp, once per day, with duration at or past the
timeout) is what identified the daily job specifically, as opposed to normal
player-triggered map transitions.

`scripts/restart-target.sh` now batches a `target=all` start into groups
(`DUNE_RESTART_ALL_BATCH_SIZE`, default 6 services per batch) with a pause
between batches (`DUNE_RESTART_ALL_BATCH_PAUSE_SECONDS`, default 60s) inside
the same helper container invocation, so the daily job (and any other
`target=all` restart) no longer forces the whole fleet through cold init at
once. This applies only to `target=all`; a targeted restart of a specific
service subset is unaffected and unbatched, since those are already small.

## Update Logic

Only the explicit legacy `automatic` policy refreshes or waits for the official
self-hosted server Steam app during the maintenance job:

```bash
./scripts/update-steam-tool.sh .env
```

With that opt-out, `DUNE_RESTART_STEAM_UPDATE_MODE=auto` uses the desktop Steam
path when available. On desktop Steam hosts, where
the Steam client is already running and owns the library, DASH sends
`steam://validate/4754530` to the client and waits for the local appmanifest
download/staging state to settle before image ingest. This avoids running a
competing SteamCMD process against the same library.

For headless hosts without a running Steam client, auto mode falls back to
SteamCMD. It runs app `4754530`, the Dune: Awakening Self-Hosted Server tool,
with `validate` and `+@sSteamCmdForcePlatformType linux`. Anonymous access is
not reliable for production hotfixes; set `DUNE_OWNED_STEAM_LOGIN` or
`DUNE_STEAM_LOGIN` to an account that owns the tool, and set
`DUNE_STEAMCMD_HOME` to a persistent SteamCMD cache such as
`/home/keith/.steamcmd-dune`. DASH sets this directory as SteamCMD's `HOME`, so
its `Steam/config` and authentication state survive root-run systemd jobs and
admin container recreation. Seed/authenticate that exact home once; a cache in
the invoking account's normal home is not used. If unattended login cannot use cached Steam Guard
state, set `DUNE_STEAM_PASSWORD_FILE` to a mode-0600 file containing the account
password (preferred over plaintext in `.env`). The updater reads it only at
execution time. On a production
headless host, prefer a SteamCMD-owned directory such as
`/home/keith/dune-steamcmd-server` over a nested Steam desktop client library
path. If `steamcmd` is not installed on the host, the restart hook can run
SteamCMD through `DUNE_RESTART_STEAMCMD_HELPER_IMAGE`. The default helper image
is `cm2network/steamcmd:root`; pre-pull it before relying on unattended
maintenance if you do not want the maintenance window to depend on a Docker Hub
pull.

When the production SteamCMD runtime is known-broken, enable the separate
worker fallback with `DUNE_STEAM_UPDATE_WORKER_ENABLED=true` plus
`DUNE_STEAM_UPDATE_WORKER_HOST`, `DUNE_STEAM_UPDATE_WORKER_SSH_USER`, and
`DUNE_STEAM_UPDATE_WORKER_DIR`. The worker must be initialized once with the
owned SteamCMD cache and SSH access. A local acquisition failure then runs the
same updates on the worker, pulls the manifest and six battlegroup tarballs back,
and continues through the normal image/tag/restart gates. Incomplete packages
fail closed.

Keep `DUNE_RESTART_STEAMCMD_REQUIRED=true` on live. When it is false, missing or
failed SteamCMD logs a warning and the flow may continue with the package
already present on disk, which can start stale images after a Funcom hotfix.

After SteamCMD returns, the update phase runs:

```bash
./scripts/check-steam-update.sh .env
```

The script reads Docker `manifest.json` entries from the official Steam package
image tarballs under `DUNE_STEAM_SERVER_DIR`, then compares the package tag with
`DUNE_IMAGE_TAG`. Dashboard readiness performs the same identity check with a
bounded seekable-tar reader: it validates headers in only the first/last 16 MiB
and seeks directly to the manifest instead of traversing image-layer payloads.

The hotfix timer also compares `DUNE_IMAGE_TAG` with every running game-map,
Director, Gateway, Text Router, and RMQ auth-shim container before it skips a
restart. If Steam and `.env` are already current but any live container still
uses an older tag, the timer performs the normal backed-up full-farm restart to
converge the deployment.

When the tags match, the update phase does nothing.

When exactly one newer package tag is found, the update phase runs:

```bash
./scripts/load-images.sh .env
./scripts/check-steam-update.sh .env --write-env
```

That loads the official Funcom image tarballs into Docker and updates `.env` to the package tag. The following start phase uses Compose `up -d --force-recreate --no-deps`, so recreated services use the new tag.

When the package is incomplete, unreadable, or contains multiple server tags, the update phase logs a warning and keeps the existing `DUNE_IMAGE_TAG`. This avoids guessing during unattended maintenance.

To disable the maintenance-window update check:

```env
DUNE_RESTART_CHECK_STEAM_UPDATE=false
```

## Pre-Start Hygiene

The maintenance start phase runs two local safety cleanups before services are
recreated:

- `scripts/apply-official-db-patches.sh` reads Funcom's
  `DuneSandbox/Database/Upgrade/__order.txt` from the configured
  `seabass-server:${DUNE_IMAGE_TAG}` image and applies any missing official SQL
  patches to `dune_sb_1_4_0_0` before maps start.
- `scripts/clear-player-rmq-sessions.sh` closes stale 16-hex player RabbitMQ
  connections and deletes matching `<FLS_ID>_queue` / `<FLS_ID>_rpcQueue`
  queues. This prevents a client from getting stuck when it reconnects after
  downtime and tries to recreate its per-player login queue.

For `DUNE_RESTART_TARGET=all`, player RabbitMQ cleanup is enabled by default.
For narrower restarts, it is skipped unless
`DUNE_RESTART_CLEAR_PLAYER_RMQ_SESSIONS=true` is set.

## Container And Host Requirements

The normal host path uses local `docker compose`.

All normal Compose services use fixed addresses on the Dune bridge network.
The network gateway is pinned to `172.31.240.1`, service containers use the
lower static range, and Docker's dynamic allocation pool is restricted to
`172.31.240.128/25`. This keeps restart helpers and recreated one-shot
containers from taking addresses reserved for RabbitMQ, text-router, map
servers, or Postgres. It also keeps the Postgres host bind
`172.31.240.1:15432` valid after a full network recreate.

Before relying on an unattended restart after Compose changes, run:

```bash
make check-compose-static-ips ENV_FILE=.env
```

Inside the admin-panel container, `scripts/restart-target.sh` falls back to the mounted Docker socket and starts a short-lived privileged Docker CLI helper for update/start phases. That helper mounts:

- the Docker socket,
- the DASH repo path from `DUNE_RESTART_HOST_WORKSPACE`,
- the same repo at `/workspace`,
- `DUNE_STEAM_SERVER_DIR` when it is an absolute path.

That last mount is writable because SteamCMD must be able to update the local
Steam tool before DASH reads the package tarballs.

Required env values for unattended maintenance:

```env
DUNE_ADMIN_RESTART_COMMAND=/workspace/scripts/restart-target.sh
DUNE_RESTART_HOST_WORKSPACE=/path/to/DuneAwakeningSelfHost
DUNE_RESTART_COMPOSE_PROJECT=dune_server
DUNE_RESTART_USE_HOST_COMPOSE=true
DUNE_STEAM_SERVER_DIR=/path/to/dune-steamcmd-server
DUNE_RESTART_STEAM_UPDATE_MODE=steamcmd
DUNE_RESTART_STEAMCMD_HELPER_IMAGE=cm2network/steamcmd:root
DUNE_STEAM_FORCE_PLATFORM=linux
DUNE_HARDCORE_DD_WEEKLY_WIPE_ENABLED=true
```

When `DUNE_HARDCORE_DD_WEEKLY_WIPE_ENABLED=true`, the restart start hook runs
`scripts/wipe-hardcore-deep-desert.sh .env --execute --if-due` before recreating map
servers. That script expects partition `31` to be labeled `02 PVE Hardcore` and
is deliberately narrower than the game's DB-wipe flag:

- actor/respawn cleanup uses `dune.coriolis_cleanup_partition` for partition
  `31`, `DeepDesert_1`, dimension `1`;
- resource fields are deleted only from `dune.resourcefield_state` where
  `map='DeepDesert'` and `dimension_index=1`;
- spice field current counters are reset only through
  `dune.reset_global_spice_field_state('DeepDesert', 1)` when the current
  database schema exposes that optional table/function; otherwise the cleanup
  records that spice-field reset is unavailable and continues with the
  partition/resource cleanup;
- marker and surveyed-area cleanup are not called because the official
  map-level cleanup only scopes those tables by map name.

Player-facing Deep Desert rule notices are handled by
`scripts/player-presence-announcer.py` through private Paul whispers. Keep
`DUNE_PLAYER_PRESENCE_DEEP_DESERT_JOIN_MESSAGES_ENABLED=true` so partition `8`
receives the PVE Casual notice and partition `31` receives the PVE Hardcore
notice.

Manual dry-run:

```bash
./scripts/wipe-hardcore-deep-desert.sh .env
```

Force an execution outside the weekly marker interval:

```bash
./scripts/wipe-hardcore-deep-desert.sh .env --execute --force
```

## Manual Checks

Check the Steam package tag without changing `.env`:

```bash
./scripts/check-steam-update.sh .env
```

Force Steam to refresh the local self-hosted server tool:

```bash
./scripts/update-steam-tool.sh .env
```

Load images and update `.env` when a single safe package tag is found:

```bash
./scripts/load-images.sh .env
./scripts/check-steam-update.sh .env --write-env
```

Dry-run the restart hook update phase:

```bash
DUNE_RESTART_TARGET=all \
DUNE_RESTART_PHASE=update \
DUNE_RESTART_DRY_RUN=true \
./scripts/restart-target.sh
```

Run only the update phase with checks disabled:

```bash
DUNE_RESTART_TARGET=all \
DUNE_RESTART_PHASE=update \
DUNE_RESTART_CHECK_STEAM_UPDATE=false \
./scripts/restart-target.sh
```

## Rollback

Rollback after an image-tag upgrade means restoring the old `DUNE_IMAGE_TAG` and restoring state from a backup taken before the upgrade. Do not mix a downgraded image tag with post-upgrade database state unless schema compatibility has been verified.

## Building And Subfief Cap Patch Rollback

The `10000` building-piece experiment is applied during game-server startup. The
pak patch updates the cooked `BuildingPiece` data-table row inside the recreated
container overlay; it does not edit the official image or host save data
directly. The binary patcher can also bypass the Ghidra-confirmed subfief/totem
cap branch plus the server-wide and map-wide building-piece binary cap branches.

Current production wiring is derived by `scripts/compose-files.sh`. On `kspls0`,
`POSTGRES_REMOTE_REPLICA_HOST=kspls0` causes `compose.failover-standby.yaml` to
be included, and the cap patches are enabled by feature flags:

```env
POSTGRES_REMOTE_REPLICA_HOST=kspls0
DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED=true
DUNE_BUILDING_PIECE_LIMIT=10000
DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED=true
DUNE_SUBFIEF_CAP_BINARY_TARGET=all
DUNE_SUBFIEF_CAP=6
DUNE_LANDSRAAD_VENDOR_FACTION_GATE_PATCH_ENABLED=false
DUNE_OODLE_HOST_LIBRARY=/home/keith/Documents/code/DuneAwakeningSelfHost/backups/operator-oodle/liboodle-data-shared.so
```

`DUNE_SUBFIEF_CAP_BINARY_TARGET=all` applies all three binary bypass targets:
`subfief`, `building-server`, and `building-map`. A live read-only dry-run on
2026-06-01 found exactly those three target sites in a running game container:

```bash
docker exec dune_server-testing-waterfat-1 \
  python3 /workspace/scripts/patch-subfief-cap-binary.py \
    --binary /home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping \
    --target all \
    --new-cap 6 \
    --dry-run
```

Expected output includes `DRY RUN: would patch 3 target(s)` and the three target
names. If the dry-run cannot find exactly those sites after a Funcom binary
update, leave `DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED=false` until the Ghidra
signatures are refreshed.

Prepared-for-downtime production file backup:

```text
backups/live-config/<file>.20260601T213113Z.pre-subfief-binary-enable
```

Known rollback anchors from the initial production wiring:

```text
pre-change files: /home/keith/Documents/code/DuneAwakeningSelfHost/backups/operator-changes/20260523T193322Z-building-piece-limit
pre-maintenance operator backup: /home/keith/Documents/code/DuneAwakeningSelfHost/backups/20260523T193525Z
scheduled maintenance backups: /home/keith/Documents/code/DuneAwakeningSelfHost/backups/admin-panel/maintenance/<stamp>-<job-id>
```

### Disable Before Maintenance Starts

Use this if the patch should not be attempted in the next 06:00 maintenance run.

```bash
cd /home/keith/Documents/code/DuneAwakeningSelfHost
python3 - <<'PY'
from pathlib import Path

path = Path(".env")
updates = {
    "DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED": "false",
    "DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED": "false",
    "COMPOSE_FILES": "compose.yaml:compose.allmaps.yaml",
}
out = []
seen = set()
for line in path.read_text().splitlines():
    if line and not line.lstrip().startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY

docker compose --env-file .env \
  -f compose.yaml \
  -f compose.allmaps.yaml \
  up -d --force-recreate --no-deps admin-panel admin-panel-ingress
```

Confidence: high. This removes the overlay from the admin panel's scheduled restart environment before the game servers are recreated.

### Recover If Startup Fails

Use this if the 06:00 job stopped the game services but the patched recreate fails. This keeps the current database and save state and simply starts containers from the unpatched compose set.

```bash
cd /home/keith/Documents/code/DuneAwakeningSelfHost
python3 - <<'PY'
from pathlib import Path

path = Path(".env")
updates = {
    "DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED": "false",
    "COMPOSE_FILES": "compose.yaml:compose.allmaps.yaml",
}
out = []
seen = set()
for line in path.read_text().splitlines():
    if line and not line.lstrip().startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY

DUNE_RESTART_TARGET=all \
DUNE_RESTART_PHASE=start \
DUNE_RESTART_ACTION=restart \
DUNE_RESTART_USE_HOST_COMPOSE=true \
DUNE_RESTART_HOST_WORKSPACE=/home/keith/Documents/code/DuneAwakeningSelfHost \
COMPOSE_FILES=compose.yaml:compose.allmaps.yaml \
./scripts/restart-target.sh
```

After the farm is online, recreate the admin panel so future scheduled jobs use the unpatched compose set:

```bash
docker compose --env-file .env \
  -f compose.yaml \
  -f compose.allmaps.yaml \
  up -d --force-recreate --no-deps admin-panel admin-panel-ingress
```

Confidence: high for a patcher, Oodle, or pak-layout failure. The original pak is restored by recreating containers without the overlay because the patch only touched the previous container filesystem.

## Landsraad Vendor Faction-Gate Patch

`DUNE_LANDSRAAD_VENDOR_FACTION_GATE_PATCH_ENABLED=true` applies an experimental startup pak patch that keeps the active Landsraad vendor/decree gate but removes the dialogue condition that restricts vendors to the reigning faction. It targets eight generated vendor dialogue payloads in `pakchunk0-LinuxServer.pak`; if the target count changes, startup fails before the server runs. Use `compose.landsraad-vendor-faction-gate.yaml` when the building-piece overlay is not already mounting Oodle.

Dry-run from inside an opted-in game container:

```bash
python3 /workspace/scripts/patch-landsraad-vendor-faction-gate-pak.py \
  --pak /home/dune/server/DuneSandbox/Content/Paks/pakchunk0-LinuxServer.pak \
  --oodle /tmp/oodle/liboodle-data-shared.so \
  --dry-run
```

Rollback is the same as the building-piece patch: set `DUNE_LANDSRAAD_VENDOR_FACTION_GATE_PATCH_ENABLED=false` and recreate the affected game-server containers. The patch only touches the container overlay pak, not the official image or persisted world data.

## Landsraad Term Length Tuning

`DUNE_LANDSRAAD_TERM_LENGTH_TUNING_ENABLED=true` runs
`scripts/tune-landsraad-term-length.sh` during restart pre-start hygiene. The
script uses `dune.landsraad_change_term_end_time` to move the active term to
`start_time + DUNE_LANDSRAAD_TERM_LENGTH_DAYS`, rounded up to the next configured
Coriolis cycle boundary by default. The default is `7` days with
`DUNE_LANDSRAAD_TERM_LENGTH_ALIGN_TO_CORIOLIS=true`. Execute mode is
host-guarded to `kspls0` by default.

Do not use unaligned Landsraad end times. Landsraad period state is derived from
Coriolis timing, and a term ending between Coriolis boundaries can make the UI
show Suspended even while the DB term and tasks are valid.

Restart pre-start hygiene also runs
`scripts/validate-landsraad-term-coriolis-alignment.sh` by default through
`DUNE_LANDSRAAD_TERM_CORIOLIS_ALIGNMENT_GUARD_ENABLED=true`. The guard fails if
the active term end is more than
`DUNE_LANDSRAAD_TERM_ALIGNMENT_TOLERANCE_SECONDS` from a Coriolis boundary.

Dry-run:

```bash
./scripts/tune-landsraad-term-length.sh .env
```

Apply:

```bash
./scripts/tune-landsraad-term-length.sh .env --execute
```

## Landsraad Goal Tuning

`DUNE_LANDSRAAD_GOAL_TUNING_ENABLED=true` runs `scripts/tune-landsraad-goals.sh`
during restart pre-start hygiene. The script installs a small DB-owned tuning
table, patches `dune.landsraad_insert_tasks` so future terms use the configured
target, and idempotently applies the same target to the current term. The
default is `DUNE_LANDSRAAD_GOAL_TARGET=25000`, which keeps every per-house tile
goal at `25,000` even if stock goals change. A term is won by completing a row,
column, or diagonal on the 5x5 board, so the winning-line target stays at
`125,000`.

Dry-run:

```bash
./scripts/tune-landsraad-goals.sh .env
```

Apply:

```bash
./scripts/tune-landsraad-goals.sh .env --execute
```

Rollback or retune by changing `DUNE_LANDSRAAD_GOAL_TARGET` and running the
script again. Completed Landsraad tiles are not reopened by this script.

## Landsraad Reveal Watchdog

`DUNE_LANDSRAAD_REVEAL_WATCHDOG_ENABLED=true` runs
`scripts/landsraad-reveal-watchdog.sh` during restart pre-start hygiene. The
script catches the suspended-board state where the active term has tasks but
`last_processed_reveal_day=0` and no `dune.landsraad_task_reveal_state` rows.
When eligible, it calls the first-party
`dune.landsraad_perform_daily_task_reveal` function for day-one boards `0..4`
and both Landsraad factions.

The execute path is host-gated by
`DUNE_LANDSRAAD_REVEAL_WATCHDOG_REQUIRE_HOST=kspls0`. Every execute run records
either a skip or repair row in `dune.landsraad_reveal_watchdog_audit`.

Dry-run:

```bash
./scripts/landsraad-reveal-watchdog.sh .env
```

Apply:

```bash
hostname
./scripts/landsraad-reveal-watchdog.sh .env --execute
```

Incident details and the 2026-06-02 repair record are in
`docs/landsraad-reveal-watchdog.md`.

### Restore World State

Use a state restore only if the server booted and then wrote bad world state, or if Postgres/RabbitMQ/server-saved data is otherwise suspect. Do not restore state for a simple patcher startup failure.

Find the newest stopped-world maintenance backup:

```bash
cd /home/keith/Documents/code/DuneAwakeningSelfHost
latest_backup="$(ls -dt backups/admin-panel/maintenance/* | head -1)"
printf '%s\n' "$latest_backup"
```

Dry-run the restore first:

```bash
./scripts/restore-state.sh --dry-run --rabbitmq --server-saved --config --tls .env "$latest_backup"
```

Then run the disruptive restore only after confirming the selected backup:

```bash
./scripts/restore-state.sh --rabbitmq --server-saved --config --tls .env "$latest_backup"
```

Confidence: high that the maintenance backup is the right restore point after the stop phase completes. Confidence is lower for the immediate operator backup taken while the world is live; prefer the stopped-world `backups/admin-panel/maintenance/...` backup when it exists.

### Full File Rollback

Use the pre-change file copy only if the `.env` or compose files themselves need to be restored to the exact pre-experiment state.

```bash
cd /home/keith/Documents/code/DuneAwakeningSelfHost
rollback_dir=backups/operator-changes/20260523T193322Z-building-piece-limit
cp -a "$rollback_dir/.env" .env
cp -a "$rollback_dir/compose.yaml" compose.yaml
cp -a "$rollback_dir/compose.allmaps.yaml" compose.allmaps.yaml
cp -a "$rollback_dir/scripts/run_server_safe.sh" scripts/run_server_safe.sh
rm -f compose.building-piece-limit.yaml
```

Only use this path when overwriting later `.env` changes is acceptable. The
safer default is to set `DUNE_BUILDING_PIECE_LIMIT_PATCH_ENABLED=false`,
`DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED=false`, and remove obsolete patch overlays
from `COMPOSE_FILES` if present.
# Recovery and router SSH requirements

The production maintenance and restart path runs on `kspls0`. Map startup
reconciles the configured game and IGW UDP ranges with the AsusWRT router before
starting workers. That reconciliation must run with the operator's router SSH
identity: the map watchdog, hotfix updater, and reboot-resume units run as
`keith`, and the privileged transient restart helper mounts
`DUNE_RESTART_HOST_SSH_DIR` read-only for the network-capacity check. The
watchdog and hotfix updater keep their `flock` files in systemd-managed state
directories so stale root-owned `/tmp` locks cannot prevent recovery after a
service-user change. Do not run these lifecycle units as root without an
equivalent router SSH identity;
router forwarding failures must be reported with their underlying SSH error
and must not be mistaken for a game-server startup failure.

After maintenance, verify the active database partitions and FLS publication
with `scripts/status.sh .env --public-summary`, and verify the runtime patches
with the relevant post-start health hooks before declaring the farm recovered.
