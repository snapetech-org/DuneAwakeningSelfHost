from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AdminIngressSecurityImageTest(unittest.TestCase):
    def test_compose_defaults_to_qualified_local_image(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn(
            "${DUNE_ADMIN_INGRESS_IMAGE:-snape/dune-admin-caddy:security-2.11.9}",
            compose,
        )

    def test_builder_preserves_caddy_identity_and_validates_config(self):
        script = (ROOT / "scripts/build-admin-ingress-security-image.sh").read_text()
        self.assertIn('candidate_version" == "$running_version', script)
        self.assertIn("caddy validate", script)
        self.assertIn("brotli-libs-1\\.2\\.1", script)
        self.assertIn("DOCKER_BUILDKIT=0", script)

    def test_only_matching_utility_services_use_qualified_image(self):
        compose = (ROOT / "compose.yaml").read_text()
        qualified = (
            "${DUNE_DB_UTILS_SECURITY_IMAGE:-"
            "snape/dune-db-utils:python-security-2173753}"
        )
        self.assertEqual(compose.count(qualified), 4)
        chat = re.search(
            r"(?ms)^  admin-chat-commands:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:)",
            compose,
        ).group("body")
        self.assertIn(
            "${DUNE_ADMIN_CHAT_SECURITY_IMAGE:-"
            "snape/dune-db-utils:python-security-2162777}",
            chat,
        )
        self.assertIn('user: "1000:1001"', chat)
        self.assertNotIn(qualified, chat)


if __name__ == "__main__":
    unittest.main()
