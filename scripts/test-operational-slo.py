#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import random
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("operational_slo", ROOT / "admin" / "operational_slo.py")
slo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(slo)


class OperationalSLOTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.clock = [1_800_000_000.0]
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({
            "version": 1,
            "sampleIntervalSeconds": 60,
            "maxSampleGapSeconds": 300,
            "sampleRetentionDays": 90,
            "objectives": [
                {"id": "farm", "name": "Farm", "signal": "farm_ready", "targetAvailability": 0.99, "severity": "critical", "consecutiveFailures": 2, "excludeMaintenance": True},
                {"id": "backup", "name": "Backup", "signal": "backup_ready", "targetAvailability": 0.95, "severity": "warning", "consecutiveFailures": 1, "excludeMaintenance": False},
            ],
        }), encoding="utf-8")
        self.path = self.root / "state" / "slo.sqlite3"
        self.store = slo.Store(self.path, self.policy, owner_uid=os.getuid(), owner_gid=os.getgid(), now=lambda: self.clock[0])
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def advance(self, seconds=60):
        self.clock[0] += seconds

    def record(self, farm=True, backup=True):
        return self.store.record({"farm_ready": farm, "backup_ready": backup}, context={"marker": "test"}, observed_at=self.clock[0])

    def test_policy_validation_and_private_store(self):
        self.assertEqual(0o700, self.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        invalid = json.loads(self.policy.read_text())
        invalid["objectives"][1]["id"] = "farm"
        with self.assertRaises(ValueError):
            slo.validate_policy(invalid)
        invalid["objectives"][1]["id"] = "bad-id"
        with self.assertRaises(ValueError):
            slo.validate_policy(invalid)

    def test_time_weighted_windows_and_error_budget(self):
        self.record(farm=True)
        self.advance(60)
        self.record(farm=False)
        status = self.store.status(now=self.clock[0])
        farm = next(item for item in status["objectives"] if item["id"] == "farm")
        hour = farm["windows"]["3600"]
        self.assertAlmostEqual(0.5, hour["availability"])
        self.assertAlmostEqual(50.0, hour["burnRate"])
        self.assertEqual(0.0, hour["errorBudgetRemaining"])
        self.assertLess(hour["coverage"], 0.04)

    def test_incident_debounce_open_ack_note_resolve_and_hash_chain(self):
        first = self.record(farm=False, backup=True)
        self.assertFalse(first["incidentsOpened"])
        self.advance()
        second = self.record(farm=False, backup=True)
        self.assertEqual(1, len(second["incidentsOpened"]))
        incident_id = second["incidentsOpened"][0]
        ack = self.store.acknowledge(incident_id, "operator", "investigating")
        note = self.store.add_note(incident_id, "operator", "found stale map")
        self.assertNotEqual(ack["eventHash"], note["eventHash"])
        self.advance()
        resolved = self.record(farm=True, backup=True)
        self.assertEqual([incident_id], resolved["incidentsResolved"])
        integrity = self.store.integrity_check()
        self.assertTrue(integrity["ok"])
        self.assertEqual(4, integrity["eventCount"])
        status = self.store.status()
        self.assertFalse(status["openIncidents"])
        self.assertEqual("healthy", status["overall"])

    def test_incident_event_ledger_is_database_immutable(self):
        self.record(farm=False)
        self.advance()
        self.record(farm=False)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("update incident_events set note='rewritten'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("delete from incident_events")
        finally:
            connection.close()
        self.assertTrue(self.store.integrity_check()["eventChainValid"])

    def test_planned_maintenance_excludes_only_configured_objectives(self):
        window = self.store.create_maintenance(self.clock[0], self.clock[0] + 600, "planned restart", "operator")
        result = self.record(farm=False, backup=False)
        rows = {item["id"]: item for item in result["objectives"]}
        self.assertTrue(rows["farm"]["excluded"])
        self.assertFalse(rows["backup"]["excluded"])
        self.assertEqual(1, len(result["incidentsOpened"]))
        status = self.store.status()
        self.assertEqual(window["id"], status["activeMaintenance"]["id"])
        cancelled = self.store.cancel_maintenance(window["id"], "operator")
        self.assertTrue(cancelled["ok"])
        with self.assertRaises(ValueError):
            self.store.cancel_maintenance(window["id"], "operator")

    def test_maintenance_rejects_overlap_retroactive_and_unbounded(self):
        self.store.create_maintenance(self.clock[0] + 60, self.clock[0] + 600, "one", "operator")
        with self.assertRaises(ValueError):
            self.store.create_maintenance(self.clock[0] + 300, self.clock[0] + 900, "overlap", "operator")
        with self.assertRaises(ValueError):
            self.store.create_maintenance(self.clock[0] - 301, self.clock[0] + 60, "old", "operator")
        with self.assertRaises(ValueError):
            self.store.create_maintenance(self.clock[0], self.clock[0] + 86401, "long", "operator")

    def test_missing_signal_fails_closed_and_backup_objective_opens_immediately(self):
        result = self.store.record({"farm_ready": True}, observed_at=self.clock[0])
        backup = next(item for item in result["objectives"] if item["id"] == "backup")
        self.assertFalse(backup["good"])
        self.assertEqual("signal missing", backup["reason"])
        self.assertEqual(1, len(result["incidentsOpened"]))

    def test_sample_retention_does_not_delete_incident_events(self):
        self.record(farm=False)
        self.advance()
        self.record(farm=False)
        before = self.store.integrity_check()["eventCount"]
        self.advance(91 * 86400)
        self.record(farm=True)
        with self.store.connect() as connection:
            old = connection.execute("select count(*) from samples where observed_at<?", (self.clock[0] - 90 * 86400,)).fetchone()[0]
        self.assertEqual(0, old)
        self.assertGreaterEqual(self.store.integrity_check()["eventCount"], before)

    def test_prometheus_is_bounded_and_contains_error_budget_metrics(self):
        self.record()
        metrics = self.store.prometheus(now=self.clock[0])
        self.assertIn("dash_slo_collector_up 1", metrics)
        self.assertIn('dash_slo_objective_good{objective="farm",severity="critical"} 1', metrics)
        self.assertIn("dash_slo_error_budget_burn_rate", metrics)
        self.assertNotIn(str(self.path), metrics)

    def test_sql_windows_match_reference_clipping_exclusion_and_empty_history(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("create table samples(objective_id text, observed_at real, interval_seconds real, good integer, excluded integer)")
        generator = random.Random(20261106)
        values = [("farm", generator.uniform(1000, 12000), generator.uniform(0, 600), generator.randrange(2), generator.randrange(2)) for _ in range(2000)]
        values += [("farm", 11000, 300, 1, 0), ("farm", 10000, 0, 0, 0), ("other", 9999, 300, 0, 0)]
        connection.executemany("insert into samples values(?,?,?,?,?)", values)
        oldest, now = 2000.0, 10000.0
        samples = connection.execute("select * from samples where objective_id='farm' and observed_at>=? order by observed_at", (oldest,)).fetchall()
        for cutoff in (0, 5000, 9700, 10000):
            expected = self.store._window(samples, cutoff, now, 0.99)
            actual = self.store._aggregate_window(connection, "farm", oldest, cutoff, now, 0.99)
            self.assertEqual(set(expected), set(actual))
            for key, value in expected.items():
                if value is None:
                    self.assertIsNone(actual[key], key)
                else:
                    self.assertAlmostEqual(value, actual[key], places=8, msg=key)
        expected = self.store._window([], 0, now, 0.99)
        self.assertEqual(expected, self.store._aggregate_window(connection, "missing", oldest, 0, now, 0.99))
        connection.close()

    def test_consistent_backup_and_corruption_detection(self):
        self.record(backup=False)
        destination = self.root / "backup" / "operational-slo.sqlite3"
        result = self.store.backup(destination)
        self.assertTrue(result["ok"])
        self.assertEqual(0o600, destination.stat().st_mode & 0o777)
        connection = sqlite3.connect(destination)
        try:
            self.assertEqual("ok", connection.execute("pragma integrity_check").fetchone()[0])
        finally:
            connection.close()
        with self.store.connect(write=True) as connection:
            connection.execute("drop trigger incident_events_no_update")
            connection.execute("update incident_events set event_hash='0' where sequence=1")
        integrity = self.store.integrity_check()
        self.assertGreater(integrity["eventCount"], 0)
        self.assertFalse(integrity["eventChainValid"])


if __name__ == "__main__":
    unittest.main()
