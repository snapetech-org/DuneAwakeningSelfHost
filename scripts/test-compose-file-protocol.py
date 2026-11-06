#!/usr/bin/env python3
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent


class ComposeFileProtocolTests(unittest.TestCase):
    def test_approved_configuration_skips_writable_overlay_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            approved = pathlib.Path(directory) / 'approved-compose.json'
            approved.write_text('{}')
            env = os.environ | {'DUNE_COMPOSE_APPROVED_CONFIGURATION_FILE': str(approved)}
            result = subprocess.run(['bash', str(SCRIPTS / 'compose-files.sh'), '/missing/dotenv'], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, str(approved) + '\n')
            approved.unlink()
            result = subprocess.run(['bash', str(SCRIPTS / 'compose-files.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 78)

    def test_initial_secret_relocation_cannot_become_a_compose_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            workspace = base / 'workspace'
            scripts = workspace / 'scripts'
            scripts.mkdir(parents=True)
            for name in ('compose-files.sh', 'protect-compose-secrets.sh'):
                shutil.copy2(SCRIPTS / name, scripts / name)
            (workspace / 'compose.yaml').write_text('services: {}\n')
            environment = workspace / '.env'
            environment.write_text('COMPOSE_FILES=compose.yaml\nDUNE_MAP_AUTO_DISCOVERY_ENABLED=false\n')
            environment.chmod(0o600)
            secrets = workspace / 'config/secrets'
            secrets.mkdir(parents=True, mode=0o700)
            secret = secrets / 'fixture.secret'
            secret.write_text('fixture-value\n')
            secret.chmod(0o600)
            private = base / 'private'
            env = os.environ | {'DUNE_PRIVATE_CONFIG_DIR': str(private), 'DUNE_MAP_AUTO_DISCOVERY_ENABLED': 'false', 'COMPOSE_FILES': 'compose.yaml'}
            for attempt in range(2):
                result = subprocess.run(['bash', str(scripts / 'compose-files.sh'), str(environment)], cwd=workspace, env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, 'compose.yaml\n')
                if attempt == 0:
                    self.assertIn('Protected Compose dotenv', result.stderr)
            self.assertTrue(environment.is_symlink())
            self.assertTrue(environment.resolve().is_relative_to(private))
            self.assertEqual((secrets / 'fixture.secret').read_text(), 'fixture-value\n')
            self.assertEqual(environment.resolve().stat().st_mode & 0o077, 0)


if __name__ == '__main__':
    unittest.main()
