#!/usr/bin/env python3
import copy
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import desired_state


def container(service="admin-panel", image="image:v1", image_id="sha256:one", token="secret-one", privileged=False):
    return {
        "Name": f"/{service}", "Image": image_id,
        "Config": {
            "Image": image, "Entrypoint": ["/entry"], "Cmd": ["serve"], "WorkingDir": "/workspace", "User": "1000",
            "Env": [f"TOKEN={token}", "MODE=production"],
            "Labels": {"com.docker.compose.service": service},
        },
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped"}, "Privileged": privileged,
            "ReadonlyRootfs": True, "NetworkMode": "project_default", "CapAdd": [], "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 128, "Memory": 1024,
            "MemorySwap": 1024, "NanoCpus": 1000000000, "CpusetCpus": "0-3",
        },
        "Mounts": [{"Destination": "/workspace/config", "Type": "bind", "RW": False, "Source": "/host/config"}],
        "NetworkSettings": {"Networks": {"project_default": {"IPAddress": "172.31.0.9", "MacAddress": "02:42:ac:1f:00:09"}}},
    }


class DesiredStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "admin").mkdir()
        (self.root / "config" / "secrets").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        (self.root / ".env").write_text("TOKEN=not-returned\n", encoding="utf-8")
        (self.root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (self.root / "admin" / "admin_panel.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "scripts" / "restart-target.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        self.policy_path = self.root / "config" / "desired-state.json"
        self.policy_path.write_text(json.dumps({
            "schemaVersion": 1, "pollSeconds": 60, "observationRetentionDays": 90,
            "maxFiles": 100, "maxFileBytes": 1000000, "maxTotalBytes": 10000000,
            "trackFileModes": True, "trackContainers": True,
            "includePatterns": [".env", "compose*.yaml", "admin/**/*", "scripts/*", "config/**/*"],
            "excludePatterns": ["config/desired-state.key"],
            "requiredPaths": [".env", "compose.yaml", "admin/admin_panel.py", "scripts/restart-target.sh"],
            "criticalPatterns": [".env", "compose*.yaml", "admin/**/*"],
        }), encoding="utf-8")
        self.secret_path = self.root / "config" / "desired-state.key"
        self.secret_path.write_text("a" * 64 + "\n", encoding="utf-8")
        self.secret_path.chmod(0o600)
        self.database = self.root / "backups" / "desired-state.sqlite3"
        self.store = desired_state.Store(self.database, self.policy_path, self.secret_path)
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, containers=None, at=1000):
        return desired_state.build_snapshot(self.root, self.store.policy, containers if containers is not None else [container()], self.store.secret, observed_at=at)

    def runtime_transition(self):
        return {'service': 'worker', 'fromImageId': 'sha256:'+'a'*64,
                'fromImageRef': 'vendor:v1', 'toImageId': 'sha256:'+'b'*64,
                'toImageRef': 'qualified:v2'}

    def test_runtime_attestation_seals_only_exact_image_transition(self):
        old = container(service='worker', image='vendor:v1', image_id='sha256:'+'a'*64)
        new = container(service='worker', image='qualified:v2', image_id='sha256:'+'b'*64)
        baseline = self.store.seal(self.snapshot([old]), 'operator', 'initial')
        current = self.snapshot([new], at=1001)
        self.store.observe(current, at=1001)
        self.assertTrue(self.store.status()['openFindings'])
        result = self.store.seal(current, 'operator', 'verified image', runtime_transition=self.runtime_transition())
        self.assertNotEqual(result['baselineId'], baseline['baselineId'])
        self.assertFalse(self.store.status()['openFindings'])
        self.assertTrue(self.store.verify()['ok'])

    def test_runtime_attestation_rejects_unrelated_settings_and_preserves_baseline(self):
        old = container(service='worker', image='vendor:v1', image_id='sha256:'+'a'*64)
        baseline = self.store.seal(self.snapshot([old]), 'operator', 'initial')
        for field in ('environment', 'mount', 'command', 'memory', 'file', 'other-container'):
            with self.subTest(field=field):
                new = container(service='worker', image='qualified:v2', image_id='sha256:'+'b'*64)
                raw = [new]
                if field == 'environment': new['Config']['Env'].append('UNREVIEWED=value')
                if field == 'mount': new['Mounts'][0]['RW'] = True
                if field == 'command': new['Config']['Cmd'].append('--unreviewed')
                if field == 'memory': new['HostConfig']['Memory'] += 1
                if field == 'file': (self.root/'compose.yaml').write_text('services: {changed: {}}')
                if field == 'other-container': raw.append(container(service='other'))
                current = self.snapshot(raw, at=1001)
                self.store.observe(current, at=1001)
                with self.assertRaises(ValueError):
                    self.store.seal(current, 'operator', 'reject unrelated drift', runtime_transition=self.runtime_transition())
                self.assertEqual(self.store.active_baseline()['id'], baseline['baselineId'])
                self.assertTrue(self.store.status()['openFindings'])
                (self.root/'compose.yaml').write_text('services: {}\n')

    def test_runtime_attestation_accepts_only_reviewed_disabled_brt_aliases(self):
        legacy = 'DUNE_BRT_DD_ACTION_GATE_PATCH_ENABLED'
        canonical = 'DUNE_BRT_DD_ACTION_GATE_BINARY_PATCH_ENABLED'
        old = container(service='worker', image='vendor:v1', image_id='sha256:'+'a'*64)
        new = container(service='worker', image='qualified:v2', image_id='sha256:'+'b'*64)
        old['Config']['Env'].append(legacy+'=false')
        new['Config']['Env'].append(canonical+'=false')
        self.store.seal(self.snapshot([old]), 'operator', 'initial disabled legacy flag')
        result = self.store.seal(self.snapshot([new]), 'operator', 'verified disabled alias migration', runtime_transition=self.runtime_transition())
        self.assertTrue(result['ok'])
        self.assertTrue(self.store.verify()['ok'])

    def test_runtime_attestation_rejects_enabled_or_arbitrary_aliases(self):
        cases = [
            ('DUNE_BRT_DD_ACTION_GATE_PATCH_ENABLED', 'DUNE_BRT_DD_ACTION_GATE_BINARY_PATCH_ENABLED', 'true', 'true'),
            ('DUNE_BRT_DD_ACTION_GATE_PATCH_ENABLED', 'DUNE_BRT_DD_ACTION_GATE_BINARY_PATCH_ENABLED', 'false', 'true'),
            ('DUNE_BRT_DD_TOOL_ENABLE_PATCH_ENABLED', 'DUNE_BRT_DD_TOOL_ENABLE_BINARY_PATCH_ENABLED', 'true', 'false'),
            ('UNREVIEWED_OLD', 'UNREVIEWED_NEW', 'false', 'false'),
        ]
        for legacy, canonical, old_value, new_value in cases:
            with self.subTest(legacy=legacy, old_value=old_value, new_value=new_value):
                old = container(service='worker', image='vendor:v1', image_id='sha256:'+'a'*64)
                new = container(service='worker', image='qualified:v2', image_id='sha256:'+'b'*64)
                old['Config']['Env'].append(legacy+'='+old_value)
                new['Config']['Env'].append(canonical+'='+new_value)
                baseline = self.store.seal(self.snapshot([old]), 'operator', 'reviewed fixture baseline')
                with self.assertRaises(ValueError):
                    self.store.seal(self.snapshot([new]), 'operator', 'reject changed flag semantics', runtime_transition=self.runtime_transition())
                self.assertEqual(self.store.active_baseline()['id'], baseline['baselineId'])

    def test_runtime_attestation_rejects_wrong_source_or_candidate(self):
        old = container(service='worker', image='vendor:v1', image_id='sha256:'+'a'*64)
        new = container(service='worker', image='qualified:v2', image_id='sha256:'+'b'*64)
        self.store.seal(self.snapshot([old]), 'operator', 'initial')
        for field in ('fromImageId', 'toImageId', 'service'):
            transition = self.runtime_transition()
            transition[field] = 'different' if field == 'service' else 'sha256:'+'c'*64
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.seal(self.snapshot([new]), 'operator', 'wrong identity', runtime_transition=transition)

    def test_policy_secret_and_private_modes(self):
        self.assertEqual(self.store.policy["pollSeconds"], 60)
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.database.parent.stat().st_mode & 0o777, 0o700)
        self.secret_path.chmod(0o644)
        with self.assertRaises(PermissionError):
            desired_state.read_secret(self.secret_path)

    def test_verification_reads_key_once_and_rechecks_after_all_rows(self):
        self.store.seal(self.snapshot(at=1000), actor="fixture", reason="bounded verification fixture")
        for at in range(1001, 1006):
            self.store.observe(self.snapshot(at=at))
        with mock.patch.object(desired_state, "read_secret", wraps=desired_state.read_secret) as read:
            result = self.store.verify()
        self.assertTrue(result["ok"])
        self.assertEqual(2, read.call_count)

    def test_verification_fails_if_key_rotates_during_row_checks(self):
        self.store.seal(self.snapshot(at=1000), actor="fixture", reason="key rotation fixture")
        original = self.store._sign
        changed = False

        def rotate(payload, *, secret=None):
            nonlocal changed
            if not changed:
                changed = True
                self.secret_path.write_text("b" * 64 + "\n", encoding="utf-8")
            return original(payload, secret=secret)

        with mock.patch.object(self.store, "_sign", side_effect=rotate):
            result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertIn("key changed during verification", result["error"])

    def test_snapshot_never_contains_plaintext_secrets_or_mount_sources(self):
        snapshot = self.snapshot()
        encoded = json.dumps(snapshot)
        self.assertNotIn("not-returned", encoded)
        self.assertNotIn("secret-one", encoded)
        self.assertNotIn("/host/config", encoded)
        self.assertEqual(snapshot["fileCount"], 5)
        self.assertIn("TOKEN", snapshot["containers"]["admin-panel"]["envHmacs"])

    def test_container_fingerprint_ignores_ephemeral_network_identity_but_tracks_static_ipam(self):
        first = container()
        second = copy.deepcopy(first)
        second["NetworkSettings"]["Networks"]["project_default"].update({
            "IPAddress": "172.31.0.99", "MacAddress": "02:42:ac:1f:00:63", "EndpointID": "runtime-two",
        })
        self.assertEqual(
            desired_state.normalize_container(first, self.store.secret)["fingerprint"],
            desired_state.normalize_container(second, self.store.secret)["fingerprint"],
        )
        first["NetworkSettings"]["Networks"]["project_default"]["IPAMConfig"] = {"IPv4Address": "172.31.0.9"}
        second["NetworkSettings"]["Networks"]["project_default"]["IPAMConfig"] = {"IPv4Address": "172.31.0.10"}
        self.assertNotEqual(
            desired_state.normalize_container(first, self.store.secret)["fingerprint"],
            desired_state.normalize_container(second, self.store.secret)["fingerprint"],
        )

    def test_seal_attest_and_hmac_integrity(self):
        baseline = self.store.seal(self.snapshot(), "operator", "known-good deploy", at=1000)
        result = self.store.observe(self.snapshot(at=1060), at=1060)
        self.assertTrue(result["sealed"])
        self.assertEqual(result["driftCount"], 0)
        status = self.store.status()
        self.assertEqual(status["state"], "attested")
        self.assertEqual(status["baseline"]["id"], baseline["baselineId"])
        self.assertTrue(status["integrity"]["ok"])

    def test_file_and_container_drift_open_acknowledge_resolve_and_reopen(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        (self.root / ".env").write_text("TOKEN=changed\n", encoding="utf-8")
        drifted = self.snapshot([container(image="image:v2", image_id="sha256:two", privileged=True)], at=1060)
        observed = self.store.observe(drifted, at=1060)
        self.assertEqual(observed["driftCount"], 2)
        self.assertEqual(observed["criticalCount"], 2)
        status = self.store.status()
        self.assertEqual(status["state"], "drift")
        subjects = {row["subject"] for row in status["openFindings"]}
        self.assertEqual(subjects, {".env", "admin-panel"})
        finding = status["openFindings"][0]
        self.store.acknowledge(finding["id"], "operator", "reviewing", at=1070)
        acknowledged = next(row for row in self.store.status()["openFindings"] if row["id"] == finding["id"])
        self.assertEqual(acknowledged["note"], "reviewing")
        (self.root / ".env").write_text("TOKEN=not-returned\n", encoding="utf-8")
        self.store.observe(self.snapshot(at=1080), at=1080)
        self.assertEqual(self.store.status()["state"], "attested")
        self.store.observe(drifted, at=1090)
        reopened = self.store.status()["openFindings"]
        self.assertEqual(len(reopened), 2)
        self.assertTrue(all(row["acknowledgedAt"] is None for row in reopened))

    def test_missing_required_file_is_critical_drift(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        (self.root / "compose.yaml").unlink()
        snapshot = self.snapshot(at=1060)
        result = self.store.observe(snapshot, at=1060)
        self.assertEqual(result["criticalCount"], 1)
        row = self.store.status()["openFindings"][0]
        self.assertEqual(row["details"]["after"]["kind"], "missing")

    def test_maintenance_suppresses_alert_metric_not_evidence(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        (self.root / ".env").write_text("DRIFT=yes\n", encoding="utf-8")
        self.store.observe(self.snapshot(at=1060), maintenance_active=True, at=1060)
        metrics = self.store.prometheus()
        self.assertIn("dash_desired_state_open_critical_drift 1", metrics)
        self.assertIn("dash_desired_state_alertable_critical_drift 0", metrics)
        self.assertIn("dash_desired_state_maintenance_active 1", metrics)

    def test_reseal_preserves_history_and_resolves_findings(self):
        first = self.store.seal(self.snapshot(), "operator", "first", at=1000)
        (self.root / ".env").write_text("DRIFT=yes\n", encoding="utf-8")
        changed = self.snapshot(at=1060)
        self.store.observe(changed, at=1060)
        second = self.store.seal(changed, "operator", "approved change", at=1070)
        status = self.store.status()
        self.assertNotEqual(first["baselineId"], second["baselineId"])
        self.assertEqual(status["state"], "attested")
        self.assertEqual(len([row for row in status["events"] if row["eventType"] == "baseline-sealed"]), 2)
        self.assertEqual([row["id"] for row in status["baselineHistory"]], [second["baselineId"], first["baselineId"]])
        self.assertTrue(status["baselineHistory"][0]["active"])
        self.assertFalse(status["baselineHistory"][1]["active"])

    def test_resolution_reconciliation_requires_existing_signed_resolved_finding(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        (self.root / ".env").write_text("DRIFT=yes\n")
        self.store.observe(self.snapshot(at=1060), at=1060)
        finding = self.store.status()["openFindings"][0]["id"]
        self.assertEqual({}, self.store.verified_resolutions([finding, "missing"]))
        self.store.seal(self.snapshot(at=1070), "operator", "approved", at=1070)
        evidence = self.store.verified_resolutions([finding, "missing"])
        self.assertEqual({finding}, set(evidence))
        self.assertTrue(evidence[finding]["resolvedAt"])
        connection = sqlite3.connect(self.database)
        connection.execute("update findings set resolved_at=1071 where id=?", (finding,))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, "failed verification"):
            self.store.verified_resolutions([finding])

    def test_tampering_and_missing_triggers_fail_verification(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        connection = sqlite3.connect(self.database)
        connection.execute("drop trigger desired_state_baselines_no_update")
        connection.execute("drop trigger desired_state_baselines_no_delete")
        connection.execute("update baselines set signature='bad'")
        connection.commit()
        connection.close()
        self.assertFalse(self.store.verify()["ok"])

    def test_finding_resolution_and_notes_are_hmac_authenticated(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        (self.root / ".env").write_text("DRIFT=yes\n", encoding="utf-8")
        self.store.observe(self.snapshot(at=1060), at=1060)
        finding = self.store.status()["openFindings"][0]
        self.store.acknowledge(finding["id"], "operator", "owned", at=1070)
        self.assertTrue(self.store.verify()["findingSignaturesValid"])
        connection = sqlite3.connect(self.database)
        connection.execute("update findings set resolved_at=1071,note='forged' where id=?", (finding["id"],))
        connection.commit()
        connection.close()
        verification = self.store.verify()
        self.assertFalse(verification["ok"])
        self.assertFalse(verification["findingSignaturesValid"])
        self.assertEqual("invalid", self.store.status()["state"])

    def test_legacy_unsigned_findings_are_migrated_once(self):
        database = self.root / "legacy" / "desired-state.sqlite3"
        database.parent.mkdir()
        connection = sqlite3.connect(database)
        connection.execute("""create table findings (
            id text primary key, finding_key text not null unique, category text not null,
            subject text not null, change_type text not null, critical integer not null,
            first_seen_at real not null, last_seen_at real not null, resolved_at real,
            acknowledged_at real, acknowledged_by text, note text not null default '', details_json text not null
        )""")
        connection.execute(
            "insert into findings values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy-1", "files:.env:changed", "files", ".env", "changed", 1, 1.0, 2.0, None, None, None, "", "{}"),
        )
        connection.commit()
        connection.close()
        migrated = desired_state.Store(database, self.policy_path, self.secret_path)
        result = migrated.initialize()
        self.assertTrue(result["ok"])
        self.assertTrue(result["findingSignaturesValid"])
        connection = sqlite3.connect(database)
        signature = connection.execute("select signature from findings where id='legacy-1'").fetchone()[0]
        schema = connection.execute("select value from metadata where key='schema_version'").fetchone()[0]
        connection.close()
        self.assertEqual(64, len(signature))
        self.assertEqual("2", schema)

    def test_backup_is_consistent_and_private(self):
        self.store.seal(self.snapshot(), "operator", "baseline", at=1000)
        target = self.root / "archive" / "desired-state.sqlite3"
        result = self.store.backup(target)
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_bounds_duplicate_services_and_unsealed_state(self):
        unsealed = self.store.observe(self.snapshot(at=1000), at=1000)
        self.assertFalse(unsealed["sealed"])
        self.assertEqual(self.store.status()["state"], "unsealed")
        with self.assertRaises(ValueError):
            desired_state.build_snapshot(self.root, self.store.policy, [container(), container()], self.store.secret)


if __name__ == "__main__":
    unittest.main()
