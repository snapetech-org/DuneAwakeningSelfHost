#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "restart-post-start-health.sh"


class RestartPostStartHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dune-post-start-health-")
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        shutil.copy2(SCRIPT, self.scripts / SCRIPT.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text("COMPOSE_PROJECT_NAME=dune_server\n", encoding="utf-8")
        self.marker = self.root / "image-check-ran"
        self.running_services = self.root / "running-services"
        self.running_services.write_text("survival\novermap\n", encoding="utf-8")

        self.write_executable("compose-files.sh", "#!/bin/sh\nprintf 'compose.yaml\\n'\n")
        self.write_executable(
            "map_catalog.py",
            "#!/bin/sh\nprintf 'survival overmap\\n'\n",
        )
        self.write_executable(
            "status.sh",
            """#!/bin/sh
printf 'current_ready_alive=%s current_alive_active=%s active_servers=%s partitions=2 game_sg_connections=%s admin_sg_connections=2\\n' \\
  "${TEST_FARM_READY:-2}" "${TEST_FARM_READY:-2}" "${TEST_FARM_READY:-2}" "${TEST_FARM_READY:-2}"
printf 'core_ready_alive=%s core_alive_active=%s core_active_servers=%s core_partitions=2\\n' \\
  "${TEST_CORE_READY:-2}" "${TEST_CORE_READY:-2}" "${TEST_CORE_READY:-2}"
""",
        )
        self.write_executable("verify-rmq-auth-path.sh", "#!/bin/sh\nexit 0\n")
        self.write_executable("refresh-map-neighbors.sh", "#!/bin/sh\nexit 0\n")
        self.write_executable(
            "verify-running-image-tag.sh",
            "#!/bin/sh\ntouch \"$TEST_IMAGE_MARKER\"\nexit 0\n",
        )
        self.write_executable(
            "docker",
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  inspect) printf 'true\\n' ;;\n"
            "  exec) exit 0 ;;\n"
            "  ps) cat \"$TEST_RUNNING_SERVICES\" ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
        )

        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{self.scripts}:{self.root}:{self.env['PATH']}",
            "ENV_FILE": str(self.env_file),
            "COMPOSE_PROJECT_NAME": "dune_server",
            "DUNE_RESTART_TARGET": "all",
            "DUNE_RESTART_PHASE": "start",
            "DUNE_RESTART_POST_START_TIMEOUT_SECONDS": "2",
            "DUNE_RESTART_POST_START_INTERVAL_SECONDS": "1",
            "DUNE_RESTART_POST_START_STABILITY_SECONDS": "0",
            "DUNE_LAN_REFLECTION_REFRESH_AFTER_START": "false",
            "DUNE_LANDSRAAD_CORIOLIS_GUARD_ENABLED": "false",
            "DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED": "false",
            "TEST_IMAGE_MARKER": str(self.marker),
            "TEST_RUNNING_SERVICES": str(self.running_services),
        })

    def tearDown(self):
        self.temp.cleanup()

    def write_executable(self, name, content):
        path = self.scripts / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_health(self, **environment):
        env = self.env.copy()
        env.update(environment)
        return subprocess.run(
            ["bash", str(self.scripts / SCRIPT.name)],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_all_active_maps_and_core_readiness_are_required(self):
        result = self.run_health()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())

    def test_missing_active_map_prevents_success(self):
        self.running_services.write_text("survival\n", encoding="utf-8")
        result = self.run_health()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_unready_core_prevents_success(self):
        result = self.run_health(TEST_CORE_READY="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_incomplete_active_map_registration_prevents_success(self):
        result = self.run_health(TEST_FARM_READY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
