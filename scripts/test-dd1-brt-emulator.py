#!/usr/bin/env python3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dd1-brt-emulator.py"
DOC = ROOT / "docs" / "brt-deep-desert-plan.md"
MAKEFILE = ROOT / "Makefile"


class Dd1BrtEmulatorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.doc = DOC.read_text(encoding="utf-8")

    def test_live_commit_requires_rpc_classification_without_client_requirement(self):
        self.assertIn("RPC_CLASSIFICATION_CHOICES", self.source)
        self.assertIn("normal-request-not-observed", self.source)
        self.assertIn("operator-controlled-fallback", self.source)
        self.assertIn("require_rpc_classification(args)", self.source)
        self.assertIn("SERVER-RPC-ENTRY/SERVER-RPC-EXEC", self.source)
        self.assertIn('"clientModificationRequired": False', self.source)
        self.assertIn("First classify whether", self.source)
        self.assertIn("the normal request reaches the server", self.source)
        self.assertIn("spoof/emulate", self.source)
        self.assertIn("equivalent request server-side", self.source)
        self.assertIn("TRACE_CLASSIFICATION_SCHEMA_VERSION", self.source)
        self.assertIn("rpc_classification_from_file", self.source)
        self.assertIn("--rpc-classification-json", self.source)
        self.assertIn('"rpcClassificationJson"', self.source)
        self.assertIn("classification requires client modification", self.source)
        self.assertIn("normal-request-reached-server", self.source)
        self.assertIn("run_create_backup", self.source)
        self.assertIn("CREATE DD1 BRT BACKUP", self.source)
        self.assertIn("base_backup_save_from_totem", self.source)
        self.assertIn("created_backup_summary", self.source)

    def test_runbook_documents_classification_for_server_side_emulation(self):
        self.assertIn("--rpc-classification normal-request-not-observed", self.doc)
        self.assertIn("Neither branch requires client-side file changes", self.doc)
        self.assertIn("SERVER-RPC-ENTRY", self.doc)
        self.assertIn("SERVER-RPC-EXEC", self.doc)

    def test_brt_tooling_regressions_are_in_standard_validation(self):
        source = MAKEFILE.read_text(encoding="utf-8")
        validate_line = next(line for line in source.splitlines() if line.startswith("validate:"))
        self.assertIn("test-brt-dd-tooling", validate_line)
        self.assertIn("scripts/test-dd1-brt-emulator.py", source)
        self.assertIn("scripts/test-brt-dd-trace.py", source)


if __name__ == "__main__":
    unittest.main()
