# Server Knobs Audit

Audit date: 2026-05-19.

This file tracks Funcom server settings that are useful to expose in the local admin panel or Compose stack. It intentionally avoids storing local secrets.

## Exposed Now

The admin panel config editor can edit these local files:

- `config/UserEngine.ini`
- `config/UserGame.ini`
- `config/UserServerCustomSettings.ini`
- `config/UserServerCustomSettings.survival.ini`
- `config/UserServerCustomSettings.deep-desert-pvp.ini`
- `config/director.ini`
- `config/gateway.ini`
- `config/rabbitmq-admin.conf`
- `config/rabbitmq-game.conf`

Game server containers copy `UserEngine.ini`, `UserGame.ini`, and the selected
`UserServerCustomSettings*.ini` overlay into Unreal's saved config paths at launch:

- `DuneSandbox/Saved/Config/LinuxServer/Engine.ini`
- `DuneSandbox/Saved/Config/LinuxServer/Game.ini`
- `DuneSandbox/Saved/UserSettings/UserEngine.ini`
- `DuneSandbox/Saved/UserSettings/UserGame.ini`
- `DuneSandbox/Saved/UserSettings/ServerCustomSettings.ini`

`DUNE_SERVER_LOGIN_PASSWORD` remains in `.env`; `scripts/run_server_safe.sh` injects it at launch so the tracked config file does not contain the live password.

The admin panel exposes `[/Script/DuneSandbox.PlayerOnlineStateSettings]` from `UserGame.ini` as Settings -> Logout and Reconnect Timers. The typed API endpoints are:

- `GET /api/settings/player-online-state`
- `POST /api/settings/player-online-state`

The admin panel also exposes a safer typed gameplay-knob layer:

- `GET /api/settings/typed-knobs`
- `POST /api/settings/typed-knobs`

Dry-runs are available by passing `dry_run=true`. Actual writes require:

```env
DUNE_ADMIN_MUTATIONS_ENABLED=true
DUNE_ADMIN_TYPED_KNOBS_ENABLED=true
```

and confirmation:

```text
WRITE TYPED KNOBS
```

Typed writes back up the target config file under `backups/admin-panel` before replacing values.

## Official User Engine Knobs

These came from the Steam server package's `scripts/setup/config/UserEngine.ini` template.

These legacy engine keys remain server-side only when they have no direct
client-replicated equivalent:

- `Bgd.ServerDisplayName`: public server display name.
- `SecurityZones.PvpResourceMultiplier`: resource multiplier in PvP/security-zone contexts.
- `dw.VehicleDurabilityDamageMultiplier`: vehicle durability damage multiplier.
- `Sandstorm.Treasure.Enabled`: enable or disable sandstorm treasure.
- `Vehicle.SandwormCollisionInteraction`: vehicle/sandworm collision behavior.
- `Sandworm.SandwormDangerZonesEnabled`: enable danger-zone behavior.
- `Vehicle.SandwormInvulnerabilitySecondsOnExit`: post-exit protection duration.
- `Vehicle.SandwormInvulnerabilitySecondsOnServerRestart`: post-restart protection duration.

Moved to `[/Script/DuneSandbox.UserServerCustomSettings]` and now copied to both
saved locations so 1.5.3+ clients receive the same values:

- `Dune.GlobalMiningOutputMultiplier` + `Dune.GlobalVehicleMiningOutputMultiplier` -> `GatheringAmount`.
- `Sandstorm.Enabled` -> `bAllowSandstorms`.
- `sandworm.dune.Enabled` -> `bAllowSandworms`.
- `dw.LandsraadMissionRewardMultiplierSpecializationXP` -> `LandsraadSpecializationXpMultiplier`.

The typed admin controls use the new custom keys for these moved settings. The
per-map overlays keep the shared maps at `1.0x` gathering and the Hardcore DD
overlay at `3.0x`; the Survival overlay keeps its existing zero-second backup
restriction.

