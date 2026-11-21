import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "scripts/security-images/steamcmd/assess-debian.py"
SPEC = importlib.util.spec_from_file_location("assess_debian", PATH)
assessment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assessment)


class DebianAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.cve = "CVE-2026-78408"
        self.purl = "pkg:deb/debian/bsdutils@2.42.4-1?arch=amd64&distro=debian-13.8&epoch=1"
        self.finding = {"PkgName": "bsdutils", "InstalledVersion": "1:2.42.4-1",
                        "VulnerabilityID": self.cve, "PkgIdentifier": {"PURL": self.purl}}
        self.installed = {("bsdutils", "amd64", "1:2.42.4-1"): ("util-linux", "2.42.4-1")}
        self.release = {"status": "resolved", "fixed_version": "2.42.4-1",
                        "repositories": {"sid": "2.42.4-1"}}
        self.tracker = {"util-linux": {self.cve: {"releases": {"sid": self.release}}}}

    def result(self):
        return assessment.assess(self.finding, self.installed, self.tracker)

    def test_epoch_and_source_must_match(self):
        self.assertEqual(self.result()["purl"], self.purl)
        self.finding["PkgIdentifier"]["PURL"] = self.purl.replace("epoch=1", "epoch=2")
        self.assertIsNone(self.result())

    def test_open_or_unrelated_advisories_are_never_assessed(self):
        self.release["status"] = "open"
        self.assertIsNone(self.result())
        self.release["status"] = "resolved"
        self.finding["VulnerabilityID"] = "CVE-fixture"
        self.assertIsNone(self.result())

    def test_changed_or_insufficient_source_versions_require_review(self):
        self.release["fixed_version"] = "2.42.4-2"
        self.assertIsNone(self.result())
        self.release["fixed_version"] = "2.42.3-1"
        self.release["repositories"]["sid"] = "2.42.4-2"
        self.assertIsNone(self.result())
