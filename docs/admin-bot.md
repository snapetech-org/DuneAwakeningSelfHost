# Paul Admin Bot

Paul is the in-game/admin-bot identity. `scripts/admin-bot.py` is the umbrella automation runner for approved Paul features.

Enabled in this first pass:

- Spam warn/kick path lives in `scripts/admin-chat-commands.py` and `scripts/spam-kick-player.sh`.
- Map death watchdog integration through `scripts/watch-maps.sh`.
- Stuck-transition reporting.
- Backup freshness reporting, with opt-in stale-backup execution.
- Restart workflow remains in the existing admin panel scheduler and `scripts/restart-target.sh`.
- Admin action audit digest.
- Economy anomaly reporting for large currency balances.
- Base/claim count reporting.
- Config drift detection for `.env`, Compose, `UserGame.ini`, and `director.ini`.
- Admin token/Host/Origin/denied-request audit summary.
- Optional join/leave announcements through the same Paul/DASH Admin in-game announcement path.

Intentionally skipped:

- Toxicity/slur filtering.
- Queue/population forecasts.

Run one report:

```bash
./scripts/admin-bot.py --once
```

Run as a loop:

```bash
./scripts/admin-bot.py --loop
```

Defaults are report-first. The map watchdog runs `watch-maps.sh --dry-run` unless `DUNE_ADMIN_BOT_MAP_WATCHDOG_RECOVER=true`. Stale backup execution is also disabled unless `DUNE_ADMIN_BOT_BACKUP_STALE_RUN=true`.

Important knobs:

```env
DUNE_ADMIN_BOT_INTERVAL_SECONDS=300
DUNE_ADMIN_BOT_BACKUP_MAX_AGE_HOURS=24
DUNE_ADMIN_BOT_BACKUP_STALE_RUN=false
DUNE_ADMIN_BOT_MAP_WATCHDOG_ENABLED=true
DUNE_ADMIN_BOT_MAP_WATCHDOG_RECOVER=false
DUNE_ADMIN_BOT_STUCK_TRANSITIONS_ENABLED=true
DUNE_ADMIN_BOT_STUCK_TRANSITION_MINUTES=10
DUNE_ADMIN_BOT_AUDIT_DIGEST_ENABLED=true
DUNE_ADMIN_BOT_ECONOMY_ANOMALIES_ENABLED=true
DUNE_ADMIN_BOT_SOLARI_WARN_THRESHOLD=10000000
DUNE_ADMIN_BOT_BASE_CLAIM_MONITOR_ENABLED=true
DUNE_ADMIN_BOT_BASE_CAP_CONFIG=config/UserGame.ini
DUNE_ADMIN_BOT_BASE_CAP_MAP=HaggaBasin
DUNE_ADMIN_BOT_CONFIG_DRIFT_ENABLED=true
DUNE_ADMIN_BOT_SECURITY_GUARD_ENABLED=true
```

State is stored in `backups/admin-bot/state.json`. The state file tracks audit offsets and config hashes so repeated runs report only new audit activity and drift since the previous run.

Base-recovery reminders are stored separately in
`backups/admin-bot/base-recovery-reminders.json` by default. The registry is
shared by the CLI, the player-presence worker, and the Admin Digests panel.
Keep `DUNE_ADMIN_BASE_RECOVERY_REMINDER_MUTATIONS_ENABLED=false` unless the
authenticated panel should be allowed to add, replace, or remove reminder
records; the global `DUNE_ADMIN_MUTATIONS_ENABLED` gate is required as well.

Parked vehicle retirement and recovery reminders use the same worker and panel
path. The native multi-vehicle recovery contract, including its optional
ordinary-cargo wipe, full-dump receipt, and restore proof, is documented in
[`vehicle-recovery.md`](vehicle-recovery.md). The additional settings are:

```env
DUNE_ADMIN_VEHICLE_RETIREMENT_MUTATIONS_ENABLED=false
DUNE_ADMIN_VEHICLE_RECOVERY_REMINDER_MUTATIONS_ENABLED=false
DUNE_PLAYER_PRESENCE_VEHICLE_RECOVERY_REMINDERS_FILE=backups/admin-bot/vehicle-recovery-reminders.json
```

## Player Presence Announcements

