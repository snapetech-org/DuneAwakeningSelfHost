# Admin Panel

The admin helper panel is a LAN-only web UI for local server operators. It is not exposed publicly by default.

Admin settings and every bundled runtime configuration helper share a locked,
inode-preserving `.env` writer. This prevents file-bind mounts from becoming
attached to a deleted inode and prevents settings from disappearing on container
recreation. Operational proof and recovery steps are in
[configuration-durability.md](configuration-durability.md).

## Start

The shipped example requires the owner token and keeps mutations disabled:

```env
DUNE_ADMIN_REQUIRE_TOKEN=true
DUNE_ADMIN_TOKEN=replace-with-a-long-random-token
DUNE_ADMIN_MUTATIONS_ENABLED=false
DUNE_ADMIN_ITEM_GRANTS_ENABLED=false
```

Start the service:

```bash
docker compose -f compose.yaml -f compose.allmaps.yaml --env-file .env up -d --no-deps --no-recreate admin-panel
```

On the `kspld0` lab host, `scripts/start-admin-panel-local.sh` runs DASH directly against the locally published Postgres port when the Funcom utility image is unavailable. The launcher refuses to run on any other hostname and enables mutations/item grants only for that lab process.

Open:

```text
http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}
```

When token auth is enabled, protected APIs use `X-Admin-Token`. The browser stores the token in local/session storage when entered in the header. When token auth is disabled, the token header is hidden and the Security page reports `local unlocked`.

## LAN Hostname

The Compose service binds to localhost:

```text
${DUNE_ADMIN_BIND_ADDRESS:-127.0.0.1}:${DUNE_ADMIN_HOST_PORT:-18080} -> admin-panel:8080
```

If another local process already owns `18080`, set these in `.env` and recreate `admin-panel`:

```dotenv
DUNE_ADMIN_BIND_ADDRESS=127.0.0.1
DUNE_ADMIN_HOST_PORT=18081
DUNE_ADMIN_ALLOWED_HOSTS=127.0.0.1:18081,localhost:18081,admin.example.test,admin-panel:8080
```

The admin panel should connect to Postgres through Compose DNS (`postgres:5432`). Do not point `DUNE_ADMIN_DB_HOST` at the host bridge address for normal use; that path is more fragile across Docker restarts.

Compose also passes `DUNE_GAME_DB_NAME` explicitly as the panel's
`DUNE_ADMIN_DB_NAME`. `/api/status` must report the same database used by the
running game services after every official schema migration. A default-looking
older schema name is a deployment failure: fix the environment and recreate
only the panel before using any admin mutation or panel-created database backup.

To use a LAN hostname such as `http://admin.example.test`, point LAN DNS or `/etc/hosts` at the host running the reverse proxy:

```text
admin.example.test -> <your-server-lan-ip>
```

Keep it on trusted LAN/VPN only. The panel still runs behind the local ingress and should not be exposed directly to the internet.

If you put a separate LAN reverse proxy in front of the local admin-panel port, make sure the hostname has an explicit route. A missing route can look like the panel is offline even when `127.0.0.1:${DUNE_ADMIN_HOST_PORT}` is healthy. The reverse proxy should forward the original `Host` header and target the local published port:

```caddyfile
@admin_panel {
    host admin.example.test
}
reverse_proxy @admin_panel 127.0.0.1:18081 {
    header_up Host {host}
    header_up X-Forwarded-Host {host}
    header_up X-Forwarded-Proto http
}
```

For Caddy or any other LAN ingress, also keep the admin hostname in a private/LAN allow rule. Do not add it to a public catch-all route.

If the local published port accepts TCP but returns no HTTP bytes, check for stale permanent neighbor entries on the Docker bridge after admin container recreation:

```bash
ip neigh show dev br-<dune-bridge-id> | grep '172.31.240.8\|172.31.240.9'
./scripts/seed-gateway-neighbor.sh
curl -H 'Host: admin-panel:8080' http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18081}/healthz
```

`scripts/seed-gateway-neighbor.sh` refreshes the host bridge entries for the admin ingress and panel containers. This avoids the failure mode where Docker's localhost proxy connects but sends traffic to an old container MAC after a recreate.

Optional hardening probe:

```dotenv
DUNE_ADMIN_LAN_URL=http://admin.example.test/healthz
```

```bash
./scripts/check-admin-ingress.sh .env
```

That script checks both the direct local panel endpoint and the optional LAN hostname. It catches the common failure where the container is healthy but the external reverse proxy returns `502` because no hostname route points at the panel.

## Optional GitLab Deployment

GitLab deployment is optional. The default admin-panel install and maintenance path is still direct Compose/systemd operation from the DASH host. Use the `.gitlab-ci.yml` jobs only when you deliberately want a protected `gitlab.home` shell runner to validate and manually deploy the panel.

The optional jobs are:

- `validate:admin-panel` compiles admin-related Python and runs safe-surface/chat tests.
- `deploy:admin-panel` is manual on the default branch and recreates only the `admin-panel` Compose service.
- `observe:admin-panel` is read-only and runs `scripts/check-admin-ingress.sh`.

The deploy path is:

```bash
scripts/deploy-admin-panel.sh .env
```

That script validates the Python entry points, runs the focused Admin tests,
pauses new autoscaler lifecycle ticks through the shared maintenance marker,
waits for an in-flight tick to drain, recreates Admin and its ingress, and checks
direct and LAN ingress. It does not restart Postgres, RabbitMQ, game maps,
Director, or Gateway. Before releasing the marker it reapplies and verifies the
process-local logoff patch on every running Survival/DD map. This makes an Admin
redeploy safe even when a dynamic map request arrived immediately beforehand.

Keep scheduled GitLab jobs read-only. Admin-panel deploys should stay manual because the panel is a live control surface for mutations, restarts, backups, config writes, and player operations. Do not give a public/shared runner Docker access to the Dune host.

Example nginx site:

