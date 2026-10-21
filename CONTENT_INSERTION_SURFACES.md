# Content Insertion Surfaces

This is the canonical catalog for DASH content/admin expansion. Confidence levels are `high`, `moderate`, `low`, or `unknown`.

For a concise operator view of what is actionable now versus evidence-only, see [`docs/admin-actionability-matrix.md`](docs/admin-actionability-matrix.md).

Evidence rules:

- Shipped config plus live database behavior is strong evidence.
- Binary strings are leads only until the owning section, syntax, and runtime effect are proven.
- Public websites are candidate lookup sources, not authoritative local server evidence.
- New write paths must start read-only or dry-run unless an execution gate, confirmation phrase, and audit event are present.

| Surface | Capability | Evidence | Confidence | Mutation risk | Restart required | Validation command | Rollback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Config/INI knobs | Deep Desert spice field caps through `[/Script/DuneSandbox.SpiceHarvestingSystem] m_PerMapSystemSettings`. | `DEEP_DESERT_EVENT_KNOBS.md`, `SERVER_RUNTIME_SURFACES.md`, `dune.spicefield_types`, `dune.resourcefield_state`. | high | medium | yes | `select * from dune.spicefield_types order by map, field_kind_id;` | Restore backed-up `UserGame.ini` and restart `deep-desert`. |
| Config/INI knobs | Client-replicated server-custom policy: `PVPMode`, `GatheringAmount`, sandstorm/sandworm permissions, building restrictions, backup cooldown, and Landsraad specialization XP. | `config/UserServerCustomSettings*.ini`, official 1.5.3+ client-replication behavior, `docs/server-knobs-audit.md`. | high | medium | yes | `rg -n 'PVPMode|GatheringAmount|bAllowSandstorms|bAllowSandworms|bIsBuildingRestrictionsEnabled|BaseBackupToolTimeRestriction|LandsraadSpecializationXpMultiplier' config/UserServerCustomSettings*.ini` | Restore backed-up custom-setting overlays and restart affected maps. |
| Config/INI knobs | Sandstorm and Coriolis map-specific toggles. Cycle seed, DB wipe, and cycle-end restart fields are excluded from typed writes. | `config/UserGame.ini`, `config/UserEngine.ini`, `SERVER_RUNTIME_SURFACES.md`. | high | medium | yes | `rg -n 'Sandstorm|Coriolis' config/UserGame.ini config/UserEngine.ini` | Restore backed-up config and restart affected maps. |
| Config/INI knobs | Server-side-only resource/combat settings with no direct client-replicated equivalent: `SecurityZones.PvpResourceMultiplier`, vehicle wear, treasure, collision, danger-zone, and invulnerability values. | `config/UserEngine.ini`, `docs/server-knobs-audit.md`. | high | low | yes | `rg -n 'PvpResourceMultiplier|Vehicle\.|Sandworm\.' config/UserEngine.ini` | Restore backed-up `UserEngine.ini` and restart maps. |
| Config/INI knobs | PvP partition allow-list and other map-specific game settings without a value-preserving custom equivalent. | `config/UserGame.ini`, `SERVER_CONFIG_KEYS.md`. | high | medium | yes | `rg -n 'PvpEnabledPartitions|MapFeatures|Coriolis' config/UserGame.ini` | Restore backed-up `UserGame.ini` and restart maps. |
| Config/INI knobs | Shelter/hydration candidates. | `HYDRATION_WATER_KNOBS.md`, shipped ShelterSettings section, local candidate overrides. | low to moderate | experimental | yes | Live in-base/outside hydration test after restart. | Restore backed-up `UserGame.ini` and restart maps. |
| Database state | Currency, Solari, XP, and item grants as a bundled transaction plan or targeted Solari helper. | Existing DASH currency/XP/item grant paths, `/api/admin/solari/inventory`, `/api/admin/solari/bank`, and `docs/admin-mutation-map.md`. | high for carried Solari item stacks, moderate for bank/Exchange | medium to high | no | `/api/admin/bundle` with `dry_run=true`; `/api/admin/solari/inventory` with `dry_run=true`; `/api/admin/solari/bank` with `dry_run=true`. | Manual compensating edits from audit record. |
| Database state | Faction reputation planning and gated writes through `dune.set_player_faction_reputation`. | `dune.set_player_faction_reputation`, `dune.get_player_current_faction_reputation`, `dune.player_faction_reputation`. | moderate-to-high | high | no | `/api/admin/faction-reputation` with `dry_run=true`; `select * from dune.player_faction_reputation where actor_id=<pawn_id>;` | Run the endpoint again with `mode=set` and the previous value from the audit record. |
| Database state | Offline player recovery through `dune.admin_move_offline_player_to_partition`. | Local schema function reference and player location audit work. | moderate | high | no | `select * from dune.player_state where account_id=<id>;` before and after. | Move back to the prior partition recorded in the audit result. |
| Database state | Journey story-node planning and gated reveal/complete/reset/delete calls. | `dune.admin_get_journey_details`, `dune.reveal_journey_story_nodes_for_player`, `dune.complete_journey_story_nodes_for_player`, `dune.reset_journey_story_nodes_for_player`, `dune.delete_journey_story_nodes_for_player`. | moderate | high | no | `/api/admin/journey` with `dry_run=true` and known story node ids. | Use the opposite journey action where meaningful; audit before/after details. |
| Database state | Recipe and vehicle function discovery. | `pg_proc` discovery through `/api/admin/progression/inspect`, existing DB observations. | moderate for discovery, low for writes | blocked | unknown | Map safe DB functions or live examples first. | No execution in v1. |
| RabbitMQ/admin/GM routes | Verified chat announcements. | Existing announcement scheduler and `scripts/verify-announcement.sh`. | high | low | no | `./scripts/verify-announcement.sh` | No persistent rollback; audit records delivery. |
| RabbitMQ/admin/GM routes | Candidate native GM command envelopes. | `scripts/dune_gm_command.py`, command strings, allowed command catalog. | low | blocked | no | Keep execution blocked until `DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true` is proven by live-client validation. | No execution in v1. |
| Existing content activation | Encounters, patrol ships, treasure, and resource fields. | `DEEP_DESERT_EVENT_KNOBS.md`, `RESOURCE_RESPAWN_KNOBS.md`, runtime surface notes. | moderate for shipped toggles, low for new activation weights | medium to high | yes | Compare logs and DB state before/after restart. | Restore config backups; avoid raw DB writes unless separately validated. |
| Hard limits | True new maps, cooked assets, physics, or algorithms. | Current server surface audit shows no admin-only route for new cooked content. | high | blocked | yes/build-time | Not applicable to DASH v1. | Requires cooked client/server assets or binary/plugin work. |

