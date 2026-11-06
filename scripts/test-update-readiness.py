#!/usr/bin/env python3
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))
import update_readiness


class UpdateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.store = update_readiness.Store(self.root / "evidence", b"u" * 64, ttl_seconds=600)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, ready=True, online=0, tag="dune_sb_1_4_10_0"):
        return {
            "candidate": {
                "imageTag": tag, "currentImageTag": "dune_sb_1_4_9_0",
                "status": "update-available", "installedBuildId": "24146567",
                "targetBuildId": "24146567", "loadedBuildId": "24000000",
            },
            "checks": {key: ready for key in update_readiness.REQUIRED_CHECKS},
            "onlinePlayers": online,
            "details": {"backup": "20260716T210000Z", "readiness": "13/13"},
        }

    def test_certification_is_signed_candidate_bound_and_current(self):
        result = self.store.certify(self.snapshot(), "owner", source_commit="a" * 40, now=1000)
        receipt = result["document"]["receipt"]
        self.assertTrue(receipt["scheduledReady"])
        self.assertTrue(receipt["immediateReady"])
        self.assertFalse(receipt["updateExecuted"])
        self.assertFalse(receipt["gameMutationExecuted"])
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(0o600, pathlib.Path(result["evidencePath"]).stat().st_mode & 0o777)
        status = self.store.status(self.snapshot(), now=1100)
        self.assertTrue(status["currentReceiptReady"])
        self.assertTrue(status["applyReady"])
        self.assertIn("dash_update_readiness_receipt_current 1", self.store.prometheus(self.snapshot(), now=1100))

    def test_online_players_allow_scheduled_but_not_immediate_readiness(self):
        result = self.store.certify(self.snapshot(online=7), "operator", now=1000)
        self.assertTrue(result["document"]["receipt"]["scheduledReady"])
        self.assertFalse(result["document"]["receipt"]["immediateReady"])

    def test_failed_check_refuses_certification(self):
        snapshot = self.snapshot()
        snapshot["checks"]["restoreProofReady"] = False
        with self.assertRaisesRegex(ValueError, "restoreProofReady"):
            self.store.certify(snapshot, "owner", now=1000)
        self.assertFalse(update_readiness.normalize_snapshot(snapshot)["scheduledReady"])

    def test_rabbitmq_recovery_proof_is_a_required_check(self):
        snapshot = self.snapshot()
        snapshot["checks"]["rabbitmqRestoreProofReady"] = False
        with self.assertRaisesRegex(ValueError, "rabbitmqRestoreProofReady"):
            self.store.certify(snapshot, "owner", now=1000)
        self.assertFalse(update_readiness.normalize_snapshot(snapshot)["scheduledReady"])

    def test_v2_adds_rabbitmq_gate_without_invalidating_signed_v1_evidence(self):
        current = self.store.certify(self.snapshot(), "owner", source_commit="a" * 40, now=1000)["document"]
        self.assertEqual("dune-update-readiness/v2", current["schemaVersion"])
        self.assertEqual(2, current["receipt"]["schemaVersion"])
        legacy_receipt = json.loads(json.dumps(current["receipt"]))
        legacy_receipt["schemaVersion"] = 1
        legacy_receipt["checks"].pop("rabbitmqRestoreProofReady")
        legacy = update_readiness.signed_document(legacy_receipt, b"u" * 64, generated_at=1000)
        self.assertEqual("dune-update-readiness/v1", legacy["schemaVersion"])
        self.assertTrue(update_readiness.verify_signed_document(legacy, b"u" * 64, now=1001)["ok"])
        mismatched = json.loads(json.dumps(legacy))
        mismatched["schemaVersion"] = "dune-update-readiness/v2"
        self.assertFalse(update_readiness.verify_signed_document(mismatched, b"u" * 64, now=1001)["ok"])

    def test_candidate_change_and_expiry_invalidate_receipt(self):
        self.store.certify(self.snapshot(), "owner", now=1000)
        self.assertFalse(self.store.status(self.snapshot(tag="dune_sb_1_4_11_0"), now=1100)["currentReceiptReady"])
        self.assertFalse(self.store.status(self.snapshot(), now=1701)["currentReceiptReady"])

    def test_runtime_replacement_or_approval_change_invalidates_vendor_receipt(self):
        baseline = self.snapshot()
        self.store.certify(baseline, "owner", now=1000)
        runtime = self.snapshot()
        runtime["candidate"]["runtimeSecurity"] = {
            "imageId": "sha256:" + "a" * 64,
            "vendorImageId": "sha256:" + "b" * 64,
            "approvalSha256": "c" * 64,
        }
        self.assertFalse(self.store.status(runtime, now=1100)["currentReceiptReady"])
        self.store.certify(runtime, "owner", now=1100)
        self.assertTrue(self.store.status(runtime, now=1101)["currentReceiptReady"])
        for key in runtime["candidate"]["runtimeSecurity"]:
            changed = json.loads(json.dumps(runtime))
            changed["candidate"]["runtimeSecurity"][key] = ("sha256:" if key.endswith("ImageId") or key == "imageId" else "") + "d" * 64
            self.assertFalse(self.store.status(changed, now=1101)["currentReceiptReady"])
        self.assertFalse(self.store.status(baseline, now=1101)["currentReceiptReady"])
        runtime["candidate"]["runtimeSecurity"]["imageId"] = "mutable:tag"
        with self.assertRaisesRegex(ValueError, "runtime security digest"):
            update_readiness.normalize_snapshot(runtime)

    def test_nested_and_outer_tampering_fail(self):
        document = self.store.certify(self.snapshot(), "owner", now=1000)["document"]
        nested = json.loads(json.dumps(document))
        nested["receipt"]["checks"]["backupVerified"] = False
        self.assertFalse(update_readiness.verify_signed_document(nested, b"u" * 64, now=1001)["ok"])
        outer = json.loads(json.dumps(document))
        outer["generatedAt"] = "2030-01-01T00:00:00Z"
        self.assertFalse(update_readiness.verify_signed_document(outer, b"u" * 64, now=1001)["ok"])

    def test_invalid_candidate_and_bounded_details_fail_closed(self):
        snapshot = self.snapshot()
        snapshot["candidate"]["imageTag"] = "bad tag"
        with self.assertRaisesRegex(ValueError, "image tags"):
            update_readiness.normalize_snapshot(snapshot)
        snapshot = self.snapshot()
        snapshot["details"] = {"oversized": "x" * (129 * 1024)}
        with self.assertRaisesRegex(ValueError, "128 KiB"):
            update_readiness.normalize_snapshot(snapshot)

    def test_prometheus_exports_bounded_control_plane_latency(self):
        snapshot = self.snapshot()
        snapshot["details"] = {
            "collection": {"durationMs": 4488.203},
            "package": {"inspection": {"durationMs": 566.542}},
        }
        metrics = self.store.prometheus(snapshot, now=1100)
        self.assertIn("dash_update_readiness_collection_duration_seconds 4.488203", metrics)
        self.assertIn("dash_update_readiness_package_inspection_duration_seconds 0.566542", metrics)
        snapshot["details"]["collection"]["durationMs"] = "nan"
        self.assertIn("dash_update_readiness_collection_duration_seconds 0.000000", self.store.prometheus(snapshot, now=1100))


if __name__ == "__main__":
    unittest.main()