```nginx
server {
    listen 80;
    server_name admin.example.test;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## Current Features

- Overview-first dashboard with player roster, realtime resource use, headline health metrics, map health, network checks, and health verdicts.
- Hagga Basin coordinate grid that projects persisted database pawn coordinates onto a clean `survival_1` tile composite at `admin/static/hagga-basin.webp` through `DUNE_HAGGA_MAP_*` calibration. The default grid follows the public map tile bounds, `X -457200..355600`, `Y -457200..355600`, with world X mapped horizontally and world Y mapped downward so the observed test persistence coordinates render south-central instead of north-central. The panel intentionally does not overlay DB POIs/waypoints/landmarks.
- Location source diagnostics are shown under the map and returned by `/api/players/hagga-basin`. Current findings are documented in [`../PLAYER_LOCATION_SOURCE_AUDIT.md`](../PLAYER_LOCATION_SOURCE_AUDIT.md): actor transforms, `load_travel_to_player_info`, return info, respawn locations, actor state, overmap rows, game events, and normal server logs do not currently provide a proven live in-game arrow position.
- Server/farm state view with per-map online/offline health derived from `world_partition`, `farm_state`, and `active_server_ids`.
- Realtime resource view for host load, host memory, workspace disk, and Docker container CPU/memory/network/block I/O when the Docker socket is available.
- Infrastructure page with project-scoped Compose service/log control, manual
  and automatic verified backup lifecycle and restore, update/repair workflows,
  and an allowlisted `dune`/`public` database console with query, row, export,
  and password controls. See [`infrastructure-console.md`](infrastructure-console.md).
- Infrastructure controls for automatic verified full backups, layered restore,
  disposable PostgreSQL restore proof, sequential networkless admin/game
  RabbitMQ copied-state recovery proof with name-free topology counts and
  HMAC-anchored receipts,
  minimum-footprint/balanced/adaptive/full-warm/custom map profiles, per-map retention,
  Director travel-demand starts, LRU and memory-floor eviction, live map
  limits, p95-based ready-by map warming, the automatic memory balancer,
  retained Prometheus metrics, updates, and runtime repair. See
  [`autoscaling-memory.md`](autoscaling-memory.md),
  [`anticipatory-map-warming.md`](anticipatory-map-warming.md), and
  [`metrics.md`](metrics.md).
- Public Directory status on Infrastructure reports whether opt-in publication
  is private, valid/current, or invalid; it shows coarse signed profile state,
  expiry and truncated identity, links to the descriptor/public site, and
  exports label-free health metrics. Configuration stays in the safe Settings
  editor and the public renderer owns key generation/publication. See
  [`federated-public-directory.md`](federated-public-directory.md).
- Feature Readiness on Infrastructure evaluates the complete parity activation
  catalog against live gates, credential presence, confined artifacts, Compose
  services, dependencies, runtime probes, and explicit canary state. It returns
  no secret values and separates intentionally disabled optional integrations
  from active failures. Distinct state vectors enter an append-only HMAC ledger
  correlated with assured deployments, so the same card shows regressions and
  recoveries rather than only current state; see
  [`feature-readiness.md`](feature-readiness.md).
- Isolated Proof Autopilot on Infrastructure keeps the Community,
  Creator/Modding, and public-IP signed receipts current before expiry or after
  bound-input drift. It shows every scheduling reason and retry deadline and
  offers an `infrastructure.write` force-all action that still uses only
  disposable state. See [`canary-autopilot.md`](canary-autopilot.md).
- Operator Briefing on Overview synthesizes 15 existing governance, readiness,
  reliability, recovery, deployment, scaling, credential, proof, and update
  authorities into one signed priority queue. It shows categorical deltas and
  links to the existing control surfaces without executing recommendations.
  See [`operations-briefing.md`](operations-briefing.md).
- Capacity Intelligence beside the autoscaler: retained map-hours saved,
  idle-running cost, productive-running ratio, warm/cold revisits,
  demand-to-ready cold-start distributions, empirical next-visit forecasts,
  evidence-qualified gradual per-map retention, append-only application
  receipts, measured-p95 just-in-time warm scheduling, private metrics, and
  recovery. See [`capacity-intelligence.md`](capacity-intelligence.md) and
  [`anticipatory-map-warming.md`](anticipatory-map-warming.md).
- Desired-State Attestation beside reliability and capacity: HMAC-sealed
  repository/configuration and project-container baselines, retained drift,
  non-resolving ownership acknowledgements, baseline/event history, private
  metrics, SLO coupling, and backup-bound key verification. See
  [`desired-state-attestation.md`](desired-state-attestation.md).
- Change Intelligence correlates SLO/drift incident onset with preceding
  deployments, settings writes, service actions, restarts, restores, and
  capacity decisions in an append-only HMAC timeline. Evidence capsules rank
  candidates but never claim causality. Operators can download a bounded
  capsule signed over the verified ledger head and verify it offline. Each
  capsule includes a deterministic, policy-digested response plan that reuses
  exact bounded diagnostics and guarded recovery surfaces without executing
  them. Operators can drill one incident or certify the complete policy: shared
  diagnostics run once, all runbook recovery contracts are scored, exact gaps
  are shown, and no recovery/game mutation executes. See
  [`incident-response.md`](incident-response.md) and
  [`change-intelligence.md`](change-intelligence.md).
- Assured Change Windows turn reviewed control-plane promotion into a two-phase
  proof: exact commit/source, pre/post recovery, all-map container/process
  continuity, desired state, fleet readiness, SLO/Prometheus health, and a
  semantically verified HMAC receipt. The dashboard is bounded status/history;
  staging and promotion remain production-host-only. See
  [`deployment-assurance.md`](deployment-assurance.md).
- Security includes a Credential Lifecycle Control Center covering 19
  activation-aware first-party credentials. It reports presence, placeholder
  rejection, source permissions, minimum material, consumers, observed
  rotation age, and newest-backup coverage without returning values or keyed
  fingerprints. Its append-only HMAC observation history and recovery workflow
  are documented in [`credential-lifecycle.md`](credential-lifecycle.md).
- Infrastructure backup controls report recipient-encryption readiness,
  encrypted archive/receipt inventory, and exact host encrypt/decrypt commands.
  The private recovery key stays outside the admin container. See
  [`backup-encryption.md`](backup-encryption.md).
- Browser first-run Bootstrap page for settings readiness, read-only preflight,
  RabbitMQ TLS generation, idempotent database initialization, and post-hook-aware
  stack reconcile. See [`bootstrap-console.md`](bootstrap-console.md).
- Catalog-backed native skill, water, kick/kick-all, and vehicle spawn actions,
  plus offline vehicle repair/refuel. See
  [`player-runtime-actions.md`](player-runtime-actions.md).
- Blueprint archive lifecycle, structured augment selection/application,
  automatic/manual care packages, permissioned Discord adapter routes, and the
  SHA-pinned community addon runtime.
- Community Rewards page with isolated wallets, verified append-only ledger,
  account-link codes, versioned item/kit catalog and stock, purchases, playtime
  and signed-provider accrual, reward tracks/claims, delivery receipts, and
  ambiguous-outcome reconciliation. See
  [`community-rewards.md`](community-rewards.md).
- A policy-bound disposable Community Rewards transaction proof with signed
  portable receipts and an explicit no-live-data contract. See
  [`community-rewards-canary.md`](community-rewards-canary.md).
- An input-bound disposable Creator/Modding lifecycle proof covering base,
  preset, Landsraad, cosmetics, and addon paths with signed portable receipts
  and no live database/configuration/map/network access. See
  [`creator-modding-canary.md`](creator-modding-canary.md).
- Public-IP Repair Proof executes the full address/TLS/restart/retry/timer path
  in disposable state and records an input-bound signed receipt; see
  [`public-ip-repair-canary.md`](public-ip-repair-canary.md).
- Moderation page with isolated case and policy-ban state, append-only case
  history, confirmed native `KickPlayer` ejection, optional explicit allowlist
  enforcement, presence sessions, privacy-bounded heatmaps, redacted security
  signals, retention, and enforcement receipts. The current build has no proven
  native persistent-ban/login-rejection contract. See
  [`moderation-history.md`](moderation-history.md).
- Base Creator page with read-only exact/recentered live-base exports, portable
  JSON archives, snapping/yaw grid editing, top-down reconstruction preview,
  downloads, and an isolated visibility/rating gallery. Direct live restore is
  not claimed because no peer or current schema evidence proves an atomic
  placement contract. The same page now adds recoverable base retirement through
  the current build's native `dune.base_backup_save_from_totem` path. Retirement
  requires the target map stopped, owners/recovery player offline, a current
  SHA-256 preview fingerprint, a full PostgreSQL dump, transaction locks,
  post-call verification, and a private receipt; it does not directly delete
  base rows. The same inventory exposes the raw pack-up timestamp and a
  separately gated, stopped-map/offline-owner, fingerprint-bound reset-to-zero
  workflow with a full dump, compare-and-swap, readback verification, private
  receipt, and no automatic map lifecycle. See
  [`base-creator.md`](base-creator.md),
  [`base-retirement.md`](base-retirement.md), and
  [`base-packup-cooldown.md`](base-packup-cooldown.md).
- Gameplay Presets page with nine curated worm, threat, storm, harvest, day,
  hydration, and world profiles; exact per-setting preview; fixed target and
  key/type/range allowlists; private backups; atomic apply; confined rollback;
  mandatory seven-day Landsraad validation; and a separate guarded restart
  handoff. See [`gameplay-presets.md`](gameplay-presets.md).
- Cosmetics page with an independently observed 391-ID reviewed catalog,
  optional operator-owned pak generation, searchable current unlocks,
  catalog-confined idempotent add/remove, customization-only bulk unlock,
  Offline player enforcement under a row lock, automatic full database backup,
  compare-and-swap/post-write verification, private receipts, and hash-guarded
  rollback. Writes require relog but never restart a map. See
  [`character-cosmetics.md`](character-cosmetics.md).
- Command Console page with six named native read-only diagnostics, no
  subprocess, shell, user arguments, or arbitrary paths; bounded
  runtime/output, secret redaction, `operations.write` authorization, and
  receipt-only auditing. See [`command-console.md`](command-console.md).
- Provider-neutral OIDC or Discord OAuth authorization-code login with PKCE,
  state/nonce/replay protection, RS256 ID-token validation, explicit immutable
  subject mapping into live local RBAC, signed expiring HttpOnly sessions,
  logout, secret rotation, and owner-token recovery. See
  [`federated-auth.md`](federated-auth.md).
- World page with bounded read-only guild/member, Landsraad term/task/reward/contribution, and aggregate storage views. See [`world-console.md`](world-console.md).
- Local/upstream health checks for Postgres reachability, the Dune account portal, and public Dune/Funcom HTTP reachability.
- Restart-announcement scheduler under Ops. It accepts a restart time, message, and repeat interval, persists state under `backups/admin-panel/announcements.json`, and invokes `DUNE_ADMIN_ANNOUNCE_COMMAND` for each delivery attempt.
- Scheduled restart planner under Ops. It targets all components, core services, the service layer, all game maps, or key individual maps. Jobs persist under `backups/admin-panel/restart-jobs.json` and invoke `DUNE_ADMIN_RESTART_COMMAND` only when execution is explicitly enabled.
- Admin Digests includes persistent Base Recovery Reminders and Vehicle Recovery Reminders. Each record binds an account to native BRT or `VehicleRecovery`/`VehicleBackup` ids, shows the worker's latest conservative restore proof and player presence, and sends one Paul whisper per detected login session until restoration. Add/replace and remove actions are separately gated and audited; removing a record never changes the player, base/vehicle, recovery row, inventory, or credits. Vehicle Retirement adds a stopped-map, full-backup-first native vehicle archival path with a private pre-action cargo snapshot; see [`vehicle-recovery.md`](vehicle-recovery.md).
- Player roster split into currently online players and offline players, plus search and detail views.
- Currency/progression table visibility.
- `.env` operations editor for install, world, network, access, secret, and admin-panel knobs. Secret fields are admin-token protected, rendered as password inputs, and returned blank unless a replacement is typed.
- Artificial Exchange controls under Settings. Operators can manage the first-class Exchange economy workflow: rebuild the reviewed catalog, check buyer/seller readiness, run buyer dry-runs, inspect and claim seller settlement candidates through the gated bot, validate the seeded-listing populator, manage buyer/populator/watchdog systemd units, and edit all `DUNE_ARTIFICIAL_EXCHANGE_*` gates and tuning knobs through the safe `.env` editor.
- Typed logout/reconnect timer editor for `config/UserGame.ini` under Settings -> Logout and Reconnect Timers.
- Typed Director character-transfer settings editor for `config/director.ini`.
- Catalog tab for content-insertion surfaces, evidence levels, validation commands, typed knob dry-runs, spice/resource inspection, event dry-runs, and economy bundle dry-runs.
- Discovery tab for build/surface evidence and the retained ecosystem peer
  revision watch. `GET /api/ops/peer-watch` reports allowlisted primary-source
  pin drift, source failures, collector freshness, and transition history;
  `?refresh=1` performs an authenticated immediate poll but never updates a pin
  or writes upstream. See [`peer-watch.md`](peer-watch.md).
- Read-only content catalog APIs:
  - `GET /api/catalog/surfaces`
  - `GET /api/catalog/evidence`
  - `GET /api/catalog/validation`
- Artificial Exchange operator API:
  - `GET /api/admin/artificial-exchange`
  - `POST /api/admin/artificial-exchange`
  Supported actions are `build-catalog`, `check-ready`, `buyer-dry-run`, `settlement-report`, `validate-populator`, `install-buyer-service`, `install-populator-service`, `install-watchdog-timer`, `watchdog-once`, and `start|stop|restart|status:buyer|populator|watchdog`.
- Typed gameplay knob API at `GET/POST /api/settings/typed-knobs`. Dry-runs are available without the typed-write gate; writes require backups, the global mutation gate, `DUNE_ADMIN_TYPED_KNOBS_ENABLED=true`, and the confirmation phrase `WRITE TYPED KNOBS`.
- Config editor for selected local config files, including official `UserEngine.ini`, `UserGame.ini`, and 1.5.3+ `UserServerCustomSettings*.ini` overlays plus the validated versioned community-rewards policy, with backups under `backups/admin-panel`.
- Director GME voice-chat credentials can be added through the `director.ini` config editor when Funcom/provider supplies real `GmeAppId` and `GmeAppKey` values. Leave them unset otherwise.
- Currency and XP mutation endpoints gated by `DUNE_ADMIN_MUTATIONS_ENABLED`.
- Economy bundle planning through `POST /api/admin/bundle`. It plans currency, XP, and item grants in one response and defaults to `dry_run=true`. Execution additionally requires `DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED=true` and confirmation `EXECUTE BUNDLE`.
- Reviewed manual and automatic care packages through
  `GET/POST /api/admin/care-packages`. Automatic rules support first-online and
  returning-player grants with persisted claims/pending/history and retry/clear
  controls. Preview remains available while execution requires the master,
  bundle, care-package, automatic (for worker grants), and applicable item-grant
  gates, exact confirmation, and an automatic database backup. See
  [`care-packages.md`](care-packages.md).
- Targeted Solari grants through `POST /api/admin/solari/inventory` for carried `SolarisCoin` stacks and `POST /api/admin/solari/bank` for Exchange/bank balance.
- Native offline player teleport through `POST /api/admin/player-recovery/offline-teleport`. Execution is fingerprint-bound, backup-first, advisory/row locked, privately receipted, and transform-verified through the shipped `dune.admin_move_offline_player_to_partition(...)` helper; see [offline-player-teleport.md](offline-player-teleport.md).
- Native portable character snapshots through `GET/POST /api/admin/character-backups` and read-only planning through `/api/admin/character-backups/preview`. Capture uses the shipped transfer export without retaining a database write; restore is patch/fingerprint-bound, full-backup-first, locked, native-import-only, identity-verified, and privately receipted; see [character-backups.md](character-backups.md).
- Native offline dead-state recovery through `POST /api/admin/player-recovery/life-state`. Execution is fingerprint-bound, backup-first, advisory/row locked, privately receipted, and verified through the shipped `dune.update_death_location(...)` function; see [offline-player-life-state-recovery.md](offline-player-life-state-recovery.md).
- Spice/resource field inspection through `POST /api/admin/spice-fields/inspect`.
- Progression surface inspection through `POST /api/admin/progression/inspect`; this discovers faction, reputation, journey, recipe, vehicle, and related DB function/table evidence without executing discovered functions.
- Faction reputation planning through `POST /api/admin/faction-reputation`, default `dry_run=true`. Execution requires `DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED=true` and confirmation `WRITE REPUTATION`.
- Player faction-change planning through `POST /api/admin/faction`, default `dry_run=true`. Execution requires `DUNE_ADMIN_FACTION_MUTATIONS_ENABLED=true`, confirmation `CHANGE FACTION`, and an offline target player.
- Journey story-node planning through `POST /api/admin/journey`, default `dry_run=true`. Execution requires `DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED=true`, confirmation `WRITE JOURNEY`, and an offline target player.
- Landsraad term, reward-tier, and player-contribution planning through
  `POST /api/admin/landsraad`, default `dry_run=true`. Reward/contribution
  execution creates a backup and recalculates contribution totals in one
  transaction. Execution requires
  `DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED=true` and `WRITE LANDSRAAD`.
- Respawn-location delete planning through `POST /api/admin/respawn-location`, default `dry_run=true`. Execution requires `DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED=true`, confirmation `DELETE RESPAWN`, and an offline target player.
- World-state inspection through `POST /api/admin/world-state/inspect` for guild, vehicle, marker, landclaim, recipe, and respawn evidence.
- Guild description and role planning through `POST /api/admin/guild`, default `dry_run=true`. Execution requires `DUNE_ADMIN_GUILD_MUTATIONS_ENABLED=true` and confirmation `WRITE GUILD`.
- Marker deletion planning through `POST /api/admin/marker`, default `dry_run=true`. Execution requires `DUNE_ADMIN_MARKER_MUTATIONS_ENABLED=true` and confirmation `DELETE MARKERS`.
- Landclaim segment planning through `POST /api/admin/landclaim`, default `dry_run=true`. Execution requires `DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED=true` and confirmation `WRITE LANDCLAIM`.
- Economy inspection through `POST /api/admin/economy/inspect` for Dune Exchange, vehicle, recovered/backup vehicle, and base-backup evidence.
- Dune Exchange Solari planning through `POST /api/admin/exchange`, default `dry_run=true`. Execution requires `DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED=true` and confirmation `WRITE EXCHANGE`.
- Player lifecycle inspection through `POST /api/admin/player-lifecycle/inspect` for account/player, party, tags, access codes, Communinet, dungeon, tutorial, and lifecycle evidence.
- Player tag planning through `POST /api/admin/player-tags`, default `dry_run=true`. Execution requires `DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED=true` and confirmation `WRITE PLAYER TAGS`.
- Access-code planning through `POST /api/admin/access-code`, default `dry_run=true`. Execution requires `DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED=true` and confirmation `WRITE ACCESS CODES`.
- Character slot inspection and planning through `GET /api/admin/character-slots?account_id=...`, `POST /api/admin/character-slots/plan`, and `POST /api/admin/character-slots/execute`, default `dry_run=true`. Execution requires `DUNE_ADMIN_CHARACTER_SWAP_ENABLED=true`, confirmation `SWAP CHARACTER`, offline targets, a DB backup, and a proven native lifecycle contract; current builds are inspect/plan-only when that contract is absent.
- Canonical player identity and orphan-safe native deletion through `GET/POST /api/admin/player-identity-integrity`. Roster/detail/action resolution chooses the newest valid player-state row per account; the Players page reports duplicates and true orphans. Cleanup and deletion are fingerprint-bound, backup-first, transaction-locked, post-write verified, privately receipted, and governed as critical changes. See [`player-identity-integrity.md`](player-identity-integrity.md).
- Communinet planning through `POST /api/admin/communinet`, default `dry_run=true`. Execution requires `DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED=true` and confirmation `WRITE COMMUNINET`.
- Tutorial entry planning through `POST /api/admin/tutorial`, default `dry_run=true`. Execution requires `DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED=true` and confirmation `WRITE TUTORIAL`.
- Permission actor planning through `POST /api/admin/permission`, default `dry_run=true`. Execution requires `DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED=true` and confirmation `WRITE PERMISSION`.
- Vendor stock-cycle timestamp planning through `POST /api/admin/vendor`, default `dry_run=true`. Execution requires `DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED=true` and confirmation `WRITE VENDOR`.
- Event planner APIs:
  - `GET /api/events`
  - `POST /api/events/dry-run`
  - `POST /api/events`
  - `POST /api/events/cancel`
  - `POST /api/events/run`
  Event execution is blocked unless `DUNE_ADMIN_EVENT_EXECUTION_ENABLED=true`.
- Postgres custom-format backup under `backups/admin-panel`.
- Redacted JSONL audit trail for rejected requests and admin writes under `backups/admin-panel/audit.jsonl`.
- Asynchronous signed delivery of filtered audit events to generic HTTPS or Discord webhook receivers, with bounded retry/queue/rate controls, recursive redaction, and redirect refusal. Authenticated runtime status is available at `GET /api/ops/webhooks`; see [`outbound-webhooks.md`](outbound-webhooks.md).
- Known item template, observed item template, inventory, and inventory-type references.
- Player dropdowns in Admin Actions for currency, carried/bank Solari, XP, keystones, item grant targeting, and item maintenance.
- Selected players pre-populate controller/account/name fields, current currency and specialization selectors, owned inventories, and owned inventory items for stack/quality edits or deletion.
- Exact-template item grants, dry-runs, and item deletion behind admin gates. Existing-item stack/quality edits require the player offline, create a database backup, lock the item and owner state in one transaction, recheck offline state under that lock, preserve every other `inventoryitem` field through `dune.save_item`, verify both saved values before commit, and require a relog for the client cache.
- Unified visual item/schematic/patent grant catalog with case-insensitive template identity, deterministic rich-row deduplication, group/category/kind search, tier/name ordering, complete progressive loading, cached same-origin icons, favorites, an inspection tray, and an explicit final confirmation. Refresh committed metadata with `scripts/sync-item-catalog.py`; icon binaries are cached under ignored `backups/admin-panel/item-icons/` on first view.

## Content Catalog and Safe Expansion

The source-of-truth evidence catalog is [`../CONTENT_INSERTION_SURFACES.md`](../CONTENT_INSERTION_SURFACES.md). The Catalog tab renders the same model through read-only APIs and groups surfaces as:

- Deep Desert
- Economy/Admin
- World Rules
- GM/RabbitMQ
- Limits

Each catalog entry uses this schema:

```text
surface, capability, evidence, confidence, mutationRisk,
restartRequired, validationCommand, rollback
```

Evidence handling is deliberately strict:

- Shipped config plus live database behavior is strong evidence.
- Binary strings are leads until section, syntax, and runtime effect are proven.
- Public websites are candidate lookup sources only.
- Generic admin-RMQ GM candidates remain previews until
  `DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true`. The separate catalog-backed player
  actions use the pinned Version 2 game-notification contract and their own
  runtime gate; see [`player-runtime-actions.md`](player-runtime-actions.md).

The Catalog tab includes safe forms for:

- Typed knob dry-runs.
- Spice/resource state inspection.
- Event dry-run planning.
- Economy bundle dry-run planning.

These forms are intentionally preview-first. They make the plan visible before any write gate can matter.

## New Admin Gates

The content expansion adds separate gates so operators can expose discovery without enabling writes:

For the current actionability split, read [`admin-actionability-matrix.md`](admin-actionability-matrix.md). Confidence is high that every write-capable endpoint listed there is dry-run-first and gate-protected; confidence varies by game-system side effect.

The safe-surface test suite now includes mocked dry-run and fail-closed checks for the promoted mutator families. These tests prove the admin code plans writes and refuses execution when gates are disabled; they do not prove live client-visible game behavior. Use the disposable validation queue in [`admin-actionability-matrix.md`](admin-actionability-matrix.md) before routine use on real player/world state.

```env
DUNE_ADMIN_CATALOG_ENABLED=true
DUNE_ADMIN_TYPED_KNOBS_ENABLED=false
DUNE_ADMIN_EVENT_EXECUTION_ENABLED=false
DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED=false
DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED=false
DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED=false
DUNE_ADMIN_FACTION_MUTATIONS_ENABLED=false
DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED=false
DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED=false
DUNE_ADMIN_GUILD_MUTATIONS_ENABLED=false
DUNE_ADMIN_MARKER_MUTATIONS_ENABLED=false
DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED=false
DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED=false
DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED=false
DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED=false
DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED=false
DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED=false
DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED=false
DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED=false
DUNE_ADMIN_CHARACTER_SWAP_ENABLED=false
DUNE_ADMIN_PLAYER_IDENTITY_MUTATIONS_ENABLED=false
DUNE_ADMIN_CHARACTER_DELETE_ENABLED=false
DUNE_ADMIN_PLAYER_RUNTIME_MUTATIONS_ENABLED=false
DUNE_ADMIN_PLAYER_LIFE_RECOVERY_ENABLED=false
DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED=false
DUNE_ADMIN_CHARACTER_BACKUPS_ENABLED=false
DUNE_ADMIN_VEHICLE_MUTATIONS_ENABLED=false
DUNE_ADMIN_MEMORY_MUTATIONS_ENABLED=false
DUNE_ADMIN_AUTOSCALER_MUTATIONS_ENABLED=false
DUNE_ADMIN_BOOTSTRAP_MUTATIONS_ENABLED=false
DUNE_ADMIN_BACKUP_MUTATIONS_ENABLED=false
DUNE_ADMIN_ADDON_MUTATIONS_ENABLED=false
DUNE_DISCORD_ADAPTER_ENABLED=false
```

Gate behavior:

The safe environment editor also exposes the host-scoped network/topology
controls `DUNE_PUBLIC_IP_MONITOR_*`, `DUNE_SIETCH_MUTATIONS_ENABLED`, and
`DUNE_SIETCH_ALLOWED_HOST`. Editing those values does not install a timer or
change topology by itself. Timer installation and Sietch reconciliation use the
documented host scripts, exact-host checks, dry-run previews, and backups in
[`operations.md`](operations.md).

- `DUNE_ADMIN_CATALOG_ENABLED`: controls read-only catalog endpoints.
- `DUNE_ADMIN_TYPED_KNOBS_ENABLED`: controls typed config writes only. Typed dry-runs still work.
- `DUNE_ADMIN_EVENT_EXECUTION_ENABLED`: controls event execution. Event creation and dry-run planning still work.
- `DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED`: controls economy bundle execution. Bundle dry-runs still work.
- `DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED`: controls faction reputation writes through `dune.set_player_faction_reputation`. Progression inspection and reputation dry-runs still work.
- `DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED`: controls journey server-function calls. Journey dry-runs still work.
- `DUNE_ADMIN_FACTION_MUTATIONS_ENABLED`: controls player faction-change server-function calls. Faction dry-runs still work.
- `DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED`: controls Landsraad term server-function calls. Landsraad dry-runs still work.
- `DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED`: controls respawn-location deletion through `dune.update_respawn_locations`. Respawn dry-runs still work.
- `DUNE_ADMIN_GUILD_MUTATIONS_ENABLED`: controls guild description and role server-function calls. Guild dry-runs still work.
- `DUNE_ADMIN_MARKER_MUTATIONS_ENABLED`: controls marker deletion server-function calls. Marker dry-runs still work.
- `DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED`: controls landclaim segment server-function calls. Landclaim dry-runs still work.
- `DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED`: controls Dune Exchange Solari balance server-function calls. Economy dry-runs still work.
- `DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED`: controls player tag server-function calls. Lifecycle dry-runs still work.
- `DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED`: controls server player access-code server-function calls. Lifecycle dry-runs still work.
- `DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED`: controls Communinet player/channel server-function calls. Lifecycle dry-runs still work.
- `DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED`: controls tutorial entry server-function calls. Lifecycle dry-runs still work.
- `DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED`: controls permission actor name/access/rank server-function calls. World-state dry-runs still work.
- `DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED`: controls vendor stock-cycle timestamp server-function calls. Lifecycle dry-runs still work.
- `DUNE_ADMIN_CHARACTER_SWAP_ENABLED`: controls character slot hibernation/switch execution. Slot inspection and planning still work; execution remains blocked unless the plan returns `executable: true`.
- `DUNE_ADMIN_PLAYER_IDENTITY_MUTATIONS_ENABLED`: controls fingerprint-bound cleanup of player-state rows whose account no longer exists. Integrity inspection and cleanup preview remain available.
- `DUNE_ADMIN_CHARACTER_DELETE_ENABLED`: second gate for offline, backup-first native character deletion. It also requires the player identity gate; deletion preview remains available.
- `DUNE_ADMIN_PLAYER_RUNTIME_MUTATIONS_ENABLED`: second gate for native
  skill/water/kick/vehicle commands; also requires the general GM gate and
  shared server-command token.
- `DUNE_ADMIN_PLAYER_LIFE_RECOVERY_ENABLED`: controls fingerprint-bound native
  recovery of an explicitly Offline `Dead`, `DeadByCoriolis`, or
  `DeadBySandworm` character to persisted `Alive`. Preview remains available.
- `DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED`: controls fingerprint-bound,
  backup-first native movement of an explicitly Offline player's persisted
  pawn. Preview remains available.
- `DUNE_ADMIN_CHARACTER_BACKUPS_ENABLED`: controls private native transfer
  snapshot capture and destructive character restore/delete. Preview, bounded
  metadata listing, integrity verification, and authenticated download remain
  available while disabled; restore also requires a current isolated native
  export/import proof. See [character-backups.md](character-backups.md).
- `DUNE_ADMIN_VEHICLE_MUTATIONS_ENABLED`: controls offline persistent vehicle
  durability and fuel repair.
- `DUNE_ADMIN_MEMORY_MUTATIONS_ENABLED`: controls live container limits and
  automatic memory balancing.
- `DUNE_ADMIN_AUTOSCALER_MUTATIONS_ENABLED`: controls minimum/balanced/full/custom
  profiles, map modes, per-map retention, explicit and Director-derived demand,
  LRU/memory-pressure eviction, and reconciliation. The profile/default editor
  fields require an admin-panel recreate; live Infrastructure controls persist
  immediately. See [`autoscaling-memory.md`](autoscaling-memory.md).
- `DUNE_ADMIN_BOOTSTRAP_MUTATIONS_ENABLED`: controls browser TLS generation,
  database initialization, and stack reconcile.
- `DUNE_ADMIN_BACKUP_MUTATIONS_ENABLED`: controls full backup creation,
  imports, quarantine delete, and the automatic coverage-checked schedule. The
  schedule serializes with host backups and assured deployments, retains exact
  failure diagnostics, and retries earlier than its normal interval; see
  [`automatic-backups.md`](automatic-backups.md).
- `DUNE_ADMIN_ADDON_MUTATIONS_ENABLED`: controls community addon install,
  enable/disable, and quarantine removal.
- `DUNE_DISCORD_ADAPTER_ENABLED`: exposes the separately authenticated,
  role-mapped read-only Discord adapter route family.
- `DUNE_ADMIN_GRANT_PRIVATE_MESSAGE_ENABLED`: controls best-effort private relog reminders after successful admin item and inventory-Solari grants.
- `DUNE_ADMIN_GRANT_PRIVATE_MESSAGE_TEMPLATE`: private relog reminder template. Supports `{item}`, `{amount}`, and `{template_id}`.

Existing gates still apply:

```env
DUNE_ADMIN_MUTATIONS_ENABLED=true
DUNE_ADMIN_ITEM_GRANTS_ENABLED=true
DUNE_ADMIN_GM_COMMANDS_ENABLED=false
DUNE_GM_COMMAND_PAYLOAD_VERIFIED=false
```

Write confirmation phrases:

```text
WRITE TYPED KNOBS
EXECUTE BUNDLE
MOVE OFFLINE PLAYER
WRITE REPUTATION
WRITE JOURNEY
CHANGE FACTION
WRITE LANDSRAAD
DELETE RESPAWN
WRITE GUILD
DELETE MARKERS
WRITE LANDCLAIM
WRITE EXCHANGE
WRITE PLAYER TAGS
WRITE ACCESS CODES
WRITE COMMUNINET
WRITE TUTORIAL
WRITE PERMISSION
WRITE VENDOR
RUN GM COMMAND
RUN PLAYER ACTION
KICK ALL ONLINE PLAYERS
REPAIR VEHICLE DECAY
REFUEL VEHICLE
CHANGE MEMORY BALANCER
SET MAP MEMORY
CHANGE AUTOSCALER
CHANGE BACKUP SCHEDULE
RUN BOOTSTRAP
```

## Typed Gameplay Knobs

`GET /api/settings/typed-knobs` returns the typed knob registry and current values. `POST /api/settings/typed-knobs` accepts:

```json
{
  "dry_run": true,
  "updates": {
    "gatheringAmount": "2.5"
  }
}
```

For Deep Desert spice caps, the dry-run body can use structured caps:

```json
{
  "dry_run": true,
  "updates": {
    "spiceDeepDesertCaps": {
      "Medium": {"primed": 24, "active": 24},
      "Large": {"primed": 3, "active": 3}
    }
  }
}
```

Current typed knobs:

| ID | File | Section/key | Confidence | Risk |
| --- | --- | --- | --- | --- |
| `spiceDeepDesertCaps` | `UserGame.ini` | `[/Script/DuneSandbox.SpiceHarvestingSystem] m_PerMapSystemSettings` | high | medium |
| `pvpMode` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] PVPMode` | high | medium |
| `gatheringAmount` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] GatheringAmount` | high | low |
| `sandstormsAllowed` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] bAllowSandstorms` | high | medium |
| `sandwormsAllowed` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] bAllowSandworms` | high | medium |
| `buildingRestrictionsEnabled` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] bIsBuildingRestrictionsEnabled` | high | medium |
| `baseBackupToolTimeRestrictionHours` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] BaseBackupToolTimeRestriction` | high | medium |
| `landsraadSpecializationXpMultiplier` | `UserServerCustomSettings.ini` | `[/Script/DuneSandbox.UserServerCustomSettings] LandsraadSpecializationXpMultiplier` | high | medium |
| `sandstormTreasureEnabled` | `UserEngine.ini` | `[ConsoleVariables] Sandstorm.Treasure.Enabled` | moderate | medium |
| `coriolisAutoSpawnEnabled` | `UserGame.ini` | `[/Script/DuneSandbox.SandStormConfig] m_bCoriolisAutoSpawnEnabled` | high | medium |
| `pvpResourceMultiplier` | `UserEngine.ini` | `[ConsoleVariables] SecurityZones.PvpResourceMultiplier` | high | low |
| `characterRecustomizationCost` | `UserGame.ini` | `[/Script/DuneSandbox.CharacterRecustomizerSubsystem] m_CostAmount` | high | low |
| `buildingShelterThreshold` | `UserGame.ini` | `[/Script/DuneSandbox.ShelterSettings] m_BuildingShelterThreshold` | moderate | experimental |
| `placeableShelterThreshold` | `UserGame.ini` | `[/Script/DuneSandbox.ShelterSettings] m_PlaceableShelterThreshold` | moderate | experimental |
| `shelteredProtectionThreshold` | `UserGame.ini` | `[/Script/DuneSandbox.HydrationSubsystem] ShelteredProtectionThreshold` | low | experimental |

Typed writes create a backup under `backups/admin-panel` before writing the config file. Most of these values require restarting the affected map containers.

Character recustomization is currently overridden to `0` in `config/UserGame.ini`, making it free instead of the shipped/default `5000` Solaris. Use the `characterRecustomizationCost` typed knob to restore or tune the cost.

The typed layer deliberately does not expose Coriolis cycle seed, DB wipe, or cycle-end restart fields.

## Economy Bundle Plans

`POST /api/admin/bundle` produces a single audited plan for currency, XP, and item grants. Default behavior is dry-run:

```json
{
  "dry_run": true,
  "currency": [
    {"player_controller_id": 123, "currency_id": 1, "amount": 1000, "mode": "add"}
  ],
  "xp": [
    {"player_id": 123, "track_type": "Combat", "amount": 1000, "mode": "add"}
  ],
  "items": [
    {"account_id": 456, "template_id": "SolarisCoin", "stack_size": 1}
  ]
}
```

Execution requires all of:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_ITEM_GRANTS_ENABLED=true` for item rows
- `confirm: "EXECUTE BUNDLE"`

