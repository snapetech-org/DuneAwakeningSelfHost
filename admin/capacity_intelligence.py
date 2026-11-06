#!/usr/bin/env python3
"""Retained autoscaler efficiency, cold-start, and retention-planning evidence."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import time
import uuid


WINDOWS = (86400, 604800, 2592000)
VALID_MODES = {"always-on", "dynamic", "disabled"}
VALID_STATES = {"running", "exited", "created", "restarting", "paused", "dead", "missing", "unknown"}


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return _datetime.datetime.fromtimestamp(float(value), _datetime.timezone.utc).isoformat()


def _number(value, name, minimum, maximum, *, integer=False):
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def load_policy(path):
    path = pathlib.Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise ValueError("capacity policy schemaVersion must be 1")
    policy = {
        "schemaVersion": 1,
        "sampleIntervalSeconds": _number(payload.get("sampleIntervalSeconds", 30), "sampleIntervalSeconds", 10, 3600, integer=True),
        "maxObservationGapSeconds": _number(payload.get("maxObservationGapSeconds", 300), "maxObservationGapSeconds", 30, 86400, integer=True),
        "sampleRetentionDays": _number(payload.get("sampleRetentionDays", 90), "sampleRetentionDays", 7, 3650, integer=True),
        "eventRetentionDays": _number(payload.get("eventRetentionDays", 730), "eventRetentionDays", 30, 3650, integer=True),
        "startTimeoutSeconds": _number(payload.get("startTimeoutSeconds", 300), "startTimeoutSeconds", 30, 3600, integer=True),
        "minimumRevisitSamples": _number(payload.get("minimumRevisitSamples", 5), "minimumRevisitSamples", 2, 1000, integer=True),
        "minimumStartSamples": _number(payload.get("minimumStartSamples", 2), "minimumStartSamples", 1, 1000, integer=True),
        "fallbackColdStartSeconds": _number(payload.get("fallbackColdStartSeconds", 90), "fallbackColdStartSeconds", 1, 3600),
        "coldStartWaitWeight": _number(payload.get("coldStartWaitWeight", 4), "coldStartWaitWeight", 0.1, 100),
        "minimumRetentionSeconds": _number(payload.get("minimumRetentionSeconds", 60), "minimumRetentionSeconds", 0, 86400, integer=True),
        "maximumRetentionSeconds": _number(payload.get("maximumRetentionSeconds", 3600), "maximumRetentionSeconds", 60, 86400, integer=True),
        "recommendationStepSeconds": _number(payload.get("recommendationStepSeconds", 60), "recommendationStepSeconds", 10, 3600, integer=True),
        "forecastHorizonSeconds": _number(payload.get("forecastHorizonSeconds", 900), "forecastHorizonSeconds", 60, 86400, integer=True),
        "prewarmSafetySeconds": _number(payload.get("prewarmSafetySeconds", 30), "prewarmSafetySeconds", 0, 900, integer=True),
        "minimumPrewarmLeadSeconds": _number(payload.get("minimumPrewarmLeadSeconds", 60), "minimumPrewarmLeadSeconds", 10, 3600, integer=True),
        "maximumPrewarmLeadSeconds": _number(payload.get("maximumPrewarmLeadSeconds", 600), "maximumPrewarmLeadSeconds", 60, 3600, integer=True),
        "maximumApplyFraction": _number(payload.get("maximumApplyFraction", 0.5), "maximumApplyFraction", 0.05, 1.0),
    }
    if policy["maximumRetentionSeconds"] < policy["minimumRetentionSeconds"]:
        raise ValueError("maximumRetentionSeconds must be at least minimumRetentionSeconds")
    if policy["maxObservationGapSeconds"] < policy["sampleIntervalSeconds"]:
        raise ValueError("maxObservationGapSeconds must be at least sampleIntervalSeconds")
    if policy["maximumPrewarmLeadSeconds"] < policy["minimumPrewarmLeadSeconds"]:
        raise ValueError("maximumPrewarmLeadSeconds must be at least minimumPrewarmLeadSeconds")
    return policy


def _quantile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * float(fraction)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Store:
    def __init__(self, database, policy, owner_uid=None, owner_gid=None):
        self.database = pathlib.Path(database)
        self.policy_path = pathlib.Path(policy)
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None

    @property
    def policy(self):
        return load_policy(self.policy_path)

    def _secure(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.database.parent, 0o700)
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
        # Bound read-only page mappings; retain all SQL, HMAC and write checks.
        # Unsupported SQLite builds silently retain ordinary page reads.
        connection.execute("pragma mmap_size=1073741824")
        connection.execute("pragma busy_timeout=10000")
        connection.execute("pragma foreign_keys=on")
        if not readonly:
            connection.execute("pragma journal_mode=wal")
            connection.execute("pragma synchronous=full")
        return connection

    def initialize(self):
        policy = self.policy
        connection = self.connect()
        try:
            connection.executescript("""
                create table if not exists service_state (
                  service text primary key,
                  last_observed_at real not null,
                  last_runtime_state text not null,
                  last_ready integer not null check(last_ready in (0,1)),
                  last_active integer not null check(last_active in (0,1)),
                  last_inactive_at real,
                  pending_start_id text,
                  updated_at real not null
                );
                create table if not exists samples (
                  id integer primary key autoincrement,
                  observed_at real not null,
                  service text not null,
                  mode text not null,
                  runtime_state text not null,
                  players integer not null check(players >= 0),
                  demanded integer not null check(demanded in (0,1)),
                  ready integer not null check(ready in (0,1)),
                  optional_warm integer not null check(optional_warm in (0,1)),
                  retention_seconds integer not null check(retention_seconds >= 0),
                  active integer not null check(active in (0,1)),
                  interval_seconds real not null check(interval_seconds >= 0)
                );
                create index if not exists samples_service_time on samples(service, observed_at);
                create table if not exists events (
                  id text primary key,
                  service text not null,
                  kind text not null check(kind in ('start','revisit')),
                  started_at real not null,
                  completed_at real,
                  duration_seconds real,
                  outcome text,
                  source text not null,
                  details_json text not null default '{}',
                  created_at real not null
                );
                create index if not exists events_service_kind_time on events(service, kind, started_at);
                create unique index if not exists one_pending_start_per_service on events(service) where kind='start' and completed_at is null;
                create table if not exists applications (
                  id text primary key,
                  applied_at real not null,
                  actor text not null,
                  source text not null,
                  changes_json text not null,
                  sha256 text not null
                );
                create trigger if not exists capacity_applications_no_update before update on applications begin select raise(abort, 'capacity applications are append-only'); end;
                create trigger if not exists capacity_applications_no_delete before delete on applications begin select raise(abort, 'capacity applications are append-only'); end;
                create table if not exists metadata (key text primary key, value text not null);
            """)
            connection.execute("insert into metadata(key,value) values('schema_version','1') on conflict(key) do update set value=excluded.value")
            connection.execute("insert into metadata(key,value) values('policy_sha256',?) on conflict(key) do update set value=excluded.value", (hashlib.sha256(_canonical(policy).encode()).hexdigest(),))
            connection.commit()
        finally:
            connection.close()
        self._secure()
        return self.verify()

    def record_start(self, service, source="autoscaler", at=None, details=None):
        service = str(service or "").strip()
        if not service or len(service) > 128:
            raise ValueError("service is required")
        now = float(at if at is not None else time.time())
        connection = self.connect()
        try:
            connection.execute("begin immediate")
            existing = connection.execute("select id from events where service=? and kind='start' and completed_at is null", (service,)).fetchone()
            if existing:
                connection.commit()
                return existing["id"]
            event_id = f"start-{uuid.uuid4().hex}"
            connection.execute(
                "insert into events(id,service,kind,started_at,source,details_json,created_at) values(?,?,'start',?,?,?,?)",
                (event_id, service, now, str(source)[:64], _canonical(details or {}), now),
            )
            state = connection.execute("select * from service_state where service=?", (service,)).fetchone()
            if state:
                connection.execute("update service_state set pending_start_id=?,updated_at=? where service=?", (event_id, now, service))
            connection.commit()
            return event_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_start(self, service, error, at=None):
        now = float(at if at is not None else time.time())
        connection = self.connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute("select id,started_at,details_json from events where service=? and kind='start' and completed_at is null", (service,)).fetchone()
            if row:
                details = json.loads(row["details_json"] or "{}")
                details["error"] = str(error or "start failed")[:1000]
                connection.execute("update events set completed_at=?,duration_seconds=?,outcome='failed',details_json=? where id=?", (now, max(0, now-row["started_at"]), _canonical(details), row["id"]))
                connection.execute("update service_state set pending_start_id=null,updated_at=? where service=?", (now, service))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def observe(self, maps, observed_at=None):
        policy = self.policy
        now = float(observed_at if observed_at is not None else time.time())
        if not isinstance(maps, list) or not maps:
            raise ValueError("maps must be a non-empty list")
        normalized = []
        seen = set()
        for raw in maps:
            if not isinstance(raw, dict):
                raise ValueError("each map sample must be an object")
            service = str(raw.get("service") or "").strip()
            if not service or len(service) > 128 or service in seen:
                raise ValueError("map services must be unique non-empty identifiers")
            seen.add(service)
            mode = str(raw.get("mode") or "").strip()
            state = str(raw.get("state") or "unknown").strip().lower()
            if mode not in VALID_MODES:
                raise ValueError(f"invalid mode for {service}")
            if state not in VALID_STATES:
                state = "unknown"
            players = int(raw.get("players") or 0)
            if not 0 <= players <= 100000:
                raise ValueError(f"invalid player count for {service}")
            demanded = bool(raw.get("demanded"))
            ready = bool(raw.get("ready"))
            active = players > 0 or demanded
            normalized.append({
                "service": service, "mode": mode, "state": state, "players": players,
                "demanded": demanded, "ready": ready, "optionalWarm": bool(raw.get("optionalWarm")),
                "retentionSeconds": max(0, min(int(raw.get("retentionSeconds") or 0), 86400)),
                "active": active,
            })
        connection = self.connect()
        completed = []
        revisits = []
        try:
            connection.execute("begin immediate")
            for row in normalized:
                service = row["service"]
                previous = connection.execute("select * from service_state where service=?", (service,)).fetchone()
                interval = float(policy["sampleIntervalSeconds"])
                if previous:
                    interval = max(0.0, min(now - float(previous["last_observed_at"]), float(policy["maxObservationGapSeconds"])))
                connection.execute(
                    "insert into samples(observed_at,service,mode,runtime_state,players,demanded,ready,optional_warm,retention_seconds,active,interval_seconds) values(?,?,?,?,?,?,?,?,?,?,?)",
                    (now, service, row["mode"], row["state"], row["players"], int(row["demanded"]), int(row["ready"]), int(row["optionalWarm"]), row["retentionSeconds"], int(row["active"]), interval),
                )
                last_inactive = float(previous["last_inactive_at"]) if previous and previous["last_inactive_at"] is not None else (now if not row["active"] else None)
                pending = previous["pending_start_id"] if previous else None
                if not pending:
                    pending_row = connection.execute(
                        "select id from events where service=? and kind='start' and completed_at is null",
                        (service,),
                    ).fetchone()
                    pending = pending_row["id"] if pending_row else None
                previous_running = bool(previous and previous["last_runtime_state"] == "running")
                running = row["state"] == "running"
                if previous and running and not previous_running and not pending:
                    pending = f"start-{uuid.uuid4().hex}"
                    connection.execute(
                        "insert into events(id,service,kind,started_at,source,details_json,created_at) values(?,?,'start',?,'observed','{}',?)",
                        (pending, service, float(previous["last_observed_at"]), now),
                    )
                if row["active"] and previous and not bool(previous["last_active"]):
                    gap = max(0.0, now - float(previous["last_inactive_at"] or previous["last_observed_at"]))
                    outcome = "warm" if running and row["ready"] and not pending else "cold"
                    event_id = f"revisit-{uuid.uuid4().hex}"
                    connection.execute(
                        "insert into events(id,service,kind,started_at,completed_at,duration_seconds,outcome,source,details_json,created_at) values(?,?,'revisit',?,?,?,?,?,'{}',?)",
                        (event_id, service, float(previous["last_inactive_at"] or previous["last_observed_at"]), now, gap, outcome, "observed", now),
                    )
                    revisits.append({"service": service, "gapSeconds": gap, "outcome": outcome})
                    last_inactive = None
                elif not row["active"] and (not previous or bool(previous["last_active"])):
                    last_inactive = now
                if pending:
                    event = connection.execute("select * from events where id=?", (pending,)).fetchone()
                    if event and event["completed_at"] is None:
                        age = max(0.0, now - float(event["started_at"]))
                        if running and row["ready"]:
                            connection.execute("update events set completed_at=?,duration_seconds=?,outcome='ready' where id=?", (now, age, pending))
                            completed.append({"service": service, "durationSeconds": age, "outcome": "ready"})
                            pending = None
                        elif age >= policy["startTimeoutSeconds"]:
                            connection.execute("update events set completed_at=?,duration_seconds=?,outcome='timeout' where id=?", (now, age, pending))
                            completed.append({"service": service, "durationSeconds": age, "outcome": "timeout"})
                            pending = None
                connection.execute(
                    "insert into service_state(service,last_observed_at,last_runtime_state,last_ready,last_active,last_inactive_at,pending_start_id,updated_at) values(?,?,?,?,?,?,?,?) on conflict(service) do update set last_observed_at=excluded.last_observed_at,last_runtime_state=excluded.last_runtime_state,last_ready=excluded.last_ready,last_active=excluded.last_active,last_inactive_at=excluded.last_inactive_at,pending_start_id=excluded.pending_start_id,updated_at=excluded.updated_at",
                    (service, now, row["state"], int(row["ready"]), int(row["active"]), last_inactive, pending, now),
                )
            sample_cutoff = now - policy["sampleRetentionDays"] * 86400
            event_cutoff = now - policy["eventRetentionDays"] * 86400
            connection.execute("delete from samples where observed_at < ?", (sample_cutoff,))
            connection.execute("delete from events where completed_at is not null and completed_at < ?", (event_cutoff,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"ok": True, "observedAt": _iso(now), "maps": len(normalized), "startsCompleted": completed, "revisits": revisits}

    def _window_rows(self, connection, cutoff):
        return connection.execute("""
            select service,
              sum(interval_seconds) observed,
              sum(case when runtime_state='running' then interval_seconds else 0 end) running,
              sum(case when active=1 then interval_seconds else 0 end) active,
              sum(case when runtime_state='running' and active=0 then interval_seconds else 0 end) idle_running,
              sum(case when runtime_state!='running' and mode in ('dynamic','disabled') then interval_seconds else 0 end) saved,
              sum(players * interval_seconds) player_seconds
            from samples where observed_at >= ? group by service order by service
        """, (cutoff,)).fetchall()

    def _recommendation(self, connection, service, latest, now):
        policy = self.policy
        cutoff = now - policy["eventRetentionDays"] * 86400
        revisits = connection.execute("select duration_seconds,outcome from events where service=? and kind='revisit' and completed_at>=? and duration_seconds is not null order by completed_at", (service, cutoff)).fetchall()
        starts = connection.execute("select duration_seconds,outcome from events where service=? and kind='start' and completed_at>=? and duration_seconds is not null order by completed_at", (service, cutoff)).fetchall()
        gaps = [float(row["duration_seconds"]) for row in revisits]
        ready_starts = [float(row["duration_seconds"]) for row in starts if row["outcome"] == "ready"]
        cold_seconds = _quantile(ready_starts, 0.5) or float(policy["fallbackColdStartSeconds"])
        eligible = len(gaps) >= policy["minimumRevisitSamples"] and len(ready_starts) >= policy["minimumStartSamples"]
        minimum = policy["minimumRetentionSeconds"]
        maximum = policy["maximumRetentionSeconds"]
        step = policy["recommendationStepSeconds"]
        candidates = list(range(minimum, maximum + 1, step))
        if maximum not in candidates:
            candidates.append(maximum)
        scored = []
        penalty = cold_seconds * policy["coldStartWaitWeight"]
        for candidate in candidates:
            if not gaps:
                cost = None
            else:
                cost = sum(min(gap, candidate) + (penalty if gap > candidate else 0) for gap in gaps) / len(gaps)
            scored.append((candidate, cost))
        recommended = min((row for row in scored if row[1] is not None), key=lambda row: (row[1], row[0]), default=(int(latest.get("retentionSeconds") or minimum), None))[0]
        idle_age = max(0.0, now - float(latest.get("lastInactiveAt") or now)) if not latest.get("active") else 0.0
        survivors = [gap for gap in gaps if gap > idle_age]
        horizon = policy["forecastHorizonSeconds"]
        probability = (sum(1 for gap in survivors if gap <= idle_age + horizon) / len(survivors)) if survivors else None
        confidence = "high" if len(gaps) >= 20 and len(ready_starts) >= 5 else "moderate" if eligible else "low"
        measured_lead = (_quantile(ready_starts, 0.95) or float(policy["fallbackColdStartSeconds"])) + policy["prewarmSafetySeconds"]
        prewarm_lead = max(policy["minimumPrewarmLeadSeconds"], min(policy["maximumPrewarmLeadSeconds"], math.ceil(measured_lead)))
        return {
            "eligible": eligible,
            "confidence": confidence,
            "revisitSamples": len(gaps),
            "startSamples": len(ready_starts),
            "currentRetentionSeconds": int(latest.get("retentionSeconds") or 0),
            "recommendedRetentionSeconds": int(recommended),
            "revisitGapP50Seconds": _quantile(gaps, 0.5),
            "revisitGapP75Seconds": _quantile(gaps, 0.75),
            "revisitGapP90Seconds": _quantile(gaps, 0.9),
            "coldStartP50Seconds": _quantile(ready_starts, 0.5),
            "coldStartP95Seconds": _quantile(ready_starts, 0.95),
            "prewarmLeadSeconds": int(prewarm_lead),
            "prewarmLeadSource": "measured-p95" if ready_starts else "policy-fallback",
            "warmHits": sum(1 for row in revisits if row["outcome"] == "warm"),
            "coldRevisits": sum(1 for row in revisits if row["outcome"] == "cold"),
            "nextVisitProbability": probability,
            "forecastHorizonSeconds": horizon,
            "idleAgeSeconds": idle_age,
            "model": "minimize retained-idle seconds plus weighted cold-start wait",
        }

    def status(self, now=None):
        self.initialize_if_needed()
        now = float(now if now is not None else time.time())
        connection = self.connect(readonly=True)
        try:
            latest_rows = connection.execute("""
                select s.*,st.last_inactive_at,st.pending_start_id
                from samples s join (select service,max(id) id from samples group by service) newest on newest.id=s.id
                left join service_state st on st.service=s.service order by s.service
            """).fetchall()
            latest = {}
            for row in latest_rows:
                latest[row["service"]] = {
                    "service": row["service"], "observedAt": _iso(row["observed_at"]), "mode": row["mode"],
                    "state": row["runtime_state"], "players": row["players"], "demanded": bool(row["demanded"]),
                    "ready": bool(row["ready"]), "active": bool(row["active"]), "optionalWarm": bool(row["optional_warm"]),
                    "retentionSeconds": row["retention_seconds"], "lastInactiveAt": row["last_inactive_at"],
                    "pendingStartId": row["pending_start_id"],
                }
            windows = {}
            for seconds in WINDOWS:
                rows = self._window_rows(connection, now - seconds)
                totals = {"observedSeconds": 0.0, "runningSeconds": 0.0, "activeSeconds": 0.0, "idleRunningSeconds": 0.0, "savedSeconds": 0.0, "playerSeconds": 0.0}
                maps = {}
                for row in rows:
                    item = {
                        "observedSeconds": float(row["observed"] or 0), "runningSeconds": float(row["running"] or 0),
                        "activeSeconds": float(row["active"] or 0), "idleRunningSeconds": float(row["idle_running"] or 0),
                        "savedSeconds": float(row["saved"] or 0), "playerSeconds": float(row["player_seconds"] or 0),
                    }
                    item["resourceAvoidanceRatio"] = item["savedSeconds"] / item["observedSeconds"] if item["observedSeconds"] else None
                    item["productiveRunningRatio"] = item["activeSeconds"] / item["runningSeconds"] if item["runningSeconds"] else None
                    maps[row["service"]] = item
                    for key in totals:
                        totals[key] += item[key]
                totals["resourceAvoidanceRatio"] = totals["savedSeconds"] / totals["observedSeconds"] if totals["observedSeconds"] else None
                totals["productiveRunningRatio"] = totals["activeSeconds"] / totals["runningSeconds"] if totals["runningSeconds"] else None
                totals["mapHoursSaved"] = totals["savedSeconds"] / 3600
                totals["idleMapHours"] = totals["idleRunningSeconds"] / 3600
                totals["coverageRatio"] = min(1.0, totals["observedSeconds"] / (seconds * max(1, len(latest)))) if latest else 0.0
                windows[str(seconds)] = {"fleet": totals, "maps": maps}
            recommendations = {service: self._recommendation(connection, service, row, now) for service, row in latest.items() if row["mode"] == "dynamic"}
            starts = [dict(row) for row in connection.execute("select id,service,started_at,completed_at,duration_seconds,outcome,source from events where kind='start' order by started_at desc limit 100").fetchall()]
            for row in starts:
                row["startedAt"] = _iso(row.pop("started_at"))
                row["completedAt"] = _iso(row.pop("completed_at"))
                row["durationSeconds"] = row.pop("duration_seconds")
            applications = [dict(row) for row in connection.execute("select id,applied_at,actor,source,changes_json,sha256 from applications order by applied_at desc limit 100").fetchall()]
            for row in applications:
                row["appliedAt"] = _iso(row.pop("applied_at"))
                row["changes"] = json.loads(row.pop("changes_json"))
            return {
                "ok": True, "generatedAt": _iso(now), "policy": self.policy, "maps": list(latest.values()),
                "windows": windows, "recommendations": recommendations, "recentStarts": starts,
                "applications": applications, "integrity": self.verify(),
            }
        finally:
            connection.close()

    def initialize_if_needed(self):
        if not self.database.exists():
            self.initialize()

    def record_application(self, actor, source, changes, applied_at=None):
        now = float(applied_at if applied_at is not None else time.time())
        payload = {"appliedAt": now, "actor": str(actor)[:128], "source": str(source)[:64], "changes": changes}
        digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        application_id = f"capacity-{uuid.uuid4().hex}"
        connection = self.connect()
        try:
            connection.execute("insert into applications(id,applied_at,actor,source,changes_json,sha256) values(?,?,?,?,?,?)", (application_id, now, payload["actor"], payload["source"], _canonical(changes), digest))
            connection.commit()
        finally:
            connection.close()
        return {"id": application_id, "appliedAt": _iso(now), "sha256": digest, "changes": changes}

    def verify(self):
        if not self.database.exists():
            return {"ok": False, "sqlite": "missing", "applicationsValid": False}
        connection = self.connect(readonly=True)
        try:
            integrity = connection.execute("pragma integrity_check").fetchone()[0]
            triggers = {row["name"] for row in connection.execute("select name from sqlite_master where type='trigger'")}
            required = {"capacity_applications_no_update", "capacity_applications_no_delete"}
            application_valid = True
            count = 0
            for row in connection.execute("select applied_at,actor,source,changes_json,sha256 from applications order by applied_at,id"):
                count += 1
                payload = {"appliedAt": row["applied_at"], "actor": row["actor"], "source": row["source"], "changes": json.loads(row["changes_json"])}
                if hashlib.sha256(_canonical(payload).encode()).hexdigest() != row["sha256"]:
                    application_valid = False
            ok = integrity == "ok" and required.issubset(triggers) and application_valid
            return {"ok": ok, "sqlite": integrity, "appendOnlyTriggers": required.issubset(triggers), "applicationsValid": application_valid, "applicationCount": count}
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "sqlite": "error", "applicationsValid": False, "error": str(exc)}
        finally:
            connection.close()

    def backup(self, target):
        self.initialize_if_needed()
        target = pathlib.Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
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
        check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            integrity = check.execute("pragma integrity_check").fetchone()[0]
        finally:
            check.close()
        if integrity != "ok":
            target.unlink(missing_ok=True)
            raise RuntimeError(f"capacity backup integrity check failed: {integrity}")
        return {"path": str(target), "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "integrity": integrity}

    def prometheus(self, now=None):
        status = self.status(now=now)
        lines = [
            "# HELP dash_capacity_collector_up Capacity intelligence ledger is readable.",
            "# TYPE dash_capacity_collector_up gauge",
            "dash_capacity_collector_up 1",
        ]
        observed = [
            _datetime.datetime.fromisoformat(row["observedAt"]).timestamp()
            for row in status.get("maps", []) if row.get("observedAt")
        ]
        lines.append(f"dash_capacity_last_sample_timestamp_seconds {max(observed) if observed else 'NaN'}")
        for window, payload in status["windows"].items():
            fleet = payload["fleet"]
            for metric, key in (("map_hours_saved", "mapHoursSaved"), ("idle_map_hours", "idleMapHours"), ("resource_avoidance_ratio", "resourceAvoidanceRatio"), ("productive_running_ratio", "productiveRunningRatio"), ("observation_coverage_ratio", "coverageRatio")):
                value = fleet.get(key)
                lines.append(f'dash_capacity_{metric}{{window_seconds="{window}"}} {"NaN" if value is None else value}')
        for service, recommendation in status["recommendations"].items():
            label = service.replace("\\", "_").replace('"', "_")
            lines.append(f'dash_capacity_recommended_retention_seconds{{service="{label}",confidence="{recommendation["confidence"]}"}} {recommendation["recommendedRetentionSeconds"]}')
            lines.append(f'dash_capacity_recommendation_eligible{{service="{label}"}} {1 if recommendation["eligible"] else 0}')
            lines.append(f'dash_capacity_warm_hits_total{{service="{label}"}} {recommendation["warmHits"]}')
            lines.append(f'dash_capacity_cold_revisits_total{{service="{label}"}} {recommendation["coldRevisits"]}')
            lines.append(f'dash_capacity_cold_start_p95_seconds{{service="{label}"}} {"NaN" if recommendation["coldStartP95Seconds"] is None else recommendation["coldStartP95Seconds"]}')
            lines.append(f'dash_capacity_retention_delta_seconds{{service="{label}"}} {recommendation["recommendedRetentionSeconds"] - recommendation["currentRetentionSeconds"]}')
        return "\n".join(lines) + "\n"