`scripts/player-presence-announcer.py` is a lightweight companion loop for chatty player-presence events. It polls `dune.player_state`, stores the previous online set in `backups/admin-bot/player-presence.json`, and announces only transitions after the first baseline poll.

```env
DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED=true
DUNE_PLAYER_PRESENCE_POLL_SECONDS=15
DUNE_PLAYER_PRESENCE_SERVER_NAME=My Dune Server
DUNE_PLAYER_PRESENCE_SERVER_URL=https://example.test
DUNE_PLAYER_PRESENCE_RULES_URL=https://example.test
DUNE_PLAYER_PRESENCE_JOIN_TEMPLATE=Welcome {playername}! Current player count is now {count}.
DUNE_PLAYER_PRESENCE_RETURN_JOIN_TEMPLATE=Welcome back {playername}! Current player count is now {count}.
DUNE_PLAYER_PRESENCE_LEAVE_TEMPLATE={playername} has left, current count is {count}.
DUNE_PLAYER_PRESENCE_JOIN_MESSAGE_DELAY_SECONDS=30
DUNE_PLAYER_PRESENCE_TRANSFER_GRACE_SECONDS=90
DUNE_PLAYER_PRESENCE_SESSION_REJOIN_ON_LOCATION_CHANGE=false
DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND=/workspace/scripts/announce.sh
DUNE_PLAYER_PRESENCE_ANNOUNCE_ROUTING_KEYS=<empty>
DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED=true
DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_TEMPLATE=Welcome! Please check {rules_url} for server rules.
DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_EXCHANGE=chat.whispers
DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_CHANNEL=Whispers
DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_ROUTING_KEY=
DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_COMMAND=/workspace/scripts/announce.sh
DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDERS_FILE=backups/admin-bot/base-recovery-reminders.json
# Compatibility fallback only; registry records take precedence once the file exists.
DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_ENABLED=false
DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_ACCOUNT_ID=5247
DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_BACKUP_ID=127
DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_TOTEM_ID=22323
DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_MESSAGE=Mara, your base is preserved in the server's Base Reconstruction Tool backup. When you return, please contact a server admin so we can recover it for you.
DUNE_PLAYER_PRESENCE_FIRST_SEEN_ENABLED=true
DUNE_PLAYER_PRESENCE_FIRST_SEEN_TEMPLATE=Welcome to {server_name}. This is a friendly PvE server. Please keep shared paths, spawns, and resources clear. Rules: {rules_url}
DUNE_PLAYER_PRESENCE_HAGGA_ARRIVAL_ENABLED=true
DUNE_PLAYER_PRESENCE_HAGGA_ARRIVAL_TEMPLATE=You made it to Hagga Basin. Build with room around roads, spawns, resource areas, and points of interest. Rules: {rules_url}
DUNE_PLAYER_PRESENCE_DEEP_DESERT_FIRST_ENABLED=true
DUNE_PLAYER_PRESENCE_DEEP_DESERT_FIRST_TEMPLATE=Deep Desert is high risk. Expect sandstorms, sandworms, and harsher recovery. Support: {server_url}
DUNE_PLAYER_PRESENCE_DEEP_DESERT_JOIN_MESSAGES_ENABLED=true
DUNE_PLAYER_PRESENCE_DEEP_DESERT_CASUAL_PARTITIONS=8
DUNE_PLAYER_PRESENCE_DEEP_DESERT_HARDCORE_PARTITIONS=31
DUNE_PLAYER_PRESENCE_DEEP_DESERT_CASUAL_JOIN_TEMPLATE=PVE Casual ({partition_label}): persistent PvE Deep Desert, standard harvest, no weekly cleanup, no Shifting Sands reset.
DUNE_PLAYER_PRESENCE_DEEP_DESERT_HARDCORE_JOIN_TEMPLATE=PVE Hardcore ({partition_label}): PvE combat, 3x harvest, high sandstorm/Coriolis damage, Shifting Sands, 15% higher vehicle wear, and weekly Hardcore DD cleanup during maintenance.
DUNE_PLAYER_PRESENCE_BASE_REMINDERS_ENABLED=true
DUNE_PLAYER_PRESENCE_BASE_CAP_CONFIG=config/UserGame.ini
DUNE_PLAYER_PRESENCE_BASE_CAP_MAP=HaggaBasin
DUNE_PLAYER_PRESENCE_BASE_NEAR_CAP=5
DUNE_PLAYER_PRESENCE_FIRST_BASE_TEMPLATE=Base reminder: please avoid blocking shared paths, NPCs, resources, caves, wrecks, and POIs. Rules: {rules_url}
DUNE_PLAYER_PRESENCE_BASE_NEAR_CAP_TEMPLATE=Base reminder: this server has a {base_cap} landclaim cap per Hagga Basin map. Please clean up unused claims. Rules: {rules_url}
DUNE_PLAYER_PRESENCE_BASE_OVER_CAP_TEMPLATE=Heads up: you appear to be over the {base_cap} landclaim cap. Please clean up unused claims. Rules: {rules_url}
DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_ENABLED=true
DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_WINDOW_SECONDS=600
DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_TEMPLATE=Welcome back. If you were disconnected during travel, wait a moment before retrying the same transition.
DUNE_PLAYER_PRESENCE_RESTART_PRIVATE_WARNINGS_ENABLED=true
DUNE_PLAYER_PRESENCE_RESTART_PRIVATE_WARNING_MARKS_SECONDS=1800,600,300,60
DUNE_PLAYER_PRESENCE_RESTART_PRIVATE_WARNING_TEMPLATE=Server maintenance in about {remaining}. Please get to a safe place.
DUNE_PLAYER_PRESENCE_POST_RESTART_RETURN_ENABLED=true
DUNE_PLAYER_PRESENCE_POST_RESTART_WINDOW_SECONDS=3600
DUNE_PLAYER_PRESENCE_POST_RESTART_TEMPLATE=Maintenance is complete. If anything looks wrong, report it through {server_url}.
DUNE_PLAYER_PRESENCE_STUCK_POSITION_ENABLED=false
DUNE_PLAYER_PRESENCE_STUCK_POSITION_TEMPLATE=Your position looks unusual. If you are stuck, message an admin before relogging repeatedly.
DUNE_PLAYER_PRESENCE_ADMIN_NAMES=
DUNE_PLAYER_PRESENCE_ADMIN_FLS_IDS=
DUNE_PLAYER_PRESENCE_ADMIN_ALERT_INTERVAL_SECONDS=900
DUNE_PLAYER_PRESENCE_MAP_HEALTH_PUBLIC_ENABLED=true
DUNE_PLAYER_PRESENCE_MAP_HEALTH_DEGRADED_TEMPLATE=Server notice: some travel destinations are recovering ({online}/{expected} online). If travel fails, wait 1-2 minutes and retry.
DUNE_PLAYER_PRESENCE_MAP_HEALTH_RECOVERED_TEMPLATE=Server notice: all travel destinations are online again ({online}/{expected}).
DUNE_PLAYER_PRESENCE_MAP_HEALTH_ADMIN_ENABLED=true
DUNE_PLAYER_PRESENCE_MAP_HEALTH_ADMIN_TEMPLATE=Admin alert: map health degraded, {online}/{expected} online. Offline: {offline_maps}
DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_ENABLED=true
DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_THRESHOLDS=30,35,40
DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_TEMPLATE=Server is getting busy: {count} online. Travel and loading may take longer.
DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_ENABLED=true
DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_INTERVAL_SECONDS=1800
DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_TEMPLATE=Admin population: {count} online. Maps: {map_counts}
DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_ENABLED=true
DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_WINDOW_SECONDS=600
DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_THRESHOLD=3
DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_TEMPLATE=Looks like you have reconnected several times. If travel is stuck, report it through {server_url}.
DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_ENABLED=true
DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_INTERVAL_SECONDS=1800
DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_TEMPLATE=Admin digest: stuck/recent anomalies={stuck_count} ({stuck_names}); over base cap={over_base_cap}.
DUNE_PLAYER_PRESENCE_ADMIN_MAP_ROSTER_DIGEST_ENABLED=true
DUNE_PLAYER_PRESENCE_ADMIN_MAP_ROSTER_DIGEST_INTERVAL_SECONDS=1800
DUNE_PLAYER_PRESENCE_ADMIN_MAP_ROSTER_DIGEST_TEMPLATE=Admin roster: {count} online. {map_roster}
DUNE_PLAYER_PRESENCE_ADMIN_UNIQUE_DAILY_DIGEST_ENABLED=true
DUNE_PLAYER_PRESENCE_ADMIN_UNIQUE_DAILY_DIGEST_INTERVAL_SECONDS=86400
DUNE_PLAYER_PRESENCE_ADMIN_UNIQUE_DAILY_DIGEST_TEMPLATE=Admin daily players: {unique_count} unique accounts seen today, {count} currently online.
DUNE_PLAYER_PRESENCE_MAINTENANCE_ONLINE_ADMIN_ENABLED=true
DUNE_PLAYER_PRESENCE_MAINTENANCE_ONLINE_WINDOW_SECONDS=1800
DUNE_PLAYER_PRESENCE_MAINTENANCE_ONLINE_ADMIN_TEMPLATE=Admin maintenance check: {count} players still online before maintenance. {map_roster}
DUNE_PLAYER_PRESENCE_MAP_WITH_PLAYERS_UNHEALTHY_ADMIN_ENABLED=true
DUNE_PLAYER_PRESENCE_MAP_WITH_PLAYERS_UNHEALTHY_ADMIN_TEMPLATE=Admin alert: unhealthy maps still report players: {impacted_maps}
DUNE_PLAYER_PRESENCE_PUBLIC_MAINTENANCE_CANCELLED_ENABLED=true
DUNE_PLAYER_PRESENCE_PUBLIC_MAINTENANCE_CANCELLED_TEMPLATE=Maintenance has been cancelled or delayed. Normal play can continue.
DUNE_PLAYER_PRESENCE_INCIDENT_MODE_PUBLIC_ENABLED=true
DUNE_PLAYER_PRESENCE_INCIDENT_MODE_ACTIVE=false
DUNE_PLAYER_PRESENCE_INCIDENT_MODE_ON_TEMPLATE=Server notice: admins are investigating travel instability. Updates at {server_url}.
DUNE_PLAYER_PRESENCE_INCIDENT_MODE_OFF_TEMPLATE=Server notice: incident mode is cleared. Normal play can continue.
DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED=true
DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DIGEST_LIMIT=8
DUNE_PLAYER_PRESENCE_DIGEST_LOG_LIMIT=250
DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED=true
DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_MESSAGE_ENABLED=true
DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_MESSAGE_TEMPLATE=A Base Reconstruction Tool has been added to your inventory. You may need to log out and back in before it appears.
DUNE_PLAYER_PRESENCE_VERMILIUS_GAP_ENABLED=true
DUNE_PLAYER_PRESENCE_VERMILIUS_GAP_NODE=DA_SQ_VermiliusGap.Relocate.RelocateOutsideHBS.Drive north to the Vermilius Gap
DUNE_PLAYER_PRESENCE_VERMILIUS_GAP_TEMPLATE=Congrats! {playername} has outrun Shai-Hulud!
```

