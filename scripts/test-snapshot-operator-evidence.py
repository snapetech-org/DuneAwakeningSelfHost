#!/usr/bin/env python3
import os
import pathlib
import tarfile
import tempfile
import unittest
from unittest import mock
import snapshot_operator_evidence as module

class EvidenceSnapshotTests(unittest.TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
  self.root=pathlib.Path(self.directory.name);self.source=self.root/'source';self.source.mkdir();self.target=self.root/'evidence.tgz'
  self.capsule=self.source/'receipt.signed.json';self.capsule.write_bytes(b'{"signed":"unchanged"}')
 def test_unlinked_after_open_keeps_complete_selected_capsule(self):
  original=os.open
  def opening(path,*args,**kwargs):
   fd=original(path,*args,**kwargs)
   if pathlib.Path(path)==self.capsule:self.capsule.unlink()
   return fd
  with mock.patch.object(module.os,'open',side_effect=opening):self.assertEqual(module.snapshot(self.source,self.target),1)
  with tarfile.open(self.target) as archive:self.assertEqual(archive.extractfile('operator-evidence/receipt.signed.json').read(),b'{"signed":"unchanged"}')
  self.assertEqual(self.target.stat().st_mode&0o077,0)
 def test_disappeared_during_acquisition_retries_complete_selection(self):
  original=os.open;first=True
  def opening(path,*args,**kwargs):
   nonlocal first
   if pathlib.Path(path)==self.capsule and first:first=False;raise FileNotFoundError()
   return original(path,*args,**kwargs)
  with mock.patch.object(module.os,'open',side_effect=opening):self.assertEqual(module.snapshot(self.source,self.target),1)
 def test_persistent_race_never_publishes_partial_archive(self):
  with mock.patch.object(module.os,'open',side_effect=FileNotFoundError):
   with self.assertRaises(RuntimeError):module.snapshot(self.source,self.target,attempts=2)
  self.assertFalse(self.target.exists());self.assertEqual(list(self.root.glob('*.partial')),[])
 def test_symlink_capsule_is_rejected(self):
  self.capsule.unlink();self.capsule.symlink_to('/etc/hostname')
  with self.assertRaises(OSError):module.snapshot(self.source,self.target)
  self.assertFalse(self.target.exists())
 def test_existing_archive_and_empty_capsule_are_rejected(self):
  self.capsule.write_bytes(b'')
  with self.assertRaises(ValueError):module.snapshot(self.source,self.target)
  self.target.write_bytes(b'preserved')
  with self.assertRaises(ValueError):module.snapshot(self.source,self.target)
  self.assertEqual(self.target.read_bytes(),b'preserved')

if __name__=='__main__':unittest.main()
