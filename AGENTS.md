## Runner policy — owner directive, 2026-10-26

All repositories and all GitHub Actions build, test, scan, packaging and release
jobs must use compatible private ephemeral JIT runners. GitHub-hosted runners
and automatic hosted fallback are not authorized. This directive supersedes
older hosted-first, pilot-only, selected-jobs-only and permission-pending notes.
Do not restore `NG_HOSTED_FIRST`, a hosted literal, or a hosted matrix fallback.
Missing capacity is a provisioning blocker, never permission to spend hosted
minutes. Preserve OS and architecture requirements. Keep untrusted pull-request
code on an isolated disposable runner with no secrets; do not widen trusted
runner-group access to make it run. Preserve active builds and other agents'
work. Do not restart the broker while it owns running guests. Validate a new
workflow revision on private JIT; rerunning an old run uses its old revision.

The shared policy and exact workflow inventory live in
`snapetech-org/packer/scripts/shared-jit-runner-pool/`. Source routing, deployed
broker policy, runner-group access and live platform capacity must all agree.
Do not report the rollout complete from YAML changes alone.

# Agent instructions - DuneAwakeningSelfHost

## Operational target safety

- `kspld0` is a lab/testing host only. Never treat local Docker Compose on
  `kspld0` as production.
- Production player/admin mutations must run on `kspls0`, not `kspld0`.
- Before any live admin mutation, grant, inventory edit, currency edit, XP edit,
  player recovery, or database write intended for the live server, first verify
  the target host with `hostname`. If it is not `kspls0`, stop and connect to
  `kspls0`.
- Do not run production mutations through `docker compose` from `kspld0`.
- Default to server-side changes and do not casually edit local Steam/Dune
  client game files. Client loader, hook, pak, binary, or launch-wrapper work is
  allowed only when the operator explicitly authorizes client-side work for the
  current task. Prefer repo-contained launch-time injection, overlay artifacts,
  generated packages, and reversible scripts over in-place Steam directory
  mutation. If direct client-file mutation is explicitly requested, record the
  target path, create/verify a backup or checksum manifest first, and keep it
  separate from production server mutations.
- Do not set Standard PvE DD Coriolis cycle duration to a far-future value.
  Landsraad uses the Coriolis cycle for its active/suspended window. DD1 can
  have Coriolis damage, shifting sands, restart, and DB wipe disabled, but
  `config/UserGame.ini` and `config/UserGame.deep-desert-coriolis.ini` must
  keep `m_CycleDurationInDays=7`. Run
  `scripts/validate-landsraad-coriolis-cycle.sh .env` before and after live map
  restarts when touching Coriolis config.
- Do not manually recreate/start live game-map containers with raw
  `docker compose up`, `docker compose restart`, or `docker compose start`
  unless you immediately run `scripts/restart-post-start-health.sh` on
  `kspls0` afterward and verify the logoff timer runtime patch with
  `scripts/patch-logoff-timers-runtime.sh --local --dry-run`. Prefer
  `scripts/restart-target.sh`, `scripts/recover-map.sh`, or
  `scripts/start-map-with-post-hooks.sh`, because those paths run the
  post-start hooks that restore process-local patches.

## Reverse engineering tooling

- For local Ghidra MCP/headless setup, use `docs/local-ghidra-mcp.md`.
- The initialized local PyGhidra MCP project is `/tmp/ghidra-work/pyghidra-mcp`
  and currently contains the staged server binary as `/server-bin-d7120c`.
- Ghidra projects are single-writer. Do not run the MCP server, headless Ghidra
  wrapper, or project-management commands against the same project at the same
  time.

## Communication style

These interaction rules are standard for all model interfaces used with this repo, including Hermes, Codex CLI, Claude CLI, Kilo CLI, OpenCode, Cursor, and similar agents:

- Never praise questions or validate premises before answers.
- If the user is wrong, say so immediately and directly.
- Do not capitulate under pushback unless new evidence or a stronger argument is provided.
- Do not anchor on numbers or estimates provided by the user. Generate an independent assessment first, then compare.
- Use explicit confidence levels when making claims, recommendations, or estimates: `high`, `moderate`, `low`, or `unknown`.
- Do not add disclaimers.
- Do not give ethics lectures unless explicitly asked.
- Do not use "it is important to consider" style hedges.
- Surface negative conclusions and bad news directly.
- Optimize for accuracy, not approval.
- If you do not know, say so. Never fabricate.