Presence templates support `{playername}`, `{player_name}`, `{count}`, `{player_count}`, `{server_name}`, `{server_url}`, and `{rules_url}`. Base-cap templates also support `{base_cap}`. Restart-warning templates support `{remaining}` and `{remaining_seconds}`. Vermilius Gap templates support `{playername}`, `{player_name}`, and `{story_node_id}`.

Global join/leave messages use `DUNE_PLAYER_PRESENCE_JOIN_TEMPLATE`, `DUNE_PLAYER_PRESENCE_RETURN_JOIN_TEMPLATE`, and `DUNE_PLAYER_PRESENCE_LEAVE_TEMPLATE` through the public `announce()` path. First-time joins are accounts missing from `seenAccounts`; returning joins are accounts already recorded there. `DUNE_PLAYER_PRESENCE_TRANSFER_GRACE_SECONDS` suppresses leave/rejoin noise when a player briefly disappears from the online query and returns on a different map or partition during normal travel. Session timestamp changes are treated as rejoins only when the player location did not change; set `DUNE_PLAYER_PRESENCE_SESSION_REJOIN_ON_LOCATION_CHANGE=true` to restore the older behavior. `DUNE_PLAYER_PRESENCE_JOIN_MESSAGE_DELAY_SECONDS` can delay join-triggered public and private messages until a player has remained online long enough for the client chat path to render them. Player-presence announcements override the shared announcement routing and default to `DUNE_PLAYER_PRESENCE_ANNOUNCE_ROUTING_KEYS=<empty>` so each online client receives one copy. Do not set this to multiple routing keys unless you intentionally want fan-out; the shared `DUNE_ANNOUNCE_CHAT_ROUTING_KEYS=HaggaBasin.0,Survival_1.dim_0,<empty>` pattern can make HUD notices render more than once.

