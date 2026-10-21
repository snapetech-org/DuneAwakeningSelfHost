# Configuration durability

The live `.env` file is both the Compose configuration source and a writable
file bind-mounted into `admin-panel` as `/workspace/.env`. Runtime tooling must
therefore preserve its filesystem inode. Renaming a temporary file over `.env`
looks correct on the host, but an already-running container remains attached to
the deleted old inode. Admin changes then update that unreachable file and are
lost the next time the container is recreated.

## Supported writer

Use `scripts/update-env-file.py` for script-driven changes:

```bash
./scripts/update-env-file.py .env \
  --set DUNE_AUTOSCALER_PROFILE adaptive \
  --set DUNE_AUTOSCALER_ENABLED true
```

The writer:

- requires an existing regular file and refuses symbolic links by default;
- `--allow-protected-link` accepts only a link that resolves exactly to
  `~/.config/dune-selfhost/compose.env`, with the file and parent directory
  owned by the current user and private from group and other users; it then
  updates the resolved regular file in place;
- validates keys, UTF-8, control characters, and a 4 MiB size ceiling;
- takes an exclusive advisory lock before it reads or renders the file;
- applies every `--set` argument as one transaction and removes duplicate
  definitions of keys it owns;
- writes through the open file descriptor, truncates to the exact new length,
  calls `fsync`, and reads the bytes back for verification;
- proves the device and inode are unchanged, which also preserves the existing
  owner and mode.

The Admin API uses the same `admin/env_file_store.py` implementation for normal
settings changes, archive restores, credential rotation, and rollback. This
prevents lost updates when an operator and an automation job write at the same
time.

The following runtime paths use the shared writer:

- CPU-affinity persistence;
- host-tuning persistence;
- autoscaler profile selection;
- the complete feature-parity activation helper;
- Steam package `DUNE_IMAGE_TAG` pinning, including guarded update/restart;
- official database patch metadata;
- initial local environment secret population;
- map UDP-capacity range changes. The capacity guard accepts only the same
  protected `.env` link, takes its backup under the shared file lock, and uses
  the shared writer so the Admin container's bind-mounted inode remains live.

## Verification

Run:

```bash
make test-update-env-file
make test-cpu-affinity test-host-tuning test-configure-autoscaler-profile
```

On a running host, compare the host file with the Admin container's mount. Both
device/inode pairs must match and the mount source must not end in `//deleted`:

```bash
container="$(docker compose --env-file .env ps -q admin-panel)"
pid="$(docker inspect -f '{{.State.Pid}}' "$container")"
stat -Lc 'host %d:%i' .env
stat -Lc 'container %d:%i' "/proc/$pid/root/workspace/.env"
grep ' /workspace/.env ' "/proc/$pid/mountinfo"
```

Then make a harmless settings change through Admin, confirm the host file sees
it immediately, recreate only `admin-panel`, and confirm the value remains.
Production execution still follows the hostname, change-contract, backup, and
deployment-assurance gates documented in [admin-panel.md](admin-panel.md).

## Recovery

In-place writing is necessary for file-bind continuity, so it cannot use a
rename as its final atomic operation. The writer minimizes the write window,
locks cooperative writers, fsyncs, and verifies. Mutation workflows retain
their existing pre-change backups under `backups/`; restore a verified backup
through the same writer or Admin restore workflow if an operating-system or
storage failure interrupts a write.

Never use `sed -i`, `os.replace`, `mv temporary .env`, or an editor configured
for atomic-save renames against a live `.env`. Use the shared writer or stop and
recreate every consumer after the edit.
