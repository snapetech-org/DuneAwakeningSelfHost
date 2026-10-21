# Dune cutover / cutback runbook

This runbook describes the bidirectional procedure for moving the active Dune
Awakening Self-Host workload between the configured primary and standby hosts.
It supersedes ad-hoc cutover steps that lived across
`setup-lan-reflection.sh`, `host-network-failover.sh`, and
`router-cutover-asuswrt.sh`. Keep concrete hostnames, LAN IPs, public IPs,
router users, and bridge IDs in `.env` or private operator notes, not in this
repo.

The runbook assumes the canonical port ranges, which retain the static map
ports and reserve room for automatically discovered map workers:

- `GAME_UDP_PORT_RANGE=7777:7871`
- `IGW_UDP_PORT_RANGE=7888:7982`
- `GAME_RMQ_PUBLIC_PORT=31982`

Do not shrink these to the pre-discovery static-map ranges (`7777:7810`,
`7888:7918`): the map-worker generator allocates only inside the configured
pools and fails closed when either pool is exhausted.

## What "active" means

The active Dune host:

1. Owns `$EXTERNAL_ADDRESS/32` as an alias on its LAN interface (added by
   `scripts/setup-lan-reflection.sh`).
2. Receives router-NAT-loopback'd LAN client traffic to the public IP.
3. Runs the Dune compose stack (`docker compose -f compose.allmaps.yaml up -d`
   plus the survival/director/gateway core).
4. Owns `OUTPUT` REDIRECT rules so its own outbound traffic to the public IP
   short-circuits to localhost.
5. Has Postgres timeline ahead of the other host (after promotion).

The non-active host:

1. Does **not** own the public IP /32.
2. Does **not** have `OUTPUT` REDIRECTs for the public IP (the failover script
   strips them; if you run probes from this host they go through the real LAN
   path so you can verify the active host is reachable).
3. Runs at most a Postgres replica.

## Role values that move together

Confidence high: stale role values are dangerous. The active gameserver primary
is the host that owns the writable Dune Postgres, runs Director/Gateway/maps,
publishes to FLS, owns active backup/snapshot/status/bot timers, and receives
router forwards.

Keep these values synchronized in `.env` on both hosts:

```sh
DUNE_CURRENT_HOST=<active-host-label>
DUNE_CURRENT_LAN_IP=<active-lan-ip>
DUNE_FAILOVER_PRIMARY_HOST=<active-host-label>
DUNE_FAILOVER_PRIMARY_LAN_IP=<active-lan-ip>
DUNE_FAILOVER_STANDBY_HOST=<inactive-standby-label>
DUNE_FAILOVER_STANDBY_LAN_IP=<inactive-standby-lan-ip>
POSTGRES_REPLICATION_BIND_ADDRESS=<active-lan-ip>
POSTGRES_REPLICATION_PRIMARY_HOST=<active-lan-ip>
POSTGRES_REMOTE_REPLICA_HOST=<inactive-standby-lan-ip-or-ssh-host>
POSTGRES_REPLICATION_ALLOWED_ADDRESS=<inactive-standby-lan-ip>
```

Use the helper instead of hand-editing these values:

```bash
# dry-run
make set-active-gameserver ENV_FILE=.env \
  ACTIVE_HOST=kspls0 ACTIVE_IP=192.168.50.85 \
  STANDBY_HOST=kspld0 STANDBY_IP=192.168.50.148

# apply, then mirror the corrected env/docs/scripts to the standby
make set-active-gameserver ENV_FILE=.env \
  ACTIVE_HOST=kspls0 ACTIVE_IP=192.168.50.85 \
  STANDBY_HOST=kspld0 STANDBY_IP=192.168.50.148 APPLY=--apply
make sync-standby-files ENV_FILE=.env
```

