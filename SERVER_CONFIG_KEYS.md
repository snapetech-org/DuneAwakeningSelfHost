# Server Config Keys

This file tracks the known `config/UserGame.ini` override keys in this repo.
Values here are server-side overrides unless a note says the client also needs
the same value.

For the exhaustive generated index of every key present in Funcom's shipped
`DefaultGame.ini`, see `SERVER_CONFIG_KEY_INDEX.md`. That file lists 2,242 raw
key entries across 156 sections and marks each as known, inferred, UI/visual,
asset/reference, or unknown/investigate.

For binary-only research candidates, see `SERVER_BINARY_CONFIG_CANDIDATES.md`.
That file is generated from strings in `DuneSandboxServer-Linux-Shipping` after
excluding keys already present in `DefaultGame.ini`. Treat those entries as
leads, not confirmed config keys.

For runtime/database notes, see `SERVER_RUNTIME_SURFACES.md`. That file maps
high-value config sections to shipped DB tables/functions and records observed
live state from the local all-maps stack.

Evidence levels:

- Known: present in Funcom's shipped default/setup config or already validated in local use.
- Inferred: exposed by shipped server binaries or naming, but still needs live validation.
- Unknown / investigate: carried from shipped config or repo history, but exact gameplay effect needs testing.

## Current Overrides

