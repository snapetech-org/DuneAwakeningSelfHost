# LAN Reflection / Internal Join

Dune self-hosting advertises one external address to Funcom/FLS and to clients.
For a public server this should normally stay set to the public WAN address:

```dotenv
EXTERNAL_ADDRESS=<public-wan-ip>
```

Do not switch this value between public and LAN addresses for day-to-day use.
That creates different behavior for internal and external players and can make
FLS/gateway diagnosis confusing.

## The Problem

When a player on the same LAN joins through the public server listing, the client
still tries to reach the public advertised address. Many home routers do not
support NAT hairpin/loopback correctly for UDP game traffic. The symptom is:

- the game hangs at `Connecting to Sietch`;
- the server containers are up and farm-ready;
- Docker has UDP publishes for `7777-7810`;
- host counters for those UDP ports stay at `0`;
- `tcpdump` on the LAN interface sees no packets for the public IP.

If the host never sees packets, this is not a Dune password, map readiness, or
Docker port-publish problem. The LAN path is not reaching the host.

## Standard Decision

Use one stable public advertised address and make LAN clients able to reach that
same address internally.

Do not change working hairpin/NAT reflection rules casually. Before touching
router NAT, export the router config or capture the current forwarding/NAT state.
For routers that expose shell access, a read-only snapshot should include the
port-forward definitions and NAT/filter rules. Example shape:

```bash
mkdir -p backups/router-inspection
ssh <router-user>@<router-lan-ip> '<dump-port-forward-config>; iptables -t nat -S; iptables -S' \
  > "backups/router-inspection/router-$(date -u +%Y%m%dT%H%M%SZ).txt"
```

Preferred order:

1. Router NAT reflection/hairpin for UDP to the server host.
2. A LAN static route for the public `/32` to the server host.
3. A per-client route for the public `/32` to the server host.

Avoid changing `EXTERNAL_ADDRESS` for internal testing unless you are running a
LAN-only server.

## Router Static Route Mode

This keeps Dune configured for public play while allowing LAN clients to use the
same public server address.

On the router, add:

```text
Destination: <public-server-ip>
Prefix:      /32
Gateway:     <dune-host-lan-ip>
Interface:   LAN
```

Then enable host-side reflection on the Dune host:

```bash
sudo ./scripts/setup-lan-reflection.sh
```

For persistence, install the rendered service and timer from the active
checkout. The timer re-runs the idempotent repair after firewall reloads or
Docker network changes. The host service also reseeds bridge and map neighbors
when running Dune containers change or restart, including after maintenance:

```bash
test "$(hostname)" = kspls0
./scripts/install-autoheal-services.sh .env
systemctl --no-pager --full status dune-lan-reflection.timer
```

The auto-heal installer also renders the map and Director watchdog units.
Use it as the normal production installation path so the unit paths point at
the current repository checkout. For a LAN-only change, the timer and service
templates can be installed separately, but both units must be installed and
the timer must be enabled.

The script:

- adds the public `/32` to the LAN interface;
- disables strict reverse-path filtering for the LAN reflection path;
- ensures Dune container replies are masqueraded back through the LAN interface.
- redirects local same-host client traffic for `GAME_RMQ_PUBLIC_PORT` TCP and
  the gameplay UDP range back into the host-published services.

Compose pins the Linux bridge interface to `dune-br0` by default through
`DUNE_DOCKER_BRIDGE_NAME`. This prevents a network recreation from replacing it
with a random `br-<network-id>` name and invalidating nftables interface rules.
`scripts/restart-post-start-health.sh` also refreshes the rules after farm starts
to repair legacy deployments and firewall reloads. Set
`DUNE_LAN_REFLECTION_REFRESH_AFTER_START=false` only when another firewall
manager owns equivalent dynamic rules.

## Per-Client Route Mode

Use this when the router cannot add static routes and does not support UDP
hairpin NAT. Add the route on each LAN client that needs to play internally.

Linux:

```bash
sudo ip route add <public-server-ip>/32 via <dune-host-lan-ip>
```

Windows PowerShell as Administrator:

```powershell
route -p add <public-server-ip> mask 255.255.255.255 <dune-host-lan-ip>
```

macOS:

```bash
sudo route -n add -host <public-server-ip> <dune-host-lan-ip>
```

Host-side reflection must still be enabled on the Dune host.

## Known-Good Router Baseline

For a working deployment, keep a private operator note or backup that records:

```text
Router model/firmware
WAN/public address
Dune host LAN address
Gameplay UDP forwards for the selected layout
IGW UDP forwards if used by that layout
GAME_RMQ_PUBLIC_PORT TCP forward
Any NAT reflection/hairpin/loopback toggle or rule
```

For router-managed hairpin, the private backup should show both:

```text
DNAT/port-forward rules for the public Dune ports
A LAN-to-LAN reflection/masquerade path for same-LAN clients using the public address
```

Do not remove or rewrite a working LAN-to-LAN reflection/masquerade rule without
a router backup and a rollback plan. It may be part of the same-LAN
public-listing join path.

## Validation

Watch for packets while attempting to join from the LAN:

```bash
sudo timeout 75 tcpdump -ni <lan-interface> 'host <public-server-ip> and udp'
```

Check Docker's UDP counters:

```bash
sudo iptables -t nat -L DOCKER -n -v | rg 'dpt:(7777|7888|7778|7889)'
```

Interpretation:

- `0 packets captured` and Docker counters stay `0`: LAN routing/hairpin is not
  sending traffic to the Dune host.
- packets appear on `enp17s0` but Docker counters stay `0`: host firewall/NAT
  path is wrong.
- Docker counters increase but the client still hangs: Dune service discovery,
  map routing, or RabbitMQ/FLS state is the next layer to inspect.

## Notes

Forward public gameplay UDP to the Dune host for external players:

```text
7777-7810/udp -> <dune-host-lan-ip>
```

For the full warm-pool layout, the paired IGW range is `7888-7918/udp`. If your
layout uses those ports for live-client routing or server-browser checks,
forward them to the Dune host along with the gameplay UDP range.

For live-client login, the game RabbitMQ endpoint advertised by Gateway must be
reachable from the client. In the public self-host layout that means forwarding
`GAME_RMQ_PUBLIC_PORT`, default `31982/tcp`, to the host. Keep RabbitMQ
management, Postgres, and admin panel ports private.
