# Network-Disconnect Teleport Runbook

This records the first verified online-adjacent teleport primitive found for DASH.
The mechanism is useful, but it is not a soft disconnect.

Confidence: high for the verified Survival/Hagga Basin teleport test on 2026-05-21. Confidence: moderate for automation and other maps until repeated.

Update 2026-05-24: the non-base logoff wait is a separate server-side logoff timer, not the reconnect grace period. The active build has runtime unsafe-location timer values of `30.0` seconds and `300.0` seconds. A guarded runtime patch script now sets both values to `0.0` on the active `kspls0` Survival and Deep Desert containers.

## Result

The working path is a targeted network timeout followed by a first-party offline DB move. A DB-only presence flip is not enough.

This is a verified teleport mechanism, not a verified soft-reconnect mechanism. The player can be dumped to a disconnected/server-offline style screen and may need to reconnect manually. Use it only when that UX is acceptable, or keep it behind an explicit operator confirmation until a native targeted kick/session-close path is found.

Verified sequence:

1. Identify the player's active UDP game endpoint from Survival logs or packet capture.
2. Drop that player's UDP game traffic inside the target map server network namespace long enough for Unreal to time out the `UNetConnection`.
3. Wait for Survival to update the player from `Online` to `LoggingOut`.
4. Wait for the logoff timer to finish and for `online_status='Offline'`.
5. Call `dune.admin_move_offline_player_to_partition(...)`.
6. Remove the network block.
7. Let the client reconnect or have the player reconnect manually.

The reconnect loaded the moved pawn position and the operator confirmed the character moved.

## Mechanism Notes

The safest verified packet scope is inside the target map server's network namespace, not a broad host or router outage. On the tested Survival server, two exact rules were enough to affect only the target client tuple:

```text
INPUT:  client_ip:client_port -> server_ip:7777
OUTPUT: server_ip:7777 -> client_ip:client_port
```

The tested `DROP` window hit the exact target packets and produced:

```text
UNetConnection::Tick: Connection TIMED OUT. Closing connection.
NetworkFailure: ConnectionTimeout
Updated player TEST_FLS_ID online status to LoggingOut
Updated player TEST_FLS_ID online status to Offline
```

Observed timing on 2026-05-21:

- `12s` targeted `DROP` reached the server timeout threshold and started logoff.
- Survival then kept the character in `LoggingOut` for roughly `30s`.
- The offline move helper succeeded after `online_status='Offline'`.

The 2026-05-24 runtime patch changes the unsafe-location wait target to `0.0` for the active process lifetime. With that patch applied, expected behavior is that unsafe-location disconnects become `Offline` immediately after the game enters `OnLeavingGame`, matching the base/safe-location path. Confidence: moderate-high until repeated live non-base logoff lines show `SetLeavingGameTimer ... to <same timestamp>` in both Hagga and Deep Desert.

Targeted `REJECT --reject-with icmp-port-unreachable` did hit packets but did not make the game session close during the tested window. Host-level `DOCKER-USER` and `FORWARD` rules missed the hairpin path in this deployment; the map server netns rules hit reliably.

## Verified Test Case

Date: 2026-05-21

Target:

- Character: `test player`
- FLS/user id: `TEST_FLS_ID`
- Server id: `o13FuZmcSvCCi5kuofbU5w`
- Map: `Survival_1`
- Partition: `1`
- Dimension: `0`
- Actor rows:
  - controller: `17`
  - player state: `18`
  - pawn: `19`

Observed real disconnect:

```text
2026-05-21 17:16:14 UTC: UNetConnection timed out for CLIENT_IP:CLIENT_PORT, FLS TEST_FLS_ID.
2026-05-21 17:16:14 UTC: player updated to LoggingOut.
2026-05-21 17:16:45 UTC: player updated to Offline.
```

Move staged while offline:

```text
before: X=100000.000 Y=100000.000 Z=9191.031
after:  X=100000.000 Y=112000.000 Z=9191.031
delta:  +12000 Y
```

Reconnect proof from Survival logs:

```text
Player TEST_FLS_ID attempting to log in with stored pawn info id=19 map=Survival_1, dimension=0, location=X=100000.000 Y=112000.000 Z=9191.031
Updated player TEST_FLS_ID online status to Online
Updated TEST_FLS_ID with 1 servers (X=100000.000 Y=112000.000 Z=8470.877): o13FuZmcSvCCi5kuofbU5w
```

Final DB state after reconnect:

```text
online_status=Online
server_id=o13FuZmcSvCCi5kuofbU5w
actor 17: X=100000.000 Y=112000.000 Z=9191.031 serial=1890
actor 18: X=100000.000 Y=112000.000 Z=9191.031 serial=1890
actor 19: X=100000.000 Y=112000.000 Z=9191.031 serial=1890
```

## What Did Not Work

DB-only presence manipulation is a false positive. Setting `dune.encrypted_player_state.online_status='Offline'` and `server_id=null` can trigger DASH/admin-bot presence automation, but it does not prove the client disconnected and does not make Survival release the live pawn.

Setting only `[/Script/DuneSandbox.PlayerOnlineStateSettings]` reconnect grace values to `0` is also insufficient for immediate non-base despawn. Those settings remove reconnect/persistence grace, but live logs showed `ADunePlayerCharacter::SetLeavingGameTimer` still scheduling unsafe-location timers of roughly `30s` in Hagga and `5m` in Deep Desert before recording `logoff_persistence_end_time`.

Observed failures:

- Raw online actor transform updates were overwritten by the live Survival server.
- Updating `travel_return_info` alongside actor transforms did not move the live player.
- Flipping DB presence to `Offline`, calling the offline helper, then restoring `Online` did not move the player when the live connection remained active.
- Closing the player's RabbitMQ connections caused reconnects at the RabbitMQ layer but did not disconnect the game session.
- Guessed native GM/RabbitMQ command payloads were consumed or ignored and did not produce `PrintPos` or teleport behavior.
- Guessed `kick`, `RemoveSessionMember`, and `KickLobbyMember` payloads sent to the admin RPC route and the game server queue were routed or consumed but did not close the target game session.
- Targeted ICMP `REJECT` and conntrack flow deletion did not trigger a useful soft reconnect in the tested deployment.
- Startup `-ExecCmds` executes console commands, but tested command names did not expose a useful online teleport route.
- Live 2026-06-02 probes for `BattlEyeMegaKick`, `ClientReturnToMainMenu`,
  `ClientReturnToMainMenuWithTextReason`, `ClientWasKicked`,
  `RemoveSessionMember`, `KickLobbyMember`, `ServerStartLogOffTimer`, and
  `ClientLogOff` through the observed RMQ/admin routes did not close the target
  game session or produce confirming command logs.
- Direct `gdb` calls into `ADunePlayerController` return-to-menu client RPC
  thunks are rejected as an operational path. They can close the target
  `UNetConnection`, but the live Deep Desert test also crashed the map process
  and left stale duplicate `farm_state` rows for the public map port.

## First-Party SQL Contract

The useful function is:

```sql
dune.admin_move_offline_player_to_partition(
  in_fls_id text,
  in_target_partition_id bigint,
  in_target_location dune.vector
)
```

It explicitly requires:

```sql
dune.is_player_offline(in_fls_id) = true
```

If the player is still `Online` or `LoggingOut`, it raises:

```text
Player must be Offline
```

The function uses `dune.accounts.user` as the FLS/user id. It finds `player_state.player_pawn_id`, resolves the target partition from `dune.world_partition`, then updates the pawn actor transform/map/dimension/partition.

## Operational Shape

A production helper should use a guarded, auditable flow:

```text
resolve target -> snapshot player/actor state -> find active UDP endpoint -> block endpoint
poll for LoggingOut -> poll for Offline -> call admin_move_offline_player_to_partition
unblock endpoint -> poll reconnect/final position
```

Use an advisory lock per account/FLS id. Always clean up network filters in a `finally` path.

The current verified block primitive is timeout-shaped. Name UI and audit events accordingly, for example `network-timeout-teleport`, not `soft-teleport`.

## Immediate Logoff Runtime Patch

The runtime patch is build-specific and process-local. It does not edit the shipped binary or config files, and it must be reapplied after Survival or Deep Desert restarts.

Status as of 2026-06-10: patched live for build
`6f8ca9ee5f3420c0b4c1ef7cefb412347bcba04b` / revision `1988751`. The current
runtime patch offsets are:

- `0x1652e898`: 30-second unsafe-location timer array pointer.
- `0x1652e8b0`: 300-second unsafe-location timer array pointer.
- `0x0d515424`: deadline clamp instruction, original `48 01 c1`, patched to `48 89 c1`.
- `0x0d51587a`: timer duration reload, original `c5 fa 10 45 d4`, patched to `c5 f8 57 c0 90`.
- `0x16530ee0`: client-visible logoff timer array pointer, live value `300 300 0 0`.
- `0x16530f10`: client-visible logoff timer array pointer, live value `300 300 0 0`.
- `0x16530f28`: alternate client-visible array pointer, live value `0 0 0 0`.

Post-apply dry-run on `kspls0` showed all target arrays as `0 0 0 0` and both
code patches present on `dune_server-survival-1`,
`dune_server-deep-desert-1`, and `dune_server-deep-desert-pvp-1`. The live
`.env` has `DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED=true`, so
`scripts/restart-post-start-health.sh`, `scripts/recover-map.sh`, and
`scripts/start-map-with-post-hooks.sh` reapply or verify this process-local patch
after map recovery/restart paths.

Status as of 2026-06-01: patched live for build
`9bf5fbdef43a6d6d64459df973f3d252c01ab4ad`. The old data-pointer offsets moved
in that build; the data-pointer offsets were:

- `0x16521698`: 30-second unsafe-location timer array.
- `0x165216b0`: 300-second unsafe-location timer array.

Live validation on `kspls0` found both pointers in `dune_server-survival-1` and
`dune_server-deep-desert-1`; before patch they contained:

```text
30 30 0 0
300 300 0 0
```

After applying `scripts/patch-logoff-timers-runtime.sh --host kspls0` they read:

```text
0 0 0 0
0 0 0 0
```

The target containers remained running after the data-pointer patch. Confidence
is high that the runtime values are patched to zero. Confidence is moderate-high
that this restores immediate unsafe-location logoff, pending a live player
disconnect proof showing no future `SetLeavingGameTimer` / persistence end time.

Ghidra showed the current call path:

- `ADunePlayerCharacter::OnLeavingGame`: Ghidra `0x0d60f270`, ELF/runtime offset `0x0d50f270`.
- `ADunePlayerCharacter::SetLeavingGameTimer`: Ghidra `0x0d60f810`, ELF/runtime offset `0x0d50f810`.
- Duration accessor called before `SetLeavingGameTimer`: Ghidra `0x13049050`, ELF/runtime offset `0x12f49050`.

The accessor at runtime offset `0x12f49050` returns a pointer-like duration
payload from either `object + 0x800` or `*(object + 0x320) + 0x138`. The
`SetLeavingGameTimer` helper then calls the float-duration reader at runtime
offset `0x0d510870`; that helper chooses between the two moved global pointers
above and reads their first float.

Follow-up validation on 2026-06-01 showed the data-pointer patch alone was not
enough for deliberate client "Exit to main" quits in Deep Desert. The client
still showed the five-minute warning, and a live quit at `2026.06.01-23.11.36`
recorded:

```text
SetLeavingGameTimer ... to 2026.06.01-23.16.11 - reason: OnLeavingGame
RecordLogoffPersistenceEndTime ... to 2026.06.01-23.16.11
```

The current script also patches `ADunePlayerCharacter::SetLeavingGameTimer`
itself at ELF/runtime offset `0x0d50f864`. The original instruction is:

```text
48 01 c1    add rax, rcx
```

That computes `deadline = now + rounded_duration`. The patched instruction is:

```text
48 89 c1    mov rax, rcx
```

That preserves the surrounding function and forces `deadline = now` for both
unsafe disconnects and deliberate exit quits. A live Deep Desert quit after this
clamp at `2026.06.01-23.19.25` recorded:

```text
SetLeavingGameTimer ... to 2026.06.01-23.19.25 - reason: OnLeavingGame
RecordLogoffPersistenceEndTime ... to 2026.06.01-23.19.25
```

The client-side dialog text still displayed the five-minute warning during that
test. Treat that warning as stale UI until client-side assets/config are found.
The server-side persistence deadline was immediate.