## DASH API Mapping

- Read-only catalog: `/api/catalog/surfaces`, `/api/catalog/evidence`, `/api/catalog/validation`.
- Typed knob inventory and dry-run: `/api/settings/typed-knobs`.
- Typed knob writes: require `DUNE_ADMIN_MUTATIONS_ENABLED=true`, `DUNE_ADMIN_TYPED_KNOBS_ENABLED=true`, and confirmation `WRITE TYPED KNOBS`.
- Economy bundle plans: `/api/admin/bundle`, default `dry_run=true`; execution also requires `DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED=true` and confirmation `EXECUTE BUNDLE`.
- Solari grants: `/api/admin/solari/inventory` and `/api/admin/solari/bank`, default `dry_run=true`; inventory execution requires confirmation `GRANT SOLARI`, bank execution uses the Exchange gate and confirmation `WRITE EXCHANGE`.
- Offline player recovery: `/api/admin/player-recovery/offline-teleport`, default `dry_run=true`; execution requires confirmation `MOVE OFFLINE PLAYER`.
- Progression inspection: `/api/admin/progression/inspect`.
- Faction reputation planning: `/api/admin/faction-reputation`, default `dry_run=true`; execution requires `DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED=true` and confirmation `WRITE REPUTATION`.
- Journey story-node planning: `/api/admin/journey`, default `dry_run=true`; execution requires `DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED=true` and confirmation `WRITE JOURNEY`.
- Character slot inspection/planning: `/api/admin/character-slots`, `/api/admin/character-slots/plan`, `/api/admin/character-slots/execute`; execution stays blocked unless a validated native lifecycle path is discovered, and would require `DUNE_ADMIN_CHARACTER_SWAP_ENABLED=true` plus confirmation `SWAP CHARACTER`.
- Spice/resource inspection: `/api/admin/spice-fields/inspect`.
- Event planner: `/api/events`, `/api/events/dry-run`, `/api/events/cancel`, `/api/events/run`; execution requires `DUNE_ADMIN_EVENT_EXECUTION_ENABLED=true`.