For cutback, reverse the active and standby arguments. Run
`make failover-bidirectional-audit ENV_FILE=.env` after syncing; it fails if
`DUNE_CURRENT_*`, `DUNE_FAILOVER_*`, and Postgres replication source/target
values drift apart.

Primary-only systemd timers must also be symmetric. Use generic unit names for
role-swapped timers, with host-local config pointing at the peer. For backup
mirroring this repo uses `dune-backup-mirror-peer.timer` on both hosts:

- on kspls0, it uses `examples/backup/rsync-kspld0.env`;
- on kspld0, it uses `examples/backup/rsync-kspls0.env`.

Do not put host-specific names such as `dune-backup-mirror-kspld0.timer` in
`DUNE_STANDBY_ROLE_TIMERS`; they cannot be enabled on both sides during a
bidirectional cutover.

## One-time prerequisites on each host

These need to persist across host rebuilds.

### Both hosts: `inet filter forward` policy is `drop`

`/etc/nftables.conf` on both hosts may have a `chain forward` with `policy
drop`. The Dune docker bridge needs explicit accept rules in that chain or all
external traffic to published Dune ports is silently dropped **after** DNAT
(counters in `iptables -t nat DOCKER` increment, but the `FORWARD` chain shows
zero packets reaching `DOCKER-USER`).

The accept rules live next to the existing per-bridge accepts in
`/etc/nftables.conf`. They are tied to the specific Docker bridge name, which
varies per host. Derive the bridge name locally with `docker network inspect`
or from `scripts/setup-lan-reflection.sh` output.

Rules required on each host (already present, verify with
`sudo nft list chain inet filter forward`):

```
ip daddr 172.31.240.0/24 oifname "<bridge>" accept comment "Dune docker DNAT inbound"
iifname "<bridge>" ip saddr 172.31.240.0/24 accept comment "Dune docker bridge outbound"
```

Before editing host firewall files, write a timestamped backup under `/etc/`.

`scripts/setup-lan-reflection.sh` also installs these rules at runtime if they
are missing, so a fresh container restart that changes the bridge name will
self-heal — but the persistent `/etc/nftables.conf` entries should be kept
in sync if the bridge name changes.

### Both hosts: `/usr/local/sbin/sync-knock-blocks.sh` patch

The host-local `sync-knock-blocks.sh` script (driven by
`sync-knock-blocks.timer`, every minute) used to insert this rule into
`ip filter DOCKER-USER`:

```
ip saddr @blocked_scanners_v4 tcp dport 31982 drop comment "knock-scanner dune tcp auto-block"
```

The intent is "drop 31982 only for scanner-blocked IPs". The iptables-nft
compat layer cannot represent the named-set saddr predicate when it lives
inside an `ip filter` table managed by iptables-nft, so the kernel ends up
applying the rule as a broad `tcp dport 31982 drop` and all Dune login TCP
dies. This is the same failure mode documented at
`scripts/setup-lan-reflection.sh:83`.

The DOCKER-USER insertion block in `/usr/local/sbin/sync-knock-blocks.sh` is
commented out on both hosts (backups at
`/usr/local/sbin/sync-knock-blocks.sh.bak.20260523-dune-fix`). The host-bound
input-chain block still applies — `inet filter input` correctly drops the
named set without compat-layer truncation, so scanner blocking continues to
work for host-local services. Only the Docker-published port rule was the
problem.

If the host script is ever overwritten by a rebuild, re-apply the patch:
comment out the `if ! nft list chain ${DOCKER_TABLE_NAME} DOCKER-USER 2>/dev/null | grep -q "knock-scanner dune tcp auto-block"` block (lines 38-40 of the original).

### Router

`nat_redirect_enable=1` (NAT loopback) must be on — LAN clients hit the public
IP and the router reflects to the active LAN host. Verify with
`ssh "$DUNE_FAILOVER_ROUTER_SSH" 'nvram get nat_redirect_enable'` -> `1`.

