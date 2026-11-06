#!/usr/bin/env python3
import importlib.util,pathlib,time,unittest

ROOT=pathlib.Path(__file__).resolve().parents[1];SPEC=importlib.util.spec_from_file_location("command_console",ROOT/"admin"/"command_console.py");MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)

class CommandConsoleTests(unittest.TestCase):
    def test_catalog_is_named_native_and_argument_free(self):
        rows=MODULE.catalog();self.assertEqual(set(MODULE.COMMANDS),{row["id"] for row in rows});self.assertTrue(all(row["available"] and not row["acceptsArguments"] and row["backend"]=="native-read-only" for row in rows))
    def test_executor_receives_only_exact_id(self):
        calls=[]
        result=MODULE.run("inventory-audit",lambda command_id:(calls.append(command_id) or {"clean":True}),environment={})
        self.assertEqual(calls,["inventory-audit"]);self.assertTrue(result["ok"]);self.assertFalse(result["shell"]);self.assertFalse(result["subprocess"]);self.assertIn('"clean": true',result["output"])
    def test_native_diagnostic_failure_is_not_reported_as_success(self):
        result=MODULE.run("rmq-health",lambda _: {"ok":False,"probes":[]},environment={})
        self.assertFalse(result["ok"]);self.assertEqual(1,result["returncode"]);self.assertFalse(result["timedOut"])
    def test_unknown_command_fails_closed(self):
        with self.assertRaises(ValueError): MODULE.run("status; id",lambda value:value)
    def test_output_redaction_and_cap(self):
        secret="correct-horse-battery-staple";text=f"token={secret} https://user:pass@example.test Authorization: Bearer abc\n"+("x"*(MODULE.MAX_OUTPUT_BYTES+100));value=MODULE.redact(text,{"DUNE_ADMIN_TOKEN":secret})
        self.assertNotIn(secret,value);self.assertNotIn(":pass@",value);self.assertNotIn("Bearer abc",value);self.assertIn("output truncated",value)
    def test_timeout_is_bounded_result(self):
        old=MODULE.COMMANDS["stack-status"]["timeout"];MODULE.COMMANDS["stack-status"]["timeout"]=0.01
        try: result=MODULE.run("stack-status",lambda _:time.sleep(.05),environment={})
        finally: MODULE.COMMANDS["stack-status"]["timeout"]=old
        self.assertFalse(result["ok"]);self.assertTrue(result["timedOut"]);self.assertEqual(result["returncode"],124)
    def test_exception_becomes_redacted_failure(self):
        result=MODULE.run("rmq-health",lambda _:(_ for _ in ()).throw(RuntimeError("token top-secret")),environment={"API_TOKEN":"top-secret"})
        self.assertFalse(result["ok"]);self.assertNotIn("top-secret",result["output"])

if __name__=="__main__":unittest.main()