## Implementation Status

Implemented in `admin/admin_panel.py`:

- Catalog data model and read-only catalog endpoints.
- Catalog UI tab.
- Typed knob registry, validation, dry-run, backup-before-write, and restart metadata.
- Dry-run-first economy bundle planner.
- Offline player recovery dry-run and gated execution path using the mapped `dune.admin_move_offline_player_to_partition(...)` function.
- Progression surface inspection for faction, reputation, journey, recipe, and vehicle DB evidence.
- Dry-run-first faction reputation mutator using `dune.set_player_faction_reputation`, runtime schema checks, and a separate execution gate.
- Dry-run-first player faction-change mutator using `dune.change_player_faction`, faction id validation, offline-only execution, and a separate execution gate.
- Dry-run-first journey story-node mutator using server-provided reveal/complete/reset/delete functions and a separate execution gate.
- Dry-run-first Landsraad term mutator using first-party term end-time and force-end functions with a separate execution gate.
- Dry-run-first respawn-location delete mutator using `get_respawn_locations` plus `update_respawn_locations`; arbitrary respawn creation/editing remains blocked.
- Read-only world-state inspector for guild, vehicle, marker, landclaim, recipe, and respawn evidence.
- Dry-run-first guild mutator for description and member role changes through first-party guild functions; disband/remove/invite operations remain blocked.
- Dry-run-first marker deletion mutator for marker IDs and static-location keys through first-party marker functions; marker creation/editing remains blocked.
- Dry-run-first landclaim segment mutator for adding a segment to a known totem; rollback requires backup/manual repair because no delete function is mapped.
- Read-only economy inspector for Dune Exchange, vehicles, recovered/backup vehicles, and base backups.
- Dry-run-first Dune Exchange Solari balance mutator through first-party exchange balance functions; order add/fulfill/cancel/relist remains blocked.
- Read-only player lifecycle inspector for account/player, party, tags, access codes, Communinet, dungeon, tutorial, and lifecycle evidence.
- Character slot inspector and planner for active/new/switch/restore workflows. It refuses online targets and does not execute without a proven native DB lifecycle contract.
- Dry-run-first player tag and access-code mutators through first-party functions; account deletion/takeover, party membership, Communinet, dungeon, tutorial, and raw player save remain blocked.
- Dry-run-first Communinet player/channel mutator through first-party functions; vendor, tutorial, lore, dungeon, overmap, and Coriolis writes remain blocked.
- Dry-run-first tutorial entry mutator through `create_or_update_tutorial_entry`; bulk tutorial deletion and tutorial registration remain blocked.
- Dry-run-first permission actor mutator for name/access level/player rank through first-party functions; permission actor register/takeover/destroy remains blocked.
- Dry-run-first vendor stock-cycle timestamp mutator through `update_vendor_timestamp_for_player`; vendor purchase counts and stock cleanup remain blocked.
- Read-only spice/resource field inspection.
- Event definition persistence under `backups/admin-panel/events.json`.
- Event dry-run planner and explicit execution gate.

The implementation intentionally keeps catalog reads independent from write gates. This lets an operator expose evidence and validation data without enabling mutation paths.

## Gates and Confirmation Phrases

