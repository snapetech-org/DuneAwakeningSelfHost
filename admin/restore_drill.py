"""Isolated, evidence-producing PostgreSQL restore rehearsals for DASH.

The drill never connects to the live database.  It restores one confined dump
inside a disposable, networkless Docker container, validates Dune invariants,
performs a round-trip dump, and records a private hash-chained receipt.
"""

from __future__ import annotations

import datetime as _datetime
import fcntl
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import socket
import stat
import time
import urllib.parse


DEFAULT_IMAGE = "registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13"
CONTAINER_PREFIX = "dash-restore-drill-"
LABEL_KEY = "com.dash.restore-drill"
REQUIRED_TABLES = (
    "actors", "player_state", "world_partition", "farm_state", "items",
    "inventories", "buildings", "building_instances", "base_backups",
    "permission_actor",
)
REQUIRED_FUNCTIONS = (
    "base_backup_save_from_totem", "get_player_pawn", "update_death_location",
    "admin_move_offline_player_to_partition", "character_transfer_export",
    "character_transfer_import", "_character_transfer_get_patches_checksum",
)
COUNTED_TABLES = (
    "actors", "player_state", "world_partition", "farm_state", "items",
    "inventories", "building_instances", "base_backups",
)


class RestoreDrillError(RuntimeError):
    pass


class RestoreDrillBusy(RestoreDrillError):
    pass


