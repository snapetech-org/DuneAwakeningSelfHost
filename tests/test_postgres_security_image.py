from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "snape/dune-postgres:security-17.12-alpine3.24-gosu1.19-go1.27.4-20261114"
VENDOR = "registry.funcom.com/funcom/self-hosting/igw-postgres:17.4-alpine-fc-13"
BASE = "postgres@sha256:005c5c37db96c1e6652a79ea31f9886402fba81ac95dc2db897739121dd7f8f2"


class PostgresSecurityImageTest(unittest.TestCase):
    def test_primary_replica_and_restore_drill_use_security_image(self):
        compose = (ROOT / "compose.yaml").read_text()
        replica = (ROOT / "compose.replica.yaml").read_text()
        qualified = "${DUNE_POSTGRES_SECURITY_IMAGE:-" + CANDIDATE + "}"
        self.assertEqual(compose.count(qualified), 2)
        self.assertIn(qualified, replica)

    def test_builder_is_bound_to_exact_supported_official_image_and_version(self):
        script = (ROOT / "scripts/build-postgres-security-image.sh").read_text()
        self.assertIn(BASE, script)
        self.assertIn("EXPECTED_POSTGRES_VERSION:-17.12", script)
        self.assertIn("docker pull --platform linux/amd64", script)
        dockerfile = (ROOT / "images/postgres-security/Dockerfile").read_text()
        self.assertIn("^3\\.24\\.", dockerfile)
        self.assertIn("pg_trgm.so", dockerfile)
        self.assertIn("pgcrypto.so", dockerfile)

    def test_restore_proxy_approves_current_and_rollback_images(self):
        proxy = (ROOT / "scripts/docker_api_proxy.py").read_text()
        self.assertIn(CANDIDATE, proxy)
        self.assertIn(VENDOR, proxy)
        self.assertIn("snape/dune-postgres:security-17.4-alpine321-fc13", proxy)
        self.assertIn("PG_IMAGES", proxy)


if __name__ == "__main__":
    unittest.main()