| Gate | Default | Scope |
| --- | --- | --- |
| `DUNE_ADMIN_CATALOG_ENABLED` | `true` | Read-only catalog endpoints. |
| `DUNE_ADMIN_TYPED_KNOBS_ENABLED` | `false` | Typed config writes. Typed dry-runs remain available. |
| `DUNE_ADMIN_EVENT_EXECUTION_ENABLED` | `false` | Event run execution. Event creation and dry-runs remain available. |
| `DUNE_ADMIN_BUNDLE_MUTATIONS_ENABLED` | `false` | Economy bundle execution. Bundle dry-runs remain available. |
| `DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED` | `false` | Faction reputation execution. Progression inspection and reputation dry-runs remain available. |
| `DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED` | `false` | Journey story-node execution. Journey dry-runs and progression inspection remain available. |
| `DUNE_ADMIN_FACTION_MUTATIONS_ENABLED` | `false` | Player faction-change execution. Faction dry-runs and progression inspection remain available. |
| `DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED` | `false` | Landsraad term execution. Landsraad dry-runs and progression inspection remain available. |
| `DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED` | `false` | Respawn-location deletion. Respawn dry-runs and player detail inspection remain available. |
| `DUNE_ADMIN_GUILD_MUTATIONS_ENABLED` | `false` | Guild description and role execution. Guild inspection and dry-runs remain available. |
| `DUNE_ADMIN_MARKER_MUTATIONS_ENABLED` | `false` | Marker deletion execution. Marker inspection and dry-runs remain available. |
| `DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED` | `false` | Landclaim segment execution. Landclaim inspection and dry-runs remain available. |
| `DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED` | `false` | Dune Exchange Solari balance execution. Economy inspection and dry-runs remain available. |
| `DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED` | `false` | Player tag execution. Lifecycle inspection and dry-runs remain available. |
| `DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED` | `false` | Player access-code execution. Lifecycle inspection and dry-runs remain available. |
| `DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED` | `false` | Communinet player/channel execution. Lifecycle inspection and dry-runs remain available. |
| `DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED` | `false` | Tutorial entry execution. Lifecycle inspection and dry-runs remain available. |
| `DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED` | `false` | Permission actor name/access/rank execution. World-state inspection and dry-runs remain available. |
| `DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED` | `false` | Vendor stock-cycle timestamp execution. Lifecycle inspection and dry-runs remain available. |
| `DUNE_ADMIN_CHARACTER_SWAP_ENABLED` | `false` | Character slot swap execution. Inspection and planning remain available; execution is blocked until a native path is proven. |
| `DUNE_ADMIN_MUTATIONS_ENABLED` | example default `false` | Existing global mutation gate. |
| `DUNE_ADMIN_ITEM_GRANTS_ENABLED` | example default `false` | Item grant and bundle item execution. |
| `DUNE_ADMIN_GM_COMMANDS_ENABLED` | `false` | Native GM command execution. Still also blocked by payload verification. |
| `DUNE_GM_COMMAND_PAYLOAD_VERIFIED` | `false` | Required for native GM command execution. |

| Confirmation | Used by |
| --- | --- |
| `WRITE TYPED KNOBS` | Typed config writes. |
| `EXECUTE BUNDLE` | Economy bundle execution. |
| `MOVE OFFLINE PLAYER` | Offline player recovery execution. |
| `WRITE REPUTATION` | Faction reputation execution. |
| `WRITE JOURNEY` | Journey story-node execution. |
| `CHANGE FACTION` | Player faction-change execution. |
| `WRITE LANDSRAAD` | Landsraad term execution. |
| `DELETE RESPAWN` | Respawn-location deletion. |
| `WRITE GUILD` | Guild description and role execution. |
| `DELETE MARKERS` | Marker deletion execution. |
| `WRITE LANDCLAIM` | Landclaim segment execution. |
| `WRITE EXCHANGE` | Dune Exchange Solari balance execution. |
| `WRITE PLAYER TAGS` | Player tag execution. |
| `WRITE ACCESS CODES` | Player access-code execution. |
| `WRITE COMMUNINET` | Communinet player/channel execution. |
| `WRITE TUTORIAL` | Tutorial entry execution. |
| `WRITE PERMISSION` | Permission actor name/access/rank execution. |
| `WRITE VENDOR` | Vendor stock-cycle timestamp execution. |
| `SWAP CHARACTER` | Character slot hibernation/switch execution, still blocked until a native path is proven. |
| `RUN GM COMMAND` | Native GM command execution, still blocked until payload verification. |

