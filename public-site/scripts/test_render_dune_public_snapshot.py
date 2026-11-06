#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).with_name("render-dune-public-snapshot.py")


def load_snapshot_module(env):
    with mock.patch.dict(os.environ, env, clear=True):
        spec = importlib.util.spec_from_file_location("render_dune_public_snapshot_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class PublicSnapshotConfigTest(unittest.TestCase):
    def test_explicit_public_database_beats_stale_game_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".env").write_text(
                "DUNE_GAME_DB_NAME=dune_sb_1_4_5_0\n"
                "DUNE_DATABASE=dune_sb_1_4_5_0\n",
                encoding="utf-8",
            )
            module = load_snapshot_module({
                "DUNE_ROOT": str(root),
                "STATIC_DIR": str(root / "static"),
                "DUNE_DATABASE": "dune_sb_1_4_0_0",
                "DUNE_PUBLIC_STATUS_DB_NAME": "dune_sb_1_4_5_0",
                "PATH": os.environ.get("PATH", ""),
            })

            self.assertEqual(module.DATABASE, "dune_sb_1_4_5_0")

            captured = {}

            def fake_check_output(cmd, cwd, text, timeout):
                captured["cmd"] = cmd
                captured["cwd"] = cwd
                return "[]\n"

            with mock.patch.object(module.subprocess, "check_output", side_effect=fake_check_output), \
                 mock.patch.object(module, "postgres_container", return_value="postgres-fixture"):
                module.docker_psql("select 1")

            self.assertEqual(captured["cwd"], root)
            self.assertEqual(captured["cmd"][captured["cmd"].index("-d") + 1], "dune_sb_1_4_5_0")
            self.assertEqual(captured["cmd"][:3], ["docker", "exec", "postgres-fixture"])
            self.assertNotIn("--env-file", captured["cmd"])

    def test_public_snapshot_does_not_read_private_game_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            live_env = root / "live.env"
            live_env.write_text(
                "DUNE_DATABASE=dune_sb_1_4_5_0\n"
                "COMPOSE_FILES=compose.yaml:compose.allmaps.yaml:compose.patch.yaml\n",
                encoding="utf-8",
            )
            (root / ".env").write_text("DUNE_DATABASE=dune_sb_1_4_0_0\n", encoding="utf-8")
            module = load_snapshot_module({
                "DUNE_ROOT": str(root),
                "ENV_FILE": str(live_env),
                "STATIC_DIR": str(root / "static"),
                "PATH": os.environ.get("PATH", ""),
            })

            self.assertEqual(module.DATABASE, "dune_sb_1_5_15_0")
            self.assertFalse(hasattr(module, "GAME_ENV_FILE"))
            self.assertFalse(hasattr(module, "COMPOSE_FILES"))

    def test_map_health_summary_separates_on_demand_from_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            module = load_snapshot_module({
                "DUNE_ROOT": str(root),
                "STATIC_DIR": str(root / "static"),
                "PATH": os.environ.get("PATH", ""),
            })

            summary = module.summarize_map_health([
                {"status": "online"},
                {"status": "online"},
                {"status": "warming"},
                {"status": "on-demand"},
                {"status": "on-demand"},
                {"status": "offline"},
            ])

            self.assertEqual(summary, {
                "online": 2,
                "warming": 1,
                "onDemand": 2,
                "offline": 1,
                "total": 6,
            })

    def test_core_partition_ids_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            module = load_snapshot_module({
                "DUNE_ROOT": str(root),
                "STATIC_DIR": str(root / "static"),
                "DUNE_CORE_PARTITION_IDS": "1, 2, nope, -3",
                "PATH": os.environ.get("PATH", ""),
            })

            self.assertEqual(module.CORE_PARTITION_IDS, [1, 2])


if __name__ == "__main__":
    unittest.main()