Rollback is compensating work from the audit record: set currency back, set XP back through the specialization function, and delete or adjust granted item rows.

## Offline Player Recovery

`POST /api/admin/player-recovery/offline-teleport` previews or executes a strict-offline pawn move. It backs the Admin Actions Offline Teleport panel, which lets an operator pick a player, choose a partition, enter exact coordinates, or click the Hagga Basin map to fill rough X/Y coordinates.

The endpoint calls the shipped database helper below. This is the verified primitive for the network-disconnect teleport path once Survival has marked the player `Offline`, because it updates the pawn actor row consumed by reconnect:

```sql
dune.admin_move_offline_player_to_partition(
  in_fls_id text,
  in_target_partition_id bigint,
  in_target_location dune.vector
)
```

The endpoint does not force a disconnect and does not hide the timeout-based online-adjacent workflow. Operators must make the target truly `Offline` first, either by waiting for a normal logout or by using the documented network-timeout runbook. That workflow remains separate in [soft-disconnect-teleport.md](soft-disconnect-teleport.md).

Dry-run body:

```json
{
  "dry_run": true,
  "account_id": 456,
  "partition_id": 12,
  "location": {"x": 0, "y": 0, "z": 0}
}
```

The endpoint requires both the explicit persisted `Offline` state and
`dune.is_player_offline(fls_id)`, a valid pawn, an unblocked partition, the
native function, and finite coordinates bounded to +/-10,000,000 on every
axis. Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED=true`
- the unchanged preview `expectedFingerprint`
- `confirm: "MOVE OFFLINE PLAYER"`

Execution takes a full backup, writes a pending receipt, locks the account,
player-state, pawn, and target partition, rechecks the fingerprint, invokes the
native helper, and requires persisted pawn actor/map/dimension/partition/XYZ
readback plus both Offline predicates before commit. Rollback is a newly
previewed native move to the receipted prior partition/location or restoration
of the referenced full backup. No map restart is required; the player relogs.
The exact contract, private evidence, metrics, and networkless rolled-back
semantic proof are documented in [offline-player-teleport.md](offline-player-teleport.md).

### Native persisted life-state recovery

`POST /api/admin/player-recovery/life-state` is a separate recovery contract. It does not teleport and does not issue an unverified heal/revive command. Preview fingerprints the exact active player-state, pawn, Offline predicate, current dead state, death-location digest, and native function availability. Execution requires `DUNE_ADMIN_PLAYER_LIFE_RECOVERY_ENABLED=true`, an unchanged fingerprint, and `RECOVER OFFLINE PLAYER LIFE STATE`.

The execution path creates a full dump, takes account advisory plus player-state/pawn row locks, rechecks eligibility, calls `dune.get_player_pawn` and `dune.update_death_location(..., 'Alive')`, then requires persisted `Alive` with no death location before commit. It produces a private receipt and requires relog but no restart. Exact semantics, rollback, metrics, API examples, and the networkless disposable Dead/Alive proof are documented in [offline-player-life-state-recovery.md](offline-player-life-state-recovery.md).

## Spice and Resource Inspection

`POST /api/admin/spice-fields/inspect` is read-only. It queries:

- `dune.spicefield_types`
- `dune.spicefield_server_availability`
- grouped `dune.resourcefield_state`
- the current `spiceDeepDesertCaps` typed knob value

Use it before and after Deep Desert cap changes, then validate with:

```sql
select * from dune.spicefield_types order by map, field_kind_id;