Sensitive:

- `Bgd.ServerLoginPassword`: use `.env` and runtime injection, not tracked config.

## Official User Game Knobs

These came from the Steam server package's `scripts/setup/config/UserGame.ini` template.

These legacy game keys remain server-side because they are map-specific or have
no value-preserving custom equivalent:

- `m_PvpEnabledPartitions`: explicitly enable PvP on listed partitions.
- `m_PingsPerPlayerLimit`, `m_PingMaximumDistance`, `m_PingInWorldMarkerExpiryTime`, and `m_PingMapMarkerExpiryTime`: ping count/range/lifetime settings for group coordination. The current private-server override uses `10`, `5000`, `15`, and `300` instead of shipped `5`, `2000`, `5`, and `60`.
- `m_CostAmount` under `[/Script/DuneSandbox.CharacterRecustomizerSubsystem]`: Solaris cost for character recustomization. The local override sets this to `0`; shipped/default is `5000`.
- `UpdateRateInSeconds`: item deterioration update cadence.
- `m_bCoriolisAutoSpawnEnabled`: Coriolis storm auto-spawn behavior.
- `m_MaxGlobalContractsNumberPerServer`: global contract availability cap. The current private-server override is `20`; shipped/default is `10`.
- `m_DefaultReconnectGracePeriodSeconds`: normal-map reconnect grace/logoff persistence window; set to `0` for immediate disconnect/logout expiry.
- `m_OvermapReturnGracePeriodSeconds`: overmap return grace window; set to `0` for Steam Deck suspend-friendly immediate exit.
- `m_InstancedMapReconnectGracePeriodSeconds`: instanced-map reconnect grace/logoff persistence window; set to `0` for immediate disconnect/logout expiry.
- `m_MaxNumLandclaimSegments`: landclaim segment cap. Current private-server override is `12`; it is not silently mapped to `FiefdomLimit`, whose supported client-facing range does not preserve `12`.
- `DUNE_SUBFIEF_LIMIT` / `DUNE_SUBFIEF_LIMIT_BONUS`: repo-owned experimental subfief/totem count knob. Apply with `scripts/apply-subfief-limit-knob.sh .env`; it writes `DunePlayerCharacterAttributeSet.SubfiefLimitBonus` into persisted player-character actors and current `dune.player_state.player_pawn_id` actors, including blank-class pawn rows. It installs DB triggers for future actor saves and player-state pawn changes. If `DUNE_SUBFIEF_LIMIT_BONUS` is set, it wins over derived `DUNE_SUBFIEF_LIMIT - DUNE_SUBFIEF_BASE_LIMIT`. The player-presence announcer also repairs joined/rejoined players whose current pawn bonus is below the configured value when `DUNE_PLAYER_PRESENCE_SUBFIEF_BONUS_ENFORCER_ENABLED=true`. This is not a shipped INI knob and must be validated in game. Roll back DB triggers with `scripts/apply-subfief-limit-knob.sh .env rollback`.
- `DUNE_SUBFIEF_CAP_BINARY_PATCH_ENABLED` / `DUNE_SUBFIEF_CAP_BINARY_TARGET`: experimental binary patch path for the per-player subfief/totem cap. On the current `2111270-0-shipping` image, `DUNE_SUBFIEF_CAP_BINARY_TARGET=subfief` matches exactly one site and is safe to apply at startup. The historical `all` target also tries obsolete server/map building-cap sites and must not be used with this image. The bypass is current-image validated but still needs in-game placement validation.
- `m_StakingUnitExtensionDefaultTimes` and `m_StakingUnitVerticalExtensionDefaultTimes`: staking-unit activation timers. Current private-server override removes the shipped 60-30720 second entries and replaces both horizontal and vertical extension activation with `1` second.
- `BuildingPieceLimitMultiplier` is the current 1.5.3+ supported server-custom setting under `[/Script/DuneSandbox.UserServerCustomSettings]`. On production image `2124138-0-shipping` (Steam build `25486303`), the running Survival container has `DifficultyLevel=Custom` and `BuildingPieceLimitMultiplier=2.000000` in both `Saved/UserSettings/ServerCustomSettings.ini` and `Saved/Config/LinuxServer/ServerCustomSettings.ini`. The 1.5.3.4 hotfix fixed custom settings being ignored when `DifficultyLevel=Custom` was missing. Funcom documents that UserSettings changes require a battlegroup restart. Confidence is high that the current setting is written to the supported paths; confidence is moderate that 2.0 allows placement up to exactly 10000 because beyond-5000 placement has not been tested in-game.
- The cooked `DT_BuildableStructureCategoryData`/`BuildingPiece` PAK patch remains only as an explicit legacy route (`DUNE_BUILDING_PIECE_LIMIT_MODE=pak`). On `2111270-0-shipping` its old row signature failed before the game process started. Production now uses `server-settings`; `scripts/patch-building-piece-limit-pak.py` is retained for older image builds and lab comparison.
- UE4SS `BuildingPieceCap` remains an experimental fallback. On production image `2124138-0-shipping` (Steam build `25486303`), the current Lua runtime loads on both Deep Desert maps, but both callbacks report `observed=0 patched=0`; the current Dune asset/property was not found, so this mod does not change the live limit. Production uses the supported `BuildingPieceLimitMultiplier=2.000000` server-custom setting instead. Startup logs confirmed that setting on all six always-on maps. Confidence is high that the setting is loaded; the exact 10000-piece placement threshold still needs an in-game placement check.
- Landsraad vendor faction gate lead: eight cooked dialogue assets under `/Game/Dune/NPCs/Dialogue/Generated/DA_Dialogue_*` contain both `PlayerCanAccessLandsraadVendor` and `PlayerFactionHasReignOverLandsraad`. The first condition is the desired active-decree/vendor-type gate; the second is the faction winner gate. `scripts/patch-landsraad-vendor-faction-gate-pak.py` replaces the faction condition class name with the same-length generic Solaris condition name in those dialogue payloads so the active vendor access gate remains while the reigning-faction dialogue gate is bypassed. Confidence is high that this is the right gate; confidence is moderate until validated by a losing-side player after a restart.
- Native specialization XP bonus and popup channel: cooked event asset `/Game/Dune/Events/DataAssets/DA_MTXEvent_SpecializationXPBonus` exists and uses `MTXEventModifier_TrackSpecializationScaleXP`. The game server accepts startup command syntax `-ExecCmds=ScheduleMTXEvent SpecializationXPBonus 60 604800,ListMTXEvents` in lab. Scheduling that MTX event produces client-visible event popup notifications, so the same native event path may be reusable for player-base announcements such as downtime timers if a suitable cooked event asset or JSON/native command variant is found. Confidence is high that `ScheduleMTXEvent SpecializationXPBonus ...` schedules a visible specialization-XP event; confidence is low that the shipped event is configurable to arbitrary announcement text or an arbitrary `3x` scalar without a proved `ScheduleMTXEventJson` payload, cooked asset override, or native command route. `DUNE_SERVER_STARTUP_EXECCMDS` stages startup console commands for the next game-server start only. For the current production visible-3x rollout, `LandsraadSpecializationXpMultiplier=3.000000` is enabled in all three `UserServerCustomSettings` overlays and the database trigger `dune.specialization_tracks_apply_3x_xp_rate` is disabled to prevent 9x stacking; see [Specialization XP Multipliers](specialization-xp.md). The custom setting applies to Landsraad mission reward XP, while the disabled database trigger no longer globally triples other specialization XP writes.
- `m_BuildingBlueprintMaxExtensions`: blueprint extension cap.
- `m_BaseBackupMaxExtensions`: base backup extension cap.
- `m_bBuildingRestrictionLimitsEnabled`: enable/disable building restriction limits. Moved to `bIsBuildingRestrictionsEnabled`.

