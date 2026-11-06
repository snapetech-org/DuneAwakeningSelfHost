#!/usr/bin/env python3
import importlib.util
import pathlib
import unittest
import tempfile
import types
from unittest import mock


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
        with tempfile.NamedTemporaryFile(mode="w") as fixture:
            fixture.write("DUNE_WORLD_PARTITION_COUNT=31\n")
            fixture.flush()
            with mock.patch.object(API, "ENV_FILE", pathlib.Path(fixture.name)):
                self.assertIn("testing-hephaestus", API.configured_map_services())

    def test_accepts_a_full_fleet_request(self):
        result = API.validate_request(request(target="all", services=[]), self.maps)
        self.assertEqual(result["target"], "all")

    def test_build_check_is_read_only_and_farm_scoped(self):
            payload = request(target="all", services=[], phase="check-current-build", fastDynamicStart=False)
            self.assertEqual(API.validate_request(payload, self.maps)["phase"], "check-current-build")
            for overrides in ({"target": "survival", "services": ["survival"]},
                              {"services": ["survival"]}, {"dryRun": True},
                              {"fastDynamicStart": True}, {"action": "shutdown"}):
                with self.subTest(overrides=overrides), self.assertRaises(API.RequestError):
                    API.validate_request({**payload, **overrides}, self.maps)

    def test_build_check_executes_only_the_host_checker_without_write_env(self):
            payload = request(target="all", services=[], phase="check-current-build", fastDynamicStart=False)
            with mock.patch.object(API.host_service_control, "Admission") as admission, mock.patch.object(API.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="status: current", stderr="")) as run:
                admission.return_value.expected_hostname = API.socket.gethostname().split(".")[0]
                self.assertTrue(API.execute_restart(payload)["ok"])
                admission.return_value.recover_existing.assert_not_called()
            self.assertEqual(run.call_args.args[0], [str(API.ROOT / "scripts/check-steam-update.sh"), str(API.ENV_FILE)])
            self.assertEqual(run.call_args.kwargs["timeout"], 90)
    def test_build_check_is_read_only_and_all_farm_only(self):
        accepted = request(target="all", services=[], phase="check-current-build", fastDynamicStart=False)
        self.assertEqual(API.validate_request(accepted, self.maps)["phase"], "check-current-build")
        for changes in ({"target": "survival"}, {"services": ["survival"]},
                        {"dryRun": True}, {"fastDynamicStart": True}):
            with self.subTest(changes=changes), self.assertRaises(API.RequestError):
                API.validate_request({**accepted, **changes}, self.maps)

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