The Dune port forwards live in `vts_rulelist` and are managed by
`scripts/router-cutover-asuswrt.sh`. Do not edit them in the AsusWRT web UI
directly; the cutover script's regex substitution will not survive
out-of-band edits cleanly.

## Cutover procedure (any direction)

For a fully orchestrated run that includes Postgres promotion, image sync,
service role swap, and router cutover:

```bash
# dry run first — review the output, no changes are applied
./scripts/failover-orchestrate.sh .env standby

# actually apply (move active from primary to standby)
./scripts/failover-orchestrate.sh .env standby --apply

# cut back (move active from standby to primary)
./scripts/failover-orchestrate.sh .env primary --apply
```

After any successful direction change, update the role values with
`make set-active-gameserver`, then `make sync-standby-files`. The orchestration
moves traffic and services; the role-env helper records which host is now the
active primary so the next operation derives from the right side.

Under the hood this calls, in order:

1. `make sync-standby-files` / `sync-standby-images` — keeps the destination
   in shape before flipping.
2. `make postgres-failover-seal` + `promote-standby` (standby direction only;
   primary direction assumes you have already rebuilt the replica).
3. `make host-network-failover` — removes `$EXTERNAL_ADDRESS/32` from the old
   host, strips its stale `OUTPUT` REDIRECTs, then runs
   `setup-lan-reflection.sh` on the new active host to install the /32
   alias, MASQUERADE, raw control-plane accept, knock-scanner DOCKER-USER
   strip, inet-filter-forward Dune accepts, and self-host REDIRECT rules.
4. `make router-cutover` — rewrites the AsusWRT `vts_rulelist` Dune entries
   to point at the new active host's LAN IP, idempotent under range drift.
5. `make failover-role-services` — swaps which host runs the systemd unit
   set listed in `DUNE_STANDBY_ROLE_SERVICES` / `DUNE_STANDBY_ROLE_TIMERS`.
6. `make cutover-network-status` + `cutover-check` — read-only verification.

## Manual / piecemeal commands

When only one piece needs to move:

| Action | Command |
|---|---|
| Router only | `CONFIRM_ROUTER_CUTOVER=yes make router-cutover ENV_FILE=.env TARGET="$DUNE_CURRENT_LAN_IP"` |
| Host network only | `CONFIRM_HOST_NETWORK_FAILOVER=yes make host-network-failover ENV_FILE=.env ROLE=standby` |
| Apply host-side reflection on this box | `sudo ./scripts/setup-lan-reflection.sh .env` |
| Service role swap only | `CONFIRM_FAILOVER_ROLE_SERVICES=yes make failover-role-services ENV_FILE=.env ROLE=standby` |
| Role env only | `make set-active-gameserver ENV_FILE=.env ACTIVE_HOST=<active> ACTIVE_IP=<active-ip> STANDBY_HOST=<standby> STANDBY_IP=<standby-ip> APPLY=--apply` |
| Inspect current router config | `./scripts/router-cutover-asuswrt.sh .env` (dry run; writes backup to `backups/router-inspection/`) |

`ROLE=standby` means "move the active workload to the configured standby host
(`DUNE_FAILOVER_STANDBY_LAN_IP`)". `ROLE=primary` is the reverse. The script
does not infer direction from current state — you tell it which role to make
active.

## Post-cutover verification

```bash
# from the active host
sudo iptables -S DOCKER-USER | grep 31982 || echo '(clean — knock-scanner OK)'
sudo nft list chain inet filter forward | grep 172.31.240 && echo '(forward accepts present)'
ip -4 addr | grep "$EXTERNAL_ADDRESS"

# from anywhere on the LAN
bash -c "echo > /dev/tcp/<EXTERNAL_ADDRESS>/31982" && echo OK
bash -c "echo > /dev/tcp/<active LAN IP>/31982" && echo OK

# from the now-inactive host: probes should reach the new active host, not localhost
bash -c "echo > /dev/tcp/<EXTERNAL_ADDRESS>/31982" && echo OK
sudo iptables -t nat -S OUTPUT | grep "$EXTERNAL_ADDRESS" || echo '(self-redirects cleaned)'
```

