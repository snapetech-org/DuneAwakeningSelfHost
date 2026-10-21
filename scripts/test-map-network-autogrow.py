#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


network_capacity = load_module(
    "ensure_map_network_capacity", ROOT / "scripts" / "ensure-map-network-capacity.py",
)
router_ranges = load_module(
    "update_router_map_port_ranges", ROOT / "scripts" / "update-router-map-port-ranges.py",
)


class MapNetworkAutogrowTest(unittest.TestCase):
    def test_resolves_only_the_standard_private_env_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "project"
            private = base / "home" / ".config" / "dune-selfhost"
            root.mkdir()
            private.mkdir(parents=True)
            private.chmod(0o700)
            target = private / "compose.env"
            target.write_text("GAME_UDP_PORT_RANGE=7777:7871\n", encoding="utf-8")
            target.chmod(0o600)
            (root / ".env").symlink_to(target)

            with mock.patch.dict(os.environ, {"HOME": str(base / "home")}):
                resolved = network_capacity.resolve_env_path(root / ".env", root=root)

            self.assertEqual(target, resolved)

    def test_rejects_other_env_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            target = root / "elsewhere.env"
            target.write_text("GAME_UDP_PORT_RANGE=7777:7871\n", encoding="utf-8")
            (root / ".env").symlink_to(target)

            with self.assertRaisesRegex(network_capacity.NetworkCapacityError, "must resolve to"):
                network_capacity.resolve_env_path(root / ".env", root=root)

    def test_rejects_non_private_standard_env_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "project"
            private = base / "home" / ".config" / "dune-selfhost"
            root.mkdir()
            private.mkdir(parents=True)
            private.chmod(0o700)
            target = private / "compose.env"
            target.write_text("GAME_UDP_PORT_RANGE=7777:7871\n", encoding="utf-8")
            target.chmod(0o644)
            (root / ".env").symlink_to(target)

            with mock.patch.dict(os.environ, {"HOME": str(base / "home")}):
                with self.assertRaisesRegex(network_capacity.NetworkCapacityError, "not be accessible"):
                    network_capacity.resolve_env_path(root / ".env", root=root)

    def test_env_range_update_preserves_other_settings_and_comments(self):
        original = (
            "# do not change this comment\n"
            "DUNE_IMAGE_TAG=prod\n"
            "GAME_UDP_PORT_RANGE=7777:7871\n"
            "IGW_UDP_PORT_RANGE=7888:7982\n"
        )

        updated = network_capacity.replace_env_ranges(original, "7713:7871", "7888:8046")

        self.assertEqual(
            "# do not change this comment\n"
            "DUNE_IMAGE_TAG=prod\n"
            "GAME_UDP_PORT_RANGE=7713:7871\n"
            "IGW_UDP_PORT_RANGE=7888:8046\n",
            updated,
        )

    def test_env_range_update_rejects_duplicate_assignments(self):
        original = "GAME_UDP_PORT_RANGE=7777:7871\nGAME_UDP_PORT_RANGE=7000:7871\nIGW_UDP_PORT_RANGE=7888:7982\n"
        with self.assertRaisesRegex(network_capacity.NetworkCapacityError, "exactly one GAME_UDP_PORT_RANGE"):
            network_capacity.replace_env_ranges(original, "7713:7871", "7888:8046")

    def test_env_update_keeps_backup_private_and_preserves_original_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            env_file = root / ".env"
            backup_root = root / "backups" / "map-network-autogrow"
            original = "GAME_UDP_PORT_RANGE=7777:7871\nIGW_UDP_PORT_RANGE=7888:7982\n"
            env_file.write_text(original, encoding="utf-8")
            env_file.chmod(0o600)

            before = env_file.stat()
            backup = network_capacity.update_env_ranges(
                env_file,
                "7713:7871",
                "7888:8046",
                backup_root=backup_root,
            )

            self.assertEqual(original, backup.read_text(encoding="utf-8"))
            self.assertEqual(0o600, backup.stat().st_mode & 0o777)
            self.assertEqual(0o600, env_file.stat().st_mode & 0o777)
            self.assertEqual((before.st_dev, before.st_ino), (env_file.stat().st_dev, env_file.stat().st_ino))
            self.assertEqual(0o700, backup_root.stat().st_mode & 0o777)
            self.assertIn("GAME_UDP_PORT_RANGE=7713:7871", env_file.read_text(encoding="utf-8"))

    def test_router_change_only_widens_map_udp_ranges_and_preserves_destinations(self):
        unrelated = "<OtherRule>1234>192.168.50.2>1234>TCP>"
        game_one = "<duneA1>7777:7871>192.168.50.10>>UDP>"
        game_two = "<duneA1>7777:7806>192.168.50.11>>UDP>"
        igw = "<duneA2>7888:7982>192.168.50.12>>UDP>"
        current = unrelated + game_one + game_two + igw

        updated, ranges, destinations = router_ranges.transform_rulelist(
            current,
            (7713, 7871),
            (7888, 8046),
        )

        self.assertEqual(
            unrelated
            + "<duneA1>7713:7871>192.168.50.10>>UDP>"
            + "<duneA1>7713:7871>192.168.50.11>>UDP>"
            + "<duneA2>7888:8046>192.168.50.12>>UDP>",
            updated,
        )
        self.assertEqual({"duneA1": (7713, 7871), "duneA2": (7888, 8046)}, ranges)
        self.assertEqual(
            {"duneA1": ("192.168.50.10", "192.168.50.11"), "duneA2": ("192.168.50.12",)},
            destinations,
        )

    def test_router_change_never_shrinks_existing_rules(self):
        current = "<duneA1>7777:7871>192.168.50.10>>UDP><duneA2>7888:7982>192.168.50.11>>UDP>"

        updated, ranges, _ = router_ranges.transform_rulelist(current, (7800, 7801), (7900, 7901))

        self.assertEqual(current, updated)
        self.assertEqual({"duneA1": (7777, 7871), "duneA2": (7888, 7982)}, ranges)

    def test_router_update_fails_closed_on_missing_or_malformed_map_rule(self):
        with self.assertRaisesRegex(router_ranges.RouterRangeError, "duneA1 was not found"):
            router_ranges.transform_rulelist(
                "<duneA2>7888:7982>192.168.50.11>>UDP>", (7713, 7871), (7888, 8046),
            )
        with self.assertRaisesRegex(router_ranges.RouterRangeError, "unsupported format"):
            router_ranges.transform_rulelist(
                "<duneA1>7777>192.168.50.10>>UDP><duneA2>7888:7982>192.168.50.11>>UDP>",
                (7713, 7871), (7888, 8046),
            )


if __name__ == "__main__":
    unittest.main()