select map,dimension_index,field_kind_id,count(*),sum(value_remaining)
from dune.resourcefield_state
group by 1,2,3
order by 1,2,3;
```

## Progression Inspection and Reputation

`POST /api/admin/progression/inspect` is the read-only evidence collector for player progression work. With an `account_id`, it returns current player faction/reputation rows. It also introspects `pg_proc` and `information_schema.columns` for journey, recipe, vehicle, faction, and reputation surfaces.

`POST /api/admin/faction-reputation` plans a faction reputation mutation through `dune.set_player_faction_reputation`. Default dry-run body:

```json
{
  "dry_run": true,
  "account_id": 456,
  "faction_id": 1,
  "amount": 100,
  "mode": "add"
}
```

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED=true`
- `confirm: "WRITE REPUTATION"`

The endpoint checks the live table shape and confirms the server-provided setter before planning or writing. It only recognizes `actor_id`, `faction_id`, and one of `reputation`, `reputation_amount`, `amount`, or `value` as the reputation value column. Confidence is moderate-to-high when `dune.set_player_faction_reputation` is present; still validate execution on disposable/offline character data before routine use.

`POST /api/admin/faction` plans or executes first-party faction changes through `dune.change_player_faction`.

Dry-run body:

```json
{
  "dry_run": true,
  "account_id": 456,
  "faction_id": 1,
  "neutral_faction_id": 3
}
```

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_FACTION_MUTATIONS_ENABLED=true`
- `confirm: "CHANGE FACTION"`
- target player must be offline

The live schema currently has `dune.factions` rows for `1=Atreides`, `2=Harkonnen`, `3=None`, and `4=Smuggler`. Confidence is moderate because the faction-change function is first-party; guild side effects and client refresh behavior still need disposable-character validation.

Faction chat seeding is handled by `scripts/player-presence-announcer.py`. The
game records effective side choice on the player controller, while the faction
chat/UI path can still see the pawn as neutral on self-host. By default the
seeder infers Atreides/Harkonnen only from
`dune.get_player_faction(player_controller_id, 3)`, then uses the first-party
`dune.change_player_faction` function to seed the pawn-side faction row. It can
also bind the online player queue to `chat.faction.<id>` and optionally tune
configured Communinet channel names. Reputation-only inference is available via
`DUNE_PLAYER_PRESENCE_FACTION_CHAT_INFER_FROM_REPUTATION=true`, but keep it off
unless low reputation tutorial/intro rows have been ruled out.

Dry-run backfill:

```bash
scripts/player-presence-announcer.py --seed-faction-chat-backfill --all-players
```

Execution requires the explicit CLI `--execute`, or both
`DUNE_PLAYER_PRESENCE_FACTION_CHAT_SEED_ENABLED=true` and
`DUNE_PLAYER_PRESENCE_FACTION_CHAT_SEED_EXECUTE=true` for the loop path.
Queue binding defaults on through `DUNE_PLAYER_PRESENCE_FACTION_CHAT_BIND_QUEUES=true`;
Communinet channel writes stay off unless
`DUNE_PLAYER_PRESENCE_FACTION_CHAT_TUNE_COMMUNINET=true` and
`DUNE_PLAYER_PRESENCE_FACTION_CHAT_COMMUNINET_CHANNELS` maps faction names to
known channel IDs.

Live validation on 2026-06-02 found the self-host state had controller-side
faction choices but neutral pawn-side faction rows. The dry-run reported 11
safe candidates whose controller faction was already explicit: 10 Atreides and
1 Harkonnen. After a DB backup, the execute backfill repaired those 11 pawn
rows, bound the three online Atreides queues to `chat.faction.1`, and the
follow-up dry-run reported `count=0`. Confidence is high for controller-side
inference and DB repair; confidence is moderate that already-online clients may
need a relog or UI refresh before exposing the tab.

`POST /api/admin/landsraad` plans or executes Landsraad term, reward-tier, and
player-contribution administration. Supported actions are `change-end-time`,
`force-end`, `task-goal`, `term-task-goals`, `reward-tier`, and
`player-contribution`.

Dry-run body:

```json
{
  "dry_run": true,
  "action": "change-end-time",
  "term_id": 1,
  "new_end_time": "2026-05-26 04:55:00",
  "test_term": false
}
```

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED=true`
- `confirm: "WRITE LANDSRAAD"`

The endpoint reads `dune.landsraad_load_current_term()` and recent
`dune.landsraad_decree_term` rows before planning. End-time and reward changes
return compensating rollback bodies; `force-end` is not safely reversible.
Player contribution writes replace the selected player's row and recompute
faction and, when supported, guild totals in the same transaction. Reward and
contribution writes create a database backup first. See
[`player-runtime-actions.md`](player-runtime-actions.md). Confidence is
moderate for proprietary live-server behavior and high that each action is
world-impacting.

`POST /api/admin/respawn-location` plans or executes deletion of one known respawn location by UUID.

Dry-run body:

```json
{
  "dry_run": true,
  "action": "delete",
  "account_id": 456,
  "respawn_id": "0a0556f6-a387-41f2-b613-deacee4e2bd0"
}
```

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED=true`
- `confirm: "DELETE RESPAWN"`
- target player must be offline

The endpoint verifies the UUID against `dune.player_respawn_locations` and then re-saves `dune.get_respawn_locations(account_id)` without that entry through `dune.update_respawn_locations`. Confidence is moderate for deletion mechanics and low for creation/editing, so creation and arbitrary edits remain blocked.

`POST /api/admin/world-state/inspect` reads guild, vehicle, marker, landclaim, recipe, and respawn evidence without writing. It accepts optional `account_id`, `player_id`, and `guild_id`, resolves missing player/guild IDs when possible, and returns matching `pg_proc` functions plus table/column metadata.

`POST /api/admin/guild` plans or executes narrow guild administration. Supported actions are `edit-description`, `promote-member`, and `demote-member`.

Dry-run body:

```json
{
  "dry_run": true,
  "action": "edit-description",
  "guild_id": 789,
  "description": "Updated guild note"
}
```

Role-change dry-run body:

```json
{
  "dry_run": true,
  "action": "promote-member",
  "guild_id": 789,
  "player_id": 123,
  "new_role": 1
}
```

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_GUILD_MUTATIONS_ENABLED=true`
- `confirm: "WRITE GUILD"`

The endpoint uses `dune.edit_guild_description`, `dune.promote_guild_member`, and `dune.demote_guild_member`. Confidence is moderate because the functions are first-party and rollback is a compensating call from the dry-run/audit record. Disband, invite, remove-member, create-guild, and allegiance operations remain blocked.

`POST /api/admin/marker` plans or executes marker deletion. Supported actions are `delete-by-id` and `delete-static-location`.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_MARKER_MUTATIONS_ENABLED=true`
- `confirm: "DELETE MARKERS"`

The endpoint uses `dune.delete_markers_by_id` and `dune.delete_static_location_markers`. Confidence is moderate for deletion mechanics and low for rollback because marker recreation/payload semantics are not fully mapped.

`POST /api/admin/landclaim` plans or executes adding one landclaim segment to a known totem id.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED=true`
- `confirm: "WRITE LANDCLAIM"`

The endpoint uses `dune.get_landclaim_segments` for preflight and `dune.add_landclaim_segment` for execution. Confidence is low-to-moderate because the local table is empty and no delete-segment rollback function is mapped.

`POST /api/admin/economy/inspect` reads Dune Exchange, vehicle, recovered/backup vehicle, and base-backup evidence without writing. It accepts optional `account_id`, `player_id`, `controller_id`, and `exchange_id`, and returns matching `pg_proc` functions plus table/column metadata.

`POST /api/admin/exchange` plans or executes Dune Exchange Solari balance changes. Supported modes are `add` and `set`.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED=true`
- `confirm: "WRITE EXCHANGE"`

The endpoint reads the visible Solaris row from `dune.player_virtual_currency_balances`, ensures the Exchange user row with `dune.dune_exchange_get_user_id`, then writes both `player_virtual_currency_balances.balance` and `dune_exchange_users.solari_balance`. Do not use `dune.dune_exchange_modify_user_solari_balance` for operator grants; that first-party function transfers up to the player's current wallet Solaris into Exchange/bank and subtracts the same amount from `player_virtual_currency_balances`. Confidence is moderate for direct balance adjustment and low for order operations, so add/fulfill/cancel/relist/retrieve/purge order functions remain blocked.

`POST /api/admin/solari/inventory` grants Solari to a player's carried inventory as a fresh `SolarisCoin` item stack in a free slot. It accepts `inventory_id` directly, or resolves a player inventory from `account_id` / `character_name`.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `confirm: "GRANT SOLARI"`

`POST /api/admin/solari/bank` grants Solari to the player's visible Solaris balance by resolving the controller/owner id, ensuring the Exchange user row, and setting both `dune.player_virtual_currency_balances` currency `dune.get_solaris_id()` and `dune.dune_exchange_users.solari_balance`. Verify both rows.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED=true`
- `confirm: "WRITE EXCHANGE"`

### Reproducible Item Grants

Use `scripts/admin-grant-item.py` when an operator needs a repeatable backend item grant outside the browser. It resolves reviewed display names through `config/artificial-exchange-prices.csv`, prints a dry-run plan by default, and only writes when `--execute --confirm "GRANT ITEM"` are both present.

Examples:

```bash
./scripts/admin-grant-item.py "Complex Machinery" 2 --character Lukano
./scripts/admin-grant-item.py "Complex Machinery" 2 --character Lukano --execute --confirm "GRANT ITEM"
```

Known machinery labels:

| Display label | Server template ID | Confidence |
| --- | --- | --- |
| Complex Machinery | `T2MachineComponent` | high |
| Advanced Machinery | `T6Machinery` | high |

The script targets carried inventory type `0` by default, chooses a free slot, and calls `dune.save_item(dune.inventoryitem)`. Use `--inventory-id` for an exact container, `--inventory-type` for a different resolved inventory type, or `--position` for an exact free slot. Live grants are additive: each execution creates a new stack and does not declare the player's full inventory state.

For permission-aware base-box discovery, marker-stack identification, exact
container dry-runs, execution, verification, equipment-stat caveats, and the
Exchange-bank distinction, use
[`base-storage-item-grants.md`](base-storage-item-grants.md).

`POST /api/admin/player-lifecycle/inspect` reads account/player, party, tag, access-code, Communinet, dungeon, tutorial, and lifecycle evidence without writing.

`POST /api/admin/player-tags` plans or executes player tag add/remove calls through `dune.update_player_tags`.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED=true`
- `confirm: "WRITE PLAYER TAGS"`

Confidence is moderate. Rollback is the inverse add/remove set from the dry-run/audit record.

`POST /api/admin/access-code` plans or executes server player access-code create/delete/reset calls.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED=true`
- `confirm: "WRITE ACCESS CODES"`

The endpoint uses `dune.get_player_access_codes`, `dune.create_server_player_access_codes`, `dune.delete_server_player_access_codes`, and `dune.reset_server_all_player_access_codes`. Confidence is moderate for function mechanics and high for operational risk because reset needs manual recreation from audit data.

## Character Slots

Character slot tooling is admin-only and inspect/plan-first. It is built for three operator intents:

- `new-character`: hibernate the current active character and let the game run its native character creator on next login.
- `switch-character`: make a previously hibernated, same-owner character active again.
- `restore-character`: same safety model as switch, used when returning to a prior hibernated character.

Endpoints:

```text
GET /api/admin/character-slots?account_id=456
POST /api/admin/character-slots/plan
POST /api/admin/character-slots/execute
```

`GET /api/admin/character-slots` returns:

- `activeCharacter`: the current `dune.player_state` row joined to account identity fields.
- `offline`: whether the active character is offline.
- `candidates`: same-owner native candidate characters matched through account user/Funcom/platform identity.
- `contract`: discovered lifecycle functions and identity table metadata.
- `executionGate`: `DUNE_ADMIN_CHARACTER_SWAP_ENABLED`.
- `confirm`: `SWAP CHARACTER`.

Executable switch/restore plans include `plan.transactionSafety`, which records
that execution creates a backup before the transaction, takes account-id
advisory locks, locks both underlying `encrypted_player_state` and
`encrypted_accounts` rows, rechecks offline state inside the transaction, and
requires post-swap identity verification before commit.

Safe preview examples:

```json
{
  "dry_run": true,
  "account_id": 456,
  "action": "new-character"
}
```

```json
{
  "dry_run": true,
  "account_id": 456,
  "action": "switch-character",
  "target_account_id": 789
}
```

Safe HTTP smoke checks:

```bash
curl -sS "http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/admin/character-slots?account_id=456" \
  | jq '.accountId, .offline, .candidates, .contract.safeNativeSwapPath'

curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/admin/character-slots/plan \
  -d '{"dry_run":true,"account_id":456,"action":"new-character"}' \
  | jq '.dryRun, .executable, .plan.blockers'
```

CLI workflow:

Find accounts that actually have same-owner slot candidates:

```bash
python3 scripts/character-slot-tool.py \
  --action scan \
  --summary \
  --pretty
```

```bash
python3 scripts/character-slot-tool.py \
  --account-id 456 \
  --action inspect \
  --summary \
  --pretty
```

```bash
python3 scripts/character-slot-tool.py \
  --account-id 456 \
  --action switch-character \
  --target-account-id 789 \
  --summary \
  --pretty
```

```bash
python3 scripts/character-slot-tool.py \
  --account-id 456 \
  --action switch-character \
  --target-account-id 789 \
  --execute \
  --confirm "SWAP CHARACTER" \
  --pretty
```