## Typed Knob Registry

High-confidence controls:

- `spiceDeepDesertCaps`
- `pvpMode`
- `gatheringAmount`
- `sandstormsAllowed`
- `sandwormsAllowed`
- `buildingRestrictionsEnabled`
- `baseBackupToolTimeRestrictionHours`
- `landsraadSpecializationXpMultiplier`
- `coriolisAutoSpawnEnabled`
- `pvpResourceMultiplier`

Moderate or experimental controls:

- `sandstormTreasureEnabled`
- `buildingShelterThreshold`
- `placeableShelterThreshold`
- `shelteredProtectionThreshold`

Blocked by design:

- Coriolis cycle seed and duration controls.
- Coriolis DB wipe controls.
- Native GM command execution.
- True new maps/assets/physics/algorithms.

## Validation Checklist

Static:

```bash
python3 -m py_compile admin/admin_panel.py scripts/admin-chat-commands.py scripts/dune_gm_command.py
python3 scripts/test-admin-panel-safe-surfaces.py
make validate
```

`scripts/test-admin-panel-safe-surfaces.py` uses a temporary `ADMIN_WORKSPACE` and does not touch the live config or backup tree. It covers:

- Catalog schema and grouping.
- Catalog evidence and validation payload shape.
- Catalog disabled-gate refusal.
- Read-only inspector metadata for progression, world state, economy, player lifecycle, and spice fields.
- Typed knob validation.
- Backup-before-write behavior for typed config updates.
- Structured Deep Desert spice cap rendering.
- Event dry-run planning.
- Event persistence/cancel.
- Fail-closed event execution when `DUNE_ADMIN_EVENT_EXECUTION_ENABLED` is not set.
- Character-slot discovery, same-owner target validation, switch/restore target requirements, online blockers, missing native contracts, route-level audit/error behavior, and no-backup/no-write behavior when execution remains blocked.
- Dry-run planning and fail-closed gates for the promoted mutator families listed in `docs/admin-actionability-matrix.md`.

HTTP smoke checks:

```bash
curl -sS http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/catalog/surfaces | jq '.enabled, (.surfaces|length)'

curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/settings/typed-knobs \
  -d '{"dry_run":true,"updates":{"gatheringAmount":"2.5"}}' \
  | jq '.dryRun, .planned.gatheringAmount'

curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/events/dry-run \
  -d '{"name":"test","actions":[{"type":"spice-cap-proposal","caps":{"Medium":{"primed":24,"active":24}}}]}' \
  | jq '.dryRun, .event.plan[0].type'

curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/admin/bundle \
  -d '{"dry_run":true,"currency":[],"xp":[],"items":[]}' \
  | jq '.dryRun, (.plan|length)'

curl -sS "http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/admin/character-slots?account_id=456" \
  | jq '.accountId, .offline, .contract.safeNativeSwapPath'

curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}/api/admin/character-slots/plan \
  -d '{"dry_run":true,"account_id":456,"action":"new-character"}' \
  | jq '.dryRun, .executable, .plan.blockers'
```

Manual Deep Desert validation:

1. Inspect current state with `POST /api/admin/spice-fields/inspect`.
2. Preview `spiceDeepDesertCaps`.
3. Enable `DUNE_ADMIN_TYPED_KNOBS_ENABLED=true` only when ready to write.
4. Write typed knob with `WRITE TYPED KNOBS`.
5. Restart `deep-desert`.
6. Query `dune.spicefield_types` and grouped `dune.resourcefield_state`.
7. Roll back from the config backup if behavior is wrong.

