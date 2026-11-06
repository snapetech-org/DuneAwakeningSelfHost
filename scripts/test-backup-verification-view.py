#!/usr/bin/env python3
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import backup_verification_view as view


class VerificationReadViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.source = self.root / 'recovery-point'
        self.source.mkdir()
        (self.source / 'database.sqlite3').write_bytes(b'immutable fixture')
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()

    def test_copy_preserves_bytes_identity_and_cleans_only_its_private_view(self):
        before = (self.source / 'database.sqlite3').read_bytes()
        with view.local_read_view(self.source, scratch_root=self.scratch) as local:
            target = local
            self.assertNotEqual(local, self.source)
            self.assertEqual(local.name, self.source.name)
            self.assertEqual((local / 'database.sqlite3').read_bytes(), before)
            self.assertEqual(local.parent.stat().st_mode & 0o077, 0)
        self.assertFalse(target.exists())
        self.assertEqual((self.source / 'database.sqlite3').read_bytes(), before)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_source_change_during_validation_fails_and_preserves_source(self):
        with self.assertRaisesRegex(ValueError, 'backup changed during verification'):
            with view.local_read_view(self.source, scratch_root=self.scratch):
                (self.source / 'database.sqlite3').write_bytes(b'new source evidence')
        self.assertEqual((self.source / 'database.sqlite3').read_bytes(), b'new source evidence')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_local_verifier_cannot_repair_a_bad_copy_and_report_success(self):
        with self.assertRaisesRegex(ValueError, 'verifier modified'):
            with view.local_read_view(self.source, scratch_root=self.scratch) as local:
                (local / 'database.sqlite3').write_bytes(b'repaired fixture')
        self.assertEqual((self.source / 'database.sqlite3').read_bytes(), b'immutable fixture')

    def test_verifier_cannot_add_missing_recovery_evidence(self):
        with self.assertRaisesRegex(ValueError, 'recovery-file inventory'):
            with view.local_read_view(self.source, scratch_root=self.scratch) as local:
                (local / 'invented-anchor.json').write_text('{}')

    def test_scratch_cannot_be_created_inside_the_recovery_point(self):
        with self.assertRaisesRegex(ValueError, 'outside the recovery point'):
            with view.local_read_view(self.source, scratch_root=self.source):
                self.fail('overlapping scratch admitted')

    def test_file_and_directory_symlinks_are_rejected(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                target = self.root / 'outside'
                if directory:
                    target.mkdir(exist_ok=True)
                link = self.source / 'link'
                link.symlink_to(target, target_is_directory=directory)
                with self.assertRaises(ValueError):
                    with view.local_read_view(self.source, scratch_root=self.scratch):
                        self.fail('unsafe view admitted')
                link.unlink()

    def test_unreadable_inventory_fails_instead_of_silently_omitting_data(self):
        with mock.patch.object(view.os, 'scandir', side_effect=PermissionError('fixture inventory inaccessible')):
            with self.assertRaises(PermissionError):
                with view.local_read_view(self.source, scratch_root=self.scratch):
                    self.fail('unreadable recovery data omitted')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_size_bound_and_space_reserve_fail_before_copying(self):
        with mock.patch.object(view, 'MAX_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'size bound'):
                with view.local_read_view(self.source, scratch_root=self.scratch):
                    self.fail('oversized view admitted')
        with mock.patch.object(view.os, 'statvfs', return_value=mock.Mock(f_bavail=1, f_frsize=4096)):
            with self.assertRaisesRegex(ValueError, 'insufficient local space'):
                with view.local_read_view(self.source, scratch_root=self.scratch):
                    self.fail('space reserve waived')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_reader_indexes_are_ignored_but_nonempty_wal_is_recovery_data(self):
        (self.source / 'database.sqlite3-shm').write_bytes(b'reader index')
        (self.source / 'database.sqlite3-wal').touch()
        with view.local_read_view(self.source, scratch_root=self.scratch) as local:
            self.assertFalse((local / 'database.sqlite3-shm').exists())
            self.assertFalse((local / 'database.sqlite3-wal').exists())
        (self.source / 'database.sqlite3-wal').write_bytes(b'recovery frames')
        with view.local_read_view(self.source, scratch_root=self.scratch) as local:
            self.assertEqual((local / 'database.sqlite3-wal').read_bytes(), b'recovery frames')


if __name__ == '__main__':
    unittest.main()
