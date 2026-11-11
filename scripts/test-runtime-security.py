#!/usr/bin/env python3
import hashlib
import json
import pathlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))
import runtime_security as security


class RuntimeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.settings = {"DUNE_IMAGE_TAG": "2173753-0-shipping", "DUNE_GAME_IMAGE": "snape/dune-server:qualified",
                         "DUNE_RUNTIME_SECURITY_APPROVAL_FILE": "approval.json"}
        self.approval = {"schemaVersion": 1, "vendorTag": "2173753-0-shipping", "image": "snape/dune-server:qualified",
                         "imageId": "sha256:" + "a" * 64, "vendorImageId": "sha256:" + "b" * 64,
                         "scan": {"critical": 0, "high": 0, "errors": 0},
                         "gameBinaryAssetsAndInterpreterUnchanged": True}
        self.qualification = {"candidate_image": self.approval["imageId"],
                              "game_binary_assets_and_embedded_interpreter_unchanged": True,
                              "only_python_packaging_files_changed": True, "native_binary_linker_passed": True,
                              "image_runtime_configuration_unchanged": True,
                              "embedded_python_fixture_compatibility_passed": True}
        self.report = {"Metadata": {"ImageID": self.approval["imageId"], "OS": {"Family": "ubuntu"}},
                       "Results": [{"Class": "os-pkgs", "Vulnerabilities": []}]}
        self.bind_evidence()
        self.write()

    def bind_evidence(self):
        for key, name, value in [("qualification", "qualification.json", self.qualification),
                                 ("scanReport", "scan.json", self.report)]:
            data = json.dumps(value).encode()
            (self.root / name).write_bytes(data)
            self.approval[key] = {"path": name, "sha256": hashlib.sha256(data).hexdigest()}

    def write(self):
        (self.root / "approval.json").write_text(json.dumps(self.approval))

    def test_default_vendor_image_needs_no_approval(self):
        self.assertIsNone(security.configuration(self.root, {"DUNE_IMAGE_TAG": "2173753-0-shipping"}))

    def test_current_qualified_image_is_accepted(self):
        result = security.configuration(self.root, self.settings)
        security.verify_loaded_images(result, self.approval["imageId"], self.approval["vendorImageId"])

    def test_saved_image_scan_config_is_bound_to_candidate(self):
        self.approval["schemaVersion"] = 2
        self.approval["scanImageId"] = "sha256:" + "c" * 64
        self.qualification["scan_image_config_id"] = self.approval["scanImageId"]
        self.report["Metadata"]["ImageID"] = self.approval["scanImageId"]
        self.bind_evidence()
        self.write()
        self.assertEqual(security.configuration(self.root, self.settings)["imageId"], self.approval["imageId"])
        self.qualification["scan_image_config_id"] = "sha256:" + "d" * 64
        self.bind_evidence()
        self.write()
        with self.assertRaisesRegex(ValueError, "qualification"):
            security.configuration(self.root, self.settings)

    def test_future_game_version_is_rejected_before_update(self):
        with self.assertRaises(ValueError):
            security.configuration(self.root, self.settings, "2180000-0-shipping")

    def test_unpatched_vendor_pin_cannot_hide_future_game_version(self):
        settings = {"DUNE_IMAGE_TAG": "2173753-0-shipping",
                    "DUNE_GAME_IMAGE": "registry.funcom.com/funcom/self-hosting/seabass-server:2173753-0-shipping"}
        self.assertIsNone(security.configuration(self.root, settings))
        with self.assertRaises(ValueError):
            security.configuration(self.root, settings, "2180000-0-shipping")

    def test_changed_image_id_is_rejected(self):
        with self.assertRaises(ValueError):
            security.verify_loaded_images(self.approval, "sha256:" + "c" * 64, self.approval["vendorImageId"])

    def test_changed_evidence_is_rejected(self):
        (self.root / "scan.json").write_text("{}")
        with self.assertRaises(ValueError):
            security.configuration(self.root, self.settings)

    def test_scan_for_another_image_or_with_findings_is_rejected(self):
        for report in [
            {"Metadata": {"ImageID": "sha256:" + "c" * 64, "OS": {"Family": "ubuntu"}}, "Results": [{}]},
            {"Metadata": self.report["Metadata"], "Results": [{"Class": "os-pkgs", "Vulnerabilities": [{"Severity": "HIGH"}]}]},
        ]:
            self.report = report
            self.bind_evidence()
            self.write()
            with self.assertRaises(ValueError):
                security.configuration(self.root, self.settings)

    def test_findings_or_missing_payload_qualification_are_rejected(self):
        for key in ("critical", "high", "errors"):
            with self.subTest(key=key):
                self.approval["scan"][key] = 1
                self.write()
                with self.assertRaises(ValueError):
                    security.configuration(self.root, self.settings)
                self.approval["scan"][key] = 0
        self.approval["gameBinaryAssetsAndInterpreterUnchanged"] = False
        self.write()
        with self.assertRaises(ValueError):
            security.configuration(self.root, self.settings)

    def verifier(self, override=True, actual_id=None, control=False, control_pin=True,
                 control_vendor_tag=None):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        (self.root / "admin").mkdir(exist_ok=True)
        for name in ("runtime-security.py", "verify-running-image-tag.sh"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        shutil.copyfile(ROOT / "admin/runtime_security.py", self.root / "admin/runtime_security.py")
        (scripts / "check-steam-update.sh").write_text("#!/bin/sh\nexit 0\n")
        (scripts / "check-steam-update.sh").chmod(0o700)
        settings = dict(self.settings)
        if not override:
            settings.pop("DUNE_GAME_IMAGE")
        service = "gateway" if control == "gateway" else "director"
        prefix = "DUNE_GATEWAY" if service == "gateway" else "DUNE_DIRECTOR"
        control_image = f"snape/dune-{service if service == 'gateway' else 'bg-director'}:cve-candidate-20261110"
        if control:
            settings[f"{prefix}_IMAGE"] = control_image
            if control_pin:
                settings[f"{prefix}_IMAGE_ID"] = self.approval["imageId"]
                settings[f"{prefix}_VENDOR_TAG"] = control_vendor_tag or settings["DUNE_IMAGE_TAG"]
        env_file = self.root / ".env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
        docker = self.root / "docker"
        docker.write_text("#!/usr/bin/env python3\nimport sys\na=sys.argv[1:]\n"
                          + "if a[:2] == ['image','inspect']:\n"
                          + f" print({self.approval['imageId']!r} if (a[2] if a[2] != '--format' else a[-1]).startswith('snape/') else {self.approval['vendorImageId']!r})\n"
                          + f"elif a[0] == 'ps': print('fixture\\t{service if control else 'map-fixture'}')\n"
                          + "elif a[0] == 'inspect':\n"
                          + f" print({(control_image if control else self.settings['DUNE_GAME_IMAGE'])!r} if a[2] == '{{{{.Config.Image}}}}' else {(actual_id or self.approval['imageId'])!r})\n"
                          + "else: sys.exit(3)\n")
        docker.chmod(0o700)
        env = {k: v for k, v in os.environ.items() if k not in security.KEYS}
        env["PATH"] = str(self.root) + os.pathsep + env["PATH"]
        return subprocess.run(["bash", str(scripts / "verify-running-image-tag.sh"), str(env_file), "--versions-only"],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_running_verifier_accepts_qualified_image(self):
        result = self.verifier()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_running_verifier_rejects_actual_digest_drift(self):
        result = self.verifier(actual_id="sha256:" + "c" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime security image mismatch", result.stderr)

    def test_running_verifier_rejects_disabled_approval_for_local_image(self):
        result = self.verifier(override=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unapproved runtime security image", result.stderr)

    def test_control_plane_overlay_requires_exact_pin_and_vendor_build(self):
        self.assertEqual(self.verifier(control=True).returncode, 0)
        for options, expected in [
            ({"control": True, "control_pin": False}, "invalid director security image pin"),
            ({"control": True, "control_vendor_tag": "older-build"}, "invalid director security image pin"),
            ({"control": True, "actual_id": "sha256:" + "c" * 64}, "control-plane security image mismatch"),
        ]:
            with self.subTest(options=options):
                result = self.verifier(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_gateway_overlay_requires_exact_pin_and_vendor_build(self):
        self.assertEqual(self.verifier(control="gateway").returncode, 0)
        for options, expected in [
            ({"control": "gateway", "control_pin": False}, "invalid gateway security image pin"),
            ({"control": "gateway", "control_vendor_tag": "older-build"}, "invalid gateway security image pin"),
            ({"control": "gateway", "actual_id": "sha256:" + "c" * 64}, "control-plane security image mismatch"),
        ]:
            with self.subTest(options=options):
                result = self.verifier(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_approval_path_must_be_confined_and_regular(self):
        (self.root / "alias.json").symlink_to(self.root / "approval.json")
        self.settings["DUNE_RUNTIME_SECURITY_APPROVAL_FILE"] = "alias.json"
        with self.assertRaises(ValueError):
            security.configuration(self.root, self.settings)


if __name__ == "__main__":
    unittest.main()
