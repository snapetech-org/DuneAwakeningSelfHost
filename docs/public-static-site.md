# Public Static Site

DASH includes an optional public static site for server operators who want to publish server settings, coarse status, an active-player list, a Hagga Basin player map, and an opt-in signed discovery descriptor without exposing the LAN admin panel.

The public site is deliberately boring from a security perspective:

- The browser only fetches static files; the optional directory makes
  uncredentialed cross-origin descriptor requests only after a visitor starts a
  latency scan.
- The renderer runs locally on the DASH host.
- The public web server never needs Docker, Postgres, RabbitMQ, `.env`, admin-token, or admin-panel access.
- Generated `players.json` includes public player display rows, coarse map health, and public map-status labels. It omits Steam IDs, Steam persona names, Steam profile URLs, Funcom IDs, account IDs, controller IDs, pawn IDs, raw database rows, internal hostnames, ports, tokens, and raw coordinate JSON.

## What It Generates

```text
/status.html
/players.json
/hagga-pois.json
/hagga-map.svg
/hagga-basin.webp
/deep-desert-map.svg
/deep-desert.webp
/directory-entry.json       # generated only after explicit opt-in
/directory/index.html       # optional federation host
/directory/directory.json   # generated verified catalog
```

The page JavaScript refreshes those files every 60 seconds inline. It does not reload the whole page.

Public health follows the map lifecycle policy instead of requiring every world
partition to stay running. By default, partitions `1,2` (Survival/Hagga Basin
and the Overmap) are the required core. Dynamic destinations that are stopped
or warming are shown as `On demand` or `Warming`; they do not make the server
degraded. `Player access` also requires healthy FLS publication and enough game
RabbitMQ connections for the required core.

The public `Since maintenance` age is derived from Docker start times for the
six always-on maps, using the oldest current core-map start as the conservative
marker for the last full maintenance restart. Completed or failed restart-job
timestamps do not set this age; a failed preflight can be recorded without
restarting any map. Active scheduled or in-progress maintenance is still shown
from the restart-job state.

The FLS publication probe reads a bounded recent log tail (default `2000` lines,
controlled by `DUNE_FLS_PUBLICATION_HEALTH_LOG_TAIL`) so high-volume Director
logs cannot make the one-minute status renderer time out. A probe timeout is
reported as degraded rather than aborting the renderer; active-player data is
still published independently.

Override the required public/core partitions only when the always-on policy is
intentionally changed:

```text
DUNE_CORE_PARTITION_IDS=1,2
```

`deep-desert-map.svg` is generated from server state. If `admin/static/deep-desert.webp`
exists, the renderer embeds that registered weekly map image as the background; otherwise it
uses a derived schematic background from Deep Desert markers, Coriolis seeds, resource fields,
spice field state, and static shifting-sand rows.

## Files

```text
public-site/static/
public-site/scripts/
public-site/systemd/
public-site/dune-public-site.env.example
examples/public-site/
```

## Linux Quickstart

Run from the DASH repo on the host that can run `docker compose` against the Dune stack:

```bash
sudo STATIC_DIR=/srv/dash-public-site \
  ENV_FILE=/etc/dune-public-site.env \
  DUNE_PUBLIC_SITE_USER="$USER" \
  ./public-site/scripts/install-dune-public-site.sh
```

Edit the environment file:

```bash
sudoedit /etc/dune-public-site.env
```

Set at minimum:

```text
DUNE_ROOT=/opt/DuneAwakeningSelfHost
STATIC_DIR=/srv/dash-public-site
INDEX_FILE=/srv/dash-public-site/index.html
STATUS_FILE=/srv/dash-public-site/status.html
```

The public-site renderer reads the live game database and compose-file settings
from the DASH game env file, defaulting to `$DUNE_ROOT/.env`. Do not duplicate
`DUNE_DATABASE` in `/etc/dune-public-site.env`; that can drift after official
database upgrades.

Customize public text:

```bash
sudo PUBLIC_SITE_TITLE="Example Dune Awakening Server" \
  PUBLIC_SERVER_NAME="Example PVE Friendly Server" \
  PUBLIC_SERVER_DESCRIPTION="A friendly community PvE Dune Awakening server." \
  PUBLIC_SERVER_WHERE="Dune Awakening > Servers > Experimental > search Example." \
  STATIC_DIR=/srv/dash-public-site \
  /usr/local/sbin/configure-dune-public-site.sh
```

Render once and enable the timer:

```bash
sudo systemctl restart render-dune-static-status.service
sudo systemctl enable --now render-dune-static-status.timer
./public-site/scripts/validate-dune-public-site.sh /srv/dash-public-site
```

## Signed publication and federation

Signed directory publication is independent of the player list and is disabled
by default. When enabled in the game `.env`, the normal renderer creates a
short-lived `directory-entry.json` in the static root and a private recovery
copy under `backups/public-directory/`. A private mode-`0600` Ed25519 key is
created under `config/secrets/`; the public descriptor contains only its public
key, a stable derived identity, the configured public profile, coarse health,
population, Sietch/map totals, build label, and signature.

