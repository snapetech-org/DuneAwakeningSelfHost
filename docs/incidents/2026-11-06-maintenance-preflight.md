# Morning maintenance preflight failure, 6 November 2026

Production was checked on `kspls0`. At 05:32 CST, Steam reported installed,
target and loaded build `26332623`, with game tag `2173753-0-shipping`.
Subsequent hotfix polls continued to report the same build. There was no
observed overnight server-package update.

The daily scheduler persisted job `fQZ9iTiNo0Ger44C`. After 17 shared-operation
lock deferrals, it failed before stopping any services because the current-build
preflight invoked `check-steam-update.sh` inside the minimal Admin image:
`env: can't execute 'bash': No such file or directory`.

The fix uses `check-current-steam-build.py` to request the bounded, read-only
`check-current-build` phase through the existing host restart socket. Only an
all-farm request with no service overrides or lifecycle flags is accepted. The
host runs its existing checker against its actual environment and Steam path,
including any configured runtime-security validation. Hosts without the socket
use the existing shell checker directly. Missing sockets and failed checks
continue to prevent shutdown. The checker also defines `script_dir`, which the
deployed runtime-security extension requires.

At investigation time, the live database `dune_sb_1_5_15_0` had six ready,
active game servers, FLS publication was healthy, and TCP 31982 was listening.
The public-summary command requires `DUNE_PUBLIC_STATUS_DB_NAME` explicitly;
passing `.env` does not populate that variable in its public-summary branch.
An initial check against its legacy default database therefore produced a
false zero-ready result. No game-map restart was needed for this repair.

Verification:

```bash
hostname # kspls0
python3 scripts/test-dune-restart-api.py
docker exec dune_server-admin-panel-1 python3 /workspace/scripts/check-current-steam-build.py /workspace/.env
DUNE_PUBLIC_STATUS_DB_NAME=dune_sb_1_5_15_0 scripts/status.sh .env --public-summary
```