def _utc(epoch=None):
    epoch = time.time() if epoch is None else float(epoch)
    return _datetime.datetime.fromtimestamp(epoch, _datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_file(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path, root):
    return path == root or root in path.parents


def _reject_symlink_components(path, root):
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("backup dump path may not contain symlinks")


def select_dump(workspace, requested=None):
    """Return a regular, non-symlinked .dump confined beneath backups/."""
    workspace = pathlib.Path(workspace).resolve(strict=True)
    backup_root = (workspace / "backups").resolve(strict=True)
    if requested:
        candidate = pathlib.Path(requested)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        unresolved = candidate.absolute()
        resolved = candidate.resolve(strict=True)
        if not _within(resolved, backup_root):
            raise ValueError("backup dump is outside the workspace backup root")
        if resolved.name.startswith(".source-") or "restore-drills" in resolved.parts:
            raise ValueError("private restore-drill staging files are not selectable backups")
        _reject_symlink_components(unresolved, backup_root)
        candidates = [resolved]
    else:
        candidates = []
        for candidate in backup_root.rglob("*.dump"):
            try:
                if candidate.name.startswith(".source-") or "restore-drills" in candidate.parts:
                    continue
                unresolved = candidate.absolute()
                resolved = candidate.resolve(strict=True)
                if not _within(resolved, backup_root):
                    continue
                _reject_symlink_components(unresolved, backup_root)
                mode = resolved.stat().st_mode
                if stat.S_ISREG(mode):
                    candidates.append(resolved)
            except (OSError, ValueError):
                continue
        candidates.sort(key=lambda item: (item.stat().st_mtime_ns, str(item)), reverse=True)
    if not candidates:
        raise FileNotFoundError("no PostgreSQL custom-format dump exists beneath backups/")
    selected = candidates[0]
    if selected.suffix != ".dump" or not stat.S_ISREG(selected.stat().st_mode):
        raise ValueError("backup source must be a regular .dump file")
    return selected


def _decode_chunked(body):
    decoded = bytearray()
    offset = 0
    while True:
        end = body.find(b"\r\n", offset)
        if end < 0:
            raise RestoreDrillError("Docker API returned malformed chunked data")
        size_text = body[offset:end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise RestoreDrillError("Docker API returned an invalid chunk length") from exc
        offset = end + 2
        if size == 0:
            break
        decoded.extend(body[offset:offset + size])
        offset += size + 2
    return bytes(decoded)


def _decode_multiplexed(body):
    output = bytearray()
    offset = 0
    while offset + 8 <= len(body) and body[offset] in (0, 1, 2, 3):
        size = int.from_bytes(body[offset + 4:offset + 8], "big")
        offset += 8
        if offset + size > len(body):
            break
        output.extend(body[offset:offset + size])
        offset += size
    if not output and body:
        output.extend(body)
    return output.decode("utf-8", errors="replace")


class DockerSocketClient:
    """Small Docker Engine client restricted to the operations a drill needs."""

    def __init__(self, socket_path="/var/run/docker.sock"):
        self.socket_path = str(socket_path)

    def request(self, method, path, body=None, *, accepted=(200,), timeout=30, max_bytes=32 * 1024 * 1024):
        if method not in ("GET", "POST", "DELETE"):
            raise ValueError("unsupported Docker API method")
        encoded = b"" if body is None else _canonical(body)
        request = (
            f"{method} {path} HTTP/1.1\r\nHost: docker\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(encoded)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + encoded
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            sock.sendall(request)
            chunks = []
            received = 0
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout as exc:
                    raise RestoreDrillError(f"Docker API timed out after {timeout}s") from exc
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise RestoreDrillError("Docker API response exceeded its bounded size")
                chunks.append(chunk)
        raw = b"".join(chunks)
        header, separator, response = raw.partition(b"\r\n\r\n")
        if not separator:
            raise RestoreDrillError("Docker API returned an invalid HTTP response")
        status_line = header.split(b"\r\n", 1)[0]
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError) as exc:
            raise RestoreDrillError(status_line.decode(errors="replace")) from exc
        headers = {}
        for line in header.split(b"\r\n")[1:]:
            key, found, value = line.partition(b":")
            if found:
                headers[key.decode(errors="replace").strip().lower()] = value.decode(errors="replace").strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            response = _decode_chunked(response)
        elif headers.get("content-length", "").isdigit():
            response = response[:int(headers["content-length"])]
        if status not in accepted:
            detail = response.decode("utf-8", errors="replace")[-4000:]
            raise RestoreDrillError(f"Docker API {status} for {method} {path}: {detail}")
        return response

    def list_drill_containers(self):
        filters = urllib.parse.quote(json.dumps({"label": [f"{LABEL_KEY}=true"]}, separators=(",", ":")))
        body = self.request("GET", f"/containers/json?all=1&filters={filters}")
        return json.loads(body.decode("utf-8") or "[]")

    def create_container(self, name, spec):
        path = "/containers/create?name=" + urllib.parse.quote(name, safe="")
        body = self.request("POST", path, spec, accepted=(201,))
        identifier = str((json.loads(body.decode("utf-8") or "{}") or {}).get("Id") or "")
        if len(identifier) < 12:
            raise RestoreDrillError("Docker did not return a container id")
        return identifier

    def start_container(self, identifier):
        self.request("POST", f"/containers/{identifier}/start", accepted=(204, 304))

    def inspect_container(self, identifier):
        body = self.request("GET", f"/containers/{identifier}/json")
        return json.loads(body.decode("utf-8") or "{}")

    def logs(self, identifier):
        body = self.request(
            "GET", f"/containers/{identifier}/logs?stdout=1&stderr=1&tail=200",
            timeout=10, max_bytes=2 * 1024 * 1024,
        )
        return _decode_multiplexed(body)[-16000:]

    def exec(self, identifier, argv, *, timeout=60):
        body = self.request(
            "POST", f"/containers/{identifier}/exec",
            {"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": list(argv)},
            accepted=(201,), timeout=10,
        )
        exec_id = str((json.loads(body.decode("utf-8") or "{}") or {}).get("Id") or "")
        if len(exec_id) < 12:
            raise RestoreDrillError("Docker did not return an exec id")
        output = self.request(
            "POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": False},
            timeout=timeout, max_bytes=32 * 1024 * 1024,
        )
        inspected = json.loads(self.request("GET", f"/exec/{exec_id}/json").decode("utf-8") or "{}")
        return int(inspected.get("ExitCode") if inspected.get("ExitCode") is not None else -1), _decode_multiplexed(output)

    def remove_container(self, identifier, force=True):
        force_value = "1" if force else "0"
        self.request("DELETE", f"/containers/{identifier}?force={force_value}&v=1", accepted=(204, 404), timeout=30)


def build_container_spec(host_dump, image=DEFAULT_IMAGE, *, host_passwd=None, host_group=None,
                         run_uid=None, run_gid=None,
                         memory_bytes=2 * 1024**3, cpu_count=2.0, pids_limit=128,
                         pgdata_bytes=1536 * 1024**2):
    run_uid = os.getuid() if run_uid is None else int(run_uid)
    run_gid = os.getgid() if run_gid is None else int(run_gid)
    host_dump = str(pathlib.Path(host_dump))
    mounts = [{
        "Type": "bind", "Source": host_dump, "Target": "/drill/source.dump",
        "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"},
    }]
    if host_passwd:
        mounts.append({"Type": "bind", "Source": str(host_passwd), "Target": "/etc/passwd", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}})
    if host_group:
        mounts.append({"Type": "bind", "Source": str(host_group), "Target": "/etc/group", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}})
    return {
        "Image": image,
        "User": f"{run_uid}:{run_gid}",
        "Env": [
            "POSTGRES_DB=drill", "POSTGRES_USER=dune",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "PGDATA=/var/lib/postgresql/data/pgdata",
        ],
        "Labels": {LABEL_KEY: "true"},
        "StopTimeout": 30,
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": int(pids_limit),
            "NanoCpus": int(float(cpu_count) * 1_000_000_000),
            "Memory": int(memory_bytes),
            "MemorySwap": int(memory_bytes),
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Mounts": mounts,
            "Tmpfs": {
                "/var/lib/postgresql/data": f"rw,noexec,nosuid,size={int(pgdata_bytes)},uid={run_uid},gid={run_gid},mode=0700",
                "/var/run/postgresql": f"rw,noexec,nosuid,size=16777216,uid={run_uid},gid={run_gid},mode=0750",
                "/tmp": f"rw,noexec,nosuid,size=268435456,uid={run_uid},gid={run_gid},mode=0700",
            },
        },
    }