Moved to `[/Script/DuneSandbox.UserServerCustomSettings]`:

- `m_bShouldForceEnablePvpOnAllPartitions=False` + `m_bAreSecurityZonesEnabled=True` -> `PVPMode=Limited`.
- `m_BaseBackupToolTimeRestrictionInSeconds=60` -> `BaseBackupToolTimeRestriction=0.016667` hours. Survival's existing command-line zero is preserved as `0.000000` in its overlay.

The weekly Coriolis cycle fields, Coriolis auto-spawn/damage/shifting fields,
map feature matrix, landclaim map distribution, and other raw `UserGame.ini`
keys remain in their original overlays because the custom file has no direct
equivalent or because moving them would change map semantics.
- `m_MaxGuildsAllowed`: global guild-count cap. The current private-server override is `999`; shipped/default is `3`.

Typed controls currently implemented:

- `PVPMode`
- `GatheringAmount`
- `bAllowSandstorms`
- `bAllowSandworms`
- `bIsBuildingRestrictionsEnabled`
- `BaseBackupToolTimeRestriction`
- `LandsraadSpecializationXpMultiplier`
- `m_bCoriolisAutoSpawnEnabled`
- `[/Script/DuneSandbox.SpiceHarvestingSystem] m_PerMapSystemSettings`
- `m_BuildingShelterThreshold`
- `m_PlaceableShelterThreshold`
- `ShelteredProtectionThreshold`