The private welcome path runs on every detected join for existing and new players after the first baseline poll. It uses the same Paul chat sender, disables dashboard `!!!` wrapping, derives the joined player's live whisper route from their FLS id, publishes to `DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_EXCHANGE`, and sets `m_ChannelType` to `DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_CHANNEL`. The intended private route is `chat.whispers` with `Whispers`. The shared publisher defaults to the confirmed client-visible Paul whisper spelling, `m_TimeStamp`, and can be overridden with `DUNE_ANNOUNCE_CHAT_TIMESTAMP_FIELD` if another game build requires `m_Timestamp`. See `docs/private-chat-replies.md`. The default private welcome message is:

```text
Welcome! Please check {rules_url} for server rules.
```

One-shot account-specific login notices use the same private Paul whisper path
but are separate from native BRT recovery reminders. Configure the registry
with `DUNE_PLAYER_PRESENCE_LOGIN_NOTICES_FILE` (default
`backups/admin-bot/player-login-notices.json`) and manage it with:

```bash
./scripts/player-login-notice.py add --account-id 105 \
  --message "Server notice: your former base was transferred because it was inactive. A backup of the prior server state is available on request; contact a server admin if you want to discuss recovery."
./scripts/player-login-notice.py list
./scripts/player-login-notice.py remove --account-id 105
```