A directory host installs the static `/directory/` interface and a separate
hardened oneshot/timer that pulls an exact allowlist of descriptor URLs. The
collector requires public DNS hostnames, pins a validated public address for
the TLS connection, verifies the certificate against the original hostname,
refuses redirects and oversized/non-JSON responses, validates every descriptor
and isolates individual failures. The browser then repeats schema, digest,
identity, signature, and expiry checks before rendering. It makes no automatic
cross-origin request; visitors explicitly choose **Measure my latency**.

Complete configuration, serving headers, installer commands, metrics, alerts,
key rotation, and recovery are in
[`federated-public-directory.md`](federated-public-directory.md).

## Deep Desert Background Registration

The self-host server does not currently expose a ready-made Deep Desert minimap raster in
Postgres or the static site. To use a weekly in-game map screenshot as the actual background,
register it with three control points whose world coordinates are known from DB markers:

```bash
./public-site/scripts/register-deep-desert-background.py \
  /path/to/deep-desert-weekly-map.png \
  /path/to/deep-desert-control-points.json \
  --output admin/static/deep-desert.webp
```

Control point JSON format:

```json
[
  {"world": [-992239, 972498], "pixel": [210, 145]},
  {"world": [-120609, 935241], "pixel": [1040, 170]},
  {"world": [768375, 906401], "pixel": [1880, 190]}
]
```

After registration, rerun the renderer/deploy script. The generated SVG will include the
registered image plus the live DB overlays.

## Serve It

Use any static web server. Copyable examples:

```text
examples/public-site/caddy.Caddyfile
examples/public-site/nginx.conf
examples/public-site/caddy-subpaths.Caddyfile
examples/public-site/nginx-subpaths.conf
examples/public-site/compose.yaml
examples/public-site/rclone-sync.sh
```

Serve the top-level static files generated by the renderer and shipped in `public-site/static`.
The example configs allow these top-level extensions plus the exact
`/directory` subtree so new map assets propagate without route edits:

```text
/
/*.html
/*.css
/*.js
/*.json
/*.svg
/*.webp
/directory
/directory/
/directory/index.html
/directory/directory.css
/directory/directory.js
/directory/directory.json
```

Return `404` for everything else.

`/directory-entry.json` must be readable cross-origin because federated hosts
pull it and visitors may explicitly measure it. Serve that exact file with
`Access-Control-Allow-Origin: *`, `Cross-Origin-Resource-Policy: cross-origin`,
`X-Content-Type-Options: nosniff`, and `Cache-Control: no-store`; do not apply
those broader cross-origin headers to private or administrative routes.

## Production ingress hardening

Do not expose the Caddy origin directly to the Internet. Public DNS should be a
proxied CNAME to the named Cloudflare Tunnel (`<tunnel-id>.cfargotunnel.com`),
and the host firewall should drop public TCP 80/443. Install the repository's
guard on the production host with:

```bash
sudo ./scripts/install-cloudflare-origin-guard.sh
```

The installed `apply-proxy` mode permits only Cloudflare's published edge
networks, private networks, and loopback. This supports an existing proxied
origin DNS record without allowing direct-origin bypass. After every public
hostname is routed to the named tunnel, change the systemd unit to use `apply`;
that tunnel-only mode drops all non-private inbound TCP 80/443.

For the remotely managed production tunnel, store a current root-readable API
token with `Cloudflare Tunnel: Edit` and `Zone DNS: Edit` in
`/etc/snape/cloudflare.env`, preview the change, and then apply it:

```bash
sudo ./scripts/configure-cloudflare-web-tunnel.sh
sudo ./scripts/configure-cloudflare-web-tunnel.sh --apply
```

The script preserves unrelated published routes, sets the four game-site
hostnames to loopback HTTP behind the encrypted tunnel, replaces each DNS record
with a proxied tunnel CNAME, and refuses ambiguous duplicate DNS records.

Keep the detailed admin panel off public DNS unless a deny-by-default
Cloudflare Access application and MFA policy already protect its hostname.
The supported default is loopback-bound ingress plus the private
`duneadmin.home` route. `/healthz` is deliberately minimal; `/api/status` and
all other detailed API routes require an admin token.

## Optional Multi-Game Landing Page

The package includes a deterministic static landing-page generator for operators
who publish more than one game dashboard from a single hostname. The bundled
manifest is deliberately Dune-only; keep site-specific additional game entries,
icons, colors, and links in your own configuration.

Generate the example locally:

```bash
./public-site/scripts/generate-game-landing.py \
  --config public-site/landing/game-links.example.json \
  --output /tmp/game-landing
```

Install a customized manifest:

```bash
sudo GAME_LANDING_CONFIG=/etc/game-links.json \
  LANDING_DIR=/srv/game-landing \
  ./public-site/scripts/install-game-landing.sh
```

The manifest accepts one to eight ordered `games` entries. Each entry provides
its visible name, short label, destination, local icon, and three theme colors:

```json
{
  "site_name": "YOUR.SERVER",
  "eyebrow": "Self-hosted game servers",
  "heading": "Choose your world.",
  "intro": "Select a game to view its public server information.",
  "footer": "Static public information",
  "games": [
    {
      "slug": "dune-awakening",
      "name": "Dune: Awakening",
      "label": "DA",
      "description": "Live status, players, rules, and maps.",
      "href": "/dune/",
      "icon": "assets/dune-awakening.svg",
      "accent": "#e36c35",
      "accent_soft": "#efb15f",
      "ink": "#25130c"
    }
  ]
}
```

Icon paths are resolved relative to the manifest and copied into the generated
output with a content hash. Root-relative links and HTTPS links are accepted.
The same manifest always produces byte-identical HTML, CSS, and asset names.

Use `caddy-subpaths.Caddyfile` or `nginx-subpaths.conf` when the generated
landing page owns `/` and the Dune dashboard should be reachable at `/dune/`,
`/duneawakening/`, and `/da/`. Each bare alias redirects to its trailing-slash
form so the dashboard's relative CSS, JavaScript, map, and telemetry paths stay
inside the selected prefix.

## Windows And macOS

Windows and macOS are reasonable static web hosts, but the renderer should normally run on the Linux DASH host that owns the Docker Compose stack.

Recommended pattern:

1. Run the renderer on the DASH host.
2. Sync `/srv/dash-public-site` to the Windows/macOS host with `rsync`, `scp`, Syncthing, SMB, or another sync tool.
3. Serve only the synced files.

Do not give a public IIS, nginx, Apache, or Caddy process permission to run Docker or read the DASH `.env`.

## Standby And Offsite Sync

For an SSH standby:

```text
SYNC_HOST=standby.example
SYNC_STATIC_ROOT=/srv
```

The render script uploads the generated site directory after each render. Object storage is also fine if it serves HTML, CSS, JS, JSON, SVG, and WebP with sane content types; see `examples/public-site/rclone-sync.sh`.

## Map Calibration

The renderer copies `admin/static/hagga-basin.webp` and projects player pawns using `DUNE_HAGGA_MAP_*` settings from `/etc/dune-public-site.env` or the DASH `.env`.

Defaults:

```text
DUNE_HAGGA_MAP_MIN_X=-457200
DUNE_HAGGA_MAP_MAX_X=355600
DUNE_HAGGA_MAP_MIN_Y=-457200
DUNE_HAGGA_MAP_MAX_Y=355600
DUNE_HAGGA_MAP_INVERT_X=false
DUNE_HAGGA_MAP_INVERT_Y=false
DUNE_HAGGA_MAP_IMAGE_MIN_U=0
DUNE_HAGGA_MAP_IMAGE_MAX_U=1
DUNE_HAGGA_MAP_IMAGE_MIN_V=0
DUNE_HAGGA_MAP_IMAGE_MAX_V=1
```

If markers drift, use two or more known in-game locations far apart from each other and adjust one calibration value at a time.

## Package A Copy

Create a portable tarball containing only the public-site package:

```bash
make public-site-check
./public-site/scripts/package-dune-public-site.sh /tmp/dash-public-site.tar.gz
```

Inspect the tarball before sharing. It should contain only `public-site/`, `examples/public-site/`, and this document. The optional landing generator, Dune example manifest, and Dune icon are included in `public-site/landing/`.

## Optional GitLab Publishing

GitLab publishing is optional. The default install and maintenance path is still the local shell/systemd flow above. Use `.gitlab-ci.yml` only when you deliberately want a protected `gitlab.home` shell runner to validate and publish the static site.

The optional pipeline deliberately separates safe automation from live operations:

- `validate:public-site` runs `make public-site-check` and creates a package artifact.
- `deploy:public-site` is manual on the default branch and deploys only static-site files plus a fresh render.
- `observe:public-site` verifies no-store headers, `status.html` freshness, and `players.json` shape.
- `observe:server-health` is read-only and runs the existing health/RabbitMQ checks.

Runner requirements for this optional path:

```text
bash
curl
docker compose access to the DASH stack
jq
make
python3
systemctl permission to restart render-dune-static-status.service
write access to STATIC_DIR from /etc/dune-public-site.env
```

Recommended runner environment:

```text
PUBLIC_SITE_ENV_FILE=/etc/dune-public-site.env
PUBLIC_SITE_URL=https://dune.snape.tech
```

Keep game-state mutations out of scheduled GitLab jobs. Use manual jobs only for static publish and read-only checks unless a separate operator-approved runbook says otherwise. Do not treat this as a required install step for users who prefer direct shell/systemd operation.
