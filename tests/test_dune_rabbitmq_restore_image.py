import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "restore_image", Path(__file__).resolve().parents[1]
    / "scripts/dune-rabbitmq-restore-current-image.py",
)
restore_image = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore_image)


class DeployedImageTests(unittest.TestCase):
    def brokers(self):
        return [{"image": "sha256:" + "a" * 64, "running": True,
                 "project": "fixture", "service": service}
                for service in ["admin-rmq", "game-rmq"]]

    def inspect(self, brokers):
        return patch.object(restore_image.subprocess, "run", return_value=SimpleNamespace(
            stdout="\n".join(json.dumps(b) for b in brokers)))

    def test_only_inspects_exact_brokers_and_returns_immutable_image(self):
        with self.inspect(self.brokers()) as run:
            self.assertEqual(restore_image.deployed_image("fixture"), "sha256:" + "a" * 64)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["docker", "inspect", "--type", "container"])
        self.assertEqual(args[-2:], ["fixture-admin-rmq-1", "fixture-game-rmq-1"])
        self.assertNotIn("Env", args[5])

    def test_rejects_differing_images(self):
        brokers = self.brokers()
        brokers[1]["image"] = "sha256:" + "b" * 64
        with self.inspect(brokers), self.assertRaises(ValueError):
            restore_image.deployed_image("fixture")

    def test_rejects_stopped_or_wrongly_identified_broker(self):
        for key, value in [("running", False), ("project", "other"), ("service", "other")]:
            with self.subTest(key=key):
                brokers = self.brokers()
                brokers[0][key] = value
                with self.inspect(brokers), self.assertRaises(ValueError):
                    restore_image.deployed_image("fixture")

    def test_rejects_missing_broker(self):
        with self.inspect(self.brokers()[:1]), self.assertRaises(ValueError):
            restore_image.deployed_image("fixture")

    def test_rejects_mutable_or_malformed_image_reference(self):
        for image in ["vendor/rabbitmq:latest", "sha256:abc", ""]:
            with self.subTest(image=image):
                brokers = self.brokers()
                brokers[0]["image"] = image
                with self.inspect(brokers), self.assertRaises(ValueError):
                    restore_image.deployed_image("fixture")

    def test_rejects_invalid_project_before_docker_access(self):
        with patch.object(restore_image.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                restore_image.deployed_image("../other")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
