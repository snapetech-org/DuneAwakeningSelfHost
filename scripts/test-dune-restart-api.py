#!/usr/bin/env python3
import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).with_name("dune_restart_api.py")
SPEC = importlib.util.spec_from_file_location("dune_restart_api", SCRIPT)
API = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(API)


def request(**overrides):
    value = {
        "target": "testing-hephaestus",
        "services": ["testing-hephaestus"],
        "action": "restart",
        "phase": "start",
        "dryRun": False,
        "fastDynamicStart": True,
        "jobId": "autoscaler-123",
    }
    value.update(overrides)
    return value


class RestartApiValidationTests(unittest.TestCase):
    maps = {"survival", "testing-hephaestus", "heighliner-dungeon"}

    def test_accepts_one_configured_map_start(self):
        result = API.validate_request(request(), self.maps)
        self.assertEqual(result["target"], "testing-hephaestus")
        self.assertEqual(result["phase"], "start")

    def test_host_map_roster_contains_autoscaler_target(self):
        self.assertIn("testing-hephaestus", API.configured_map_services())

    def test_accepts_a_full_fleet_request(self):
        result = API.validate_request(request(target="all", services=[]), self.maps)
        self.assertEqual(result["target"], "all")

    def test_rejects_unknown_target(self):
        with self.assertRaises(API.RequestError):
            API.validate_request(request(target="admin-panel", services=["admin-panel"]), self.maps)

    def test_rejects_services_outside_the_map_roster(self):
        with self.assertRaises(API.RequestError):
            API.validate_request(request(services=["testing-hephaestus", "postgres"]), self.maps)

    def test_rejects_cross_target_service_override(self):
        with self.assertRaises(API.RequestError):
            API.validate_request(request(services=["survival"]), self.maps)

    def test_rejects_update_and_reboot_phases(self):
        for action, phase in (("restart", "update"), ("restart", "reboot"), ("shutdown", "restart")):
            with self.subTest(action=action, phase=phase), self.assertRaises(API.RequestError):
                API.validate_request(request(action=action, phase=phase), self.maps)

    def test_rejects_unexpected_request_fields(self):
        with self.assertRaises(API.RequestError):
            API.validate_request(request(command="sh"), self.maps)


if __name__ == "__main__":
    unittest.main()