The typed layer deliberately excludes Coriolis cycle-start, cycle-duration, DB wipe, and cycle-end restart fields. Those are high-impact fields and should remain raw-config-only until a stronger rollback and validation workflow exists.

## Deep Desert Harvest Bonus

`config/UserEngine.deep-desert-pvp.ini` sets the Hardcore PvE Deep Desert harvest
and vehicle-wear bonus:

```ini
GatheringAmount=3.000000
SecurityZones.PvpResourceMultiplier=1.0
dw.VehicleDurabilityDamageMultiplier=1.15
```

`compose.allmaps.yaml` wires the corresponding
`config/UserServerCustomSettings.deep-desert-pvp.ini` overlay into only the
partition-31 Deep Desert service. The partition-8 Deep Desert service uses the
shared custom file, which keeps gathering at 1.0x. This keeps the 3x second-DD
harvest bonus scoped to partition `31` without advertising the instance as PvP.

Player-facing Paul notices should say:

- partition `8`: `01 Recommended PVE Casual` is persistent, standard harvest,
  no weekly cleanup, no Shifting Sands reset;
- partition `31`: `02 PVE Hardcore` has PvE combat, 3x harvest,
  Shifting Sands/high-damage Coriolis, 15% higher vehicle wear, and weekly
  cleanup of Hardcore DD actors, respawns, and resource fields.

No shipped or validated INI surface currently gates mining/resource multipliers
by player level. Confidence is unknown for a level-gated harvest bonus until a
binary asset override, native command, or DB-backed gameplay hook is proven.

## Deep Desert Spice Caps

The high-confidence Deep Desert content knob is:

```ini
[/Script/DuneSandbox.SpiceHarvestingSystem]
m_PerMapSystemSettings=...
```

The typed knob id is:

```text
spiceDeepDesertCaps
```

Current private-server profile:

- Small Deep Desert spice fields: `60` primed, `60` active.
- Medium Deep Desert spice fields: `24` primed, `24` active.
- Large Deep Desert spice fields: `3` primed, `3` active.
- Survival small spice fields: `5` primed, `5` active.

Medium fields were raised from the previous private-server `12/12` target to
`24/24` so both Standard DD and Hardcore DD feel busier without raising the
large-field cap further. Confidence is high that the cap writes are accepted by
the spice system; balance still needs post-restart observation.

Structured dry-run example:

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

Validation after restart:

```sql
select * from dune.spicefield_types order by map, field_kind_id;

select map,dimension_index,field_kind_id,count(*),sum(value_remaining)
from dune.resourcefield_state
group by 1,2,3
order by 1,2,3;
```

`POST /api/admin/spice-fields/inspect` returns the same high-signal state without writing.

## Director Knobs

Already exposed through `config/director.ini` or typed admin settings:

- Character transfer policy and timeouts.
- Default and per-map `PlayerHardCap`.
- `ShouldUpdatePlayerCountOnFls`.
- `ForceLock`.
- `DauCap`, `WauCap`, `HbsCap`.
- `AllowGroupTravel`.
- `ScalingResourceTarget`.
- Per-map `NumExtraServers`, `MinServers`, and `EnableAutomaticInstanceScaling` where present.
- Per-map queue fallback destination for Deep Desert.

Good next candidates for typed admin controls:

- `ForceIsWorldClosed`: deliberately close the world at Director level.
- `ForceIsWorldClosingSoon`: advertise a closing-soon state.
- `TravelRequestExpirationTimeSeconds`: travel request lifetime.
- `SettingsUpdateFrequencySeconds`: Director settings refresh cadence.
- `FlsServerSettingsUpdateFrequencySeconds`: FLS settings push cadence.
- `FlsShouldSendHeartbeat` and heartbeat frequency values.
- Per-map `AllowQueueingOnLogin`, `KeepPartiesTogether`, `MaxParties`, `QueueFailMap`, and `QueueFailLocation`.

Riskier Director controls:

- `[InstancingModes]`: changing map modes can strand travel targets unless database partitions, compose services, ports, and Director expectations all agree.
- FLS timeout/heartbeat tuning: bad values may look like auth or discovery failures.
- `ScalingResourceTarget`: Kubernetes/operator-specific semantics may not map cleanly to Compose.

## Compose Stack Knobs

Safe candidates:

- Warm-pool size by compose profile/override: minimal, nine-map, or full 30-partition pool.
- Optional resource limits via `compose.limits.example.yaml`.
- Host UDP game port ranges.
- `EXTERNAL_ADDRESS`, `WORLD_NAME`, `WORLD_UNIQUE_NAME`, and `WORLD_REGION`.
- Admin panel limits such as request body size, audit retention, item stack cap, and row limits.

Riskier candidates:

- Public RabbitMQ/database binds. Keep these local-only.
- IGW/S2S UDP forwarding. For the full warm-pool layout, `7888-7918/udp` is the paired IGW range; forward it only when the deployment's live-client routing or server-browser checks require it.
- Arbitrary map service count changes without matching `world_partition` rows.

## Reverse Proxy / Ingress

A reverse proxy is useful for admin and informational HTTP surfaces only:

- Keep `admin.example.test` or equivalent admin hostnames restricted to LAN/VPN.
- Keep the admin panel bound to `127.0.0.1:18080` on the Docker host.
- Use host allowlists and the admin token together; neither should be the only guard.
- Leave `dune.snape.tech` as an informational web response. Dune game traffic is UDP and should use direct router forwarding, not HTTP reverse proxying.

Do not proxy the game UDP path through an HTTP reverse proxy unless a separate UDP transport design is deliberately built and tested.

## Current Gaps

- `gateway` is defined in the stack but was not running during this audit. Full farm readiness has been green without it, but this should be validated against live-client travel and FLS behavior before deciding it is optional.
- Admin panel has raw config-file editing for `UserEngine.ini` and `UserGame.ini`; logout/reconnect timers and selected high-confidence gameplay knobs now have typed controls. Shelter/hydration candidates are still experimental even though they are represented in the typed layer.
- Native GM command execution remains blocked until the RabbitMQ payload route is verified by a live client.
- Journey, recipe, and vehicle unlock mutation remain blocked until safe DB functions or live examples are mapped.
- There is no automated per-map resource recommendation yet. Use `scripts/profile-runtime.sh` and `scripts/summarize-runtime-profile.sh` while testing player travel.