The CLI reads `DUNE_ADMIN_HOST_PORT` and `DUNE_ADMIN_TOKEN` from `.env` by
default. Use `--base-url` or `--token` to override them.

Execution remains blocked unless the plan reports `executable: true`. Current contract discovery treats `dune.takeover_account(in_user_to_takeover text, in_current_user text)` as the only executable native switch/restore path. It swaps the active login identity onto the selected same-owner hibernated character and moves that target identity onto the current character account. Confidence: moderate.

`new-character` execution remains blocked. The mapped native blank-character path is `delete_account(in_user_id, in_reason)`, and that function deletes the current account/actors instead of hibernating them. DASH does not use it for character-slot creation. Confidence: high.

Non-dry-run execution requires all of:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_CHARACTER_SWAP_ENABLED=true`
- `confirm: "SWAP CHARACTER"`
- the active account and selected target are offline
- the action is `switch-character` or `restore-character`
- the plan returns `executable: true`
- a DB backup can be created before any native lifecycle call

Execution behavior:

- creates a Postgres backup first
- opens one DB transaction, takes account-id advisory locks, locks both underlying `encrypted_player_state` and `encrypted_accounts` rows, and aborts before mutation if either came online
- audits before/after rows for the active and target account ids
- calls only `select dune.takeover_account(target_fls_id, active_fls_id)`
- verifies that the active and target FLS identities swapped as expected
- returns an inverse restore payload and backup path

Blocked behavior is intentional. The panel does not create synthetic starter rows, does not overwrite raw `player_state`, and does not execute account deletion or save-player functions as a guessed swap mechanism.

Test coverage includes direct planner tests and handler-route tests for `GET /api/admin/character-slots`, `POST /api/admin/character-slots/plan`, and dry-run/blocked `POST /api/admin/character-slots/execute`. The tests verify that blocked execution does not call SQL write helpers or create a backup.

`POST /api/admin/communinet` plans or executes Communinet active/selected-channel and per-channel tuned state.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED=true`
- `confirm: "WRITE COMMUNINET"`

The endpoint uses `dune.load_communinet_player_data`, `dune.update_communinet_player_data`, `dune.update_communinet_player_channel`, and `dune.remove_communinet_player_channel`. Confidence is moderate. Vendor stock, tutorial, lore, dungeon, overmap, and Coriolis writes remain blocked.

`POST /api/admin/tutorial` plans or executes one tutorial state update for a player through `dune.create_or_update_tutorial_entry`.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED=true`
- `confirm: "WRITE TUTORIAL"`

Confidence is moderate. Bulk tutorial deletion and tutorial registration remain blocked.

`POST /api/admin/permission` plans or executes permission actor name/access/rank changes.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED=true`
- `confirm: "WRITE PERMISSION"`

The endpoint uses `dune.permission_set_name`, `dune.permission_set_access_level`, `dune.permission_set_player_rank`, and `dune.permission_remove_player_rank`. Confidence is moderate mechanically and high risk operationally because these calls affect base/actor access.

`POST /api/admin/vendor` plans or executes one vendor/player stock-cycle timestamp update.

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED=true`
- `confirm: "WRITE VENDOR"`

The endpoint uses `dune.update_vendor_timestamp_for_player` and reads `dune.interact_get_vendor_items_bought_from_player` for dry-run context. Confidence is moderate for timestamp mechanics. Purchase-count and stock-cleanup writes remain blocked.

`POST /api/admin/journey` plans or executes journey story-node server functions. Supported actions are `reveal`, `complete`, `reset`, and `delete`.

Dry-run body:

```json
{
  "dry_run": true,
  "account_id": 456,
  "action": "reveal",
  "story_node_ids": ["ExampleStoryNode"]
}
```

Execution requires:

- `DUNE_ADMIN_MUTATIONS_ENABLED=true`
- `DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED=true`
- `confirm: "WRITE JOURNEY"`
- target player must be offline

The endpoint uses `dune.admin_get_journey_details` for preflight details and then calls the matching server function: `reveal_journey_story_nodes_for_player`, `complete_journey_story_nodes_for_player`, `reset_journey_story_nodes_for_player`, or `delete_journey_story_nodes_for_player`. Confidence is moderate because the functions are first-party DB functions; story-node IDs and reward semantics still need a local reference catalog.

## Event Orchestrator

Events are persisted in:

```text
backups/admin-panel/events.json
```

Supported action types:

| Type | Behavior |
| --- | --- |
| `announcement` | Plans a call to `/api/ops/announcement`. |
| `restart` | Plans a restart schedule with execution disabled in the action payload. |
| `typed-knob-plan` | Plans typed knob updates only. |
| `economy-bundle` | Plans an economy bundle with `dry_run=true`. |
| `spice-cap-proposal` | Plans Deep Desert spice cap typed knob changes. |

Dry-run example:

```json
{
  "name": "Deep Desert spice proposal",
  "actions": [
    {
      "type": "spice-cap-proposal",
      "caps": {
        "Medium": {"primed": 24, "active": 24},
        "Large": {"primed": 3, "active": 3}
      }
    }
  ]
}
```

`POST /api/events/run` remains blocked unless `DUNE_ADMIN_EVENT_EXECUTION_ENABLED=true`. In the current v1 implementation, dry-run-only actions still do not perform underlying writes during event execution; they record the planned action and tell the operator which dedicated endpoint to use. Confidence: high that this is fail-closed, moderate that it is sufficient for real event automation without a later worker loop.

## Blocked in v1

The admin expansion does not implement:

- Native GM command execution.
- True new maps, cooked assets, physics, or algorithms.
- Raw recipe/journey/vehicle DB grants.
- Coriolis wipe/cycle mutation controls.
- Ordinary resource-node respawn mutation beyond documented config candidates.

Those areas need stronger evidence, safe DB functions, or cooked asset/plugin/binary work before they should move out of the catalog.

## Regression Tests

The safe-surface expansion has a focused test target:

```bash
python3 scripts/test-admin-panel-safe-surfaces.py
```

It runs under a temporary `ADMIN_WORKSPACE`, so it does not edit the live `.env`, `config/`, or `backups/` paths. `make validate` runs this test after the existing map-watch tests.

Admin chat and private player-message routing have a separate focused target:

```bash
make test-admin-chat
```

That target runs the chat-command parser/reply tests plus the player-presence private whisper routing test.

Current coverage:

- Catalog schema and groups.
- Catalog evidence and validation payloads.
- Catalog disabled-gate refusal.
- Read-only inspector mutator metadata for progression, world state, economy, player lifecycle, and spice fields.
- Typed knob value validation.
- Typed config writes with backups in a temporary workspace.
- Deep Desert spice cap rendering from structured JSON.
- Event dry-run planning.
- Event persistence and cancellation.
- Event execution blocked by default.
- Character-slot discovery, dry-run behavior, online-player refusal, and missing native-contract blockers.
- Dry-run planning and fail-closed gates for promoted mutator families.

## Base Recovery Reminders

The Admin Digests page includes the persistent Base Recovery Reminders panel.
It reads and writes the same registry used by
[`scripts/base-recovery-reminder.py`](../scripts/base-recovery-reminder.py):

```text
backups/admin-bot/base-recovery-reminders.json
```

Each row stores `accountId`, `backupId`, `totemId`, and the private message.
The player-presence worker records `active`, `restoredAt`, `lastStatus`,
`lastStatusCheckedAt`, `lastSentAt`, and the online/player-name marker in
`backups/admin-bot/player-presence.json`. Restore is deliberately conservative:
the BRT backup must be absent, the totem must still exist, the target account
must have an active permission rank on it, and no linked actor may remain
marked `BaseBackup`. Query failure therefore leaves the reminder active.

The read endpoint is authenticated and read-only:

```http
GET /api/admin/base-recovery-reminders
```

Add/replace and removal use the same authenticated endpoint. Both require
`DUNE_ADMIN_MUTATIONS_ENABLED=true` and
`DUNE_ADMIN_BASE_RECOVERY_REMINDER_MUTATIONS_ENABLED=true`; removal also
requires the exact `REMOVE BASE RECOVERY REMINDER` confirmation phrase:

```json
{
  "action": "upsert",
  "accountId": 5247,
  "backupId": 127,
  "totemId": 22323,
  "message": "Contact a server admin to recover your preserved base."
}
```

```json
{
  "action": "remove",
  "accountId": 5247,
  "confirm": "REMOVE BASE RECOVERY REMINDER"
}
```

Every write is included in the authenticated audit path. Removing a reminder
only stops future messages; it does not delete or modify the player, base,
native BRT backup, inventory, or credits.

## Vehicle Retirement and Recovery Reminders

The Admin Panel exposes **Vehicle Retirement** at
`GET/POST /api/admin/vehicle-retirement`. It is a preview-bound wrapper around
the native multi-vehicle
`dune.store_recovered_vehicles_wiped_before_spawn(bigint[],recoveredvehiclereason,boolean)` contract. The scan shows vehicle class, rank-1 owner, map/partition activity, native state, cargo/module counts, and a content fingerprint. Archive requires an explicitly offline owner, a stopped map, a full database dump, advisory/row locks, the unchanged preview fingerprint, and post-call proof of `recovered_vehicles`, `VehicleRecovery`, and removed world permission rows.

The native recovery queue preserves ordinary vehicle inventory by default.
Selecting **Allow inventory wipe** explicitly requests the native `delete_items`
behavior and requires `AND WIPE VEHICLE INVENTORY` in the confirmation. A
private receipt snapshots actor, vehicle, permissions, inventories, items,
actor state, and modules before the native call. The vehicle actors remain
recoverable; the operation does not change player currency or credits. See
[`vehicle-recovery.md`](vehicle-recovery.md).

Admin Digests also includes **Vehicle Recovery Reminders**, backed by
`backups/admin-bot/vehicle-recovery-reminders.json` and the
`scripts/vehicle-recovery-reminder.py` CLI. It shows every account/vehicle
record, conservative restore status across both native backup systems, online
state, and last-send time. Add or replace requires both mutation gates; removal
requires the exact `REMOVE VEHICLE RECOVERY REMINDER` phrase and changes only
future messaging.

## Restart Announcements

The Ops tab includes a restart-announcement scheduler with these restart-time presets:

```text
immediate, 30s, 60s, 5min, 10min, 15min, 30min, 60min, 1hr, 2hr, 3hr, 4hr, 6hr, 12hr
```

The scheduler is real, and in-game delivery is delegated to:

```env
DUNE_ADMIN_ANNOUNCE_COMMAND=/workspace/scripts/announce.sh
```

`scripts/announce.sh` publishes directly to the game RabbitMQ `chat.map` exchange as the local Paul announcer account. This is the only path currently verified in-game. The old `ServiceBroadcast`/JSON-RPC route, RabbitMQ management `publish`, and hand-written AMQP publisher can report success while the client renders nothing.

The stable transport is:

1. The hook binds currently online player queues to the configured `chat.map` routing keys.
2. The hook publishes a `TextChat` payload with bundled `pika`, matching the AMQP property encoding used by real player chat.
3. Paul-origin messages are wrapped as `!!! message !!!` before delivery. Direct/manual hook invocations with `DUNE_ANNOUNCE_JOB_ID=manual` are not wrapped.

Default announcement transport settings:

```env
DUNE_ANNOUNCE_GAME_RMQ_MANAGEMENT_URL=http://game-rmq:15672
DUNE_ANNOUNCE_GAME_RMQ_AMQP_HOST=172.31.240.1
DUNE_ANNOUNCE_GAME_RMQ_AMQP_PORT=31982
DUNE_ANNOUNCE_HOST_AMQP_HOST=172.31.240.1
DUNE_ANNOUNCE_HOST_AMQP_PORT=31982
DUNE_ANNOUNCE_HOST_WORKSPACE=/path/to/DuneAwakeningSelfHost
DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS=true
DUNE_ANNOUNCE_HTTP_TIMEOUT_SECONDS=0.5
DUNE_ANNOUNCE_ALLOW_MANAGEMENT_PUBLISH=false
DUNE_ANNOUNCE_CHAT_USER=A000000000000001
DUNE_ANNOUNCE_CHAT_PASSWORD=<local announcer password>
DUNE_ANNOUNCE_CHAT_FUNCOM_ID=ADMIN#00001
DUNE_ANNOUNCE_CHAT_SPOOF_NAME=Paul
DUNE_ANNOUNCE_CHAT_EXCHANGE=chat.map
DUNE_ANNOUNCE_CHAT_ROUTING_KEYS=HaggaBasin.0,Survival_1.dim_0,<empty>
DUNE_ANNOUNCE_CHAT_CHANNEL=Map
DUNE_ANNOUNCE_CHAT_USE_SPOOF_NAME=true
DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES=true
DUNE_ANNOUNCE_CHAT_ENSURE_ACCOUNT=false
DUNE_ANNOUNCE_CHAT_PLATFORM_ID=PAUL
DUNE_ANNOUNCE_CHAT_PLATFORM_NAME=Paul
```

`DUNE_ANNOUNCE_CHAT_ROUTING_KEYS` is comma-separated. Use `<empty>` for the blank RabbitMQ routing key. Keep the three default routes unless a new build changes chat routing; those are the routes verified with the live client. When `DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES=true`, the hook first lists connected player queues on game RabbitMQ and idempotently binds them to the configured chat routes, then publishes the restart message. The hook reads `/workspace/.env` at delivery time, so route and sender changes are picked up without recreating the admin-panel container.

Do not replace the `pika` publisher with RabbitMQ HTTP publish unless you validate the client visually. The management API can accept messages and return `routed=true` while the Dune client ignores them. The bundled dependency lives under `scripts/vendor/pika` so the admin panel does not need internet access.

Verification command:

```bash
./scripts/verify-announcement.sh 'PAUL ANNOUNCEMENT VERIFY'
```

For a dashboard-origin test, use Ops -> Restart Announcement or POST `/api/ops/announcement`; the visible message should be wrapped, for example `!!! ADMIN Alert f00 !!!`.

Known formatting limits: normal chat does not parse Unreal/HTML-style rich text tags. Tested color syntaxes such as `<#ff4444>`, `<RichColor ...>`, `<Red>`, `<b>`, `^1`, and `&c` render literally or blank the message. Keep automated restart announcements plain ASCII.

The default sender account is created with the game database login function so the client treats it as a real player-shaped identity:

```sql
select *
from dune.login_account('A000000000000001','ADMIN#00001','PAUL','Paul',0,'Paul',0,0)
limit 1;
```

If in-game chat shows an older sender name, the client is resolving the persisted announcer account instead of honoring only `DUNE_ANNOUNCE_CHAT_SPOOF_NAME`. Repair only the synthetic announcer account:

```sql
update dune.encrypted_player_state
set encrypted_character_name = dune.encrypt_user_data('Paul')
where account_id = (select id from dune.encrypted_accounts where "user" = 'A000000000000001');

update dune.encrypted_accounts
set platform_id = 'PAUL', platform_name = 'Paul'
where "user" = 'A000000000000001';
```