def _run_checked(docker, container_id, argv, *, timeout=60, label="command"):
    code, output = docker.exec(container_id, argv, timeout=timeout)
    if code != 0:
        raise RestoreDrillError(f"{label} failed with exit {code}: {output[-4000:]}")
    return output.strip()


def _sql_catalog():
    table_values = ",".join("('%s')" % name for name in REQUIRED_TABLES)
    function_values = ",".join("('%s')" % name for name in REQUIRED_FUNCTIONS)
    return f"""
WITH required_tables(name) AS (VALUES {table_values}),
required_functions(name) AS (VALUES {function_values})
SELECT json_build_object(
  'database', current_database(),
  'postgresVersion', current_setting('server_version'),
  'missingTables', (SELECT coalesce(json_agg(name ORDER BY name), '[]'::json)
                    FROM required_tables WHERE to_regclass('dune.' || name) IS NULL),
  'missingFunctions', (SELECT coalesce(json_agg(name ORDER BY name), '[]'::json)
                       FROM required_functions r WHERE NOT EXISTS (
                         SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                         WHERE n.nspname='dune' AND p.proname=r.name)),
  'invalidIndexes', (SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready),
  'unvalidatedConstraints', (SELECT count(*) FROM pg_constraint WHERE NOT convalidated),
  'databaseBytes', pg_database_size(current_database())
)::text;
""".strip()


def _sql_counts():
    entries = ",\n  ".join("'%s', (SELECT count(*) FROM dune.%s)" % (name, name) for name in COUNTED_TABLES)
    return f"SELECT json_build_object(\n  {entries}\n)::text;"


def _sql_player_life_recovery_contract():
    """Prove the native dead/alive round trip without retaining either write."""
    return r"""
BEGIN;
CREATE TEMP TABLE dash_life_candidate ON COMMIT DROP AS
SELECT eps.account_id, eps.player_pawn_id, eps.life_state::text AS original_life_state,
       eps.death_location::text AS original_death_location,
       gp.actor AS pawn, gp.server_info
FROM dune.encrypted_player_state eps
CROSS JOIN LATERAL dune.get_player_pawn(eps.account_id) gp
WHERE eps.life_state::text = 'Alive'
  AND eps.player_pawn_id IS NOT NULL
ORDER BY eps.account_id
LIMIT 1;

DO $dash$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM dash_life_candidate) THEN
    RAISE EXCEPTION 'no alive player with a native pawn/server-info tuple is available';
  END IF;
END
$dash$;

SELECT dune.update_death_location(pawn, server_info, 'Dead'::dune.playerlifestate)
FROM dash_life_candidate;

CREATE TEMP TABLE dash_life_dead_observed ON COMMIT DROP AS
SELECT eps.account_id, eps.life_state::text AS life_state,
       eps.death_location IS NOT NULL AS death_location_present
FROM dune.encrypted_player_state eps
JOIN dash_life_candidate candidate USING (account_id);

SELECT dune.update_death_location(pawn, server_info, 'Alive'::dune.playerlifestate)
FROM dash_life_candidate;

SELECT json_build_object(
  'transactionRolledBack', true,
  'candidateFound', EXISTS (SELECT 1 FROM dash_life_candidate),
  'deadTransitionVerified', EXISTS (
    SELECT 1 FROM dash_life_dead_observed
    WHERE life_state='Dead' AND death_location_present
  ),
  'aliveTransitionVerified', EXISTS (
    SELECT 1
    FROM dune.encrypted_player_state eps
    JOIN dash_life_candidate candidate USING (account_id)
    WHERE eps.life_state::text='Alive'
      AND eps.death_location IS NULL
      AND eps.player_pawn_id=candidate.player_pawn_id
  ),
  'nativeFunction', 'dune.update_death_location(actordescription,serverinfo,playerlifestate)',
  'testedAccountCount', (SELECT count(*) FROM dash_life_candidate)
)::text;
ROLLBACK;
""".strip()


