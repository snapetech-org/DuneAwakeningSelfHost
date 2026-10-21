# Specialization XP Multipliers

## Production visible 3x mode

Applied on `kspls0` on 2026-09-06 via the legacy `dw.LandsraadMissionRewardMultiplierSpecializationXP`
console variable; migrated on 2026-09-17 to the 1.5.3+ client-replicated
`ServerCustomSettings.ini` mechanism; the migration was confirmed actually
taking effect and fixed on 2026-09-18 (see "DifficultyLevel gotcha" below).

Production uses the 1.5.3+ client-replicated Landsraad mission reward multiplier:

```ini
[/Script/DuneSandbox.UserServerCustomSettings]
DifficultyLevel=Custom
LandsraadSpecializationXpMultiplier=3.000000
```

The setting is present in all three tracked custom-setting overlays:

- `config/UserServerCustomSettings.ini`
- `config/UserServerCustomSettings.survival.ini`
- `config/UserServerCustomSettings.deep-desert-pvp.ini`

### DifficultyLevel gotcha (2026-09-18)

The 2026-09-17 migration deployed and restarted correctly but a player
reported specialization XP was still 1.0x. Root cause: `ServerCustomSettings.ini`
without an explicit `DifficultyLevel=Custom` key gets treated as a preset
difficulty bundle (observed default: `Medium`), and the game silently
overrides every individual custom key — including `LandsraadSpecializationXpMultiplier`
and `BuildingPieceLimitMultiplier` — back to that preset's defaults. This
happens regardless of whether the override file is written before or after
first boot, or whether it's a wholesale replace or an in-place key merge.

This matches documented behavior from an independent Dune self-host project
(Red-Blink/dune-awakening-selfhost-docker, `docs/console/server-custom-settings.md`):
edits to `Saved/Config/LinuxServer/ServerCustomSettings.ini` must be made
while the map's container is fully stopped ("the game can overwrite it while
shutting down"), and `DifficultyLevel=Custom` must be set for individual keys
to be respected instead of the preset bundle.

Fix verified live on `kspls0` 2026-09-18: stopped `testing-carthag` (low-population
test partition) fully, confirmed the pre-fix file showed `DifficultyLevel=Medium`
/ `LandsraadSpecializationXpMultiplier=1.000000`, deployed the corrected config
with `DifficultyLevel=Custom`, started the container, and confirmed
`Saved/Config/LinuxServer/ServerCustomSettings.ini` (the authoritative file,
not just `Saved/UserSettings/ServerCustomSettings.ini`) held `DifficultyLevel=Custom`
and `LandsraadSpecializationXpMultiplier=3.000000` immediately after boot and
again after 150 seconds of uptime. Rolled out fleet-wide via
`scripts/restart-target.sh all` and spot-checked `survival`, `deep-desert`,
`deep-desert-pvp`, `overmap`, and `arrakeen` post-restart — all five show
`DifficultyLevel=Custom` and `LandsraadSpecializationXpMultiplier=3.000000` in
the authoritative `Config/LinuxServer` file. Popup/in-game reward-amount
confirmation from an actual completed Landsraad mission is still outstanding.

The database trigger `dune.specialization_tracks_apply_3x_xp_rate` is disabled in
the live `dune_sb_1_4_10_0` database. This keeps the native multiplier from
stacking with the database multiplier. In visible mode, a base 125 XP Landsraad
mission reward should display and persist as 375 XP, not 1125 XP.

This is a Landsraad mission reward setting. Disabling the database trigger also
means other specialization XP sources are no longer globally tripled by that
database-side path.

## Activation and validation

Game processes read the custom setting at startup. The production fleet was restarted with
`scripts/restart-target.sh all` after the config deployment, so the setting took
effect immediately after that restart; it does not wait for the next scheduled
downtime.

The live rollout verified:

- the custom setting in all three custom-setting overlays;
- the database trigger state as disabled (`D`); and
- all 35 map containers running after the restart.

No player XP rows were edited. End-to-end popup validation still requires
completing a Landsraad mission; the expected visible reward for a 125 XP base
reward is 375 XP.

The pre-change live config backup is at:

```text
kspls0:/home/keith/Documents/code/DuneAwakeningSelfHost/backups/manual/specialization-xp-visible-20260906T213825Z/
```

## Rollback

Rollback must be performed on `kspls0` and followed by the approved fleet
restart path:

1. Restore or remove the `LandsraadSpecializationXpMultiplier` line from all
   three custom-setting overlays.
2. Re-enable the database trigger:

   ```sql
   ALTER TABLE dune.specialization_tracks
     ENABLE TRIGGER specialization_tracks_apply_3x_xp_rate;
   ```

3. Run `scripts/restart-target.sh all` and verify the trigger and map health.

Do not enable both the native cvar and the database trigger unless a 9x total
multiplier is intentionally desired.

## Operational note

The rollout restart also reported an existing logoff-timer runtime auto-remap
warning for `deep-desert-pvp`; it is unrelated to specialization XP.
