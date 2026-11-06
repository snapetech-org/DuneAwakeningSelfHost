#!/usr/bin/env python3
import shlex
import json
import os
import re
import pathlib
import subprocess
import importlib.util
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import deployment_assurance

CLI_SPEC = importlib.util.spec_from_file_location("deployment_assurance_cli", ROOT / "scripts" / "deployment-assurance.py")
deployment_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(deployment_cli)


class DeploymentAssuranceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "admin").mkdir()
        (self.workspace / "admin" / "panel.py").write_text("print('one')\n", encoding="utf-8")
        self.secret = b"d" * 64
        self.store = deployment_assurance.Store(
            self.root / "state", self.root / "evidence", self.workspace, self.secret,
        )
        self.before = {
            "survival": {"containerId": "a" * 64, "state": "running", "startedAt": "2026-07-16T10:00:00Z"},
            "overmap": {"containerId": "b" * 64, "state": "running", "startedAt": "2026-07-16T10:00:00Z"},
            "arrakeen": {"containerId": "c" * 64, "state": "exited", "startedAt": "2026-07-16T09:00:00Z"},
        }

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self):
        path = self.workspace / "admin" / "panel.py"
        return [{"path": "admin/panel.py", "sha256": deployment_assurance.file_sha256(path)}]

    def health(self, value=True):
        return {
            "desiredStateAttested": value, "readinessCurrent": value, "sloHealthy": value,
            "changeIntegrity": value, "prometheusReadiness": value, "adminHealthy": value,
            "backupVerified": value,
        }

    def start(self, now=1000):
        return self.store.start(
            commit="a" * 40, reason="Deploy reviewed control-plane update", manifest=self.manifest(),
            principal_id="owner", snapshot=self.before,
            protected_services=["survival", "overmap", "arrakeen"], strict_services=["survival", "overmap"],
            recovery_backup={"ok": True, "path": "20260716T095000Z", "exitCode": 0}, now=now,
            source_rollback={"verified": True, "path": "deployments/before.tgz", "sha256": "e" * 64, "bytes": 100},
        )

    def test_complete_receipt_proves_manifest_continuity_health_and_backup(self):
        window = self.start()
        result = self.store.finish(
            window["id"], principal_id="owner", snapshot=self.before,
            health=self.health(), backup={"ok": True, "path": "20260716T100000Z", "exitCode": 0}, now=1100,
        )
        receipt = result["document"]["receipt"]
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["invariants"]["sourceManifestMatches"])
        self.assertTrue(receipt["invariants"]["protectedContainersUnrecreated"])
        self.assertTrue(receipt["invariants"]["runningMapProcessesUnrestarted"])
        self.assertTrue(receipt["invariants"]["strictMapServicesContinuous"])
        self.assertFalse(receipt["recoveryExecuted"])
        self.assertFalse(receipt["gameMutationExecuted"])
        self.assertTrue(result["verification"]["ok"])
        evidence = pathlib.Path(result["evidencePath"])
        self.assertEqual(0o600, evidence.stat().st_mode & 0o777)
        status = self.store.status(now=1100)
        self.assertTrue(status["ok"])
        self.assertTrue(status["latestReady"])
        self.assertFalse(status["openWindows"])
        self.assertIn("dash_deployment_assurance_latest_ready 1", self.store.prometheus(now=1100))
        with self.assertRaisesRegex(ValueError, "already finalized"):
            self.store.finish(window["id"], principal_id="owner", snapshot=self.before, health=self.health(), backup={"ok": True}, now=1200)

    def test_changed_source_produces_failed_signed_evidence(self):
        window = self.start()
        (self.workspace / "admin" / "panel.py").write_text("print('forged')\n", encoding="utf-8")
        result = self.store.finish(window["id"], principal_id="owner", snapshot=self.before, health=self.health(), backup={"ok": True}, now=1100)
        self.assertFalse(result["document"]["receipt"]["ready"])
        self.assertFalse(result["document"]["receipt"]["invariants"]["sourceManifestMatches"])
        self.assertTrue(result["verification"]["ok"])
        self.assertFalse(self.store.status(now=1100)["openWindows"])

    def test_recreated_or_restarted_map_and_failed_health_produce_failed_evidence(self):
        window = self.start()
        after = json.loads(json.dumps(self.before))
        after["arrakeen"] = {"containerId": "e" * 64, "state": "running", "startedAt": "2026-07-16T10:01:00Z"}
        after["survival"]["startedAt"] = "2026-07-16T10:02:00Z"
        result = self.store.finish(
            window["id"], principal_id="owner", snapshot=after,
            health=self.health(False), backup={"ok": False, "path": "bad", "exitCode": 1}, now=1100,
        )
        receipt = result["document"]["receipt"]
        self.assertFalse(receipt["ready"])
        self.assertFalse(receipt["invariants"]["protectedContainersUnrecreated"])
        self.assertFalse(receipt["invariants"]["runningMapProcessesUnrestarted"])
        self.assertFalse(receipt["invariants"]["strictMapServicesContinuous"])
        self.assertTrue(result["verification"]["ok"])
        self.assertFalse(self.store.status(now=1100)["latestReady"])

    def test_dynamic_start_without_container_recreate_is_allowed(self):
        window = self.start()
        after = json.loads(json.dumps(self.before))
        after["arrakeen"]["state"] = "running"
        after["arrakeen"]["startedAt"] = "2026-07-16T10:01:00Z"
        result = self.store.finish(
            window["id"], principal_id="owner", snapshot=after,
            health=self.health(), backup={"ok": True, "path": "one", "exitCode": 0}, now=1100,
        )
        self.assertTrue(result["document"]["receipt"]["ready"])

    def test_tampering_wrong_key_invalid_paths_expiry_and_cancel_fail_closed(self):
        window = self.start()
        with self.assertRaisesRegex(ValueError, "private or mutable"):
            self.store.start(
                commit="a" * 40, reason="Invalid private file manifest", manifest=[{"path": ".env", "sha256": "a" * 64}],
                principal_id="owner", snapshot=self.before, protected_services=["survival"], strict_services=["survival"],
                recovery_backup={"ok": True, "path": "one"}, source_rollback={"verified": True, "path": "before.tgz", "sha256": "e" * 64, "bytes": 1}, now=1000,
            )
        path = self.root / "state" / f"{window['id']}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["window"]["commit"] = "f" * 40
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "HMAC"):
            self.store.finish(window["id"], principal_id="owner", snapshot=self.before, health=self.health(), backup={"ok": True}, now=1100)

        second = self.start(now=2000)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.store.finish(second["id"], principal_id="owner", snapshot=self.before, health=self.health(), backup={"ok": True}, now=2000 + deployment_assurance.MAX_WINDOW_SECONDS + 1)
        cancelled = self.store.cancel(second["id"], principal_id="owner", reason="Operator cancelled change", now=2100)
        self.assertEqual("cancelled", cancelled["status"])

    def test_signed_receipt_detects_nested_and_outer_tampering(self):
        window = self.start()
        document = self.store.finish(
            window["id"], principal_id="owner", snapshot=self.before,
            health=self.health(), backup={"ok": True, "path": "one", "exitCode": 0}, now=1100,
        )["document"]
        nested = json.loads(json.dumps(document))
        nested["receipt"]["ready"] = False
        self.assertFalse(deployment_assurance.verify_signed_document(nested, self.secret)["ok"])
        outer = json.loads(json.dumps(document))
        outer["generatedAt"] = "2026-07-16T20:00:00Z"
        self.assertFalse(deployment_assurance.verify_signed_document(outer, self.secret)["ok"])
        self.assertFalse(deployment_assurance.verify_signed_document(document, b"x" * 64)["ok"])

    def test_manifest_cli_binds_current_bytes_to_exact_commit(self):
        output = self.root / "manifest.json"
        committed = (ROOT / "LICENSE").read_bytes()
        with mock.patch.object(deployment_cli, "resolve_commit", return_value="a" * 40), \
             mock.patch.object(deployment_cli, "selected_files", return_value=["LICENSE"]), \
             mock.patch.object(deployment_cli, "git_bytes", return_value=committed):
            document = deployment_cli.generate("HEAD", explicit=["LICENSE"], reason="Exact committed fixture")
        deployment_cli.atomic_write(output, document)
        self.assertEqual("dune-deployment-manifest/v1", document["schemaVersion"])
        self.assertEqual(40, len(document["commit"]))
        self.assertEqual(0o600, output.stat().st_mode & 0o777)
        verified = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "deployment-assurance.py"), "verify", "--manifest", str(output),
        ], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(0, verified.returncode, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["ok"])

        document["files"][0]["sha256"] = "f" * 64
        output.write_text(json.dumps(document), encoding="utf-8")
        tampered = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "deployment-assurance.py"), "verify", "--manifest", str(output),
        ], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertNotEqual(0, tampered.returncode)

        with mock.patch.object(deployment_cli, "resolve_commit", return_value="a" * 40), \
             mock.patch.object(deployment_cli, "selected_files", return_value=["LICENSE"]), \
             mock.patch.object(deployment_cli, "git_bytes", return_value=b"different"):
            with self.assertRaisesRegex(ValueError, "does not match exact deployment commit"):
                deployment_cli.generate("HEAD", explicit=["LICENSE"], reason="Mismatch fixture")
        selected = deployment_cli.selected_files("a" * 40, explicit=["LICENSE"])
        self.assertIn("admin/admin_panel.py", selected)
        self.assertIn("scripts/lib/snape-curl-auth.sh", selected)
        self.assertIn("admin/runtime_security.py", selected)
        self.assertTrue(deployment_cli.SUPPORT_FILES.issubset(set(selected)))

    def test_status_exposes_digest_from_a_real_signed_window(self):
        window = self.start()
        row = self.store.status(now=1001)["openWindows"][0]
        self.assertEqual(row["manifestSha256"], window["manifestSha256"])

    def test_runtime_metadata_query_authenticates_existing_record_and_exposes_no_key_material(self):
        window = self.start()
        key = self.root / "fixture-secret"
        key.write_bytes(self.secret)
        key.chmod(0o600)
        environment = {
            **os.environ, "ADMIN_WORKSPACE": str(self.workspace),
            "PYTHONPATH": str(ROOT / "admin"),
            "DUNE_DEPLOYMENT_ASSURANCE_STATE_DIR": str(self.store.root),
            "DUNE_CHANGE_INTELLIGENCE_EVIDENCE_DIR": str(self.store.evidence_root),
            "DUNE_CHANGE_INTELLIGENCE_HMAC_SECRET_FILE": str(key),
        }
        native_run = subprocess.run
        def runtime(args, **kwargs):
            self.assertEqual(args[:3], ["docker", "exec", "-i"])
            return native_run([sys.executable, "-", args[-1]], input=kwargs["input"],
                              env=environment, capture_output=True, text=True, timeout=5)
        path = self.store._window_path(window["id"])
        original = path.read_bytes()
        with mock.patch.object(deployment_cli.subprocess, "run", side_effect=runtime):
            result = deployment_cli.runtime_window_metadata(window["id"], "fixture-admin")
            self.assertEqual(result["manifestSha256"], window["manifestSha256"])
            self.assertEqual(set(result), {"id", "commit", "reason", "manifestSha256", "status"})
            self.assertEqual(path.read_bytes(), original)
            document = json.loads(original)
            document["window"]["manifestSha256"] = "f" * 64
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "HMAC verification failed"):
                deployment_cli.runtime_window_metadata(window["id"], "fixture-admin")
            key.unlink()
            with self.assertRaises(ValueError):
                deployment_cli.runtime_window_metadata(window["id"], "fixture-admin")
            self.assertFalse(key.exists())

    def test_legacy_status_recovery_requires_matching_verified_runtime_metadata(self):
        recovery = self.shell_function("recover_start_response")
        for valid_metadata in (True, False):
            with self.subTest(valid_metadata=valid_metadata):
                work = self.root / ("legacy-valid" if valid_metadata else "legacy-invalid")
                work.mkdir()
                code = work / "client/scripts"
                code.mkdir(parents=True)
                commit, digest = "a" * 40, "b" * 64
                window_id = "deployment-window-" + "1" * 32
                direct_id = "deployment-window-" + "2" * 32
                metadata = {"id": window_id, "commit": commit, "reason": "reviewed fixture", "status": "open", "manifestSha256": digest if valid_metadata else "f" * 64}
                (work / "metadata.json").write_text(json.dumps(metadata))
                (code / "deployment-assurance.py").write_text("import json,pathlib\nprint((pathlib.Path(__file__).parents[2]/'metadata.json').read_text())\n")
                (work / "manifest.json").write_text(json.dumps({"commit": commit, "manifestSha256": digest}))
                row = {"id": window_id, "commit": commit, "reason": "reviewed fixture", "status": "open"}
                (work / "status-1.json").write_text(json.dumps({"openWindows": [row]}))
                (work / "status-2.json").write_text(json.dumps({"openWindows": [{**row, "id": direct_id, "manifestSha256": digest}]}))
                fixture = '''set -e
work="$FIXTURE_WORK"
code_root="$work/client"
admin_container=fixture-admin
manifest="$work/manifest.json"
reason="reviewed fixture"
verification_timeout=60
attempts=0
api_get() { attempts=$((attempts + 1)); cat "$work/status-$attempts.json"; }
api_post_file() { touch "$work/unexpected-post"; return 1; }
sleep() { :; }
''' + recovery + "\nrecover_start_response\n"
                result = subprocess.run(["bash", "-c", fixture], env={**os.environ, "FIXTURE_WORK": str(work)}, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                recovered = json.loads((work / "start-response.json").read_text())
                self.assertEqual(recovered["id"], window_id if valid_metadata else direct_id)
                self.assertFalse((work / "unexpected-post").exists())

    def shell_function(self, name):
        source = (ROOT / "scripts/assured-control-plane-deploy.sh").read_text()
        match = re.search(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S)
        self.assertIsNotNone(match)
        return match.group(0)

    def test_verification_wait_budget_is_bounded_and_does_not_evaluate_input(self):
        source = 'read_env() { printf "%s" "$FIXTURE_TIMEOUT"; }\n' + self.shell_function("verification_timeout_seconds") + "\nverification_timeout_seconds\n"
        marker = self.root / "must-not-exist"
        for value, expected in [("", "1800"), ("60", "60"), ("0060", "60"), ("3600", "3600")]:
            result = subprocess.run(["bash", "-c", source], env={**os.environ, "FIXTURE_TIMEOUT": value}, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
        for value in ["0", "59", "3601", "-1", "99999999999999999999999", f"1[$(touch {marker})]"]:
            result = subprocess.run(["bash", "-c", source], env={**os.environ, "FIXTURE_TIMEOUT": value}, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_convergence_requests_share_the_remaining_deadline(self):
        function = self.shell_function("convergence_request_timeout")
        for offset, expected in [(600, "120"), (90, "90"), (0, "1")]:
            fixture = function + f'\nconvergence_request_timeout "$((SECONDS + {offset}))"\n'
            result = subprocess.run(["bash", "-c", fixture], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
        result = subprocess.run(["bash", "-c", function + '\nconvergence_request_timeout invalid\n'], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_start_recovery_waits_for_the_exact_manifest_without_resubmitting(self):
        work = self.root / "recovery"
        work.mkdir()
        manifest = work / "manifest.json"
        commit, digest = "a" * 40, "b" * 64
        manifest.write_text(json.dumps({"commit": commit, "manifestSha256": digest}))
        candidates = [
            {"id": "wrong-commit", "commit": "c" * 40, "reason": "reviewed fixture", "manifestSha256": digest},
            {"id": "wrong-manifest", "commit": commit, "reason": "reviewed fixture", "manifestSha256": "d" * 64},
            {"id": "expected-window", "commit": commit, "reason": "reviewed fixture", "manifestSha256": digest},
        ]
        for index, candidate in enumerate(candidates, 1):
            (work / f"status-{index}.json").write_text(json.dumps({"openWindows": [candidate]}))
        fixture = '''set -e
work="$FIXTURE_WORK"
manifest="$work/manifest.json"
reason="reviewed fixture"
verification_timeout=60
attempts=0
api_get() { attempts=$((attempts + 1)); cat "$work/status-$attempts.json"; }
api_post_file() { touch "$work/unexpected-post"; return 1; }
sleep() { :; }
''' + self.shell_function("recover_start_response") + "\nrecover_start_response\n"
        result = subprocess.run(["bash", "-c", fixture], env={**os.environ, "FIXTURE_WORK": str(work)}, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((work / "start-response.json").read_text())["id"], "expected-window")
        self.assertFalse((work / "unexpected-post").exists())

    def test_host_workflow_is_production_guarded_post_hook_safe_and_evidence_complete(self):
        source = (ROOT / "scripts" / "assured-control-plane-deploy.sh").read_text(encoding="utf-8")
        self.assertIn('required_host="${DUNE_PRODUCTION_HOST:-kspls0}"', source)
        self.assertIn('[[ "$actual_host" == "$required_host" ]]', source)
        self.assertIn('operation_lock="${DUNE_OPERATION_LOCK_FILE:-backups/admin-panel/operation.lock}"', source)
        self.assertIn('flock -w "$operation_lock_wait" 8', source)
        self.assertIn('export DUNE_OPERATION_LOCK_HELD=true', source)
        self.assertIn("./scripts/deploy-admin-panel.sh", source)
        self.assertNotIn("docker compose", source)
        self.assertNotIn("docker restart", source)
        self.assertIn("docker kill --signal HUP", source)
        self.assertGreaterEqual(source.count("validate-landsraad-coriolis-cycle.sh"), 2)
        self.assertGreaterEqual(source.count("verified_backup"), 4)  # definition plus pre/post/final calls
        self.assertIn("for attempt in 1 2 3", source)
        self.assertIn('verifier="$stage/scripts/verify-backup.sh"', source)
        self.assertIn('"$verifier" "$backup"', source)
        self.assertIn('verified_existing_backup "$pre_change_backup"', source)
        self.assertIn('pre-change backup must be an existing directory beneath workspace/backups', source)
        self.assertIn("could not create and verify a complete backup after 3 attempts", source)
        self.assertIn("wait_for_assurance_health", source)
        self.assertIn("finalize_assurance_when_healthy", source)
        self.assertIn("recover_start_response", source)
        self.assertIn("recovered durable assured change window", source)
        self.assertIn("recovered finalized assurance receipt", source)
        self.assertIn('len(matches) != 1', source)
        self.assertIn("deployment assurance finalization deferred", source)
        self.assertIn("consecutive healthy samples", source)
        self.assertIn("DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_TIMEOUT_SECONDS", source)
        self.assertIn("DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_POLL_SECONDS", source)
        self.assertIn("DUNE_DEPLOYMENT_ASSURANCE_CONVERGENCE_SAMPLES", source)
        self.assertIn("DUNE_METRICS_PROMETHEUS_PORT", source)
        self.assertIn("dash_incident_readiness_certification_ready", source)
        self.assertIn('"prometheusReadiness": any(', source)
        wait_call = source.rindex("wait_for_assurance_health\n")
        finish_request = source.rindex('python3 - "$window_id" "$post_backup" >"$work/finish.json"')
        finalize_call = source.rindex("finalize_assurance_when_healthy\n")
        receipt_check = source.rindex('python3 - "$work/finish-response.json" <<\'PY\'')
        self.assertLess(wait_call, finish_request)
        self.assertLess(finish_request, finalize_call)
        self.assertLess(finalize_call, receipt_check)
        self.assertNotIn('if [[ "$desired_state" != "attested" ]]', source)
        self.assertLess(source.index('>"$work/seal-response.json"'), source.index('>"$work/certify-response.json"'))
        self.assertIn("credential lifecycle trust-state initialization failed", source)
        self.assertLess(source.index("/api/ops/credential-lifecycle?refresh=true"), source.index('>"$work/desired.json"'))
        for phrase in (
            "START ASSURED CHANGE WINDOW", "SEAL DESIRED STATE",
            "CERTIFY INCIDENT RESPONSE READINESS", "FINALIZE ASSURED CHANGE WINDOW",
        ):
            self.assertIn(phrase, source)
        push = (ROOT / "scripts" / "push-assured-control-plane.sh").read_text(encoding="utf-8")
        preflight = push.index('"$repo_root/scripts/deploy-admin-panel.sh" --test-only')
        self.assertLess(push.index('verify --manifest "$manifest"'), preflight)
        self.assertLess(preflight, push.index('ssh -o BatchMode=yes'))
        self.assertIn("/tmp/dash-assured-stage-", push)
        self.assertIn("tar -C \"$repo_root\" -czf -", push)
        self.assertIn("--stage $stage_q", push)
        self.assertIn("DUNE_PRODUCTION_HOST=$required_q", push)
        self.assertIn('remote_env="${remote_workspace%/}/$remote_env"', push)
        self.assertIn('--pre-change-backup', push)
        support_block = push.split("for support in ", 1)[1].split("; do", 1)[0]
        staged_support = set(shlex.split(support_block.replace("\\\n", " ")))
        self.assertTrue(deployment_cli.SUPPORT_FILES.issubset(staged_support),
                        "every support dependency must actually be staged")

    def test_staged_apply_is_verified_atomic_and_has_source_rollback(self):
        stage = self.root / "stage"
        (stage / "admin").mkdir(parents=True)
        staged = stage / "admin" / "panel.py"
        staged.write_text("print('two')\n", encoding="utf-8")
        files = [{"path": "admin/panel.py", "sha256": deployment_assurance.file_sha256(staged), "bytes": staged.stat().st_size}]
        document = {
            "schemaVersion": deployment_cli.SCHEMA, "commit": "a" * 40, "reason": "Staged apply fixture",
            "files": files, "manifestSha256": deployment_assurance.digest(files),
        }
        rollback = self.root / "rollback.tgz"
        archived = deployment_cli.archive_rollback(document, self.workspace, rollback)
        self.assertTrue(archived["ok"])
        self.assertEqual(0o600, rollback.stat().st_mode & 0o777)
        applied = deployment_cli.apply_manifest(document, stage, self.workspace)
        self.assertTrue(applied["ok"])
        self.assertEqual("print('two')\n", (self.workspace / "admin" / "panel.py").read_text(encoding="utf-8"))
        import tarfile
        with tarfile.open(rollback, "r:gz") as archive:
            self.assertIn("rollback-manifest.json", archive.getnames())
            self.assertIn("files/admin/panel.py", archive.getnames())

    def test_root_container_transfers_private_state_to_host_operator(self):
        store = deployment_assurance.Store(
            self.root / "owned-state", self.root / "owned-evidence", self.workspace, self.secret,
            owner_uid=1234, owner_gid=5678,
        )
        calls = []
        with mock.patch.object(deployment_assurance.os, "geteuid", return_value=0), \
             mock.patch.object(deployment_assurance.os, "chown", side_effect=lambda path, uid, gid: calls.append((pathlib.Path(path).name, uid, gid))):
            store.initialize()
        self.assertEqual({("owned-state", 1234, 5678), ("owned-evidence", 1234, 5678)}, set(calls))


if __name__ == "__main__":
    unittest.main()