The admin panel passes:

- `DUNE_ANNOUNCE_MESSAGE`
- `DUNE_ANNOUNCE_RESTART_AT`
- `DUNE_ANNOUNCE_JOB_ID`

The script also receives the message as its first argument. Delivery attempts and failures are recorded in the admin audit log.

## Chat Commands

`scripts/admin-chat-commands.py` is the DASH chat-command bridge. It listens only for private whispers/PMs sent to Paul that start with `DUNE_CHAT_COMMAND_PREFIX`, resolves the sending account through `dune.accounts.user`, and only accepts commands from configured admins. Map, proximity, party, and guild chat are not command input channels.

Default command settings:

```env
DUNE_CHAT_COMMAND_PREFIX=&
DUNE_CHAT_COMMAND_ADMINS=AdminUser
DUNE_CHAT_COMMAND_ADMIN_FLS_IDS=TEST_FLS_ID
DUNE_CHAT_COMMAND_DRY_RUN=true
DUNE_CHAT_COMMAND_EXECUTE_TELEPORT=false
DUNE_CHAT_COMMAND_ADMIN_PORT=18080
DUNE_CHAT_COMMAND_ADMIN_TIMEOUT_SECONDS=180
DUNE_CHAT_COMMAND_EXECUTE_ONLINE_GM_TELEPORT=false
DUNE_CHAT_COMMAND_EXECUTE_PLAYER_DISCONNECT=false
DUNE_CHAT_COMMAND_AUCTION_ENABLED=false
DUNE_CHAT_COMMAND_AUCTION_BASE_STORAGE_ENABLED=false
DUNE_CHAT_COMMAND_AUCTION_EXCHANGE_ID=2
DUNE_CHAT_COMMAND_AUCTION_ACCESS_POINT_ID=1
DUNE_CHAT_COMMAND_AUCTION_MAX_ORDERS_PER_PLAYER=50
DUNE_CHAT_COMMAND_AUCTION_LISTING_FEE=0
DUNE_CHAT_COMMAND_AUCTION_DURATION_SECONDS=2419200
DUNE_CHAT_COMMAND_AUCTION_CATEGORY_MASK=0
DUNE_CHAT_COMMAND_AUCTION_CATEGORY_DEPTH=0
DUNE_CHAT_COMMAND_AUCTION_CONFIRM_SECONDS=120
DUNE_CHAT_COMMAND_AUCTION_SUGGESTION_MIN_SCORE=0.55
DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_ENABLED=false
DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_LIMIT=50
DUNE_PLAYER_DISCONNECT_COMMAND=RemoveSessionMember
DUNE_PLAYER_DISCONNECT_ALLOW_BATTLEYE=false
DUNE_ADMIN_RESTART_DISCONNECT_WAIT_SECONDS=5
DUNE_GM_COMMAND_PAYLOAD_VERIFIED=false
DUNE_GM_COMMAND_ENVELOPE_MODE=service-message
DUNE_GM_COMMAND_TRANSPORT=amqp
DUNE_GM_COMMAND_EXCHANGE=rpc
DUNE_GM_COMMAND_REPLY_TO=bgdRpc
DUNE_GM_COMMAND_AMQP_HOST=admin-rmq
DUNE_GM_COMMAND_AMQP_PORT=5672
DUNE_GM_COMMAND_AMQP_USER=<admin-rmq command user>
DUNE_GM_COMMAND_AMQP_PASSWORD=<admin-rmq command password>
DUNE_GM_COMMAND_RMQ_URL=http://admin-rmq:15672
DUNE_GM_COMMAND_RMQ_USER=<admin-rmq command user>
DUNE_GM_COMMAND_RMQ_PASSWORD=<admin-rmq command password>
DUNE_CHAT_COMMAND_EXCHANGE=chat.whispers
DUNE_CHAT_COMMAND_EXCHANGES=chat.whispers
DUNE_CHAT_COMMAND_QUEUE=dash_admin_chat_commands
DUNE_CHAT_COMMAND_ROUTING_KEY=ADMIN#00001
DUNE_CHAT_COMMAND_BIND_ROUTING_KEYS=ADMIN#00001
DUNE_CHAT_COMMAND_AMQP_HOST=game-rmq
DUNE_CHAT_COMMAND_AMQP_PORT=5672
DUNE_CHAT_COMMAND_AMQP_TLS=true
DUNE_CHAT_COMMAND_AMQP_RETRY_SECONDS=5
DUNE_CHAT_COMMAND_AMQP_CONNECT_ATTEMPTS=0
DUNE_CHAT_COMMAND_AMQP_USER=guest
DUNE_CHAT_COMMAND_AMQP_PASSWORD=guest
DUNE_CHAT_COMMAND_REPLY_COMMAND=/workspace/scripts/announce.sh
DUNE_CHAT_COMMAND_PRIVATE_REPLIES_ENABLED=true
DUNE_CHAT_COMMAND_PRIVATE_REPLY_EXCHANGE=chat.whispers
DUNE_CHAT_COMMAND_PRIVATE_REPLY_CHANNEL=Whispers
DUNE_CHAT_COMMAND_PRIVATE_REPLY_ROUTING_KEY=
DUNE_CHAT_COMMAND_TARGET_REPLY_MODE=whisper
DUNE_CHAT_COMMAND_TARGET_REPLY_EXCHANGE=chat.proximity
DUNE_CHAT_COMMAND_TARGET_REPLY_CHANNEL=Proximity
DUNE_CHAT_COMMAND_TARGET_REPLY_ROUTING_KEY=
DUNE_CHAT_SPAM_PROTECT_ENABLED=true
DUNE_CHAT_SPAM_SAME_CONSECUTIVE_LIMIT=3
DUNE_CHAT_SPAM_SAME_WINDOW_LIMIT=5
DUNE_CHAT_SPAM_SAME_WINDOW_SECONDS=30
DUNE_CHAT_SPAM_KICK_COMMAND=/workspace/scripts/spam-kick-player.sh --player {character_name} --fls-id {fls_id} --reason {reason} --message {message}
DUNE_SPAM_KICK_BACKEND=blocked
```

Implemented commands:

```text
&test
&list
&inv_list
&where <playername>
&disconnect <playername>
&kick <playername>
&auction [--base|--inventory <inventory_id>] [--item-id <item_id>|"<item name or template>"] <count> <price>
&exchange_list [limit]
&exchange_cashout
&teleport <playername>
&teleport list|locations
&teleport set <slot> [name]
&teleport replace <slot> [name]
&teleport delete|rm <slot>
&teleport <playername> <slot>
&teleport <slot>
&goto <playername>
&bring <playername>
&gm help
&gm routes
&gm mark|marks|unmark|recall
&gm pos|dry
&gm where|goto|bring|unstuck
&gm item|kit|xp
&gm tp|map|travel|dimension|patrol|sandworm|marker|vehicle
&gm fly|ghost|walk
```

`&test` replies with `f00` through the configured announcement/reply path. Use it as the first live smoke test for chat-command ingestion and reply delivery.

`&list` replies to the sender with all currently online players formatted as `Player (Map)`. It also accepts `&players` and `&online`.

`&inv_list` replies to the sender with their personal inventory stacks, including `code=<template_id>` and `item-id=<item_id>` for auction listing. Players can use `&auction --item-id <item-id> <count> <price>` for an exact stack, or `&auction "<item code/template>" <count> <price>` for template matching. Bare `&auction` replies with the syntax and the same inventory list.

`&exchange_list [limit]` replies to the sender with their own active player exchange sales without requiring travel to Arrakeen. It filters orders to the sender's player controller, excludes NPC orders, and excludes fulfilled orders when the fulfilled-order table is present.

`&exchange_cashout` claims the sender's completed seller Solari settlements through the same validated settlement path used by `scripts/artificial-exchange-bot.py`: it locks each completed seller-claim row, credits the sender's Solaris balance by `item_price * stack_size`, deletes the completed claim order, and validates that the order is gone and the credited amount matches. It previews by default; set `DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_ENABLED=true` to execute from chat. The batch size defaults to `DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_LIMIT=50`.

`&where` reports the resolved player's current online/offline state and last
known location. `&teleport <playername>` previews or moves an offline target to
the admin's current partition and location. The chat bridge no longer calls the
database helper directly: it invokes the loopback Admin API for a fresh guarded
preview and, when enabled, an execution bound to that exact fingerprint. The
Admin path performs the dual Offline checks, coordinate bounds, full backup,
locks, native call, transform readback, audit, and private receipt documented
in [offline-player-teleport.md](offline-player-teleport.md). Live actor
transforms are owned by the running map server and can be overwritten, so raw
online actor updates are not a teleport path.

Shared numbered teleport slots live in `backups/admin-panel/teleport-slots.json`. `&teleport set <slot> [name]` saves the issuing admin's current location only when the slot is empty; if occupied, it reports the current slot and next free number. Use `&teleport replace <slot> [name]` to overwrite, `&teleport list` to show slots in numeric order, and `&teleport delete <slot>` to remove a slot. `&teleport <playername> <slot>` moves a strict-offline target to the saved slot through the same guarded Admin contract; `&teleport <slot>` prepares or sends a gated native `TeleportToExact` for the issuing admin.

Recommended city slots are operator-created, not seeded: stand in Arrakeen and run `&teleport set 0 arrakeen`; stand in Harko Village and run `&teleport set 1 harko`.

The verified online-adjacent fallback is network-disconnect teleport: force a real connection timeout, wait until Survival marks the player `Offline`, call `dune.admin_move_offline_player_to_partition(...)`, then let the client reconnect and load the moved pawn. DB-only presence flips are not sufficient. The full contract is in [soft-disconnect-teleport.md](soft-disconnect-teleport.md).

`&auction "<item name or template>" <count> <price>` is a player-facing exchange listing command. It does not require admin allow-list membership, but only operates for the sender's own resolved character. Use `--item-id <item_id>` when the operator already knows the exact item row and wants to bypass fuzzy name/template matching. By default the command previews the listing and does not mutate the database. Set `DUNE_CHAT_COMMAND_AUCTION_ENABLED=true` to execute. The command uses `dune.dune_exchange_add_sell_order(...)`, so the order is owned by the sender's player controller and should use the normal exchange seller settlement path. Confidence: moderate until a live purchase settlement has been validated.

Auction item matching searches the sender's pawn/controller inventories by normalized template/name text and requires a single stack with at least `<count>` items. Use `--base` to search permitted shared base storage, or `--inventory <inventory_id>` to target a specific personal/permitted base inventory. Base and explicit-storage sources require `DUNE_CHAT_COMMAND_AUCTION_BASE_STORAGE_ENABLED=true`; otherwise they are rejected. Split-across-stacks listings are not supported.

If no exact/contains match is found, the command scores allowed inventory items and replies with `did you mean <template>? reply &auction yes or &auction no` when the best score is at least `DUNE_CHAT_COMMAND_AUCTION_SUGGESTION_MIN_SCORE`. The pending suggestion expires after `DUNE_CHAT_COMMAND_AUCTION_CONFIRM_SECONDS`. Confirmation state is held in the running chat listener process, so it works in service mode but not across separate one-shot `--dry-run-command` invocations.

Private command replies are routed through the direct player-queue path documented in [private-chat-replies.md](private-chat-replies.md). The timestamp field is build-sensitive: older testing rendered `m_TimeStamp`, while live player chat on 2026-05-27 used `m_Timestamp`. `scripts/announce.sh` now defaults to `m_TimeStamp` and exposes `DUNE_ANNOUNCE_CHAT_TIMESTAMP_FIELD=m_Timestamp` as the rollback knob. Confidence: moderate until the current build is re-confirmed in-game.

Private-chat investigation found a real `chat.whispers` direct exchange and a required TextRouter redirect header, `redirect_exchange`, whose AMQP value must be bytes, for example `b"chat.whispers"`. The TextRouter binary also confirms private player queue names are `{FLS_ID}_queue` and private RPC queue names are `{FLS_ID}_rpcQueue`.

Current confidence: high for exchange/header/queue naming and RabbitMQ delivery, but only moderate for client-visible private rendering until the current game build is re-confirmed in-game. Direct `chat.whispers` and direct queue publishes can be delivered to RabbitMQ. Operator confirmation showed `fieldfix 204311 timeS-whispers` rendered with `m_TimeStamp`, so the shared publisher defaults to `m_TimeStamp`. Intercepted synthetic whispers with no AMQP `user_id` reach TextRouter but fail `DemoPlayersFilter` with `ArgumentNullException`; intercepted synthetic whispers with `user_id` pass redirect permission checks but TextRouter republishes with the original `user_id` while authenticated as its router account, causing RabbitMQ `PRECONDITION_FAILED`.

Online research on 2026-05-20 did not find a public self-host recipe for injecting client-visible private whispers. GitHub code search found no Dune-specific use of `chat.whispers`, `redirect_exchange`, `send-whisper-probe.py`, or this repo's private-reply toggles. General web/Steam/community-wiki searches found private-chat references but no RabbitMQ/TextRouter implementation details. The useful external signal is official/community patch-note evidence for update `1.3.20.0` on 2026-04-28: one fix says muted players receive a whisper notification, and another says private chat messages could fail to show after opening chat directly into the private tab. That confirms the client has a real private-message display path, and also that display behavior has been buggy. Confidence: high that private chat exists in the product; low that anyone has publicly documented a working self-host injection path.

Deeper local decompilation found a TextRouter launch knob: `--RMQCredentials <username>:<password>`. In normal mode TextRouter authenticates to RabbitMQ as a generated `tr.<battlegroup>.<correlation>` user. Intercepted messages keep the original AMQP `user_id` when TextRouter republishes them, so RabbitMQ rejects the publish when `user_id` differs from the authenticated router user. In fixed-credential mode, TextRouter also enables battlegroup-prefixed exchange names such as `<world>_chat.intercept` and `<world>_chat.whispers`.

Experimental result on 2026-05-20: TextRouter was temporarily restarted with fixed `guest` credentials, a synthetic message was published to the battlegroup-prefixed intercept exchange with AMQP `user_id=guest`, and TextRouter successfully redirected four variants to `chat.whispers` without `PRECONDITION_FAILED`: `prefixed-fixed-tr 203132 Map`, `prefixed-fixed-tr 203132 Whisper`, `prefixed-fixed-tr 203132 Private`, and `prefixed-fixed-tr 203132 Whispers`. Temporary target-queue bindings were removed afterward and TextRouter was restored to its normal compose command. These did not render for the operator, so fixed TextRouter credentials are not required for the working direct-queue path.

Use `scripts/send-whisper-probe.py` for repeatable tests. The probe uses `m_TimeStamp`, matching the production publisher:

```bash
scripts/send-whisper-probe.py --target-name SamplePlayer --target-fls-id TEST_FLS_ID --bind-target-queue --send-intercept --message-prefix "probe $(date +%H%M%S)"
```

