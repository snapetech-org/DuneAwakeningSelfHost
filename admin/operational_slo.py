"""Time-weighted SLO, error-budget, maintenance, and incident accounting.

The store is deliberately independent from the game database. Samples are
bounded-retention operational observations; incident events are append-only and
globally hash chained so reliability history cannot be silently rewritten.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import time


ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
WINDOWS = (3600, 21600, 86400, 604800, 2592000)
EVENT_TYPES = {"opened", "acknowledged", "note", "resolved"}


class SLOError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _iso(epoch):
    return _datetime.datetime.fromtimestamp(float(epoch), _datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_epoch(value):
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("timestamp must be Unix seconds or ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.timestamp()


def validate_policy(payload):
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("operational SLO policy version must be 1")
    interval = int(payload.get("sampleIntervalSeconds", 60))
    max_gap = int(payload.get("maxSampleGapSeconds", 300))
    retention = int(payload.get("sampleRetentionDays", 90))
    if not 10 <= interval <= 3600:
        raise ValueError("sampleIntervalSeconds must be between 10 and 3600")
    if not interval <= max_gap <= 86400:
        raise ValueError("maxSampleGapSeconds must be at least the sample interval and at most 86400")
    if not 7 <= retention <= 3650:
        raise ValueError("sampleRetentionDays must be between 7 and 3650")
    objectives = payload.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        raise ValueError("policy objectives must be a nonempty array")
    seen = set()
    normalized = []
    for raw in objectives:
        if not isinstance(raw, dict):
            raise ValueError("each objective must be an object")
        objective_id = str(raw.get("id") or "").strip()
        signal = str(raw.get("signal") or "").strip()
        if not ID_RE.fullmatch(objective_id) or objective_id in seen:
            raise ValueError(f"invalid or duplicate objective id: {objective_id!r}")
        if not ID_RE.fullmatch(signal):
            raise ValueError(f"invalid signal for {objective_id}")
        seen.add(objective_id)
        target = float(raw.get("targetAvailability", 0))
        if not 0.5 <= target < 1.0:
            raise ValueError(f"targetAvailability for {objective_id} must be in [0.5,1)")
        failures = int(raw.get("consecutiveFailures", 2))
        if not 1 <= failures <= 60:
            raise ValueError(f"consecutiveFailures for {objective_id} must be 1..60")
        severity = str(raw.get("severity") or "warning").strip().lower()
        if severity not in ("warning", "critical"):
            raise ValueError(f"severity for {objective_id} must be warning or critical")
        name = str(raw.get("name") or objective_id).strip()
        description = str(raw.get("description") or "").strip()
        if not name or len(name) > 120 or len(description) > 1000:
            raise ValueError(f"invalid name or description for {objective_id}")
        normalized.append({
            "id": objective_id,
            "name": name,
            "description": description,
            "signal": signal,
            "targetAvailability": target,
            "severity": severity,
            "consecutiveFailures": failures,
            "excludeMaintenance": bool(raw.get("excludeMaintenance", True)),
        })
    return {
        "version": 1,
        "sampleIntervalSeconds": interval,
        "maxSampleGapSeconds": max_gap,
        "sampleRetentionDays": retention,
        "objectives": normalized,
    }


def load_policy(path):
    path = pathlib.Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"operational SLO policy does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"operational SLO policy is invalid JSON: {exc}") from exc
    return validate_policy(payload)


class Store:
    def __init__(self, path, policy_path, *, owner_uid=None, owner_gid=None, now=time.time):
        self.path = pathlib.Path(path)
        self.policy_path = pathlib.Path(policy_path)
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None
        self.now = now

    @property
    def policy(self):
        return load_policy(self.policy_path)

    def _secure(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            os.chmod(self.path, 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None and self.owner_gid is not None:
            os.chown(self.path.parent, self.owner_uid, self.owner_gid)
            if self.path.exists():
                os.chown(self.path, self.owner_uid, self.owner_gid)

    @contextlib.contextmanager
    def connect(self, *, write=False):
        self._secure()
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("pragma foreign_keys=on")
            connection.execute("pragma busy_timeout=15000")
            connection.execute("pragma journal_mode=wal")
            connection.execute("pragma synchronous=full")
            if write:
                connection.execute("begin immediate")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()
            self._secure()

    def initialize(self):
        policy = self.policy
        with self.connect(write=True) as connection:
            connection.executescript("""
                create table if not exists snapshots (
                  id text primary key,
                  observed_at real not null,
                  context_json text not null
                );
                create table if not exists samples (
                  id integer primary key autoincrement,
                  snapshot_id text not null references snapshots(id) on delete cascade,
                  objective_id text not null,
                  observed_at real not null,
                  interval_seconds real not null check(interval_seconds > 0),
                  good integer not null check(good in (0,1)),
                  excluded integer not null check(excluded in (0,1)),
                  signal_json text,
                  reason text not null,
                  unique(snapshot_id, objective_id)
                );
                create index if not exists samples_objective_time on samples(objective_id, observed_at);
                create table if not exists incidents (
                  id text primary key,
                  objective_id text not null,
                  severity text not null check(severity in ('warning','critical')),
                  status text not null check(status in ('open','resolved')),
                  opened_at real not null,
                  last_seen_at real not null,
                  resolved_at real,
                  failure_count integer not null,
                  acknowledged_at real,
                  acknowledged_by text,
                  summary text not null
                );
                create unique index if not exists incidents_one_open on incidents(objective_id) where status='open';
                create index if not exists incidents_time on incidents(opened_at desc);
                create table if not exists incident_events (
                  sequence integer primary key autoincrement,
                  incident_id text not null references incidents(id),
                  objective_id text not null,
                  event_type text not null check(event_type in ('opened','acknowledged','note','resolved')),
                  created_at real not null,
                  actor text,
                  note text,
                  payload_json text not null,
                  previous_hash text,
                  event_hash text not null unique
                );
                create trigger if not exists incident_events_no_update
                before update on incident_events begin select raise(abort,'incident events are append-only'); end;
                create trigger if not exists incident_events_no_delete
                before delete on incident_events begin select raise(abort,'incident events are append-only'); end;
                create table if not exists maintenance_windows (
                  id text primary key,
                  starts_at real not null,
                  ends_at real not null check(ends_at > starts_at),
                  reason text not null,
                  created_at real not null,
                  created_by text not null,
                  cancelled_at real,
                  cancelled_by text
                );
                create index if not exists maintenance_time on maintenance_windows(starts_at, ends_at);
                create table if not exists metadata (key text primary key, value text not null);
            """)
            connection.execute("insert into metadata(key,value) values('schema_version','1') on conflict(key) do nothing")
            connection.execute("insert into metadata(key,value) values('policy_sha256',?) on conflict(key) do update set value=excluded.value", (hashlib.sha256(_canonical(policy).encode()).hexdigest(),))
        return self.status(limit=20)

    def integrity_check(self):
        with self.connect() as connection:
            result = connection.execute("pragma integrity_check").fetchone()[0]
            event_rows = connection.execute("select * from incident_events order by sequence").fetchall()
        previous = None
        chain_ok = True
        for row in event_rows:
            try:
                payload = {
                    "incidentId": row["incident_id"], "objectiveId": row["objective_id"],
                    "eventType": row["event_type"], "createdAt": row["created_at"],
                    "actor": row["actor"], "note": row["note"],
                    "payload": json.loads(row["payload_json"]), "previousHash": row["previous_hash"],
                }
                expected = hashlib.sha256(_canonical(payload).encode()).hexdigest()
            except (json.JSONDecodeError, TypeError, ValueError):
                chain_ok = False
                break
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                chain_ok = False
                break
            previous = row["event_hash"]
        return {"ok": result == "ok" and chain_ok, "sqlite": result, "eventChainValid": chain_ok, "eventCount": len(event_rows), "lastEventHash": previous}

    def _event(self, connection, incident_id, objective_id, event_type, epoch, *, actor=None, note=None, payload=None):
        if event_type not in EVENT_TYPES:
            raise ValueError("invalid incident event type")
        previous_row = connection.execute("select event_hash from incident_events order by sequence desc limit 1").fetchone()
        previous = previous_row[0] if previous_row else None
        document = {
            "incidentId": incident_id, "objectiveId": objective_id,
            "eventType": event_type, "createdAt": float(epoch),
            "actor": actor, "note": note, "payload": payload or {}, "previousHash": previous,
        }
        event_hash = hashlib.sha256(_canonical(document).encode()).hexdigest()
        connection.execute(
            "insert into incident_events(incident_id,objective_id,event_type,created_at,actor,note,payload_json,previous_hash,event_hash) values(?,?,?,?,?,?,?,?,?)",
            (incident_id, objective_id, event_type, epoch, actor, note, _canonical(payload or {}), previous, event_hash),
        )
        return event_hash

    def _active_maintenance(self, connection, epoch):
        return connection.execute(
            "select * from maintenance_windows where cancelled_at is null and starts_at<=? and ends_at>? order by starts_at limit 1",
            (epoch, epoch),
        ).fetchone()

    def record(self, signals, *, context=None, observed_at=None):
        if not isinstance(signals, dict):
            raise ValueError("signals must be an object")
        policy = self.policy
        epoch = float(self.now() if observed_at is None else observed_at)
        if not math.isfinite(epoch) or epoch <= 0:
            raise ValueError("observed_at must be positive Unix seconds")
        snapshot_id = _datetime.datetime.fromtimestamp(epoch, _datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + secrets.token_hex(5)
        opened = []
        resolved = []
        rows = []
        with self.connect(write=True) as connection:
            maintenance = self._active_maintenance(connection, epoch)
            connection.execute("insert into snapshots(id,observed_at,context_json) values(?,?,?)", (snapshot_id, epoch, _canonical(context or {})))
            for objective in policy["objectives"]:
                previous = connection.execute(
                    "select observed_at from samples where objective_id=? order by observed_at desc limit 1",
                    (objective["id"],),
                ).fetchone()
                raw_interval = policy["sampleIntervalSeconds"] if not previous else max(1.0, epoch - float(previous[0]))
                interval = min(float(policy["maxSampleGapSeconds"]), raw_interval)
                present = objective["signal"] in signals
                value = signals.get(objective["signal"])
                good = bool(value) if present else False
                reason = "good" if good else "signal false" if present else "signal missing"
                excluded = bool(maintenance and objective["excludeMaintenance"])
                connection.execute(
                    "insert into samples(snapshot_id,objective_id,observed_at,interval_seconds,good,excluded,signal_json,reason) values(?,?,?,?,?,?,?,?)",
                    (snapshot_id, objective["id"], epoch, interval, int(good), int(excluded), _canonical(value), reason),
                )
                row = {"id": objective["id"], "good": good, "excluded": excluded, "reason": reason, "intervalSeconds": interval}
                rows.append(row)
                if excluded:
                    continue
                incident = connection.execute("select * from incidents where objective_id=? and status='open'", (objective["id"],)).fetchone()
                if good:
                    if incident:
                        connection.execute("update incidents set status='resolved',resolved_at=?,last_seen_at=? where id=?", (epoch, epoch, incident["id"]))
                        self._event(connection, incident["id"], objective["id"], "resolved", epoch, payload={"snapshotId": snapshot_id})
                        resolved.append(incident["id"])
                    continue
                streak_rows = connection.execute(
                    "select good,excluded from samples where objective_id=? order by observed_at desc limit ?",
                    (objective["id"], objective["consecutiveFailures"]),
                ).fetchall()
                streak = len(streak_rows) >= objective["consecutiveFailures"] and all(not item["good"] and not item["excluded"] for item in streak_rows)
                if incident:
                    connection.execute("update incidents set last_seen_at=?,failure_count=failure_count+1 where id=?", (epoch, incident["id"]))
                elif streak:
                    incident_id = "slo-" + objective["id"] + "-" + secrets.token_hex(8)
                    summary = f"{objective['name']} SLO signal is failing"
                    connection.execute(
                        "insert into incidents(id,objective_id,severity,status,opened_at,last_seen_at,failure_count,summary) values(?,?,?,'open',?,?,?,?)",
                        (incident_id, objective["id"], objective["severity"], epoch, epoch, objective["consecutiveFailures"], summary),
                    )
                    self._event(connection, incident_id, objective["id"], "opened", epoch, payload={"snapshotId": snapshot_id, "reason": reason})
                    opened.append(incident_id)
            cutoff = epoch - policy["sampleRetentionDays"] * 86400
            connection.execute("delete from snapshots where observed_at<?", (cutoff,))
        return {"ok": all(row["good"] or row["excluded"] for row in rows), "snapshotId": snapshot_id, "observedAt": _iso(epoch), "maintenanceWindow": dict(maintenance) if maintenance else None, "objectives": rows, "incidentsOpened": opened, "incidentsResolved": resolved}

    def acknowledge(self, incident_id, actor, note=""):
        incident_id = str(incident_id or "").strip()[:128]
        actor = str(actor or "").strip()[:128]
        note = str(note or "").strip()[:2000]
        if not actor:
            raise ValueError("actor is required")
        epoch = float(self.now())
        with self.connect(write=True) as connection:
            incident = connection.execute("select * from incidents where id=? and status='open'", (incident_id,)).fetchone()
            if not incident:
                raise ValueError("open incident does not exist")
            connection.execute("update incidents set acknowledged_at=?,acknowledged_by=? where id=?", (epoch, actor, incident["id"]))
            event_hash = self._event(connection, incident["id"], incident["objective_id"], "acknowledged", epoch, actor=actor, note=note)
        return {"ok": True, "incidentId": incident_id, "acknowledgedAt": _iso(epoch), "actor": actor, "eventHash": event_hash}

    def add_note(self, incident_id, actor, note):
        incident_id = str(incident_id or "").strip()[:128]
        actor = str(actor or "").strip()[:128]
        note = str(note or "").strip()[:2000]
        if not actor or not note:
            raise ValueError("actor and note are required")
        epoch = float(self.now())
        with self.connect(write=True) as connection:
            incident = connection.execute("select * from incidents where id=?", (incident_id,)).fetchone()
            if not incident:
                raise ValueError("incident does not exist")
            event_hash = self._event(connection, incident["id"], incident["objective_id"], "note", epoch, actor=actor, note=note)
        return {"ok": True, "incidentId": incident_id, "createdAt": _iso(epoch), "eventHash": event_hash}

    def create_maintenance(self, starts_at, ends_at, reason, actor):
        start = _parse_epoch(starts_at)
        end = _parse_epoch(ends_at)
        now = float(self.now())
        reason = str(reason or "").strip()[:1000]
        actor = str(actor or "").strip()[:128]
        if not reason or not actor:
            raise ValueError("maintenance reason and actor are required")
        if start < now - 300 or end <= start or end - start > 24 * 3600:
            raise ValueError("maintenance must start no more than five minutes ago and last at most 24 hours")
        identifier = "maintenance-" + secrets.token_hex(8)
        with self.connect(write=True) as connection:
            overlap = connection.execute(
                "select id from maintenance_windows where cancelled_at is null and starts_at<? and ends_at>? limit 1",
                (end, start),
            ).fetchone()
            if overlap:
                raise ValueError("maintenance window overlaps an existing active window")
            connection.execute(
                "insert into maintenance_windows(id,starts_at,ends_at,reason,created_at,created_by) values(?,?,?,?,?,?)",
                (identifier, start, end, reason, now, actor),
            )
        return {"ok": True, "id": identifier, "startsAt": _iso(start), "endsAt": _iso(end), "reason": reason, "createdBy": actor}

    def cancel_maintenance(self, identifier, actor):
        actor = str(actor or "").strip()[:128]
        if not actor:
            raise ValueError("actor is required")
        epoch = float(self.now())
        with self.connect(write=True) as connection:
            row = connection.execute("select * from maintenance_windows where id=? and cancelled_at is null", (str(identifier),)).fetchone()
            if not row:
                raise ValueError("active maintenance window does not exist")
            if float(row["ends_at"]) <= epoch:
                raise ValueError("completed maintenance history cannot be canceled")
            connection.execute("update maintenance_windows set cancelled_at=?,cancelled_by=? where id=?", (epoch, actor, row["id"]))
        return {"ok": True, "id": identifier, "cancelledAt": _iso(epoch), "cancelledBy": actor}

    @staticmethod
    def _window(samples, cutoff, now, target):
        good_seconds = 0.0
        bad_seconds = 0.0
        excluded_seconds = 0.0
        for sample in samples:
            end = min(now, float(sample["observed_at"]))
            start = max(cutoff, end - float(sample["interval_seconds"]))
            duration = max(0.0, end - start)
            if sample["excluded"]:
                excluded_seconds += duration
            elif sample["good"]:
                good_seconds += duration
            else:
                bad_seconds += duration
        return Store._window_from_seconds(good_seconds, bad_seconds, excluded_seconds, cutoff, now, target)

    @staticmethod
    def _window_from_seconds(good_seconds, bad_seconds, excluded_seconds, cutoff, now, target):
        observed = good_seconds + bad_seconds
        availability = good_seconds / observed if observed else None
        allowed_ratio = 1.0 - target
        burn = (bad_seconds / observed) / allowed_ratio if observed and allowed_ratio else None
        remaining = max(0.0, 1.0 - burn) if burn is not None else None
        return {
            "availability": availability,
            "goodSeconds": round(good_seconds, 3),
            "badSeconds": round(bad_seconds, 3),
            "excludedSeconds": round(excluded_seconds, 3),
            "observedSeconds": round(observed, 3),
            "coverage": min(1.0, observed / max(1.0, now - cutoff)),
            "burnRate": burn,
            "errorBudgetRemaining": remaining,
        }

    @staticmethod
    def _aggregate_window(connection, objective_id, oldest, cutoff, now, target):
        # Preserve interval clipping and exclusion semantics without materializing
        # hundreds of thousands of Python rows for every status request.
        row = connection.execute("""
            with durations as (
                select good, excluded,
                    max(0.0, min(:now, observed_at) -
                        max(:cutoff, min(:now, observed_at) - interval_seconds)) as duration
                from samples
                where objective_id=:objective and observed_at>=:oldest
                order by observed_at
            )
            select
                total(case when not excluded and good then duration else 0.0 end),
                total(case when not excluded and not good then duration else 0.0 end),
                total(case when excluded then duration else 0.0 end)
            from durations
        """, {"now": now, "cutoff": cutoff, "oldest": oldest, "objective": objective_id}).fetchone()
        return Store._window_from_seconds(*row, cutoff, now, target)

    def status(self, *, limit=100, now=None):
        policy = self.policy
        epoch = float(self.now() if now is None else now)
        oldest = epoch - max(WINDOWS)
        with self.connect() as connection:
            connection.execute("begin")  # One consistent view for every objective and window.
            latest = {}
            windows_by_objective = {}
            for objective in policy["objectives"]:
                latest[objective["id"]] = connection.execute(
                    "select * from samples where objective_id=? and observed_at>=? order by observed_at desc, rowid desc limit 1",
                    (objective["id"], oldest - policy["maxSampleGapSeconds"]),
                ).fetchone()
                windows_by_objective[objective["id"]] = {
                    str(seconds): self._aggregate_window(connection, objective["id"],
                        oldest - policy["maxSampleGapSeconds"], epoch - seconds, epoch, objective["targetAvailability"])
                    for seconds in WINDOWS
                }
            incidents = connection.execute("select * from incidents order by opened_at desc limit ?", (max(1, min(int(limit), 1000)),)).fetchall()
            events = connection.execute("select * from incident_events order by sequence desc limit ?", (max(1, min(int(limit), 1000)),)).fetchall()
            maintenance = connection.execute("select * from maintenance_windows where ends_at>=? or cancelled_at is not null order by starts_at desc limit 100", (epoch - 30 * 86400,)).fetchall()
            last_snapshot = connection.execute("select id,observed_at,context_json from snapshots order by observed_at desc limit 1").fetchone()
        open_by_objective = {row["objective_id"]: dict(row) for row in incidents if row["status"] == "open"}
        objectives = []
        for objective in policy["objectives"]:
            current = latest[objective["id"]]
            windows = windows_by_objective[objective["id"]]
            primary = windows[str(max(WINDOWS))]
            objectives.append({
                **objective,
                "currentGood": bool(current["good"]) if current else None,
                "currentExcluded": bool(current["excluded"]) if current else None,
                "currentReason": current["reason"] if current else "no data",
                "lastObservedAt": _iso(current["observed_at"]) if current else None,
                "windows": windows,
                "availability": primary["availability"],
                "errorBudgetRemaining": primary["errorBudgetRemaining"],
                "openIncident": open_by_objective.get(objective["id"]),
            })
        open_incidents = [dict(row) for row in incidents if row["status"] == "open"]
        critical_open = any(row["severity"] == "critical" for row in open_incidents)
        any_open = bool(open_incidents)
        no_data = not last_snapshot
        overall = "no-data" if no_data else "critical" if critical_open else "degraded" if any_open else "healthy"
        active_maintenance = next((dict(row) for row in maintenance if not row["cancelled_at"] and row["starts_at"] <= epoch < row["ends_at"]), None)
        return {
            "ok": overall == "healthy",
            "overall": overall,
            "generatedAt": _iso(epoch),
            "policy": policy,
            "objectives": objectives,
            "openIncidents": open_incidents,
            "incidents": [dict(row) for row in incidents],
            "events": [{**dict(row), "payload": json.loads(row["payload_json"])} for row in events],
            "maintenanceWindows": [dict(row) for row in maintenance],
            "activeMaintenance": active_maintenance,
            "lastSnapshot": ({"id": last_snapshot["id"], "observedAt": _iso(last_snapshot["observed_at"]), "observedAtEpoch": last_snapshot["observed_at"], "context": json.loads(last_snapshot["context_json"])} if last_snapshot else None),
        }

    def prometheus(self, now=None):
        status = self.status(limit=100, now=now)
        epoch = float(self.now() if now is None else now)
        lines = [
            "# HELP dash_slo_collector_up Whether the SLO collector has recorded any snapshot.",
            "# TYPE dash_slo_collector_up gauge",
            f"dash_slo_collector_up {1 if status['lastSnapshot'] else 0}",
            "# HELP dash_slo_last_snapshot_timestamp_seconds Unix timestamp of the newest SLO snapshot.",
            "# TYPE dash_slo_last_snapshot_timestamp_seconds gauge",
            f"dash_slo_last_snapshot_timestamp_seconds {status['lastSnapshot']['observedAtEpoch'] if status['lastSnapshot'] else 0}",
            "# HELP dash_slo_open_incidents Current SLO incidents by severity.",
            "# TYPE dash_slo_open_incidents gauge",
        ]
        for severity in ("warning", "critical"):
            lines.append(f'dash_slo_open_incidents{{severity="{severity}"}} {sum(1 for row in status["openIncidents"] if row["severity"] == severity)}')
        lines.extend(["# HELP dash_slo_maintenance_active Whether a planned-maintenance exclusion is active.", "# TYPE dash_slo_maintenance_active gauge", f"dash_slo_maintenance_active {1 if status['activeMaintenance'] else 0}"])
        for objective in status["objectives"]:
            labels = f'objective="{objective["id"]}",severity="{objective["severity"]}"'
            lines.append(f'dash_slo_objective_good{{{labels}}} {1 if objective["currentGood"] else 0}')
            for window, metric in objective["windows"].items():
                window_labels = labels + f',window_seconds="{window}"'
                availability = metric["availability"] if metric["availability"] is not None else "NaN"
                burn = metric["burnRate"] if metric["burnRate"] is not None else "NaN"
                remaining = metric["errorBudgetRemaining"] if metric["errorBudgetRemaining"] is not None else "NaN"
                lines.append(f'dash_slo_availability_ratio{{{window_labels}}} {availability}')
                lines.append(f'dash_slo_error_budget_burn_rate{{{window_labels}}} {burn}')
                lines.append(f'dash_slo_error_budget_remaining_ratio{{{window_labels}}} {remaining}')
                lines.append(f'dash_slo_observation_coverage_ratio{{{window_labels}}} {metric["coverage"]}')
        lines.append(f"dash_slo_metrics_generated_timestamp_seconds {epoch}")
        return "\n".join(lines) + "\n"

    def backup(self, destination):
        destination = pathlib.Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        try:
            with self.connect() as source, contextlib.closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                if target.execute("pragma integrity_check").fetchone()[0] != "ok":
                    raise SLOError("operational SLO backup failed integrity_check")
                target.commit()
            os.chmod(temporary, 0o600)
            if os.geteuid() == 0 and self.owner_uid is not None and self.owner_gid is not None:
                os.chown(temporary, self.owner_uid, self.owner_gid)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"ok": True, "path": str(destination), "bytes": destination.stat().st_size}