| INI section | Key | Current value | Status | Description |
| --- | --- | --- | --- | --- |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `PVPMode` | `Limited` | Known on 1.5.3+ | Client-replicated PvP policy. Replaces the paired `m_bShouldForceEnablePvpOnAllPartitions=False` and `m_bAreSecurityZonesEnabled=True` overrides. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `GatheringAmount` | `1.000000` shared; `3.000000` DD#2 | Known on 1.5.3+ | Client-replicated player/vehicle gathering multiplier. Replaces the two legacy mining console variables. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `bAllowSandstorms` | `True` | Known on 1.5.3+ | Client-replicated sandstorm permission. Coriolis auto-spawn, damage, shifting, and cycle fields remain map-specific. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `bAllowSandworms` | `True` | Known on 1.5.3+ | Client-replicated sandworm permission. Detailed sandworm/collision behavior remains in `UserEngine.ini`. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `bIsBuildingRestrictionsEnabled` | `False` | Known on 1.5.3+ | Client-replicated building restriction toggle. Replaces `m_bBuildingRestrictionLimitsEnabled=False`. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `BaseBackupToolTimeRestriction` | `0.016667` hours; `0.000000` Survival | Known on 1.5.3+ | Client-replicated base-backup cooldown. Preserves the previous 60-second shared-map value and Survival's command-line zero. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `LandsraadSpecializationXpMultiplier` | `3.000000` | Known on 1.5.3+ | Client-replicated Landsraad specialization mission reward multiplier. |
| `[/Script/DuneSandbox.PvpPveSettings]` | `+m_PvpEnabledPartitions` | commented examples `1`, `2` | Unknown / investigate | Per-partition PvP allow-list. Present as commented examples in the shipped setup config. Needs live validation before enabling. |
| `[/Script/DuneSandbox.PingSystemSettings]` | `m_PingsPerPlayerLimit` | `10` | Inferred / validate | Private-server group QoL override. Shipped default is `5`; raises simultaneous ping capacity for exploration/coordination. |
| `[/Script/DuneSandbox.PingSystemSettings]` | `m_PingMaximumDistance` | `5000.000000` | Inferred / validate | Private-server group QoL override. Shipped default is `2000`; extends ping placement/use distance. |
| `[/Script/DuneSandbox.PingSystemSettings]` | `m_PingInWorldMarkerExpiryTime` | `15` | Inferred / validate | Private-server group QoL override. Shipped default is `5`; keeps in-world pings visible longer. |
| `[/Script/DuneSandbox.PingSystemSettings]` | `m_PingMapMarkerExpiryTime` | `300` | Inferred / validate | Private-server group QoL override. Shipped default is `60`; keeps map pings visible longer. |
| `[/DeteriorationSystem.ItemDeteriorationConstants]` | `UpdateRateInSeconds` | `1.0` | Known | Item deterioration tick/update cadence in seconds. Lower values update more frequently; higher values update less frequently. |
| `[/Script/DuneSandbox.SpiceHarvestingSystem]` | `m_PerMapSystemSettings` | Deep Desert small `60/60`, medium `24/24`, large `3/3`; Survival small `5/5` | Known / validate balance | Private-server spice caps. Large fields are raised from shipped `1/1` to `3/3`; medium fields are raised to `24/24` for a busier Deep Desert. |
| `[/Script/DuneSandbox.ContractsSubsystem]` | `m_MaxGlobalContractsNumberPerServer` | `20` | Inferred / validate | Private-server activity override. Shipped default is `10`; keeps more global contracts available on low-pop worlds. |
| `[/Script/DuneSandbox.SandStormConfig]` | `m_bCoriolisAutoSpawnEnabled` | `False` | Known | Controls automatic Coriolis storm spawning. Shipped setup default is `True`; this repo disables auto-spawn. |
| `[/Script/DuneSandbox.PlayerOnlineStateSettings]` | `m_DefaultReconnectGracePeriodSeconds` | `0` | Known | Normal-map reconnect grace period before the server stops preserving an offline/disconnected session. Shipped default observed in build `1963158` is `300`. |
| `[/Script/DuneSandbox.PlayerOnlineStateSettings]` | `m_OvermapReturnGracePeriodSeconds` | `0` | Known | Overmap return/reconnect grace period. Shipped default observed in build `1963158` is `90`. |
| `[/Script/DuneSandbox.PlayerOnlineStateSettings]` | `m_InstancedMapReconnectGracePeriodSeconds` | `0` | Known | Instanced-map reconnect grace period. Shipped default observed in build `1963158` is `300`. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_MaxLandclaimSegmentsPerMap` | `(((Name="HaggaBasin"), 6),((Name="Survival_1"), 6),((Name="DeepDesert"), 6),((Name="DeepDesert_1"), 6))` | Inferred / validate | Map-level landclaim segment/base-distribution candidate. Deep Desert entries are a lab candidate for landclaim-backed Base Reconstruction Tool placement; this is not the observed per-player 3 subfief/totem cap. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_MaxNumLandclaimSegments` | `10` | Known | Maximum connected landclaim segments per base/landclaim. Shipped default is `6`. Funcom's setup comment says this must also be applied to each client. |
| `[/Script/DuneSandbox.UserServerCustomSettings]` | `BuildingPieceLimitMultiplier` | `2.000000` | Applied on 1.5.3.4 / validate balance | Supported server-custom setting. Production image `2124138-0-shipping` (Steam build `25486303`) has `DifficultyLevel=Custom` and `2.000000` in both `Saved/UserSettings/ServerCustomSettings.ini` and `Saved/Config/LinuxServer/ServerCustomSettings.ini`. This is intended to raise the observed 5000 default to 10000; exact multiplier-to-cap behavior still needs in-game placement proof. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_LimitNumberOfBuildablesPerServer` | `7500` | Legacy candidate / superseded | Older binary-only total buildable/building-piece cap candidate. Not the current production path after 1.5.3 added `BuildingPieceLimitMultiplier`. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_LimitNumberOfBuildablesPerMap` | `(((Name="HaggaBasin"), 7500),((Name="Survival_1"), 7500))` | Legacy candidate / superseded | Older binary-only per-map total buildable/building-piece cap candidate. Not the current production path after 1.5.3 added `BuildingPieceLimitMultiplier`. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildingBlueprintMaxExtensions` | `4` | Known | Maximum number of times a building blueprint / landclaim can be expanded. Shipped default is `4`. This is not the active base-count knob. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BaseBackupMaxExtensions` | `8` | Known | Maximum extension count used by base backup/reconstruction-related data. Shipped default is `8`. This is not the active base-count knob. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BaseBackupToolMapRestriction` | `((Name="HaggaBasin"), (Name="Survival_1"), (Name="DeepDesert"), (Name="DeepDesert_1"), (Name="Editor_Default"), (Name="IGW_Test_Small"))` | Inferred / validate | BRT map allow-list candidate. Shipped/default evidence excluded Deep Desert, matching the "base backup not allowed in this region" symptom better than the landclaim segment limit alone. |
| `[/Script/DuneSandbox.CharacterRecustomizerSubsystem]` | `m_CostAmount` | `0` | Known | Solaris cost for character recustomization. Shipped/default observed in build `1963158` is `5000`; this repo makes it free. |
| `[/Script/DuneSandbox.DuneExchangeSettings]` | `SellOrderDailySolarisFee` | `0` | Known / validate balance | Private-server economy override. Shipped default is `20`; this repo keeps exchange listing friction removed. |
| `[/Script/DuneSandbox.DuneExchangeSettings]` | `SellOrderPricePercentageFee` | `0.000000` | Known / validate balance | Private-server economy override. Shipped default is `2.000000`; this repo keeps exchange listing friction removed. |
| `[/Script/DuneSandbox.GuildSettings]` | `m_MaxGuildsAllowed` | `999` | Inferred / validate | Private-server social override. Shipped default is `3`; removes the practical guild count cap for this community. |

## Building Knob Notes

- Target policy for this repo: preserve the current `12` connected landclaim segments per base while investigating map-level landclaim distribution.
- Segment cap: `m_MaxNumLandclaimSegments=12` remains a server-side legacy override. It is not silently translated to `FiefdomLimit`, because the documented client-facing custom range does not preserve the value `12` and the two settings represent different landclaim concepts.
- Map-level landclaim candidate: `m_MaxLandclaimSegmentsPerMap=...6...`.
- Total buildable/building-piece cap experiment: `m_LimitNumberOfBuildablesPerServer=7500` plus `m_LimitNumberOfBuildablesPerMap=...7500...`. Binary evidence is strong for the key names and `UBuildingSettings` ownership, but Funcom does not ship default values in `DefaultGame.ini`; live validation is required. Confidence is only low that these are the per-base `5000` piece cap, because live behavior allows multiple 5000-piece bases in one Hagga Basin map.
- The old per-base piece-cap lead was the cooked data table `/Game/Dune/Systems/Building/Data/DT_BuildableStructureCategoryData`, row/category `BuildingPiece`, with an observed literal `5000`. On `2111270-0-shipping` its PAK signature failed before startup. Current production `2124138-0-shipping` uses `BuildingPieceLimitMultiplier` with `DifficultyLevel=Custom`; `scripts/patch-building-piece-limit-pak.py` remains for older image builds and lab comparison only.
- `m_BuildingBlueprintMaxExtensions` and `m_BaseBackupMaxExtensions` are extension/reconstruction limits, not the active base-count cap.
- The per-player 3 subfief/totem cap is not `m_MaxLandclaimSegmentsPerMap`. Binary evidence points to `SubfiefLimitBonus` / `SubfiefCount` as player attribute/UI state. This repo exposes `DUNE_SUBFIEF_LIMIT` as an experimental operator knob through `scripts/apply-subfief-limit-knob.sh`; the player-presence announcer also repairs joined/rejoined current pawn actors whose bonus is below the configured value.

## Nearby Shipped Keys

These keys are present in Funcom's shipped default config near the settings this
repo already overrides. They are not currently overridden here unless listed in
the current override table above.

| INI section | Key | Shipped value | Status | Description |
| --- | --- | --- | --- | --- |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildingBlueprintRangeMultiplier` | `0.660000` | Unknown / investigate | Likely scales building blueprint range or allowed extension distance. Needs live validation. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildingCategoryLimitWarningPercentageVisible` | `0.650000` | Inferred | UI warning threshold for category/building-limit pressure. Likely visual only. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_PlacementHelperAliveThresholdTimeInSecs` | `900.000000` | Inferred | Lifetime threshold for placement-helper state. Exact player-facing effect needs validation. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildingSystemSecurityZoneUpdateRateInSeconds` | `1.000000` | Known | Server update cadence for building/security-zone interaction checks. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_NearbyBuildingDetectionHeightRange` | `25000.000000` | Inferred | Vertical range used when detecting nearby buildings. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_DefaultBuildAndFillTimeInSeconds` | `0.500000` | Inferred | Default build/fill interaction time. Exact coverage by buildable type needs validation. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildAndFillStartThresholdTimerInSeconds` | `0.200000` | Inferred | Delay/threshold before build/fill action starts. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildableBuildAndFillHoldTimes` | `Short=0.875`, `Medium=1.250`, `Long=2.000`, `VeryLong=2.000` | Inferred | Hold-time buckets for build/fill interactions. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_BuildRange` | `2000.000000` | Known | Player build placement range in centimeters. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_bEnableBuildingNearServerBorders` | `False` | Known | Allows/disallows building near server/map borders. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_bMinBuildableDistanceFromServerBorder` | `1000.000000` | Inferred | Minimum build distance from a server border. Name looks boolean-prefixed, but value is numeric in shipped config; treat carefully. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_bCustomMinBuildableDistanceFromServerBorder` | `(((Name="DeepDesert"), 10000.000000))` | Inferred | Per-map custom minimum build distance from server border. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_LandclaimThresholdDistance` | `512.000000` | Inferred | Distance threshold used by landclaim placement/validation. Needs live testing before changing. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_LandclaimFoundationDistanceFromBorder` | `35.000000` | Inferred | Required foundation distance from a landclaim border. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_ThresholdDistanceVFXTotemRadius` | `5000.000000` | Inferred | Radius for landclaim/totem threshold visual effects. Likely visual feedback. |
| `[/Script/DuneSandbox.BuildingSettings]` | `+m_StakingUnitExtensionDefaultTimes` | `60` through `30720` | Inferred | Default horizontal staking-unit/landclaim extension durations. Units appear to be seconds. |
| `[/Script/DuneSandbox.BuildingSettings]` | `+m_StakingUnitVerticalExtensionDefaultTimes` | `60` through `30720` | Inferred | Default vertical staking-unit extension durations. Units appear to be seconds. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_bCanRemoveBuildablesWithNoOwner` | `True` | Known | Allows server/game systems to remove ownerless buildables. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_StakingUnitType` | `(Name="StakingUnit_Placeable")` | Known | Item/template name for the normal staking unit. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_StakingUnitVerticalType` | `(Name="StakingUnitVertical_Placeable")` | Known | Item/template name for the vertical staking unit. |
| `[/Script/DuneSandbox.PlayerOnlineStateSettings]` | `m_DefaultReconnectGracePeriodSeconds` | `300` | Known | Shipped normal-map reconnect grace default. This repo overrides to `0`. |
| `[/Script/DuneSandbox.PlayerOnlineStateSettings]` | `m_OvermapReturnGracePeriodSeconds` | `90` | Known | Shipped overmap return grace default. This repo overrides to `0`. |
| `[/Script/DuneSandbox.PlayerOnlineStateSettings]` | `m_InstancedMapReconnectGracePeriodSeconds` | `300` | Known | Shipped instanced-map reconnect grace default. This repo overrides to `0`. |

## Other High-Value Shipped Sections

The exhaustive index has every key. These are the sections most likely to matter
for self-host tuning, based on shipped section/key names and defaults.

| INI section | Key(s) | Shipped value / summary | Status | Description |
| --- | --- | --- | --- | --- |
| `[/Script/Engine.GameNetworkManager]` | movement/network timing keys | `ClientErrorUpdateRateLimit=0.35f`, `MaxMoveDeltaTime=0.25f`, movement discrepancy checks enabled | Inferred | Unreal network movement correction/throttling. Useful only if diagnosing movement/network desync; risky to tune blindly. |
| `[/Script/DuneSandbox.MapFpsSettings]` | `+m_Maps` | 32 map FPS caps, commonly `20` for gameplay maps and `10` for hubs/overland | Inferred | Per-map dedicated server FPS cap. Candidate performance knob, but changing it changes CPU budget and simulation cadence. |
| `[/Script/DuneSandbox.MapFeatures]` | `m_Maps` | Per-map feature flags: taxation, Deep Desert gameplay, shifting sands, social-only, story gameplay, instancing, outdoors, transfer flags | Inferred | Map capability matrix. High impact; do not override casually. |
| `[/Script/DuneSandbox.PingSystemSettings]` | ping limits/ranges/expiry | limit `5`, max distance `2000`, world expiry `5`, map expiry `60` | Inferred | Player ping system limits and expiry timers. |
| `[/Script/DuneSandbox.ShelterSettings]` | shelter traces/thresholds | trace length `10000`, building threshold `0.9`, placeable threshold `0.65` | Inferred | Shelter detection traces and thresholds. For base-only thirst reduction, prefer making bases count as sheltered instead of zeroing global dehydration. See `HYDRATION_WATER_KNOBS.md`. |
| `[/Script/DuneSandbox.SpiceHarvestingSystem]` | spice field settings | per-map and default spice field active/primed counts | Inferred | Spice field spawning/activation system. Candidate economy/resource knob; needs live validation. |
| `[/Script/DuneSandbox.HydrationSubsystem]` | hydration settings | `m_bHydrationEnabled=True`, hydration settings data asset; binary fields `m_DehydrationPerSecondBase`, `m_DehydrationRateScale`, `ShelteredProtectionThreshold` | Inferred | Player hydration/water behavior. `m_DehydrationPerSecondBase` and `m_DehydrationRateScale` are the global thirst-rate targets; `ShelteredProtectionThreshold` is the better candidate for no dehydration while in base. See `HYDRATION_WATER_KNOBS.md`. |
| `[/Script/DuneSandbox.DewHarvestSettings]` | dew harvesting settings | dew harvesting keys | Inferred | Dew collection behavior. Exact safe knobs need targeted testing. |
| `[/Script/DuneSandbox.CraftingSettings]` | crafting output multipliers | recipe multiplier lists and chance attributes | Inferred | Crafting output/economy tuning. Many values are data-table driven; validate before overriding. |
| `[/Script/DuneSandbox.DuneVehicleSettings]` | vehicle settings | vehicle behavior and recovery-related keys | Inferred | Vehicle behavior, persistence, and recovery candidates. Needs focused testing. |
| `[/DeteriorationSystem.ItemDeteriorationConstants]` | `UpdateRateInSeconds` | `1.0` | Known | Deterioration rate/cadence. Shipped setup says `0=off`. |
| `[/Script/DuneSandbox.SandwormSettings]` | sandworm settings | worm behavior tuning keys | Inferred | Sandworm behavior/threat tuning. High gameplay impact; validate carefully. |
| `[/Script/DuneSandbox.SandStormConfig]` | storm settings | includes `m_bCoriolisAutoSpawnEnabled=True` in shipped setup/default | Known / inferred | Coriolis storm auto-spawn and related storm behavior. |
| `[/Script/DuneSandbox.EncountersSubsystem]` | encounter settings | encounter subsystem keys | Inferred | World encounter spawning/selection. Needs targeted testing. |
| `[/Script/DuneSandbox.ContractsSubsystem]` | contract settings | contract subsystem keys | Inferred | Contract generation/limits/rewards. Needs targeted testing. |
| `[/Script/DuneSandbox.SecurityZonesSubsystem]` | security-zone settings | `m_bAreSecurityZonesEnabled=True` | Known | Security-zone system. Shipped setup says disabling allows PvP and ability use everywhere. |
| `[/Script/DuneSandbox.InventorySystemSettings]` | inventory/economy settings | item stack/resource/vendor/mining settings | Inferred | Inventory/resource/economy behavior. Useful for admin tooling research, but unsafe to bulk-change. |
| `[/Script/DuneSandbox.RespawnSettings]` | respawn maps/location limits | cross-map respawn enabled for Hagga Basin, Deep Desert disables checkpoint group, location group limits all `1` | Inferred | Respawn routing and allowed respawn location groups. High impact. |
| `[/Script/DuneSandbox.CoriolisSubsystem]` | Coriolis cycle/wipe keys | cycle starts day `3` hour `5`, duration `7` days, restart on cycle end `True`, DB wipe `True` | Known / inferred | Deep Desert Coriolis cycle and wipe behavior. Very high impact; do not change without backup/testing. |
| `[/Script/DuneSandbox.CoriolisSettings]` | `+m_IgnoredMarkersList` | many ignored marker types | Inferred | Marker categories ignored by Coriolis behavior/reset handling. |
| `[/Script/DuneSandbox.PartySettings]` | `m_SocialRange` | `1000000.000000` | Inferred | Social/party interaction range. |
| `[/Script/DuneSandbox.LootSettings]` | `GlobalLootRightsBehaviour` | `PerPlayerChestAndNpcDrop` | Known / inferred | Global loot-rights behavior. Strong candidate for loot policy tuning. |
| `[/Script/DuneSandbox.GuildSettings]` | guild limits | creation cost `1000`, max guilds `3`, max members `32`, pending invites `10` | Inferred | Guild economy and capacity limits. Good self-host candidate after live validation. |
| `[/Script/DuneSandbox.PlayerRequestSubsystem]` | request timeout/static data | notification timeout buffer `0.25` | Inferred | Player request UI/interaction behavior, such as duel/share request notifications. |
| `[/Script/DuneSandbox.TravelDestinationSubsystem]` | travel destination asset | data asset reference | Asset/reference | Travel destination data source. Important for routing research, but not a simple scalar knob. |

## Runtime/Database Findings

These are not additional config keys; they are persistence surfaces found while
validating the shipped config.

| Area | DB evidence | Status | Why it matters |
| --- | --- | --- | --- |
| Spice fields | `spicefield_types`, `spicefield_server_availability`, `resourcefield_state`; functions such as `try_prime_spicefield`, `try_spawn_spicefield`, `update_global_spice_field_rules` | Known | Confirms `m_PerMapSystemSettings` caps are authoritative and live-observable. |
| Ordinary resource nodes | `actor_spawners`, `actor_spawner_actors`; runtime logs for `BP_AzuriteOre_*_Spawner`, `BP_ScrapMetal_*_Spawner`, and `BP_ImpureFuel_*_Spawner` | Inferred / investigate | Resource-node spawn chance keys are shipped, and binary strings prove `FResourceRespawnTimer`, but no safe respawn timer override is confirmed yet. See `RESOURCE_RESPAWN_KNOBS.md`. |
| Items/inventory | `items`, `inventories`, `actor_inventories`; functions such as `load_items`, `update_inventory`, `move_inventory_item`, `merge_inventory_items` | Known | Admin item grants should account for inventory ownership, free/mergeable slots, stack size, stats JSON, and quality. |
| Encounters | `encounters_static`; static encounter load/save functions only | Known / limited | Confirms static encounter persistence exists, but dynamic random encounter weights/caps are not in DB. |
| Buried treasure | No treasure tables/functions found; empty `DUNE-153318_nuke_persisted_buried_treasure.sql` migration | Inferred | Supports the current conclusion that buried treasure is runtime/asset driven in this build. |
| Patrol/crash sites | No patrol/crash-site tables/functions found | Inferred | Frequency/selection likely lives in level assets or runtime subsystem state, not Postgres. |
| Respawn | `player_respawn_locations`, `travel_return_info`, `player_travel_state` | Known | Provides the DB side for respawn/travel admin investigation. |
| Guilds | `guilds`, `guild_members`, `guild_invites` plus guild functions | Known | Config limits have matching persistence surfaces. |
| Vehicles | `vehicles`, `vehicle_modules`, `vehicle_module_inventories`, `backup_vehicles`, `recovered_vehicles` | Known / needs live examples | Schema is known, but this test farm has no vehicle rows yet. |
| Access codes | `player_access_codes` plus create/get/reset/delete functions | Known | Good candidate for admin-panel management through DB functions. |

## Binary-Only Keys Found So Far

These keys were found in the shipped server binary but not in the shipped
`DefaultGame.ini`. They can often still be valid Unreal config properties, but
they need live validation because Funcom did not ship default values for them.

| INI section | Key | Evidence | Status | Description |
| --- | --- | --- | --- | --- |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_MaxLandclaimSegmentsPerMap` | Binary contains `m_MaxLandclaimSegmentsPerMap` and `m_MaxLandclaimSegmentsPerMap_Key` near `UBuildingSettings` strings | Inferred / validate | Map-level landclaim candidate, not the per-player 3 subfief/totem cap. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_LimitNumberOfBuildablesPerServer` | Binary contains `m_LimitNumberOfBuildablesPerServer`, `m_LimitNumberOfBuildablesPerMap`, and `m_MaximumNumberOfBuildables` near `UBuildingSettings` / buildable-structure-limit strings | Candidate / validate | Likely total buildable/building-piece cap. Added as an experimental override at `7500` to test raising the observed `5000` limit. Low confidence that this is the per-base cap by itself. |
| `[/Script/DuneSandbox.BuildingSettings]` | `m_LimitNumberOfBuildablesPerMap` | Binary contains `m_LimitNumberOfBuildablesPerMap` and `m_LimitNumberOfBuildablesPerMap_Key` near `UBuildingSettings` / buildable-structure-limit strings | Candidate / validate | Per-map companion to the total buildable cap candidate. Current override targets `HaggaBasin` and `Survival_1`. Low confidence that this is the per-base cap by itself. |
| Cooked data table, likely `/Game/Dune/Systems/Building/Data/DT_BuildableStructureCategoryData` | `BuildingPiece` / `m_MaximumNumberOfBuildables` | Extracted asset strings include `DT_BuildableStructureCategoryData`, `BuildableStructureLimitData`, `BuildingPiece`, `m_BuildableStructureLimitsOnServer`, `m_BuildableStructureLimitsPerMap`, `m_MaximumNumberOfBuildables`, and `m_TargetNumberOfLandclaims`; adjacent payload entry `03505-00ed78` contains int32 `5000` in the `BuildingPiece` row before the `Production` row | Candidate / boot-validated | Best current lead for the per-base building-piece cap. Cooked pak patching to `7500` is implemented and boot-validated in the `kspld0` handoff lab. Actual client placement past `5000` still needs client/RPC validation. |
| Unknown, likely building-related | `m_BuildableStructureLimitsPerMap` | Binary and `DT_BuildableStructureCategoryData` strings contain `m_BuildableStructureLimitsPerMap` and `m_BuildableStructureLimitsPerMap_Key` | Candidate / investigate | Possible buildable structure limit map used by category rows. Needs section/asset override discovery and live validation before use. |
| Unknown, likely admin-related | `m_AdminPasswords` | Binary contains `m_AdminPasswords` and `m_AdminPasswords_Key` | Candidate / investigate | Possible admin-password map/list used by an internal admin path. Do not use until section and auth behavior are understood. |

## Validation Notes From This Pass

- I did not mark binary-only strings as validated unless they also appear in shipped config or have strong section evidence.
- `_Key` companion strings are useful evidence that a property is a map/set-like Unreal field, but they are not themselves the override key.
- The exhaustive shipped-config index is stronger evidence than the binary-only candidate index.
- Live validation still requires a restart and in-game test, especially for building, inventory, travel, and Coriolis/wipe behavior.

## Validation Checklist

- Restart the server after changing `config/UserGame.ini`; these are startup config values.
- Do not use `m_MaxLandclaimSegmentsPerMap` as the validation target for the 3 subfief/totem cap; that cap appears to be driven by player attributes (`SubfiefLimitBonus` / `SubfiefCount`) or cooked game data.
- Validate the retained `m_MaxNumLandclaimSegments=12` behavior with a client carrying the same legacy setting if segment placement is tested; this setting was intentionally not migrated to the custom file.
- Validate `m_LimitNumberOfBuildablesPerServer` / `m_LimitNumberOfBuildablesPerMap` by restarting affected map containers and attempting to build past 5000 pieces. These are binary-only candidates, not shipped default keys, and probably represent a broader map/server layer rather than the per-base cap.
- Continue per-base piece-cap research in `DT_BuildableStructureCategoryData` / `BuildingPiece`; this is currently stronger evidence than the map/server INI candidate.
- Avoid changing `m_BuildingBlueprintMaxExtensions` or `m_BaseBackupMaxExtensions` while testing base count; they control extension/reconstruction limits, not the active base cap.

## Source Evidence

- Repo override file: `config/UserGame.ini`.
- Shipped setup file: `Dune Awakening Self-Hosted Server/scripts/setup/config/UserGame.ini`.
- Shipped default config extracted from server image: `DuneSandbox/Config/DefaultGame.ini`.
- Shipped server binary string evidence: `DuneSandboxServer-Linux-Shipping` contains `m_MaxLandclaimSegmentsPerMap` and `m_MaxLandclaimSegmentsPerMap_Key`.
