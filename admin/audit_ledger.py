"""Tamper-evident, append-only evidence for DASH admin audit events."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from contextlib import closing


SCHEMA_VERSION = 1
ZERO_HMAC = "0" * 64
EVENT_ID_PATTERN = re.compile(r"^audit-[0-9a-f]{32}$")
REQUEST_ID_PATTERN = re.compile(r"^request-[0-9a-f]{32}$")
MAX_EVENT_BYTES = 64 * 1024
VERIFY_CACHE_SECONDS = 60


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_time(value: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self, database, *, key_path=None, anchor_path=None, owner_uid=None, owner_gid=None):
        self.database = pathlib.Path(database)
        self.key_path = pathlib.Path(key_path) if key_path else self.database.with_suffix(".hmac.key")
        self.anchor_path = pathlib.Path(anchor_path) if anchor_path else self.database.with_suffix(".anchor.json")
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None
        self.lock = threading.RLock()
        self.append_failures = 0
        self._verified_integrity = None
        self._verified_artifacts = None
        self._verified_at = 0.0

    def _artifact_signature(self):
        signature = []
        for index, path in enumerate((self.database.parent, self.database, self.key_path, self.anchor_path)):
            stat_result = path.lstat()
            stable = (
                stat_result.st_dev, stat_result.st_ino, stat_result.st_mode,
                stat_result.st_uid, stat_result.st_gid,
            )
            # The shared parent contains unrelated, frequently rewritten Admin
            # state. Its ownership/mode are security-relevant; its directory
            # size and mtime are not ledger artifacts and must not invalidate a
            # verified 80k+ event chain on every autoscaler state rename.
            signature.append(stable if index == 0 else stable + (
                stat_result.st_size, stat_result.st_mtime_ns,
            ))
        return tuple(signature)

    def _remember_verification(self, integrity):
        self._verified_integrity = dict(integrity)
        self._verified_artifacts = self._artifact_signature()
        self._verified_at = time.monotonic()

    def _cached_verification(self):
        if self._verified_integrity is None or self._verified_artifacts is None:
            return None
        if time.monotonic() - self._verified_at > VERIFY_CACHE_SECONDS:
            return None
        try:
            if self._artifact_signature() != self._verified_artifacts:
                return None
        except OSError:
            return None
        return dict(self._verified_integrity)

    def _forget_verification(self):
        self._verified_integrity = None
        self._verified_artifacts = None
        self._verified_at = 0.0

    def _chown(self, path):
        if self.owner_uid is not None or self.owner_gid is not None:
            os.chown(path, self.owner_uid if self.owner_uid is not None else -1, self.owner_gid if self.owner_gid is not None else -1)

    def _private_path(self, path, mode):
        try:
            os.chmod(path, mode)
            self._chown(path)
        except FileNotFoundError:
            pass

    def _connect(self):
        if self.database.is_symlink():
            raise RuntimeError("audit ledger database must not be a symbolic link")
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        connection.execute("pragma busy_timeout=10000")
        # Full chain verification remains mandatory. A bounded read-only page
        # mapping avoids a network-filesystem metadata round trip per SQLite
        # page; unsupported builds retain SQLite's normal read fallback.
        connection.execute("pragma mmap_size=1073741824")
        return connection

    def _key(self) -> bytes:
        if self.key_path.is_symlink():
            raise RuntimeError("audit ledger HMAC key must not be a symbolic link")
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("audit ledger HMAC key must contain exactly 32 bytes")
        return key

    def initialize(self):
        with self.lock:
            if any(path.is_symlink() for path in (self.database, self.key_path, self.anchor_path)):
                raise RuntimeError("audit ledger artifacts must not be symbolic links")
            self.database.parent.mkdir(parents=True, exist_ok=True)
            self._private_path(self.database.parent, 0o700)
            if not self.key_path.exists():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                descriptor = os.open(self.key_path, flags, 0o600)
                try:
                    os.write(descriptor, secrets.token_bytes(32))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._private_path(self.key_path, 0o600)
            self._key()
            with closing(self._connect()) as connection:
                connection.executescript(
                    """
                    create table if not exists metadata (
                      key text primary key,
                      value text not null
                    );
                    create table if not exists events (
                      sequence integer primary key autoincrement,
                      event_id text not null unique,
                      occurred_at text not null,
                      action text not null,
                      ok integer not null check (ok in (0, 1)),
                      request_id text,
                      event_json text not null,
                      event_sha256 text not null,
                      previous_hmac_sha256 text not null,
                      event_hmac_sha256 text not null
                    );
                    create index if not exists events_request_idx on events(request_id, sequence);
                    create trigger if not exists audit_events_no_update
                      before update on events begin
                        select raise(abort, 'audit ledger events are append-only');
                      end;
                    create trigger if not exists audit_events_no_delete
                      before delete on events begin
                        select raise(abort, 'audit ledger events are append-only');
                      end;
                    """
                )
                row = connection.execute("select value from metadata where key='schema_version'").fetchone()
                if row and int(row["value"]) != SCHEMA_VERSION:
                    raise RuntimeError(f"unsupported audit ledger schema version {row['value']}")
                connection.execute(
                    "insert into metadata(key,value) values('schema_version',?) on conflict(key) do nothing",
                    (str(SCHEMA_VERSION),),
                )
                count = int(connection.execute("select count(*) from events").fetchone()[0])
            self._private_path(self.database, 0o600)
            if not self.anchor_path.exists():
                if count:
                    raise RuntimeError("audit ledger head anchor is missing for a non-empty ledger")
                self._write_anchor(0, ZERO_HMAC)
            self._read_anchor()
            return self.status()

    @staticmethod
    def _event_hmac(key: bytes, sequence: int, event_sha256: str, previous_hmac_sha256: str) -> str:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "sequence": int(sequence),
            "eventSha256": event_sha256,
            "previousHmacSha256": previous_hmac_sha256,
        }
        return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()

    @staticmethod
    def _anchor_hmac(key: bytes, sequence: int, head_hmac_sha256: str, updated_at: str) -> str:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "sequence": int(sequence),
            "headHmacSha256": head_hmac_sha256,
            "updatedAt": updated_at,
        }
        return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()

    def _write_anchor(self, sequence: int, head_hmac_sha256: str):
        updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "sequence": int(sequence),
            "headHmacSha256": head_hmac_sha256,
            "updatedAt": updated_at,
        }
        document["anchorHmacSha256"] = self._anchor_hmac(self._key(), sequence, head_hmac_sha256, updated_at)
        temporary = self.anchor_path.with_name(f".{self.anchor_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.anchor_path)
            self._private_path(self.anchor_path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_anchor(self):
        if self.anchor_path.is_symlink():
            raise RuntimeError("audit ledger head anchor must not be a symbolic link")
        try:
            document = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("audit ledger head anchor is unreadable") from exc
        required = {"schemaVersion", "sequence", "headHmacSha256", "updatedAt", "anchorHmacSha256"}
        if not isinstance(document, dict) or set(document) != required:
            raise RuntimeError("audit ledger head anchor has an invalid shape")
        if int(document["schemaVersion"]) != SCHEMA_VERSION or int(document["sequence"]) < 0:
            raise RuntimeError("audit ledger head anchor has invalid metadata")
        if not re.fullmatch(r"[0-9a-f]{64}", str(document["headHmacSha256"])):
            raise RuntimeError("audit ledger head anchor has an invalid head HMAC")
        expected = self._anchor_hmac(
            self._key(), int(document["sequence"]), document["headHmacSha256"], document["updatedAt"]
        )
        if not hmac.compare_digest(expected, str(document["anchorHmacSha256"])):
            raise RuntimeError("audit ledger head anchor HMAC verification failed")
        return document

    def _normalize_event(self, event):
        if not isinstance(event, dict):
            raise ValueError("audit event must be a JSON object")
        try:
            encoded = _canonical(event)
            normalized = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("audit event must contain only JSON-compatible values") from exc
        if not 2 <= len(encoded) <= MAX_EVENT_BYTES:
            raise ValueError(f"audit event must be at most {MAX_EVENT_BYTES} bytes")
        event_id = str(normalized.get("eventId") or "")
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ValueError("audit event requires an audit-<32 lowercase hex> eventId")
        occurred_at = str(normalized.get("ts") or "")
        if _parse_time(occurred_at) is None:
            raise ValueError("audit event requires an ISO-8601 ts")
        action = str(normalized.get("action") or "").strip()
        if not action or len(action) > 128:
            raise ValueError("audit event action must contain 1-128 characters")
        if not isinstance(normalized.get("ok"), bool):
            raise ValueError("audit event ok must be boolean")
        request_id = normalized.get("request_id")
        if request_id is not None and not REQUEST_ID_PATTERN.fullmatch(str(request_id)):
            raise ValueError("audit event request_id must be request-<32 lowercase hex>")
        return normalized, encoded, event_id, occurred_at, action, bool(normalized["ok"]), request_id

    def _assert_head(self, connection):
        row = connection.execute(
            "select sequence,event_hmac_sha256 from events order by sequence desc limit 1"
        ).fetchone()
        sequence = int(row["sequence"]) if row else 0
        head = row["event_hmac_sha256"] if row else ZERO_HMAC
        anchor = self._read_anchor()
        if int(anchor["sequence"]) != sequence or not hmac.compare_digest(anchor["headHmacSha256"], head):
            raise RuntimeError("audit ledger database head does not match its authenticated anchor")
        return sequence, head

    def append(self, event, *, verify_chain=False):
        normalized, encoded, event_id, occurred_at, action, ok, request_id = self._normalize_event(event)
        event_sha256 = _sha256(encoded)
        with self.lock:
            cached_integrity = self._cached_verification()
            verified_integrity = None
            try:
                with closing(self._connect()) as connection:
                    connection.execute("begin immediate")
                    try:
                        if verify_chain:
                            # A successful startup/request verification is cached and
                            # tied to the database/key/anchor artifact signature. Reuse
                            # that proof for the next append instead of rescanning the
                            # entire retained chain while holding the append lock. If
                            # the artifacts changed or the cache expired, verify once
                            # before admitting the append.
                            verified_integrity = cached_integrity or self._verify_connection(connection)
                        sequence, previous = self._assert_head(connection)
                        existing = connection.execute(
                            "select sequence,event_sha256,event_hmac_sha256 from events where event_id=?", (event_id,)
                        ).fetchone()
                        if existing:
                            if not hmac.compare_digest(existing["event_sha256"], event_sha256):
                                raise RuntimeError("audit event ID collision has a different payload")
                            connection.rollback()
                            return {
                                "ok": True,
                                "idempotent": True,
                                "sequence": int(existing["sequence"]),
                                "eventHmacSha256": existing["event_hmac_sha256"],
                            }
                        sequence += 1
                        event_hmac = self._event_hmac(self._key(), sequence, event_sha256, previous)
                        connection.execute(
                            """insert into events(
                                 sequence,event_id,occurred_at,action,ok,request_id,event_json,event_sha256,
                                 previous_hmac_sha256,event_hmac_sha256
                               ) values(?,?,?,?,?,?,?,?,?,?)""",
                            (
                                sequence, event_id, occurred_at, action, int(ok), request_id,
                                encoded.decode("utf-8"), event_sha256, previous, event_hmac,
                            ),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                self._write_anchor(sequence, event_hmac)
                self._private_path(self.database, 0o600)
                chain_integrity = verified_integrity or cached_integrity
                if chain_integrity is not None:
                    anchor = self._read_anchor()
                    chain_integrity.update({
                        "events": sequence,
                        "headSequence": sequence,
                        "headHmacSha256": event_hmac,
                        "anchorUpdatedAt": anchor["updatedAt"],
                    })
                    self._remember_verification(chain_integrity)
                else:
                    self._forget_verification()
                return {
                    "ok": True,
                    "idempotent": False,
                    "sequence": sequence,
                    "eventHmacSha256": event_hmac,
                    "eventSha256": event_sha256,
                }
            except Exception:
                self._forget_verification()
                self.append_failures += 1
                raise

    def _verify_connection(self, connection):
        for path in (self.database, self.key_path, self.anchor_path):
            if path.is_symlink():
                raise RuntimeError(f"audit ledger artifact must not be a symbolic link: {path.name}")
            if not path.is_file():
                raise RuntimeError(f"audit ledger artifact is missing or not a regular file: {path.name}")
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise RuntimeError(f"audit ledger artifact permissions must be 0600: {path.name}")
        if stat.S_IMODE(self.database.parent.stat().st_mode) & 0o077:
            raise RuntimeError("audit ledger directory must not grant group/world permissions")
        key = self._key()
        previous = ZERO_HMAC
        expected_sequence = 1
        triggers = {
            row[0] for row in connection.execute(
                "select name from sqlite_master where type='trigger' and tbl_name='events'"
            )
        }
        required_triggers = {"audit_events_no_update", "audit_events_no_delete"}
        if not required_triggers.issubset(triggers):
            raise RuntimeError("audit ledger append-only triggers are missing")
        rows = connection.execute("select * from events order by sequence").fetchall()
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                raise RuntimeError(f"audit ledger sequence gap at {expected_sequence}")
            try:
                event = json.loads(row["event_json"])
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"audit ledger event {sequence} contains invalid JSON") from exc
            encoded = _canonical(event)
            event_sha256 = _sha256(encoded)
            if not hmac.compare_digest(event_sha256, row["event_sha256"]):
                raise RuntimeError(f"audit ledger event {sequence} payload digest verification failed")
            expected_request = event.get("request_id")
            if (
                event.get("eventId") != row["event_id"]
                or event.get("ts") != row["occurred_at"]
                or event.get("action") != row["action"]
                or int(bool(event.get("ok"))) != int(row["ok"])
                or expected_request != row["request_id"]
            ):
                raise RuntimeError(f"audit ledger event {sequence} indexed fields do not match its payload")
            if not hmac.compare_digest(previous, row["previous_hmac_sha256"]):
                raise RuntimeError(f"audit ledger event {sequence} previous HMAC verification failed")
            expected_hmac = self._event_hmac(key, sequence, event_sha256, previous)
            if not hmac.compare_digest(expected_hmac, row["event_hmac_sha256"]):
                raise RuntimeError(f"audit ledger event {sequence} HMAC verification failed")
            previous = row["event_hmac_sha256"]
            expected_sequence += 1
        anchor = self._read_anchor()
        sequence = len(rows)
        if int(anchor["sequence"]) != sequence or not hmac.compare_digest(anchor["headHmacSha256"], previous):
            raise RuntimeError("audit ledger tail deletion or head mismatch detected")
        return {
            "ok": True,
            "schemaVersion": SCHEMA_VERSION,
            "events": sequence,
            "headSequence": sequence,
            "headHmacSha256": previous,
            "anchorUpdatedAt": anchor["updatedAt"],
        }

    def verify(self, *, force=False):
        with self.lock:
            if not force:
                cached = self._cached_verification()
                if cached is not None:
                    return cached
            with closing(self._connect()) as connection:
                integrity = self._verify_connection(connection)
            self._remember_verification(integrity)
            return dict(integrity)

    def list(self, limit=100):
        limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as connection:
            rows = connection.execute("select * from events order by sequence desc limit ?", (limit,)).fetchall()
        result = []
        for row in reversed(rows):
            event = json.loads(row["event_json"])
            event.update({
                "ledgerSequence": int(row["sequence"]),
                "ledgerHmacSha256": row["event_hmac_sha256"],
            })
            result.append(event)
        return result

    def backup(self, destination_database):
        destination_database = pathlib.Path(destination_database)
        destination_key = destination_database.with_name("audit-ledger.hmac.key")
        destination_anchor = destination_database.with_name("audit-ledger.anchor.json")
        destinations = (destination_database, destination_key, destination_anchor)
        with self.lock:
            verified = self.verify(force=True)
            destination_database.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(destination_database.parent, 0o700)
            for destination in destinations:
                destination.unlink(missing_ok=True)
            try:
                shutil.copyfile(self.key_path, destination_key)
                shutil.copyfile(self.anchor_path, destination_anchor)
                with closing(self._connect()) as source, closing(sqlite3.connect(destination_database)) as target:
                    target.execute("pragma mmap_size=1073741824")
                    source.backup(target)
                    if target.execute("pragma integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("audit ledger backup failed SQLite integrity_check")
                for destination in destinations:
                    self._private_path(destination, 0o600)
                copied = Store(
                    destination_database, key_path=destination_key, anchor_path=destination_anchor
                ).verify()
                return {
                    "ok": True,
                    "events": copied["events"],
                    "headSequence": copied["headSequence"],
                    "sourceHeadHmacSha256": verified["headHmacSha256"],
                    "database": str(destination_database),
                    "key": str(destination_key),
                    "anchor": str(destination_anchor),
                }
            except Exception:
                for destination in destinations:
                    destination.unlink(missing_ok=True)
                raise

    def _request_status(self):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """select request_id,action,occurred_at,event_json from events
                   where request_id is not null and action in (
                     'privileged-request-admitted','privileged-request-completed','privileged-request-reconciled'
                   )
                   order by sequence"""
            ).fetchall()
        requests = {}
        for row in rows:
            request = requests.setdefault(row["request_id"], {
                "id": row["request_id"], "admittedAt": None, "completedAt": None,
                "resolution": None, "path": None, "capability": None, "principalId": None,
                "admissionEventId": None,
            })
            if row["action"] == "privileged-request-admitted":
                request["admittedAt"] = row["occurred_at"]
                event = json.loads(row["event_json"])
                request.update({
                    "path": event.get("path"), "capability": event.get("capability"),
                    "principalId": event.get("principal_id"), "admissionEventId": event.get("eventId"),
                })
            else:
                request["completedAt"] = row["occurred_at"]
                request["resolution"] = "reconciled" if row["action"] == "privileged-request-reconciled" else "completed"
        open_rows = [value for value in requests.values() if value["admittedAt"] and not value["completedAt"]]
        admitted_times = [value for value in (_parse_time(row["admittedAt"]) for row in open_rows) if value is not None]
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        public_open = []
        for row in sorted(open_rows, key=lambda value: value["admittedAt"] or "")[:100]:
            admitted_at = _parse_time(row["admittedAt"])
            public_open.append({
                **row,
                "ageSeconds": max(0, int(now - admitted_at)) if admitted_at is not None else 0,
            })
        return {
            "admitted": sum(1 for value in requests.values() if value["admittedAt"]),
            "completed": sum(1 for value in requests.values() if value["completedAt"]),
            "reconciled": sum(1 for value in requests.values() if value["resolution"] == "reconciled"),
            "open": len(open_rows),
            "oldestOpenAgeSeconds": max(0, int(now - min(admitted_times))) if admitted_times else 0,
            "openRequests": public_open,
        }

    def request_state(self, request_id):
        request_id = str(request_id or "").strip()
        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ValueError("request ID must be request-<32 lowercase hex>")
        self.verify()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """select action,occurred_at,event_json from events
                   where request_id=? and action in (
                     'privileged-request-admitted','privileged-request-completed','privileged-request-reconciled'
                   ) order by sequence""",
                (request_id,),
            ).fetchall()
        admission = next((row for row in rows if row["action"] == "privileged-request-admitted"), None)
        if admission is None:
            raise ValueError("privileged request admission does not exist")
        event = json.loads(admission["event_json"])
        terminal = next((row for row in reversed(rows) if row["action"] != "privileged-request-admitted"), None)
        result = {
            "id": request_id,
            "open": terminal is None,
            "admittedAt": admission["occurred_at"],
            "path": event.get("path"),
            "capability": event.get("capability"),
            "principalId": event.get("principal_id"),
            "admissionEventId": event.get("eventId"),
        }
        if terminal is not None:
            result.update({
                "completedAt": terminal["occurred_at"],
                "resolution": "reconciled" if terminal["action"] == "privileged-request-reconciled" else "completed",
            })
        return result

    def status(self):
        try:
            integrity = self.verify()
            error = None
        except Exception as exc:
            integrity = {"ok": False, "events": 0, "headSequence": 0, "headHmacSha256": ""}
            error = str(exc)[:1000]
        try:
            requests = self._request_status()
        except (OSError, sqlite3.Error, ValueError):
            requests = {"admitted": 0, "completed": 0, "reconciled": 0, "open": 0, "oldestOpenAgeSeconds": 0, "openRequests": []}
        return {
            "ok": bool(integrity["ok"]),
            "ledger": integrity,
            "requests": requests,
            "appendFailures": self.append_failures,
            "error": error,
            "database": self.database.name,
            "anchor": self.anchor_path.name,
        }

    def prometheus(self, *, enabled=True):
        status = self.status()
        ledger = status["ledger"]
        requests = status["requests"]
        return "".join([
            f"dash_admin_audit_ledger_enabled {1 if enabled else 0}\n",
            f"dash_admin_audit_ledger_valid {1 if status['ok'] else 0}\n",
            f"dash_admin_audit_ledger_events {int(ledger.get('events') or 0)}\n",
            f"dash_admin_audit_ledger_head_sequence {int(ledger.get('headSequence') or 0)}\n",
            f"dash_admin_audit_ledger_append_failures_total {int(status['appendFailures'])}\n",
            f"dash_admin_audit_privileged_requests_admitted_total {int(requests['admitted'])}\n",
            f"dash_admin_audit_privileged_requests_completed_total {int(requests['completed'])}\n",
            f"dash_admin_audit_privileged_requests_reconciled_total {int(requests['reconciled'])}\n",
            f"dash_admin_audit_privileged_requests_open {int(requests['open'])}\n",
            f"dash_admin_audit_privileged_request_oldest_open_age_seconds {int(requests['oldestOpenAgeSeconds'])}\n",
        ])
