from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "snape/dune-postgres:security-17.4-alpine321-fc13"
VENDOR = "registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13"


class PostgresSecurityImageTest(unittest.TestCase):
    def test_primary_replica_and_restore_drill_use_security_image(self):
        compose = (ROOT / "compose.yaml").read_text()
        replica = (ROOT / "compose.replica.yaml").read_text()
        qualified = "${DUNE_POSTGRES_SECURITY_IMAGE:-" + CANDIDATE + "}"
        self.assertEqual(compose.count(qualified), 2)
        self.assertIn(qualified, replica)

    def test_builder_is_bound_to_exact_vendor_image_and_version(self):
        script = (ROOT / "scripts/build-postgres-security-image.sh").read_text()
        self.assertIn(
            "sha256:3a6efbd2d908f0acdafca6f4a6ffe5a3eb77d4ec634d6f9359bfcb0a36538531",
            script,
        )
        self.assertIn("EXPECTED_POSTGRES_VERSION:-17.4", script)
        self.assertIn("candidate does not preserve base layer", script)
        self.assertNotIn("docker pull", script)

    def test_restore_proxy_approves_only_bound_vendor_and_security_images(self):
        proxy = (ROOT / "scripts/docker_api_proxy.py").read_text()
        self.assertIn(CANDIDATE, proxy)
        self.assertIn(VENDOR, proxy)
        self.assertIn("PG_IMAGES", proxy)


if __name__ == "__main__":
    unittest.main()
