# Runtime secret handling

Game server database passwords and service tokens are mounted as read-only
Compose secrets. `run_server_safe.sh` invokes `game-runtime-secrets.py` to write
them to the server's protected `Saved/Config/LinuxServer/Game.ini` and
`Engine.ini` files before launching the game process. It also drops legacy
arguments containing the FLS token, database password, login password, or
server-command token. Credential values must stay in mounted secret files; do
not add them to a game server command or environment.

The admin panel, chat command consumer, database bootstrap helper, RabbitMQ
auth shim, and PostgreSQL exporter read credentials from mounted files using
`*_FILE` settings. The admin panel also mounts the protected secret source
directory so its database password rotation can update the source file after
the database role and `.env` value change. The examples keep secret values
blank; set real values before staging the files.

The host `.env` remains the source for credentials that are synchronized by
the operator. Stage the ignored, mode-0600 Compose secret files with:

```sh
python3 scripts/game-runtime-secrets.py sync-from-env --env-file .env
```

The command prints only a completion count. Do not enable shell tracing around
credential operations. Per-Sietch login passwords are written to separate
ignored files by `scripts/sietches.sh` and mounted only into their matching
server.

Some private Funcom control-plane images still accept database, FLS, and
RabbitMQ credentials only through their native environment settings. Their
entrypoints have not been verified to support file-based settings. Keep those
values out of command arguments and do not replace them with guessed `*_FILE`
names until the image behavior is confirmed.

The public-site snapshot collector does not load the signing key. It sends only
coarse public status fields over `/run/dune-public-directory-signer.sock` to a
socket-activated systemd signer. The signer runs under a dynamic service UID,
receives the Ed25519 key through `LoadCredential=`, has no Docker socket or
static-site write access, and can use only local Unix sockets. Keep the key
source under the protected external secret directory; do not add key access
back to the collector or its environment file.

Compose mounts the repository at `/workspace` in game containers, so the host
dotenv file and `config/secrets/` must resolve outside the repository. Run
`scripts/protect-compose-secrets.sh` before invoking Compose; the normal
`scripts/compose-files.sh` path enforces this automatically. The helper moves
the private sources under `$HOME/.config/dune-selfhost/` and leaves symlinks at
the host paths Compose expects. The symlink targets are outside the container's
workspace mount, so game processes cannot read those host files through
`/workspace`. Keep the external directory private and do not replace these
links with regular files.

Postgres uses `POSTGRES_PASSWORD_FILE` with the read-only
`dune_postgres_password` secret. The optional streaming replica uses
`POSTGRES_REPLICATION_PASSWORD_FILE`; its entrypoint reads the file, creates a
mode-`0600` temporary `.pgpass` under `/run`, and removes the password variables
before starting Postgres. Do not pass either password through Docker `-e`
arguments or a Compose environment value.

The map UDP-capacity guard follows only the standard private `.env` link after
checking the exact target, owner, and permissions. If it needs to widen the two
port ranges, it takes the pre-change backup under the shared dotenv lock and
updates the existing inode so Compose bind mounts stay connected. Other env
symlinks remain rejected.

The player-presence announcer must not read the full game `.env`. Stage its
own filtered systemd environment file with:

```sh
python3 scripts/sync-player-presence-env.py --env-file .env
```

The generated `config/secrets/player-presence.env` is ignored and mode 0600.
It contains the announcer's `DUNE_PLAYER_PRESENCE_*` settings, the exact chat
and connection settings required by `announce.sh`, and a small set of runtime
selectors. It excludes the admin-chat consumer settings, database passwords,
dotenv path overrides, and executable overrides. The service loads that file
through systemd and points its dotenv readers at `/dev/null`. Its announcement
child also sets `DUNE_ANNOUNCE_ENV_FILE=/dev/null`; `announce.sh` then skips
both default `.env` paths. Database and FLS health checks select running
containers by Compose labels and use `docker exec` / `docker logs`, so they do
not resolve the full Compose project or parse the game `.env` on every poll.
RabbitMQ management requests use authenticated HTTP inside the Python process
instead of putting the password in a `rabbitmqadmin` command line. The rare
Docker fallback mounts only the scripts directory and a mode-0600 temporary
JSON file; it does not mount the repository or put credentials in container
command arguments or environment metadata.

New secret values take effect when the affected containers are recreated. Use
the existing guarded daily maintenance flow for the game farm. A database
password change also requires changing the PostgreSQL role password in the same
maintenance operation; changing only `.env` or the mounted file will break
database clients.