Capture a real client whisper with:

```bash
scripts/capture-chat-routing.py --seconds 120 \
  --routing-key '#' \
  --routing-key TEST_FLS_ID \
  --routing-key SamplePlayer \
  --routing-key 36A0226630A5875 \
  --routing-key Talon \
  --routing-key EC8C54C4BB4463D9 \
  --routing-key Xale \
  --routing-key HaggaBasin.0 \
  --routing-key Survival_1.dim_0
```

Then compare the real payload's exchange, routing key, AMQP properties, `redirect_exchange` header, outer `Type`, and inner `m_ChannelType`/`m_UserNameTo` against probe output.

The working private reply path is also documented as a standalone runbook in [private-chat-replies.md](private-chat-replies.md). Keep that file updated when changing `scripts/announce.sh`, `scripts/send-whisper-probe.py`, or chat-command reply routing.

Additional channel probes are available through `scripts/send-chat-channel-probe.py`. This sends labeled `Proximity`, `Guild`, `Faction`, and optional `Party` messages as both direct exchange publishes and TextRouter intercept redirects:

```bash
scripts/send-chat-channel-probe.py --target-name SamplePlayer --target-fls-id TEST_FLS_ID --guild-id 1 --faction-id 3 --message-prefix "chan-native $(date +%H%M%S)"
scripts/send-chat-channel-probe.py --target-name SamplePlayer --target-fls-id TEST_FLS_ID --guild-id 1 --faction-id 3 --bind-target-queue --message-prefix "chan-bound $(date +%H%M%S)"
```

Live findings: the test player's player controller `17` is in guild `1`; no active party row was present during testing. Direct `chat.proximity`, `chat.guild.1`, and `chat.faction.3` publishes with temporary target-queue bindings reached RabbitMQ and were delivered to the player queue, then the bindings were removed. Operator confirmation reported proximity and guild messages rendered in the client. TextRouter intercept redirects without `user_id` fail permission checks; redirects with `user_id` pass permission for `chat.proximity` but hit the same RabbitMQ `PRECONDITION_FAILED` republish issue seen with whispers.

For command replies, set `DUNE_CHAT_COMMAND_TARGET_REPLY_MODE=whisper` and `DUNE_CHAT_COMMAND_PRIVATE_REPLY_CHANNEL=Whispers` to reply through `chat.whispers`. The route is deterministic: `scripts/dune_whisper_route.py` derives routing key `<FLS_ID>` and queue `<FLS_ID>_queue`, so no player-created whisper is required after reboot. Command responses generated inside `handle_command()` infer the issuing player as the target when the sender FLS id is known, so admin command results and errors stay private by default. Returned command JSON includes `reply.stdout` from `scripts/announce.sh`; private replies should show `transport=chat.whispers` and `exchange=chat.whispers`. Set `DUNE_CHAT_COMMAND_TARGET_REPLY_MODE=proximity` to use `chat.proximity`, or set `DUNE_CHAT_COMMAND_TARGET_REPLY_MODE=guild` and `DUNE_CHAT_COMMAND_TARGET_REPLY_EXCHANGE=chat.guild.<id>` to use a confirmed guild exchange. The command listener sets `DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS=true` for these targeted replies so the temporary binding is removed after publish. Spam-protection action announcements are intentionally outside command context and remain global when `DUNE_CHAT_SPAM_ANNOUNCE_ACTION=true`.

The complete private/global split, command smoke tests, expected `reply.stdout` shape, and stale-binding checks live in [private-chat-replies.md](private-chat-replies.md). Run `make test-admin-chat` after changing command reply routing, `scripts/announce.sh`, or player-presence private messaging.

`&disconnect` and its `&kick` alias resolve a target player, route the request to the player's current map, and default to `RemoveSessionMember <playername>`. That is the softest known native session-removal candidate. `KickLobbyMember` can be selected with `DUNE_PLAYER_DISCONNECT_COMMAND=KickLobbyMember` if session removal does not work. `BattlEyeMegaKick` is intentionally excluded unless `DUNE_PLAYER_DISCONNECT_ALLOW_BATTLEYE=true` and `DUNE_PLAYER_DISCONNECT_COMMAND=BattlEyeMegaKick` are set, because it is the most likely option to behave like a punitive kick or retry cooldown.

Targeted disconnect execution has its own gate. It requires all three of `DUNE_ADMIN_GM_COMMANDS_ENABLED=true`, `DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true`, and `DUNE_CHAT_COMMAND_EXECUTE_PLAYER_DISCONNECT=true`. Until those are true, the command returns the exact native payload preview instead of publishing it. The repo also sets the server reconnect grace periods to `0` in `config/UserGame.ini`, so normal disconnects should not leave a long persisted reconnect window.

DB-only soft-disconnect is not a verified game-session disconnect. The verified fallback is network-disconnect teleport: force a real timeout, wait for `Offline`, run the offline move helper, then let reconnect load the moved pawn. Keep it behind its own mutation gate and audit trail; do not mix it with unverified native kick gates.

`&goto` and `&bring` are wired through the native GM command adapter for online movement, but execution remains gated until the command payload is proven. `&goto <playername>` prepares `TeleportToPlayer <playername>` targeted at the admin; `&bring <playername>` prepares `TeleportToExact <admin-x> <admin-y> <admin-z>` targeted at the online player. The three required gates are `DUNE_ADMIN_GM_COMMANDS_ENABLED=true`, `DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true`, and `DUNE_CHAT_COMMAND_EXECUTE_ONLINE_GM_TELEPORT=true`. Until then, the commands return the exact payload preview instead of publishing a live teleport.

Online movement has additional live-test guards. `DUNE_CHAT_COMMAND_ONLINE_GM_TELEPORT_REQUIRE_SAME_ROUTE=true` by default, so the admin and target must both be online and resolve to the same GM route before a publish is attempted. `DUNE_CHAT_COMMAND_ONLINE_GM_TELEPORT_REQUIRE_ARM=true` by default, so live movement also requires a short-lived, one-use arm command:

```text
&armgoto <playername>
&goto <playername>

&armbring <playername>
&bring <playername>
```

The arm expires after `DUNE_CHAT_COMMAND_ONLINE_GM_TELEPORT_ARM_SECONDS`, default `60`. This keeps first live tests limited to same-map/same-partition movement and prevents an old preview or repeated chat command from publishing unexpectedly.

`&gm` is the richer native-command namespace. It supports safe probes, saved admin marks, movement/travel previews, player help, item/kit previews, XP mutation request previews, and movement-mode previews. All native publishes remain gated by the same three GM flags; dangerous building/placeable destroy commands are intentionally not wired as chat shortcuts.

### Chat Spam Auto-Protection

The chat-command listener also watches all intercepted chat messages for repeated-message spam. It is enabled by default with these rules:

- More than `3` identical messages in a row from the same player triggers enforcement.
- `5` identical messages within `30` seconds from the same player triggers enforcement.
- Admins are exempt by default through `DUNE_CHAT_SPAM_PROTECT_EXEMPT_ADMINS=true`.
- A cooldown prevents repeated enforcement on the same player for `300` seconds.

The detector normalizes whitespace and case before comparing messages. On violation it runs `DUNE_CHAT_SPAM_KICK_COMMAND` and announces the result. The default hook is `scripts/spam-kick-player.sh`, which fails closed with `DUNE_SPAM_KICK_BACKEND=blocked` until a real kick backend is configured. Because targeted disconnect depends on the native GM command route being verified for the current server build, violations are logged and announced as blocked by default instead of silently doing unsafe state writes.

Useful knobs:

```env
DUNE_CHAT_SPAM_PROTECT_ENABLED=true
DUNE_CHAT_SPAM_PROTECT_EXEMPT_ADMINS=true
DUNE_CHAT_SPAM_SAME_CONSECUTIVE_LIMIT=3
DUNE_CHAT_SPAM_SAME_WINDOW_LIMIT=5
DUNE_CHAT_SPAM_SAME_WINDOW_SECONDS=30
DUNE_CHAT_SPAM_KICK_COOLDOWN_SECONDS=300
DUNE_CHAT_SPAM_KICK_COMMAND=/workspace/scripts/spam-kick-player.sh --player {character_name} --fls-id {fls_id} --reason {reason} --message {message}
DUNE_SPAM_KICK_BACKEND=blocked
DUNE_SPAM_KICK_BACKEND_COMMAND=
DUNE_CHAT_SPAM_EXEMPT_NAMES=
DUNE_CHAT_SPAM_EXEMPT_FLS_IDS=
```

`DUNE_CHAT_SPAM_KICK_COMMAND` is parsed with shell-style quoting and supports these placeholders: `{character_name}`, `{player}`, `{fls_id}`, `{reason}`, and `{message}`.

`scripts/spam-kick-player.sh` supports `DUNE_SPAM_KICK_BACKEND=blocked` and `DUNE_SPAM_KICK_BACKEND=command`. The `command` backend delegates to `DUNE_SPAM_KICK_BACKEND_COMMAND` with `--player`, `--fls-id`, `--reason`, and `--message`; use it only after a targeted disconnect primitive is proven.

Teleport starts in dry-run mode. To apply the movement write, set:

```env
DUNE_CHAT_COMMAND_DRY_RUN=false
DUNE_CHAT_COMMAND_EXECUTE_TELEPORT=true
DUNE_ADMIN_MUTATIONS_ENABLED=true
DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED=true
```

`DUNE_CHAT_COMMAND_ADMIN_PORT` must point to the loopback-bound Admin host port
and defaults to `DUNE_ADMIN_HOST_PORT`. The bridge refuses a missing Admin
token, non-loopback transport, blocked preview, stale fingerprint, execution
without a verified receipt, and any high-risk approval response that has not
actually executed. Under dual control, rerun the same command after the exact
request receives approval and while the preview evidence remains unchanged.

Dry-run verification from the live admin container:

```bash
docker compose exec -T admin-chat-commands /workspace/scripts/admin-chat-commands.py \
  --dry-run-command '&teleport Cletus' \
  --sender-name SamplePlayer \
  --sender-fls-id TEST_FLS_ID
```

The listener has been validated to bind `chat.intercept` from the admin container path. If that regresses, first check that `/workspace/.env` contains the current `DUNE_ANNOUNCE_*` and `DUNE_CHAT_COMMAND_*` credentials; stale container environment was the original blocker.

Persistent listener service:

```bash
docker compose up -d --no-deps admin-chat-commands
docker compose logs -f admin-chat-commands
docker compose ps admin-chat-commands
docker compose exec -T admin-chat-commands /workspace/scripts/admin-chat-commands.py --healthcheck
```

The listener service is separate from the web panel so a command-loop failure does not take down the admin panel hostname. It uses `restart: unless-stopped` and has a Docker healthcheck for the command queue consumer. Compose injects the settings it needs; the listener does not mount the full `.env` file or Docker socket. Its root filesystem is read-only, all Linux capabilities are dropped, and `no-new-privileges` is enabled; only the backup volume and `/tmp` tmpfs are writable. Replies use `scripts/announce_pika.py` directly instead of starting a temporary Docker publisher. The service runs on host networking and uses the host-published Postgres/RabbitMQ ports for command ingestion and replies, so Paul can recover even when the compose bridge path is degraded. Credentials are still process environment variables and remain visible in Docker container metadata.

## Scheduled Restarts And Shutdowns

The Infrastructure page also owns a separate automatic full-backup schedule.
It snapshots every enabled durable store, writes and verifies a coverage
declaration, shares `backups/admin-panel/operation.lock` with host backups and
assured deployments, and never invokes map lifecycle. Lock collisions defer;
creation/coverage/verification failures retain bounded diagnostics and use the
configured short retry. Only successful schedule-owned paths are eligible for
its retention policy. See [`automatic-backups.md`](automatic-backups.md).

Operations combines that schedule with active restart jobs, recurring Event
Automation work, map prewarming, and Operational SLO maintenance exclusions in
a bounded conflict-aware calendar. Executing restart admission fails closed on
critical overlaps unless an API caller provides the exact exceptional override;
the browser never supplies it. The override cannot bypass runtime serialization.
Queued jobs can be superseded or cancelled, but an `executing` or
`awaiting_reboot` job cannot be replaced while its worker owns the outcome.
See [`operations-calendar.md`](operations-calendar.md) for the conflict table,
API, metrics, alerts, configuration, and operator runbook.

The same tab exposes `GET /api/ops/alerts` and its guarded acknowledgement
POST. It separates collector failure from active source state and never
presents acknowledgement as resolution. See
[`alert-inbox.md`](alert-inbox.md).

The Ops tab can also schedule restart or shutdown jobs for restart-safe components, the service layer, all game maps, or key individual maps such as Survival, Overmap, Arrakeen, Harko Village, and Deep Desert. It does not stop or restart Postgres or RabbitMQ by default because replacing those services disconnects all running map servers. It also does not include `admin-panel` in the admin-triggered `all` target, because stopping the container running the scheduler would interrupt the stop-backup-update-start workflow.

Scheduled maintenance defaults to dry-run mode. In dry-run mode, the job matures, records that it would have run, and does not touch containers. Executed restart jobs now use a stop-backup-update-start sequence: stop the selected game services, take the maintenance backup while they are down, check the local Steam package for a newer Funcom image tag, then start/recreate the selected services. Executed shutdown jobs stop the selected services, take the maintenance backup, run the same Steam-package update check, and leave them stopped. If the stop step fails, no backup, update check, or start is attempted. If the backup step fails during a restart, the failure is recorded as a warning and the selected services are still started so a backup issue does not strand the farm offline.

An executing job owns `backups/admin-panel/operation.lock` across preflight,
player disconnect, stop, backup, update, start, recovery, and online proof. If a
backup or assured deployment already owns the lock, the job is returned to
`scheduled` for a bounded retry without disconnecting a player, invoking a
service command, or creating a failed maintenance receipt.

Maintenance announcements automatically append the live time remaining, or fill `{remaining}` / `{time_remaining}` if either token appears in the configured message. At the zero mark, the announcement worker sends a final "starting now" notice before execution begins. Announcement jobs also support an optional cadence list, using `remaining_seconds` and `interval_seconds` entries, so unattended maintenance can announce every 5 minutes during the 30-minute warning window and every 1 minute during the final 5 minutes.

Before an executed restart or shutdown stops containers, the admin panel resolves the online players affected by the target, publishes the configured soft-disconnect command to each player's current map route, waits `DUNE_ADMIN_RESTART_DISCONNECT_WAIT_SECONDS` seconds after all publishes complete, and only then starts the stop phase. If online players are present and the targeted-disconnect gates are not enabled, the restart/shutdown fails closed and does not stop services.

