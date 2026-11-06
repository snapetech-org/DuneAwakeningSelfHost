# Guarded service recovery

Manual service recovery uses the existing socket-activated `dune-restart-api`
host service. It does not grant the Admin container a Docker socket or arbitrary
host command execution. The older map-only start/restart route remains available
for autoscaling; its request allowlist is unchanged.

The new `GET /v1/health` route reports protocol and guard availability without
performing a lifecycle operation. Readiness certification requires this live
proof as well as the existing capability, confirmation, and feature gates.
Both service-control flags default to false. Image-update mutations stay disabled.

`POST /v1/service-control` accepts exactly `service`, `action`, and a 32-character
hexadecimal `jobId`. Services must belong to the configured map roster or the
explicit control/database/broker allowlist. Actions are start, stop, or restart.
Container peers must be the actual project Admin container; host peers must be
root or the service account. The socket is limited to its owner and the root group.

Before execution, the host checks its configured identity, the independent
stateful-service gate, all four current/recent player counts, a current signed
readiness receipt, and the exact selected/running image identity. It takes the
shared maintenance lock, creates and verifies a fresh native backup, then repeats
the activity, readiness, image and configuration checks. It starts, stops or
restarts only the approved existing container ID, then runs the native post-start
health and runtime-patch verification hooks. It cannot create or recreate a
container, apply changed Compose settings or promote an image; use the separately
qualified native host workflow for those operations.

Protocol 2 requires a host-approved snapshot of the dotenv, fully rendered
Compose configuration, and container/image IDs in the private host state directory
outside the Admin workspace. `scripts/install-dune-restart-api.sh .env --reload-idle`
creates this approval only from the native host, checks for unfinished recovery
jobs, and preserves the live listening socket. No HTTP route grants approval.
The assured deployment option `--enable-service-recovery` first loads and qualifies
protocol 2 while the controls remain disabled, then enables the two recovery
flags and reapproves their exact configuration before recreating Admin. It does
not enable image updates or perform a container lifecycle operation.
Any dotenv change or replaced container requires native host reapproval. The
older autoscaler route uses the same fixed existing-container operations and
approved post-hook configuration, while retaining its map-only request shape.
Autoscaling cannot provision a missing container through this transport.

The host stores private durable job records outside the Admin workspace.
`GET /v1/service-control/JOB_ID` reports progress and the outcome without returning
raw command output or credentials. Reusing an ID returns that operation rather
than executing it again. A different operation cannot reuse the ID; an interrupted
operation blocks new jobs until an operator reviews its evidence. A client timeout
does not cancel or automatically retry a host operation.

Installation updates unit files and enables the listening socket. It deliberately
does not restart an active host service. Verify that no restart/recovery operation
is running before restarting the service to load new source. Preserve the live
socket inode when it is bind-mounted into Admin. Unknown player activity, including
an unavailable database, fails closed and requires the native operator recovery
runbook instead of this browser route.