Additional validation on 2026-06-01 showed that clamping only the recorded
deadline still allowed the actual timer-manager callback to use the original
duration. A Deep Desert logout at `2026.06.01-23.28.18` recorded an immediate
deadline but stayed `LoggingOut` for roughly five minutes and flipped `Offline`
around `2026.06.01-23.35.07`.

The current script therefore also patches the timer-registration duration reload
inside `SetLeavingGameTimer` at ELF/runtime offset `0x0d50fcba`. The original
instruction is:

```text
c5 fa 10 45 d4    vmovss -0x2c(%rbp),%xmm0
```

That reloads the saved duration before the timer-manager call. The patched bytes
are:

```text
c5 f8 57 c0 90    vxorps %xmm0,%xmm0,%xmm0; nop
```

That forces the registered timer duration to `0.0`. Live dry-run verification on
`kspls0` after applying showed both `dune_server-survival-1` and
`dune_server-deep-desert-1` with:

```text
deadline clamp bytes: 48 89 c1
timer duration bytes: c5 f8 57 c0 90
```

Confidence is high that future `SetLeavingGameTimer` calls now schedule a zero
delay timer. A live Deep Desert quit at `2026.06.02-00.06.01` showed the
backend path working: `SetLeavingGameTimer`, `RecordLogoffPersistenceEndTime`,
`LoggingOut`, and `Offline` all landed in the same second.

That live quit still showed the stale five-minute client warning. A focused
trace caught `ServerInitiateLogOffDialog` and `ServerStartLogOffTimer`, proving
the warning/countdown is driven by the server RPC path even though the backend
persistence timer is already zero.

The missing client-visible duration source is a separate set of global
pointers read by the `ServerStartLogOffTimer` implementation at runtime offset
`0x0d55f9a0`. Its duration helper at runtime offset `0x0d55f730` reads:

- `0x16523ce0`: client-visible logoff timer array, live value `300 300 0 0`.
- `0x16523d10`: client-visible logoff timer array, live value `300 300 0 0`.
- `0x16523d28`: alternate client-visible array, live value `0 0 0 0`.

The current script patches the first two client-visible arrays to `0.0` in
addition to the backend persistence arrays and code clamps. The third array is
reported in dry-run output but is already zero and is not mutated.

After applying this UI-duration patch live, both `dune_server-survival-1` and
`dune_server-deep-desert-1` reported:

```text
ui after ui1=... ui2=... ui3=...
0 0 0 0
0 0 0 0
0 0 0 0
```

A Deep Desert logout immediately afterward at `2026.06.02-00.14.48` recorded
`SetLeavingGameTimer ... to 2026.06.02-00.14.48`, then `LoggingOut` at
`00.14.49.030`, `RecordLogoffPersistenceEndTime ... to 00.14.48` at
`00.14.49.031`, and `Offline` at `00.14.49.084`.

Rejected attempt: on 2026-06-01, a live runtime patch changed the accessor entry
at runtime offset `0x12f49050` from:

```text
55 48 89 e5 f6 87 a4 02 00 00 10 75 1a
```

to:

```text
31 c0 c3 ...
```

That is `xor eax,eax; ret`. It matched the expected bytes in
`dune_server-survival-1` and `dune_server-deep-desert-1`, but both processes
exited with signal `139` shortly afterward. The containers were recreated and
started again immediately. Do not reapply that code-return patch.

That failed code patch was reverted by recreating the two containers. The safe
current path is the data-pointer patch, not a return-value code patch.

Script:

```bash
./scripts/patch-logoff-timers-runtime.sh --host kspls0
```

Dry run:

```bash
./scripts/patch-logoff-timers-runtime.sh --host kspls0 --dry-run
```

Post-start/local host mode:

```bash
./scripts/patch-logoff-timers-runtime.sh --local
```

The script first uses a known-offset table for the current expected ELF Build ID:

```text
6f8ca9ee5f3420c0b4c1ef7cefb412347bcba04b
```

If the running Build ID does not match the table, the script defaults to
`DUNE_LOGOFF_TIMER_AUTO_REMAP_ENABLED=true`. It scans the live server ELF for
the `SetLeavingGameTimer` and UI timer instruction patterns, derives the moved
offsets, and then validates the live runtime pointers, float values, and patch
instruction bytes before any write. If the signatures are missing, ambiguous, or
the live validation fails, it exits without patching.