The router's `vts_rulelist`:

```bash
ssh "$DUNE_FAILOVER_ROUTER_SSH" 'nvram get vts_rulelist' | tr '<' '\n<' | grep -iE 'dune'
```

Should show three entries — `duneA1` (game UDP), `duneA2` (IGW UDP), `DuneRMQ`
(login TCP) — all pointing at the active host's LAN IP with the canonical
ranges.

## Things deliberately not automated

- Re-enabling `sync-knock-blocks.timer` after a host rebuild. The patch to the
  host script must be in place first or you'll re-introduce the broad-DROP
  bug. Check with
  `sudo grep -c 'knock-scanner dune tcp auto-block' /usr/local/sbin/sync-knock-blocks.sh` -
  expect 1.
- Adjusting `/etc/nftables.conf` for new docker bridges. The `inet filter
  forward` accepts are bridge-name-specific. If `compose down && compose up`
  recreates the network with a different bridge ID,
  `setup-lan-reflection.sh` will install runtime accepts for the new bridge,
  but the persistent config still references the old name and will lose them
  at the next nftables reload. Update `/etc/nftables.conf` to match the new
  bridge name when this happens.
- Removing the `inet filter forward` accepts on the inactive host. They're
  harmless when no Dune containers are running on that bridge.

## 2026-05-24 stale DB / FLS stealing incident

Confidence high on the operational causes and prevention:

- Recreating `director` or `gateway` without `--no-deps` can recreate
  dependency containers such as Postgres. For control-plane-only refreshes, use
  `docker compose --env-file .env up -d --no-deps --force-recreate director`
  or the matching service name. Do not run broad `up -d --force-recreate`
  commands against the live stack.
- A full `docker compose down && up` can create a new Docker bridge. If
  `/etc/nftables.conf` still accepts the old bridge name under `inet filter
  forward`, published ports DNAT but forwarding drops. Start with
  `scripts/start-full-warm-pool.sh` or run `scripts/setup-lan-reflection.sh`
  after dependencies are healthy so bridge accepts are refreshed.
- If another host accidentally starts a Director/Gateway stack for the same
  battlegroup and steals FLS registration, stop that stack first, then restart
  `gateway` on the intended active host. Restarting `gateway` is the low-blast
  reset for FLS registration; do not restart Postgres or all maps unless map
  health actually requires it.
- The inactive host must run at most `dune-postgres-replica`. No
  `dune_server-*` or `dune_handoff_lab-*` containers should be up there.
  `make failover-bidirectional-audit` checks this.
- If a stale DB is ever exposed, stop the game stack before players rejoin,
  take a dump of the stale live DB for forensics, restore the latest known-good
  stopped-world or replica snapshot, start with the warm-pool script, then
  rebuild the standby replica from the restored primary. Physical replication
  follows timelines; after a restore or promotion, the old standby data is not
  trustworthy until rebuilt.

## Known leaky edges

- `failover-orchestrate.sh primary` does **not** reverse a promoted Postgres
  timeline. Rebuild a fresh replica on the future primary first, then run the
  orchestration.
- `setup-lan-reflection.sh` reads the Docker network ID at runtime; if you've
  taken Dune down before running it, the script silently skips the
  bridge-specific FORWARD ACCEPT and the inet-filter-forward Dune accept
  rules. Bring the stack up first, then run reflection setup.
- The router `vts_rulelist` only carries three Dune entries
  (`duneA1`/`duneA2`/`DuneRMQ`). Per-map UDP ports inside the configured
  ranges all DNAT to the same target host; the actual map-to-container DNAT
  is handled by Docker on the target host. Do not try to add per-map router
  forwards.