The worker retries failed delivery and marks a notice delivered after the
first successful whisper. A new notice revision can be queued by replacing
the message or removing and re-adding the account record.

The base-recovery reminder registry targets any number of accounts and uses the
verified Paul whisper route. Each record sends one private reminder for each
distinct `lastLoginTime` session while its native BRT backup is still staged.
The poll checks the live database on every cycle and stops only when that
backup is gone, the totem exists with an active owner permission for the target
account, and the totem is no longer marked `BaseBackup`. A failed status query
leaves the reminder active; a failed whisper is retried on the next poll. Keep
each account, backup, and totem id bound to the same recovery receipt.

Manage records from the repository checkout without editing `.env`:

```bash
# Add or replace one persistent reminder.
./scripts/base-recovery-reminder.py add --account-id 5247 --backup-id 127 --totem-id 22323 \
  --message "Mara, your base is preserved in the server's Base Reconstruction Tool backup. Contact a server admin when you return."

# Show configured records plus the latest worker status and online marker.
./scripts/base-recovery-reminder.py list

# Run a read-only native BRT status check (optionally --account-id N).
./scripts/base-recovery-reminder.py check

# Stop future reminders for one account; this does not touch the player, base,
# native BRT backup, inventory, or any credits/items.
./scripts/base-recovery-reminder.py remove --account-id 5247
```

The CLI accepts `--file PATH` before the subcommand for a separate registry.
If the registry file does not exist, the worker temporarily honors the legacy
`DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_*` single-record environment
variables for compatibility. Creating the registry (including an empty one)
intentionally supersedes that fallback.

The Admin Digests page exposes the same records with account/backup/totem ids,
message text, active/restored state, online/player name, last status check, and
last send time. Saving a record uses `POST /api/admin/base-recovery-reminders`
and requires both mutation gates. Removal requires the exact confirmation
phrase shown in the panel and is audited; it only removes the reminder record.

Vehicle retirement is a separate preview/archive action and is never performed
by the presence worker. It uses the native multi-vehicle `VehicleRecovery`
queue (the one-row-per-character `VehicleBackup` slot cannot hold multiple
vehicles). Use the CLI after stopping the owning map and reviewing the preview;
cargo is preserved unless the exact wipe option is supplied:

```bash
./scripts/vehicle-retirement.py list --account-id 5247
./scripts/vehicle-retirement.py preview --account-id 5247 --vehicle-id 22279 --vehicle-id 22296
./scripts/vehicle-retirement.py archive --account-id 5247 --vehicle-id 22279 --vehicle-id 22296 --expected-fingerprint <fingerprint> --confirm 'ARCHIVE THOPTERS 22279,22296'
```

Vehicle reminders are managed independently and repeat until every listed
native recovery or backup row is restored:

```bash
./scripts/vehicle-recovery-reminder.py add --account-id 5247 --vehicle-id 22279 --vehicle-id 22296
./scripts/vehicle-recovery-reminder.py list
./scripts/vehicle-recovery-reminder.py check --account-id 5247
./scripts/vehicle-recovery-reminder.py remove --account-id 5247
```

The Admin Digests page exposes the same vehicle records with vehicle ids,
restore proof, online state, and last-send markers. Removing a vehicle reminder
does not touch the vehicle, native recovery row, private inventory snapshot,
player, or credits.

Automated private messages are derived from local state and operator config rather than hard-coded server details. These use the private helper and should render as whispers/private messages:

- `FIRST_SEEN` sends once per account after its first observed join.
- `HAGGA_ARRIVAL` sends once when an online player is first observed on `HaggaBasin`.
- `DEEP_DESERT_FIRST` sends once when an online player is first observed on a Deep Desert map.
- `DEEP_DESERT_JOIN_MESSAGES` sends a private Paul whisper each time a player enters or logs into a Deep Desert instance. Partition `8` is treated as PVE Casual and is labeled `01 Recommended PVE Casual`; partition `31` is treated as PVE Hardcore and is labeled `02 PVE Hardcore` with 3x harvest, higher storm/Coriolis penalties, and weekly scoped cleanup.
- `BASE_REMINDERS` queries `dune.totems`, `dune.actors`, and `dune.landclaim_segments` for that account and sends first-base, near-cap, or over-cap reminders only when the count changes.
- `RECONNECT_RECOVERY` sends when a player rejoins inside the configured short reconnect window.
- `RESTART_PRIVATE_WARNINGS` mirrors scheduled admin-panel restart/announcement jobs to online players at configured remaining-time marks.
- `POST_RESTART_RETURN` sends once per recently executed restart job when a player rejoins after maintenance.
- `STUCK_POSITION` is available but disabled by default; it only warns when an online player has no map/location in the DB, which can be noisy during travel.
- `MAP_HEALTH_PUBLIC` announces when the farm transitions from full health to degraded, and again when all partition rows are back online. It derives `{online}` and `{expected}` from `dune.world_partition`, `dune.farm_state`, and `dune.active_server_ids`.
- `MAP_HEALTH_ADMIN` privately alerts currently online admins at `DUNE_PLAYER_PRESENCE_ADMIN_ALERT_INTERVAL_SECONDS` while map health remains degraded.
- `POPULATION_PUBLIC` announces once when online player count crosses a configured threshold band.
- `POPULATION_ADMIN_DIGEST` privately sends currently online admins a periodic player-count-by-map digest.
- `RECONNECT_SUPPORT` privately nudges players who reconnect repeatedly inside a short window.
- `ADMIN_ANOMALY_DIGEST` privately sends currently online admins a compact digest for stuck-transition candidates and over-cap base counts.
- `ADMIN_MAP_ROSTER_DIGEST` privately sends admins exact online player rosters grouped by map.
- `ADMIN_UNIQUE_DAILY_DIGEST` privately sends admins the daily unique-account count.
- `MAINTENANCE_ONLINE_ADMIN` privately tells admins who is still online, grouped by map, before scheduled maintenance.
- `MAP_WITH_PLAYERS_UNHEALTHY_ADMIN` privately alerts admins when an unhealthy map still reports connected players.
- `PUBLIC_MAINTENANCE_CANCELLED` publishes a cancellation/delay notice when an announcement or restart job is cancelled after scheduling.
- `INCIDENT_MODE_PUBLIC` publishes manual incident on/off notices when `DUNE_PLAYER_PRESENCE_INCIDENT_MODE_ACTIVE` changes.
- `ADMIN_FIRST_LOGIN_DAILY` sends the current admin digest set to each configured admin on their first login each day.

The subfief bonus repair path runs quietly for joined/rejoined players when
`DUNE_PLAYER_PRESENCE_SUBFIEF_BONUS_ENFORCER_ENABLED=true`. It checks the
joined account's current `dune.player_state.player_pawn_id` actor and writes
`DunePlayerCharacterAttributeSet.SubfiefLimitBonus` only when the current
base/current value is below the configured bonus. The configured bonus is
`DUNE_PLAYER_PRESENCE_SUBFIEF_MIN_BONUS` when set, otherwise
`DUNE_SUBFIEF_LIMIT_BONUS`, otherwise `DUNE_SUBFIEF_LIMIT -
DUNE_SUBFIEF_BASE_LIMIT`.

