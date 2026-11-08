from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AdminIngressSecurityImageTest(unittest.TestCase):
    def test_compose_defaults_to_qualified_local_image(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn(
            "${DUNE_ADMIN_INGRESS_IMAGE:-snape/dune-admin-caddy:security-2.11.3}",
            compose,
        )

    def test_builder_preserves_caddy_identity_and_validates_config(self):
        script = (ROOT / "scripts/build-admin-ingress-security-image.sh").read_text()
        self.assertIn('candidate_version" == "$running_version', script)
        self.assertIn("caddy validate", script)
        self.assertIn("brotli-libs-1\\.2\\.1", script)
        self.assertIn("DOCKER_BUILDKIT=0", script)


if __name__ == "__main__":
    unittest.main()
