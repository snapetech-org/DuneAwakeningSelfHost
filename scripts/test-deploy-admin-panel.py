#!/usr/bin/env python3
import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / "scripts" / "deploy-admin-panel.sh").read_text(encoding="utf-8")
ADMIN = (ROOT / "admin" / "admin_panel.py").read_text(encoding="utf-8")


class DeployAdminPanelSafetyTests(unittest.TestCase):
    def test_ingress_requires_consecutive_health_and_stops_at_the_deadline(self):
        match = re.search(r'^wait_for_admin_ingress\(\) \{\n.*?^\}', DEPLOY, re.M | re.S)
        self.assertIsNotNone(match)
        for pattern, increment, expected_code, expected_calls in (
            ("01011", 10, 0, 5),
            ("000000", 40, 1, 3),
        ):
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                count = root / "calls"
                check = root / "check-admin-ingress.sh"
                check.write_text('''#!/usr/bin/env bash
set -euo pipefail
count=0
[[ ! -e "$TEST_INGRESS_CALLS" ]] || count="$(cat "$TEST_INGRESS_CALLS")"
printf '%s' "$((count + 1))" > "$TEST_INGRESS_CALLS"
[[ "${TEST_INGRESS_PATTERN:count:1}" == 1 ]]
''')
                check.chmod(0o755)
                script = match.group() + '''
script_dir="$TEST_INGRESS_DIRECTORY"
env_file=unused-fixture
sleep() { SECONDS=$((SECONDS + TEST_CLOCK_INCREMENT)); }
wait_for_admin_ingress
'''
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", script],
                    env={**os.environ, "TEST_INGRESS_DIRECTORY": str(root),
                         "TEST_INGRESS_CALLS": str(count), "TEST_INGRESS_PATTERN": pattern,
                         "TEST_CLOCK_INCREMENT": str(increment),
                         "DUNE_ADMIN_DEPLOY_INGRESS_READY_SECONDS": "90"},
                    text=True, capture_output=True,
                )
                self.assertEqual(expected_code, result.returncode, result.stderr)
                self.assertEqual(expected_calls, int(count.read_text()))

    def test_test_only_needs_no_environment_file_or_container_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "python-calls"
            python = root / "python3"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_PYTHON_CALL_LOG"\n')
            python.chmod(0o755)
            runtime = root / "docker"
            runtime.write_text('#!/bin/sh\nexit 99\n')
            runtime.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/deploy-admin-panel.sh"), "--test-only"],
                cwd=root, env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                               "TEST_PYTHON_CALL_LOG": str(log)},
                text=True, capture_output=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            calls = log.read_text().splitlines()
            self.assertGreaterEqual(len(calls), 10)
            self.assertIn("scripts/test-admin-panel-safe-surfaces.py", calls)
            self.assertIn("scripts/test-deployment-assurance.py", calls)

    def test_deploy_holds_marker_before_recreate_and_repairs_after(self):
        marker = DEPLOY.index('maintenance_marker_created=true')
        drain = DEPLOY.index('wait_for_autoscaler_idle', marker)
        recreate = DEPLOY.index('up -d --no-deps --force-recreate admin-panel', drain)
        repair = DEPLOY.index('repair_running_map_runtime_patches', recreate)
        self.assertLess(marker, drain)
        self.assertLess(drain, recreate)
        self.assertLess(recreate, repair)
        self.assertIn('trap cleanup EXIT', DEPLOY)

    def test_runtime_repair_requires_apply_and_dry_run(self):
        match = re.search(r'^repair_running_map_runtime_patches\(\) \{\n(?P<body>.*?)\n\}', DEPLOY, re.M | re.S)
        self.assertIsNotNone(match)
        body = match.group('body')
        self.assertIn('patch-logoff-timers-runtime.sh" --local', body)
        self.assertIn('patch-logoff-timers-runtime.sh" --local --dry-run', body)

    def test_autoscaler_observes_shared_maintenance_marker(self):
        self.assertIn('AUTOSCALER_MAINTENANCE_MARKER = pathlib.Path(', ADMIN)
        self.assertGreaterEqual(ADMIN.count('marker_held = autoscaler_maintenance_requested()'), 2)
        self.assertGreaterEqual(ADMIN.count('if restart_executing or marker_held:'), 2)


if __name__ == "__main__":
    unittest.main()