The public/global presence events are intentionally not private:

- `JOIN` and `LEAVE` are the visible welcome/goodbye population notices.
- `MAP_HEALTH_PUBLIC`, `POPULATION_PUBLIC`, `PUBLIC_MAINTENANCE_CANCELLED`, `INCIDENT_MODE_PUBLIC`, `TRANSFER_POLICY_PUBLIC`, `RULES_CHANGE_PUBLIC`, `PEAK_PUBLIC`, and `DAILY_STATUS_PUBLIC` publish server-wide notices.
- `VERMILIUS_GAP` is currently a public celebration announcement.

For the full private/global routing matrix, command-reply behavior, and RabbitMQ verification steps, use [private-chat-replies.md](private-chat-replies.md). That runbook tracks `chat.whispers`, `Whispers`, timestamp-field behavior, and the expected `reply.stdout` metadata from command smoke tests.

Admin-private recipients are derived from currently online players whose character name or FLS id matches `DUNE_PLAYER_PRESENCE_ADMIN_NAMES` / `DUNE_PLAYER_PRESENCE_ADMIN_FLS_IDS`. Keep real admin identifiers in private `.env`, not in committed examples or docs. Digest entries are stored in `backups/admin-bot/player-presence.json` under `adminDigestLog`, and the admin panel exposes them on the Admin Digests tab.

The starter Base Reconstruction Tool path grants one `BaseBackupTool` to each newly observed joining account that has not already been recorded in `backups/admin-bot/player-presence.json`. The grant is queued while the player is online and committed only after the player is offline; writing a live player's inventory directly can be overwritten by the game's persistence snapshot during logout. A failed or deferred grant remains pending and is retried after `DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_RETRY_SECONDS` (default `60`). When the grant succeeds, the state records it and the item is available on the next login. The private message is recorded as skipped while the player is offline rather than falsely claiming a live delivery.

## DD1 BRT Chat Commands

`scripts/admin-chat-commands.py` now wires two player-scoped PM commands for
the Deep Desert #1 BRT workaround:

```text
&DD1_backup [totem_id]
&DD1_restore [backup_id]
&DD1_restore yes
&DD1_restore cancel
```

The commands are self-service: they resolve the whisper sender and only operate
on that character's own totems/backups. `&DD1_backup` uses
`dune.base_backup_save_from_totem(player_id, totem_id)` when enabled. It first
resolves the sender's current DD1 position from the incoming whisper
`m_OriginLocation`, falling back to the stored DB actor transform. The selected
totem must be an owned DD1 totem whose base footprint contains that current
position. An explicit totem id is allowed, but it still has to be the base the
player is standing in.

`&DD1_restore` is currently a guarded preview/confirmation flow. It captures the
target from the incoming whisper's `m_OriginLocation` when present, otherwise it
falls back to the stored DB actor transform, then applies the configured restore
offset so the placement target is slightly away from the player's current
location. The restore preview computes the translation from the backup's
original totem actor position to the requested DD1 target and stores a
short-lived confirmation. The actual persistence writer is blocked until the
row-copy mapper is disposable-base validated; this is intentional because a real
base spans actors, building instances, placeables, totems, landclaim segments,
permission rows, FGL entities, inventories, and items. This is not currently the
native client BRT restore path, so restore execution must stay disabled until
support/foundation behavior is validated.

Relevant gates:

```env
DUNE_CHAT_COMMAND_DD1_BRT_BACKUP_ENABLED=false
DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ENABLED=false
DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_COPY_SOURCE_ENABLED=false
DUNE_CHAT_COMMAND_DD1_RESTORE_CONFIRM_SECONDS=120
DUNE_CHAT_COMMAND_DD1_RESTORE_CONFIRM_MAX_DISTANCE=500
DUNE_CHAT_COMMAND_DD1_PARTITION_ID=8
DUNE_CHAT_COMMAND_DD1_MAP_NAMES=DeepDesert,DeepDesert_1
DUNE_CHAT_COMMAND_DD1_BASE_CONTAINMENT_PADDING=1500
DUNE_CHAT_COMMAND_DD1_RESTORE_OFFSET_X=3000
DUNE_CHAT_COMMAND_DD1_RESTORE_OFFSET_Y=0
DUNE_CHAT_COMMAND_DD1_RESTORE_OFFSET_Z=0
```

