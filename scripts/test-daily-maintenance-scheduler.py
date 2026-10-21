#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "schedule-daily-maintenance.sh"


class DailyMaintenanceSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "request.json"
        self.headers = self.root / "headers.json"
        self.state_file = self.root / "restart-state.json"
        self.post_failures = self.root / "post-failures"
        self.env_file = self.root / ".env"
        self.env_file.write_text(
            "\n".join([
                "DUNE_ADMIN_TOKEN=test-token",
                "DUNE_ADMIN_REQUIRE_TOKEN=true",
                "DUNE_ADMIN_HOST_PORT=18081",
                "DUNE_UPDATE_REQUIRE_READINESS_RECEIPT=true",
                "",
            ]),
            encoding="utf-8",
        )
        curl = self.bin / "curl"
        curl.write_text(
            """#!/usr/bin/env python3
import json
import os, pathlib, sys
args=sys.argv[1:]
if any("/api/ops/update-readiness" in arg for arg in args):
    print(os.environ["FAKE_READINESS"])
elif any("/api/security/change-contract" in arg for arg in args):
    index=args.index("--data")
    request=json.loads(args[index + 1])
    assert request["targetPath"] == "/api/ops/restart"
    print('{"ok":true,"required":true,"contract":{"governed":true},"token":"contract-token"}')
elif any("/api/ops/restart" in arg for arg in args) and "--data" not in args:
    state_file=pathlib.Path(os.environ["FAKE_STATE_FILE"])
    print(state_file.read_text(encoding="utf-8") if state_file.exists() else '{"jobs":[]}')
else:
    index=args.index("--data")
    request=json.loads(args[index + 1])
    pathlib.Path(os.environ["FAKE_CAPTURE"]).write_text(args[index + 1], encoding="utf-8")
    stdin_config=sys.stdin.read() if "--config" in args and not sys.stdin.isatty() else ""
    pathlib.Path(os.environ["FAKE_HEADERS"]).write_text(__import__("json").dumps(args + stdin_config.splitlines()), encoding="utf-8")
    failure_file=pathlib.Path(os.environ["FAKE_POST_FAILURE_FILE"])
    remaining=int(failure_file.read_text(encoding="utf-8") or "0") if failure_file.exists() else 0
    if remaining > 0:
        failure_file.write_text(str(remaining - 1), encoding="utf-8")
        print("simulated transient POST failure", file=sys.stderr)
        raise SystemExit(28)
    state_file=pathlib.Path(os.environ["FAKE_STATE_FILE"])
    state_file.write_text(json.dumps({"jobs":[{
        "id":"fake-daily", "status":"scheduled", "action":"restart", "target":"all",
        "execute":True, "backup":True, "runAt":0,
        "automaticDailyMaintenanceKey":request.get("daily_maintenance_key"),
    }]}), encoding="utf-8")
    if os.environ.get("FAKE_POST_LOST_RESPONSE") == "true":
        print("simulated lost response", file=sys.stderr)
        raise SystemExit(28)
    print('{"ok":true}')
""",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        # The scheduler refreshes and checks the Steam package before scheduling;
        # run it against a stub repo so no real SteamCMD or /var/lib write happens.
        self.repo = self.root / "repo"
        (self.repo / "scripts" / "lib").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "lib" / "snape-curl-auth.sh", self.repo / "scripts" / "lib")
        (self.repo / "scripts" / "update-steam-tool.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.repo / "scripts" / "check-steam-update.sh").write_text(
            "#!/bin/sh\n"
            "if [ \"${FAKE_STEAM_STATUS:-current}\" = update ]; then echo 'status: update available'; exit 1; fi\n"
            "echo 'status: current'\nexit 0\n", encoding="utf-8")
        for stub in ("update-steam-tool.sh", "check-steam-update.sh"):
            (self.repo / "scripts" / stub).chmod(0o755)
        self.state_file.write_text('{"jobs":[]}', encoding="utf-8")
        self.post_failures.write_text("0", encoding="utf-8")

    def invoke(self, readiness, **overrides):
        environment = os.environ.copy()
        environment.update({
            "PATH": str(self.bin) + os.pathsep + environment.get("PATH", ""),
            "DUNE_ENV_FILE": str(self.env_file),
            "DUNE_REPO_DIR": str(self.repo),
            "DUNE_DAILY_RESTART_ALLOW_OUTSIDE_WINDOW": "true",
            "DUNE_DAILY_RESTART_UPDATE_POLICY": "certified",
            "DUNE_HOTFIX_UPDATE_LOCK_FILE": str(self.root / "hotfix-update.lock"),
            "FAKE_READINESS": json.dumps(readiness),
            "FAKE_CAPTURE": str(self.capture),
            "FAKE_HEADERS": str(self.headers),
            "FAKE_STATE_FILE": str(self.state_file),
            "FAKE_POST_FAILURE_FILE": str(self.post_failures),
        })
        environment.update(overrides)
        return subprocess.run(
            [str(SCRIPT)], cwd=ROOT, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    @staticmethod
    def update_readiness(apply_ready):
        return {
            "applyReady": apply_ready,
            "currentReceiptReady": apply_ready,
            "evaluation": {
                "candidate": {
                    "status": "update-available", "updateRequired": True,
                    "imageTag": "dune_sb_1_4_11_0", "currentImageTag": "dune_sb_1_4_10_0",
                }
            },
        }

    def test_uncertified_staged_build_refuses_current_build_restart(self):
        completed = self.invoke(self.update_readiness(False), FAKE_STEAM_STATUS="update")
        self.assertEqual(1, completed.returncode)
        self.assertIn("staged but not certified", completed.stderr)
        self.assertFalse(self.capture.exists())

    def test_uncertified_candidate_falls_back_to_current_build_restart(self):
        completed = self.invoke(self.update_readiness(False))
        self.assertEqual(0, completed.returncode, completed.stderr)
        body = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual("current", body["update_policy"])
        self.assertTrue(body["automatic_daily_maintenance"])
        self.assertIn("pinned to the current build", completed.stderr)

    def test_certified_candidate_remains_bound_for_execution_revalidation(self):
        completed = self.invoke(self.update_readiness(True))
        self.assertEqual(0, completed.returncode, completed.stderr)
        body = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual("certified", body["update_policy"])
        self.assertIn("daily maintenance update policy: certified", completed.stdout)
        args = json.loads(self.headers.read_text(encoding="utf-8"))
        self.assertIn('header = "X-DASH-Change-Contract: contract-token"', args)

    def test_invalid_readiness_response_falls_back_without_losing_daily_restart(self):
        completed = self.invoke({}, FAKE_READINESS="not-json")
        self.assertEqual(0, completed.returncode, completed.stderr)
        body = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual("current", body["update_policy"])
        self.assertIn("response was invalid", completed.stderr)

    def test_reconciles_an_existing_daily_job_without_posting_again(self):
        target_epoch = int(__import__("time").time()) + 3600
        target_epoch -= target_epoch % 60
        target_time = __import__("time").strftime("%H:%M", __import__("time").localtime(target_epoch))
        self.state_file.write_text(json.dumps({"jobs": [{
            "id": "already-scheduled", "status": "scheduled", "action": "restart", "target": "all",
            "execute": True, "backup": True, "runAt": target_epoch,
        }]}), encoding="utf-8")

        completed = self.invoke(
            self.update_readiness(False), DUNE_DAILY_RESTART_TIME=target_time,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(self.capture.exists())
        self.assertIn("persisted", completed.stdout)

    def test_retries_transient_post_failure_until_job_is_persisted(self):
        self.post_failures.write_text("1", encoding="utf-8")

        completed = self.invoke(
            self.update_readiness(False),
            DUNE_DAILY_RESTART_RETRY_SECONDS="1",
            DUNE_DAILY_RESTART_RETRY_WINDOW_SECONDS="5",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("0", self.post_failures.read_text(encoding="utf-8"))
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual("scheduled", state["jobs"][0]["status"])

    def test_lost_post_response_is_reconciled_from_persisted_job(self):
        completed = self.invoke(
            self.update_readiness(False), FAKE_POST_LOST_RESPONSE="true",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("persisted", completed.stdout)

    def test_automatic_policy_requires_explicit_receipt_enforcement_opt_out(self):
        completed = self.invoke(
            self.update_readiness(True), DUNE_DAILY_RESTART_UPDATE_POLICY="automatic",
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("blocked", completed.stderr)
        self.assertFalse(self.capture.exists())

    def test_staged_only_restart_mode_never_invokes_steam_acquisition(self):
        workspace = self.root / "workspace"
        scripts = workspace / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "restart-target.sh", scripts / "restart-target.sh")
        shutil.copy2(ROOT / "scripts" / "map_catalog.py", scripts / "map_catalog.py")
        marker = workspace / "acquisition-ran"
        (scripts / "update-steam-tool.sh").write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8",
        )
        (scripts / "check-steam-update.sh").write_text(
            "#!/bin/sh\necho 'status: current'\nexit 0\n", encoding="utf-8",
        )
        for path in scripts.iterdir():
            path.chmod(0o755)
        env_file = workspace / ".env"
        env_file.write_text(
            "DUNE_IMAGE_TAG=dune_sb_1_4_10_0\nDUNE_RESTART_STEAM_UPDATE_MODE=none\n",
            encoding="utf-8",
        )
        docker = self.bin / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "echo '{\"services\":{\"survival\":{\"command\":[\"/run\",\"/Game/Dune/Maps/Survival_1.Survival_1\",\"-PartitionIndex=1\"]}}}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": str(self.bin) + os.pathsep + environment.get("PATH", ""),
            "ENV_FILE": str(env_file),
            "DUNE_RESTART_TARGET": "all",
            "DUNE_RESTART_PHASE": "update",
            "DUNE_RESTART_ACTION": "restart",
            "DUNE_RESTART_CHECK_STEAM_UPDATE": "true",
            "DUNE_RESTART_STEAM_UPDATE_MODE": "none",
        })
        completed = subprocess.run(
            [str(scripts / "restart-target.sh"), "all"], cwd=workspace, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("acquisition disabled", completed.stdout)
        source = (scripts / "restart-target.sh").read_text(encoding="utf-8")
        self.assertIn('if [ "$steam_mode" = "none" ]', source)
        self.assertIn('if [ \\"$steam_mode\\" = none ]', source)


if __name__ == "__main__":
    unittest.main()
