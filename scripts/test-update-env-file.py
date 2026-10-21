#!/usr/bin/env python3

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMAND = ROOT / "scripts" / "update-env-file.py"


class UpdateEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.env = self.root / ".env"
        self.env.write_text("# retained\nKEEP=yes\nDUP=old\nDUP=stale\n", encoding="utf-8")
        self.env.chmod(0o640)

    def tearDown(self):
        self.temporary.cleanup()

    def run_update(self, *arguments, check=True, env=None):
        return subprocess.run(
            [str(COMMAND), str(self.env), *arguments], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, env=env,
        )

    def test_updates_under_same_inode_and_open_descriptor(self):
        before = self.env.stat()
        descriptor = os.open(self.env, os.O_RDONLY)
        try:
            completed = self.run_update("--set", "DUP", "new", "--set", "ADDED", "a=b; literal")
            os.lseek(descriptor, 0, os.SEEK_SET)
            through_existing_mount = os.read(descriptor, 4096).decode("utf-8")
        finally:
            os.close(descriptor)
        after = self.env.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(before.st_mode, after.st_mode)
        self.assertIn("inode=", completed.stdout)
        self.assertEqual(through_existing_mount, self.env.read_text(encoding="utf-8"))
        self.assertEqual(self.env.read_text(encoding="utf-8").count("DUP="), 1)
        self.assertIn("DUP=new\n", through_existing_mount)
        self.assertIn("ADDED=a=b; literal\n", through_existing_mount)

    def test_concurrent_writers_do_not_lose_updates(self):
        processes = [
            subprocess.Popen(
                [str(COMMAND), str(self.env), "--quiet", "--set", f"KEY_{index}", str(index)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for index in range(12)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        content = self.env.read_text(encoding="utf-8")
        for index in range(12):
            self.assertIn(f"KEY_{index}={index}\n", content)

    def test_refuses_symlink_invalid_key_and_oversize(self):
        target = self.root / "target"
        target.write_text("SAFE=yes\n", encoding="utf-8")
        self.env.unlink()
        self.env.symlink_to(target)
        refused = self.run_update("--set", "SAFE", "no", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "SAFE=yes\n")

        self.env.unlink()
        self.env.write_text("SAFE=yes\n", encoding="utf-8")
        invalid = self.run_update("--set", "BAD-KEY", "value", check=False)
        self.assertNotEqual(invalid.returncode, 0)
        oversized = self.run_update("--max-bytes", "8", "--set", "SAFE", "value", check=False)
        self.assertNotEqual(oversized.returncode, 0)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "SAFE=yes\n")

    def test_updates_only_the_approved_private_env_symlink_in_place(self):
        home = self.root / "home"
        private_dir = home / ".config" / "dune-selfhost"
        private_dir.mkdir(parents=True)
        private_dir.chmod(0o700)
        target = private_dir / "compose.env"
        target.write_text("SAFE=yes\n", encoding="utf-8")
        target.chmod(0o600)
        self.env.unlink()
        self.env.symlink_to(target)
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        before = target.stat()

        completed = self.run_update(
            "--allow-protected-link", "--set", "SAFE", "updated", env=environment,
        )

        after = target.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(target.read_text(encoding="utf-8"), "SAFE=updated\n")
        self.assertIn("inode=", completed.stdout)

    def test_allow_protected_link_rejects_other_targets(self):
        target = self.root / "target"
        target.write_text("SAFE=yes\n", encoding="utf-8")
        self.env.unlink()
        self.env.symlink_to(target)

        refused = self.run_update(
            "--allow-protected-link", "--set", "SAFE", "no", check=False,
        )

        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "SAFE=yes\n")


if __name__ == "__main__":
    unittest.main()