The backup command must stay disabled for public use until a live actor unload
route is proven. `dune.base_backup_save_from_totem(...)` creates a real backup
and marks the linked actors `BaseBackup`, but it does not live-unload actors
already loaded by the running DD process. If DD later reloads while the partial
state remains, the base can disappear from persistence. The safe undo for that
partial state is `dune.base_backup_finish_placing(backup_id)` plus recreating
the totem `permission_actor` / owner rank.

`scripts/dd1-brt-emulator.py` contains the current DB mapper and safety tooling
for restore experiments. Read-only commands:

```bash
scripts/dd1-brt-emulator.py list-totems --player-id 17
scripts/dd1-brt-emulator.py list-backups --player-id 17
scripts/dd1-brt-emulator.py inspect-backup --backup-id 59
```

Rollback-only transform validation:

```bash
scripts/dd1-brt-emulator.py simulate-from-totem \
  --player-id 17 --totem-id 5903 --offset-x 1000 --offset-y 0 --offset-z 0
```

For a backup accidentally left staged by `base_backup_save_from_totem`, the
guarded in-place undo path is:

```bash
scripts/dd1-brt-emulator.py finish-staged-backup --backup-id 59
scripts/dd1-brt-emulator.py finish-staged-backup --backup-id 59 \
  --commit \
  --confirm 'FINISH DD1 BRT BACKUP' \
  --rpc-classification-json /tmp/brt-dd-trace-classification.json
```

Commit mode refuses to run off `kspls0`, requires the exact confirmation phrase,
and requires an RPC classification. Prefer a classifier artifact:

```bash
scripts/classify-brt-dd-trace.py /tmp/brt-place-trace-lab.log --format json \
  > /tmp/brt-dd-trace-classification.json
```

The classifier records whether `SERVER-RPC-ENTRY` / `SERVER-RPC-EXEC` or the
equivalent uprobe `brt_rpc_*` events fired. If they did not, the normal request
is classified as not observed and the repair path is server-side emulation, not
client file modification. This validates and repairs DB shape only. Restore
execution must stay disabled until the mapper is committed on a disposable base
and a live runtime unload/native RPC route is known.

The quiet starter emote path grants `DUNE_PLAYER_PRESENCE_STARTER_EMOTE_TEMPLATES` into the configured emote inventory type, default `14`, for newly observed joining accounts. It records successful accounts under `starterEmotesGranted` in the same state file and does not send a public or private message.

The Vermilius Gap announcement watches `dune.journey_story_node` for the configured node's `complete_condition_state = true`. It records completed account ids under `backups/admin-bot/player-presence.json`, so each player is congratulated once. The first poll after enabling the feature baselines already-completed players and does not announce them retroactively.

Run one baseline/check without installing the service:

```bash
./scripts/player-presence-announcer.py --once
```

Install the host service with:

```bash
./scripts/install-player-presence-announcer-service.sh .env
```

or:

```bash
make install-player-presence-announcer-service ENV_FILE=.env
```

The installer stages an ignored mode-0600 `config/secrets/player-presence.env` with the announcer's settings and required chat connection values, then writes `/etc/systemd/system/dune-player-presence-announcer.service`, enables it, and restarts the service. The unit loads the filtered settings through systemd and fixes its dotenv paths and announcement command. Its announcement child sets `DUNE_ANNOUNCE_ENV_FILE=/dev/null`, so `announce.sh` skips the default `.env` files. The announcer and FLS health check use container labels and direct Docker exec/log calls instead of resolving the full Compose project. RabbitMQ credentials go in the management API's HTTP authorization header; the helper no longer passes them to `rabbitmqadmin` arguments. Because it is enabled with `WantedBy=multi-user.target`, `Requires=docker.service`, `Restart=always`, and an unlimited start-limit interval, it starts after host reboots and keeps restarting after script failures or early boot dependency races. The generated unit uses the current checkout path and current user by default; set `DUNE_PLAYER_PRESENCE_SERVICE_USER=<user>` when running the installer to choose another system user.

Operational checks:

```bash
systemctl is-enabled dune-player-presence-announcer.service
systemctl status dune-player-presence-announcer.service
journalctl -u dune-player-presence-announcer.service -f
```

The first service poll records the current online set and does not announce those already online. Announcements start on later observed joins/leaves.
