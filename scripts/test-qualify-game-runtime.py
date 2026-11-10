#!/usr/bin/env python3
"""Exercise the immutable payload and scanner admission gates."""

import importlib.util
import io
import pathlib
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("qualify_game_runtime", ROOT / "scripts/qualify-game-runtime.py")
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.binary = qualification.GAME_BINARY.lstrip("/")
        self.package = qualification.SITE + "setuptools/__init__.py"

    def archive(self, name, files):
        path = self.root / name
        with tarfile.open(path, "w") as archive:
            for member_name, data in files.items():
                info = tarfile.TarInfo(member_name)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_only_embedded_packaging_can_change(self):
        vendor = self.archive("vendor.tar", {self.binary: b"game", self.package: b"old"})
        candidate = self.archive("candidate.tar", {self.binary: b"game", self.package: b"new"})
        self.assertEqual((2, 1), qualification.compare_payload(vendor, candidate))
        candidate = self.archive("candidate.tar", {self.binary: b"different", self.package: b"new"})
        with self.assertRaisesRegex(ValueError, "game payload changed"):
            qualification.compare_payload(vendor, candidate)

    def test_image_config_and_base_layers_are_pinned(self):
        vendor = {"RootFS": {"Layers": ["base"]}, "Config": {"Cmd": ["/run"]}}
        candidate = {"RootFS": {"Layers": ["base", "patch"]}, "Config": {"Cmd": ["/run"]}}
        qualification.check_config(vendor, candidate)
        candidate["Config"]["Cmd"] = ["/other"]
        with self.assertRaisesRegex(ValueError, "runtime configuration"):
            qualification.check_config(vendor, candidate)

    def test_scan_must_match_image_and_cover_os_without_findings(self):
        image = "sha256:" + "a" * 64
        report = {"Metadata": {"ImageID": image, "OS": {"Family": "ubuntu"}},
                  "Results": [{"Class": "os-pkgs", "Vulnerabilities": []}]}
        qualification.check_scan(report, image)
        report["Results"][0]["Vulnerabilities"] = [{"Severity": "HIGH", "VulnerabilityID": "CVE-fixture"}]
        with self.assertRaisesRegex(ValueError, "HIGH/CRITICAL"):
            qualification.check_scan(report, image)
        report["Results"][0]["Vulnerabilities"] = []
        with self.assertRaisesRegex(ValueError, "candidate image"):
            qualification.check_scan(report, "sha256:" + "b" * 64)


if __name__ == "__main__":
    unittest.main()
