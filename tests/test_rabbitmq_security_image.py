from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RabbitMQSecurityImageTest(unittest.TestCase):
    def test_compose_uses_one_version_bound_security_image(self):
        compose = (ROOT / "compose.yaml").read_text()
        qualified = (
            "${DUNE_RABBITMQ_SECURITY_IMAGE:-"
            "snape/dune-rabbitmq:security-2168446}"
        )
        self.assertEqual(compose.count(qualified), 3)

    def test_builder_is_bound_to_exact_vendor_image_and_version(self):
        script = (ROOT / "scripts/build-rabbitmq-security-image.sh").read_text()
        self.assertIn(
            "sha256:395d2d85bb9bd42165421430959a832e2166c7518a31c48932450bbdf4f34f59",
            script,
        )
        self.assertIn("EXPECTED_RABBITMQ_VERSION:-3.13.7", script)
        self.assertIn("candidate does not preserve base layer", script)
        self.assertNotIn("docker pull", script)


if __name__ == "__main__":
    unittest.main()