Maintenance backups are written under `backups/admin-panel/maintenance/<utc-stamp>-<job-id>/`. Each backup includes a unique Postgres custom-format dump, config/env archive, and mounted `data/server-saved` / `data/rabbitmq` archives when those paths are available to the admin container. Each backup also writes `postgres-layers.json`, which records primary streaming-replication slots and active senders at the stopped-world backup point. If `POSTGRES_REMOTE_REPLICA_HOST` is configured and `DUNE_ADMIN_MAINTENANCE_REPLICA_SNAPSHOT_ENABLED=true` (default), the backup attempts `scripts/replica-snapshot.sh` so the remote standby also gets a rolling logical snapshot. Replica status and remote snapshot failures are recorded as warnings in `manifest.json`; they do not replace or block the authoritative local stopped-world dump.

After a restart start hook returns, the admin panel waits for farm DB readiness before marking the job successful. With autoscaling disabled, the gate requires every current `world_partition` row to have an alive farm row and an `active_server_ids` entry. With autoscaling enabled, it requires only maps whose configured mode is `always-on`; dynamic, warm, and retained maps may correctly remain stopped until demand. The execution details identify the decision as `verificationMode=full-farm` or `verificationMode=autoscaler-always-on`, include the exact `requiredServices`, and record the stricter ready/alive count as `readyOnline`. If the autoscaler state cannot be read or has no `always-on` services, verification fails safe to the full farm. If readiness is incomplete, the panel runs one guarded recovery pass through `DUNE_ADMIN_RESTART_RECOVERY_COMMAND` before waiting again. The default recovery command is `scripts/watch-maps.sh .env --once` with startup grace disabled, so a required map that is running but not alive/active in the DB is recovered instead of leaving the baseline farm unhealthy. A start hook return code of `141` is treated as recoverable only if the required farm subset subsequently reports fully online, because that code commonly means a broken output pipe rather than a confirmed container startup failure.

When the restart form's announcement checkbox is enabled, relative-delay jobs
schedule the first warning immediately. Exact-time jobs default to beginning
warnings 30 minutes before execution, every five minutes until five minutes
remain, then every minute. The standalone announcement card is separate: it
only schedules chat notices and does not stop, start, or back up services.

The Ops tab also provides a player-impact maintenance planner. It records
zero-inclusive five-minute population buckets with no identities, ranks exact
future windows against the `06:00` baseline, and loads a selected timestamp into
the normal restart form. Learning/fallback mode remains visible until complete
historical windows satisfy policy. See
[`player-impact-maintenance.md`](player-impact-maintenance.md).

Actual execution is delegated to:

```env
DUNE_ADMIN_RESTART_COMMAND=/workspace/scripts/restart-target.sh
```

`scripts/restart-target.sh` first uses Docker Compose when the Docker CLI is available. Inside the admin-panel container it falls back to the mounted Docker Engine socket and operates on containers by Compose labels:

```env
DUNE_RESTART_COMPOSE_PROJECT=dune_server
DUNE_RESTART_DOCKER_SOCKET=/var/run/docker.sock
DUNE_RESTART_HOST_WORKSPACE=/path/to/DuneAwakeningSelfHost
DUNE_RESTART_COMPOSE_IMAGE=docker:27.5.1-cli
DUNE_RESTART_USE_HOST_COMPOSE=true
DUNE_RESTART_COMPOSE_TIMEOUT_SECONDS=1800
DUNE_RESTART_DOCKER_STOP_TIMEOUT_SECONDS=120
DUNE_RESTART_DOCKER_API_TIMEOUT_SECONDS=30
DUNE_ADMIN_RESTART_RECOVERY_ENABLED=true
DUNE_ADMIN_RESTART_RECOVERY_COMMAND=/workspace/scripts/watch-maps.sh
DUNE_ADMIN_RESTART_RECOVERY_TIMEOUT_SECONDS=900
```

The Docker socket is privileged host control. Keep the admin panel bound to localhost or a trusted reverse proxy, and do not expose the admin hostname publicly. If the panel is reachable beyond a trusted local admin surface, enable `DUNE_ADMIN_REQUIRE_TOKEN=true`. The script receives `DUNE_RESTART_JOB_ID`, `DUNE_RESTART_TARGET`, `DUNE_RESTART_SERVICES`, and `DUNE_RESTART_ACTION`, plus the target as its first argument.

For admin-triggered restart jobs, the stop phase uses Docker stop, and the start phase uses Compose `up -d --force-recreate --no-deps`. When the admin image has no Docker CLI, the socket fallback starts a short-lived `docker:27.5.1-cli` helper with the repo and Docker socket mounted, then runs host-side Compose from that helper. The helper drops all capabilities except `DAC_OVERRIDE` for owner-only workspace access, enables `no-new-privileges`, and does not share the host PID namespace. It retains host networking and UTS so the production-host guard and router SSH checks work. The Docker socket still grants host-level Docker control. It also runs `scripts/seed-gateway-neighbor.sh` before and after recreate so gateway's static `172.31.240.40`/`02:42:ac:1f:f0:28` identity is refreshed in Postgres, gateway, RabbitMQ, service-layer, and host bridge neighbor tables. After recreate and seeding, `scripts/restart-post-start-health.sh` waits for Postgres connectivity, re-seeds bridge neighbors, recreates `text-router` if it exited during early startup, and retries `scripts/verify-rmq-auth-path.sh` until the auth and text-router paths are reachable or the timeout expires. This catches the broken state where `admin-rmq` cannot reach `rmq-auth-shim`, `game-rmq` cannot reach `text-router`, or `text-router` exits because it raced Postgres during startup. This means the daily restart schedule applies changed `.env` values and bind-mounted config files during the recreate phase without pulling in excluded dependencies such as Postgres or RabbitMQ. Keep `DUNE_RESTART_COMPOSE_IMAGE` available locally on the Docker host; if it is missing, pull it before relying on unattended maintenance. The socket fallback gives stop/restart calls a longer timeout than Docker's graceful stop window so a normal slow shutdown is not misreported as a failed maintenance job.

The host-side daily scheduler uses `scripts/schedule-daily-maintenance.sh`. Run it from cron or systemd before the desired maintenance time; the deployed schedule should call it at 05:30 and create an executed, backed-up, announced `all` restart for 06:00, after Funcom's nightly maintenance window. Install the provided timer with `./scripts/install-daily-maintenance-timer.sh .env`. See [`docs/maintenance-updates.md`](maintenance-updates.md) for the timeline, cadence, and Steam-package update logic.

`scripts/restart-target.sh` refuses to stop or restart `postgres`, `admin-rmq`, or `game-rmq` unless `DUNE_RESTART_ALLOW_STATEFUL=true` is set for a deliberate maintenance window. If Postgres must be restarted, expect all game maps to need recovery afterward.

## Write Safety

Mutation endpoints are disabled unless:

```env
DUNE_ADMIN_MUTATIONS_ENABLED=true
```

Current mutation support is intentionally narrow:

- Currency balance add/set through `dune.player_virtual_currency_balances`.
- Inventory Solari grants through `dune.save_item(dune.inventoryitem)` against `SolarisCoin` item stacks.
- Exchange/bank Solari grants through direct visible Solaris row updates in `dune.player_virtual_currency_balances`, mirrored to `dune.dune_exchange_users.solari_balance` after `dune.dune_exchange_get_user_id`.
- Specialization XP add/set through existing `dune.specialization_tracks` rows.
- Database backup through `pg_dump -Fc`.
- Item grants through `dune.save_item(dune.inventoryitem)` when `DUNE_ADMIN_ITEM_GRANTS_ENABLED=true`.
- Item grant dry-runs that resolve the target inventory and warnings without requiring `DUNE_ADMIN_MUTATIONS_ENABLED=true`.
- Item stack changes and item/count deletion through the server's existing item functions.
- Solido blueprint list/export/import/delete through the **Blueprints** page; import/delete use separate gates, backups, offline-owner checks, transaction verification, and downloadable rollback archives. See [`blueprints.md`](blueprints.md).
- Structured compatible augments for new grants and existing owned items; execution unlocks the required augment slots in the same transaction. See [`augments.md`](augments.md).

Item grants require an exact server `template_id`. You can enter an inventory ID directly, or let the panel resolve a player-owned inventory from account ID or character name. The mutation page now offers exact local template IDs from item, Landsraad reward, vendor, vehicle, and exchange tables. Public databases such as `https://dune.gaming.tools/items` and `https://dune.geno.gg/items/` are still useful for names and research, but dry-run and verify against local server data before bulk grants.

Recipe and research unlocks are available under Admin Actions. They validate
keys against data observed in the active database, require an Offline player,
create a database backup, lock and compare-and-swap the complete actor JSON,
verify the affected state after the write, and create a self-hashed receipt.
The same panel can preview and execute a receipt-bound rollback; rollback is
refused if the player is Online or the affected state has changed since the
receipt. See `docs/player-progression-receipts.md`.

See `docs/admin-mutation-map.md` for the current DB contract map.

Back up before enabling mutations:

```bash
./scripts/backup-state.sh .env
```

## Logout Timers

Steam Deck suspend and quick logout behavior is controlled by the typed Settings -> Logout and Reconnect Timers editor. It writes this section in `config/UserGame.ini`:

```ini
[/Script/DuneSandbox.PlayerOnlineStateSettings]
m_DefaultReconnectGracePeriodSeconds=0
m_OvermapReturnGracePeriodSeconds=0
m_InstancedMapReconnectGracePeriodSeconds=0
```

The same settings are available through token-protected JSON APIs:

- `GET /api/settings/player-online-state`
- `POST /api/settings/player-online-state`

Recreate affected game-server containers after saving so `scripts/run_server_safe.sh` copies the updated `UserGame.ini` into the Unreal saved config paths.

## Security Notes

The Security page includes the optional four-eyes approval queue. A requester
submits an exact governed JSON body; a distinct named operator with the same
target capability reviews a secret-redacted representation and approves or
rejects it. Approved requests expire, bind to the original requester and route,
and can be consumed once through `X-DASH-Approval-ID`. See
`docs/change-approvals.md` for policy levels, browser/API workflows, storage,
metrics, recovery, and fail-closed behavior.

The same page exposes the default-on Mutation Flight Recorder. Before any
authenticated non-read POST dispatches, DASH verifies the complete HMAC event
chain and separately authenticated head, then seals an identity/path/
capability/body-digest admission. Response construction appends a correlated
completion and returns `X-DASH-Request-ID`; old unmatched admissions are
explicit unknown outcomes. See [`audit-ledger.md`](audit-ledger.md).

Every governed high-impact mutation also passes through the default-on
blast-radius change-contract gate. DASH signs the exact operator, target route,
RBAC capability, canonical body digest, current risk/impact policy, and short
expiry. The browser displays backup, reversibility, restart, player, and map
impact before dispatch; API clients use `X-DASH-Change-Contract`. A changed
body, route, operator, capability, policy, expired lifetime, or Admin Panel
restart fails closed. See [`change-contracts.md`](change-contracts.md).

- Do not expose this service to the public internet.
- Use a long random `DUNE_ADMIN_TOKEN` whenever `DUNE_ADMIN_REQUIRE_TOKEN=true`.
- `DUNE_ADMIN_MUTATIONS_ENABLED=false` is the example default. Enable it only after backup/restore validation and operator access controls are in place.
- `DUNE_ADMIN_ITEM_GRANTS_ENABLED=false` is the example default. Enable it only for item grant, stack edit, and deletion workflows that have been validated on the current build.
- `DUNE_ADMIN_BLUEPRINT_MUTATIONS_ENABLED=false` leaves blueprint listing, export, and dry-run available while import/delete execution is disabled.
- `DUNE_ADMIN_AUGMENT_MUTATIONS_ENABLED=false` leaves compatibility discovery and dry-run available while structured augment writes are disabled.
- Director character-transfer settings write `config/director.ini`; recreate the Director container before relying on a changed transfer policy.
- Director GME voice-chat settings also live in `config/director.ini`; recreate Director after changing them.
- `UserEngine.ini`, `UserGame.ini`, and `UserServerCustomSettings*.ini` edits are copied into game containers during game-service startup. Recreate affected game containers before relying on changed gameplay knobs.
- Keep `DUNE_ADMIN_MAX_BODY_BYTES` small unless editing unusually large config files; the default is `65536`.
- Keep `DUNE_ADMIN_AUDIT_MAX_BYTES` bounded; the default rotates the JSONL audit log at 5 MiB.
- Keep `DUNE_ADMIN_AUDIT_LEDGER_ENABLED=true` and `DUNE_ADMIN_AUDIT_LEDGER_REQUIRED_FOR_MUTATIONS=true`; restore the ledger database, HMAC key, and authenticated anchor as one backup unit if verification fails.
- Keep `DUNE_ADMIN_CHANGE_CONTRACTS_ENABLED=true` and `DUNE_ADMIN_CHANGE_CONTRACTS_REQUIRED=true`; the default 120-second contract lifetime is long enough for review without allowing stale reviews to linger.
- Keep `config/outbound-webhooks.json` ignored and mode `0600`; its URL paths and HMAC keys are credentials. Leave `DUNE_WEBHOOK_ALLOW_HTTP=false` outside controlled local receiver tests.
- Keep `DUNE_ADMIN_REQUEST_TIMEOUT_SECONDS` bounded; the default is `10` seconds to limit slow-body and idle connection abuse.
- Keep `DUNE_ADMIN_MAX_ITEM_STACK_SIZE` bounded; the default is `1000000` to prevent accidental enormous stack writes.
- Keep `DUNE_ADMIN_AUDIT_EVENT_LIMIT`, `DUNE_ADMIN_REFERENCE_LIMIT`, and `DUNE_ADMIN_CHARACTER_SEARCH_LIMIT` bounded so read endpoints stay predictable as local data grows.
- Steam account ids in `dune.accounts.platform_id` are SteamID64 values. The roster resolves them to public Steam persona names and profile links, cached in `backups/admin-panel/steam-profiles.json`; tune the cache age with `DUNE_ADMIN_STEAM_PROFILE_CACHE_TTL_SECONDS`, or disable outbound lookups with `DUNE_ADMIN_STEAM_PROFILE_LOOKUP_ENABLED=false`. Steam private login names are not exposed.
- Set `DUNE_ADMIN_ALLOWED_HOSTS` to the exact hostnames used to reach the panel, for example `127.0.0.1:18080,localhost:18080,admin.example.test`.
- Review the Security tab's recent audit events after failed login attempts, blocked host/origin requests, config edits, backups, or mutation runs.
- Restart affected game services after config changes when the target service does not hot-reload.
- POST APIs require `application/json`; form posts, chunked bodies, duplicate `Content-Length`, and oversized requests are rejected before mutation routing.
- Destructive item/keystone actions require server-side confirmation phrases in addition to browser prompts.
- The browser UI uses a per-response CSP nonce and does not require broad `unsafe-inline` script execution.

## Container Hardening

The admin panel container runs with a read-only root filesystem, drops Linux capabilities, sets `no-new-privileges`, uses a small no-exec `/tmp`, and has process/memory guardrails. Only the explicit bind mounts for `admin/`, `config/`, `.env`, and `backups/` are writable where needed.