def _sql_offline_teleport_contract():
    """Prove a native persisted pawn move without retaining the write."""
    return r"""
BEGIN;
CREATE TEMP TABLE dash_teleport_candidate ON COMMIT DROP AS
SELECT eps.account_id, eps.player_pawn_id, ea."user" AS fls_id,
       pawn.partition_id, pawn.map AS original_map,
       pawn.dimension_index AS original_dimension_index,
       ((pawn.transform).location).x AS original_x,
       ((pawn.transform).location).y AS original_y,
       ((pawn.transform).location).z AS original_z,
       wp.map AS partition_map, wp.dimension_index AS partition_dimension_index,
       dune.upgrade_map_name(wp.map) AS expected_pawn_map
FROM dune.encrypted_player_state eps
JOIN dune.encrypted_accounts ea ON ea.id=eps.account_id
JOIN dune.actors pawn ON pawn.id=eps.player_pawn_id
JOIN dune.world_partition wp ON wp.partition_id=pawn.partition_id
WHERE eps.online_status::text='Offline'
  AND dune.is_player_offline(ea."user")
  AND NOT wp.blocked
ORDER BY eps.account_id
LIMIT 1;

DO $dash$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM dash_teleport_candidate) THEN
    RAISE EXCEPTION 'no explicitly Offline player with a persisted pawn and active partition is available';
  END IF;
END
$dash$;

SELECT dune.admin_move_offline_player_to_partition(
  fls_id,
  partition_id,
  row(original_x + 1.0, original_y, original_z)::dune.vector
)
FROM dash_teleport_candidate;

SELECT json_build_object(
  'transactionRolledBack', true,
  'candidateFound', EXISTS (SELECT 1 FROM dash_teleport_candidate),
  'moveVerified', EXISTS (
    SELECT 1
    FROM dash_teleport_candidate candidate
    JOIN dune.encrypted_player_state eps USING (account_id)
    JOIN dune.actors pawn ON pawn.id=eps.player_pawn_id
    WHERE eps.online_status::text='Offline'
      AND dune.is_player_offline(candidate.fls_id)
      AND pawn.id=candidate.player_pawn_id
      AND pawn.partition_id=candidate.partition_id
      AND pawn.dimension_index=candidate.partition_dimension_index
      AND pawn.map=candidate.expected_pawn_map
      AND abs(((pawn.transform).location).x - (candidate.original_x + 1.0)) <= 0.00001
      AND abs(((pawn.transform).location).y - candidate.original_y) <= 0.00001
      AND abs(((pawn.transform).location).z - candidate.original_z) <= 0.00001
  ),
  'nativeFunction', 'dune.admin_move_offline_player_to_partition(text,bigint,vector)',
  'testedAccountCount', (SELECT count(*) FROM dash_teleport_candidate)
)::text;
ROLLBACK;
""".strip()


def _sql_character_transfer_contract():
    """Prove native full-character export/import while rolling back all writes."""
    return r"""
BEGIN;
CREATE TEMP TABLE dash_transfer_candidate ON COMMIT DROP AS
SELECT eps.account_id AS original_account_id, eps.id AS original_player_state_row_id,
       eps.player_controller_id AS original_player_controller_id,
       ea."user" AS fls_id,
       dune.decrypt_user_data(eps.encrypted_character_name) AS character_name,
       dune._character_transfer_get_patches_checksum() AS patches_checksum,
       null::jsonb AS transfer_data,
       null::bigint AS imported_player_controller_id
FROM dune.encrypted_player_state eps
JOIN dune.encrypted_accounts ea ON ea.id=eps.account_id
WHERE eps.online_status::text='Offline'
  AND dune.is_player_offline(ea."user")
ORDER BY eps.account_id
LIMIT 1;

DO $dash$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM dash_transfer_candidate) THEN
    RAISE EXCEPTION 'no explicitly Offline character is available for transfer proof';
  END IF;
END
$dash$;

UPDATE dash_transfer_candidate
SET transfer_data=dune.character_transfer_export(fls_id);

DO $dash$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM dash_transfer_candidate
    WHERE transfer_data->>'_patches_checksum'=patches_checksum
      AND jsonb_typeof(transfer_data->'entries')='array'
      AND jsonb_array_length(transfer_data->'entries') > 0
  ) THEN
    RAISE EXCEPTION 'native transfer export failed shape/checksum validation';
  END IF;
END
$dash$;

DROP TABLE IF EXISTS pg_temp.export_data;

UPDATE dash_transfer_candidate
SET imported_player_controller_id=dune.character_transfer_import(transfer_data, fls_id, character_name);

SELECT json_build_object(
  'transactionRolledBack', true,
  'candidateFound', EXISTS (SELECT 1 FROM dash_transfer_candidate),
  'exportVerified', EXISTS (
    SELECT 1 FROM dash_transfer_candidate
    WHERE transfer_data->>'_patches_checksum'=patches_checksum
      AND jsonb_typeof(transfer_data->'entries')='array'
      AND jsonb_array_length(transfer_data->'entries') > 0
  ),
  'importVerified', EXISTS (
    SELECT 1
    FROM dash_transfer_candidate candidate
    JOIN dune.encrypted_accounts ea ON ea."user"=candidate.fls_id
    JOIN dune.encrypted_player_state eps ON eps.account_id=ea.id
    WHERE eps.player_controller_id=candidate.imported_player_controller_id
      AND dune.decrypt_user_data(eps.encrypted_character_name)=candidate.character_name
  ),
  'nativeExportFunction', 'dune.character_transfer_export(text)',
  'nativeImportFunction', 'dune.character_transfer_import(jsonb,text,text)',
  'testedAccountCount', (SELECT count(*) FROM dash_transfer_candidate)
)::text;
ROLLBACK;
""".strip()


def _parse_json_output(output, label):
    lines = [line.strip() for line in str(output).splitlines() if line.strip()]
    if not lines:
        raise RestoreDrillError(f"{label} returned no data")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RestoreDrillError(f"{label} returned invalid JSON: {lines[-1][-1000:]}") from exc


