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
the activity, readiness, image and configuration checks. It runs the fixed native
restart script with its normal post-start hooks. This route cannot promote an
image; use the separately qualified image-update workflow.

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