The backend accessor scanner accepts the verified RIP-relative timer-global
loads emitted through either `r15` or `rax`. Funcom build `2036754-0-shipping`
changed only that destination register in this accessor; live pointer, float,
and instruction-byte validation remains mandatory before patching.

Build `2036754-0-shipping` has ELF Build ID
`cde875918e7dee23366c71e0d7bc20237810ea92` and a verified known-offset entry.
Its backend arrays resolve to `30 30 0 0` and `300 300 0 0`; its three UI
arrays resolve to `300 300 0 0`. The known entry still runs all live pointer,
float, and instruction-byte validation before writing.

Build `1d03e11f7a9161d3755e3aec4cbda3eb924193ae` uses backend globals at
`0x172fed50` and `0x172fed68`, clamp instruction `0x0da7dac4`, duration
instruction `0x0da7ded1`, and UI pointer slots `0x174eef00`, `0x174eef18`, and
`0x174eef30`. This build split the UI timer loads across code paths, so the
older three-load scanner did not recognize it. The offsets were verified by
live dry runs on Survival and both Deep Desert maps: backend arrays resolved
to `30 30 0 0` and `300 300 0 0`, the three UI slots each resolved to a
separate four-float array, and the clamp and duration bytes matched. The UI
arrays were `0 0 0 0` while no UI logoff countdown was active.

Build `2124138-0-shipping` / Steam build `25486303` has ELF Build ID
`39e7aabe1c6201e43b81254e1c54631f8f16c8df`. On this build the verified
server timer pointer slots are `0x173089f0` and `0x17308a08`; they resolve to
`30 30 0 0` and `300 300 0 0`. The deadline clamp instruction at
`0x0da84274` is `48 01 c1`, and the duration instruction at `0x0da84681` is
`c5 fa 10 45 d4`. The older UI-array signature matched unrelated vectors on
this build, so the runtime patch now validates and writes only the server
timer arrays and those two instructions. A post-apply dry run read back zeroed
timer values and patched instruction bytes on Survival, Deep Desert, and
Deep Desert PvP. UI countdown arrays are not targeted by this build entry.

Build `50d73d6403583bbcad919119c08d5083181c4d51` was captured from the live
Survival process on 2026-10-17 (binary SHA-256
`bcb6bc6b3c7ada038400716eb34c8812cb98a9d8b8eb360262b203e753c64c6b`). The
verified backend pointer slots are `0x1731ebf0` and `0x1731ec08`, the deadline
clamp is at `0x0da907d4`, and the timer-duration reload is at `0x0da90be1`.
Before patching, live dry runs on Survival and both Deep Desert maps resolved
the arrays to `30 30 0 0` and `300 300 0 0`, with original instruction bytes
`48 01 c1` and `c5 fa 10 45 d4`. The UI auto-remapper found no unique candidate,
so this build uses the verified backend-only patch path; client-visible timer
arrays remain untargeted.

After applying the patch to all three live maps, a second dry run verified both
backend arrays as `0 0 0 0`, the deadline clamp as `48 89 c1`, and the duration
reload as `c5 f8 57 c0 90`. The map containers remained running, and the farm
health summary reported 31 ready/alive/active partitions, both required core
partitions connected, and healthy FLS publication.

Force the auto-remap path for a dry-run test with:

```bash
DUNE_LOGOFF_TIMER_BUILD_ID=auto ./scripts/patch-logoff-timers-runtime.sh --local --dry-run
```

It defaults to these active production containers:

```text
dune_server-survival-1
dune_server-deep-desert-1
dune_server-deep-desert-pvp-1
```

Override the target list with `DUNE_LOGOFF_TIMER_CONTAINERS` if the live map
layout changes. `scripts/restart-post-start-health.sh` reapplies this runtime
patch after successful restart health checks when
`DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED=true`, because the patch is
process-local and container recreation restores the original values.

### 2026-06-02 DD1 Manual Restart Incident