def _verify_isolation(inspected, expected):
    host = inspected.get("HostConfig") or {}
    config = inspected.get("Config") or {}
    mounts = inspected.get("Mounts") or []
    source_mount = next((item for item in mounts if item.get("Destination") == "/drill/source.dump"), {})
    passwd_mount = next((item for item in mounts if item.get("Destination") == "/etc/passwd"), {})
    group_mount = next((item for item in mounts if item.get("Destination") == "/etc/group"), {})
    actual = {
        "networkMode": host.get("NetworkMode"),
        "readOnlyRootfs": bool(host.get("ReadonlyRootfs")),
        "user": config.get("User"),
        "capDrop": sorted(host.get("CapDrop") or []),
        "securityOpt": sorted(host.get("SecurityOpt") or []),
        "pidsLimit": host.get("PidsLimit"),
        "nanoCpus": host.get("NanoCpus"),
        "memoryBytes": host.get("Memory"),
        "memorySwapBytes": host.get("MemorySwap"),
        "sourceReadOnly": not bool(source_mount.get("RW", True)),
        "identityFilesReadOnly": bool(passwd_mount) and bool(group_mount) and not bool(passwd_mount.get("RW", True)) and not bool(group_mount.get("RW", True)),
        "publishedPorts": bool((host.get("PortBindings") or {})),
    }
    required = (
        actual["networkMode"] == "none" and actual["readOnlyRootfs"] and
        actual["user"] == expected["User"] and "ALL" in actual["capDrop"] and
        "no-new-privileges:true" in actual["securityOpt"] and
        actual["sourceReadOnly"] and actual["identityFilesReadOnly"] and not actual["publishedPorts"] and
        int(actual["pidsLimit"] or 0) == int(expected["HostConfig"]["PidsLimit"]) and
        int(actual["memoryBytes"] or 0) == int(expected["HostConfig"]["Memory"]) and
        int(actual["nanoCpus"] or 0) == int(expected["HostConfig"]["NanoCpus"])
    )
    actual["verified"] = bool(required)
    if not required:
        raise RestoreDrillError("Docker did not apply every required restore-drill isolation control")
    return actual


