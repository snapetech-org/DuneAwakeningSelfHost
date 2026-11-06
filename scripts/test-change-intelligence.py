#!/usr/bin/env python3
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import change_intelligence
import command_console


class ChangeIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.policy = self.root / "change-intelligence.json"
        self.policy.write_text((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.secret = self.root / "change-intelligence-hmac.secret"
        self.secret.write_text("d" * 64 + "\n", encoding="utf-8")
        self.secret.chmod(0o600)
        self.database = self.root / "state" / "change-intelligence.sqlite3"
        self.store = change_intelligence.Store(self.database, self.policy, self.secret)
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def record(self, action, epoch, **fields):
        return self.store.record({"action": action, "ts": epoch, "ok": True, **fields}, ingested_at=epoch + 0.5)

    def test_integrity_cache_extends_valid_chain_and_invalidates_on_artifact_change(self):
        self.record("settings-write", 1000, path="/api/settings/env")
        with mock.patch.object(self.store, "_verify_connection", wraps=self.store._verify_connection) as verify:
            self.assertTrue(self.store.verify()["ok"])
            self.assertEqual(0, verify.call_count)
            self.record("service-control", 1001, service="director")
            self.assertEqual(2, self.store.verify()["eventCount"])
            self.assertEqual(0, verify.call_count)
            (self.database.parent / "unrelated-admin-state.tmp").write_text("changed", encoding="utf-8")
            self.assertEqual(2, self.store.verify()["eventCount"])
            self.assertEqual(0, verify.call_count)
            stat_result = self.database.stat()
            os.utime(self.database, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1))
            self.assertTrue(self.store.verify()["ok"])
            self.assertEqual(1, verify.call_count)

    def test_private_modes_policy_and_secret_validation(self):
        self.assertEqual(0o700, self.database.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.database.stat().st_mode & 0o777)
        self.assertEqual(3600, self.store.policy["correlationWindowBeforeSeconds"])
        self.assertEqual(13, len(self.store.policy["response"]["runbooks"]))
        self.assertEqual(64, len(self.store.policy["response"]["policySha256"]))
        self.secret.chmod(0o644)
        with self.assertRaises(PermissionError):
            change_intelligence.read_secret(self.secret)

    def test_response_policy_rejects_missing_fallback_and_unsafe_mutation_contract(self):
        document = json.loads((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"))
        document["response"]["runbooks"].pop()
        self.policy.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "generic fallback"):
            change_intelligence.load_policy(self.policy)
        document = json.loads((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"))
        document["response"]["runbooks"][0]["steps"][0]["mutation"] = True
        self.policy.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mutation/command contract"):
            change_intelligence.load_policy(self.policy)

    def test_redaction_hashes_identity_paths_and_credentials(self):
        userinfo = "operator" + ":" + "password"
        self.record(
            "service-control", 1000, method="POST", target="Alice", subject="FLS-123",
            peer="203.0.113.42", password="plain-password", api_token="plain-token",
            actor="Operator Alice",
            filesystem_path="/srv/private/world", path="/api/ops/services",
            message=f"https://{userinfo}@example.test/x Bearer abcdefghijklmnop",
        )
        encoded = json.dumps(self.store.status()["recentEvents"])
        for forbidden in ("Alice", "Operator Alice", "FLS-123", "203.0.113.42", "plain-password", "plain-token", "/srv/private/world", userinfo, "abcdefghijklmnop"):
            self.assertNotIn(forbidden, encoded)
        self.assertIn("/api/ops/services", encoded)
        self.assertIn("hmac:", encoded)
        self.assertIn("path-hmac:", encoded)
        self.assertIn("<redacted>", encoded)

    def test_classification_temporal_ranking_and_noncausal_capsule(self):
        settings = self.record("settings-write", 1000, method="POST", path="/api/settings/env", actor="operator")
        service = self.record("service-control", 1100, method="POST", service="director", actor="operator")
        opened = self.record("slo-incident-opened", 1200, incident_id="slo-1")
        self.assertEqual("incident-open", opened["kind"])
        candidates = opened["candidates"]
        self.assertEqual([service["id"], settings["id"]], [row["id"] for row in candidates[:2]])
        self.assertGreater(candidates[0]["score"], candidates[1]["score"])
        capsule = self.store.capsule("slo:slo-1")
        self.assertFalse(capsule["causalityClaimed"])
        self.assertIn("not proof of causality", capsule["interpretation"])
        self.assertEqual("open", capsule["status"])

    def test_incident_resolution_and_followup_evidence(self):
        self.record("settings-write", 1000, method="POST")
        self.record("desired-state-drift-opened", 1100, finding_id="drift-1", subject=".env")
        self.record("backup-finished", 1150, path="backups/test")
        self.record("desired-state-drift-resolved", 1200, finding_id="drift-1", subject=".env")
        status = self.store.status()
        self.assertFalse(status["openIncidents"])
        self.assertEqual("resolved", status["incidents"][0]["status"])
        capsule = self.store.capsule("desired:drift-1")
        self.assertEqual("resolved", capsule["status"])
        self.assertTrue(any(row["action"] == "backup-finished" for row in capsule["followupEvidence"]))
        self.record("desired-state-drift-opened", 1300, finding_id="drift-1", subject=".env")
        reopened = self.store.capsule("desired:drift-1")
        self.assertEqual("open", reopened["status"])
        self.assertEqual("1970-01-01T00:21:40+00:00", reopened["opened"]["occurredAt"])
        with self.assertRaises(ValueError):
            self.store.capsule("../../invalid")

    def test_signed_capsule_binds_bounded_evidence_to_verified_ledger_head(self):
        self.record("settings-write", 1000, method="POST")
        self.record("slo-incident-opened", 1100, incident_id="portable")
        self.record("slo-incident-resolved", 1200, incident_id="portable")
        document = self.store.signed_capsule("slo:portable", at=1300)
        verified = change_intelligence.verify_signed_capsule(document, change_intelligence.read_secret(self.secret))
        self.assertTrue(verified["ok"])
        self.assertEqual("slo:portable", verified["incidentKey"])
        self.assertEqual(self.store.verify()["lastEventSignature"], document["ledger"]["lastEventSignature"])
        self.assertFalse(document["capsule"]["causalityClaimed"])
        self.assertEqual("generic-slo", document["capsule"]["responsePlan"]["runbookId"])
        self.assertTrue(change_intelligence.verify_response_plan(document["capsule"]["responsePlan"])["ok"])
        self.assertTrue(verified["responsePlanValid"])

        tampered = json.loads(json.dumps(document))
        tampered["capsule"]["status"] = "open"
        self.assertFalse(change_intelligence.verify_signed_capsule(tampered, change_intelligence.read_secret(self.secret))["ok"])
        with_extra = {**document, "unsignedNote": "not covered"}
        self.assertFalse(change_intelligence.verify_signed_capsule(with_extra, change_intelligence.read_secret(self.secret))["ok"])
        self.assertFalse(change_intelligence.verify_signed_capsule(document, b"e" * 64)["ok"])

        legacy = json.loads(json.dumps(document))
        legacy["schemaVersion"] = 1
        legacy["capsule"].pop("responsePlan")
        legacy.pop("signature")
        legacy["signature"] = change_intelligence.hmac.new(
            change_intelligence.read_secret(self.secret), change_intelligence._canonical(legacy).encode(), change_intelligence.hashlib.sha256,
        ).hexdigest()
        legacy_verified = change_intelligence.verify_signed_capsule(legacy, change_intelligence.read_secret(self.secret))
        self.assertTrue(legacy_verified["ok"])
        self.assertTrue(legacy_verified["legacyWithoutResponsePlan"])
        self.assertIsNone(legacy_verified["responsePlanValid"])

    def test_response_policy_maps_every_objective_and_only_reuses_bounded_diagnostics(self):
        expected = {
            "database_availability": "database-availability",
            "control_plane_availability": "control-plane-availability",
            "required_map_availability": "required-map-availability",
            "backup_rpo": "backup-rpo",
            "restore_proof": "restore-proof",
            "memory_headroom": "memory-headroom",
            "admin_authentication": "admin-authentication",
            "desired_state_attestation": "desired-state-attestation",
            "change_intelligence_integrity": "change-intelligence-integrity",
        }
        ledger = {"sqlite": "ok", "appendOnlyTriggers": True, "eventChainValid": True, "eventCount": 42, "lastEventSignature": "a" * 64}
        for objective, runbook in expected.items():
            capsule = {
                "incidentKey": f"slo:{objective}", "status": "open", "candidateChanges": [], "followupEvidence": [],
                "opened": {"id": f"open-{objective}", "action": "slo-incident-opened", "data": {"objective_id": objective}},
                "resolved": None, "causalityClaimed": False,
            }
            plan = change_intelligence.compile_response_plan(capsule, self.store.policy, ledger)
            self.assertEqual(runbook, plan["runbookId"])
            self.assertFalse(plan["executesAutomatically"])
            self.assertFalse(plan["causalityClaimed"])
            self.assertEqual("verified", plan["steps"][0]["status"])
            self.assertEqual("pending", plan["steps"][1]["status"])
            self.assertTrue(change_intelligence.verify_response_plan(plan)["ok"])
            for step in plan["steps"]:
                if step.get("commandId"):
                    self.assertIn(step["commandId"], command_console.COMMANDS)
                if step["mutation"]:
                    self.assertEqual("manual-gated", step["execution"])
                    self.assertNotEqual("read", step["requiredCapability"])
                    self.assertTrue(step.get("featureGate"))

    def test_response_plan_predicates_fallback_and_digest_tamper_detection(self):
        self.record("settings-write", 1000, method="POST")
        self.record("desired-state-drift-opened", 1100, finding_id="response")
        self.record("backup-finished", 1150, path="backups/one")
        self.record("desired-state-drift-resolved", 1200, finding_id="response")
        plan = self.store.capsule("desired:response")["responsePlan"]
        self.assertEqual("desired-state-drift", plan["runbookId"])
        statuses = {step["id"]: step["status"] for step in plan["steps"]}
        self.assertEqual("verified", statuses["verify-evidence-chain"])
        self.assertEqual("verified", statuses["confirm-incident-state"])
        self.assertEqual("pending", statuses["review-ranked-candidates"])
        self.assertEqual("pending", statuses["review-followup-evidence"])
        tampered = json.loads(json.dumps(plan))
        tampered["steps"][-1]["description"] = "unsafe replacement"
        self.assertFalse(change_intelligence.verify_response_plan(tampered)["ok"])
        unsafe = json.loads(json.dumps(plan))
        unsafe["executesAutomatically"] = True
        unsafe.pop("planSha256")
        unsafe["planSha256"] = change_intelligence.hashlib.sha256(change_intelligence._canonical(unsafe).encode()).hexdigest()
        self.assertFalse(change_intelligence.verify_response_plan(unsafe)["ok"])

        generic = change_intelligence.compile_response_plan({
            "incidentKey": "event:custom", "status": "open", "candidateChanges": [], "followupEvidence": [],
            "opened": {"id": "open-custom", "action": "custom-incident", "data": {}}, "resolved": None, "causalityClaimed": False,
        }, self.store.policy, {"sqlite": "error", "appendOnlyTriggers": False, "eventChainValid": False, "eventCount": 0})
        self.assertEqual("generic-incident", generic["runbookId"])
        self.assertEqual("blocked", generic["state"])
        self.assertEqual("blocked", generic["steps"][0]["status"])

    def test_response_drill_is_incident_linked_evidence_and_changes_signed_plan_inputs(self):
        self.record("settings-write", 1000, method="POST")
        self.record("slo-incident-opened", 1100, incident_id="drill", objective_id="database_availability")
        before = self.store.capsule("slo:drill")
        receipt = {
            "id": "response-drill-one", "incidentKey": "slo:drill", "runbookId": "database-availability",
            "planSha256": before["responsePlan"]["planSha256"], "policySha256": before["responsePlan"]["policySha256"],
            "principalId": "owner", "diagnostics": [], "recoveryContracts": [],
            "diagnosticsReady": True, "recoveryContractsReady": True, "ready": True,
            "recoveryExecuted": False, "gameMutationExecuted": False,
        }
        receipt_sha256 = change_intelligence.hashlib.sha256(change_intelligence._canonical(receipt).encode()).hexdigest()
        event = self.store.record({
            "action": "incident-response-drill", "ts": 1200, "ok": True,
            "response_incident_key": "slo:drill", "runbook_id": "database-availability",
            "drill_id": "response-drill-one", "plan_sha256": before["responsePlan"]["planSha256"],
            "policy_sha256": before["responsePlan"]["policySha256"], "principal_id": "owner",
            "diagnostics": [], "recovery_contracts": [], "diagnostics_ready": True,
            "recovery_contracts_ready": True, "recovery_executed": False,
            "game_mutation_executed": False, "receipt_sha256": receipt_sha256,
        }, ingested_at=1201)
        after = self.store.capsule("slo:drill")
        self.assertEqual("evidence", event["kind"])
        self.assertEqual("slo:drill", event["incidentKey"])
        self.assertEqual("incident-response-drill", after["responseDrills"][0]["action"])
        self.assertEqual(event["id"], after["responseDrills"][0]["id"])
        self.assertTrue(after["responseDrills"][0]["receiptVerification"]["ok"])
        self.assertNotEqual(before["responsePlan"]["inputSha256"], after["responsePlan"]["inputSha256"])
        self.assertTrue(change_intelligence.verify_response_plan(after["responsePlan"])["ok"])
        signed = self.store.signed_capsule("slo:drill", at=1300)
        secret = change_intelligence.read_secret(self.secret)
        self.assertTrue(change_intelligence.verify_signed_capsule(signed, secret)["ok"])
        tampered = json.loads(json.dumps(signed))
        tampered["capsule"]["responseDrills"][0]["data"]["diagnostics_ready"] = False
        tampered.pop("signature")
        tampered["signature"] = change_intelligence.hmac.new(secret, change_intelligence._canonical(tampered).encode(), change_intelligence.hashlib.sha256).hexdigest()
        self.assertFalse(change_intelligence.verify_signed_capsule(tampered, secret)["ok"])
        metrics = self.store.prometheus()
        self.assertIn("dash_incident_response_latest_drill_ready 1", metrics)
        self.assertIn("dash_incident_response_last_drill_timestamp_seconds 1200.0", metrics)

    def test_policy_wide_readiness_certification_is_global_verified_evidence(self):
        self.record("slo-incident-opened", 1100, incident_id="certified", objective_id="database_availability")
        policy_sha = self.store.policy["response"]["policySha256"]
        receipt = {
            "id": "readiness-certification-one", "policySha256": policy_sha, "principalId": "owner",
            "diagnostics": [{"commandId": "stack-status", "ok": True}],
            "runbooks": [{"id": "database-availability", "ready": True}],
            "summary": {"runbooksReady": 12, "runbooksTotal": 12, "diagnosticsReady": 3, "diagnosticsTotal": 3, "recoveryContractsReady": 10, "recoveryContractsTotal": 10, "coverageRatio": 1.0},
            "ready": True, "recoveryExecuted": False, "gameMutationExecuted": False,
        }
        receipt_sha = change_intelligence.hashlib.sha256(change_intelligence._canonical(receipt).encode()).hexdigest()
        event = self.store.record({
            "action": "incident-readiness-certification", "ts": 1200, "ok": True,
            "certification_id": receipt["id"], "policy_sha256": policy_sha, "principal_id": "owner",
            "diagnostics": receipt["diagnostics"], "runbooks": receipt["runbooks"], "summary": receipt["summary"],
            "recovery_executed": False, "game_mutation_executed": False, "receipt_sha256": receipt_sha,
        }, ingested_at=1201)
        status = self.store.status()
        self.assertEqual(event["id"], status["readinessCertification"]["id"])
        self.assertTrue(status["readinessCertification"]["receiptVerification"]["ok"])
        self.assertTrue(status["readinessCertification"]["policyCurrent"])
        self.assertTrue(status["readinessCertification"]["currentReady"])
        with mock.patch.object(self.store, "_verify_connection", side_effect=AssertionError("receipt metrics must not walk retained history")):
            self.assertEqual(status["readinessCertification"], self.store.certification_status())
        capsule = self.store.capsule("slo:certified")
        self.assertEqual(event["id"], capsule["readinessCertification"]["id"])
        signed = self.store.signed_capsule("slo:certified", at=1300)
        verified = change_intelligence.verify_signed_capsule(signed, change_intelligence.read_secret(self.secret))
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["readinessReceiptsValid"])
        metrics = self.store.prometheus()
        self.assertIn("dash_incident_readiness_certification_ready 1", metrics)
        self.assertIn("dash_incident_readiness_runbooks_ready 12", metrics)
        self.assertIn("dash_incident_readiness_runbooks_total 12", metrics)
        self.assertIn("dash_incident_readiness_coverage_ratio 1", metrics)

        self.store.policy["response"]["policySha256"] = "f" * 64
        stale = self.store.status()["readinessCertification"]
        self.assertFalse(stale["policyCurrent"])
        self.assertFalse(stale["currentReady"])
        self.assertIn("dash_incident_readiness_certification_ready 0", self.store.prometheus())
        self.assertFalse(self.store.certification_status()["currentReady"])
        with sqlite3.connect(self.database) as connection:
            connection.execute("drop trigger change_events_no_update")
            connection.execute("update events set signature=? where id=?", ("0" * 64, event["id"]))
        with self.assertRaisesRegex(ValueError, "event HMAC"):
            self.store.certification_status()

    def test_signed_capsule_uses_one_snapshot_when_writer_appends_mid_export(self):
        self.record("settings-write", 1000, method="POST")
        self.record("slo-incident-opened", 1100, incident_id="race")
        original_verify = self.store._verify_connection

        def verify_then_append(connection):
            result = original_verify(connection)
            self.record("slo-incident-resolved", 1200, incident_id="race")
            return result

        self.store._verify_connection = verify_then_append
        document = self.store.signed_capsule("slo:race", at=1300)
        self.assertEqual(2, document["ledger"]["eventCount"])
        self.assertEqual("open", document["capsule"]["status"])
        self.assertEqual("resolved", self.store.capsule("slo:race")["status"])
        self.assertTrue(change_intelligence.verify_signed_capsule(document, change_intelligence.read_secret(self.secret))["ok"])

    def test_cli_exports_private_capsule_and_verifies_without_source_database(self):
        self.record("settings-write", 1000, method="POST")
        self.record("desired-state-drift-opened", 1100, finding_id="portable")
        output = self.root / "exports" / "capsule.json"
        command = [
            sys.executable, str(ROOT / "scripts" / "change-intelligence.py"), "export-capsule",
            "--incident-key", "desired:portable", "--output", str(output),
            "--database", str(self.database), "--policy", str(self.policy), "--secret-file", str(self.secret),
        ]
        exported = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(0, exported.returncode, exported.stderr)
        self.assertEqual(0o600, output.stat().st_mode & 0o777)
        verified = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "change-intelligence.py"), "verify-capsule",
            "--capsule-file", str(output), "--secret-file", str(self.secret),
            "--database", str(self.root / "does-not-exist.sqlite3"),
        ], cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(0, verified.returncode, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["signatureValid"])
        planned = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "change-intelligence.py"), "plan",
            "--incident-key", "desired:portable", "--database", str(self.database),
            "--policy", str(self.policy), "--secret-file", str(self.secret),
        ], cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(0, planned.returncode, planned.stderr)
        self.assertEqual("desired-state-drift", json.loads(planned.stdout)["runbookId"])

    def test_post_fallback_is_change_and_read_is_observation(self):
        write = self.record("new-admin-surface", 1000, method="POST")
        read = self.record("new-read-surface", 1001, method="GET")
        self.assertEqual("change", write["kind"])
        self.assertEqual("observation", read["kind"])

    def test_source_fingerprint_makes_history_import_idempotent(self):
        event = {"action": "settings-write", "ts": 1000, "ok": True, "method": "POST", "eventId": "audit-1"}
        first = self.store.record(event, source="audit-history", ingested_at=1001)
        second = self.store.record(event, source="audit-history", ingested_at=1002)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(1, self.store.status()["eventCount"])

    def test_batch_import_is_atomic_bounded_and_can_skip_invalid_legacy_rows(self):
        events = [
            {"action": "settings-write", "ts": 1000 + index, "ok": True, "method": "POST", "eventId": f"audit-{index}"}
            for index in range(100)
        ]
        events.append({"action": "invalid action", "ts": 1200})
        first = self.store.record_many(events, skip_invalid=True, ingested_at=1300)
        second = self.store.record_many(events[:-1], skip_invalid=True, ingested_at=1400)
        self.assertEqual(100, first["insertedCount"])
        self.assertEqual(1, first["errors"])
        self.assertEqual(100, second["duplicates"])
        self.assertEqual(100, self.store.status()["eventCount"])
        self.assertTrue(self.store.verify()["eventChainValid"])

    def test_append_only_hmac_chain_detects_tampering_and_missing_trigger(self):
        self.record("settings-write", 1000, method="POST")
        self.record("service-control", 1100, method="POST")
        self.assertTrue(self.store.verify()["ok"])
        connection = sqlite3.connect(self.database)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("update events set action='forged' where sequence=1")
        connection.rollback()
        connection.execute("drop trigger change_events_no_update")
        connection.execute("update events set action='forged' where sequence=1")
        connection.commit()
        connection.close()
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertFalse(result["appendOnlyTriggers"])
        self.assertFalse(result["eventChainValid"])

    def test_payload_bounds_and_invalid_actions_fail_closed(self):
        with self.assertRaises(ValueError):
            self.store.record({"action": "bad action", "ts": 1000})
        bounded = self.store.record({"action": "settings-write", "ts": 1000, "value": "x" * 100000})
        stored = next(row for row in self.store.status()["recentEvents"] if row["id"] == bounded["id"])
        self.assertLessEqual(len(stored["data"]["value"]), 514)
        with self.assertRaises(ValueError):
            self.store.record({"action": "settings-write", "ts": 1001, **{f"field_{index}": "x" * 1000 for index in range(64)}})
        with self.assertRaises(ValueError):
            self.store.record({"action": "settings-write", "ts": 1000}, source="bad source")

    def test_backup_is_private_and_fully_verified(self):
        self.record("settings-write", 1000, method="POST")
        target = self.root / "archive" / "change-intelligence.sqlite3"
        result = self.store.backup(target)
        self.assertTrue(result["integrity"]["ok"])
        self.assertEqual(64, len(result["sha256"]))
        self.assertEqual(0o600, target.stat().st_mode & 0o777)

    def test_metrics_are_label_free_and_bounded(self):
        self.record("settings-write", 1000, method="POST")
        self.record("slo-incident-opened", 1100, incident_id="private-incident")
        verified = self.store.status()
        with mock.patch.object(self.store, "status", side_effect=AssertionError("verified status must be reused")):
            metrics = self.store.prometheus(verified)
        self.assertIn("dash_change_intelligence_collector_up 1", metrics)
        self.assertIn("dash_change_intelligence_events_total 2", metrics)
        self.assertIn("dash_change_intelligence_open_incidents 1", metrics)
        self.assertNotIn("private-incident", metrics)
        self.assertNotIn("{", metrics)


if __name__ == "__main__":
    unittest.main()