On 2026-06-02, DD1 was recreated with a direct `docker compose up -d
deep-desert` while repairing Lukano's Landsraad contract state. That bypassed
`scripts/restart-post-start-health.sh`, so the DD1 process came back with the
upstream unsafe-location logoff timers restored:

```text
30 30 0 0
300 300 0 0
```

The repo INI values were still correct, but they only control reconnect grace.
The unsafe-location logoff timer is process-local runtime state. Reapply it
after any manual game-server start/recreate:

```bash
./scripts/restart-post-start-health.sh
./scripts/patch-logoff-timers-runtime.sh --local --dry-run
```

Expected dry-run proof for affected containers is:

```text
0 0 0 0
0 0 0 0
deadline clamp bytes: 0x48 0x89 0xc1
timer duration bytes: 0xc5 0xf8 0x57 0xc0 0x90
```

`scripts/restart-target.sh` already runs the post-start health script.
`scripts/recover-map.sh` now also runs it after the recovered partition becomes
ready/alive/active. Avoid raw Compose starts for live maps; if one is
unavoidable, run the post-start health script before leaving the host.

For the old 2026-05-24 build, it read two runtime float arrays through `gdb`. Before that patch, targeted containers had:

```text
30 30 0 0
300 300 0 0
```

After applying, both containers confirmed:

```text
0 0 0 0
0 0 0 0
```

Confidence: high that the memory values were changed. Confidence: moderate-high that these are the exact unsafe-location logoff durations, because the values are read by the duration function immediately before `ADunePlayerCharacter::SetLeavingGameTimer`. Final proof is a live disconnect outside a base showing no future timestamp in `LogLogOffSystem`.

Suggested gates:

```env
DUNE_ADMIN_MUTATIONS_ENABLED=true
DUNE_ADMIN_NETWORK_DISCONNECT_ENABLED=true
DUNE_ADMIN_NETWORK_DISCONNECT_TELEPORT_ENABLED=true
DUNE_CHAT_COMMAND_EXECUTE_NETWORK_TELEPORT=true
```

Suggested confirmation phrase:

```text
NETWORK DISCONNECT TELEPORT
```

## Safety Rules

- Do not treat admin-bot join/leave messages as proof of a real game disconnect.
- Do not call this a soft disconnect. It is a targeted hard timeout until a native session-close/kick path is proven.
- Only call the offline move helper after `dune.player_state.online_status='Offline'`.
- Snapshot original transform, partition, map, dimension, `server_id`, and actor ids before writing.
- Prefer same-partition moves first. Cross-partition and cross-map moves still need validation.
- Keep the network block scoped to the target endpoint and target map server namespace.
- Avoid broad UDP outages except as an emergency/manual test because they affect every player on that Survival process.
- Always remove packet rules even if the move fails. Verify the namespace rules are back to default before ending the operation.
- Log the old/new coordinates, timeout time, offline time, unblock time, reconnect time, and final row state.

## Verification Query

```sql
with ps as (
  select *
  from dune.player_state
  where character_name = :character_name
)
select
  ps.online_status,
  ps.server_id,
  a.id,
  a.map,
  a.partition_id,
  a.dimension_index,
  (((a.transform).location).x)::numeric(14,3) as x,
  (((a.transform).location).y)::numeric(14,3) as y,
  (((a.transform).location).z)::numeric(14,3) as z,
  a.serial
from ps
join dune.actors a
  on a.id in (ps.player_controller_id, ps.player_state_id, ps.player_pawn_id)
order by a.id;
```

Expected shape after reconnect:

- `online_status='Online'`.
- `server_id` is the active map server id.
- Pawn row is at the target coordinate.
- Survival may update controller/player-state rows to match after login/save.

## Open Validation

- Repeat with a second player.
- Repeat on another map/partition.
- Validate cross-partition same-map movement.
- Validate cross-map movement.
- Find a cleaner targeted disconnect knob than network packet drop, preferably a native `UNetConnection::Close`, game-session kick, or Online Services `RemoveSessionMember`/`KickLobbyMember` path.
- Automate endpoint discovery from Survival logs or packet capture.
- If a map crash follows a failed disconnect experiment, remove only duplicate
  `dune.farm_state` rows where the old server id has `alive=false` and the
  public `game_port` conflicts with the restarted live map, then run
  `scripts/fls-publication-health.py`.