def _atomic_json(path, payload, owner_uid=None, owner_gid=None):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if os.geteuid() == 0 and owner_uid is not None and owner_gid is not None:
            os.chown(temporary, int(owner_uid), int(owner_gid))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_source(source, destination, run_uid, run_gid):
    """Copy a dump to a private, container-readable file without weakening it."""
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            os.chmod(temporary, 0o600)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if os.geteuid() == 0:
            os.chown(temporary, int(run_uid), int(run_gid))
        elif os.geteuid() != int(run_uid):
            raise PermissionError("restore-drill process cannot create a private source owned by the configured container UID")
        os.chmod(temporary, 0o400)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _stage_identity(path, content, run_uid, run_gid):
    path = pathlib.Path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.geteuid() == 0:
            os.chown(temporary, int(run_uid), int(run_gid))
        elif os.geteuid() != int(run_uid):
            raise PermissionError("restore-drill process cannot create private identity files for the configured container UID")
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _write_receipt(receipt_root, receipt, retention_count=1000, owner_uid=None, owner_gid=None):
    receipt_root = pathlib.Path(receipt_root)
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(receipt_root, 0o700)
    if os.geteuid() == 0 and owner_uid is not None and owner_gid is not None:
        os.chown(receipt_root, int(owner_uid), int(owner_gid))
    previous = None
    latest = receipt_root / "latest.json"
    if latest.is_file() and not latest.is_symlink():
        try:
            previous = json.loads(latest.read_text(encoding="utf-8")).get("receiptSha256")
        except (OSError, json.JSONDecodeError, AttributeError):
            previous = None
    receipt["previousReceiptSha256"] = previous
    receipt["receiptSha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    path = receipt_root / f"{receipt['id']}.json"
    _atomic_json(path, receipt, owner_uid, owner_gid)
    _atomic_json(latest, receipt, owner_uid, owner_gid)
    receipts = sorted(item for item in receipt_root.glob("*.json") if item.name != "latest.json")
    excess = max(0, len(receipts) - max(10, int(retention_count)))
    for old in receipts[:excess]:
        old.unlink(missing_ok=True)
    return path


def list_receipts(receipt_root, limit=50):
    root = pathlib.Path(receipt_root)
    rows = []
    if not root.exists():
        return rows
    for path in sorted((item for item in root.glob("*.json") if item.name != "latest.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            claimed = payload.get("receiptSha256")
            unsigned = dict(payload)
            unsigned.pop("receiptSha256", None)
            payload["receiptHashValid"] = bool(claimed) and hashlib.sha256(_canonical(unsigned)).hexdigest() == claimed
            payload["receiptPath"] = path.name
            rows.append(payload)
        except (OSError, json.JSONDecodeError):
            rows.append({"id": path.stem, "ok": False, "receiptHashValid": False, "error": "receipt is unreadable"})
        if len(rows) >= max(1, min(int(limit), 1000)):
            break
    return rows


def status(receipt_root, limit=20):
    receipts = list_receipts(receipt_root, limit=limit)
    latest = receipts[0] if receipts else None
    return {
        "ok": bool(latest and latest.get("ok") and latest.get("receiptHashValid")),
        "latest": latest,
        "receipts": receipts,
        "receiptCountShown": len(receipts),
    }


def _clean_stale(docker, now_epoch, *, stale_seconds=6 * 3600, exclude=None):
    removed = []
    for container in docker.list_drill_containers():
        identifier = str(container.get("Id") or "")
        names = [str(name).lstrip("/") for name in (container.get("Names") or [])]
        labels = container.get("Labels") or {}
        if identifier == exclude or labels.get(LABEL_KEY) != "true" or not any(name.startswith(CONTAINER_PREFIX) for name in names):
            continue
        created = int(container.get("Created") or 0)
        if container.get("State") != "running" or created <= int(now_epoch - stale_seconds):
            docker.remove_container(identifier, force=True)
            removed.append(identifier[:12])
    return removed


def _clean_stale_files(receipt_root, now_epoch, stale_seconds=6 * 3600):
    removed = []
    root = pathlib.Path(receipt_root).resolve(strict=True)
    for pattern in (".source-*.dump", ".passwd-*", ".group-*"):
        for path in root.glob(pattern):
            try:
                if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                    continue
                if path.stat().st_mtime > float(now_epoch) - float(stale_seconds):
                    continue
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
    return sorted(removed)


def run_drill(workspace, *, host_workspace=None, source=None, receipt_root=None,
              docker=None, docker_socket="/var/run/docker.sock", image=DEFAULT_IMAGE,
              max_backup_age_seconds=36 * 3600, max_restore_seconds=900,
              readiness_seconds=120, command_timeout_seconds=900,
              memory_bytes=2 * 1024**3, cpu_count=2.0, pids_limit=128,
              run_uid=None, run_gid=None,
              pgdata_bytes=1536 * 1024**2, retention_count=1000, sleep=time.sleep):
    workspace = pathlib.Path(workspace).resolve(strict=True)
    host_workspace = pathlib.Path(host_workspace or workspace)
    if not host_workspace.is_absolute():
        raise ValueError("host workspace path must be absolute")
    run_uid = os.getuid() if run_uid is None else int(run_uid)
    run_gid = os.getgid() if run_gid is None else int(run_gid)
    if run_uid <= 0 or run_gid < 0:
        raise ValueError("restore-drill PostgreSQL must run as a non-root UID/GID")
    receipt_root = pathlib.Path(receipt_root or workspace / "backups" / "admin-panel" / "restore-drills")
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root = (workspace / "backups").resolve(strict=True)
    if not _within(receipt_root.resolve(strict=True), backup_root):
        raise ValueError("restore-drill receipts and private staging must remain beneath workspace/backups")
    os.chmod(receipt_root, 0o700)
    if os.geteuid() == 0:
        os.chown(receipt_root, run_uid, run_gid)
    lock_path = receipt_root / ".restore-drill.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if os.geteuid() == 0:
        os.fchown(lock_fd, run_uid, run_gid)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RestoreDrillBusy("another restore drill is already running") from exc

        started_epoch = time.time()
        started_mono = time.monotonic()
        selected = select_dump(workspace, source)
        relative = selected.relative_to(workspace)
        source_stat = selected.stat()
        identifier = _datetime.datetime.fromtimestamp(started_epoch, _datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
        container_name = CONTAINER_PREFIX + identifier[-12:]
        staged_source = receipt_root / f".source-{identifier}.dump"
        staged_passwd = receipt_root / f".passwd-{identifier}"
        staged_group = receipt_root / f".group-{identifier}"
        source_sha = _sha256_file(selected)
        age_seconds = max(0.0, started_epoch - source_stat.st_mtime)
        docker = docker or DockerSocketClient(docker_socket)
        container_id = None
        cleanup = {"removedCurrent": False, "removedStagedSource": False, "removedIdentityFiles": False, "staleContainersRemoved": [], "staleFilesRemoved": [], "error": None}
        receipt = {
            "receiptSchema": 1,
            "id": identifier,
            "startedAt": _utc(started_epoch),
            "sourcePath": relative.as_posix(),
            "sourceSha256": source_sha,
            "sourceBytes": source_stat.st_size,
            "sourceModifiedAt": _utc(source_stat.st_mtime),
            "backupAgeSeconds": round(age_seconds, 3),
            "image": image,
            "liveDatabaseTouched": False,
            "integrityOk": False,
            "policyOk": False,
            "ok": False,
        }
        try:
            cleanup["staleContainersRemoved"] = _clean_stale(docker, started_epoch)
            cleanup["staleFilesRemoved"] = _clean_stale_files(receipt_root, started_epoch)
            _stage_source(selected, staged_source, run_uid, run_gid)
            _stage_identity(
                staged_passwd,
                f"root:x:0:0:root:/root:/bin/sh\npostgres:x:70:70:PostgreSQL:/var/lib/postgresql:/bin/sh\ndash:x:{run_uid}:{run_gid}:DASH restore drill:/tmp:/sbin/nologin\n",
                run_uid, run_gid,
            )
            _stage_identity(
                staged_group,
                f"root:x:0:\npostgres:x:70:\ndash:x:{run_gid}:\n",
                run_uid, run_gid,
            )
            if _sha256_file(staged_source) != source_sha:
                raise RestoreDrillError("private staged source SHA-256 does not match the selected dump")
            host_staged_source = host_workspace / staged_source.relative_to(workspace)
            host_staged_passwd = host_workspace / staged_passwd.relative_to(workspace)
            host_staged_group = host_workspace / staged_group.relative_to(workspace)
            spec = build_container_spec(host_staged_source, image, host_passwd=host_staged_passwd,
                                        host_group=host_staged_group, run_uid=run_uid, run_gid=run_gid,
                                        memory_bytes=memory_bytes, cpu_count=cpu_count,
                                        pids_limit=pids_limit, pgdata_bytes=pgdata_bytes)
            container_id = docker.create_container(container_name, spec)
            docker.start_container(container_id)
            receipt["isolation"] = _verify_isolation(docker.inspect_container(container_id), spec)

            ready_started = time.monotonic()
            ready = False
            last_ready_output = ""
            while time.monotonic() - ready_started < float(readiness_seconds):
                code, last_ready_output = docker.exec(container_id, ["pg_isready", "-q", "-d", "drill", "-U", "dune"], timeout=10)
                if code == 0:
                    ready = True
                    break
                sleep(1)
            if not ready:
                logs = docker.logs(container_id)
                raise RestoreDrillError(f"isolated PostgreSQL was not ready: {last_ready_output[-1000:]} {logs[-3000:]}")
            receipt["timings"] = {"serviceReadySeconds": round(time.monotonic() - ready_started, 3)}

            staged_sha = _run_checked(docker, container_id, ["sha256sum", "/drill/source.dump"], label="staged source hash").split()[0]
            staged_mode = _run_checked(docker, container_id, ["stat", "-c", "%a", "/drill/source.dump"], label="staged source mode")
            if staged_sha != source_sha or staged_mode != "400":
                raise RestoreDrillError("Docker-staged source dump failed SHA-256 or private-mode verification")
            receipt["isolation"].update({"sourceSha256Verified": True, "sourceMode": staged_mode})

            _run_checked(docker, container_id, ["pg_restore", "--list", "/drill/source.dump"],
                         timeout=command_timeout_seconds, label="source archive listing")
            restore_started = time.monotonic()
            _run_checked(
                docker, container_id,
                ["pg_restore", "--exit-on-error", "--no-owner", "--no-privileges", "--username=dune", "--dbname=drill", "/drill/source.dump"],
                timeout=command_timeout_seconds, label="isolated restore",
            )
            restore_seconds = time.monotonic() - restore_started
            receipt["timings"]["restoreSeconds"] = round(restore_seconds, 3)

            catalog_output = _run_checked(
                docker, container_id, ["psql", "-XAt", "--username=dune", "--set", "ON_ERROR_STOP=1", "-d", "drill", "-c", _sql_catalog()],
                timeout=command_timeout_seconds, label="Dune catalog validation",
            )
            catalog = _parse_json_output(catalog_output, "Dune catalog validation")
            if catalog.get("missingTables") or catalog.get("missingFunctions"):
                raise RestoreDrillError(f"restored Dune schema is incomplete: {json.dumps(catalog, sort_keys=True)}")
            if int(catalog.get("invalidIndexes") or 0) != 0 or int(catalog.get("unvalidatedConstraints") or 0) != 0:
                raise RestoreDrillError(f"restored database has invalid indexes or unvalidated constraints: {json.dumps(catalog, sort_keys=True)}")

            counts_output = _run_checked(
                docker, container_id, ["psql", "-XAt", "--username=dune", "--set", "ON_ERROR_STOP=1", "-d", "drill", "-c", _sql_counts()],
                timeout=command_timeout_seconds, label="Dune core-table reads",
            )
            counts = _parse_json_output(counts_output, "Dune core-table reads")
            if int(counts.get("actors") or 0) <= 0 or int(counts.get("world_partition") or 0) <= 0:
                raise RestoreDrillError("restored database lacks required actor or world-partition rows")
            if int(counts.get("player_state") or 0) > int(counts.get("actors") or 0):
                raise RestoreDrillError("restored player_state count exceeds actor count")

            life_contract_output = _run_checked(
                docker, container_id,
                ["psql", "-qXAt", "--username=dune", "--set", "ON_ERROR_STOP=1", "-d", "drill", "-c", _sql_player_life_recovery_contract()],
                timeout=command_timeout_seconds, label="native player life-state recovery contract",
            )
            life_contract = _parse_json_output(life_contract_output, "native player life-state recovery contract")
            if not all(bool(life_contract.get(key)) for key in (
                "transactionRolledBack", "candidateFound", "deadTransitionVerified", "aliveTransitionVerified",
            )):
                raise RestoreDrillError(
                    "native player life-state recovery contract failed: "
                    + json.dumps(life_contract, sort_keys=True)
                )

            teleport_contract_output = _run_checked(
                docker, container_id,
                ["psql", "-qXAt", "--username=dune", "--set", "ON_ERROR_STOP=1", "-d", "drill", "-c", _sql_offline_teleport_contract()],
                timeout=command_timeout_seconds, label="native offline teleport contract",
            )
            teleport_contract = _parse_json_output(teleport_contract_output, "native offline teleport contract")
            if not all(bool(teleport_contract.get(key)) for key in (
                "transactionRolledBack", "candidateFound", "moveVerified",
            )):
                raise RestoreDrillError(
                    "native offline teleport contract failed: "
                    + json.dumps(teleport_contract, sort_keys=True)
                )

            transfer_contract_output = _run_checked(
                docker, container_id,
                ["psql", "-qXAt", "--username=dune", "--set", "ON_ERROR_STOP=1", "-d", "drill", "-c", _sql_character_transfer_contract()],
                timeout=command_timeout_seconds, label="native character transfer contract",
            )
            transfer_contract = _parse_json_output(transfer_contract_output, "native character transfer contract")
            if not all(bool(transfer_contract.get(key)) for key in (
                "transactionRolledBack", "candidateFound", "exportVerified", "importVerified",
            )):
                raise RestoreDrillError(
                    "native character transfer contract failed: "
                    + json.dumps(transfer_contract, sort_keys=True)
                )

            _run_checked(docker, container_id, ["vacuumdb", "--analyze-only", "--username=dune", "--dbname=drill"],
                         timeout=command_timeout_seconds, label="restored database analyze")
            _run_checked(docker, container_id, ["pg_dump", "--format=custom", "--no-owner", "--username=dune", "--file=/tmp/roundtrip.dump", "drill"],
                         timeout=command_timeout_seconds, label="round-trip dump")
            _run_checked(docker, container_id, ["pg_restore", "--list", "/tmp/roundtrip.dump"],
                         timeout=command_timeout_seconds, label="round-trip archive listing")
            roundtrip_size = int(_run_checked(docker, container_id, ["stat", "-c", "%s", "/tmp/roundtrip.dump"], label="round-trip size"))
            if roundtrip_size <= 0:
                raise RestoreDrillError("round-trip dump is empty")

            receipt["validation"] = {
                "archiveListed": True,
                "schema": catalog,
                "rowCounts": counts,
                "playerLifeRecoveryContract": life_contract,
                "offlineTeleportContract": teleport_contract,
                "characterTransferContract": transfer_contract,
                "analyzeCompleted": True,
                "roundTripArchiveListed": True,
                "roundTripBytes": roundtrip_size,
            }
            receipt["integrityOk"] = True
            receipt["policy"] = {
                "maxBackupAgeSeconds": int(max_backup_age_seconds),
                "maxRestoreSeconds": int(max_restore_seconds),
                "backupAgeWithinTarget": age_seconds <= float(max_backup_age_seconds),
                "restoreWithinTarget": restore_seconds <= float(max_restore_seconds),
            }
            receipt["policyOk"] = all((receipt["policy"]["backupAgeWithinTarget"], receipt["policy"]["restoreWithinTarget"]))
            receipt["ok"] = receipt["integrityOk"] and receipt["policyOk"]
        except Exception as exc:
            receipt["error"] = str(exc)[-6000:]
        finally:
            if container_id:
                try:
                    docker.remove_container(container_id, force=True)
                    cleanup["removedCurrent"] = True
                except Exception as exc:
                    cleanup["error"] = str(exc)[-2000:]
                    receipt["ok"] = False
                    receipt["integrityOk"] = False
            try:
                staged_source.unlink(missing_ok=True)
                cleanup["removedStagedSource"] = not staged_source.exists()
            except Exception as exc:
                cleanup["error"] = ((cleanup.get("error") or "") + f" staged source cleanup failed: {exc}").strip()
                receipt["ok"] = False
                receipt["integrityOk"] = False
            try:
                staged_passwd.unlink(missing_ok=True)
                staged_group.unlink(missing_ok=True)
                cleanup["removedIdentityFiles"] = not staged_passwd.exists() and not staged_group.exists()
            except Exception as exc:
                cleanup["error"] = ((cleanup.get("error") or "") + f" identity file cleanup failed: {exc}").strip()
                receipt["ok"] = False
                receipt["integrityOk"] = False
            receipt["cleanup"] = cleanup
            receipt.setdefault("timings", {})["totalSeconds"] = round(time.monotonic() - started_mono, 3)
            receipt["finishedAt"] = _utc()
            receipt_path = _write_receipt(
                receipt_root, receipt, retention_count=retention_count,
                owner_uid=run_uid, owner_gid=run_gid,
            )
            receipt["receiptPath"] = str(receipt_path.relative_to(workspace)) if _within(receipt_path.resolve(), workspace) else receipt_path.name
        return receipt
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
