#!/usr/bin/env python3
"""HMAC-sealed desired-state baselines and retained drift findings."""

from __future__ import annotations

import datetime as _datetime
import fnmatch
import hashlib
import hmac
import json
import os
import pathlib
import re
import sqlite3
import stat
import time
import uuid


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _iso(value):
    return _datetime.datetime.fromtimestamp(float(value), _datetime.timezone.utc).isoformat() if value is not None else None


def _bounded_int(value, name, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_policy(path):
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if raw.get("schemaVersion") != 1:
        raise ValueError("desired-state policy schemaVersion must be 1")
    patterns = raw.get("includePatterns")
    excludes = raw.get("excludePatterns", [])
    required = raw.get("requiredPaths", [])
    critical = raw.get("criticalPatterns", [])
    for name, value, minimum, maximum in (
        ("includePatterns", patterns, 1, 256),
        ("excludePatterns", excludes, 0, 256),
        ("requiredPaths", required, 1, 256),
        ("criticalPatterns", critical, 1, 256),
    ):
        if not isinstance(value, list) or not minimum <= len(value) <= maximum or not all(isinstance(item, str) and 0 < len(item) <= 256 for item in value):
            raise ValueError(f"{name} must contain {minimum}..{maximum} bounded patterns")
    return {
        "schemaVersion": 1,
        "pollSeconds": _bounded_int(raw.get("pollSeconds", 60), "pollSeconds", 15, 3600),
        "observationRetentionDays": _bounded_int(raw.get("observationRetentionDays", 90), "observationRetentionDays", 7, 3650),
        "maxFiles": _bounded_int(raw.get("maxFiles", 5000), "maxFiles", 10, 100000),
        "maxFileBytes": _bounded_int(raw.get("maxFileBytes", 67108864), "maxFileBytes", 1024, 1073741824),
        "maxTotalBytes": _bounded_int(raw.get("maxTotalBytes", 536870912), "maxTotalBytes", 1048576, 10737418240),
        "includePatterns": patterns,
        "excludePatterns": excludes,
        "requiredPaths": required,
        "criticalPatterns": critical,
        "trackFileModes": bool(raw.get("trackFileModes", True)),
        "trackContainers": bool(raw.get("trackContainers", True)),
    }


def read_secret(path):
    path = pathlib.Path(path)
    raw = path.read_text(encoding="utf-8").strip()
    if len(raw) < 64:
        raise ValueError("desired-state HMAC secret must contain at least 256 bits encoded as 64 characters")
    if path.stat().st_mode & 0o077:
        raise PermissionError("desired-state HMAC secret must not be group/world accessible")
    return raw.encode("utf-8")


def _digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def collect_files(root, policy):
    root = pathlib.Path(root).resolve()
    paths = set()
    for pattern in policy["includePatterns"]:
        for candidate in root.glob(pattern):
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if _matches(relative, policy["excludePatterns"]):
                continue
            if candidate.is_file() or candidate.is_symlink():
                paths.add(relative)
    for required in policy["requiredPaths"]:
        if not (root / required).exists():
            paths.add(required)
    if len(paths) > policy["maxFiles"]:
        raise ValueError(f"desired-state file count {len(paths)} exceeds policy maxFiles")
    total = 0
    files = {}
    for relative in sorted(paths):
        path = root / relative
        if not path.exists() and not path.is_symlink():
            files[relative] = {"kind": "missing", "critical": _matches(relative, policy["criticalPatterns"])}
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            files[relative] = {
                "kind": "symlink",
                "targetSha256": hashlib.sha256(target.encode()).hexdigest(),
                "mode": mode if policy["trackFileModes"] else None,
                "critical": _matches(relative, policy["criticalPatterns"]),
            }
            continue
        size = int(info.st_size)
        if size > policy["maxFileBytes"]:
            raise ValueError(f"desired-state file exceeds maxFileBytes: {relative}")
        total += size
        if total > policy["maxTotalBytes"]:
            raise ValueError("desired-state files exceed maxTotalBytes")
        files[relative] = {
            "kind": "file",
            "sha256": _digest_file(path),
            "bytes": size,
            "mode": mode if policy["trackFileModes"] else None,
            "critical": _matches(relative, policy["criticalPatterns"]),
        }
    return {"files": files, "fileCount": len(files), "totalBytes": total}


def secret_hash(secret, value):
    return hmac.new(secret, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_container(raw, secret):
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    env = {}
    for item in config.get("Env") or []:
        key, separator, value = str(item).partition("=")
        if separator and key:
            env[key] = secret_hash(secret, value)
    mounts = []
    for item in raw.get("Mounts") or []:
        destination = str(item.get("Destination") or "")
        mounts.append({
            "destination": destination,
            "type": str(item.get("Type") or ""),
            "rw": bool(item.get("RW")),
            "sourceHmac": secret_hash(secret, item.get("Source") or ""),
        })
    networks = {}
    for name, item in sorted((raw.get("NetworkSettings") or {}).get("Networks", {}).items()):
        ipam = item.get("IPAMConfig") or {}
        networks[name] = {
            "aliases": sorted(str(value) for value in (item.get("Aliases") or []) if value),
            "configuredIpv4": ipam.get("IPv4Address") or "",
            "configuredIpv6": ipam.get("IPv6Address") or "",
            "configuredLinkLocalIps": sorted(str(value) for value in (ipam.get("LinkLocalIPs") or []) if value),
        }
    labels = config.get("Labels") or {}
    normalized = {
        "service": labels.get("com.docker.compose.service") or str(raw.get("Name") or "").lstrip("/"),
        "imageRef": config.get("Image") or "",
        "imageId": raw.get("Image") or "",
        "entrypoint": config.get("Entrypoint") or [],
        "command": config.get("Cmd") or [],
        "workingDir": config.get("WorkingDir") or "",
        "user": config.get("User") or "",
        "envHmacs": dict(sorted(env.items())),
        "restartPolicy": (host.get("RestartPolicy") or {}).get("Name") or "",
        "privileged": bool(host.get("Privileged")),
        "readOnlyRootfs": bool(host.get("ReadonlyRootfs")),
        "networkMode": host.get("NetworkMode") or "",
        "capAdd": sorted(host.get("CapAdd") or []),
        "capDrop": sorted(host.get("CapDrop") or []),
        "securityOpt": sorted(host.get("SecurityOpt") or []),
        "pidsLimit": int(host.get("PidsLimit") or 0),
        "memory": int(host.get("Memory") or 0),
        "memorySwap": int(host.get("MemorySwap") or 0),
        "nanoCpus": int(host.get("NanoCpus") or 0),
        "cpusetCpus": host.get("CpusetCpus") or "",
        "mounts": sorted(mounts, key=lambda item: (item["destination"], item["type"])),
        "networks": networks,
    }
    normalized["fingerprint"] = hashlib.sha256(_canonical(normalized).encode()).hexdigest()
    return normalized


def build_snapshot(root, policy, containers, secret, observed_at=None):
    file_state = collect_files(root, policy)
    normalized_containers = {}
    if policy["trackContainers"]:
        for raw in containers or []:
            row = normalize_container(raw, secret)
            service = row.pop("service")
            if not service or service in normalized_containers:
                raise ValueError("container services must be unique and non-empty")
            normalized_containers[service] = row
    payload = {
        "schemaVersion": 1,
        "files": file_state["files"],
        "containers": dict(sorted(normalized_containers.items())),
    }
    return {
        **payload,
        "observedAt": _iso(observed_at if observed_at is not None else time.time()),
        "fileCount": file_state["fileCount"],
        "totalBytes": file_state["totalBytes"],
        "containerCount": len(normalized_containers),
        "snapshotSha256": hashlib.sha256(_canonical(payload).encode()).hexdigest(),
    }


def compare_snapshots(baseline, current):
    changes = []
    for category in ("files", "containers"):
        before_rows = baseline.get(category) or {}
        after_rows = current.get(category) or {}
        for subject in sorted(set(before_rows) | set(after_rows)):
            before = before_rows.get(subject)
            after = after_rows.get(subject)
            if before == after:
                continue
            change = "added" if before is None else "removed" if after is None else "modified"
            critical = category == "containers" or bool((before or after or {}).get("critical"))
            details = {}
            if category == "files":
                details = {"before": before, "after": after}
            else:
                before = before or {}
                after = after or {}
                changed_fields = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
                details = {
                    "changedFields": changed_fields,
                    "beforeFingerprint": before.get("fingerprint"),
                    "afterFingerprint": after.get("fingerprint"),
                    "beforeImageRef": before.get("imageRef"),
                    "afterImageRef": after.get("imageRef"),
                    "beforeImageId": before.get("imageId"),
                    "afterImageId": after.get("imageId"),
                }
            key = hashlib.sha256(f"{category}:{subject}:{change}".encode()).hexdigest()
            changes.append({"key": key, "category": category, "subject": subject, "change": change, "critical": critical, "details": details})
    return changes


def validate_runtime_transition(baseline, current, transition, secret):
    required = {'service', 'fromImageId', 'fromImageRef', 'toImageId', 'toImageRef'}
    if not isinstance(transition, dict) or set(transition) != required:
        raise ValueError('complete runtime image transition required')
    service = transition['service']
    if not isinstance(service, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', service):
        raise ValueError('invalid runtime transition service')
    if not all(isinstance(transition[key], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', transition[key]) for key in ('fromImageId', 'toImageId')):
        raise ValueError('immutable runtime image identities required')
    before = (baseline.get('containers') or {}).get(service)
    after = (current.get('containers') or {}).get(service)
    if not before or not after:
        raise ValueError('existing runtime service required')
    if (after.get('imageId'), after.get('imageRef')) != (transition['toImageId'], transition['toImageRef']):
        raise ValueError('runtime candidate identity changed')
    changes = compare_snapshots(baseline, current)
    if not changes and before == after:
        return
    if (before.get('imageId'), before.get('imageRef')) != (transition['fromImageId'], transition['fromImageRef']):
        raise ValueError('runtime source identity changed')
    if len(changes) != 1 or changes[0]['category'] != 'containers' or changes[0]['subject'] != service or changes[0]['change'] != 'modified':
        raise ValueError('unrelated desired-state drift blocks runtime attestation')
    fields = set(changes[0]['details']['changedFields'])
    if 'envHmacs' in fields:
        # These two legacy names were replaced by explicit binary-patch names.
        # Only an already-disabled flag may migrate, with the identical signed
        # false value. Arbitrary environment renames remain drift.
        aliases = {
            'DUNE_BRT_DD_ACTION_GATE_PATCH_ENABLED': 'DUNE_BRT_DD_ACTION_GATE_BINARY_PATCH_ENABLED',
            'DUNE_BRT_DD_TOOL_ENABLE_PATCH_ENABLED': 'DUNE_BRT_DD_TOOL_ENABLE_BINARY_PATCH_ENABLED',
        }
        old_env, new_env = before.get('envHmacs') or {}, dict(after.get('envHmacs') or {})
        disabled = secret_hash(secret, 'false')
        for legacy, canonical in aliases.items():
            if legacy in old_env and canonical not in old_env and legacy not in new_env and canonical in new_env:
                if old_env[legacy] != disabled or new_env[canonical] != disabled:
                    raise ValueError('BRT alias migration must preserve disabled false flags')
                new_env[legacy] = new_env.pop(canonical)
        if new_env != old_env:
            raise ValueError('unreviewed runtime environment drift')
        fields.remove('envHmacs')
    if not fields <= {'fingerprint', 'imageId', 'imageRef'} or 'imageId' not in fields:
        raise ValueError('runtime settings changed beyond the approved image')


class Store:
    def __init__(self, database, policy, secret_file, owner_uid=None, owner_gid=None):
        self.database = pathlib.Path(database)
        self.policy_path = pathlib.Path(policy)
        self.secret_file = pathlib.Path(secret_file)
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None

    @property
    def policy(self):
        return load_policy(self.policy_path)

    @property
    def secret(self):
        return read_secret(self.secret_file)

    def _secure(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only traversal is required for private signed state; 0644
        # would remove traversal and expose directory entries to other users.
        os.chmod(self.database.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        if self.database.exists():
            os.chmod(self.database, 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None:
            gid = self.owner_gid if self.owner_gid is not None else -1
            os.chown(self.database.parent, self.owner_uid, gid)
            if self.database.exists():
                os.chown(self.database, self.owner_uid, gid)

    def connect(self, readonly=False):
        self._secure()
        if readonly:
            connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True, timeout=10)
        else:
            connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout=10000")
        connection.execute("pragma foreign_keys=on")
        if not readonly:
            connection.execute("pragma journal_mode=wal")
            connection.execute("pragma synchronous=full")
        return connection

    def initialize(self):
        self.policy
        self.secret
        connection = self.connect()
        try:
            connection.executescript("""
                create table if not exists baselines (
                  id text primary key, created_at real not null, actor text not null,
                  reason text not null, snapshot_json text not null, snapshot_sha256 text not null,
                  signature text not null, active integer not null check(active in (0,1))
                );
                create unique index if not exists one_active_desired_state_baseline on baselines(active) where active=1;
                create table if not exists observations (
                  id integer primary key autoincrement, observed_at real not null,
                  baseline_id text, snapshot_sha256 text not null, drift_count integer not null,
                  critical_count integer not null, maintenance_active integer not null check(maintenance_active in (0,1)),
                  signature text not null, foreign key(baseline_id) references baselines(id)
                );
                create index if not exists desired_state_observations_time on observations(observed_at);
                create table if not exists findings (
                  id text primary key, finding_key text not null unique, category text not null,
                  subject text not null, change_type text not null, critical integer not null check(critical in (0,1)),
                  first_seen_at real not null, last_seen_at real not null, resolved_at real,
                  acknowledged_at real, acknowledged_by text, note text not null default '',
                  details_json text not null, signature text not null default ''
                );
                create table if not exists events (
                  sequence integer primary key autoincrement, event_type text not null,
                  created_at real not null, actor text not null, payload_json text not null,
                  previous_signature text, signature text not null
                );
                create trigger if not exists desired_state_baselines_no_update before update on baselines
                  when not (
                    old.active=1 and new.active=0 and
                    new.id=old.id and new.created_at=old.created_at and new.actor=old.actor and
                    new.reason=old.reason and new.snapshot_json=old.snapshot_json and
                    new.snapshot_sha256=old.snapshot_sha256 and new.signature=old.signature
                  )
                  begin select raise(abort,'sealed desired-state baselines are immutable'); end;
                create trigger if not exists desired_state_baselines_no_delete before delete on baselines begin select raise(abort,'desired-state baselines are append-only'); end;
                create trigger if not exists desired_state_events_no_update before update on events begin select raise(abort,'desired-state events are append-only'); end;
                create trigger if not exists desired_state_events_no_delete before delete on events begin select raise(abort,'desired-state events are append-only'); end;
                create table if not exists metadata (key text primary key, value text not null);
            """)
            finding_columns = {row[1] for row in connection.execute("pragma table_info(findings)")}
            if "signature" not in finding_columns:
                connection.execute("alter table findings add column signature text")
                for row in connection.execute("select id from findings"):
                    self._sign_finding(connection, row["id"])
            connection.execute("insert into metadata(key,value) values('schema_version','2') on conflict(key) do update set value=excluded.value")
            connection.commit()
        finally:
            connection.close()
        self._secure()
        return self.verify()

    def _sign(self, payload, *, secret=None):
        return hmac.new(self.secret if secret is None else secret, _canonical(payload).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _finding_document(row):
        details = row["details_json"]
        if isinstance(details, str):
            details = json.loads(details)
        return {
            "id": row["id"], "findingKey": row["finding_key"], "category": row["category"],
            "subject": row["subject"], "changeType": row["change_type"], "critical": bool(row["critical"]),
            "firstSeenAt": row["first_seen_at"], "lastSeenAt": row["last_seen_at"],
            "resolvedAt": row["resolved_at"], "acknowledgedAt": row["acknowledged_at"],
            "acknowledgedBy": row["acknowledged_by"], "note": row["note"], "details": details,
        }

    def _sign_finding(self, connection, finding_id):
        row = connection.execute("select * from findings where id=?", (finding_id,)).fetchone()
        if not row:
            raise ValueError("desired-state finding not found while signing")
        signature = self._sign(self._finding_document(row))
        connection.execute("update findings set signature=? where id=?", (signature, finding_id))
        return signature

    def _event(self, connection, event_type, actor, payload, at):
        previous = connection.execute("select signature from events order by sequence desc limit 1").fetchone()
        previous_signature = previous["signature"] if previous else None
        document = {"eventType": event_type, "createdAt": at, "actor": actor, "payload": payload, "previousSignature": previous_signature}
        signature = self._sign(document)
        connection.execute(
            "insert into events(event_type,created_at,actor,payload_json,previous_signature,signature) values(?,?,?,?,?,?)",
            (event_type, at, actor, _canonical(payload), previous_signature, signature),
        )
        return signature

    def seal(self, snapshot, actor, reason, at=None, *, runtime_transition=None):
        actor = str(actor or "").strip()[:128]
        reason = str(reason or "").strip()[:1000]
        if not actor or not reason:
            raise ValueError("actor and reason are required to seal desired state")
        now = float(at if at is not None else time.time())
        snapshot_payload = {key: snapshot[key] for key in ("schemaVersion", "files", "containers")}
        snapshot_sha = hashlib.sha256(_canonical(snapshot_payload).encode()).hexdigest()
        baseline_id = f"desired-{uuid.uuid4().hex}"
        signed = {"id": baseline_id, "createdAt": now, "actor": actor, "reason": reason, "snapshotSha256": snapshot_sha, "snapshot": snapshot_payload}
        signature = self._sign(signed)
        connection = self.connect()
        try:
            connection.execute("begin immediate")
            prior = connection.execute("select id from baselines where active=1").fetchone()
            if runtime_transition is not None:
                if not self.verify(connection).get('ok'):
                    raise ValueError('desired-state integrity blocks runtime attestation')
                baseline = self.active_baseline(connection)
                if baseline is None:
                    raise ValueError('sealed baseline required for runtime attestation')
                validate_runtime_transition(baseline['snapshot'], snapshot_payload, runtime_transition, self.secret)
            if prior:
                connection.execute("update baselines set active=0 where id=?", (prior["id"],))
            connection.execute(
                "insert into baselines(id,created_at,actor,reason,snapshot_json,snapshot_sha256,signature,active) values(?,?,?,?,?,?,?,1)",
                (baseline_id, now, actor, reason, _canonical(snapshot_payload), snapshot_sha, signature),
            )
            open_findings = [row["id"] for row in connection.execute("select id from findings where resolved_at is null")]
            connection.execute("update findings set resolved_at=? where resolved_at is null", (now,))
            for finding_id in open_findings:
                self._sign_finding(connection, finding_id)
            self._event(connection, "baseline-sealed", actor, {"baselineId": baseline_id, "priorBaselineId": prior["id"] if prior else None, "snapshotSha256": snapshot_sha, "reason": reason}, now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"ok": True, "baselineId": baseline_id, "createdAt": _iso(now), "snapshotSha256": snapshot_sha, "signature": signature}

    def verified_resolutions(self, finding_ids):
        """Return positive, signed resolution evidence from one SQLite snapshot."""
        self.initialize_if_needed()
        connection = self.connect(readonly=True)
        try:
            connection.execute("begin")
            if not self.verify(connection).get("ok"):
                raise ValueError("desired-state resolution evidence failed verification")
            result = {}
            for finding_id in set(finding_ids):
                row = connection.execute("select * from findings where id=?", (finding_id,)).fetchone()
                if row is not None and row["resolved_at"] is not None:
                    result[finding_id] = {"resolvedAt": _iso(row["resolved_at"]), "signature": row["signature"]}
            return result
        finally:
            connection.close()

    def active_baseline(self, connection=None):
        own = connection is None
        connection = connection or self.connect(readonly=True)
        try:
            row = connection.execute("select * from baselines where active=1").fetchone()
            if not row:
                return None
            return {
                "id": row["id"], "createdAt": _iso(row["created_at"]), "createdAtEpoch": row["created_at"],
                "actor": row["actor"], "reason": row["reason"], "snapshot": json.loads(row["snapshot_json"]),
                "snapshotSha256": row["snapshot_sha256"], "signature": row["signature"],
            }
        finally:
            if own:
                connection.close()

    def observe(self, snapshot, maintenance_active=False, at=None):
        now = float(at if at is not None else time.time())
        connection = self.connect()
        opened = []
        resolved = []
        try:
            connection.execute("begin immediate")
            baseline = self.active_baseline(connection)
            changes = compare_snapshots(baseline["snapshot"], snapshot) if baseline else []
            active_keys = {row["key"] for row in changes}
            existing = {row["finding_key"]: row for row in connection.execute("select * from findings where resolved_at is null")}
            for change in changes:
                row = existing.get(change["key"])
                if row:
                    connection.execute("update findings set last_seen_at=?,details_json=? where finding_key=?", (now, _canonical(change["details"]), change["key"]))
                    self._sign_finding(connection, row["id"])
                else:
                    prior = connection.execute("select id from findings where finding_key=?", (change["key"],)).fetchone()
                    if prior:
                        finding_id = prior["id"]
                        connection.execute(
                            "update findings set critical=?,last_seen_at=?,resolved_at=null,acknowledged_at=null,acknowledged_by=null,note='',details_json=? where id=?",
                            (int(change["critical"]), now, _canonical(change["details"]), finding_id),
                        )
                    else:
                        finding_id = f"drift-{uuid.uuid4().hex}"
                        connection.execute(
                            "insert into findings(id,finding_key,category,subject,change_type,critical,first_seen_at,last_seen_at,details_json) values(?,?,?,?,?,?,?,?,?)",
                            (finding_id, change["key"], change["category"], change["subject"], change["change"], int(change["critical"]), now, now, _canonical(change["details"])),
                        )
                    self._sign_finding(connection, finding_id)
                    opened.append(finding_id)
            for key, row in existing.items():
                if key not in active_keys:
                    connection.execute("update findings set resolved_at=? where finding_key=?", (now, key))
                    self._sign_finding(connection, row["id"])
                    resolved.append(row["id"])
            observation = {
                "observedAt": now, "baselineId": baseline["id"] if baseline else None,
                "snapshotSha256": snapshot["snapshotSha256"], "driftCount": len(changes),
                "criticalCount": sum(1 for row in changes if row["critical"]), "maintenanceActive": bool(maintenance_active),
            }
            signature = self._sign(observation)
            connection.execute(
                "insert into observations(observed_at,baseline_id,snapshot_sha256,drift_count,critical_count,maintenance_active,signature) values(?,?,?,?,?,?,?)",
                (now, observation["baselineId"], observation["snapshotSha256"], observation["driftCount"], observation["criticalCount"], int(maintenance_active), signature),
            )
            cutoff = now - self.policy["observationRetentionDays"] * 86400
            connection.execute("delete from observations where observed_at<?", (cutoff,))
            if opened:
                self._event(connection, "drift-opened", "collector", {"findingIds": opened, "count": len(opened)}, now)
            if resolved:
                self._event(connection, "drift-resolved", "collector", {"findingIds": resolved, "count": len(resolved)}, now)
            connection.commit()
            return {"ok": True, **observation, "observedAt": _iso(now), "sealed": bool(baseline), "findingsOpened": opened, "findingsResolved": resolved}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acknowledge(self, finding_id, actor, note="", at=None):
        actor = str(actor or "").strip()[:128]
        note = str(note or "").strip()[:2000]
        if not actor:
            raise ValueError("actor is required")
        now = float(at if at is not None else time.time())
        connection = self.connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute("select * from findings where id=? and resolved_at is null", (str(finding_id),)).fetchone()
            if not row:
                raise ValueError("open drift finding not found")
            connection.execute("update findings set acknowledged_at=?,acknowledged_by=?,note=? where id=?", (now, actor, note, row["id"]))
            self._sign_finding(connection, row["id"])
            self._event(connection, "drift-acknowledged", actor, {"findingId": row["id"], "note": note}, now)
            connection.commit()
            return {"ok": True, "id": row["id"], "acknowledgedAt": _iso(now), "acknowledgedBy": actor, "note": note}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(self, limit=200):
        self.initialize_if_needed()
        connection = self.connect(readonly=True)
        try:
            baseline = self.active_baseline(connection)
            latest = connection.execute("select * from observations order by observed_at desc limit 1").fetchone()
            findings = []
            for row in connection.execute("select * from findings order by (resolved_at is null) desc,critical desc,last_seen_at desc limit ?", (max(1, min(int(limit), 1000)),)):
                findings.append({
                    "id": row["id"], "category": row["category"], "subject": row["subject"], "change": row["change_type"],
                    "critical": bool(row["critical"]), "firstSeenAt": _iso(row["first_seen_at"]), "lastSeenAt": _iso(row["last_seen_at"]),
                    "resolvedAt": _iso(row["resolved_at"]), "acknowledgedAt": _iso(row["acknowledged_at"]),
                    "acknowledgedBy": row["acknowledged_by"], "note": row["note"], "details": json.loads(row["details_json"]),
                })
            events = []
            for row in connection.execute("select * from events order by sequence desc limit ?", (max(1, min(int(limit), 1000)),)):
                events.append({"sequence": row["sequence"], "eventType": row["event_type"], "createdAt": _iso(row["created_at"]), "actor": row["actor"], "payload": json.loads(row["payload_json"]), "signature": row["signature"]})
            baseline_history = []
            for row in connection.execute("select * from baselines order by created_at desc,id desc limit ?", (max(1, min(int(limit), 1000)),)):
                baseline_history.append({
                    "id": row["id"], "createdAt": _iso(row["created_at"]), "actor": row["actor"],
                    "reason": row["reason"], "snapshotSha256": row["snapshot_sha256"],
                    "signature": row["signature"], "active": bool(row["active"]),
                })
            active = [row for row in findings if row["resolvedAt"] is None]
            integrity = self.verify(connection)
            return {
                "ok": True,
                "sealed": bool(baseline),
                "state": "invalid" if not integrity["ok"] else "unsealed" if not baseline else "drift" if active else "attested",
                "baseline": None if not baseline else {key: baseline[key] for key in ("id", "createdAt", "actor", "reason", "snapshotSha256", "signature")},
                "latestObservation": None if not latest else {
                    "observedAt": _iso(latest["observed_at"]), "baselineId": latest["baseline_id"], "snapshotSha256": latest["snapshot_sha256"],
                    "driftCount": latest["drift_count"], "criticalCount": latest["critical_count"], "maintenanceActive": bool(latest["maintenance_active"]),
                },
                "openFindings": active,
                "findings": findings,
                "baselineHistory": baseline_history,
                "events": events,
                "integrity": integrity,
            }
        finally:
            connection.close()

    def initialize_if_needed(self):
        if not self.database.exists():
            self.initialize()

    def verify(self, connection=None):
        if not self.database.exists():
            return {"ok": False, "sqlite": "missing", "baselineSignaturesValid": False, "eventChainValid": False}
        own = connection is None
        connection = connection or self.connect(readonly=True)
        try:
            # Bind this complete verification to one key, then detect rotation
            # before accepting it. Reading the file per retained row overloads
            # filesystem/sensor work without strengthening the row HMACs.
            secret = self.secret
            integrity = connection.execute("pragma integrity_check").fetchone()[0]
            triggers = {row["name"] for row in connection.execute("select name from sqlite_master where type='trigger'")}
            required = {"desired_state_baselines_no_update", "desired_state_baselines_no_delete", "desired_state_events_no_update", "desired_state_events_no_delete"}
            baselines_valid = True
            for row in connection.execute("select * from baselines order by created_at,id"):
                snapshot = json.loads(row["snapshot_json"])
                document = {"id": row["id"], "createdAt": row["created_at"], "actor": row["actor"], "reason": row["reason"], "snapshotSha256": row["snapshot_sha256"], "snapshot": snapshot}
                if hashlib.sha256(_canonical(snapshot).encode()).hexdigest() != row["snapshot_sha256"] or not hmac.compare_digest(self._sign(document, secret=secret), row["signature"]):
                    baselines_valid = False
            observations_valid = True
            for row in connection.execute("select * from observations order by id"):
                document = {
                    "observedAt": row["observed_at"], "baselineId": row["baseline_id"],
                    "snapshotSha256": row["snapshot_sha256"], "driftCount": row["drift_count"],
                    "criticalCount": row["critical_count"], "maintenanceActive": bool(row["maintenance_active"]),
                }
                if not hmac.compare_digest(self._sign(document, secret=secret), row["signature"]):
                    observations_valid = False
            findings_valid = True
            for row in connection.execute("select * from findings order by id"):
                if not row["signature"] or not hmac.compare_digest(self._sign(self._finding_document(row), secret=secret), row["signature"]):
                    findings_valid = False
            events_valid = True
            previous = None
            event_count = 0
            for row in connection.execute("select * from events order by sequence"):
                event_count += 1
                document = {"eventType": row["event_type"], "createdAt": row["created_at"], "actor": row["actor"], "payload": json.loads(row["payload_json"]), "previousSignature": row["previous_signature"]}
                if row["previous_signature"] != previous or not hmac.compare_digest(self._sign(document, secret=secret), row["signature"]):
                    events_valid = False
                previous = row["signature"]
            if not hmac.compare_digest(secret, self.secret):
                raise ValueError("desired-state HMAC key changed during verification")
            ok = integrity == "ok" and required.issubset(triggers) and baselines_valid and observations_valid and findings_valid and events_valid
            return {"ok": ok, "sqlite": integrity, "appendOnlyTriggers": required.issubset(triggers), "baselineSignaturesValid": baselines_valid, "observationSignaturesValid": observations_valid, "findingSignaturesValid": findings_valid, "eventChainValid": events_valid, "eventCount": event_count, "lastEventSignature": previous}
        except (sqlite3.Error, ValueError, json.JSONDecodeError, OSError) as exc:
            return {
                "ok": False, "sqlite": "error", "baselineSignaturesValid": False,
                "observationSignaturesValid": False, "findingSignaturesValid": False,
                "eventChainValid": False, "error": str(exc),
            }
        finally:
            if own:
                connection.close()

    def backup(self, target):
        self.initialize_if_needed()
        target = pathlib.Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Evidence directories contain private snapshots and require owner-only traversal.
        os.chmod(target.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        source = self.connect(readonly=True)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None:
            os.chown(target, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)
        integrity = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            result = integrity.execute("pragma integrity_check").fetchone()[0]
        finally:
            integrity.close()
        if result != "ok":
            target.unlink(missing_ok=True)
            raise RuntimeError(f"desired-state backup integrity failed: {result}")
        return {"path": str(target), "bytes": target.stat().st_size, "sha256": _digest_file(target), "integrity": result}

    def prometheus(self):
        status = self.status()
        latest = status.get("latestObservation") or {}
        observed_at = str(_datetime.datetime.fromisoformat(latest["observedAt"]).timestamp()) if latest.get("observedAt") else "NaN"
        open_rows = status.get("openFindings") or []
        maintenance = bool(latest.get("maintenanceActive"))
        critical = sum(1 for row in open_rows if row["critical"])
        return "\n".join([
            "# HELP dash_desired_state_collector_up Desired-state ledger and HMAC verification are readable.",
            "# TYPE dash_desired_state_collector_up gauge",
            f"dash_desired_state_collector_up {1 if status['integrity']['ok'] else 0}",
            f"dash_desired_state_sealed {1 if status['sealed'] else 0}",
            f"dash_desired_state_last_observation_timestamp_seconds {observed_at}",
            f"dash_desired_state_open_drift {len(open_rows)}",
            f"dash_desired_state_open_critical_drift {critical}",
            f"dash_desired_state_alertable_critical_drift {0 if maintenance else critical}",
            f"dash_desired_state_maintenance_active {1 if maintenance else 0}",
        ]) + "\n"