## Going Forward

Near-term, high-confidence work:

- Add focused tests around typed knob validation and `set_ini_section_values`.
- Add UI write controls for typed knobs only after operators have used the dry-run panel and confirmed the rendered config output.
- Add event action execution for announcements and restart scheduling by calling the already-safe scheduler functions directly.
- Add export/download of event dry-run plans and catalog entries as JSON for operator review.
- Validate faction reputation execution on disposable/offline character data and document exact table columns observed in the live schema.
- Validate journey reveal/complete/reset/delete on disposable offline character data and build a local story-node id reference from observed `admin_get_journey_details` calls.
- Validate player faction changes on disposable/offline character data and document guild side effects.
- Validate Landsraad end-time changes on disposable/private test terms. Keep force-end manual-only until recovery/term lifecycle effects are fully documented.
- Validate respawn-location deletion on disposable/offline character data. Do not add arbitrary respawn creation until `spawnlocatordescriptor` semantics and restoration are fully documented.
- Validate guild description and role changes on a disposable guild. Keep disband, remove-member, invite, allegiance, and create-guild operations blocked until role IDs, faction side effects, and rollback are documented.
- Validate marker deletion on disposable/static test markers only. Do not add marker creation/editing until `saveplayermarkerdata`, `savemarkerdata`, marker payloads, and ID update semantics are fully documented.
- Validate landclaim segment addition on disposable base/totem data only. Do not add landclaim deletion or permission-actor mutation until ownership and permission side effects are documented.
- Validate Dune Exchange Solari balance changes on disposable player economy data. Do not add sell-order add/fulfill/cancel/relist until inventory locking, order revision, purge/completion types, and rollback are documented.
- Keep vehicle restore and base backup save/recycle/delete blocked until `serverinfo`, `transform`, inventory side effects, and spawned actor ownership are validated end-to-end.
- Validate player tag and access-code mutations on disposable accounts. Keep account delete/takeover, party membership, Communinet changes, dungeon completion deletion, tutorial/lore updates, and raw `save_player` paths blocked until lifecycle side effects and recovery are documented.
- Validate Communinet active/channel changes on disposable accounts. Keep vendor stock/player timestamps, tutorial/lore, dungeon completion, overmap survival, and Coriolis player/map writes blocked until the client-visible effects and rollback are documented.
- Validate tutorial entry updates on disposable player data. Keep bulk tutorial deletion, tutorial registration, permission actor writes, taxation invoice writes, Landsraad task progress writes, and vendor stock writes blocked until client-visible effects and rollback are documented.
- Validate permission actor name/access/rank changes on disposable bases only. Keep permission actor register/takeover/destroy, taxation invoice writes, Landsraad task progress writes, vendor stock writes, lore, dungeon, overmap, and Coriolis writes blocked until side effects and rollback are documented.
- Validate vendor timestamp changes on disposable player/vendor data. Keep `player_purchased_item_from_vendor`, vendor stock cleanup, lore, dungeon, taxation, Landsraad task, overmap, and Coriolis writes blocked until client-visible effects and rollback are documented.

Medium-confidence work:

- Validate `spiceDeepDesertCaps` live across a restart and record before/after DB snapshots in this file.
- Validate shelter/hydration candidates with a controlled in-base/outside test and either promote or demote those typed controls.
- Add a dedicated rollback helper that restores the exact config backup created by a typed knob write.
- Expand the typed layer to safe Director world-open/closing-soon controls after testing against live login/travel behavior.

Blocked until stronger evidence:

- Native GM command execution. Need a verified live-client payload for `UDuneServerCommandSubsystem`.
- Journey, recipe, and vehicle unlock writes. Need safe DB functions or complete table/JSON contracts.
- Ordinary resource-node respawn writes. Current evidence proves machinery exists, not a safe plain-INI respawn override.
- True new maps/assets/physics/algorithms. These require cooked assets, plugin support, binary work, or a newly discovered supported loading route.
