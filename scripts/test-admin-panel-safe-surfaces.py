#!/usr/bin/env python3
import importlib.util
import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import pathlib
import sys
import tempfile
import tarfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


try:
    import psycopg2  # noqa: F401
except ModuleNotFoundError:
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    psycopg2_extras_stub.RealDictCursor = object
    psycopg2_stub.extras = psycopg2_extras_stub
    psycopg2_stub.connect = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database access is not available in unit tests"))
    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = psycopg2_extras_stub


def load_admin_panel(workspace):
    os.environ["ADMIN_WORKSPACE"] = str(workspace)
    spec = importlib.util.spec_from_file_location("admin_panel_under_test", ROOT / "admin" / "admin_panel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdminPanelSafeSurfacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.tmp.name)
        (self.workspace / "config").mkdir(parents=True)
        (self.workspace / "scripts").mkdir(parents=True)
        (self.workspace / "config" / "UserEngine.ini").write_text(
            "\n".join([
                "[ConsoleVariables]",
                "Dune.GlobalMiningOutputMultiplier=1.0",
                "Dune.GlobalVehicleMiningOutputMultiplier=1.0",
                "SecurityZones.PvpResourceMultiplier=2.5",
                "Sandstorm.Enabled=1",
                "Sandstorm.Treasure.Enabled=1",
                "",
            ]),
            encoding="utf-8",
        )
        (self.workspace / "config" / "UserGame.ini").write_text(
            "\n".join([
                "[/Script/DuneSandbox.PvpPveSettings]",
                "m_bShouldForceEnablePvpOnAllPartitions=False",
                "",
                "[/Script/DuneSandbox.SecurityZonesSubsystem]",
                "m_bAreSecurityZonesEnabled=True",
                "",
                "[/Script/DuneSandbox.SandStormConfig]",
                "m_bCoriolisAutoSpawnEnabled=False",
                "",
            ]),
            encoding="utf-8",
        )
        (self.workspace / "config" / "UserServerCustomSettings.ini").write_text(
            "\n".join([
                "[/Script/DuneSandbox.UserServerCustomSettings]",
                "PVPMode=Limited",
                "GatheringAmount=1.000000",
                "bAllowSandstorms=True",
                "bAllowSandworms=True",
                "bIsBuildingRestrictionsEnabled=False",
                "BaseBackupToolTimeRestriction=0.016667",
                "LandsraadSpecializationXpMultiplier=3.000000",
                "",
            ]),
            encoding="utf-8",
        )
        (self.workspace / "config" / "director.ini").write_text("[ Battlegroup ]\n", encoding="utf-8")
        (self.workspace / "config" / "gateway.ini").write_text("", encoding="utf-8")
        (self.workspace / "config" / "rabbitmq-admin.conf").write_text("", encoding="utf-8")
        (self.workspace / "config" / "rabbitmq-game.conf").write_text("", encoding="utf-8")
        (self.workspace / "config" / "maintenance-planner.json").write_text(
            json.dumps({
                "schemaVersion": 1, "timezone": "America/Regina", "lookbackDays": 28,
                "retentionDays": 90, "horizonDays": 7, "bucketSeconds": 300,
                "slotMinutes": 30, "durationMinutes": 30, "eligibleLocalStart": "02:00",
                "eligibleLocalEnd": "09:00", "defaultLocalTime": "06:00",
                "minimumNoticeMinutes": 30, "minimumWindowCoverage": 0.8,
                "minimumSampleDays": 2, "weekdayWeightingMinimumDays": 2,
                "recommendationCount": 8,
            }),
            encoding="utf-8",
        )
        (self.workspace / "research" / "surfaces").mkdir(parents=True)
        (self.workspace / "research" / "surfaces" / "test.jsonl").write_text(
            json.dumps({
                "id": "ini.test.surface",
                "build": "test-build",
                "surface": "ini",
                "scope": "global",
                "name": "Test surface",
                "status": "validated",
                "confidence": "high",
                "risk": "low",
                "validated": True,
                "evidence": ["unit test"],
            }) + "\n" + json.dumps({
                "id": "binary.test.candidate",
                "build": "test-build",
                "surface": "binary-candidate",
                "scope": "global",
                "name": "Test binary candidate",
                "status": "candidate",
                "confidence": "low",
                "risk": "medium",
                "validated": False,
                "evidence": ["unit test"],
            }) + "\n",
            encoding="utf-8",
        )
        (self.workspace / ".env").write_text("", encoding="utf-8")
        self.panel = load_admin_panel(self.workspace)
        # Existing route tests isolate their route-specific gate. Focused tests
        # below exercise the additional exact-body change-contract admission.
        self.panel.CHANGE_CONTRACTS_REQUIRED = False
        self.handler = object.__new__(self.panel.Handler)

    def tearDown(self):
        self.tmp.cleanup()

    def patch_db(self, query_fn=None, execute_fn=None):
        original_query = self.panel.query
        original_execute = self.panel.execute
        self.panel.query = query_fn or (lambda sql, params=None: [])
        self.panel.execute = execute_fn or (lambda sql, params=None: None)
        self.addCleanup(lambda: setattr(self.panel, "query", original_query))
        self.addCleanup(lambda: setattr(self.panel, "execute", original_execute))

    def test_secret_environment_file_takes_precedence_and_fails_closed(self):
        secret_file = self.workspace / "admin-token.secret"
        secret_file.write_text("file-token\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {
            "DUNE_ADMIN_TOKEN": "inline-token",
            "DUNE_ADMIN_TOKEN_FILE": str(secret_file),
        }):
            self.assertEqual(self.panel.secret_env("DUNE_ADMIN_TOKEN"), "file-token")
        with mock.patch.dict(os.environ, {
            "DUNE_ADMIN_TOKEN": "inline-token",
            "DUNE_ADMIN_TOKEN_FILE": str(self.workspace / "missing-token.secret"),
        }):
            self.assertEqual(self.panel.secret_env("DUNE_ADMIN_TOKEN"), "")

    def test_db_connect_reads_password_file(self):
        secret_file = self.workspace / "database-password.secret"
        secret_file.write_text("file-db-password\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DUNE_ADMIN_DB_PASSWORD_FILE": str(secret_file)}):
            with mock.patch.object(self.panel.psycopg2, "connect", return_value=object()) as connect:
                self.panel.db_connect()
        self.assertEqual(connect.call_args.kwargs["password"], "file-db-password")

    def patch_flag(self, name, value):
        original = getattr(self.panel, name)
        setattr(self.panel, name, value)
        self.addCleanup(lambda: setattr(self.panel, name, original))

    def patch_connect(self, connect_fn):
        original = self.panel.db_connect
        self.panel.db_connect = connect_fn
        self.addCleanup(lambda: setattr(self.panel, "db_connect", original))

    def make_route_handler(self, path):
        captured = {"json": None, "errors": [], "audits": []}
        handler = object.__new__(self.panel.Handler)
        handler.path = path
        handler.validate_host = lambda: None
        handler.validate_same_origin = lambda: None
        handler.require_token = lambda: None
        handler.json = lambda value, head_only=False, status=None: captured.__setitem__("json", value)

        def fake_error(status, message, head_only=False):
            captured["errors"].append({"status": status, "message": str(message)})

        def fake_audit(action, ok=True, **fields):
            captured["audits"].append(dict(action=action, ok=ok, **fields))

        handler.error = fake_error
        handler.audit = fake_audit
        return handler, captured

    def invoke_post_route(self, path, body):
        handler, captured = self.make_route_handler(path)
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.validate_json_post = lambda request_handler, **kwargs: None
        self.panel.parse_body = lambda request_handler, **kwargs: body
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        handler.do_POST()
        return captured

    def test_healthz_is_minimal_and_does_not_require_admin_token(self):
        handler, captured = self.make_route_handler("/healthz")
        handler.is_app_route = lambda path: False
        handler.require_token = lambda: self.fail("healthz must remain usable by the container healthcheck")
        handler.do_GET()
        self.assertEqual({"ok": True, "service": "dune-admin-panel"}, captured["json"])

    def test_detailed_status_requires_admin_token(self):
        handler, captured = self.make_route_handler("/api/status")
        handler.is_app_route = lambda path: False
        handler.require_token = lambda: (_ for _ in ()).throw(PermissionError("admin token required"))
        handler.do_GET()
        self.assertEqual(401, captured["errors"][0]["status"])
        self.assertIsNone(captured["json"])

    def test_api_head_requires_admin_token(self):
        handler, captured = self.make_route_handler("/api/status")
        handler.is_app_route = lambda path: False
        handler.require_token = lambda: (_ for _ in ()).throw(PermissionError("admin token required"))
        handler.do_HEAD()
        self.assertEqual(401, captured["errors"][0]["status"])
        self.assertIsNone(captured["json"])

    def test_server_banner_does_not_disclose_python_runtime(self):
        self.assertEqual("DASH", self.panel.Handler.server_version)
        self.assertEqual("", self.panel.Handler.sys_version)

    def test_admin_token_auth_enforces_required_default_and_allows_explicit_unlock(self):
        handler = object.__new__(self.panel.Handler)
        handler.path = "/api/status"
        handler.command = "GET"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        handler.audit = lambda *args, **kwargs: None
        self.patch_flag("ADMIN_REQUIRE_TOKEN", True)
        self.patch_flag("ADMIN_TOKEN", "real-owner-token")
        self.patch_flag("RBAC_ENABLED", False)
        self.patch_flag("FEDERATED_AUTH_ENABLED", False)
        with self.assertRaisesRegex(PermissionError, "invalid admin token"):
            handler.require_token()
        handler.headers = {"X-Admin-Token": "real-owner-token"}
        bucket = hashlib.sha256(b"bad").hexdigest()[:16]
        self.panel.AUTH_FAILURES[("127.0.0.1", bucket)] = [time.time()] * self.panel.AUTH_FAILURE_LIMIT
        handler.require_token()
        self.assertEqual(handler.auth_principal["id"], "owner-recovery")
        self.assertIn(("127.0.0.1", bucket), self.panel.AUTH_FAILURES)
        self.panel.ADMIN_REQUIRE_TOKEN = False
        handler.headers = {}
        handler.require_token()

    def test_invalid_token_audit_is_bounded_per_peer_and_credential(self):
        handler = object.__new__(self.panel.Handler)
        handler.path = "/api/status"
        handler.command = "GET"
        handler.client_address = ("192.0.2.10", 12345)
        handler.headers = {}
        events = []
        handler.audit = lambda action, **kwargs: events.append(action)
        self.patch_flag("ADMIN_REQUIRE_TOKEN", True)
        self.patch_flag("ADMIN_TOKEN", "real-owner-token")
        self.patch_flag("RBAC_ENABLED", False)
        self.patch_flag("FEDERATED_AUTH_ENABLED", False)
        self.panel.AUTH_FAILURES.clear()
        self.panel.AUTH_FAILURE_AUDIT.clear()
        for _ in range(3):
            with self.assertRaises(PermissionError):
                handler.require_token()
        handler.headers = {"X-Admin-Token": "real-owner-token"}
        handler.require_token()
        handler.headers = {}
        with self.assertRaises(PermissionError):
            handler.require_token()
        self.assertEqual(["auth-failed"], events)

    def test_dual_control_blocks_without_approval_and_consumes_before_dispatch(self):
        body = {"action": "add-intel", "account_id": 7, "amount": 10, "dry_run": False, "confirm": "WRITE PLAYER PROGRESSION"}
        consumed = []

        class FakeStore:
            def consume(self, *args):
                consumed.append(args)
                return {"id": args[0], "bodyHmacSha256": "a" * 64}

        original_store = self.panel.change_approval_store
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.change_approval_store = lambda: FakeStore()
        self.panel.validate_json_post = lambda handler, **kwargs: None
        self.panel.parse_body = lambda handler, **kwargs: body
        self.addCleanup(lambda: setattr(self.panel, "change_approval_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        self.patch_flag("DUAL_CONTROL_ENABLED", True)
        self.patch_flag("DUAL_CONTROL_POLICY", "high")
        self.patch_flag("RBAC_ENABLED", True)

        blocked, blocked_capture = self.make_route_handler("/api/admin/player-maintenance")
        blocked.headers = {}
        blocked.require_token = lambda: setattr(blocked, "auth_principal", {"id": "requester", "capabilities": ["players.write"]})
        blocked.player_maintenance_mutation = lambda *_args, **_kwargs: self.fail("dispatch must not run without approval")
        blocked.do_POST()
        self.assertIn("dual-control approval required", blocked_capture["errors"][0]["message"])

        handler, captured = self.make_route_handler("/api/admin/player-maintenance")
        handler.headers = {"X-DASH-Approval-ID": "approval-20260716T000000Z-0123456789abcdef"}
        handler.require_token = lambda: setattr(handler, "auth_principal", {"id": "requester", "displayName": "Requester", "capabilities": ["players.write"]})
        handler.player_maintenance_mutation = lambda request, principal=None: {"ok": True, "dryRun": False, "plan": {"action": request["action"], "accountId": 7}}
        handler.do_POST()
        self.assertTrue(captured["json"]["ok"])
        self.assertEqual(1, len(consumed))
        self.assertEqual("requester", consumed[0][1]["id"])
        self.assertEqual("/api/admin/player-maintenance", consumed[0][2])
        self.assertTrue(any(row["action"] == "change-approval-consume" for row in captured["audits"]))

    def test_dual_control_dashboard_and_metrics_contract_are_exposed(self):
        self.assertIn("Four-eyes Change Control", self.panel.INDEX)
        self.assertIn("X-DASH-Approval-ID", self.panel.INDEX)
        self.assertIn("approvalDrafts", self.panel.INDEX)
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashChangeApprovalLedgerInvalid", rules)
        self.assertIn("dash_change_approval_ledger_valid", rules)

    def test_mutation_flight_recorder_admits_before_dispatch_and_correlates_completion(self):
        body = {"action": "add-intel", "account_id": 7, "amount": 10, "confirm": "WRITE PLAYER PROGRESSION"}
        timeline = []

        def fake_audit_event(action, ok=True, _ledger_required=False, **fields):
            timeline.append({"action": action, "ok": ok, "required": _ledger_required, **fields})
            return {"event": {"eventId": f"audit-{len(timeline):032x}"}, "ledger": {"sequence": len(timeline)}}

        original_audit_event = self.panel.audit_event
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.audit_event = fake_audit_event
        self.panel.validate_json_post = lambda handler, **kwargs: None
        self.panel.parse_body = lambda handler, **kwargs: body
        self.addCleanup(lambda: setattr(self.panel, "audit_event", original_audit_event))
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        self.patch_flag("AUDIT_LEDGER_ENABLED", True)
        self.patch_flag("AUDIT_LEDGER_REQUIRED_FOR_MUTATIONS", True)
        self.patch_flag("DUAL_CONTROL_ENABLED", False)

        handler, captured = self.make_route_handler("/api/admin/player-maintenance")
        handler.headers = {}
        handler.require_token = lambda: setattr(handler, "auth_principal", {"id": "operator", "capabilities": ["players.write"]})

        def dispatch(request, principal=None):
            timeline.append({"action": "dispatch"})
            return {"ok": True, "dryRun": False}

        handler.player_maintenance_mutation = dispatch
        handler.do_POST()
        self.assertEqual([], captured["errors"])
        self.assertEqual("privileged-request-admitted", timeline[0]["action"])
        self.assertTrue(timeline[0]["required"])
        self.assertEqual(self.panel.canonical_json_sha256(body), timeline[0]["body_sha256"])
        self.assertEqual("dispatch", timeline[1]["action"])
        self.panel.Handler.complete_privileged_audit(handler, 200)
        self.assertEqual("privileged-request-completed", timeline[2]["action"])
        self.assertEqual(timeline[0]["request_id"], timeline[2]["request_id"])
        self.assertEqual(200, timeline[2]["status_code"])

        def refuse_admission(action, **kwargs):
            if action == "privileged-request-admitted":
                raise RuntimeError("ledger invalid")
            return fake_audit_event(action, **kwargs)

        self.panel.audit_event = refuse_admission
        blocked, blocked_capture = self.make_route_handler("/api/admin/player-maintenance")
        blocked.headers = {}
        blocked.require_token = lambda: setattr(blocked, "auth_principal", {"id": "operator", "capabilities": ["players.write"]})
        blocked.player_maintenance_mutation = lambda *_args, **_kwargs: self.fail("dispatch must not run after admission failure")
        blocked.do_POST()
        self.assertIn("ledger invalid", blocked_capture["errors"][0]["message"])

    def test_mutation_flight_recorder_dashboard_metrics_and_alert_contract(self):
        self.assertIn("Mutation Flight Recorder", self.panel.INDEX)
        self.assertIn("privileged-request-admitted", pathlib.Path(self.panel.__file__).read_text(encoding="utf-8"))
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashAdminAuditLedgerInvalid", rules)
        self.assertIn("DashPrivilegedRequestOutcomeUnknown", rules)
        self.assertIn("dash_admin_audit_ledger_valid", rules)
        self.assertIn("dash_admin_audit_privileged_request_oldest_open_age_seconds", rules)

    def test_change_contract_gate_refuses_missing_or_stale_contract_and_admits_exact_request(self):
        body = {"action": "add-intel", "account_id": 7, "amount": 10, "dry_run": False, "confirm": "WRITE PLAYER PROGRESSION"}
        principal = {"id": "operator", "displayName": "Operator", "capabilities": ["players.write"]}
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        original_audit_event = self.panel.audit_event
        self.panel.validate_json_post = lambda handler, **kwargs: None
        self.panel.parse_body = lambda handler, **kwargs: body
        self.panel.audit_event = lambda action, ok=True, **fields: {"event": {"eventId": "audit-" + "a" * 32}, "ledger": None}
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        self.addCleanup(lambda: setattr(self.panel, "audit_event", original_audit_event))
        self.patch_flag("CHANGE_CONTRACTS_REQUIRED", True)
        self.patch_flag("AUDIT_LEDGER_REQUIRED_FOR_MUTATIONS", False)
        self.patch_flag("DUAL_CONTROL_ENABLED", False)

        blocked, blocked_capture = self.make_route_handler("/api/admin/player-maintenance")
        blocked.headers = {}
        blocked.require_token = lambda: setattr(blocked, "auth_principal", principal)
        blocked.player_maintenance_mutation = lambda *_args, **_kwargs: self.fail("missing contract must block dispatch")
        blocked.do_POST()
        self.assertEqual(self.panel.HTTPStatus.UNAUTHORIZED, blocked_capture["errors"][0]["status"])
        self.assertIn("change contract", blocked_capture["errors"][0]["message"])

        issued = self.panel.change_contracts.issue(
            "/api/admin/player-maintenance", body, principal, "players.write",
            self.panel.CHANGE_CONTRACT_SECRET, ttl_seconds=120,
        )
        handler, captured = self.make_route_handler("/api/admin/player-maintenance")
        handler.headers = {"X-DASH-Change-Contract": issued["token"]}
        handler.require_token = lambda: setattr(handler, "auth_principal", principal)
        dispatched = []
        handler.player_maintenance_mutation = lambda request, principal=None: dispatched.append(request) or {"ok": True, "dryRun": False}
        handler.do_POST()
        self.assertEqual([], captured["errors"])
        self.assertEqual([body], dispatched)
        self.assertEqual(issued["contract"]["contractId"], handler._change_contract["contractId"])

        replay, replay_capture = self.make_route_handler("/api/admin/player-maintenance")
        replay.headers = {"X-DASH-Change-Contract": issued["token"]}
        replay.require_token = lambda: setattr(replay, "auth_principal", principal)
        replay.player_maintenance_mutation = lambda *_args, **_kwargs: self.fail("consumed contract must not dispatch twice")
        replay.do_POST()
        self.assertEqual(self.panel.HTTPStatus.UNAUTHORIZED, replay_capture["errors"][0]["status"])
        self.assertIn("already consumed", replay_capture["errors"][0]["message"])

        changed = {**body, "amount": 11}
        self.panel.parse_body = lambda handler, **kwargs: changed
        stale, stale_capture = self.make_route_handler("/api/admin/player-maintenance")
        stale.headers = {"X-DASH-Change-Contract": issued["token"]}
        stale.require_token = lambda: setattr(stale, "auth_principal", principal)
        stale.player_maintenance_mutation = lambda *_args, **_kwargs: self.fail("stale contract must block dispatch")
        stale.do_POST()
        self.assertEqual(self.panel.HTTPStatus.UNAUTHORIZED, stale_capture["errors"][0]["status"])
        self.assertIn("no longer matches target", stale_capture["errors"][0]["message"])

    def test_change_contract_dashboard_api_and_metrics_contract_are_exposed(self):
        self.assertIn("Blast-Radius Change Contracts", self.panel.INDEX)
        self.assertIn("X-DASH-Change-Contract", self.panel.INDEX)
        self.assertIn("reviewChangeContract", self.panel.INDEX)
        self.assertIn("change review: ${data.changeContracts?.required ? 'enforced' : 'advisory'}", self.panel.INDEX)
        metrics = self.panel.change_contract_prometheus()
        self.assertIn("dash_change_contract_enabled 1\n", metrics)
        self.assertIn("dash_change_contract_required 0\n", metrics)
        self.assertEqual("read", self.panel.access_control.required_capability("POST", "/api/security/change-contract"))
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashChangeContractRefusalBurst", rules)
        self.assertIn("dash_change_contract_refused_total", rules)

    def test_change_contract_preflight_route_issues_exact_operator_token(self):
        target_body = {"action": "add-intel", "account_id": 7, "amount": 10, "dry_run": False, "confirm": "WRITE PLAYER PROGRESSION"}
        outer = {"targetPath": "/api/admin/player-maintenance", "requestBody": target_body}
        principal = {"id": "operator", "displayName": "Operator", "capabilities": ["players.write", "read"]}
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        original_audit_event = self.panel.audit_event
        self.panel.validate_json_post = lambda handler, **kwargs: None
        self.panel.parse_body = lambda handler, **kwargs: outer
        self.panel.audit_event = lambda action, ok=True, **fields: {"event": {"eventId": "audit-" + "b" * 32}, "ledger": None}
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        self.addCleanup(lambda: setattr(self.panel, "audit_event", original_audit_event))

        handler, captured = self.make_route_handler("/api/security/change-contract")
        handler.require_token = lambda: setattr(handler, "auth_principal", principal)
        handler.do_POST()
        self.assertEqual([], captured["errors"])
        self.assertTrue(captured["json"]["contract"]["governed"])
        self.assertEqual("operator", captured["json"]["contract"]["principalId"])
        self.assertEqual("players.write", captured["json"]["contract"]["target"]["capability"])
        self.assertTrue(captured["json"]["token"].startswith("dash-change-v1."))

    def test_change_contract_env_settings_reject_bricking_pairs_ttl_and_injection(self):
        env_identity = (self.panel.ENV_FILE.stat().st_dev, self.panel.ENV_FILE.stat().st_ino)
        with self.assertRaisesRegex(ValueError, "cannot remain required"):
            self.panel.write_safe_env({"DUNE_ADMIN_CHANGE_CONTRACTS_ENABLED": "false"})
        for value in ("29", "301", "not-a-number"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "TTL"):
                self.panel.write_safe_env({"DUNE_ADMIN_CHANGE_CONTRACT_TTL_SECONDS": value})
        with self.assertRaisesRegex(ValueError, "control character"):
            self.panel.write_safe_env({"WORLD_NAME": "safe\nINJECTED=true"})
        self.panel.write_safe_env({
            "DUNE_ADMIN_CHANGE_CONTRACTS_ENABLED": "false",
            "DUNE_ADMIN_CHANGE_CONTRACTS_REQUIRED": "false",
            "DUNE_ADMIN_CHANGE_CONTRACT_TTL_SECONDS": "30",
        })
        values = self.panel.read_env()
        self.assertEqual("false", values["DUNE_ADMIN_CHANGE_CONTRACTS_ENABLED"])
        self.assertEqual("false", values["DUNE_ADMIN_CHANGE_CONTRACTS_REQUIRED"])
        self.assertEqual("30", values["DUNE_ADMIN_CHANGE_CONTRACT_TTL_SECONDS"])
        self.assertEqual(env_identity, (self.panel.ENV_FILE.stat().st_dev, self.panel.ENV_FILE.stat().st_ino))

    def test_federated_public_directory_api_ui_metrics_and_alerts_are_exposed(self):
        self.assertIn("Federated Public Directory", self.panel.INDEX)
        self.assertIn("/api/ops/public-directory", self.panel.INDEX)
        self.assertIn("Ed25519-signed descriptor", self.panel.INDEX)
        for key in (
            "DUNE_PUBLIC_DIRECTORY_ENABLED", "DUNE_PUBLIC_DIRECTORY_ENTRY_URL",
            "DUNE_PUBLIC_SITE_URL", "DUNE_PUBLIC_DIRECTORY_REGION",
            "DUNE_PUBLIC_DIRECTORY_NAME", "DUNE_PUBLIC_DIRECTORY_DESCRIPTION",
            "DUNE_PUBLIC_DIRECTORY_CAPACITY", "DUNE_PUBLIC_DIRECTORY_DISCORD_INVITE",
            "DUNE_PUBLIC_DIRECTORY_TTL_SECONDS",
        ):
            self.assertIn(key, self.panel.ENV_KEY_DEFINITIONS)

        handler, captured = self.make_route_handler("/api/ops/public-directory")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertFalse(captured["json"]["enabled"])
        self.assertEqual("disabled", captured["json"]["state"])

        metrics = self.panel.public_directory_prometheus()
        self.assertIn("dash_public_directory_enabled 0\n", metrics)
        self.assertIn("dash_public_directory_entry_valid 0\n", metrics)
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashPublicDirectoryEntryInvalid", rules)
        self.assertIn("DashPublicDirectoryEntryStale", rules)

    def test_feature_readiness_api_ui_and_label_free_metrics_are_exposed(self):
        self.assertIn("Feature Readiness Control Center", self.panel.INDEX)
        self.assertIn("/api/ops/feature-readiness", self.panel.INDEX)
        self.assertIn("Tamper-evident transition history", self.panel.INDEX)
        self.assertIn("DUNE_FEATURE_READINESS_CACHE_TTL_SECONDS", self.panel.ENV_KEY_DEFINITIONS)
        for key in (
            "DUNE_FEATURE_READINESS_HISTORY_ENABLED", "DUNE_FEATURE_READINESS_HISTORY_DATABASE",
            "DUNE_FEATURE_READINESS_HISTORY_HMAC_SECRET_FILE",
        ):
            self.assertIn(key, self.panel.ENV_KEY_DEFINITIONS)
        fixture = {
            "ok": False,
            "overall": "attention",
            "summary": {
                "ready": 1, "canary-pending": 1, "disabled": 1, "partial": 0,
                "blocked": 1, "degraded": 0, "external-blocked": 0,
                "total": 4, "active": 3, "activeProblems": 1,
            },
            "features": [],
            "secretValuesReturned": False,
        }
        original = dict(self.panel.FEATURE_READINESS_CACHE)
        self.panel.FEATURE_READINESS_CACHE.update({"value": fixture, "updated_at": time.time()})
        self.addCleanup(lambda: self.panel.FEATURE_READINESS_CACHE.update(original))

        handler, captured = self.make_route_handler("/api/ops/feature-readiness")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertEqual("attention", captured["json"]["overall"])
        self.assertFalse(captured["json"]["secretValuesReturned"])

        history_fixture = {"ok": True, "enabled": True, "summary": {"events": 2}, "events": [{"sequence": 2}]}
        with mock.patch.object(self.panel, "feature_readiness_history_public_status", return_value=history_fixture):
            handler, captured = self.make_route_handler("/api/ops/feature-readiness/history?limit=10")
            handler.is_app_route = lambda path: False
            handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertEqual(2, captured["json"]["summary"]["events"])

        metrics = self.panel.feature_readiness_prometheus()
        self.assertIn("dash_feature_readiness_ok 0\n", metrics)
        self.assertIn("dash_feature_readiness_active_problems 1\n", metrics)
        self.assertIn("dash_feature_readiness_history_valid 1\n", metrics)
        self.assertIn("dash_feature_readiness_history_events_total", metrics)
        self.assertNotIn("{", metrics)
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashFeatureReadinessCollectorInvalid", rules)
        self.assertIn("DashFeatureReadinessActiveProblems", rules)
        self.assertIn("DashFeatureReadinessHistoryInvalid", rules)
        self.assertIn("DashFeatureReadinessRegression", rules)

    def test_feature_readiness_cache_ttl_settings_are_bounded(self):
        for value in ("4", "301", "not-a-number"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "feature-readiness cache TTL"):
                self.panel.write_safe_env({"DUNE_FEATURE_READINESS_CACHE_TTL_SECONDS": value})
        self.panel.write_safe_env({"DUNE_FEATURE_READINESS_CACHE_TTL_SECONDS": "5"})
        self.assertEqual("5", self.panel.read_env()["DUNE_FEATURE_READINESS_CACHE_TTL_SECONDS"])

    def test_credential_lifecycle_api_ui_metrics_and_alerts_are_secret_safe(self):
        self.assertIn("Credential Lifecycle Control Center", self.panel.INDEX)
        self.assertIn("/api/ops/credential-lifecycle", self.panel.INDEX)
        for key in (
            "DUNE_CREDENTIAL_LIFECYCLE_ENABLED", "DUNE_CREDENTIAL_LIFECYCLE_DATABASE",
            "DUNE_CREDENTIAL_LIFECYCLE_HMAC_SECRET_FILE", "DUNE_CREDENTIAL_LIFECYCLE_ANCHOR_FILE",
        ):
            self.assertIn(key, self.panel.ENV_KEY_DEFINITIONS)
        fixture = {
            "ok": False,
            "enabled": True,
            "summary": {"total": 2, "configured": 1, "required": 2, "problems": 1, "missing": 1, "insecurePermissions": 0, "backupUncovered": 0, "overdue": 0, "dueSoon": 0},
            "credentials": [{"id": "fixture", "title": "Fixture", "findings": ["missing"]}],
            "history": {"ok": True, "events": 1, "rotations": 0},
            "secretValuesReturned": False,
            "materialFingerprintsReturned": False,
        }
        original = dict(self.panel.CREDENTIAL_LIFECYCLE_CACHE)
        self.panel.CREDENTIAL_LIFECYCLE_CACHE.update({"value": fixture, "updated_at": time.time()})
        self.addCleanup(lambda: self.panel.CREDENTIAL_LIFECYCLE_CACHE.update(original))

        handler, captured = self.make_route_handler("/api/ops/credential-lifecycle")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertFalse(captured["json"]["secretValuesReturned"])
        self.assertFalse(captured["json"]["materialFingerprintsReturned"])

        metrics = self.panel.credential_lifecycle_prometheus()
        self.assertIn("dash_credential_lifecycle_enabled 1\n", metrics)
        self.assertIn("dash_credential_lifecycle_missing 1\n", metrics)
        self.assertNotIn("{", metrics)
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashCredentialLifecycleHistoryInvalid", rules)
        self.assertIn("DashCredentialRequiredMissing", rules)
        self.assertIn("DashCredentialSourcePermissionsUnsafe", rules)
        self.assertIn("DashCredentialBackupCoverageGap", rules)
        self.assertIn("DashCredentialRotationOverdue", rules)

    def test_federated_public_directory_settings_fail_closed_then_accept_complete_contract(self):
        with self.assertRaisesRegex(ValueError, "entry URL"):
            self.panel.write_safe_env({"DUNE_PUBLIC_DIRECTORY_ENABLED": "true"})
        with self.assertRaisesRegex(ValueError, "public DNS hostname"):
            self.panel.write_safe_env({
                "DUNE_PUBLIC_DIRECTORY_ENABLED": "true",
                "DUNE_PUBLIC_DIRECTORY_ENTRY_URL": "https://127.0.0.1/directory-entry.json",
                "DUNE_PUBLIC_SITE_URL": "https://127.0.0.1/",
                "DUNE_PUBLIC_DIRECTORY_REGION": "North America",
                "WORLD_NAME": "Test",
            })
        self.panel.write_safe_env({
            "DUNE_PUBLIC_DIRECTORY_ENABLED": "true",
            "DUNE_PUBLIC_DIRECTORY_ENTRY_URL": "https://dune.example.test/directory-entry.json",
            "DUNE_PUBLIC_SITE_URL": "https://dune.example.test/",
            "DUNE_PUBLIC_DIRECTORY_REGION": "North America",
            "DUNE_PUBLIC_DIRECTORY_NAME": "Test Sietch",
            "DUNE_PUBLIC_DIRECTORY_DESCRIPTION": "A test community.",
            "DUNE_PUBLIC_DIRECTORY_CAPACITY": "40",
            "DUNE_PUBLIC_DIRECTORY_TTL_SECONDS": "180",
        })
        values = self.panel.read_env()
        self.assertEqual("true", values["DUNE_PUBLIC_DIRECTORY_ENABLED"])
        self.assertEqual("https://dune.example.test/directory-entry.json", values["DUNE_PUBLIC_DIRECTORY_ENTRY_URL"])

    def test_page_navigation_cancels_detached_player_detail_loads(self):
        source = self.panel.INDEX
        load_body = source.split("async function load(){", 1)[1].split("async function overview(", 1)[0]
        self.assertIn("detailLoadSerial += 1;", load_body)

    def test_respawn_queries_use_current_character_id_schema(self):
        source = pathlib.Path(self.panel.__file__).read_text(encoding="utf-8")
        self.assertIn("prl.character_id = op.character_id", source)
        self.assertNotIn("prl.account_id = op.account_id", source)

    def test_route_audits_do_not_reuse_reserved_request_path_field(self):
        source = pathlib.Path(self.panel.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"self\.audit\([^\n]*\bpath=")

    def test_admin_http_concurrency_is_bounded(self):
        self.assertGreaterEqual(self.panel.MAX_CONCURRENT_REQUESTS, 4)
        self.assertLessEqual(self.panel.MAX_CONCURRENT_REQUESTS, 128)
        self.assertTrue(self.panel.BoundedThreadingHTTPServer.daemon_threads)
        self.assertLessEqual(self.panel.BoundedThreadingHTTPServer.request_queue_size, 64)

    def test_catalog_schema_has_required_fields(self):
        entries = self.panel.content_catalog_entries()
        self.assertGreaterEqual(len(entries), 5)
        required = {
            "surface",
            "capability",
            "evidence",
            "confidence",
            "mutationRisk",
            "restartRequired",
            "validationCommand",
            "rollback",
        }
        for entry in entries:
            self.assertTrue(required.issubset(entry), entry)
        payload = self.panel.catalog_payload()
        self.assertIn("Deep Desert", payload["groups"])
        self.assertTrue(payload["enabled"])
        by_id = {entry["id"]: entry for entry in entries}
        self.assertIn("faction-reputation-plan", by_id)
        self.assertIn("set_player_faction_reputation", " ".join(by_id["faction-reputation-plan"]["evidence"]))
        self.assertIn("journey-server-functions", by_id)
        self.assertIn("respawn-location-delete", by_id)
        self.assertIn("landsraad-term-admin", by_id)
        self.assertIn("guild-admin-functions", by_id)
        self.assertIn("world-state-function-discovery", by_id)
        self.assertIn("marker-delete-functions", by_id)
        self.assertIn("landclaim-segment-functions", by_id)
        self.assertIn("exchange-solari-balance", by_id)
        self.assertIn("exchange-order-functions", by_id)
        self.assertIn("vehicle-restore-functions", by_id)
        self.assertIn("base-backup-functions", by_id)
        self.assertIn("player-tag-functions", by_id)
        self.assertIn("player-access-code-functions", by_id)
        self.assertIn("communinet-functions", by_id)
        self.assertIn("tutorial-entry-functions", by_id)
        self.assertIn("permission-actor-functions", by_id)
        self.assertIn("vendor-cycle-timestamp-functions", by_id)
        self.assertIn("taxation-landsraad-vendor-functions", by_id)
        self.assertIn("vendor-tutorial-lore-dungeon-overmap-functions", by_id)
        self.assertIn("party-account-lifecycle-functions", by_id)
        self.assertEqual(by_id["recipe-vehicle-function-discovery"]["mutationRisk"], "blocked")

    def test_typed_knob_validation_and_backup_write(self):
        self.assertEqual(self.panel.validate_typed_knob_value("gatheringAmount", "2.5"), "2.5")
        self.assertEqual(self.panel.validate_typed_knob_value("pvpMode", "Limited"), "Limited")
        self.assertEqual(self.panel.validate_typed_knob_value("sandstormsAllowed", "false"), "False")
        self.assertEqual(self.panel.validate_typed_knob_value("characterRecustomizationCost", "0"), "0")
        with self.assertRaises(ValueError):
            self.panel.validate_typed_knob_value("buildingShelterThreshold", "2")
        with self.assertRaises(ValueError):
            self.panel.validate_typed_knob_value("characterRecustomizationCost", "-1")

        result = self.panel.write_typed_knobs({
            "gatheringAmount": "2.5",
            "sandstormsAllowed": "false",
            "pvpMode": "Full",
            "characterRecustomizationCost": "0",
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["restartRequired"])
        custom = (self.workspace / "config" / "UserServerCustomSettings.ini").read_text(encoding="utf-8")
        game = (self.workspace / "config" / "UserGame.ini").read_text(encoding="utf-8")
        self.assertIn("GatheringAmount=2.5", custom)
        self.assertIn("bAllowSandstorms=False", custom)
        self.assertIn("PVPMode=Full", custom)
        self.assertNotIn("Dune.GlobalMiningOutputMultiplier", custom)
        self.assertNotIn("m_bShouldForceEnablePvpOnAllPartitions=True", game)
        self.assertIn("[/Script/DuneSandbox.CharacterRecustomizerSubsystem]", game)
        self.assertIn("m_CostAmount=0", game)
        backups = list((self.workspace / "backups" / "admin-panel").glob("*User*.ini"))
        self.assertGreaterEqual(len(backups), 2)

    def test_spice_caps_render_structured_input(self):
        rendered = self.panel.validate_typed_knob_value(
            "spiceDeepDesertCaps",
            {
                "Medium": {"primed": 24, "active": 24},
                "Large": {"primed": 3, "active": 3},
            },
        )
        self.assertIn('Name="Medium"', rendered)
        self.assertIn("MaxGloballyPrimed=24", rendered)
        self.assertIn('Name="Large"', rendered)
        self.assertIn("MaxGloballyActive=3", rendered)

    def test_catalog_validation_and_gate_metadata(self):
        validation = self.panel.catalog_validation_payload()
        commands = {row["name"]: row["command"] for row in validation["commands"]}
        self.assertIn("Static compile", commands)
        self.assertIn("Repo validation", commands)
        self.assertIn("Spice state", commands)

        evidence = self.panel.catalog_evidence_payload()
        self.assertIn("schema", evidence)
        self.assertIn("rules", evidence)
        self.assertGreaterEqual(len(evidence["entries"]), 20)

        self.patch_flag("CATALOG_ENABLED", False)
        with self.assertRaises(PermissionError):
            self.handler.require_catalog()

    def test_discovery_payload_reads_surface_ledger(self):
        payload = self.panel.discovery_payload()
        self.assertTrue(payload["ok"])
        ids = {entry["id"] for entry in payload["surfaces"]}
        self.assertIn("ini.test.surface", ids)
        self.assertIn("binary.test.candidate", ids)
        self.assertIn("ready-or-promoted", payload["queue"])
        self.assertIn("needs-startup-parse-test", payload["queue"])

    def test_discovery_routes_are_read_only_catalog_gated(self):
        for route in ("/api/discovery", "/api/discovery/surfaces", "/api/discovery/queue", "/api/discovery/builds"):
            handler, captured = self.make_route_handler(route)
            handler.do_GET()
            self.assertEqual([], captured["errors"], route)
            self.assertIsNotNone(captured["json"], route)

    def test_peer_watch_route_and_discovery_ui_expose_non_mutating_drift_contract(self):
        original = self.panel.peer_watch_public_status
        self.panel.peer_watch_public_status = lambda limit=200: {
            "ok": True, "enabled": True, "schemaVersion": "dash-peer-watch/v1",
            "summary": {"total": 32, "current": 31, "drifted": 1, "error": 0, "transitions": 2},
            "peers": [], "history": [], "collector": {"ageSeconds": 10},
        }
        self.addCleanup(lambda: setattr(self.panel, "peer_watch_public_status", original))
        handler, captured = self.make_route_handler("/api/ops/peer-watch")
        handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertEqual(1, captured["json"]["summary"]["drifted"])
        self.assertIn("Ecosystem Peer Watch", self.panel.INDEX)
        self.assertIn("never changes a pin automatically", self.panel.INDEX)
        self.assertIn("/api/ops/peer-watch?refresh=1", self.panel.INDEX)

    def test_read_only_inspectors_expose_safe_mutator_metadata(self):
        def fake_query(sql, params=None):
            if "from dune.player_state" in sql:
                return [{
                    "account_id": 10,
                    "character_name": "Tester",
                    "online_status": "Offline",
                    "player_controller_id": 201,
                    "player_pawn_id": 200,
                }]
            if "from dune.player_faction " in sql:
                return [{"actor_id": 200, "faction_id": 3}]
            if "from dune.player_faction_reputation" in sql:
                return [{"actor_id": 200, "faction_id": 1, "reputation_amount": 50}]
            if "get_guild_for_player" in sql:
                return [{"guild_id": 9}]
            if "get_guild_data" in sql:
                return [{"guild_id": 9, "guild_description": "old"}]
            if "get_guild_members" in sql:
                return [{"guild_id": 9, "player_id": 200, "role_id": 1}]
            if "admin_read_player_tags" in sql:
                return [{"tags": "event"}]
            if "get_player_access_codes" in sql:
                return [{"access_code": 111, "access_code_type": 0}]
            if "load_communinet_player_data" in sql:
                return [{"is_active": True, "selected_channel_name": "general"}]
            if "get_all_tutorial_entries" in sql:
                return [{"tutorial_id": 7, "tutorial_state": 1}]
            if "vendor_stock_cycle" in sql:
                return [{"vendor_id": "ScrapVendor", "player_id": 200}]
            if "dune_exchange_retrieve_solari_balance" in sql:
                return [{"solari_balance": 500}]
            if "dune_exchange_users" in sql:
                return [{"owner_id": 200}]
            if "dune_exchange_orders" in sql:
                return [{"id": 1, "owner_id": 200}]
            if "spicefield_types" in sql:
                return [{"map": "DeepDesert", "field_kind_id": 1, "max_globally_active": 24}]
            if "spicefield_server_availability" in sql:
                return [{"server_id": 1, "field_kind_id": 1}]
            if "resourcefield_state" in sql:
                return [{"map": "DeepDesert", "field_kind_id": 1, "count": 3}]
            if "pg_proc" in sql:
                return [{"schema": "dune", "name": "example_function", "args": "", "result": "void"}]
            if "information_schema.columns" in sql:
                return [{"table_name": "example", "column_name": "id", "data_type": "bigint", "udt_name": "int8"}]
            if "count(*)" in sql:
                return [{"table_name": "example", "rows": 1}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("inspectors must not execute SQL writes")))

        progression = self.handler.progression_inspect({"account_id": 10})
        self.assertEqual(progression["player"]["player_pawn_id"], 200)
        self.assertEqual(progression["mutators"]["journey"]["executionGate"], "DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED")
        self.assertEqual(progression["mutators"]["playerFaction"]["confirm"], "CHANGE FACTION")
        self.assertEqual(progression["mutators"]["journeyRecipeVehicle"]["status"], "inspect-only")

        world = self.handler.world_state_inspect({"account_id": 10})
        self.assertEqual(world["guildId"], 9)
        self.assertEqual(world["mutators"]["marker"]["confirm"], "DELETE MARKERS")
        self.assertEqual(world["mutators"]["vehicleRecipeMarkerLandclaim"]["status"], "inspect-only")

        economy = self.handler.economy_inspect({"account_id": 10})
        self.assertEqual(economy["ownerId"], 200)
        self.assertEqual(economy["exchangeBalance"]["solari_balance"], 500)
        self.assertEqual(economy["mutators"]["exchangeSolari"]["executionGate"], "DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED")

        lifecycle = self.handler.player_lifecycle_inspect({"account_id": 10})
        self.assertEqual(lifecycle["playerId"], 200)
        self.assertEqual(lifecycle["mutators"]["playerTags"]["confirm"], "WRITE PLAYER TAGS")
        self.assertEqual(lifecycle["mutators"]["partyAccountCommuninet"]["status"], "inspect-only")

        spice = self.handler.spice_field_inspect()
        self.assertEqual(spice["caps"][0]["map"], "DeepDesert")
        self.assertIn("typedKnob", spice)

    def test_event_dry_run_is_plan_only(self):
        plan = self.panel.event_dry_run({
            "name": "test",
            "actions": [
                {"type": "spice-cap-proposal", "caps": {"Medium": {"primed": 24, "active": 24}}},
                {"type": "economy-bundle", "payload": {"currency": []}},
            ],
        })
        self.assertTrue(plan["dryRun"])
        actions = plan["event"]["plan"]
        self.assertEqual(actions[0]["type"], "spice-cap-proposal")
        self.assertTrue(actions[0]["dryRunOnly"])
        self.assertEqual(actions[1]["payload"]["dry_run"], True)

    def test_event_persistence_and_cancel(self):
        event = self.panel.create_event({"name": "persisted", "actions": [{"type": "typed-knob-plan", "updates": {}}]})
        state_path = self.workspace / "backups" / "admin-panel" / "events.json"
        self.assertTrue(state_path.exists())
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["events"][0]["id"], event["id"])
        result = self.panel.cancel_event(event["id"])
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.panel.read_event_state()["events"][0]["status"], "cancelled")

    def test_execute_event_fails_closed_by_default(self):
        event = self.panel.create_event({"name": "blocked", "actions": [{"type": "typed-knob-plan", "updates": {}}]})
        with self.assertRaises(PermissionError):
            self.panel.execute_event(event["id"])

    def test_recurring_event_executes_safe_primitives_and_records_runs(self):
        self.patch_flag("EVENT_EXECUTION_ENABLED", True)
        announcement_calls = []
        restart_calls = []
        original_announcement = self.panel.schedule_announcement
        original_restart = self.panel.schedule_restart
        self.panel.schedule_announcement = lambda payload: announcement_calls.append(payload) or {"id": "announce-1"}
        self.panel.schedule_restart = lambda payload: restart_calls.append(payload) or {"id": "restart-1"}
        self.addCleanup(lambda: setattr(self.panel, "schedule_announcement", original_announcement))
        self.addCleanup(lambda: setattr(self.panel, "schedule_restart", original_restart))
        event = self.panel.create_event({
            "name": "recurring operations",
            "runAt": "2020-01-01T00:00:00Z",
            "repeatSeconds": 60,
            "maxRuns": 2,
            "actions": [
                {"type": "announcement", "message": "test notice"},
                {"type": "restart", "target": "deep-desert"},
                {"type": "typed-knob-plan", "updates": {}},
            ],
        })
        self.assertEqual(self.panel.due_event_ids(now=1_700_000_000), [event["id"]])
        first = self.panel.execute_event(event["id"], trigger="schedule")
        self.assertTrue(first["ok"])
        self.assertTrue(first["nextRunAt"])
        self.assertEqual(announcement_calls[0]["message"], "test notice")
        self.assertFalse(restart_calls[0]["execute"])
        state = self.panel.read_event_state()
        self.assertEqual(state["events"][0]["status"], "scheduled")
        self.assertEqual(state["events"][0]["runCount"], 1)
        self.assertEqual(state["runs"][0]["trigger"], "schedule")
        second = self.panel.execute_event(event["id"], trigger="schedule")
        self.assertTrue(second["ok"])
        state = self.panel.read_event_state()
        self.assertEqual(state["events"][0]["status"], "executed")
        self.assertEqual(state["events"][0]["runCount"], 2)
        self.assertEqual(len(state["runs"]), 2)

    def test_scheduled_map_prewarm_uses_guarded_autoscaler_demand(self):
        self.patch_flag("EVENT_EXECUTION_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("AUTOSCALER_MUTATIONS_ENABLED", True)
        original_read = self.panel.read_autoscaler_state
        original_control = self.panel.autoscaler_control
        calls = []
        self.panel.read_autoscaler_state = lambda: {"enabled": True, "modes": {"arrakeen": "dynamic"}}
        self.panel.autoscaler_control = lambda action, service=None, demand_source=None, **kwargs: calls.append((action, service, demand_source)) or {"lastError": ""}
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_read))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_control", original_control))
        event = self.panel.create_event({
            "name": "arrakeen warm",
            "runAt": "2026-07-18T01:00:00Z",
            "actions": [{"type": "map-prewarm", "service": "arrakeen"}],
        })
        self.assertEqual(self.panel.scheduled_map_prewarms()[0]["services"], ["arrakeen"])

        result = self.panel.execute_event(event["id"], trigger="schedule")

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("demand", "arrakeen", "scheduled-prewarm")])
        self.assertEqual(result["executed"][0]["type"], "map-prewarm")
        schedules = self.panel.scheduled_map_prewarms()
        self.assertEqual(schedules, [])
        metrics = self.panel.map_prewarm_prometheus()
        self.assertIn("dash_capacity_prewarm_runs_total 1", metrics)
        self.assertIn("dash_capacity_prewarm_failures_total 0", metrics)

    def test_scheduled_map_prewarm_refuses_disabled_map(self):
        self.patch_flag("EVENT_EXECUTION_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("AUTOSCALER_MUTATIONS_ENABLED", True)
        original_read = self.panel.read_autoscaler_state
        self.panel.read_autoscaler_state = lambda: {"enabled": True, "modes": {"arrakeen": "disabled"}}
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_read))
        event = self.panel.create_event({"actions": [{"type": "map-prewarm", "service": "arrakeen"}]})

        result = self.panel.execute_event(event["id"])

        self.assertFalse(result["ok"])
        self.assertIn("disabled map", result["failures"][0]["error"])
        self.assertIn("dash_capacity_prewarm_failures_total 1", self.panel.map_prewarm_prometheus())

    def test_event_recurrence_bounds_fail_closed(self):
        with self.assertRaises(ValueError):
            self.panel.event_dry_run({"repeatSeconds": 10, "actions": [{"type": "typed-knob-plan", "updates": {}}]})
        with self.assertRaises(ValueError):
            self.panel.event_dry_run({"runAt": "not-a-date", "actions": [{"type": "typed-knob-plan", "updates": {}}]})

    def test_player_tags_dry_run_and_gate(self):
        calls = []

        def fake_query(sql, params=None):
            calls.append((sql, params))
            if "admin_read_player_tags" in sql:
                return [{"tags": "old_tag"}]
            return []

        def forbidden_execute(sql, params=None):
            raise AssertionError("dry-run or gated path executed SQL")

        self.patch_db(fake_query, forbidden_execute)
        result = self.handler.player_tags_mutation({
            "dry_run": True,
            "account_id": 10,
            "tags_to_add": ["event"],
            "tags_to_remove": ["old_tag"],
        })
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["executionGate"], "DUNE_ADMIN_PLAYER_TAG_MUTATIONS_ENABLED")
        self.assertEqual(result["confirm"], "WRITE PLAYER TAGS")
        self.assertEqual(result["plan"]["rollback"]["tags_to_add"], ["old_tag"])
        self.assertEqual(result["plan"]["rollback"]["tags_to_remove"], ["event"])

        with self.assertRaises(PermissionError):
            self.handler.player_tags_mutation({
                "dry_run": False,
                "account_id": 10,
                "tags_to_add": ["event"],
                "confirm": "WRITE PLAYER TAGS",
            })

    def test_access_code_dry_run_and_gate(self):
        self.patch_db(
            lambda sql, params=None: [{"access_code": 111, "access_code_type": 0}] if "get_player_access_codes" in sql else [],
            lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")),
        )
        result = self.handler.access_code_mutation({
            "dry_run": True,
            "action": "create",
            "account_id": 10,
            "access_code": 222,
            "access_code_type": 0,
        })
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["executionGate"], "DUNE_ADMIN_ACCESS_CODE_MUTATIONS_ENABLED")
        self.assertEqual(result["confirm"], "WRITE ACCESS CODES")
        self.assertEqual(result["plan"]["rollback"]["action"], "delete")

        with self.assertRaises(PermissionError):
            self.handler.access_code_mutation({
                "dry_run": False,
                "action": "delete",
                "account_id": 10,
                "access_code": 111,
                "access_code_type": 0,
                "confirm": "WRITE ACCESS CODES",
            })

    def test_character_slot_discovery_and_plan_shape(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline", "player_controller_id": 100, "player_pawn_id": 200, "fls_id": "fls"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "ownership_evidence": "same-account-user"}]
            if "from pg_proc" in sql:
                return [{"name": "login_account", "args": "", "result": "bigint"}]
            if "information_schema.columns" in sql:
                return [{"table_name": "player_state", "column_name": "account_id"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("dry-run executed SQL")))
        slots = self.handler.character_slots(10)
        self.assertTrue(slots["ok"])
        self.assertEqual(slots["activeCharacter"]["character_name"], "Active")
        self.assertEqual(slots["candidates"][0]["character_name"], "Stored")
        self.assertEqual(slots["executionGate"], "DUNE_ADMIN_CHARACTER_SWAP_ENABLED")

        plan = self.handler.character_slot_plan({"account_id": 10, "action": "new-character"})
        self.assertTrue(plan["dryRun"])
        self.assertFalse(plan["executable"])
        self.assertIn("No validated first-party", " ".join(plan["plan"]["blockers"]))

    def test_character_slot_dry_run_never_executes_sql(self):
        executed = []

        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: executed.append((sql, params)))
        result = self.handler.character_slot_execute({"dry_run": True, "account_id": 10, "action": "new-character"})
        self.assertTrue(result["dryRun"])
        self.assertEqual(executed, [])

    def test_character_slot_online_players_are_refused(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Online"}]
            return []

        self.patch_db(fake_query)
        result = self.handler.character_slot_plan({"account_id": 10, "action": "new-character"})
        self.assertFalse(result["executable"])
        self.assertIn("online", " ".join(result["plan"]["blockers"]))

    def test_character_slot_missing_native_contract_is_not_executable(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query)
        result = self.handler.character_slot_plan({"account_id": 10, "action": "new-character"})
        self.assertFalse(result["executable"])
        self.assertIn("is mapped", " ".join(result["plan"]["blockers"]))

    def test_character_slot_execute_fails_closed_without_gate_and_confirmation(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))
        with self.assertRaises(PermissionError):
            self.handler.character_slot_execute({"dry_run": False, "account_id": 10, "action": "new-character", "confirm": "SWAP CHARACTER"})

        self.patch_flag("CHARACTER_SWAP_ENABLED", True)
        with self.assertRaises(PermissionError):
            self.handler.character_slot_execute({"dry_run": False, "account_id": 10, "action": "new-character"})

    def test_player_identity_get_route_is_read_only_and_returns_gate_state(self):
        handler, captured = self.make_route_handler("/api/admin/player-identity-integrity")
        handler.is_app_route = lambda path: False
        integrity = {"ok": True, "summary": {"healthy": False, "orphan_rows": 2}}
        cleanup = {"ok": True, "canExecute": True, "expectedFingerprint": "f" * 64}
        with mock.patch.object(self.panel.player_identity, "integrity", return_value=integrity), \
             mock.patch.object(self.panel.player_identity, "cleanup_plan", return_value=cleanup):
            handler.do_GET()
        self.assertEqual(2, captured["json"]["summary"]["orphan_rows"])
        self.assertIn("mutationEnabled", captured["json"])

    def test_player_identity_preview_route_does_not_require_mutation_gate(self):
        plan = {"ok": True, "dryRun": True, "canExecute": True, "accountId": 42, "confirm": "DELETE CHARACTER 42"}
        with mock.patch.object(self.panel.player_identity, "character_plan", return_value=plan):
            captured = self.invoke_post_route("/api/admin/player-identity-integrity", {"action": "preview-delete", "accountId": 42})
        self.assertEqual(plan, captured["json"])
        self.assertEqual("preview-delete", captured["audits"][-1]["identity_action"])

    def test_player_identity_live_delete_fails_closed_when_feature_gate_is_off(self):
        self.patch_flag("PLAYER_IDENTITY_MUTATIONS_ENABLED", False)
        self.patch_flag("CHARACTER_DELETE_ENABLED", False)
        captured = self.invoke_post_route("/api/admin/player-identity-integrity", {
            "action": "delete-character", "accountId": 42, "reason": "test",
            "expectedFingerprint": "f" * 64, "confirm": "DELETE CHARACTER 42",
        })
        self.assertEqual(401, captured["errors"][0]["status"])
        self.assertIsNone(captured["json"])

    def test_player_identity_metrics_are_label_free_and_fail_closed(self):
        payload = {"summary": {
            "healthy": False, "account_rows": 4, "player_state_rows": 7,
            "duplicate_accounts": 1, "duplicate_excess_rows": 2, "orphan_rows": 1,
            "missing_pawn_references": 1, "missing_controller_references": 0,
        }}
        with mock.patch.object(self.panel.player_identity, "integrity", return_value=payload):
            metrics = self.panel.player_identity_prometheus()
        self.assertIn("dash_player_identity_orphan_rows 1\n", metrics)
        self.assertIn("dash_player_identity_duplicate_accounts 1\n", metrics)
        self.assertNotIn("{", metrics)
        with mock.patch.object(self.panel.player_identity, "integrity", side_effect=RuntimeError("db down")):
            failed = self.panel.player_identity_prometheus()
        self.assertIn("dash_player_identity_collector_up 0\n", failed)

    def test_item_catalog_is_case_insensitive_deduplicated_and_kind_grouped(self):
        self.panel.ITEM_CATALOG_FILE.write_text(json.dumps({
            "schemaVersion": 1,
            "items": [
                {"templateId": "Rifle_A", "name": "Rifle A", "category": "weapons/ranged"},
                {"templateId": "rifle_a", "name": "Rifle A Rich", "category": "weapons/ranged", "imageUrl": "https://example.test/a.png", "description": "rich"},
                {"templateId": "Rifle_A_Schematic", "name": "Rifle A Schematic", "category": "schematics/weapons", "tier": "3"},
                {"templateId": "Chair_Patent", "name": "Chair", "category": "building/patents"},
            ],
        }), encoding="utf-8")
        catalog = self.panel.load_item_catalog()
        self.assertEqual(3, len(catalog["items"]))
        self.assertEqual(1, catalog["catalog"]["duplicatesDropped"])
        self.assertEqual("Rifle A Rich", self.panel.catalog_item("RIFLE_A")["name"])
        kinds = {row["templateId"]: row["kind"] for row in catalog["items"]}
        self.assertEqual("schematic", kinds["Rifle_A_Schematic"])
        self.assertEqual("patent", kinds["Chair_Patent"])

    def test_character_slot_switch_requires_native_owned_target(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "ownership_evidence": "same-account-user"}]
            return []

        self.patch_db(fake_query)
        with self.assertRaises(ValueError):
            self.handler.character_slot_plan({"account_id": 10, "action": "switch-character"})
        with self.assertRaises(ValueError):
            self.handler.character_slot_plan({"account_id": 10, "action": "switch-character", "target_account_id": 99})

        plan = self.handler.character_slot_plan({"account_id": 10, "action": "switch-character", "target_account_id": 11})
        self.assertEqual(plan["targetAccountId"], 11)
        self.assertEqual(plan["plan"]["targetCharacter"]["character_name"], "Stored")
        self.assertFalse(plan["executable"])

    def test_character_slot_online_target_candidate_blocks_switch(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Online", "ownership_evidence": "same-account-user"}]
            return []

        self.patch_db(fake_query)
        plan = self.handler.character_slot_plan({"account_id": 10, "action": "restore-character", "target_account_id": 11})
        self.assertFalse(plan["executable"])
        self.assertIn("online", " ".join(plan["plan"]["blockers"]))

    def test_character_slot_contract_reports_evidence_but_not_execution(self):
        def fake_query(sql, params=None):
            if "from pg_proc" in sql:
                return [
                    {"name": "login_account", "args": "", "result": "bigint"},
                    {"name": "save_player", "args": "", "result": "void"},
                    {"name": "save_player_pawn", "args": "", "result": "void"},
                ]
            if "information_schema.columns" in sql:
                return [{"table_name": "accounts", "column_name": "id", "data_type": "bigint", "udt_name": "int8"}]
            return []

        self.patch_db(fake_query)
        contract = self.handler.character_slot_contract()
        self.assertEqual(contract["confidence"], "moderate")
        self.assertIn("login_account", contract["observedLifecycleEvidence"])
        self.assertFalse(contract["safeNativeSwapPath"])

    def test_character_slot_takeover_contract_enables_switch_plan(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline", "fls_id": "active-fls"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "fls_id": "stored-fls", "ownership_evidence": "same-account-user"}]
            if "from pg_proc" in sql:
                return [{"name": "takeover_account", "args": "in_user_to_takeover text, in_current_user text", "result": "void"}]
            return []

        self.patch_db(fake_query)
        plan = self.handler.character_slot_plan({"account_id": 10, "action": "switch-character", "target_account_id": 11})
        self.assertTrue(plan["executable"])
        self.assertEqual(plan["plan"]["nativeCall"]["function"], "dune.takeover_account")
        self.assertEqual(plan["plan"]["nativeCall"]["in_user_to_takeover"], "stored-fls")
        self.assertEqual(plan["plan"]["nativeCall"]["in_current_user"], "active-fls")
        self.assertTrue(plan["plan"]["transactionSafety"]["offlineRecheckInsideTransaction"])
        self.assertTrue(plan["plan"]["transactionSafety"]["commitRequiresPostSwapVerification"])

        new_plan = self.handler.character_slot_plan({"account_id": 10, "action": "new-character"})
        self.assertFalse(new_plan["executable"])
        self.assertIn("delete_account", " ".join(new_plan["plan"]["blockers"]))

    def test_character_slot_switch_execute_uses_takeover_with_backup_and_audit_rows(self):
        calls = []
        backups = []
        original_backup = self.panel.create_db_backup
        original_takeover = self.panel.character_swap_takeover

        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline", "fls_id": "active-fls"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "fls_id": "stored-fls", "ownership_evidence": "same-account-user"}]
            if "from pg_proc" in sql:
                return [{"name": "takeover_account", "args": "in_user_to_takeover text, in_current_user text", "result": "void"}]
            return []

        def fake_takeover(active_account_id, target_account_id, active_user, target_user):
            calls.append((active_account_id, target_account_id, active_user, target_user))
            return (
                [{"account_id": active_account_id, "online_status": "Offline", "fls_id": active_user}, {"account_id": target_account_id, "online_status": "Offline", "fls_id": target_user}],
                [{"account_id": active_account_id, "online_status": "Offline", "fls_id": target_user}, {"account_id": target_account_id, "online_status": "Offline", "fls_id": active_user}],
                True,
            )

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected raw execute")))
        self.patch_flag("CHARACTER_SWAP_ENABLED", True)
        self.panel.create_db_backup = lambda: backups.append("backup") or {"path": "backup.dump", "bytes": 1}
        self.panel.character_swap_takeover = fake_takeover
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))
        self.addCleanup(lambda: setattr(self.panel, "character_swap_takeover", original_takeover))

        result = self.handler.character_slot_execute({
            "dry_run": False,
            "account_id": 10,
            "action": "switch-character",
            "target_account_id": 11,
            "confirm": "SWAP CHARACTER",
        })
        self.assertFalse(result["dryRun"])
        self.assertEqual(backups, ["backup"])
        self.assertEqual(calls, [(10, 11, "active-fls", "stored-fls")])
        self.assertTrue(result["verified"])
        self.assertEqual(result["rollback"]["inversePayload"]["account_id"], 11)

    def test_character_slot_execute_rechecks_offline_after_backup(self):
        calls = []
        original_backup = self.panel.create_db_backup
        original_takeover = self.panel.character_swap_takeover

        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline", "fls_id": "active-fls"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "fls_id": "stored-fls", "ownership_evidence": "same-account-user"}]
            if "from pg_proc" in sql:
                return [{"name": "takeover_account", "args": "in_user_to_takeover text, in_current_user text", "result": "void"}]
            return []

        def fake_takeover(active_account_id, target_account_id, active_user, target_user):
            calls.append((active_account_id, target_account_id, active_user, target_user))
            raise RuntimeError("character swap aborted after backup because active or target account came online")

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected raw execute")))
        self.patch_flag("CHARACTER_SWAP_ENABLED", True)
        self.panel.create_db_backup = lambda: {"path": "backup.dump", "bytes": 1}
        self.panel.character_swap_takeover = fake_takeover
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))
        self.addCleanup(lambda: setattr(self.panel, "character_swap_takeover", original_takeover))

        with self.assertRaises(RuntimeError):
            self.handler.character_slot_execute({
                "dry_run": False,
                "account_id": 10,
                "action": "switch-character",
                "target_account_id": 11,
                "confirm": "SWAP CHARACTER",
            })
        self.assertEqual(calls, [(10, 11, "active-fls", "stored-fls")])

    def test_character_swap_takeover_commits_only_after_verified_swap(self):
        events = []

        class FakeCursor:
            description = True

            def __init__(self):
                self.select_count = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                events.append(("execute", " ".join(sql.split()), params))

            def fetchall(self):
                self.select_count += 1
                if self.select_count == 1:
                    return [{"account_id": 10, "online_status": "Offline", "fls_id": "active-fls"}, {"account_id": 11, "online_status": "Offline", "fls_id": "stored-fls"}]
                return [{"account_id": 10, "online_status": "Offline", "fls_id": "stored-fls"}, {"account_id": 11, "online_status": "Offline", "fls_id": "active-fls"}]

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self, cursor_factory=None):
                return self.cursor_obj

            def commit(self):
                events.append(("commit",))

            def rollback(self):
                events.append(("rollback",))

        self.patch_connect(lambda: FakeConn())
        before, after, verified = self.panel.character_swap_takeover(10, 11, "active-fls", "stored-fls")
        self.assertTrue(verified)
        self.assertEqual(before[0]["fls_id"], "active-fls")
        self.assertEqual(after[0]["fls_id"], "stored-fls")
        self.assertIn(("commit",), events)
        self.assertNotIn(("rollback",), events)

    def test_character_swap_takeover_rolls_back_on_failed_verification(self):
        events = []

        class FakeCursor:
            def __init__(self):
                self.select_count = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                events.append(("execute", " ".join(sql.split()), params))

            def fetchall(self):
                self.select_count += 1
                return [{"account_id": 10, "online_status": "Offline", "fls_id": "active-fls"}, {"account_id": 11, "online_status": "Offline", "fls_id": "stored-fls"}]

        class FakeConn:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self, cursor_factory=None):
                return self.cursor_obj

            def commit(self):
                events.append(("commit",))

            def rollback(self):
                events.append(("rollback",))

        self.patch_connect(lambda: FakeConn())
        with self.assertRaises(RuntimeError):
            self.panel.character_swap_takeover(10, 11, "active-fls", "stored-fls")
        self.assertIn(("rollback",), events)
        self.assertNotIn(("commit",), events)

    def test_character_swap_takeover_rolls_back_on_stale_planned_identity(self):
        events = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                events.append(("execute", " ".join(sql.split()), params))

            def fetchall(self):
                return [{"account_id": 10, "online_status": "Offline", "fls_id": "different-fls"}, {"account_id": 11, "online_status": "Offline", "fls_id": "stored-fls"}]

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self, cursor_factory=None):
                return FakeCursor()

            def commit(self):
                events.append(("commit",))

            def rollback(self):
                events.append(("rollback",))

        self.patch_connect(lambda: FakeConn())
        with self.assertRaises(RuntimeError):
            self.panel.character_swap_takeover(10, 11, "active-fls", "stored-fls")
        takeover_calls = [
            event for event in events
            if event[0] == "execute" and "takeover_account" in event[1]
        ]
        self.assertEqual(takeover_calls, [])
        self.assertIn(("rollback",), events)
        self.assertNotIn(("commit",), events)

    def test_character_slot_execution_block_does_not_create_backup(self):
        backups = []
        original_backup = self.panel.create_db_backup

        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))
        self.patch_flag("CHARACTER_SWAP_ENABLED", True)
        self.panel.create_db_backup = lambda: backups.append("backup") or {"path": "backup.dump", "bytes": 1}
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))

        with self.assertRaises(NotImplementedError):
            self.handler.character_slot_execute({"dry_run": False, "account_id": 10, "action": "new-character", "confirm": "SWAP CHARACTER"})
        self.assertEqual(backups, [])

    def test_character_slot_get_route_returns_discovery_payload(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "ownership_evidence": "same-account-user"}]
            return []

        self.patch_db(fake_query)
        handler, captured = self.make_route_handler("/api/admin/character-slots?account_id=10")
        handler.do_GET()
        self.assertFalse(captured["errors"])
        self.assertEqual(captured["json"]["accountId"], 10)
        self.assertEqual(captured["json"]["candidates"][0]["account_id"], 11)

    def test_character_slot_plan_route_audits_preview(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query)
        captured = self.invoke_post_route("/api/admin/character-slots/plan", {
            "dry_run": True,
            "account_id": 10,
            "action": "new-character",
        })
        self.assertFalse(captured["errors"])
        self.assertEqual(captured["json"]["accountId"], 10)
        self.assertEqual(captured["audits"][0]["action"], "character-slot-plan")
        self.assertFalse(captured["audits"][0]["executable"])

    def test_character_slot_execute_route_dry_run_is_audited_preview(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))
        captured = self.invoke_post_route("/api/admin/character-slots/execute", {
            "dry_run": True,
            "account_id": 10,
            "action": "new-character",
        })
        self.assertFalse(captured["errors"])
        self.assertTrue(captured["json"]["dryRun"])
        self.assertEqual(captured["audits"][0]["action"], "character-slot-execute")

    def test_character_slot_execute_route_rejects_blocked_live_attempt(self):
        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))
        captured = self.invoke_post_route("/api/admin/character-slots/execute", {
            "dry_run": False,
            "account_id": 10,
            "action": "new-character",
            "confirm": "SWAP CHARACTER",
        })
        self.assertIsNone(captured["json"])
        self.assertEqual(captured["errors"][0]["status"], self.panel.HTTPStatus.UNAUTHORIZED)
        self.assertIn("DUNE_ADMIN_CHARACTER_SWAP_ENABLED", captured["errors"][0]["message"])
        self.assertEqual(captured["audits"][0]["action"], "post-rejected")

    def test_character_slot_execute_route_returns_success_payload(self):
        original_backup = self.panel.create_db_backup
        original_takeover = self.panel.character_swap_takeover

        def fake_query(sql, params=None):
            if "where ps.account_id=%s" in sql and "left join dune.accounts" in sql:
                return [{"account_id": 10, "character_name": "Active", "online_status": "Offline", "fls_id": "active-fls"}]
            if "with target_account as" in sql:
                return [{"account_id": 11, "character_name": "Stored", "online_status": "Offline", "fls_id": "stored-fls", "ownership_evidence": "same-account-user"}]
            if "from pg_proc" in sql:
                return [{"name": "takeover_account", "args": "in_user_to_takeover text, in_current_user text", "result": "void"}]
            return []

        def fake_takeover(active_account_id, target_account_id, active_user, target_user):
            return (
                [{"account_id": active_account_id, "online_status": "Offline", "fls_id": active_user}, {"account_id": target_account_id, "online_status": "Offline", "fls_id": target_user}],
                [{"account_id": active_account_id, "online_status": "Offline", "fls_id": target_user}, {"account_id": target_account_id, "online_status": "Offline", "fls_id": active_user}],
                True,
            )

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected raw execute")))
        self.patch_flag("CHARACTER_SWAP_ENABLED", True)
        self.panel.create_db_backup = lambda: {"path": "backup.dump", "bytes": 1}
        self.panel.character_swap_takeover = fake_takeover
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))
        self.addCleanup(lambda: setattr(self.panel, "character_swap_takeover", original_takeover))

        captured = self.invoke_post_route("/api/admin/character-slots/execute", {
            "dry_run": False,
            "account_id": 10,
            "action": "switch-character",
            "target_account_id": 11,
            "confirm": "SWAP CHARACTER",
        })
        self.assertFalse(captured["errors"])
        self.assertFalse(captured["json"]["dryRun"])
        self.assertTrue(captured["json"]["verified"])
        self.assertEqual(captured["audits"][0]["action"], "character-slot-execute")
        self.assertTrue(captured["audits"][0]["executable"])

    def test_offline_teleport_preview_is_fingerprint_bound_and_exposes_gate(self):
        original_plan = self.panel.offline_teleport.plan
        original_receipts = self.panel.offline_teleport.list_receipts
        self.panel.offline_teleport.plan = lambda query, account_id, partition_id, location: {
            "ok": True, "dryRun": True, "canExecute": True,
            "accountId": account_id, "partitionId": partition_id,
            "plan": {"function": self.panel.offline_teleport.NATIVE_FUNCTION, "executable": True, "blockers": []},
            "expectedFingerprint": "a" * 64, "confirm": self.panel.offline_teleport.CONFIRM,
            "executionGate": "DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED",
        }
        self.panel.offline_teleport.list_receipts = lambda root, limit=20: []
        self.addCleanup(lambda: setattr(self.panel.offline_teleport, "plan", original_plan))
        self.addCleanup(lambda: setattr(self.panel.offline_teleport, "list_receipts", original_receipts))
        result = self.handler.offline_player_recovery({
            "dry_run": True,
            "account_id": 10,
            "partition_id": 12,
            "location": {"x": 100, "y": 200, "z": 9000},
        })

        self.assertTrue(result["dryRun"])
        self.assertTrue(result["canExecute"])
        self.assertEqual("DUNE_ADMIN_OFFLINE_TELEPORT_ENABLED", result["executionGate"])
        self.assertFalse(result["mutationEnabled"])

    def test_offline_teleport_execute_uses_dedicated_gate_and_audited_route(self):
        calls = []
        original_execute = self.panel.offline_teleport.execute
        self.panel.offline_teleport.execute = lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True, "dryRun": False, "accountId": 10, "partitionId": 12,
            "nativeFunction": self.panel.offline_teleport.NATIVE_FUNCTION,
            "verified": True, "receipt": {"id": "offline-teleport-test"},
        }
        self.addCleanup(lambda: setattr(self.panel.offline_teleport, "execute", original_execute))
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("OFFLINE_TELEPORT_ENABLED", True)
        captured = self.invoke_post_route("/api/admin/player-recovery/offline-teleport", {
            "dry_run": False,
            "account_id": 10,
            "partition_id": 12,
            "location": {"x": 100, "y": 200, "z": 9000},
            "expectedFingerprint": "a" * 64,
            "confirm": self.panel.offline_teleport.CONFIRM,
        })
        self.assertFalse(captured["errors"])
        self.assertTrue(captured["json"]["verified"])
        self.assertEqual(1, len(calls))
        self.assertEqual(10, calls[0][0][3])
        self.assertEqual(12, calls[0][0][4])
        self.assertEqual("offline-player-recovery", captured["audits"][0]["action"])
        self.assertEqual("offline-teleport-test", captured["audits"][0]["receipt_id"])

    def test_offline_teleport_execution_fails_closed_without_feature_gate(self):
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("OFFLINE_TELEPORT_ENABLED", False)
        with self.assertRaisesRegex(PermissionError, "OFFLINE_TELEPORT_ENABLED"):
            self.handler.offline_player_recovery({
                "dryRun": False, "accountId": 10, "partitionId": 12,
                "location": {"x": 1, "y": 2, "z": 3},
            })

    def test_admin_ui_binds_offline_teleport_execution_to_preview(self):
        html = self.panel.INDEX
        self.assertIn("Native Offline Teleport", html)
        self.assertIn("teleportExecuteBtn\" class=\"danger\" disabled", html)
        self.assertIn("expectedFingerprint: dryRun ? '' : (offlineTeleportPlan?.expectedFingerprint", html)

    def test_offline_life_state_recovery_preview_is_read_only_and_exposes_gate(self):
        original_plan = self.panel.player_life_recovery.plan
        original_receipts = self.panel.player_life_recovery.list_receipts
        self.panel.player_life_recovery.plan = lambda query, account_id: {
            "ok": True, "dryRun": True, "canExecute": True, "accountId": account_id,
            "player": {"lifeState": "Dead", "onlineStatus": "Offline"},
            "expectedFingerprint": "a" * 64, "confirm": self.panel.player_life_recovery.CONFIRM,
            "executionGate": "DUNE_ADMIN_PLAYER_LIFE_RECOVERY_ENABLED",
            "nativeFunction": self.panel.player_life_recovery.NATIVE_FUNCTION,
        }
        self.panel.player_life_recovery.list_receipts = lambda root, limit=20: []
        self.addCleanup(lambda: setattr(self.panel.player_life_recovery, "plan", original_plan))
        self.addCleanup(lambda: setattr(self.panel.player_life_recovery, "list_receipts", original_receipts))

        result = self.handler.offline_player_life_recovery({"dryRun": True, "accountId": 42})

        self.assertTrue(result["dryRun"])
        self.assertTrue(result["canExecute"])
        self.assertEqual("DUNE_ADMIN_PLAYER_LIFE_RECOVERY_ENABLED", result["executionGate"])
        self.assertFalse(result["mutationEnabled"])

    def test_offline_life_state_recovery_execution_uses_dedicated_gate_and_audited_route(self):
        original_execute = self.panel.player_life_recovery.execute
        calls = []
        self.panel.player_life_recovery.execute = lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True, "dryRun": False, "accountId": 42,
            "nativeFunction": self.panel.player_life_recovery.NATIVE_FUNCTION,
            "verified": True, "receipt": {"id": "player-life-recovery-test"},
        }
        self.addCleanup(lambda: setattr(self.panel.player_life_recovery, "execute", original_execute))
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("PLAYER_LIFE_RECOVERY_ENABLED", True)

        captured = self.invoke_post_route("/api/admin/player-recovery/life-state", {
            "dryRun": False, "accountId": 42, "expectedFingerprint": "a" * 64,
            "confirm": self.panel.player_life_recovery.CONFIRM,
        })

        self.assertFalse(captured["errors"])
        self.assertTrue(captured["json"]["verified"])
        self.assertEqual(1, len(calls))
        self.assertEqual(42, calls[0][0][3])
        self.assertEqual("offline-player-life-recovery", captured["audits"][0]["action"])
        self.assertEqual("player-life-recovery-test", captured["audits"][0]["receipt_id"])

    def test_offline_life_state_recovery_execution_fails_closed_without_feature_gate(self):
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("PLAYER_LIFE_RECOVERY_ENABLED", False)
        with self.assertRaisesRegex(PermissionError, "PLAYER_LIFE_RECOVERY_ENABLED"):
            self.handler.offline_player_life_recovery({"dryRun": False, "accountId": 42})

    def test_admin_ui_contains_native_offline_life_state_recovery_controls(self):
        html = self.panel.INDEX
        self.assertIn("Native Offline Life-State Recovery", html)
        self.assertIn("lifeRecoveryPreviewBtn", html)
        self.assertIn("/api/admin/player-recovery/life-state", html)
        self.assertIn("expectedFingerprint:lifeRecoveryPlan.expectedFingerprint", html)

    def test_character_backup_preview_exposes_fingerprint_and_gate(self):
        original = self.panel.character_backups.plan_capture
        self.panel.character_backups.plan_capture = lambda query, account_id: {
            "ok": True, "dryRun": True, "action": "capture", "canExecute": True,
            "accountId": int(account_id), "expectedFingerprint": "b" * 64,
            "confirm": self.panel.character_backups.CAPTURE_CONFIRM,
            "executionGate": "DUNE_ADMIN_CHARACTER_BACKUPS_ENABLED",
        }
        self.addCleanup(lambda: setattr(self.panel.character_backups, "plan_capture", original))
        captured = self.invoke_post_route("/api/admin/character-backups/preview", {"action": "capture", "accountId": 42})
        self.assertFalse(captured["errors"])
        self.assertTrue(captured["json"]["canExecute"])
        self.assertFalse(captured["json"]["mutationEnabled"])
        self.assertEqual("b" * 64, captured["json"]["expectedFingerprint"])
        self.assertEqual("character-backup-preview", captured["audits"][0]["action"])

    def test_character_backup_capture_uses_dedicated_gate_and_audited_route(self):
        original = self.panel.character_backups.capture
        calls = []
        self.panel.character_backups.capture = lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True, "dryRun": False,
            "snapshot": {"id": "character-test", "accountIdAtCapture": 42},
            "nativeFunction": self.panel.character_backups.CAPTURE_FUNCTION,
        }
        self.addCleanup(lambda: setattr(self.panel.character_backups, "capture", original))
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("CHARACTER_BACKUPS_ENABLED", True)
        captured = self.invoke_post_route("/api/admin/character-backups", {
            "action": "capture", "accountId": 42, "reason": "test",
            "expectedFingerprint": "b" * 64, "confirm": self.panel.character_backups.CAPTURE_CONFIRM,
        })
        self.assertFalse(captured["errors"])
        self.assertEqual(1, len(calls))
        self.assertEqual("character-test", captured["json"]["snapshot"]["id"])
        self.assertEqual("character-backup", captured["audits"][0]["action"])
        self.assertEqual("character-test", captured["audits"][0]["snapshot_id"])

    def test_character_backup_execution_fails_closed_without_gate(self):
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("CHARACTER_BACKUPS_ENABLED", False)
        with self.assertRaisesRegex(PermissionError, "CHARACTER_BACKUPS_ENABLED"):
            self.handler.character_backup_action({"action": "capture", "accountId": 42})

    def test_admin_ui_contains_native_character_backup_controls(self):
        html = self.panel.INDEX
        self.assertIn("Native Character Backups", html)
        self.assertIn("characterBackupCaptureBtn\" class=\"danger\" disabled", html)
        self.assertIn("/api/admin/character-backups/preview", html)
        self.assertIn("expectedFingerprint:characterBackupPlan.expectedFingerprint", html)

    def test_communinet_tutorial_vendor_dry_runs_are_plan_only(self):
        def fake_query(sql, params=None):
            if "load_communinet_player_data" in sql:
                return [{"is_active": True, "selected_channel_name": "general", "channel_name": "general", "is_tuned": True}]
            if "get_all_tutorial_entries" in sql:
                return [{"tutorial_id": 7, "tutorial_state": 1}]
            if "tutorials" in sql:
                return [{"id": 7, "name": "Intro"}]
            if "vendor_stock_cycle" in sql:
                return [{"vendor_id": "ScrapVendor", "player_id": 17, "last_interacted_timestamp": 100}]
            if "interact_get_vendor_items_bought_from_player" in sql:
                return [{"out_template_id": "Item", "out_amount_bought": 1}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))

        communinet = self.handler.communinet_mutation({
            "dry_run": True,
            "action": "update-channel",
            "account_id": 10,
            "channel_name": "general",
            "is_tuned": "false",
        })
        self.assertEqual(communinet["executionGate"], "DUNE_ADMIN_COMMUNINET_MUTATIONS_ENABLED")
        self.assertEqual(communinet["confirm"], "WRITE COMMUNINET")

        tutorial = self.handler.tutorial_mutation({
            "dry_run": True,
            "player_id": 20,
            "tutorial_id": 7,
            "tutorial_state": 2,
        })
        self.assertEqual(tutorial["executionGate"], "DUNE_ADMIN_TUTORIAL_MUTATIONS_ENABLED")
        self.assertEqual(tutorial["confirm"], "WRITE TUTORIAL")
        self.assertEqual(tutorial["plan"]["rollback"]["tutorial_state"], 1)

        vendor = self.handler.vendor_mutation({
            "dry_run": True,
            "vendor_id": "ScrapVendor",
            "player_id": 17,
            "timestamp": 200,
        })
        self.assertEqual(vendor["executionGate"], "DUNE_ADMIN_VENDOR_MUTATIONS_ENABLED")
        self.assertEqual(vendor["confirm"], "WRITE VENDOR")
        self.assertEqual(vendor["plan"]["rollback"]["timestamp"], 100)

    def test_permission_exchange_guild_dry_runs_and_gates(self):
        def fake_query(sql, params=None):
            if "permission_actor_rank" in sql:
                return [{"permission_actor_id": 100, "player_id": 20, "rank": 1}]
            if "permission_actor" in sql:
                return [{"actor_id": 100, "actor_name": "Base", "access_level": 1}]
            if "dune_exchange_retrieve_solari_balance" in sql:
                return [{"solari_balance": 500}]
            if "guilds" in sql:
                return [{"guild_id": 9, "guild_description": "old"}]
            if "guild_members" in sql:
                return [{"guild_id": 9, "player_id": 20, "role_id": 1}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))

        permission = self.handler.permission_mutation({
            "dry_run": True,
            "action": "set-player-rank",
            "actor_id": 100,
            "player_id": 20,
            "rank": 2,
            "map_id": "HaggaBasin",
        })
        self.assertEqual(permission["executionGate"], "DUNE_ADMIN_PERMISSION_MUTATIONS_ENABLED")
        self.assertEqual(permission["confirm"], "WRITE PERMISSION")
        self.assertEqual(permission["plan"]["rollback"]["rank"], 1)

        exchange = self.handler.exchange_mutation({
            "dry_run": True,
            "owner_id": 20,
            "controller_id": 21,
            "amount": 700,
            "mode": "set",
        })
        self.assertEqual(exchange["executionGate"], "DUNE_ADMIN_EXCHANGE_MUTATIONS_ENABLED")
        self.assertEqual(exchange["confirm"], "WRITE EXCHANGE")
        self.assertEqual(exchange["plan"]["delta"], 200)
        self.assertEqual(exchange["plan"]["targetBalance"], 700)

        guild = self.handler.guild_mutation({
            "dry_run": True,
            "action": "edit-description",
            "guild_id": 9,
            "description": "new",
        })
        self.assertEqual(guild["executionGate"], "DUNE_ADMIN_GUILD_MUTATIONS_ENABLED")
        self.assertEqual(guild["confirm"], "WRITE GUILD")
        self.assertEqual(guild["plan"]["rollback"]["description"], "old")

        with self.assertRaises(PermissionError):
            self.handler.permission_mutation({
                "dry_run": False,
                "action": "set-access-level",
                "actor_id": 100,
                "access_level": 2,
                "confirm": "WRITE PERMISSION",
            })

    def test_solari_inventory_grant_dry_run_creates_solaris_coin_stack(self):
        def fake_query(sql, params=None):
            if "from dune.inventories inv" in sql:
                return [{"inventory_id": 14, "account_id": 10, "online_status": "Offline", "max_item_count": 35}]
            if "generate_series" in sql:
                return [{"position_index": 4}]
            if "known_templates" in sql:
                return [{"exists": 1}]
            raise AssertionError(f"unexpected query: {sql}")

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("dry-run executed SQL")))
        result = self.handler.grant_player_inventory_solari({
            "dry_run": True,
            "player_controller_id": 21,
            "inventory_id": 14,
            "amount": 125,
        })
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["location"], "inventory")
        self.assertEqual(result["plan"]["templateId"], "SolarisCoin")
        self.assertEqual(result["plan"]["itemId"], None)
        self.assertEqual(result["plan"]["beforeStack"], 0)
        self.assertEqual(result["plan"]["afterStack"], 125)
        self.assertEqual(result["plan"]["positionIndex"], 4)
        self.assertEqual(result["confirm"], "GRANT SOLARI")

    def test_solari_inventory_grant_execute_requires_confirmation(self):
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_db(
            lambda sql, params=None: [{"inventory_id": 14, "account_id": 10, "online_status": "Offline", "max_item_count": 35}] if "from dune.inventories inv" in sql else ([{"position_index": 4}] if "generate_series" in sql else []),
            lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")),
        )
        with self.assertRaises(PermissionError):
            self.handler.grant_player_inventory_solari({
                "dry_run": False,
                "player_controller_id": 21,
                "inventory_id": 14,
                "amount": 125,
            })

    def test_solari_inventory_grant_execute_creates_stack(self):
        self.patch_flag("MUTATIONS_ENABLED", True)
        executed = []

        def fake_query(sql, params=None):
            if "from dune.inventories inv" in sql:
                return [{"inventory_id": 14, "account_id": 10, "online_status": "Offline", "max_item_count": 35}]
            if "generate_series" in sql:
                return [{"position_index": 4}]
            if "known_templates" in sql:
                return [{"exists": 1}]
            if "advance_items_id_sequencer" in sql:
                return [{"item_id": 100}]
            if "where inventory_id=%s and position_index=%s" in sql:
                return []
            if "from dune.items where id=%s" in sql:
                return [{"id": 100, "inventory_id": 14, "stack_size": 125, "position_index": 4, "template_id": "SolarisCoin"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: executed.append((" ".join(sql.split()), params)))
        result = self.handler.grant_player_inventory_solari({
            "dry_run": False,
            "player_controller_id": 21,
            "inventory_id": 14,
            "amount": 125,
            "confirm": "GRANT SOLARI",
        })
        self.assertFalse(result["dryRun"])
        self.assertEqual(executed[0][1][0], 100)
        self.assertEqual(executed[0][1][2], 125)
        self.assertEqual(result["after"][0]["stack_size"], 125)

    def test_solari_bank_grant_sets_exchange_balance_directly(self):
        def fake_query(sql, params=None):
            if "player_state" in sql:
                return [{"account_id": 10, "character_name": "Paul", "player_controller_id": 21, "player_pawn_id": 20}]
            if "dune_exchange_retrieve_solari_balance" in sql:
                return [{"solari_balance": 500}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("dry-run executed SQL")))
        result = self.handler.grant_player_bank_solari({
            "dry_run": True,
            "account_id": 10,
            "amount": 125,
        })
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["location"], "bank")
        self.assertEqual(result["ownerId"], 21)
        self.assertEqual(result["controllerId"], 21)
        self.assertEqual(result["plan"]["function"], "direct-update:dune.player_virtual_currency_balances")
        self.assertEqual(result["plan"]["delta"], 125)
        self.assertEqual(result["plan"]["targetBalance"], 625)

    def test_faction_reputation_and_faction_dry_runs(self):
        self.handler.resolve_player_identity = lambda account_id: ({
            "account_id": account_id,
            "character_name": "Tester",
            "online_status": "Offline",
            "player_pawn_id": 200,
        }, "fls-test")

        def fake_query(sql, params=None):
            if "from dune.player_state" in sql:
                return [{"account_id": 10, "character_name": "Tester", "online_status": "Offline", "player_pawn_id": 200}]
            if "information_schema.columns" in sql:
                return [{"column_name": "actor_id"}, {"column_name": "faction_id"}, {"column_name": "reputation_amount"}]
            if "set_player_faction_reputation" in sql:
                return [{"name": "get_player_current_faction_reputation"}, {"name": "set_player_faction_reputation"}]
            if "player_faction_reputation" in sql:
                return [{"actor_id": 200, "faction_id": 1, "reputation_amount": 50}]
            if "select id, name from dune.factions" in sql:
                return [{"id": 1, "name": "Atreides"}, {"id": 2, "name": "Harkonnen"}, {"id": 3, "name": "None"}]
            if "change_player_faction" in sql:
                return [{"name": "change_player_faction"}, {"name": "get_player_faction"}]
            if "player_faction where actor_id" in sql:
                return [{"actor_id": 200, "faction_id": 3}]
            if "get_player_faction" in sql:
                return [{"faction_id": 3}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))

        rep = self.handler.faction_reputation_mutation({
            "dry_run": True,
            "account_id": 10,
            "faction_id": 1,
            "amount": 25,
            "mode": "add",
        })
        self.assertEqual(rep["executionGate"], "DUNE_ADMIN_REPUTATION_MUTATIONS_ENABLED")
        self.assertEqual(rep["plan"]["currentValue"], 50)
        self.assertEqual(rep["plan"]["newValue"], 75)

        faction = self.handler.faction_change_mutation({
            "dry_run": True,
            "account_id": 10,
            "faction_id": 1,
            "neutral_faction_id": 3,
        })
        self.assertEqual(faction["executionGate"], "DUNE_ADMIN_FACTION_MUTATIONS_ENABLED")
        self.assertEqual(faction["confirm"], "CHANGE FACTION")
        self.assertEqual(faction["plan"]["currentFactionId"], 3)

        with self.assertRaises(PermissionError):
            self.handler.faction_change_mutation({
                "dry_run": False,
                "account_id": 10,
                "faction_id": 1,
                "neutral_faction_id": 3,
                "confirm": "CHANGE FACTION",
            })

    def test_journey_respawn_landsraad_dry_runs(self):
        self.handler.resolve_player_identity = lambda account_id: ({
            "character_id": 110,
            "account_id": account_id,
            "character_name": "Tester",
            "online_status": "Offline",
            "player_pawn_id": 200,
        }, "fls-test")

        def fake_query(sql, params=None):
            if "admin_get_journey_details" in sql:
                return [{"story_node_id": params[1], "state": "unknown"}]
            if params and params[0] == "complete_journey_story_nodes_for_player":
                return [{"name": "complete_journey_story_nodes_for_player"}]
            if "player_respawn_locations" in sql:
                return [{
                    "id": "0a0556f6-a387-41f2-b613-deacee4e2bd0",
                    "character_id": 110,
                    "map": "HaggaBasin",
                    "last_used_timestamp": 100,
                }]
            if "landsraad_load_current_term" in sql:
                return [{"term_id": 1, "end_time": "2026-05-26 04:55:00", "testterm": False}]
            if "landsraad_decree_term" in sql:
                return [{"term_id": 1, "end_time": "2026-05-26 04:55:00", "testterm": False}]
            if "landsraad_change_term_end_time" in sql or "landsraad_force_end_term" in sql:
                return [{"name": "landsraad_change_term_end_time"}, {"name": "landsraad_force_end_term"}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))

        journey = self.handler.journey_mutation({
            "dry_run": True,
            "account_id": 10,
            "action": "complete",
            "story_node_ids": ["StoryA"],
        })
        self.assertEqual(journey["executionGate"], "DUNE_ADMIN_JOURNEY_MUTATIONS_ENABLED")
        self.assertEqual(journey["confirm"], "WRITE JOURNEY")
        self.assertEqual(journey["plan"]["function"], "dune.complete_journey_story_nodes_for_player")

        respawn = self.handler.respawn_location_mutation({
            "dry_run": True,
            "account_id": 10,
            "respawn_id": "0a0556f6-a387-41f2-b613-deacee4e2bd0",
        })
        self.assertEqual(respawn["executionGate"], "DUNE_ADMIN_RESPAWN_MUTATIONS_ENABLED")
        self.assertEqual(respawn["confirm"], "DELETE RESPAWN")
        self.assertEqual(respawn["plan"]["remainingCount"], 0)

        landsraad = self.handler.landsraad_mutation({
            "dry_run": True,
            "action": "change-end-time",
            "term_id": 1,
            "new_end_time": "2026-05-27 04:55:00",
        })
        self.assertEqual(landsraad["executionGate"], "DUNE_ADMIN_LANDSRAAD_MUTATIONS_ENABLED")
        self.assertEqual(landsraad["confirm"], "WRITE LANDSRAAD")
        self.assertEqual(landsraad["plan"]["rollback"]["new_end_time"], "2026-05-26 04:55:00")

    def test_marker_landclaim_dry_runs_and_gates(self):
        def fake_query(sql, params=None):
            if "from dune.markers" in sql:
                return [{"marker_hash_id": 123, "dimension_index": -1, "map_name": "HaggaBasin"}]
            if "get_landclaim_segments" in sql:
                return [{"grid_location_x": 1, "grid_location_y": 2}]
            return []

        self.patch_db(fake_query, lambda sql, params=None: (_ for _ in ()).throw(AssertionError("unexpected execute")))

        marker = self.handler.marker_mutation({
            "dry_run": True,
            "action": "delete-by-id",
            "marker_ids": [123],
        })
        self.assertEqual(marker["executionGate"], "DUNE_ADMIN_MARKER_MUTATIONS_ENABLED")
        self.assertEqual(marker["confirm"], "DELETE MARKERS")
        self.assertEqual(marker["markerCount"], 1)

        landclaim = self.handler.landclaim_mutation({
            "dry_run": True,
            "action": "add-segment",
            "totem_id": 100,
            "grid_location_x": 3,
            "grid_location_y": 4,
        })
        self.assertEqual(landclaim["executionGate"], "DUNE_ADMIN_LANDCLAIM_MUTATIONS_ENABLED")
        self.assertEqual(landclaim["confirm"], "WRITE LANDCLAIM")

        with self.assertRaises(PermissionError):
            self.handler.marker_mutation({
                "dry_run": False,
                "action": "delete-by-id",
                "marker_ids": [123],
                "confirm": "DELETE MARKERS",
            })

    def test_restart_start_sigpipe_is_success_when_farm_recovers(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        self.panel.run_restart_command = lambda command, job, phase: {
            "ok": phase != "start",
            "phase": phase,
            "returncode": 141 if phase == "start" else 0,
            "output": phase,
        }
        self.panel.wait_for_restart_online = lambda: {
            "ok": True,
            "expected": 30,
            "online": 30,
            "readyOnline": 30,
            "alive": 30,
            "active": 30,
        }

        result = self.panel.execute_restart({
            "id": "sigpipe",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": False,
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["returncode"], 141)
        self.assertIn("141", result["warning"])

    def test_restart_can_skip_soft_disconnect_for_daily_maintenance(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.soft_disconnect_online_players = lambda job: (_ for _ in ()).throw(AssertionError("soft disconnect should be skipped"))
        phases = []
        self.panel.run_restart_command = lambda command, job, phase: phases.append(phase) or {
            "ok": True,
            "phase": phase,
            "returncode": 0,
            "output": phase,
        }
        self.panel.wait_for_restart_online = lambda: {
            "ok": True,
            "expected": 30,
            "online": 30,
            "readyOnline": 30,
            "alive": 30,
            "active": 30,
        }

        result = self.panel.execute_restart({
            "id": "daily",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": False,
            "requireSoftDisconnect": False,
        })

        self.assertTrue(result["ok"])
        self.assertEqual(phases, ["stop", "update", "start"])
        self.assertEqual(result["disconnect"]["skipped"], "soft disconnect not required for this maintenance job")

    def test_immediate_restart_with_announcement_gets_future_timestamp(self):
        captured = {}
        self.panel.schedule_announcement = lambda body: captured.update(body) or {"ok": True}
        job = self.panel.schedule_restart({
            "target": "all",
            "action": "restart",
            "delay": "immediate",
            "message": "maintenance now",
            "announce": True,
            "execute": False,
        })

        self.assertGreater(job["runAt"], job["createdAt"])
        self.assertEqual(captured["restart_at"], job["runAt"])
        self.assertEqual("certified", job["updatePolicy"])

    def test_restart_update_policy_is_target_safe_and_cannot_bypass_receipts(self):
        targeted = self.panel.schedule_restart({
            "target": "survival", "action": "restart", "delay": "immediate",
            "announce": False, "execute": False,
        })
        self.assertEqual("current", targeted["updatePolicy"])
        with self.assertRaisesRegex(ValueError, "targeted restarts"):
            self.panel.schedule_restart({
                "target": "survival", "action": "restart", "delay": "immediate",
                "announce": False, "execute": False, "update_policy": "certified",
            })
        self.panel.UPDATE_READINESS_REQUIRE_RECEIPT = True
        with self.assertRaisesRegex(ValueError, "RECEIPT"):
            self.panel.schedule_restart({
                "target": "all", "action": "restart", "delay": "immediate",
                "announce": False, "execute": False, "update_policy": "automatic",
            })

    def test_restart_accepts_exact_bounded_time_and_delays_first_warning(self):
        now = time.time()
        run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 7200))
        captured = {}
        original = self.panel.schedule_announcement
        self.panel.schedule_announcement = lambda body: captured.update(body) or {"id": "notice"}
        self.addCleanup(lambda: setattr(self.panel, "schedule_announcement", original))

        job = self.panel.schedule_restart({
            "target": "all", "action": "restart", "runAt": run_at,
            "message": "planned maintenance", "announce": True, "execute": False,
        })

        self.assertEqual("custom", job["delay"])
        self.assertAlmostEqual(now + 7200, job["runAt"], delta=2)
        self.assertAlmostEqual(job["runAt"] - 1800, captured["next_send_at"], delta=1)
        self.assertEqual(2, len(captured["cadence"]))
        with self.assertRaisesRegex(ValueError, "within 30 days"):
            self.panel.schedule_restart({
                "target": "all", "action": "restart", "runAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 31 * 86400)),
                "announce": False, "execute": False,
            })

    def test_certified_restart_revalidates_and_safely_falls_back_or_binds_staged_only_mode(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        candidate = {
            "imageTag": "dune_sb_1_4_11_0", "currentImageTag": "dune_sb_1_4_10_0",
            "status": "update-available", "updateRequired": True, "fingerprint": "f" * 64,
        }
        self.panel.update_readiness_snapshot = lambda force=False: {"candidate": candidate}
        self.panel.update_readiness_store = lambda: types.SimpleNamespace(status=lambda snapshot: {
            "applyReady": False, "currentReceiptReady": False,
            "evaluation": {"candidate": candidate, "failedChecks": ["backupVerified"]},
        })
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        observed = []
        self.panel.run_restart_command = lambda command, job, phase: observed.append(
            (phase, job.get("_checkSteamUpdate"), job.get("_steamUpdateMode"))
        ) or {"ok": True, "phase": phase, "returncode": 0, "output": phase}
        self.panel.wait_for_restart_online = lambda: {"ok": True}
        fallback = self.panel.execute_restart({
            "id": "uncertified-candidate", "execute": True, "action": "restart", "target": "all",
            "services": [], "backup": False, "updatePolicy": "certified",
        })
        self.assertTrue(fallback["ok"])
        self.assertEqual("current", fallback["updatePreflight"]["effectivePolicy"])
        self.assertTrue(fallback["updatePreflight"]["candidateUpdateBlocked"])
        self.assertEqual(["backupVerified"], fallback["updatePreflight"]["failedChecks"])
        self.assertEqual([("stop", False, "none"), ("update", False, "none"), ("start", False, "none")], observed)

        self.panel.update_readiness_store = lambda: types.SimpleNamespace(status=lambda snapshot: {
            "applyReady": True, "currentReceiptReady": True,
            "evaluation": {"candidate": candidate, "failedChecks": []},
            "latest": {"id": "update-readiness-" + "a" * 32, "receiptSha256": "b" * 64},
        })
        observed.clear()
        backup_dir = self.workspace / "backups" / "admin-panel" / "maintenance" / "certified"
        backup_dir.mkdir(parents=True)
        self.panel.create_maintenance_backup = lambda job: {"path": str(backup_dir), "output": "backup"}
        self.panel.verify_backup_set = lambda path: {"ok": True, "path": path, "exitCode": 0, "stdout": "OK", "stderr": ""}
        self.panel.run_restart_command = lambda command, job, phase: observed.append(
            (phase, job.get("_checkSteamUpdate"), job.get("_steamUpdateMode"))
        ) or {"ok": True, "phase": phase, "returncode": 0, "output": phase}
        self.panel.wait_for_restart_online = lambda: {"ok": True}
        allowed = self.panel.execute_restart({
            "id": "allowed-certified", "execute": True, "action": "restart", "target": "all",
            "services": [], "backup": True, "updatePolicy": "certified",
        })
        self.assertTrue(allowed["ok"])
        self.assertEqual([("stop", True, "none"), ("update", True, "none"), ("start", True, "none")], observed)
        self.assertEqual("certified", allowed["updatePreflight"]["effectivePolicy"])

    def test_restart_command_exports_the_job_bound_update_contract(self):
        command = self.workspace / "scripts" / "restart-env.sh"
        command.write_text(
            '#!/bin/sh\nprintf "%s %s\\n" "$DUNE_RESTART_CHECK_STEAM_UPDATE" "$DUNE_RESTART_STEAM_UPDATE_MODE"\n',
            encoding="utf-8",
        )
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        result = self.panel.run_restart_command(command, {
            "id": "bound-env", "target": "all", "services": [], "action": "restart",
            "_checkSteamUpdate": True, "_steamUpdateMode": "none",
        }, "update")
        self.assertTrue(result["ok"])
        self.assertEqual("true none", result["output"])

    def test_partition_31_adds_deep_desert_pvp_to_restart_targets(self):
        workspace = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(workspace, ignore_errors=True))
        workspace.joinpath(".env").write_text("DUNE_WORLD_PARTITION_COUNT=31\n", encoding="utf-8")
        panel = load_admin_panel(workspace)

        self.assertIn("deep-desert-pvp", panel.GAME_MAP_SERVICES)
        self.assertIn("deep-desert-pvp", panel.RESTART_TARGETS["all"]["services"])

    def test_compose_map_catalog_cache_drives_admin_map_and_partition_rosters(self):
        workspace = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(workspace, ignore_errors=True))
        catalog_dir = workspace / "backups" / "admin-panel"
        catalog_dir.mkdir(parents=True)
        maps = [
            {"service": "survival", "partition_id": 1, "map": "Survival_1", "dimension_index": 0, "active": True},
            {"service": "new-quest-map", "partition_id": 47, "map": "NewQuestMap", "dimension_index": 0, "active": True},
        ]
        (catalog_dir / "map-catalog.json").write_text(json.dumps({
            "schema_version": 1,
            "maps": maps,
            "active_services": ["survival", "new-quest-map"],
            "active_partition_ids": [1, 47],
            "active_profiles": [],
        }), encoding="utf-8")

        panel = load_admin_panel(workspace)

        self.assertEqual(["survival", "new-quest-map"], panel.GAME_MAP_SERVICES)
        self.assertEqual({"survival": 1, "new-quest-map": 47}, panel.GAME_MAP_PARTITIONS)
        self.assertIn("new-quest-map", panel.RESTART_TARGETS)
        self.assertEqual("new-quest-map", panel.GAME_MAP_SERVICE_BY_PARTITION[47])
        panel.query = lambda sql, params=None: [{"partition_id": 47, "players": 3}]
        self.assertEqual({"new-quest-map": 3}, panel.autoscaler_player_counts())

    def test_restart_readiness_uses_autoscaler_always_on_maps(self):
        captured = {}
        self.panel.read_autoscaler_state = lambda: {
            "enabled": True,
            "modes": {
                service: ("always-on" if service in ("survival", "overmap") else "dynamic")
                for service in self.panel.GAME_MAP_SERVICES
            },
        }

        def fake_query(sql, params=None):
            captured["params"] = params
            return [{"expected": 2, "online": 2, "ready_online": 2, "alive": 2, "active": 2}]

        self.panel.query = fake_query
        result = self.panel.restart_online_snapshot()

        self.assertTrue(result["ok"])
        self.assertEqual(result["verificationMode"], "autoscaler-always-on")
        self.assertEqual(result["requiredServices"], ["survival", "overmap"])
        self.assertEqual(captured["params"], ([1, 2],))

    def test_autoscaler_does_not_mutate_maps_during_maintenance(self):
        self.panel.read_restart_state = lambda: {
            "jobs": [{"id": "maintenance", "execute": True, "status": "executing"}]
        }
        self.panel.autoscaler_public_state = lambda include_inventory=False: {"enabled": True}
        self.panel.read_autoscaler_state = lambda: (_ for _ in ()).throw(
            AssertionError("autoscaler state reconciliation must not start during maintenance")
        )

        result = self.panel.autoscaler_tick()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "maintenance restart is executing")

    def test_restart_fails_closed_after_update_check_failure(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        phases = []

        def fake_run_restart_command(command, job, phase):
            phases.append(phase)
            if phase == "update":
                return {
                    "ok": False,
                    "phase": phase,
                    "returncode": 75,
                    "output": "missing helper image",
                    "error": "Steam package update check failed",
                }
            return {"ok": True, "phase": phase, "returncode": 0, "output": phase}

        self.panel.run_restart_command = fake_run_restart_command
        self.panel.wait_for_restart_online = lambda: {
            "ok": True,
            "expected": 30,
            "online": 30,
            "readyOnline": 30,
            "alive": 30,
            "active": 30,
        }

        result = self.panel.execute_restart({
            "id": "update-failure",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": False,
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["serviceRecovered"])
        self.assertEqual(phases, ["stop", "update", "start"])
        self.assertEqual(result["error"], "Steam package update check failed")
        self.assertIn("missing helper image", result["output"])

    def test_update_readiness_candidate_parser_binds_tag_and_build(self):
        game = {
            "returncode": 1,
            "stdout": "\n".join([
                "current DUNE_IMAGE_TAG: dune_sb_1_4_9_0",
                "Steam installed buildid: 24146567",
                "Steam target buildid: 24146567",
                "last loaded buildid: 24000000",
                "package server tags:",
                "  dune_sb_1_4_10_0",
                "status: update available",
                "next tag: dune_sb_1_4_10_0",
            ]),
            "stderr": "",
        }
        candidate, text, tags = self.panel.update_readiness_candidate(game)
        self.assertEqual("dune_sb_1_4_10_0", candidate["imageTag"])
        self.assertEqual("dune_sb_1_4_9_0", candidate["currentImageTag"])
        self.assertEqual("update-available", candidate["status"])
        self.assertEqual("24146567", candidate["installedBuildId"])
        self.assertEqual(["dune_sb_1_4_10_0"], tags)
        self.assertIn("status: update available", text)

    def test_update_readiness_native_package_and_coriolis_checks_need_no_shell(self):
        steam = self.workspace / "steam"
        battlegroup = steam / "images" / "battlegroup"
        battlegroup.mkdir(parents=True)
        manifest = json.dumps([{"RepoTags": ["registry.funcom.com/funcom/self-hosting/seabass-server:dune_sb_1_4_10_0"]}]).encode()
        for name in ("server-rabbitmq.tar", "server-text-router.tar", "server-bg-director.tar", "server-gateway.tar", "server-db-utils.tar", "server.tar"):
            with tarfile.open(battlegroup / name, "w") as archive:
                info = tarfile.TarInfo("manifest.json")
                info.size = len(manifest)
                archive.addfile(info, io.BytesIO(manifest))
        (steam / "steamapps").mkdir()
        (steam / "steamapps" / "appmanifest_4754530.acf").write_text('"buildid" "24146567"\n"TargetBuildID" "24146567"\n', encoding="utf-8")
        (battlegroup / ".loaded_buildid").write_text("24000000\n", encoding="utf-8")
        for name in ("UserGame.ini", "UserGame.deep-desert-coriolis.ini", "UserGame.deep-desert-pvp.ini"):
            (self.workspace / "config" / name).write_text((ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch.dict(os.environ, {"DUNE_UPDATE_READINESS_STEAM_DIR": str(steam), "DUNE_IMAGE_TAG": "dune_sb_1_4_9_0"}):
            evidence = self.panel.update_readiness_native_candidate()
            coriolis = self.panel.update_readiness_coriolis_native()
        self.assertTrue(evidence["packageIdentified"])
        self.assertTrue(evidence["packageComplete"])
        self.assertTrue(evidence["steamSettled"])
        self.assertEqual("update-available", evidence["candidate"]["status"])
        self.assertEqual("24146567", evidence["candidate"]["installedBuildId"])
        self.assertEqual("bounded-seekable-tar-headers", evidence["archiveInspection"]["mode"])
        self.assertEqual(6, evidence["archiveInspection"]["successfullyInspectedArchives"])
        self.assertGreaterEqual(evidence["archiveInspection"]["durationMs"], 0)
        self.assertTrue(coriolis["ok"], coriolis["failures"])

    def test_bounded_docker_manifest_finds_tail_header_without_streaming_layers(self):
        archive_path = self.workspace / "tail-manifest.tar"
        manifest = [{"RepoTags": ["registry.example.test/game:tail"]}]
        payload = json.dumps(manifest).encode()
        with tarfile.open(archive_path, "w") as archive:
            filler = tarfile.TarInfo("large-layer/layer.tar")
            filler.size = 64 * 1024
            archive.addfile(filler, io.BytesIO(b"x" * filler.size))
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        result = self.panel.bounded_docker_manifest(archive_path, window_bytes=16 * 1024)
        self.assertEqual(manifest, result)

    def test_bounded_docker_manifest_rejects_corrupt_header_checksum(self):
        archive_path = self.workspace / "corrupt-manifest.tar"
        payload = json.dumps([{"RepoTags": ["registry.example.test/game:corrupt"]}]).encode()
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        content = bytearray(archive_path.read_bytes())
        header_offset = next(
            offset for offset in range(0, len(content), 512)
            if content[offset:offset + len(b"manifest.json")] == b"manifest.json"
        )
        content[header_offset + 100] ^= 1
        archive_path.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "checksum does not match"):
            self.panel.bounded_docker_manifest(archive_path)

    def test_bounded_docker_manifest_rejects_compressed_archive(self):
        raw_path = self.workspace / "manifest.tar"
        compressed_path = self.workspace / "manifest.tar.gz"
        payload = json.dumps([{"RepoTags": ["registry.example.test/game:gzip"]}]).encode()
        with tarfile.open(raw_path, "w") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        with gzip.open(compressed_path, "wb") as handle:
            handle.write(raw_path.read_bytes())
        with self.assertRaisesRegex(ValueError, "uncompressed seekable tar"):
            self.panel.bounded_docker_manifest(compressed_path)

    def test_update_readiness_selects_atomic_backup_leaf_not_aggregate_parent(self):
        aggregate = self.workspace / "backups" / "admin-panel"
        historical = aggregate / "maintenance" / "old"
        atomic = self.workspace / "backups" / "20260716T221526Z"
        historical.mkdir(parents=True)
        atomic.mkdir(parents=True)
        (historical / "historical.dump").write_bytes(b"old")
        (aggregate / "loose-admin.dump").write_bytes(b"not-a-full-backup")
        (atomic / "current.dump").write_bytes(b"current")
        (atomic / "manifest.txt").write_text("WORLD_UNIQUE_NAME=test\n", encoding="utf-8")
        (atomic / "config.tgz").write_bytes(b"config")
        original_inventory = self.panel.backup_inventory
        self.panel.backup_inventory = lambda limit=100: {
            "sets": [{"path": "admin-panel"}, {"path": "20260716T221526Z"}]
        }
        self.addCleanup(lambda: setattr(self.panel, "backup_inventory", original_inventory))
        selected = self.panel.latest_full_backup_set()
        self.assertEqual("20260716T221526Z", selected["path"])

    def test_browser_game_update_requires_current_signed_candidate_receipt(self):
        original_required = self.panel.UPDATE_READINESS_REQUIRE_RECEIPT
        original_snapshot = self.panel.update_readiness_snapshot
        original_store = self.panel.update_readiness_store
        original_helper = self.panel.run_update_host_helper
        self.panel.UPDATE_READINESS_REQUIRE_RECEIPT = True
        self.panel.update_readiness_snapshot = lambda force=False: {"candidate": {"imageTag": "one"}}
        self.panel.update_readiness_store = lambda: type("Store", (), {"status": lambda self, snapshot: {"applyReady": False, "currentReceiptReady": False}})()
        self.addCleanup(lambda: setattr(self.panel, "UPDATE_READINESS_REQUIRE_RECEIPT", original_required))
        self.addCleanup(lambda: setattr(self.panel, "update_readiness_snapshot", original_snapshot))
        self.addCleanup(lambda: setattr(self.panel, "update_readiness_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "run_update_host_helper", original_helper))
        with self.assertRaisesRegex(PermissionError, "candidate-bound signed readiness receipt"):
            self.panel.update_console_action("game-apply")
        self.panel.update_readiness_store = lambda: type("Store", (), {"status": lambda self, snapshot: {"applyReady": True, "currentReceiptReady": True}})()
        self.panel.run_update_host_helper = lambda action: {"ok": True, "exitCode": 0, "output": "updated", "action": action}
        result = self.panel.update_console_action("game-apply")
        self.assertTrue(result["ok"])
        self.assertTrue(result["certifiedCandidateAcquisitionDisabled"])

    def test_game_update_staging_acquires_candidate_without_touching_containers(self):
        original_helper = self.panel.run_update_host_helper
        original_candidate = self.panel.update_readiness_native_candidate
        calls = []
        self.panel.run_update_host_helper = lambda action: calls.append(action) or {"ok": True, "exitCode": 0, "output": "staged"}
        self.panel.update_readiness_native_candidate = lambda: {"packageIdentified": True, "packageComplete": True, "steamSettled": True, "candidate": {"imageTag": "new"}}
        self.addCleanup(lambda: setattr(self.panel, "run_update_host_helper", original_helper))
        self.addCleanup(lambda: setattr(self.panel, "update_readiness_native_candidate", original_candidate))
        result = self.panel.update_console_action("game-stage")
        self.assertTrue(result["ok"])
        self.assertFalse(result["gameMutationExecuted"])
        self.assertFalse(result["containersTouched"])
        self.assertEqual(["stage"], calls)

    def test_update_host_helper_uses_confined_fixed_candidate_workflow(self):
        (self.workspace / ".env").write_text("DUNE_STEAM_SERVER_DIR=/srv/dune-steam\n", encoding="utf-8")
        calls = []
        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path.startswith("/containers/create"):
                return {}, json.dumps({"Id": "a" * 64}).encode()
            if path.endswith("/wait"):
                return {}, json.dumps({"StatusCode": 0}).encode()
            return {}, b""
        with mock.patch.dict(os.environ, {"DUNE_RESTART_HOST_WORKSPACE": "/srv/dash", "DUNE_RESTART_COMPOSE_IMAGE": "docker:27.5.1-cli"}), \
             mock.patch.object(self.panel, "docker_api", return_value={"Name": "kspls0"}), \
             mock.patch.object(self.panel, "docker_http_request", side_effect=request), \
             mock.patch.object(self.panel, "docker_http_get", return_value=({}, b"")):
            result = self.panel.run_update_host_helper("stage")
        self.assertTrue(result["ok"])
        create = next(row for row in calls if row[1].startswith("/containers/create"))
        body = create[2]["body"]
        self.assertIn("/srv/dash:/workspace", body["HostConfig"]["Binds"])
        self.assertIn("/srv/dune-steam:/srv/dune-steam", body["HostConfig"]["Binds"])
        self.assertIn("update-steam-tool.sh", body["Cmd"][2])
        self.assertEqual("update-readiness-host-helper", body["Labels"]["com.snapetech.dune.role"])
        self.assertTrue(any(row[0] == "DELETE" for row in calls))

    def test_full_restart_reboots_after_applied_steam_update_when_enabled(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.REBOOT_AFTER_STEAM_UPDATE = True
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        phases = []

        def fake_run(command, job, phase):
            phases.append(phase)
            output = "DUNE_STEAM_UPDATE_APPLIED=100-0:101-0" if phase == "update" else phase
            return {"ok": True, "phase": phase, "returncode": 0, "output": output}

        self.panel.run_restart_command = fake_run
        result = self.panel.execute_restart({
            "id": "updated",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": False,
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["deferred"])
        self.assertEqual(phases, ["stop", "update", "reboot"])

    def test_full_restart_does_not_reboot_when_steam_build_is_current(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.REBOOT_AFTER_STEAM_UPDATE = True
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        phases = []
        self.panel.run_restart_command = lambda command, job, phase: phases.append(phase) or {
            "ok": True, "phase": phase, "returncode": 0, "output": "status: current" if phase == "update" else phase,
        }
        self.panel.wait_for_restart_online = lambda: {"ok": True}

        result = self.panel.execute_restart({
            "id": "current",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": False,
        })

        self.assertTrue(result["ok"])
        self.assertEqual(phases, ["stop", "update", "start"])

    def test_restart_start_runs_after_maintenance_backup_failure(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.MAINTENANCE_BACKUP_ENABLED = True
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        phases = []
        self.panel.run_restart_command = lambda command, job, phase: phases.append(phase) or {
            "ok": True,
            "phase": phase,
            "returncode": 0,
            "output": phase,
        }
        self.panel.create_maintenance_backup = lambda job: (_ for _ in ()).throw(RuntimeError("backup path unavailable"))
        self.panel.wait_for_restart_online = lambda: {
            "ok": True,
            "expected": 30,
            "online": 30,
            "readyOnline": 30,
            "alive": 30,
            "active": 30,
        }

        result = self.panel.execute_restart({
            "id": "backup-failure",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": True,
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["serviceRecovered"])
        self.assertTrue(result["updateSuppressedByBackupFailure"] is False)
        self.assertEqual(phases, ["stop", "start"])
        self.assertIn("maintenance backup failed", result["warning"])
        self.assertIn("backup path unavailable", result["output"])

    def test_certified_update_is_suppressed_when_backup_verification_fails(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.MAINTENANCE_BACKUP_ENABLED = True
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        candidate = {
            "imageTag": "dune_sb_1_4_11_0", "currentImageTag": "dune_sb_1_4_10_0",
            "status": "update-available", "updateRequired": True, "fingerprint": "f" * 64,
        }
        self.panel.update_readiness_snapshot = lambda force=False: {"candidate": candidate}
        self.panel.update_readiness_store = lambda: types.SimpleNamespace(status=lambda snapshot: {
            "applyReady": True, "currentReceiptReady": True,
            "evaluation": {"candidate": candidate, "failedChecks": []},
            "latest": {"id": "update-readiness-" + "a" * 32, "receiptSha256": "b" * 64},
        })
        backup_dir = self.workspace / "backups" / "admin-panel" / "maintenance" / "bad"
        backup_dir.mkdir(parents=True)
        self.panel.create_maintenance_backup = lambda job: {"path": str(backup_dir), "output": "created"}
        self.panel.verify_backup_set = lambda path: {"ok": False, "path": path, "exitCode": 1, "stdout": "", "stderr": "corrupt"}
        phases = []
        self.panel.run_restart_command = lambda command, job, phase: phases.append(
            (phase, job.get("_checkSteamUpdate"), job.get("_steamUpdateMode"))
        ) or {"ok": True, "phase": phase, "returncode": 0, "output": phase}
        self.panel.wait_for_restart_online = lambda: {"ok": True}

        result = self.panel.execute_restart({
            "id": "bad-verified-backup", "execute": True, "action": "restart", "target": "all",
            "services": [], "backup": True, "updatePolicy": "certified",
        })

        self.assertFalse(result["ok"])
        self.assertTrue(result["serviceRecovered"])
        self.assertTrue(result["updateSuppressedByBackupFailure"])
        self.assertFalse(result["backup"]["verification"]["ok"])
        self.assertEqual([("stop", True, "none"), ("start", False, "none")], phases)

    def test_maintenance_history_api_ui_and_metrics_are_exposed(self):
        self.assertIn("/api/ops/maintenance-history?limit=100", self.panel.INDEX)
        self.assertIn("Maintenance Intelligence", self.panel.INDEX)
        self.assertIn("restartUpdatePolicy", self.panel.INDEX)
        handler, captured = self.make_route_handler("/api/ops/maintenance-history")
        handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertIn("receipts", captured["json"])
        self.assertIn("dash_maintenance_outcome_collector_up", (ROOT / "admin" / "admin_panel.py").read_text(encoding="utf-8"))

    def test_player_impact_maintenance_api_ui_and_metrics_are_exposed(self):
        self.assertIn("/api/ops/maintenance-planner", self.panel.INDEX)
        self.assertIn("Player-Impact Maintenance Planner", self.panel.INDEX)
        self.assertIn("restartRunAt", self.panel.INDEX)
        self.panel.MAINTENANCE_PLANNER_RUNTIME.update({"running": True, "lastError": None})
        handler, captured = self.make_route_handler("/api/ops/maintenance-planner")
        handler.do_GET()
        self.assertEqual([], captured["errors"])
        self.assertTrue(captured["json"]["ok"])
        self.assertEqual("policy-fallback-learning", captured["json"]["source"])
        metrics = self.panel.maintenance_planner_prometheus()
        self.assertIn("dash_maintenance_planner_collector_up 1", metrics)

    def test_restart_runs_recovery_when_farm_readiness_is_incomplete(self):
        command = self.workspace / "scripts" / "restart-target.sh"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.panel.RESTART_COMMAND = str(command)
        self.panel.soft_disconnect_online_players = lambda job: {"ok": True}
        self.panel.run_restart_command = lambda command, job, phase: {
            "ok": True,
            "phase": phase,
            "returncode": 0,
            "output": phase,
        }
        snapshots = [
            {"ok": False, "expected": 30, "online": 29, "readyOnline": 29, "alive": 29, "active": 29},
            {"ok": True, "expected": 30, "online": 30, "readyOnline": 30, "alive": 30, "active": 30},
        ]
        recoveries = []
        self.panel.wait_for_restart_online = lambda: snapshots.pop(0)
        self.panel.run_restart_recovery = lambda job: recoveries.append(job["id"]) or {"ok": True, "output": "recovered"}

        result = self.panel.execute_restart({
            "id": "recover",
            "execute": True,
            "action": "restart",
            "target": "all",
            "services": [],
            "backup": False,
        })

        self.assertTrue(result["ok"])
        self.assertEqual(recoveries, ["recover"])
        self.assertEqual(result["recovery"]["output"], "recovered")

    def test_restart_recovery_uses_host_helper_when_admin_image_has_no_bash(self):
        recovery = self.workspace / "scripts" / "watch-maps.sh"
        recovery.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        recovery.chmod(0o755)
        launcher = self.workspace / "scripts" / "restart-target.sh"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        self.panel.RESTART_RECOVERY_COMMAND = str(recovery)
        self.panel.RESTART_COMMAND = str(launcher)
        self.panel.RESTART_ONLINE_TIMEOUT_SECONDS = 7
        self.panel.RESTART_RECOVERY_TIMEOUT_SECONDS = 11
        original_which = self.panel.shutil.which
        original_run = self.panel.subprocess.run
        calls = []
        self.panel.shutil.which = lambda name: None if name == "bash" else original_which(name)
        self.panel.subprocess.run = lambda argv, **kwargs: calls.append((argv, kwargs)) or types.SimpleNamespace(
            returncode=0, stdout="host helper", stderr=""
        )
        self.addCleanup(lambda: setattr(self.panel.shutil, "which", original_which))
        self.addCleanup(lambda: setattr(self.panel.subprocess, "run", original_run))

        result = self.panel.run_restart_recovery({
            "id": "recover-host-helper",
            "target": "all",
            "services": ["survival", "overmap"],
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "host-helper")
        self.assertEqual(calls[-1][0], [str(launcher), "all"])
        self.assertEqual(calls[-1][1]["env"]["DUNE_RESTART_PHASE"], "recovery")
        self.assertEqual(calls[-1][1]["env"]["DUNE_WATCH_RECOVERY_WAIT"], "7")
        self.assertEqual(calls[-1][1]["env"]["DUNE_WATCH_RECOVER_TIMEOUT"], "11")

    def test_artificial_exchange_install_actions_use_explicit_unit_paths(self):
        captured = []
        original = self.panel.run_workspace_command

        def fake_run(command, timeout=60, **kwargs):
            captured.append((list(command), timeout))
            return {"ok": True, "stdout": '{"ok":true}', "stderr": ""}

        self.panel.run_workspace_command = fake_run
        self.addCleanup(lambda: setattr(self.panel, "run_workspace_command", original))

        self.panel.artificial_exchange_action("install-buyer-service")
        self.panel.artificial_exchange_action("install-populator-service")
        self.panel.artificial_exchange_action("install-watchdog-timer")
        self.panel.artificial_exchange_action("watchdog-once")

        commands = [item[0] for item in captured]
        self.assertEqual(commands[0][-2:], ["/etc/systemd/system/dune-artificial-exchange-bot.service", "buyer"])
        self.assertEqual(commands[1][-2:], ["/etc/systemd/system/dune-artificial-exchange-populator.service", "populator"])
        self.assertTrue(commands[2][0].endswith("install-artificial-exchange-watchdog-timer.sh"))
        self.assertTrue(commands[3][0].endswith("artificial-exchange-watchdog.sh"))

    def test_item_catalog_missing_file_is_safe_and_empty(self):
        result = self.panel.load_item_catalog()

        self.assertEqual(result["items"], [])
        self.assertIn("not been generated", result["warning"])

    def test_item_catalog_loads_visual_metadata(self):
        payload = {
            "schemaVersion": 1,
            "source": {"label": "test"},
            "items": [{"templateId": "WeldingTool_01", "name": "Welding Tool", "imageUrl": "https://media.awakening.wiki/wiki/a/a0/tool.png"}],
        }
        self.panel.ITEM_CATALOG_FILE.write_text(json.dumps(payload), encoding="utf-8")

        result = self.panel.load_item_catalog()

        self.assertEqual(result["items"][0]["templateId"], "WeldingTool_01")
        self.assertEqual(self.panel.catalog_item("WeldingTool_01")["name"], "Welding Tool")
        self.assertIsNone(self.panel.catalog_item("HouseWeldingTool"))

    def test_docker_log_stream_decoder_handles_framed_and_plain_logs(self):
        first = b"stdout line\n"
        second = b"stderr line\n"
        framed = b"\x01\x00\x00\x00" + len(first).to_bytes(4, "big") + first
        framed += b"\x02\x00\x00\x00" + len(second).to_bytes(4, "big") + second

        self.assertEqual(self.panel.decode_docker_log_stream(framed), "stdout line\nstderr line\n")
        self.assertEqual(self.panel.decode_docker_log_stream(b"plain tty log\n"), "plain tty log\n")

    def test_map_memory_stats_fanout_excludes_stopped_and_non_map_containers(self):
        original_api = self.panel.docker_api
        requests = []
        containers = [
            {"Id": "a" * 64, "Names": ["/survival"], "State": "running", "Labels": {"com.docker.compose.service": "survival"}},
            {"Id": "b" * 64, "Names": ["/arrakeen"], "State": "exited", "Labels": {"com.docker.compose.service": "arrakeen"}},
            {"Id": "c" * 64, "Names": ["/postgres"], "State": "running", "Labels": {"com.docker.compose.service": "postgres"}},
        ]
        stats = {
            "memory_stats": {"usage": 1024, "limit": 4096},
            "cpu_stats": {}, "precpu_stats": {}, "networks": {},
            "blkio_stats": {}, "pids_stats": {"current": 3},
        }
        def fake_api(path):
            requests.append(path)
            return containers if path.startswith("/containers/json") else stats
        self.panel.docker_api = fake_api
        self.addCleanup(lambda: setattr(self.panel, "docker_api", original_api))

        rows = self.panel.map_memory_rows()

        self.assertEqual([row["service"] for row in rows], ["survival"])
        stats_requests = [path for path in requests if "/stats?stream=false" in path]
        self.assertEqual(stats_requests, [f"/containers/{'a' * 64}/stats?stream=false"])

    def test_service_logs_only_accept_project_service_names(self):
        original_containers = self.panel.docker_project_containers
        original_http = self.panel.docker_http_get
        inventory = [{
            "service": "director",
            "name": "dune_server-director-1",
            "containerId": "a" * 12,
        }]
        self.panel.docker_project_containers = lambda: (_ for _ in ()).throw(AssertionError("provided inventory must be reused"))
        requested = []
        self.panel.docker_http_get = lambda path, **kwargs: requested.append(path) or ({}, b"director ready\n")
        self.addCleanup(lambda: setattr(self.panel, "docker_project_containers", original_containers))
        self.addCleanup(lambda: setattr(self.panel, "docker_http_get", original_http))

        result = self.panel.docker_service_logs("director", 250, since=1234, inventory=inventory)

        self.assertEqual(result["tail"], 250)
        self.assertEqual(result["since"], 1234)
        self.assertEqual(result["logs"], "director ready\n")
        self.assertIn("&since=1234", requested[0])
        with self.assertRaises(ValueError):
            self.panel.docker_service_logs("../../postgres", 10)
        with self.assertRaises(ValueError):
            self.panel.docker_service_logs("not-in-project", 10, inventory=[])

    def test_backup_inventory_and_verifier_reject_path_traversal(self):
        backup_set = self.workspace / "backups" / "20260715T120000Z"
        backup_set.mkdir(parents=True)
        (backup_set / "manifest.txt").write_text("WORLD_NAME=test\n", encoding="utf-8")
        (backup_set / "postgres.dump").write_bytes(b"test")
        verifier = self.workspace / "scripts" / "verify-backup.sh"
        verifier.write_text("#!/bin/sh\ntest -f \"$1/manifest.txt\"\n", encoding="utf-8")
        verifier.chmod(0o755)

        inventory = self.panel.backup_inventory()
        verified = self.panel.verify_backup_set("20260715T120000Z")
        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            native_ok, native_stdout, native_stderr = self.panel.verify_backup_set_native(backup_set)

        self.assertEqual(inventory["sets"][0]["path"], "20260715T120000Z")
        self.assertTrue(verified["ok"])
        self.assertTrue(native_ok, native_stderr)
        self.assertIn("backup verification complete: OK", native_stdout)
        with self.assertRaises(ValueError):
            self.panel.resolve_backup_set("../outside")
        with self.assertRaises(ValueError):
            self.panel.resolve_backup_set("/tmp/outside")

    def test_native_backup_verifier_uses_matching_desired_state_key_not_live_key(self):
        backup_set = self.workspace / "backups" / "20260715T130000Z"
        backup_set.mkdir(parents=True)
        (backup_set / "manifest.txt").write_text("WORLD_NAME=test\n", encoding="utf-8")
        (backup_set / "postgres.dump").write_bytes(b"test")
        policy = self.workspace / "config" / "desired-state.json"
        policy.write_text((ROOT / "config" / "desired-state.json").read_text(encoding="utf-8"), encoding="utf-8")
        secret_dir = self.workspace / "config" / "secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret = secret_dir / "desired-state-hmac.secret"
        secret.write_text("b" * 64 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        store = self.panel.desired_state.Store(backup_set / "desired-state.sqlite3", policy, secret)
        store.initialize()
        store.seal({"schemaVersion": 1, "files": {}, "containers": {}}, "test", "fixture", at=1000)
        change_policy = self.workspace / "config" / "change-intelligence.json"
        change_policy.write_text((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"), encoding="utf-8")
        change_secret = secret_dir / "change-intelligence-hmac.secret"
        change_secret.write_text("d" * 64 + "\n", encoding="utf-8")
        change_secret.chmod(0o600)
        change_store = self.panel.change_intelligence.Store(backup_set / "change-intelligence.sqlite3", change_policy, change_secret)
        change_store.initialize()
        change_store.record({"action": "settings-write", "ts": 1000, "ok": True, "eventId": "fixture"}, ingested_at=1001)
        change_store.record({"action": "slo-incident-opened", "ts": 1100, "ok": False, "incident_id": "fixture", "objective_id": "database_availability", "eventId": "fixture-open"}, ingested_at=1101)
        capsule = self.workspace / "fixture.signed.json"
        capsule.write_text(json.dumps(change_store.signed_capsule("slo:fixture", at=1200)), encoding="utf-8")
        with tarfile.open(backup_set / "operator-evidence.tgz", "w:gz") as archive:
            archive.add(capsule, arcname="operator-evidence/fixture.signed.json")
        with tarfile.open(backup_set / "config.tgz", "w:gz") as archive:
            archive.add(policy, arcname="config/desired-state.json")
            archive.add(secret, arcname="config/secrets/desired-state-hmac.secret")
            archive.add(change_policy, arcname="config/change-intelligence.json")
            archive.add(change_secret, arcname="config/secrets/change-intelligence-hmac.secret")
        secret.write_text("c" * 64 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        change_secret.write_text("f" * 64 + "\n", encoding="utf-8")
        change_secret.chmod(0o600)

        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            ok, stdout, stderr = self.panel.verify_backup_set_native(backup_set)

        self.assertTrue(ok, stderr)
        self.assertIn("OK desired-state backup-bound HMAC attestations", stdout)
        self.assertIn("OK change-intelligence backup-bound HMAC event chain", stdout)
        self.assertIn("OK 1 portable signed operator evidence capsule(s)", stdout)

    def test_native_backup_verifier_requires_complete_valid_audit_ledger_set(self):
        backup_set = self.workspace / "backups" / "20260716T220000Z"
        backup_set.mkdir(parents=True)
        (backup_set / "manifest.txt").write_text("WORLD_NAME=test\n", encoding="utf-8")
        (backup_set / "postgres.dump").write_bytes(b"test")
        store = self.panel.audit_ledger.Store(
            backup_set / "audit-ledger.sqlite3",
            key_path=backup_set / "audit-ledger.hmac.key",
            anchor_path=backup_set / "audit-ledger.anchor.json",
        )
        store.initialize()
        store.append({
            "ts": "2026-07-16T22:00:00Z",
            "action": "backup-fixture",
            "ok": True,
            "eventId": "audit-" + "1" * 32,
        })

        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            ok, stdout, stderr = self.panel.verify_backup_set_native(backup_set)
        self.assertTrue(ok, stderr)
        self.assertIn("OK audit ledger backup-bound HMAC chain and authenticated head", stdout)

        (backup_set / "audit-ledger.anchor.json").unlink()
        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            ok, stdout, stderr = self.panel.verify_backup_set_native(backup_set)
        self.assertFalse(ok)
        self.assertIn("requires database, HMAC key, and authenticated anchor together", stderr)

    def test_native_backup_verifier_checks_credential_and_change_approval_hmac_pairs(self):
        backup_set = self.workspace / "backups" / "20260716T221000Z"
        backup_set.mkdir(parents=True)
        (backup_set / "manifest.txt").write_text("WORLD_NAME=test\n", encoding="utf-8")
        (backup_set / "postgres.dump").write_bytes(b"test")
        secret_dir = self.workspace / "config" / "secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        credential_key = secret_dir / "credential-lifecycle-hmac.secret"
        credential_key.write_bytes(b"c" * 32)
        credential_key.chmod(0o600)
        credential_store = self.panel.credential_lifecycle.ObservationStore(
            backup_set / "credential-lifecycle.sqlite3", credential_key, clock=lambda: 1000
        )
        credential_store.observe("fixture", b"private fixture material")
        approval_store = self.panel.change_approvals.Store(
            backup_set / "change-approvals.sqlite3",
            key_path=backup_set / "change-approvals.key",
        )
        approval_store.initialize()
        with tarfile.open(backup_set / "config.tgz", "w:gz") as archive:
            archive.add(credential_key, arcname="config/secrets/credential-lifecycle-hmac.secret")

        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            ok, stdout, stderr = self.panel.verify_backup_set_native(backup_set)
        self.assertTrue(ok, stderr)
        self.assertIn("OK credential lifecycle backup-bound HMAC chain and authenticated head", stdout)
        self.assertIn("OK change-approval backup-bound HMAC chain", stdout)

        credential_anchor = backup_set / "credential-lifecycle.anchor.json"
        credential_anchor_value = credential_anchor.read_bytes()
        credential_anchor.unlink()
        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            ok, stdout, stderr = self.panel.verify_backup_set_native(backup_set)
        self.assertFalse(ok)
        self.assertIn("credential lifecycle backup requires its SQLite ledger and authenticated head together", stderr)
        credential_anchor.write_bytes(credential_anchor_value)
        credential_anchor.chmod(0o600)

        (backup_set / "change-approvals.key").unlink()
        with mock.patch.object(self.panel.shutil, "which", return_value=None):
            ok, stdout, stderr = self.panel.verify_backup_set_native(backup_set)
        self.assertFalse(ok)
        self.assertIn("change-approval backup requires its SQLite ledger and HMAC key together", stderr)

    def test_operator_evidence_archive_is_confined_private_and_fully_verified(self):
        evidence_root = self.workspace / "backups" / "operator-evidence"
        evidence_root.mkdir(parents=True)
        policy = self.workspace / "config" / "change-intelligence.json"
        policy.write_text((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"), encoding="utf-8")
        secret_dir = self.workspace / "config" / "secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret = secret_dir / "change-intelligence-hmac.secret"
        secret.write_text("e" * 64 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        store = self.panel.change_intelligence.Store(self.workspace / "change.sqlite3", policy, secret)
        store.initialize()
        store.record({"action": "slo-incident-opened", "ts": 1000, "ok": False, "incident_id": "archive", "objective_id": "database_availability", "eventId": "open"}, ingested_at=1001)
        signed = evidence_root / "archive.signed.json"
        signed.write_text(json.dumps(store.signed_capsule("slo:archive", at=1100)), encoding="utf-8")
        assurance = self.panel.deployment_assurance.Store(
            self.workspace / "backups" / "deployment-assurance", evidence_root, self.workspace,
            self.panel.change_intelligence.read_secret(secret),
        )
        manifest = [{"path": "config/change-intelligence.json", "sha256": self.panel.deployment_assurance.file_sha256(policy)}]
        snapshot = {"survival": {"containerId": "a" * 64, "state": "running", "startedAt": "2026-07-16T10:00:00Z"}}
        window = assurance.start(
            commit="b" * 40, reason="Verified operator evidence archive fixture", manifest=manifest,
            principal_id="owner", snapshot=snapshot, protected_services=["survival"], strict_services=["survival"],
            recovery_backup={"ok": True, "path": "before", "exitCode": 0},
            source_rollback={"verified": True, "path": "before.tgz", "sha256": "e" * 64, "bytes": 1}, now=1000,
        )
        assurance.finish(
            window["id"], principal_id="owner", snapshot=snapshot,
            health={"desiredStateAttested": True, "readinessCurrent": True, "sloHealthy": True, "changeIntegrity": True, "prometheusReadiness": True, "adminHealthy": True, "backupVerified": True},
            backup={"ok": True, "path": "after", "exitCode": 0}, now=1100,
        )
        update_store = self.panel.update_readiness.Store(
            evidence_root, self.panel.change_intelligence.read_secret(secret), ttl_seconds=600,
        )
        current_update = update_store.certify({
            "candidate": {
                "imageTag": "dune_sb_1_4_10_0", "currentImageTag": "dune_sb_1_4_9_0",
                "status": "update-available", "installedBuildId": "24146567",
                "targetBuildId": "24146567", "loadedBuildId": "24000000",
            },
            "checks": {key: True for key in self.panel.update_readiness.REQUIRED_CHECKS},
            "onlinePlayers": 0, "details": {"fixture": True},
        }, "owner", source_commit="b" * 40, now=1150)["document"]
        legacy_receipt = json.loads(json.dumps(current_update["receipt"]))
        legacy_receipt["schemaVersion"] = 1
        legacy_receipt["id"] = "update-readiness-" + "f" * 32
        legacy_receipt["checks"].pop("rabbitmqRestoreProofReady")
        legacy_document = self.panel.update_readiness.signed_document(
            legacy_receipt, self.panel.change_intelligence.read_secret(secret), generated_at=1150,
        )
        (evidence_root / f"{legacy_receipt['id']}.signed.json").write_text(
            json.dumps(legacy_document), encoding="utf-8",
        )
        maintenance_store = self.panel.maintenance_outcomes.Store(
            evidence_root, self.panel.change_intelligence.read_secret(secret), retention=10,
        )
        maintenance_store.record({
            "id": "archive_job", "target": "all", "action": "restart",
            "updatePolicy": "current", "runAt": 1160, "execute": False,
            "backup": True, "principalId": "owner",
        }, {"ok": True, "dryRun": True, "serviceRecovered": False}, 1160, 1161)
        (evidence_root / "ignored.txt").write_text("not evidence", encoding="utf-8")
        (evidence_root / "linked.signed.json").symlink_to(signed)
        original_root = self.panel.CHANGE_INTELLIGENCE_EVIDENCE_ROOT
        self.panel.CHANGE_INTELLIGENCE_EVIDENCE_ROOT = evidence_root
        self.addCleanup(lambda: setattr(self.panel, "CHANGE_INTELLIGENCE_EVIDENCE_ROOT", original_root))

        archive = self.workspace / "operator-evidence.tgz"
        result = self.panel.archive_operator_evidence(archive)
        verification = self.panel.verify_operator_evidence_archive(archive, secret)
        self.assertEqual(5, result["files"])
        self.assertEqual(5, verification["files"])
        self.assertEqual(0o600, archive.stat().st_mode & 0o777)
        with tarfile.open(archive, "r:gz") as handle:
            self.assertEqual(5, len(handle.getnames()))
            self.assertIn("operator-evidence/archive.signed.json", handle.getnames())
            self.assertTrue(any(name.startswith("operator-evidence/update-readiness-") for name in handle.getnames()))
            self.assertTrue(any(name.startswith("operator-evidence/maintenance-outcome-") for name in handle.getnames()))

        unsafe_name = evidence_root / "unsafe name.signed.json"
        unsafe_name.write_text(signed.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsafe backup name"):
            self.panel.archive_operator_evidence(self.workspace / "unsafe.tgz")
        unsafe_name.unlink()
        signed.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "invalid size"):
            self.panel.archive_operator_evidence(self.workspace / "oversized.tgz")

    def test_database_browser_is_allowlisted_capped_and_redacted(self):
        def fake_query(sql, params=None):
            if "from information_schema.tables" in sql and "table_name=%s" in sql:
                return [{"table_type": "BASE TABLE"}]
            if sql.startswith('SELECT * FROM "dune"."accounts"'):
                return [{"id": 1, "service_auth_token": "secret-token", "display_name": "Tester"}]
            if "from information_schema.columns" in sql:
                return [
                    {"name": "id", "type": "bigint", "nullable": "NO"},
                    {"name": "service_auth_token", "type": "text", "nullable": "YES"},
                ]
            if "from information_schema.tables" in sql:
                return [{"schema": "dune", "name": "accounts", "type": "BASE TABLE"}]
            raise AssertionError(sql)

        self.patch_db(fake_query)

        catalog = self.panel.database_browser_catalog()
        preview = self.panel.database_table_preview("dune", "accounts", 999)

        self.assertEqual(catalog["tables"][0]["name"], "accounts")
        self.assertEqual(preview["limit"], 200)
        self.assertEqual(preview["rows"][0]["service_auth_token"], "[redacted]")
        self.assertEqual(preview["rows"][0]["display_name"], "Tester")
        with self.assertRaises(ValueError):
            self.panel.database_table_preview("pg_catalog", "pg_authid", 10)
        with self.assertRaises(ValueError):
            self.panel.database_table_preview("dune", "accounts; drop table dune.accounts", 10)

    def test_infrastructure_page_and_routes_are_registered(self):
        self.assertTrue(self.handler.is_app_route("/infrastructure"))
        self.assertIn('data-tab="infrastructure"', self.panel.INDEX)
        self.assertIn("/api/ops/services", self.panel.INDEX)
        self.assertIn("/api/ops/backups/verify", self.panel.INDEX)
        self.assertIn("/api/ops/database/table", self.panel.INDEX)

    def test_world_views_are_read_only_bounded_and_registered(self):
        executed = []

        def fake_query(sql, params=None):
            if "from dune.guilds g" in sql:
                return [{"guild_id": 9, "guild_name": "Test Guild", "member_count": 1}]
            if "get_guild_data" in sql:
                return [{"guild_id": 9, "guild_name": "Test Guild"}]
            if "get_guild_members" in sql:
                return [{"guild_id": 9, "player_id": 200, "role_id": 1}]
            if "get_guild_invites" in sql:
                return []
            if "landsraad_load_current_term" in sql:
                return [{"term_id": 4, "end_time": "2026-07-20"}]
            if "landsraad_decree_term" in sql:
                return [{"term_id": 4}]
            if "landsraad_decrees" in sql:
                return [{"id": 1, "decree_name": "Test Decree"}]
            if "landsraad_tasks" in sql and "landsraad_task_" not in sql:
                return [{"id": 10, "term_id": 4, "board_index": 0}]
            if "landsraad_task_rewards" in sql:
                return [{"task_id": 10, "threshold": 100}]
            if "landsraad_task_faction_contributions" in sql:
                return [{"task_id": 10, "amount": 25}]
            if "landsraad_task_player_contributions" in sql:
                return [{"task_id": 10, "amount": 10}]
            if "landsraad_task_guild_contributions" in sql:
                return [{"task_id": 10, "amount": 15}]
            if "from dune.placeables p" in sql:
                return [{"id": 55, "class": "StorageContainer_Placeable", "item_count": 3, "owner_name": "Tester"}]
            raise AssertionError(sql)

        self.patch_db(fake_query, lambda sql, params=None: executed.append((sql, params)))

        guilds = self.panel.world_guilds("Test")
        members = self.panel.world_guild_members(9)
        landsraad = self.panel.world_landsraad()
        storage = self.panel.world_storage()

        self.assertTrue(guilds["readOnly"])
        self.assertEqual(guilds["maxRows"], 500)
        self.assertEqual(members["members"][0]["player_id"], 200)
        self.assertEqual(landsraad["tasks"][0]["term_id"], 4)
        self.assertEqual(storage["maxRows"], 2000)
        self.assertEqual(storage["rows"][0]["item_count"], 3)
        self.assertEqual(executed, [])
        self.assertTrue(self.handler.is_app_route("/world"))
        self.assertIn('data-tab="world"', self.panel.INDEX)
        self.assertIn("/api/world/guilds", self.panel.INDEX)
        self.assertIn("/api/world/landsraad", self.panel.INDEX)
        self.assertIn("/api/world/storage", self.panel.INDEX)

    def test_world_routes_require_valid_guild_ids_and_return_reads(self):
        self.patch_db(lambda sql, params=None: [] if "get_guild" in sql else [{"guild_id": 9}] if "from dune.guilds g" in sql else [])
        handler, captured = self.make_route_handler("/api/world/guilds?q=Test")
        handler.do_GET()
        self.assertEqual(captured["errors"], [])
        self.assertTrue(captured["json"]["readOnly"])

        handler, captured = self.make_route_handler("/api/world/guild-members?guild_id=bad")
        handler.do_GET()
        self.assertEqual(len(captured["errors"]), 1)
        self.assertIn("positive integer", captured["errors"][0]["message"])

    def test_blueprint_archive_validation_normalizes_ids_and_rejects_bad_rows(self):
        archive = self.panel.blueprint_admin.validate_archive({
            "name": "Test_Base.v1",
            "instances": [{"instance_id": 0, "building_type": "MTX_Smug_Foundation", "x": 1, "y": 2, "z": 3, "rotation": 90}],
            "placeables": [{"placeable_id": 0, "building_type": "Generator_Placeable", "x": 4, "y": 5, "z": 6}],
            "pentashields": [{"placeable_id": 0, "scale": [10, 2, 10]}],
        })

        self.assertEqual(archive["name"], "Test Base v1")
        self.assertEqual(archive["instances"][0]["instance_id"], 1)
        self.assertTrue(archive["instances"][0]["provides_stability"])
        self.assertEqual(archive["placeables"][0]["placeable_id"], 1)
        self.assertEqual(archive["pentashields"][0]["placeable_id"], 1)

        with self.assertRaises(ValueError):
            self.panel.blueprint_admin.validate_archive({"instances": []})
        with self.assertRaises(ValueError):
            self.panel.blueprint_admin.validate_archive({"instances": [{"building_type": "bad type with spaces"}]})
        with self.assertRaises(ValueError):
            self.panel.blueprint_admin.validate_archive({"placeables": [
                {"placeable_id": 2, "building_type": "A"},
                {"placeable_id": 2, "building_type": "B"},
            ]})

    def test_blueprint_import_plan_checks_player_inventory_without_writing(self):
        executed = []

        def fake_query(sql, params=None):
            if "from dune.player_state" in sql:
                return [{"player_pawn_id": 200, "character_name": "Tester", "online_status": "Offline"}]
            if "from dune.inventories" in sql:
                return [{"id": 300, "max_item_count": 40, "used_slots": 2}]
            raise AssertionError(sql)

        plan = self.panel.blueprint_admin.plan_import(fake_query, 200, {
            "name": "Base",
            "instances": [{"building_type": "MTX_Smug_Foundation", "x": 0, "y": 0, "z": 0, "rotation": 0}],
        })

        self.assertTrue(plan["dryRun"])
        self.assertEqual(plan["inventory"]["available_slots"], 38)
        self.assertEqual(plan["counts"]["instances"], 1)
        self.assertEqual(executed, [])

    def test_blueprint_routes_are_registered_and_execution_is_backed_up(self):
        originals = {
            "capabilities": self.panel.blueprint_admin.capabilities,
            "list_blueprints": self.panel.blueprint_admin.list_blueprints,
            "plan_import": self.panel.blueprint_admin.plan_import,
            "import_blueprint": self.panel.blueprint_admin.import_blueprint,
            "create_db_backup": self.panel.create_db_backup,
        }
        self.panel.blueprint_admin.capabilities = lambda query_fn: {"supported": True, "tables": {}}
        self.panel.blueprint_admin.list_blueprints = lambda query_fn: [{"id": 1, "name": "Base"}]
        self.panel.blueprint_admin.plan_import = lambda query_fn, player_id, payload, filename="": {
            "ok": True, "dryRun": True, "playerPawnId": int(player_id), "archive": {"name": "Base", "instances": [{"building_type": "Foundation"}], "placeables": [], "pentashields": []}
        }
        imports = []
        backups = []
        self.panel.blueprint_admin.import_blueprint = lambda connect_fn, player_id, archive, fallback="": imports.append((player_id, archive)) or {"ok": True, "blueprintId": 4, "playerPawnId": player_id, "verified": True}
        self.panel.create_db_backup = lambda: backups.append(True) or {"path": "backup.dump", "bytes": 1}
        for name, value in originals.items():
            target = self.panel if name == "create_db_backup" else self.panel.blueprint_admin
            self.addCleanup(lambda target=target, name=name, value=value: setattr(target, name, value))

        handler, captured = self.make_route_handler("/api/admin/blueprints")
        handler.do_GET()
        self.assertEqual(captured["errors"], [])
        self.assertEqual(captured["json"]["rows"][0]["name"], "Base")
        self.assertTrue(self.handler.is_app_route("/blueprints"))
        self.assertIn('data-tab="blueprints"', self.panel.INDEX)

        preview = self.invoke_post_route("/api/admin/blueprints", {
            "action": "import", "dry_run": True, "player_pawn_id": 200,
            "blueprint": {"instances": [{"building_type": "Foundation"}]},
        })
        self.assertEqual(preview["errors"], [])
        self.assertEqual(preview["json"]["confirm"], "IMPORT BLUEPRINT")
        self.assertEqual(backups, [])

        self.patch_flag("BLUEPRINT_MUTATIONS_ENABLED", True)
        handler = self.make_route_handler("/api/admin/blueprints")[0]
        handler.require_mutations = lambda: None
        handler.require_item_grants = lambda: None
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.validate_json_post = lambda request_handler, max_bytes=self.panel.MAX_BODY_BYTES: None
        self.panel.parse_body = lambda request_handler, max_bytes=self.panel.MAX_BODY_BYTES: {
            "action": "import", "dry_run": False, "player_pawn_id": 200,
            "blueprint": {"instances": [{"building_type": "Foundation"}]},
            "confirm": "IMPORT BLUEPRINT",
        }
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        handler.do_POST()
        self.assertEqual(backups, [True])
        self.assertEqual(imports[0][0], 200)

    def test_structured_augment_catalog_enforces_item_limits_and_builds_stats(self):
        item = {
            "templateId": "Combat_Heavy_Unique_Reinforced_Boots_06",
            "name": "Bulwark Boots",
            "category": "armor/combat",
        }
        compatibility = self.panel.augment_admin.compatible_augments(item)
        self.assertTrue(compatibility["supported"])
        self.assertEqual(compatibility["kind"], "clothing")
        self.assertEqual(compatibility["limit"], 2)
        self.assertEqual(self.panel.augment_admin.slot_keystone_ids(compatibility), [42, 43])
        augment_id = compatibility["augments"][0]["templateId"]
        queries = []

        def fake_query(sql, params=None):
            queries.append((sql, params))
            return []

        built = self.panel.augment_admin.build_stats(fake_query, item, [augment_id], 5, {})
        payload = built["stats"]["FAugmentedItemStats"][1]
        self.assertEqual(payload["AppliedAugments"], [{"Name": augment_id}])
        self.assertEqual(payload["AppliedAugmentQualities"], [5])
        self.assertTrue(all(value == 1 for value in payload["AppliedAugmentRollData"][0]["StatRolls"]))
        self.assertEqual(len(queries), 2)

        with self.assertRaises(ValueError):
            self.panel.augment_admin.validate_selection(item, [row["templateId"] for row in compatibility["augments"][:3]], 1)
        with self.assertRaises(ValueError):
            self.panel.augment_admin.validate_selection(item, ["T6_Augment_Acuracy1"], 1)

    def test_augment_routes_preview_and_back_up_execution(self):
        item = {
            "id": 41,
            "template_id": "Combat_Heavy_Unique_Reinforced_Boots_06",
            "stats": {},
            "account_id": 10,
            "character_name": "Tester",
            "player_controller_id": 11,
            "online_status": "Offline",
        }
        self.patch_db(lambda sql, params=None: [item] if "from dune.items i join dune.inventories" in sql else [])
        originals = {
            "catalog_item": self.panel.catalog_item,
            "build_stats": self.panel.augment_admin.build_stats,
            "apply_to_item": self.panel.augment_admin.apply_to_item,
            "create_db_backup": self.panel.create_db_backup,
        }
        self.panel.catalog_item = lambda template: {"templateId": template, "name": "Bulwark Boots", "category": "armor/combat"}
        compatibility = {"kind": "clothing", "limit": 2, "itemTags": ["Items.Clothes.HeavyArmor"], "augments": [], "supported": True}
        self.panel.augment_admin.build_stats = lambda query_fn, metadata, augments, grade, stats=None: {
            "stats": {"FAugmentedItemStats": [[], {}]}, "augments": list(augments), "grade": int(grade), "compatibility": compatibility,
        }
        applied = []
        self.panel.augment_admin.apply_to_item = lambda connect_fn, item_id, augments, grade, metadata: applied.append((item_id, augments, grade)) or {"ok": True, "itemId": item_id, "augments": augments, "grade": int(grade), "verified": True}
        backups = []
        self.panel.create_db_backup = lambda: backups.append(True) or {"path": "backup.dump", "bytes": 1}
        for name, value in originals.items():
            target = self.panel.augment_admin if name in ("build_stats", "apply_to_item") else self.panel
            self.addCleanup(lambda target=target, name=name, value=value: setattr(target, name, value))

        preview = self.invoke_post_route("/api/admin/augments", {
            "action": "apply", "item_id": 41, "augments": ["T6_Augment_Armor1"], "grade": 5, "dry_run": True,
        })
        self.assertEqual(preview["errors"], [])
        self.assertTrue(preview["json"]["eligible"])
        self.assertEqual(preview["json"]["confirm"], "APPLY AUGMENTS")
        self.assertEqual(backups, [])

        self.patch_flag("AUGMENT_MUTATIONS_ENABLED", True)
        handler, captured = self.make_route_handler("/api/admin/augments")
        handler.require_mutations = lambda: None
        handler.require_item_grants = lambda: None
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.validate_json_post = lambda request_handler, **kwargs: None
        self.panel.parse_body = lambda request_handler, **kwargs: {
            "action": "apply", "item_id": 41, "augments": ["T6_Augment_Armor1"], "grade": 5,
            "dry_run": False, "confirm": "APPLY AUGMENTS",
        }
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        handler.do_POST()
        self.assertEqual(captured["errors"], [])
        self.assertEqual(backups, [True])
        self.assertEqual(applied[0][0], 41)
        self.assertEqual(captured["json"]["backup"]["path"], "backup.dump")

    def test_database_sql_console_classifies_bounds_and_gates_writes(self):
        self.assertTrue(self.panel.database_sql_is_read_only("-- note\nWITH rows AS (SELECT 1) SELECT * FROM rows"))
        self.assertFalse(self.panel.database_sql_is_read_only("update dune.items set stack_size=1"))
        with self.assertRaises(ValueError):
            self.panel.normalize_database_sql("select 1; select 2")

        original_runner = self.panel.run_database_sql
        original_backup = self.panel.create_db_backup
        calls = []
        backups = []
        self.panel.run_database_sql = lambda connect_fn, sql, allow_write=False, max_rows=200: calls.append((sql, allow_write, int(max_rows))) or {"ok": True, "readOnly": not allow_write, "rows": [], "rowCount": 0, "affectedRows": 1 if allow_write else None}
        self.panel.create_db_backup = lambda: backups.append(True) or {"path": "backup.dump", "bytes": 1}
        self.addCleanup(lambda: setattr(self.panel, "run_database_sql", original_runner))
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))
        self.patch_flag("DATABASE_QUERY_ENABLED", True)

        read = self.invoke_post_route("/api/ops/database/query", {"sql": "select 1", "max_rows": 50})
        self.assertEqual(read["errors"], [])
        self.assertTrue(read["json"]["readOnly"])
        self.assertEqual(backups, [])

        self.patch_flag("DATABASE_WRITE_ENABLED", True)
        handler, captured = self.make_route_handler("/api/ops/database/query")
        handler.require_mutations = lambda: None
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.validate_json_post = lambda request_handler, **kwargs: None
        self.panel.parse_body = lambda request_handler, **kwargs: {"sql": "update dune.items set stack_size=1 where false", "confirm": "EXECUTE DATABASE WRITE"}
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        handler.do_POST()
        self.assertEqual(captured["errors"], [])
        self.assertEqual(backups, [True])
        self.assertTrue(calls[-1][1])

    def test_backup_import_rejects_traversal_and_verifies_valid_archive(self):
        original_verify = self.panel.verify_backup_set
        self.panel.verify_backup_set = lambda path: {"ok": True, "path": path}
        self.addCleanup(lambda: setattr(self.panel, "verify_backup_set", original_verify))

        valid_buffer = io.BytesIO()
        with tarfile.open(fileobj=valid_buffer, mode="w:gz") as archive:
            payload = b"postgres custom dump fixture"
            info = tarfile.TarInfo("export/postgres-dune_sb_1_4_0_0.dump")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            manifest = b"created_utc=test\n"
            info = tarfile.TarInfo("export/manifest.txt")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
        encoded = base64.b64encode(valid_buffer.getvalue()).decode()
        result = self.panel.import_backup_archive("fixture.tar.gz", encoded, "")
        self.assertTrue(result["ok"])
        self.assertTrue((self.panel.BACKUPS_ROOT / result["path"] / "manifest.txt").exists())

        bad_buffer = io.BytesIO()
        with tarfile.open(fileobj=bad_buffer, mode="w:gz") as archive:
            payload = b"bad"
            info = tarfile.TarInfo("../escape.dump")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        with self.assertRaises(ValueError):
            self.panel.import_backup_archive("bad.tar.gz", base64.b64encode(bad_buffer.getvalue()).decode(), "")

    def test_native_restore_dry_run_resolves_current_backup_layout(self):
        backup_set = self.panel.BACKUPS_ROOT / "maintenance" / "fixture"
        backup_set.mkdir(parents=True)
        (backup_set / "postgres-dune_sb_1_4_0_0.dump").write_bytes(b"fixture")
        for name in ("server-saved.tgz", "rabbitmq.tgz", "config-and-env.tgz"):
            with tarfile.open(backup_set / name, "w:gz"):
                pass
        original_verify = self.panel.verify_backup_set
        self.panel.verify_backup_set = lambda path: {"ok": True, "path": path}
        self.addCleanup(lambda: setattr(self.panel, "verify_backup_set", original_verify))

        result = self.panel.restore_backup_set(
            "maintenance/fixture",
            {"serverSaved": True, "rabbitmq": True, "config": True, "tls": True},
            dry_run=True,
        )

        self.assertTrue(result["dryRun"])
        self.assertEqual(result["plan"]["postgres"], "postgres-dune_sb_1_4_0_0.dump")
        self.assertTrue(result["layers"]["serverSaved"])
        self.assertEqual(result["confirm"], "RESTORE BACKUP")

    def test_native_backup_verifier_enforces_full_coverage_manifest(self):
        backup_set = self.panel.BACKUPS_ROOT / "coverage-fixture"
        backup_set.mkdir(parents=True)
        (backup_set / "postgres.dump").write_bytes(b"fixture")
        (backup_set / "manifest.json").write_text(json.dumps({
            "artifacts": {"postgres": {"path": "postgres.dump"}},
            "coverage": {
                "schemaVersion": "dash-full-backup-coverage/v1",
                "required": ["postgres", "config"], "captured": ["postgres"],
                "missing": ["config"], "complete": False,
            },
        }), encoding="utf-8")
        original_which = self.panel.shutil.which
        self.panel.shutil.which = lambda name: None if name == "pg_restore" else original_which(name)
        self.addCleanup(lambda: setattr(self.panel.shutil, "which", original_which))

        ok, _stdout, stderr = self.panel.verify_backup_set_native(backup_set)

        self.assertFalse(ok)
        self.assertIn("coverage declaration is incomplete", stderr)

    def test_native_restore_stops_writers_backs_up_restores_and_restarts(self):
        backup_set = self.panel.BACKUPS_ROOT / "fixture"
        backup_set.mkdir(parents=True)
        (backup_set / "postgres-dune_sb_1_4_0_0.dump").write_bytes(b"fixture")
        originals = {
            "verify_backup_set": self.panel.verify_backup_set,
            "stop_restore_writers": self.panel.stop_restore_writers,
            "create_full_backup": self.panel.create_full_backup,
            "restore_postgres_dump": self.panel.restore_postgres_dump,
            "restore_selected_file_layers": self.panel.restore_selected_file_layers,
            "start_restore_writers": self.panel.start_restore_writers,
        }
        calls = []
        self.panel.verify_backup_set = lambda path: {"ok": True, "path": path}
        self.panel.stop_restore_writers = lambda: calls.append("stop") or [{"service": "survival", "containerId": "a" * 64}]
        self.panel.create_full_backup = lambda: calls.append("backup") or {"ok": True, "path": "maintenance/pre-restore"}
        self.panel.restore_postgres_dump = lambda path: calls.append("postgres") or {"ok": True}
        self.panel.restore_selected_file_layers = lambda artifacts, requested, staging: calls.append("layers") or {}
        self.panel.start_restore_writers = lambda stopped: calls.append("restart") or [{"ok": True, "service": "survival"}]
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))
        self.patch_db(lambda sql, params=None: [{"count": 0}])

        result = self.panel.restore_backup_set("fixture", {}, dry_run=False)

        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["stop", "backup", "postgres", "layers", "restart"])
        self.assertFalse(result["worldStartRequired"])

    def test_restore_post_hooks_only_reconcile_maps_that_were_running(self):
        original_action = self.panel.docker_container_action
        original_run = self.panel.subprocess.run
        starts = []
        commands = []
        self.panel.docker_container_action = lambda container_id, action, timeout=120: starts.append((container_id, action))
        self.panel.subprocess.run = lambda argv, **kwargs: commands.append((argv, kwargs["env"])) or types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        self.addCleanup(lambda: setattr(self.panel, "docker_container_action", original_action))
        self.addCleanup(lambda: setattr(self.panel.subprocess, "run", original_run))

        result = self.panel.start_restore_writers([
            {"service": "survival", "containerId": "a" * 64},
            {"service": "prometheus", "containerId": "b" * 64},
        ])

        self.assertEqual(len(starts), 2)
        self.assertEqual(commands[0][1]["DUNE_RESTART_SERVICES"], "survival")
        self.assertNotIn("deep-desert", commands[0][1]["DUNE_RESTART_SERVICES"])
        self.assertTrue(all(row["ok"] for row in result))

    def test_memory_balancer_transfers_one_gib_and_restores_baselines(self):
        gib = 1024 ** 3
        rows = [
            {"service": "survival", "containerId": "a" * 64, "memoryUsageBytes": int(9.5 * gib), "memoryLimitBytes": 10 * gib, "memoryPercent": 95.0},
            {"service": "deep-desert", "containerId": "b" * 64, "memoryUsageBytes": 2 * gib, "memoryLimitBytes": 10 * gib, "memoryPercent": 20.0},
        ]
        original_rows = self.panel.map_memory_rows
        original_update = self.panel.update_container_memory
        self.panel.map_memory_rows = lambda: [dict(row) for row in rows]
        updates = []
        self.panel.update_container_memory = lambda container_id, limit: updates.append((container_id, limit)) or {"ok": True, "limitBytes": limit}
        self.addCleanup(lambda: setattr(self.panel, "map_memory_rows", original_rows))
        self.addCleanup(lambda: setattr(self.panel, "update_container_memory", original_update))

        enabled = self.panel.set_memory_balancer_enabled(True)
        ticked = self.panel.memory_balancer_tick()
        disabled = self.panel.set_memory_balancer_enabled(False)

        self.assertTrue(enabled["enabled"])
        self.assertEqual(updates[:2], [("a" * 64, 11 * gib), ("b" * 64, 9 * gib)])
        self.assertIn("deep-desert -> survival", ticked["lastAction"])
        self.assertFalse(disabled["enabled"])
        self.assertIn(("a" * 64, 10 * gib), updates[2:])
        self.assertIn(("b" * 64, 10 * gib), updates[2:])

    def test_autoscaler_stops_idle_dynamic_map_and_restarts_on_demand(self):
        original_inventory = self.panel.docker_service_inventory
        original_counts = self.panel.autoscaler_player_counts
        original_control = self.panel.autoscaler_service_action
        original_travel_demand = self.panel.autoscaler_collect_travel_demand
        running = {"value": True}
        self.panel.docker_service_inventory = lambda: [{
            "service": "deep-desert", "state": "running" if running["value"] else "exited",
            "containerId": "a" * 12,
        }]
        self.panel.autoscaler_player_counts = lambda: {"deep-desert": 0}
        self.panel.autoscaler_collect_travel_demand = lambda state, now=None, inventory=None: []
        actions = []
        def control(service, action):
            actions.append((service, action))
            running["value"] = action == "start"
            return {"ok": True, "service": service, "action": action}
        self.panel.autoscaler_service_action = control
        self.addCleanup(lambda: setattr(self.panel, "docker_service_inventory", original_inventory))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_player_counts", original_counts))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_service_action", original_control))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_collect_travel_demand", original_travel_demand))
        state = self.panel.read_autoscaler_state()
        state.update({
            "enabled": True,
            "profile": "custom",
            "idleSeconds": 60,
            "retentionSeconds": 60,
            "retentionByService": {},
            "maxWarmDynamicMaps": 0,
            "modes": {"deep-desert": "dynamic"},
            "idleSince": {"deep-desert": 1},
            "demand": {},
        })
        self.panel.write_autoscaler_state(state)

        stopped = self.panel.autoscaler_tick()
        started = self.panel.autoscaler_control("demand", "deep-desert")

        self.assertEqual(actions, [("deep-desert", "stop"), ("deep-desert", "start")])
        self.assertFalse(stopped["lastError"])
        self.assertFalse(started["lastError"])
        self.assertIn("deep-desert", self.panel.read_autoscaler_state()["demand"])

    def test_autoscaler_start_uses_guarded_fast_start(self):
        original_control = self.panel.control_docker_service
        original_fast_start = self.panel.AUTOSCALER_FAST_START
        calls = []
        self.panel.AUTOSCALER_FAST_START = True
        self.panel.control_docker_service = lambda service, action, fast_dynamic_start=False: calls.append(
            (service, action, fast_dynamic_start)
        ) or {"ok": True}
        self.addCleanup(lambda: setattr(self.panel, "control_docker_service", original_control))
        self.addCleanup(lambda: setattr(self.panel, "AUTOSCALER_FAST_START", original_fast_start))

        result = self.panel.autoscaler_service_action("arrakeen", "start")

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("arrakeen", "start", True)])
        restart_source = (ROOT / "scripts" / "restart-target.sh").read_text(encoding="utf-8")
        patch_source = (ROOT / "scripts" / "patch-logoff-timers-runtime.sh").read_text(encoding="utf-8")
        self.assertIn("target_logoff_containers", restart_source)
        self.assertIn("required logoff timer runtime patch did not verify", restart_source)
        self.assertIn("processed_containers", patch_source)
        self.assertIn("selected running containers", patch_source)

    def test_autoscaler_mode_change_clears_stale_demand(self):
        state = self.panel.read_autoscaler_state()
        state.update({"enabled": False, "demand": {"arrakeen": time.time()}})
        self.panel.write_autoscaler_state(state)

        self.panel.autoscaler_control("set-mode", "arrakeen", "dynamic")

        self.assertNotIn("arrakeen", self.panel.read_autoscaler_state()["demand"])

    def test_autoscaler_profiles_cover_minimum_balanced_and_full_warm(self):
        minimum = self.panel.autoscaler_apply_profile({}, "minimum-footprint")
        balanced = self.panel.autoscaler_apply_profile({}, "balanced")
        full = self.panel.autoscaler_apply_profile({}, "full-warm")

        self.assertEqual(minimum["modes"]["survival"], "always-on")
        self.assertEqual(minimum["modes"]["arrakeen"], "dynamic")
        self.assertEqual(balanced["maxWarmDynamicMaps"], self.panel.AUTOSCALER_BALANCED_MAX_WARM_MAPS)
        self.assertEqual(balanced["retentionByService"]["arrakeen"], 2700)
        self.assertTrue(all(mode == "always-on" for mode in full["modes"].values()))
        self.assertEqual(self.panel.normalize_autoscaler_profile("adaptive"), "adaptive")
        self.assertEqual(self.panel.normalize_autoscaler_profile("invalid"), "balanced")

    def test_simulation_required_maps_are_forced_always_on(self):
        self.patch_flag("AUTOSCALER_SIMULATION_REQUIRED_SERVICES", {"deep-desert"})
        state = self.panel.autoscaler_apply_profile({}, "adaptive")
        self.assertEqual("always-on", state["modes"]["deep-desert"])
        state["modes"]["deep-desert"] = "dynamic"
        original_read = self.panel.read_care_package_state
        self.panel.read_care_package_state = lambda path, default: state
        self.addCleanup(lambda: setattr(self.panel, "read_care_package_state", original_read))
        self.assertEqual("always-on", self.panel.read_autoscaler_state()["modes"]["deep-desert"])

    def test_map_health_accepts_stopped_on_demand_and_requires_core_maps(self):
        original_state = self.panel.read_autoscaler_state
        self.panel.read_autoscaler_state = lambda: {
            "modes": {"survival": "always-on", "arrakeen": "dynamic"},
            "demand": {}, "demandTtlSeconds": 900,
        }
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_state))
        partitions = [
            {"partition_id": 1, "server_id": "s1", "map": "Survival", "blocked": False},
            {"partition_id": 3, "server_id": "s3", "map": "Arrakeen", "blocked": False},
        ]
        farm = [{"server_id": "s1", "ready": True, "alive": True}]
        rows = {row["service"]: row for row in self.handler.map_health_rows(farm, partitions, {"s1"}, {})}
        self.assertTrue(rows["survival"]["expectedOnline"])
        self.assertTrue(rows["survival"]["policySatisfied"])
        self.assertFalse(rows["arrakeen"]["expectedOnline"])
        self.assertTrue(rows["arrakeen"]["policySatisfied"])

    def test_autoscaler_balanced_budget_evicts_oldest_optional_warm_map(self):
        original_inventory = self.panel.docker_service_inventory
        original_counts = self.panel.autoscaler_player_counts
        original_demand = self.panel.autoscaler_collect_travel_demand
        original_action = self.panel.autoscaler_service_action
        services = ["arrakeen", "harko-village", "testing-hephaestus"]
        self.panel.docker_service_inventory = lambda: [{"service": service, "state": "running"} for service in services]
        self.panel.autoscaler_player_counts = lambda: {}
        self.panel.autoscaler_collect_travel_demand = lambda state, now=None, inventory=None: []
        actions = []
        self.panel.autoscaler_service_action = lambda service, action: actions.append((service, action)) or {"ok": True}
        self.addCleanup(lambda: setattr(self.panel, "docker_service_inventory", original_inventory))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_player_counts", original_counts))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_collect_travel_demand", original_demand))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_service_action", original_action))
        now = time.time()
        state = self.panel.read_autoscaler_state()
        state.update({
            "enabled": True, "profile": "balanced", "retentionSeconds": 3600,
            "maxWarmDynamicMaps": 2, "minAvailableMemoryBytes": 0,
            "modes": {service: "dynamic" for service in services},
            "idleSince": {service: now - 10 for service in services},
            "lastActivity": {"arrakeen": now - 300, "harko-village": now - 200, "testing-hephaestus": now - 100},
            "demand": {},
        })
        self.panel.write_autoscaler_state(state)

        result = self.panel.autoscaler_tick()

        self.assertFalse(result["lastError"])
        self.assertEqual(actions, [("arrakeen", "stop")])
        self.assertEqual(self.panel.read_autoscaler_state()["lastEvictionReason"]["arrakeen"], "warm-budget-lru")

    def test_autoscaler_memory_floor_evicts_only_until_available_recovers(self):
        originals = {
            "docker_service_inventory": self.panel.docker_service_inventory,
            "autoscaler_player_counts": self.panel.autoscaler_player_counts,
            "autoscaler_collect_travel_demand": self.panel.autoscaler_collect_travel_demand,
            "autoscaler_service_action": self.panel.autoscaler_service_action,
            "autoscaler_host_memory": self.panel.autoscaler_host_memory,
        }
        services = ["arrakeen", "harko-village"]
        self.panel.docker_service_inventory = lambda: [{"service": service, "state": "running"} for service in services]
        self.panel.autoscaler_player_counts = lambda: {}
        self.panel.autoscaler_collect_travel_demand = lambda state, now=None, inventory=None: []
        actions = []
        self.panel.autoscaler_service_action = lambda service, action: actions.append((service, action)) or {"ok": True}
        available = [8 * 1024 ** 3, 20 * 1024 ** 3]
        self.panel.autoscaler_host_memory = lambda: {
            "totalBytes": 64 * 1024 ** 3,
            "availableBytes": available.pop(0) if len(available) > 1 else available[0],
            "swapTotalBytes": 0, "swapFreeBytes": 0,
        }
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))
        now = time.time()
        state = self.panel.read_autoscaler_state()
        state.update({
            "enabled": True, "retentionSeconds": 3600,
            "maxWarmDynamicMaps": 0, "minAvailableMemoryBytes": 16 * 1024 ** 3,
            "modes": {service: "dynamic" for service in services},
            "idleSince": {service: now - 10 for service in services},
            "lastActivity": {"arrakeen": now - 300, "harko-village": now - 100},
            "demand": {},
        })
        self.panel.write_autoscaler_state(state)

        self.panel.autoscaler_tick()

        self.assertEqual(actions, [("arrakeen", "stop")])
        self.assertEqual(self.panel.read_autoscaler_state()["lastEvictionReason"]["arrakeen"], "memory-pressure-lru")

    def test_discord_adapter_role_mapping_and_sanitization(self):
        old = {name: os.environ.get(name) for name in (
            "DISCORD_OBSERVER_ROLE_IDS", "DISCORD_MODERATOR_ROLE_IDS",
            "DISCORD_ADMIN_ROLE_IDS", "DISCORD_OWNER_ROLE_IDS",
        )}
        os.environ["DISCORD_OBSERVER_ROLE_IDS"] = "observer"
        os.environ["DISCORD_MODERATOR_ROLE_IDS"] = "moderator"
        self.addCleanup(lambda: [os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value) for name, value in old.items()])
        actor = {"guildId": "1", "channelId": "2", "userId": "3", "username": "tester", "roleIds": ["moderator"]}

        self.assertEqual(self.panel.discord_require_tier(actor, "observer"), "moderator")
        self.assertEqual(self.panel.discord_require_tier(actor, "moderator"), "moderator")
        with self.assertRaises(PermissionError):
            self.panel.discord_require_tier(dict(actor, roleIds=[]), "observer")
        sanitized = self.panel.discord_sanitize({"database": "dune", "apiToken": "secret", "nested": {"password": "secret"}})
        self.assertEqual(sanitized["apiToken"], "[redacted]")
        self.assertEqual(sanitized["nested"]["password"], "[redacted]")

    def test_discord_ops_domains_are_allowlisted_and_bounded(self):
        original_query = self.panel.query
        self.panel.query = lambda sql, params=None: [{"active_last_1h": 2, "active_last_24h": 5, "active_last_7d": 8}]
        self.addCleanup(lambda: setattr(self.panel, "query", original_query))
        self.assertEqual(self.panel.discord_ops_result("activity")["active_last_1h"], 2)
        dashboard = self.panel.discord_ops_result("dashboard")
        self.assertTrue(dashboard["private"])
        self.assertNotIn("url", dashboard)
        prometheus = self.panel.discord_ops_result("prometheus")
        self.assertTrue(prometheus["private"])
        with self.assertRaises(ValueError):
            self.panel.discord_ops_result("shell")

    def test_addon_manifest_permissions_and_paths_are_bounded(self):
        manifest = self.panel.addon_admin.normalize_manifest({
            "schemaVersion": 1, "id": "ops-addon", "name": "Ops", "version": "1.0.0",
            "type": "ui", "entry": {"path": "web/index.html"},
            "permissions": {"ops": ["read"]},
        })
        self.assertEqual(manifest["permissions"], ["ops:read"])
        self.assertEqual(manifest["entry"]["path"], "web/index.html")
        with self.assertRaises(ValueError):
            self.panel.addon_admin.normalize_manifest({
                "id": "bad-addon", "name": "Bad", "version": "1", "type": "ui",
                "entry": {"path": "../escape.html"}, "permissions": [],
            })
        with self.assertRaises(ValueError):
            self.panel.addon_admin.normalize_manifest({
                "id": "bad-addon", "name": "Bad", "version": "1", "type": "ui",
                "entry": {"path": "index.html"}, "permissions": ["shell:execute"],
            })

    def test_service_control_route_is_separately_gated_and_audited(self):
        original_control = self.panel.control_docker_service
        calls = []
        self.panel.control_docker_service = lambda service, action: calls.append((service, action)) or {"ok": True, "service": service, "action": action, "exitCode": 0, "postState": {"state": "running"}}
        self.addCleanup(lambda: setattr(self.panel, "control_docker_service", original_control))
        self.patch_flag("SERVICE_CONTROL_ENABLED", True)
        handler, captured = self.make_route_handler("/api/ops/services/control")
        handler.require_mutations = lambda: None
        original_validate = self.panel.validate_json_post
        original_parse = self.panel.parse_body
        self.panel.validate_json_post = lambda request_handler, **kwargs: None
        self.panel.parse_body = lambda request_handler, **kwargs: {"service": "survival", "action": "restart", "confirm": "CONTROL SERVICE"}
        self.addCleanup(lambda: setattr(self.panel, "validate_json_post", original_validate))
        self.addCleanup(lambda: setattr(self.panel, "parse_body", original_parse))
        handler.do_POST()
        self.assertEqual(captured["errors"], [])
        self.assertEqual(calls, [("survival", "restart")])
        self.assertEqual(captured["audits"][0]["action"], "service-control")

    def test_care_package_preview_targets_selected_player(self):
        self.panel.CARE_PACKAGES_FILE.write_text(json.dumps({
            "schemaVersion": 1,
            "packages": [{
                "id": "starter",
                "label": "Starter",
                "enabled": True,
                "oncePerAccount": True,
                "items": [{"template_id": "BasicBuildingTool", "stack_size": 1}],
                "currency": [{"currency_id": 1, "amount": 500}],
                "xp": [{"track_type": "Combat", "amount": 100}],
            }],
        }), encoding="utf-8")
        self.patch_db(lambda sql, params=None: [{
            "account_id": 10,
            "character_name": "Tester",
            "online_status": "Offline",
            "player_controller_id": 200,
            "player_pawn_id": 201,
        }] if "from dune.player_state" in sql else [])
        bundles = []
        self.handler.economy_bundle = lambda body: bundles.append(body) or {"ok": True, "dryRun": True, "plan": []}

        result = self.handler.care_package_grant({"package_id": "starter", "account_id": 10, "dry_run": True})

        self.assertTrue(result["eligible"])
        self.assertEqual(bundles[0]["items"][0]["account_id"], 10)
        self.assertEqual(bundles[0]["currency"][0]["player_controller_id"], 200)
        self.assertEqual(bundles[0]["xp"][0]["player_id"], 200)

    def test_care_package_execution_is_gated_backed_up_and_recorded(self):
        self.panel.CARE_PACKAGES_FILE.write_text(json.dumps({
            "schemaVersion": 1,
            "packages": [{
                "id": "manual",
                "enabled": True,
                "items": [{"template_id": "BasicBuildingTool", "stack_size": 1}],
                "currency": [],
                "xp": [],
            }],
        }), encoding="utf-8")
        self.patch_db(lambda sql, params=None: [{
            "account_id": 10,
            "character_name": "Tester",
            "online_status": "Offline",
            "player_controller_id": 200,
            "player_pawn_id": 201,
        }] if "from dune.player_state" in sql else [])
        self.patch_flag("CARE_PACKAGES_ENABLED", True)
        self.patch_flag("BUNDLE_MUTATIONS_ENABLED", True)
        self.handler.require_mutations = lambda: None
        calls = []
        self.handler.economy_bundle = lambda body: calls.append(body) or {"ok": True, "dryRun": body.get("dry_run"), "plan": []}
        original_backup = self.panel.create_db_backup
        self.panel.create_db_backup = lambda: {"path": "backup.dump", "bytes": 4}
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))

        result = self.handler.care_package_grant({
            "package_id": "manual",
            "account_id": 10,
            "dry_run": False,
            "confirm": "GRANT CARE PACKAGE",
        })

        self.assertFalse(result["dryRun"])
        self.assertEqual(result["backup"]["path"], "backup.dump")
        self.assertEqual(calls[-1]["confirm"], "EXECUTE BUNDLE")
        history = self.panel.care_package_history()
        self.assertEqual(history[0]["packageId"], "manual")
        self.assertEqual(history[0]["accountId"], 10)

    def test_care_package_execution_refuses_disabled_package(self):
        self.panel.CARE_PACKAGES_FILE.write_text(json.dumps({
            "schemaVersion": 1,
            "packages": [{
                "id": "disabled",
                "enabled": False,
                "items": [{"template_id": "BasicBuildingTool", "stack_size": 1}],
                "currency": [],
                "xp": [],
            }],
        }), encoding="utf-8")
        self.patch_db(lambda sql, params=None: [{
            "account_id": 10,
            "character_name": "Tester",
            "online_status": "Offline",
            "player_controller_id": 200,
            "player_pawn_id": 201,
        }] if "from dune.player_state" in sql else [])
        self.patch_flag("CARE_PACKAGES_ENABLED", True)
        self.patch_flag("BUNDLE_MUTATIONS_ENABLED", True)
        self.handler.require_mutations = lambda: None
        self.handler.economy_bundle = lambda body: {"ok": True, "dryRun": True, "plan": []}

        with self.assertRaises(PermissionError):
            self.handler.care_package_grant({
                "package_id": "disabled",
                "account_id": 10,
                "dry_run": False,
                "confirm": "GRANT CARE PACKAGE",
            })

    def test_automatic_care_package_claim_prevents_duplicate_grant(self):
        self.panel.CARE_PACKAGES_FILE.write_text(json.dumps({
            "schemaVersion": 2,
            "automatic": {
                "enabled": True,
                "intervalSeconds": 60,
                "rules": [{
                    "id": "starter-first-online",
                    "enabled": True,
                    "packageId": "starter",
                    "grantWhen": "first_online",
                }],
            },
            "packages": [{
                "id": "starter",
                "enabled": True,
                "oncePerAccount": True,
                "items": [{"template_id": "BasicBuildingTool", "stack_size": 1}],
                "currency": [],
                "xp": [],
            }],
        }), encoding="utf-8")
        self.patch_db(lambda sql, params=None: [{
            "account_id": 10,
            "character_name": "Tester",
            "online_status": "Online",
            "player_controller_id": 200,
            "player_pawn_id": 201,
            "last_login_time": "2026-07-15T00:00:00+00:00",
        }] if "from dune.player_state" in sql else [])
        original_grant = self.panel.Handler.care_package_grant
        grants = []
        self.panel.Handler.care_package_grant = lambda handler, body: grants.append(body) or {
            "ok": True, "packageId": body["package_id"], "accountId": body["account_id"]
        }
        self.addCleanup(lambda: setattr(self.panel.Handler, "care_package_grant", original_grant))

        first = self.panel.care_package_auto_scan(dry_run=False)
        second = self.panel.care_package_auto_scan(dry_run=False)

        self.assertEqual(first["granted"], 1)
        self.assertEqual(second["granted"], 0)
        self.assertEqual(len(grants), 1)
        claims = json.loads(self.panel.CARE_PACKAGE_CLAIMS_FILE.read_text(encoding="utf-8"))
        self.assertIn("starter-first-online:starter:10", claims["claims"])

    def test_returning_player_is_persisted_pending_until_online(self):
        self.panel.CARE_PACKAGES_FILE.write_text(json.dumps({
            "schemaVersion": 2,
            "automatic": {
                "enabled": True,
                "intervalSeconds": 60,
                "rules": [{
                    "id": "returning",
                    "enabled": True,
                    "packageId": "return-kit",
                    "grantWhen": "last_seen",
                    "lastSeenDays": 30,
                }],
            },
            "packages": [{
                "id": "return-kit", "enabled": True, "oncePerAccount": True,
                "items": [], "currency": [{"currency_id": 1, "amount": 100}], "xp": [],
            }],
        }), encoding="utf-8")
        online = {"value": False}
        self.patch_db(lambda sql, params=None: [{
            "account_id": 10, "character_name": "Tester",
            "online_status": "Online" if online["value"] else "Offline",
            "player_controller_id": 200, "player_pawn_id": 201,
            "last_login_time": "2025-01-01T00:00:00+00:00",
        }] if "from dune.player_state" in sql else [])
        original_grant = self.panel.Handler.care_package_grant
        grants = []
        self.panel.Handler.care_package_grant = lambda handler, body: grants.append(body) or {"ok": True}
        self.addCleanup(lambda: setattr(self.panel.Handler, "care_package_grant", original_grant))

        offline_scan = self.panel.care_package_auto_scan(dry_run=False)
        online["value"] = True
        online_scan = self.panel.care_package_auto_scan(dry_run=False)

        self.assertEqual(offline_scan["pending"], 1)
        self.assertEqual(online_scan["granted"], 1)
        self.assertEqual(len(grants), 1)

    def test_native_player_command_contract_and_redaction(self):
        modules = self.panel.native_command_admin.load_catalog(ROOT / "config" / "admin-skill-modules.json")
        vehicles = self.panel.native_command_admin.load_catalog(ROOT / "config" / "admin-vehicles.json")
        skill, meta = self.panel.native_command_admin.build_inner(
            "skill-module", "FLS#123", {"module": modules[0]["id"], "level": 1}, modules, vehicles
        )
        self.assertEqual(skill["ServerCommand"], "SkillsSetModuleLevel")
        self.assertEqual(skill["PlayerId"], "FLS#123")
        self.assertEqual(meta["skillModule"]["id"], modules[0]["id"])
        spawned, _ = self.panel.native_command_admin.build_inner(
            "spawn-vehicle", "FLS#123",
            {"vehicle": vehicles[0]["id"], "x": 1, "y": 2, "z": 3, "rotation": 90},
            modules, vehicles,
        )
        self.assertEqual(spawned["ServerCommand"], "SpawnVehicleAt")
        self.assertEqual(spawned["Persistent"], 1.0)
        outer = self.panel.native_command_admin.build_outer("secret", spawned)
        self.assertEqual(outer["Version"], 2)
        self.assertEqual(outer["AuthToken"], "secret")
        preview = self.panel.native_command_admin.public_preview(outer)
        self.assertEqual(preview["AuthToken"], "<redacted>")
        self.assertNotIn("FLS#123", preview["MessageContent"])
        with self.assertRaises(ValueError):
            self.panel.native_command_admin.build_inner(
                "skill-points", "FLS#123", {"skill_points": 100001}, modules, vehicles
            )
        teleported, _ = self.panel.native_command_admin.build_inner(
            "teleport", "FLS#123", {"x": 1, "y": 2, "z": 3, "yaw": 90}, modules, vehicles
        )
        self.assertEqual(teleported["ServerCommand"], "TeleportTo")
        self.assertEqual(teleported["Yaw"], 90.0)
        self.assertEqual(
            self.panel.native_command_admin.build_inner("clean-inventory", "FLS#123", {}, modules, vehicles)[0]["ServerCommand"],
            "CleanPlayerInventory",
        )

    def test_native_notification_uses_game_rmq_heartbeats_notifications(self):
        original_service = self.panel.docker_service_container
        original_exec = self.panel.docker_container_exec
        old_token = os.environ.get("DUNE_SERVER_COMMANDS_AUTH_TOKEN")
        captured = {}
        self.panel.docker_service_container = lambda service, running=True: {"Id": "a" * 64}
        self.panel.docker_container_exec = lambda container, argv, timeout=20: captured.update(container=container, argv=argv) or {"ok": True, "exitCode": 0, "output": "publish=ok"}
        os.environ["DUNE_SERVER_COMMANDS_AUTH_TOKEN"] = "unit-test-token"
        self.addCleanup(lambda: setattr(self.panel, "docker_service_container", original_service))
        self.addCleanup(lambda: setattr(self.panel, "docker_container_exec", original_exec))
        self.addCleanup(lambda: os.environ.__setitem__("DUNE_SERVER_COMMANDS_AUTH_TOKEN", old_token) if old_token is not None else os.environ.pop("DUNE_SERVER_COMMANDS_AUTH_TOKEN", None))
        result = self.panel.publish_native_player_notification({"ServerCommand": "KickPlayer", "PlayerId": "*"})
        self.assertTrue(result["queued"])
        command = captured["argv"][2]
        self.assertIn('<<"heartbeats">>', command)
        self.assertIn('<<"notifications">>', command)
        self.assertNotIn("unit-test-token", command)

    def test_runtime_action_preview_resolves_funcom_id_without_publish(self):
        self.panel.ADMIN_SKILL_MODULES_FILE = ROOT / "config" / "admin-skill-modules.json"
        self.panel.ADMIN_VEHICLES_FILE = ROOT / "config" / "admin-vehicles.json"
        self.patch_db(lambda sql, params=None: [{
            "account_id": 10, "character_name": "Tester", "online_status": "Online",
            "player_controller_id": 20, "player_pawn_id": 21, "funcom_id": "FLS#123",
        }] if "join dune.accounts" in sql else [])
        original_publish = self.panel.publish_native_player_notification
        self.panel.publish_native_player_notification = lambda inner: (_ for _ in ()).throw(AssertionError("dry-run must not publish"))
        self.addCleanup(lambda: setattr(self.panel, "publish_native_player_notification", original_publish))
        result = self.handler.runtime_player_action({"action": "refill-water", "account_id": 10, "dry_run": True})
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["path"], "game-rmq:heartbeats/notifications")
        self.assertNotIn("FLS#123", json.dumps(result))

    def test_autoscaler_parses_director_travel_demand_once(self):
        line_a = "Processing travel queue for ClassicalInstancing group SH_Arrakeen (servers: [], num: 2)"
        line_b = "Received travel request for 1 player(s) to DeepDesert_1 (instancingMode=Dimension)"
        line_c = "Processing travel queue for DeepDesert_1 (02 PVE Hardcore, id=abc, dimension=1, partition=31, num=3)"
        events = self.panel.parse_director_travel_demand(f"{line_a}\n{line_a}\n{line_b}\n{line_c}\n")
        self.assertEqual(
            [(row["map"], row["count"]) for row in events],
            [("SH_Arrakeen", 2), ("DeepDesert_1", 1), ("DeepDesert_1", 3)],
        )
        self.assertEqual(len({row["id"] for row in events}), 3)

    def test_autoscaler_director_scan_uses_overlap_cursor_and_shared_inventory(self):
        line = "Processing travel queue for ClassicalInstancing group SH_Arrakeen (servers: [], num: 1)"
        requested_since = []
        original_logs = self.panel.docker_service_logs
        original_inventory = self.panel.docker_service_inventory
        original_query = self.panel.query
        previous_scan = self.panel.AUTOSCALER_RUNTIME.pop("travelScanAt", None)
        self.panel.docker_service_logs = lambda service, tail=200, since=None, inventory=None: requested_since.append(since) or {"logs": line}
        self.panel.docker_service_inventory = lambda: (_ for _ in ()).throw(AssertionError("shared inventory must be reused"))
        partition_id = self.panel.GAME_MAP_PARTITIONS["arrakeen"]
        self.panel.query = lambda sql, params=None: [{"partition_id": partition_id, "map": "SH_Arrakeen", "dimension_index": 0}]
        self.addCleanup(lambda: setattr(self.panel, "docker_service_logs", original_logs))
        self.addCleanup(lambda: setattr(self.panel, "docker_service_inventory", original_inventory))
        self.addCleanup(lambda: setattr(self.panel, "query", original_query))
        self.addCleanup(lambda: self.panel.AUTOSCALER_RUNTIME.pop("travelScanAt", None) if previous_scan is None else self.panel.AUTOSCALER_RUNTIME.__setitem__("travelScanAt", previous_scan))
        state = self.panel.read_autoscaler_state()
        state["modes"]["arrakeen"] = "dynamic"
        state["demandEvents"] = {}
        inventory = {"arrakeen": {"service": "arrakeen", "state": "exited"}}

        first = self.panel.autoscaler_collect_travel_demand(state, now=1000, inventory=inventory)
        second = self.panel.autoscaler_collect_travel_demand(state, now=1003, inventory=inventory)

        self.assertEqual(requested_since, [None, 999])
        self.assertEqual([(row["service"], row["action"]) for row in first], [("arrakeen", "travel-demand")])
        self.assertEqual(second, [])

    def test_autoscaler_idle_tick_skips_unchanged_state_write(self):
        import copy
        originals = {
            "maintenance_restart_is_executing": self.panel.maintenance_restart_is_executing,
            "read_autoscaler_state": self.panel.read_autoscaler_state,
            "write_autoscaler_state": self.panel.write_autoscaler_state,
            "docker_service_inventory": self.panel.docker_service_inventory,
            "autoscaler_collect_travel_demand": self.panel.autoscaler_collect_travel_demand,
            "autoscaler_player_counts": self.panel.autoscaler_player_counts,
            "autoscaler_public_state": self.panel.autoscaler_public_state,
        }
        state = self.panel.read_autoscaler_state()
        state.update({
            "enabled": True,
            "modes": {service: "always-on" for service in self.panel.GAME_MAP_SERVICES},
            "idleSince": {}, "demand": {}, "demandSource": {}, "demandEvents": {},
            "lastActivity": {}, "demandCount": {}, "lastEvictionReason": {},
        })
        writes = []
        self.panel.maintenance_restart_is_executing = lambda: False
        self.panel.read_autoscaler_state = lambda: copy.deepcopy(state)
        self.panel.write_autoscaler_state = lambda value: writes.append(copy.deepcopy(value))
        self.panel.docker_service_inventory = lambda: [{"service": "survival", "state": "running"}]
        self.panel.autoscaler_collect_travel_demand = lambda value, now=None, inventory=None: []
        self.panel.autoscaler_player_counts = lambda: {}
        self.panel.autoscaler_public_state = lambda include_inventory=False: {"enabled": True}
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        result = self.panel.autoscaler_tick()

        self.assertTrue(result["enabled"])
        self.assertEqual(writes, [])

    def test_autoscaler_fast_demand_tick_reuses_director_without_inventory_scan(self):
        import copy
        originals = {
            "maintenance_restart_is_executing": self.panel.maintenance_restart_is_executing,
            "read_autoscaler_state": self.panel.read_autoscaler_state,
            "write_autoscaler_state": self.panel.write_autoscaler_state,
            "docker_service_logs": self.panel.docker_service_logs,
            "docker_service_inventory": self.panel.docker_service_inventory,
        }
        state = self.panel.read_autoscaler_state()
        state["enabled"] = True
        state["demandEvents"] = {}
        cached = {"service": "director", "name": "director-1", "containerId": "a" * 12, "state": "running"}
        previous_cache = copy.deepcopy(self.panel.AUTOSCALER_DIRECTOR_CACHE)
        self.panel.AUTOSCALER_DIRECTOR_CACHE.clear()
        self.panel.AUTOSCALER_DIRECTOR_CACHE["inventory"] = cached
        seen = []
        self.panel.maintenance_restart_is_executing = lambda: False
        self.panel.read_autoscaler_state = lambda: copy.deepcopy(state)
        self.panel.write_autoscaler_state = lambda value: (_ for _ in ()).throw(AssertionError("unchanged scan must not write state"))
        self.panel.docker_service_logs = lambda service, tail=200, since=None, inventory=None: seen.append(inventory) or {
            "logs": "", "container": "director-1", "containerId": "a" * 12,
        }
        self.panel.docker_service_inventory = lambda: (_ for _ in ()).throw(AssertionError("idle fast scan must not enumerate Docker"))
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))
        self.addCleanup(lambda: self.panel.AUTOSCALER_DIRECTOR_CACHE.clear() or self.panel.AUTOSCALER_DIRECTOR_CACHE.update(previous_cache))

        result = self.panel.autoscaler_demand_tick()

        self.assertTrue(result["ok"])
        self.assertFalse(result["demandDetected"])
        self.assertEqual(seen, [[cached]])

    def test_autoscaler_worker_separates_fast_scan_from_full_reconcile(self):
        originals = {
            "MUTATIONS_ENABLED": self.panel.MUTATIONS_ENABLED,
            "AUTOSCALER_MUTATIONS_ENABLED": self.panel.AUTOSCALER_MUTATIONS_ENABLED,
            "AUTOSCALER_RECONCILE_SECONDS": self.panel.AUTOSCALER_RECONCILE_SECONDS,
            "read_autoscaler_state": self.panel.read_autoscaler_state,
            "autoscaler_tick": self.panel.autoscaler_tick,
            "autoscaler_demand_tick": self.panel.autoscaler_demand_tick,
        }
        self.panel.MUTATIONS_ENABLED = True
        self.panel.AUTOSCALER_MUTATIONS_ENABLED = True
        self.panel.AUTOSCALER_RECONCILE_SECONDS = 30
        self.panel.read_autoscaler_state = lambda: {"enabled": True}
        calls = []
        demand = {"value": False}
        self.panel.autoscaler_tick = lambda force=False, collect_demand=True: calls.append(("reconcile", collect_demand)) or {"ok": True}
        self.panel.autoscaler_demand_tick = lambda: calls.append(("demand", None)) or {"demandDetected": demand["value"]}
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        first = self.panel.autoscaler_worker_iteration(0, now_monotonic=100)
        second = self.panel.autoscaler_worker_iteration(first, now_monotonic=103)
        demand["value"] = True
        third = self.panel.autoscaler_worker_iteration(second, now_monotonic=106)

        self.assertEqual((first, second, third), (100, 100, 106))
        self.assertEqual(calls, [
            ("reconcile", True),
            ("demand", None),
            ("demand", None),
            ("reconcile", False),
        ])

    def test_autoscaler_worker_retries_failed_reconcile_on_next_poll(self):
        originals = {
            "MUTATIONS_ENABLED": self.panel.MUTATIONS_ENABLED,
            "AUTOSCALER_MUTATIONS_ENABLED": self.panel.AUTOSCALER_MUTATIONS_ENABLED,
            "AUTOSCALER_RECONCILE_SECONDS": self.panel.AUTOSCALER_RECONCILE_SECONDS,
            "read_autoscaler_state": self.panel.read_autoscaler_state,
            "autoscaler_tick": self.panel.autoscaler_tick,
            "autoscaler_demand_tick": self.panel.autoscaler_demand_tick,
        }
        self.panel.MUTATIONS_ENABLED = True
        self.panel.AUTOSCALER_MUTATIONS_ENABLED = True
        self.panel.AUTOSCALER_RECONCILE_SECONDS = 30
        self.panel.read_autoscaler_state = lambda: {"enabled": True}
        outcomes = [{"lastError": "docker unavailable"}, {"lastError": ""}]
        calls = []
        self.panel.autoscaler_tick = lambda force=False, collect_demand=True: calls.append(collect_demand) or outcomes.pop(0)
        self.panel.autoscaler_demand_tick = lambda: (_ for _ in ()).throw(AssertionError("a due reconcile must retry before a fast scan"))
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        failed = self.panel.autoscaler_worker_iteration(0, now_monotonic=100)
        retried = self.panel.autoscaler_worker_iteration(failed, now_monotonic=103)

        self.assertEqual((failed, retried), (0, 103))
        self.assertEqual(calls, [True, True])

    def test_autoscaler_demand_tick_records_internal_failure_for_retry_and_metrics(self):
        original_maintenance = self.panel.maintenance_restart_is_executing
        original_read = self.panel.read_autoscaler_state
        original_runtime = dict(self.panel.AUTOSCALER_RUNTIME)
        self.panel.maintenance_restart_is_executing = lambda: False
        self.panel.read_autoscaler_state = lambda: (_ for _ in ()).throw(RuntimeError("state unavailable"))
        self.addCleanup(lambda: setattr(self.panel, "maintenance_restart_is_executing", original_maintenance))
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_read))
        self.addCleanup(lambda: self.panel.AUTOSCALER_RUNTIME.clear() or self.panel.AUTOSCALER_RUNTIME.update(original_runtime))

        result = self.panel.autoscaler_demand_tick()

        self.assertFalse(result["ok"])
        self.assertFalse(result["demandDetected"])
        self.assertIn("state unavailable", result["error"])
        self.assertIn("state unavailable", self.panel.AUTOSCALER_RUNTIME["lastDemandError"])
        self.assertGreater(self.panel.AUTOSCALER_RUNTIME["lastDemandScanAt"], 0)

    def test_autoscaler_metrics_expose_dual_cadence_and_health_without_labels(self):
        import copy
        original_state = self.panel.read_autoscaler_state
        original_runtime = copy.deepcopy(self.panel.AUTOSCALER_RUNTIME)
        original_values = {
            "AUTOSCALER_POLL_SECONDS": self.panel.AUTOSCALER_POLL_SECONDS,
            "AUTOSCALER_RECONCILE_SECONDS": self.panel.AUTOSCALER_RECONCILE_SECONDS,
            "MUTATIONS_ENABLED": self.panel.MUTATIONS_ENABLED,
            "AUTOSCALER_MUTATIONS_ENABLED": self.panel.AUTOSCALER_MUTATIONS_ENABLED,
        }
        self.panel.read_autoscaler_state = lambda: {"enabled": True}
        self.panel.AUTOSCALER_POLL_SECONDS = 3
        self.panel.AUTOSCALER_RECONCILE_SECONDS = 30
        self.panel.MUTATIONS_ENABLED = True
        self.panel.AUTOSCALER_MUTATIONS_ENABLED = True
        self.panel.AUTOSCALER_RUNTIME.update({
            "demandScanCount": 12,
            "reconcileCount": 3,
            "lastDemandScanAt": 1_700_000_003.25,
            "lastReconcileAt": 1_700_000_000.5,
            "lastDemandError": "",
            "lastError": "docker unavailable",
            "maintenancePaused": False,
        })
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_state))
        self.addCleanup(lambda: self.panel.AUTOSCALER_RUNTIME.clear() or self.panel.AUTOSCALER_RUNTIME.update(original_runtime))
        for name, value in original_values.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        metrics = self.panel.autoscaler_prometheus()

        self.assertIn("dash_autoscaler_demand_scan_interval_seconds 3\n", metrics)
        self.assertIn("dash_autoscaler_reconcile_interval_seconds 30\n", metrics)
        self.assertIn("dash_autoscaler_demand_scans_total 12\n", metrics)
        self.assertIn("dash_autoscaler_reconciliations_total 3\n", metrics)
        self.assertIn("dash_autoscaler_demand_scan_error 0\n", metrics)
        self.assertIn("dash_autoscaler_reconcile_error 1\n", metrics)
        self.assertIn("dash_autoscaler_maintenance_paused 0\n", metrics)
        self.assertNotIn("{", metrics)

    def test_metrics_document_cache_reuses_only_within_bounded_window(self):
        original_seconds = self.panel.METRICS_DOCUMENT_CACHE_SECONDS
        original_cache = dict(self.panel.METRICS_DOCUMENT_CACHE)
        original_runtime = dict(self.panel.METRICS_DOCUMENT_CACHE_RUNTIME)
        self.panel.METRICS_DOCUMENT_CACHE_SECONDS = 30
        self.panel.METRICS_DOCUMENT_CACHE.clear()
        self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update({"hits": 0, "misses": 0, "staleHits": 0, "waits": 0})
        self.addCleanup(lambda: setattr(self.panel, "METRICS_DOCUMENT_CACHE_SECONDS", original_seconds))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE.clear() or self.panel.METRICS_DOCUMENT_CACHE.update(original_cache))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.clear() or self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update(original_runtime))

        self.assertIsNone(self.panel.metrics_document_cache_get("slo", now=100))
        self.panel.metrics_document_cache_put("slo", "dash_slo_collector_up 1\n", now=100)
        self.assertEqual("dash_slo_collector_up 1\n", self.panel.metrics_document_cache_get("slo", now=130))
        self.assertIsNone(self.panel.metrics_document_cache_get("slo", now=130.001))

        telemetry = self.panel.metrics_document_cache_prometheus()
        self.assertIn("dash_admin_metrics_document_cache_entries 1\n", telemetry)
        self.assertIn("dash_admin_metrics_document_cache_hits_total 1\n", telemetry)
        self.assertIn("dash_admin_metrics_document_cache_misses_total 2\n", telemetry)
        self.assertIn("dash_admin_metrics_document_cache_stale_hits_total 0\n", telemetry)
        self.assertIn("dash_admin_metrics_document_cache_waits_total 0\n", telemetry)
        self.assertIn("dash_admin_metrics_document_cache_deferred_total 0\n", telemetry)
        self.assertNotIn("{", telemetry)

    def test_metrics_document_cache_single_flight_serves_stale_during_refresh(self):
        original_seconds = self.panel.METRICS_DOCUMENT_CACHE_SECONDS
        original_cache = dict(self.panel.METRICS_DOCUMENT_CACHE)
        original_runtime = dict(self.panel.METRICS_DOCUMENT_CACHE_RUNTIME)
        self.panel.METRICS_DOCUMENT_CACHE_SECONDS = 30
        self.panel.METRICS_DOCUMENT_CACHE.clear()
        self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update({"hits": 0, "misses": 0, "staleHits": 0, "waits": 0})
        self.addCleanup(lambda: setattr(self.panel, "METRICS_DOCUMENT_CACHE_SECONDS", original_seconds))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE.clear() or self.panel.METRICS_DOCUMENT_CACHE.update(original_cache))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.clear() or self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update(original_runtime))

        self.assertIsNone(self.panel.metrics_document_cache_get("change", now=100))
        self.panel.metrics_document_cache_put("change", "old\n", now=100)
        self.assertIsNone(self.panel.metrics_document_cache_get("change", now=131))
        self.assertEqual("old\n", self.panel.metrics_document_cache_get("change", now=131.1))
        self.panel.metrics_document_cache_put("change", "new\n", now=132)
        self.assertEqual("new\n", self.panel.metrics_document_cache_get("change", now=132.1))
        self.assertEqual(1, self.panel.METRICS_DOCUMENT_CACHE_RUNTIME["staleHits"])

    def test_metrics_document_cache_single_flight_waits_for_initial_document(self):
        original_seconds = self.panel.METRICS_DOCUMENT_CACHE_SECONDS
        original_cache = dict(self.panel.METRICS_DOCUMENT_CACHE)
        original_runtime = dict(self.panel.METRICS_DOCUMENT_CACHE_RUNTIME)
        self.panel.METRICS_DOCUMENT_CACHE_SECONDS = 30
        self.panel.METRICS_DOCUMENT_CACHE.clear()
        self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update({"hits": 0, "misses": 0, "staleHits": 0, "waits": 0})
        self.addCleanup(lambda: setattr(self.panel, "METRICS_DOCUMENT_CACHE_SECONDS", original_seconds))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE.clear() or self.panel.METRICS_DOCUMENT_CACHE.update(original_cache))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.clear() or self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update(original_runtime))

        self.assertIsNone(self.panel.metrics_document_cache_get("desired", now=100))
        result = []
        waiter = threading.Thread(target=lambda: result.append(self.panel.metrics_document_cache_get("desired")))
        waiter.start()
        time.sleep(0.02)
        self.panel.metrics_document_cache_put("desired", "ready\n")
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(["ready\n"], result)
        self.assertEqual(1, self.panel.METRICS_DOCUMENT_CACHE_RUNTIME["waits"])

    def test_metrics_document_cache_defers_concurrent_initial_scrape(self):
        original_seconds = self.panel.METRICS_DOCUMENT_CACHE_SECONDS
        original_wait = self.panel.METRICS_DOCUMENT_CACHE_WAIT_SECONDS
        original_cache = dict(self.panel.METRICS_DOCUMENT_CACHE)
        original_runtime = dict(self.panel.METRICS_DOCUMENT_CACHE_RUNTIME)
        self.panel.METRICS_DOCUMENT_CACHE_SECONDS = 30
        self.panel.METRICS_DOCUMENT_CACHE_WAIT_SECONDS = 0
        self.panel.METRICS_DOCUMENT_CACHE.clear()
        self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update({"hits": 0, "misses": 0, "staleHits": 0, "waits": 0, "deferred": 0})
        self.addCleanup(lambda: setattr(self.panel, "METRICS_DOCUMENT_CACHE_SECONDS", original_seconds))
        self.addCleanup(lambda: setattr(self.panel, "METRICS_DOCUMENT_CACHE_WAIT_SECONDS", original_wait))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE.clear() or self.panel.METRICS_DOCUMENT_CACHE.update(original_cache))
        self.addCleanup(lambda: self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.clear() or self.panel.METRICS_DOCUMENT_CACHE_RUNTIME.update(original_runtime))

        self.assertIsNone(self.panel.metrics_document_cache_get("deferred"))
        result = self.panel.metrics_document_cache_get("deferred")
        self.assertEqual('dash_admin_metrics_document_deferred{document="deferred"} 1\n', result)
        self.assertEqual(1, self.panel.METRICS_DOCUMENT_CACHE_RUNTIME["deferred"])

    def test_operations_briefing_accepts_assured_backup_verified_field(self):
        original = self.panel.deployment_assurance_public_status
        self.panel.deployment_assurance_public_status = lambda: {
            "enabled": True, "ok": True, "latestReady": True,
            "openWindows": [], "overdueWindows": [],
            "latest": {"ready": True, "backup": {"verified": True, "path": "backup-1"}},
        }
        self.addCleanup(lambda: setattr(self.panel, "deployment_assurance_public_status", original))

        sources = {row["id"]: row for row in self.panel.operations_briefing_sources()}

        self.assertTrue(sources["verified-backup"]["healthy"])
        self.assertEqual("verified", sources["verified-backup"]["state"])
        self.assertIn("verified=True", sources["verified-backup"]["detail"])

    def test_operations_briefing_surfaces_automatic_backup_failures(self):
        original = self.panel.backup_schedule_public_state
        self.panel.backup_schedule_public_state = lambda state=None: {
            "enabled": True, "lastResult": {"ok": False}, "lastSuccess": 50,
            "consecutiveFailures": 2, "deferrals": 1, "nextRunIso": "2026-07-17T12:00:00Z",
        }
        self.addCleanup(lambda: setattr(self.panel, "backup_schedule_public_state", original))

        sources = {row["id"]: row for row in self.panel.operations_briefing_sources()}

        self.assertFalse(sources["backup-automation"]["healthy"])
        self.assertEqual("failed-2", sources["backup-automation"]["state"])
        self.assertEqual("critical", sources["backup-automation"]["severity"])

    def test_operations_briefing_readiness_excludes_its_own_probe(self):
        original = self.panel.feature_readiness_public_status
        self.panel.feature_readiness_public_status = lambda force=False, allow_stale=False: {
            "features": [
                {"id": "database", "state": "ready"},
                {"id": "operations-briefing", "state": "degraded"},
                {"id": "public-directory", "state": "external-blocked"},
            ],
            "summary": {"ready": 1, "degraded": 1, "external-blocked": 1, "total": 3},
        }
        self.addCleanup(lambda: setattr(self.panel, "feature_readiness_public_status", original))

        sources = {row["id"]: row for row in self.panel.operations_briefing_sources()}

        self.assertTrue(sources["feature-readiness"]["healthy"])
        self.assertEqual("ready", sources["feature-readiness"]["state"])
        self.assertIn("excluding briefing self-check", sources["feature-readiness"]["detail"])
        self.assertEqual("1-provider-blocked", sources["external-integrations"]["state"])

    def test_desired_state_briefing_reuses_last_verified_categorical_status(self):
        original_enabled = self.panel.DESIRED_STATE_ENABLED
        original_store = self.panel.desired_state_store
        original_cache = dict(self.panel.DESIRED_STATE_STATUS_CACHE)
        calls = []
        fake_store = type("Store", (), {
            "policy": {"pollSeconds": 60},
            "status": lambda self, limit=300: calls.append(limit) or {
                "ok": True, "state": "attested", "integrity": {"ok": True},
                "openFindings": [],
            },
        })()
        self.panel.DESIRED_STATE_ENABLED = True
        self.panel.desired_state_store = lambda: fake_store
        self.panel.DESIRED_STATE_STATUS_CACHE.clear()
        self.panel.DESIRED_STATE_STATUS_CACHE.update({"value": None, "updatedAt": 0.0})
        self.addCleanup(lambda: setattr(self.panel, "DESIRED_STATE_ENABLED", original_enabled))
        self.addCleanup(lambda: setattr(self.panel, "desired_state_store", original_store))
        self.addCleanup(lambda: self.panel.DESIRED_STATE_STATUS_CACHE.clear() or self.panel.DESIRED_STATE_STATUS_CACHE.update(original_cache))

        first = self.panel.desired_state_public_status()
        self.panel.DESIRED_STATE_STATUS_CACHE["updatedAt"] = 0.0
        stale = self.panel.desired_state_public_status(allow_stale=True)
        self.assertEqual("attested", first["state"])
        self.assertEqual("attested", stale["state"])
        self.assertEqual([300], calls)
        self.panel.invalidate_desired_state_status_cache()
        self.panel.desired_state_public_status(allow_stale=True)
        self.assertEqual([300, 300], calls)

    def test_operations_briefing_alert_source_uses_feedback_safe_summary(self):
        original = self.panel.alert_inbox_public_status
        self.panel.alert_inbox_public_status = lambda limit=1: {
            "enabled": True, "ok": True,
            "summary": {"active": 1, "critical": 1, "unacknowledged": 1},
            "briefingSummary": {
                "active": 0, "critical": 0, "unacknowledged": 0,
                "warning": 0, "feedbackExcluded": 1,
            },
            "collector": {"ageSeconds": 5},
            "delivery": {"signedWebhooksEnabled": True},
        }
        self.addCleanup(lambda: setattr(self.panel, "alert_inbox_public_status", original))

        source = {row["id"]: row for row in self.panel.operations_briefing_sources()}["alert-inbox"]

        self.assertTrue(source["healthy"])
        self.assertEqual("clear", source["state"])
        self.assertIn("briefing meta-alerts excluded=1", source["detail"])

    def test_operations_briefing_invalidation_is_immediate_and_coalesced(self):
        original_enabled = self.panel.OPERATIONS_BRIEFING_ENABLED
        original_runtime = dict(self.panel.OPERATIONS_BRIEFING_RUNTIME)
        def restore_runtime():
            self.panel.OPERATIONS_BRIEFING_RUNTIME.clear()
            self.panel.OPERATIONS_BRIEFING_RUNTIME.update(original_runtime)
        self.panel.OPERATIONS_BRIEFING_ENABLED = True
        self.panel.OPERATIONS_BRIEFING_WAKE_EVENT.clear()
        self.addCleanup(lambda: setattr(self.panel, "OPERATIONS_BRIEFING_ENABLED", original_enabled))
        self.addCleanup(restore_runtime)
        self.addCleanup(self.panel.OPERATIONS_BRIEFING_WAKE_EVENT.clear)

        self.assertTrue(self.panel.request_operations_briefing_refresh("audit:desired-state-drift-opened"))

        runtime = self.panel.OPERATIONS_BRIEFING_RUNTIME
        self.assertTrue(runtime["refreshPending"])
        self.assertIsNone(runtime["currentFingerprint"])
        self.assertEqual(original_runtime.get("invalidations", 0) + 1, runtime["invalidations"])
        self.assertEqual("audit:desired-state-drift-opened", runtime["lastInvalidationReason"])
        self.assertTrue(self.panel.OPERATIONS_BRIEFING_WAKE_EVENT.is_set())
        self.assertFalse(runtime["forceRefreshPending"])
        self.assertTrue(self.panel.request_operations_briefing_refresh("audit:backup-schedule", force=True))
        self.assertTrue(runtime["forceRefreshPending"])
        self.assertTrue(self.panel.audit_action_invalidates_operations_briefing("privileged-request-completed"))
        self.assertTrue(self.panel.audit_action_invalidates_operations_briefing("deployment-assurance-finished"))
        self.assertTrue(self.panel.audit_action_invalidates_operations_briefing("prometheus-alert-firing"))
        self.assertTrue(self.panel.audit_action_invalidates_operations_briefing("prometheus-alert-resolved"))
        self.assertTrue(self.panel.audit_action_invalidates_operations_briefing("prometheus-alert-acknowledged"))
        self.assertFalse(self.panel.audit_action_invalidates_operations_briefing("prometheus-alert-refiring"))
        self.assertFalse(self.panel.audit_action_invalidates_operations_briefing("operations-briefing-generated"))

    def test_operations_briefing_changed_source_cooldown_keeps_refresh_pending(self):
        original_sources = self.panel.operations_briefing_sources
        original_store = self.panel.operations_briefing_store
        original_runtime = dict(self.panel.OPERATIONS_BRIEFING_RUNTIME)
        def restore_runtime():
            self.panel.OPERATIONS_BRIEFING_RUNTIME.clear()
            self.panel.OPERATIONS_BRIEFING_RUNTIME.update(original_runtime)
        source = {
            "id": "test-source", "title": "Test source", "state": "ready",
            "healthy": True, "severity": "warning", "detail": "ready",
            "surface": "infrastructure:test",
        }
        latest = {
            "generatedAt": self.panel.operations_briefing.iso(self.panel.time.time() - 5),
            "sourceFingerprint": "a" * 64,
        }
        fake_store = type("Store", (), {
            "status": lambda self, fingerprint, limit=1, now=None: {
                "ok": True, "currentReady": False, "latest": dict(latest),
            },
        })()
        self.panel.operations_briefing_sources = lambda: [source]
        self.panel.operations_briefing_store = lambda: fake_store
        self.panel.OPERATIONS_BRIEFING_RUNTIME.update({"refreshPending": True, "invalidations": 1})
        self.addCleanup(lambda: setattr(self.panel, "operations_briefing_sources", original_sources))
        self.addCleanup(lambda: setattr(self.panel, "operations_briefing_store", original_store))
        self.addCleanup(restore_runtime)

        result = self.panel.run_operations_briefing()

        self.assertTrue(result["due"])
        self.assertTrue(result["sourceChanged"])
        self.assertFalse(result["generated"])
        self.assertGreater(result["retryAfterSeconds"], 0)
        self.assertTrue(self.panel.OPERATIONS_BRIEFING_RUNTIME["refreshPending"])

    def test_operations_briefing_forced_detail_refresh_records_unchanged_state(self):
        original_sources = self.panel.operations_briefing_sources
        original_store = self.panel.operations_briefing_store
        original_audit = self.panel.audit_event
        original_runtime = dict(self.panel.OPERATIONS_BRIEFING_RUNTIME)
        source = {
            "id": "backup-automation", "title": "Automatic backups", "state": "verified",
            "healthy": True, "severity": "critical", "detail": "next=05:00",
            "surface": "infrastructure:backups",
        }
        fingerprint = self.panel.operations_briefing.source_fingerprint([source])
        latest = {
            "generatedAt": self.panel.operations_briefing.iso(self.panel.time.time()),
            "sourceFingerprint": fingerprint,
        }
        panel = self.panel

        class FakeStore:
            def status(self, current_fingerprint, limit=1, now=None):
                return {"ok": True, "currentReady": True, "latest": dict(latest)}

            def record(self, sources, actor, now):
                return {
                    "document": {"receipt": {
                        "id": "operations-briefing-" + "a" * 32,
                        "generatedAt": panel.operations_briefing.iso(now),
                        "state": "ready", "score": 100, "actions": [],
                    }},
                    "verification": {"ok": True},
                    "evidencePath": "operations-briefing-test.signed.json",
                }

        self.panel.operations_briefing_sources = lambda: [source]
        self.panel.operations_briefing_store = lambda: FakeStore()
        self.panel.audit_event = lambda *args, **kwargs: None
        self.panel.OPERATIONS_BRIEFING_RUNTIME.update({
            "refreshPending": True, "forceRefreshPending": True, "invalidations": 1,
        })
        self.addCleanup(lambda: setattr(self.panel, "operations_briefing_sources", original_sources))
        self.addCleanup(lambda: setattr(self.panel, "operations_briefing_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "audit_event", original_audit))
        self.addCleanup(lambda: self.panel.OPERATIONS_BRIEFING_RUNTIME.clear() or self.panel.OPERATIONS_BRIEFING_RUNTIME.update(original_runtime))

        result = self.panel.run_operations_briefing()

        self.assertTrue(result["generated"])
        self.assertTrue(result["forceTriggered"])
        self.assertFalse(self.panel.OPERATIONS_BRIEFING_RUNTIME["refreshPending"])
        self.assertFalse(self.panel.OPERATIONS_BRIEFING_RUNTIME["forceRefreshPending"])

    def test_privileged_request_reconciliation_is_append_only_and_terminal(self):
        calls = []
        states = iter([
            {"id": "request-" + "a" * 32, "open": True, "admissionEventId": "audit-" + "b" * 32, "path": "/api/ops/deployment-assurance", "capability": "infrastructure.write"},
            {"id": "request-" + "a" * 32, "open": False},
        ])
        fake_store = type("Store", (), {"request_state": lambda self, request_id: next(states)})()
        original_store = self.panel.admin_audit_ledger
        original_event = self.panel.audit_event
        self.panel.admin_audit_ledger = lambda: fake_store
        self.panel.audit_event = lambda action, ok=True, **fields: calls.append((action, ok, fields)) or {"event": {"eventId": "audit-" + "c" * 32}}
        self.addCleanup(lambda: setattr(self.panel, "admin_audit_ledger", original_store))
        self.addCleanup(lambda: setattr(self.panel, "audit_event", original_event))

        result = self.panel.reconcile_privileged_request({
            "requestId": "request-" + "a" * 32,
            "outcome": "cancelled",
            "reason": "The abandoned change window was inspected and cancelled.",
            "evidence": "deployment-window-test",
        }, {"id": "infra-owner"})

        self.assertTrue(result["ok"])
        self.assertFalse(result["request"]["open"])
        self.assertEqual("privileged-request-reconciled", calls[0][0])
        self.assertEqual("cancelled", calls[0][2]["outcome"])
        self.assertTrue(calls[0][2]["_ledger_required"])

    def test_minimum_footprint_profile_keeps_only_core_always_on(self):
        original_inventory = self.panel.docker_service_inventory
        original_counts = self.panel.autoscaler_player_counts
        original_control = self.panel.autoscaler_service_action
        original_always_on = self.panel.AUTOSCALER_ALWAYS_ON_SERVICES
        self.panel.AUTOSCALER_ALWAYS_ON_SERVICES = {"survival", "overmap"}
        self.panel.docker_service_inventory = lambda: [
            {"service": "survival", "state": "running"},
            {"service": "overmap", "state": "running"},
            {"service": "arrakeen", "state": "running"},
        ]
        self.panel.autoscaler_player_counts = lambda: {}
        actions = []
        self.panel.autoscaler_service_action = lambda service, action: actions.append((service, action)) or {"ok": True}
        self.addCleanup(lambda: setattr(self.panel, "docker_service_inventory", original_inventory))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_player_counts", original_counts))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_service_action", original_control))
        self.addCleanup(lambda: setattr(self.panel, "AUTOSCALER_ALWAYS_ON_SERVICES", original_always_on))

        result = self.panel.autoscaler_control("minimum-footprint")
        state = self.panel.read_autoscaler_state()

        self.assertTrue(result["enabled"])
        self.assertEqual(state["modes"]["survival"], "always-on")
        self.assertEqual(state["modes"]["arrakeen"], "dynamic")
        self.assertEqual(actions, [])

    def test_backup_schedule_tick_creates_one_verified_backup(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.configure_backup_schedule({"enabled": True, "time": "05:00", "interval_hours": 12, "retention_days": 0})
        state = self.panel.read_backup_schedule()
        state["nextRun"] = 100
        self.panel.write_backup_schedule(state)
        original_create = self.panel.create_full_backup
        calls = []
        self.panel.create_full_backup = lambda verification_attempts=3: calls.append(verification_attempts) or {"ok": True, "path": "admin-panel/maintenance/test", "verification": {"ok": True}, "verificationAttempts": 1}
        self.addCleanup(lambda: setattr(self.panel, "create_full_backup", original_create))
        result = self.panel.backup_schedule_tick(now=200)
        second = self.panel.backup_schedule_tick(now=201)
        self.assertEqual(len(calls), 1)
        self.assertEqual(3, calls[0])
        self.assertTrue(result["lastResult"]["ok"])
        self.assertGreater(second["nextRun"], 201)

    def test_operations_calendar_combines_backup_restart_and_slo_windows(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        state = self.panel.default_backup_schedule()
        state.update({"enabled": True, "nextRun": 1200, "intervalHours": 24})
        self.panel.write_backup_schedule(state)
        self.panel.write_restart_state({
            "jobs": [{
                "id": "maintenance", "status": "scheduled", "runAt": 1250,
                "target": "all", "targetLabel": "All maps", "action": "restart",
                "execute": True, "backup": True, "updatePolicy": "current",
            }],
            "lastExecution": None,
        })
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {
            "maintenanceWindows": [{
                "id": "window", "starts_at": 1100, "ends_at": 8000,
                "reason": "planned update", "cancelled_at": None,
            }],
        }
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        status = self.panel.operations_calendar_public_status(now=1000, horizon_days=1)

        self.assertEqual(3, status["summary"]["windows"])
        self.assertEqual(1, status["summary"]["criticalConflicts"])
        self.assertEqual(0, status["summary"]["uncoveredDisruptive"])
        self.assertEqual("slo-maintenance:window", status["next"]["id"])

    def test_operations_calendar_surfaces_slo_collector_failure(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: (_ for _ in ()).throw(RuntimeError("SLO database unavailable"))
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        status = self.panel.operations_calendar_public_status(now=1000, horizon_days=1)
        metrics = self.panel.operations_calendar.prometheus(status, now=1000)

        self.assertFalse(status["ok"])
        self.assertEqual(1, status["summary"]["sourceErrors"])
        self.assertEqual("slo-maintenance", status["errors"][0]["source"])
        self.assertIn("dash_operations_calendar_collector_up 0\n", metrics)

        with self.assertRaisesRegex(RuntimeError, "admission failed closed"):
            self.panel.operations_calendar_restart_conflicts(1200, "all")

    def test_operations_calendar_does_not_hide_corrupt_scheduler_state(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        self.panel.BACKUP_SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.panel.BACKUP_SCHEDULE_FILE.write_text("{broken", encoding="utf-8")
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        status = self.panel.operations_calendar_public_status(now=1000, horizon_days=1)

        self.assertFalse(status["ok"])
        self.assertEqual("backup-schedule", status["errors"][0]["source"])

    def test_operations_calendar_keeps_overdue_work_due_now(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        state = self.panel.default_backup_schedule()
        state.update({"enabled": True, "nextRun": 100, "intervalHours": 24})
        self.panel.write_backup_schedule(state)
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        status = self.panel.operations_calendar_public_status(now=1000, horizon_days=1)

        backup = next(row for row in status["windows"] if row["source"] == "backup-schedule")
        self.assertEqual(1000, backup["startsAt"])
        self.assertIn(backup, status["current"])

    def test_restart_schedule_rejects_calendar_conflict_without_exact_override(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        now = self.panel.time.time()
        state = self.panel.default_backup_schedule()
        state.update({"enabled": True, "nextRun": now + 600, "intervalHours": 24})
        self.panel.write_backup_schedule(state)
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))
        body = {
            "target": "all", "action": "restart", "execute": True,
            "announce": False, "runAt": self.panel.operations_calendar.iso(now + 650),
        }

        with self.assertRaisesRegex(ValueError, "operations calendar"):
            self.panel.schedule_restart(body)

        with self.assertRaisesRegex(ValueError, "exact confirmation"):
            self.panel.schedule_restart(dict(body, allowCalendarConflict=True))

        job = self.panel.schedule_restart(dict(
            body, allowCalendarConflict=True,
            calendarConflictConfirm=self.panel.CONFIRM_CALENDAR_CONFLICT_OVERRIDE,
        ))
        self.assertTrue(job["calendarConflictOverride"])
        self.assertEqual(1, len(job["calendarConflicts"]))

    def test_daily_restart_allows_only_automatic_backup_calendar_overlap(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        now = self.panel.time.time()
        state = self.panel.default_backup_schedule()
        state.update({"enabled": True, "nextRun": now + 600, "intervalHours": 24})
        self.panel.write_backup_schedule(state)
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        job = self.panel.schedule_restart({
            "target": "all", "action": "restart", "execute": True,
            "announce": False, "automatic_daily_maintenance": True,
            "runAt": self.panel.operations_calendar.iso(now + 650),
        })

        self.assertTrue(job["calendarConflictAutoOverride"])
        self.assertFalse(job["calendarConflictOverride"])
        self.assertEqual(1, len(job["calendarConflicts"]))

    def test_daily_restart_key_is_idempotent_and_preserves_the_first_job(self):
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))
        run_at = self.panel.operations_calendar.iso(self.panel.time.time() + 3600)
        body = {
            "target": "all", "action": "restart", "execute": True,
            "announce": False, "automatic_daily_maintenance": True,
            "daily_maintenance_key": "2026-08-02",
            "runAt": run_at,
        }

        first = self.panel.schedule_restart(body)
        second = self.panel.schedule_restart(dict(body, message="retry after lost response"))

        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["automaticDailyMaintenance"])
        self.assertEqual("2026-08-02", first["automaticDailyMaintenanceKey"])
        state = self.panel.read_restart_state()
        self.assertEqual(1, len([job for job in state["jobs"] if job.get("status") == "scheduled"]))

    def test_restart_schedule_retains_warning_findings_without_blocking(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        now = self.panel.time.time()
        self.panel.write_event_state({
            "events": [{
                "id": "prewarm", "name": "Prewarm Arrakeen", "status": "scheduled",
                "nextRunAt": self.panel.operations_calendar.iso(now + 600),
                "repeatSeconds": 0, "runCount": 0, "maxRuns": 1,
                "plan": [{"type": "map-prewarm", "payload": {"service": "arrakeen"}}],
            }],
            "runs": [], "lastRun": None,
        })
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        job = self.panel.schedule_restart({
            "target": "all", "action": "restart", "execute": True,
            "announce": False, "runAt": self.panel.operations_calendar.iso(now + 650),
        })

        self.assertFalse(job["calendarConflictOverride"])
        self.assertEqual("warning", job["calendarConflicts"][0]["severity"])
        self.assertEqual(1, len(job["calendarCoverageFindings"]))

    def test_disruptive_restart_defers_when_operation_lock_is_owned(self):
        original_lock = self.panel.backup_operation_lock

        @self.panel.contextlib.contextmanager
        def busy_lock():
            raise self.panel.BackupOperationBusy("assured deployment active")
            yield

        self.panel.backup_operation_lock = busy_lock
        self.addCleanup(lambda: setattr(self.panel, "backup_operation_lock", original_lock))

        result = self.panel.execute_restart({"id": "restart", "execute": True, "action": "restart"})

        self.assertFalse(result["ok"])
        self.assertTrue(result["operationDeferred"])
        self.assertEqual(self.panel.RESTART_OPERATION_RETRY_SECONDS, result["retrySeconds"])

    def test_operation_lock_deferral_is_persisted_as_a_retry(self):
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.write_restart_state({
            "jobs": [{
                "id": "maintenance", "status": "executing", "runAt": 900,
                "lastError": "transient", "operationDeferrals": 2,
            }],
            "lastExecution": None,
        })
        result = {
            "ok": False, "operationDeferred": True, "retrySeconds": 300,
            "error": "assured deployment active",
        }

        persisted = self.panel.defer_restart_operation("maintenance", result, now=1000)
        state = self.panel.read_restart_state()
        job = state["jobs"][0]

        self.assertEqual("scheduled", job["status"])
        self.assertEqual(1300, job["runAt"])
        self.assertEqual(3, job["operationDeferrals"])
        self.assertEqual(1000, job["lastDeferredAt"])
        self.assertIsNone(job["lastError"])
        self.assertIsNone(state["lastExecution"])
        self.assertEqual(result, state["lastDeferral"]["result"])
        self.assertEqual(1300, state["lastDeferral"]["retryAt"])
        self.assertEqual(job["id"], persisted["job"]["id"])

    def test_executing_restart_cannot_be_superseded_or_cancelled(self):
        self.panel.RESTART_STATE_FILE = self.workspace / "backups" / "admin-panel" / "restart-jobs.json"
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.EVENT_STATE_FILE = self.workspace / "backups" / "admin-panel" / "events.json"
        self.panel.write_restart_state({
            "jobs": [{
                "id": "in-flight", "status": "executing", "runAt": self.panel.time.time() - 10,
                "target": "all", "targetLabel": "All maps", "action": "restart",
                "execute": True, "backup": True, "updatePolicy": "current",
            }],
            "lastExecution": None,
        })
        original_slo = self.panel.operational_slo_public_status
        self.panel.operational_slo_public_status = lambda: {"maintenanceWindows": []}
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_slo))

        with self.assertRaisesRegex(RuntimeError, "cannot replace maintenance job in-flight"):
            self.panel.schedule_restart({
                "target": "all", "action": "restart", "execute": False,
                "announce": False, "delay": "30min",
            })
        with self.assertRaisesRegex(RuntimeError, "cannot cancel maintenance job in-flight"):
            self.panel.cancel_restart("in-flight")

        state = self.panel.read_restart_state()
        self.assertEqual("executing", state["jobs"][0]["status"])
        self.assertEqual(1, len(state["jobs"]))

    def test_backup_schedule_failure_retains_verifier_detail_and_retries_soon(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.configure_backup_schedule({
            "enabled": True, "time": "05:00", "interval_hours": 24,
            "retry_minutes": 7, "verification_attempts": 2, "retention_days": 0,
        })
        state = self.panel.read_backup_schedule()
        state["nextRun"] = 100
        self.panel.write_backup_schedule(state)
        original_create = self.panel.create_full_backup
        verification = {"ok": False, "path": "admin-panel/maintenance/failed", "exitCode": 1, "stdout": "OK dump\n", "stderr": "FAIL authenticated head\n"}
        self.panel.create_full_backup = lambda verification_attempts=3: (_ for _ in ()).throw(
            self.panel.BackupVerificationError("admin-panel/maintenance/failed", verification, verification_attempts)
        )
        self.addCleanup(lambda: setattr(self.panel, "create_full_backup", original_create))

        result = self.panel.backup_schedule_tick(now=200)

        self.assertFalse(result["lastResult"]["ok"])
        self.assertEqual("BackupVerificationError", result["lastResult"]["errorType"])
        self.assertEqual("FAIL authenticated head\n", result["lastResult"]["verification"]["stderr"])
        self.assertEqual(2, result["lastResult"]["verificationAttempts"])
        self.assertEqual(1, result["consecutiveFailures"])
        self.assertEqual(200 + 7 * 60, result["nextRun"])

    def test_backup_schedule_defers_for_operation_lock_without_recording_failure(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        self.panel.configure_backup_schedule({"enabled": True, "time": "05:00", "retry_minutes": 9})
        state = self.panel.read_backup_schedule()
        state["nextRun"] = 100
        self.panel.write_backup_schedule(state)
        original_create = self.panel.create_full_backup
        self.panel.create_full_backup = lambda verification_attempts=3: (_ for _ in ()).throw(self.panel.BackupOperationBusy("deployment active"))
        self.addCleanup(lambda: setattr(self.panel, "create_full_backup", original_create))

        result = self.panel.backup_schedule_tick(now=200)

        self.assertIsNone(result["lastRun"])
        self.assertIsNone(result["lastResult"])
        self.assertEqual(1, result["deferrals"])
        self.assertEqual("deployment active", result["lastDeferral"])
        self.assertEqual(200 + 9 * 60, result["nextRun"])

    def test_full_backup_retries_verification_without_recreating_snapshot(self):
        backup_dir = self.panel.BACKUP_ROOT / "maintenance" / "retry-fixture"
        backup_dir.mkdir(parents=True)
        original_create = self.panel.create_maintenance_backup
        original_verify = self.panel.verify_backup_set
        original_lock = self.panel.backup_operation_lock
        original_delay = self.panel.BACKUP_VERIFY_RETRY_SECONDS
        created = []
        checks = []
        self.panel.create_maintenance_backup = lambda job: created.append(job) or {
            "path": str(backup_dir), "coverage": {"complete": True}, "warnings": [],
        }
        self.panel.verify_backup_set = lambda path: checks.append(path) or {
            "ok": len(checks) == 2, "path": path, "exitCode": 0 if len(checks) == 2 else 1,
            "stdout": "verified" if len(checks) == 2 else "", "stderr": "transient" if len(checks) == 1 else "",
        }
        self.panel.backup_operation_lock = self.panel.contextlib.nullcontext
        self.panel.BACKUP_VERIFY_RETRY_SECONDS = 0
        self.addCleanup(lambda: setattr(self.panel, "create_maintenance_backup", original_create))
        self.addCleanup(lambda: setattr(self.panel, "verify_backup_set", original_verify))
        self.addCleanup(lambda: setattr(self.panel, "backup_operation_lock", original_lock))
        self.addCleanup(lambda: setattr(self.panel, "BACKUP_VERIFY_RETRY_SECONDS", original_delay))

        result = self.panel.create_full_backup(verification_attempts=3)

        self.assertEqual(1, len(created))
        self.assertEqual(2, len(checks))
        self.assertEqual(2, result["verificationAttempts"])
        self.assertEqual([False, True], [row["ok"] for row in result["verificationHistory"]])

    def test_backup_schedule_metrics_are_label_free_and_expose_failure_posture(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        state = self.panel.default_backup_schedule()
        state.update({
            "enabled": True, "nextRun": 100, "lastRun": 80, "lastSuccess": 50,
            "lastResult": {"ok": False}, "consecutiveFailures": 2, "deferrals": 3,
        })
        self.panel.write_backup_schedule(state)
        original_runtime = dict(self.panel.BACKUP_SCHEDULE_RUNTIME)
        self.panel.BACKUP_SCHEDULE_RUNTIME.update({"running": True, "active": False})
        self.addCleanup(lambda: self.panel.BACKUP_SCHEDULE_RUNTIME.clear() or self.panel.BACKUP_SCHEDULE_RUNTIME.update(original_runtime))

        metrics = self.panel.backup_schedule_prometheus(now=200)

        self.assertIn("dash_backup_schedule_last_run_ok 0\n", metrics)
        self.assertIn("dash_backup_schedule_consecutive_failures 2\n", metrics)
        self.assertIn("dash_backup_schedule_overdue_seconds 100.0\n", metrics)
        self.assertNotIn("{", metrics)

    def test_backup_schedule_retention_never_deletes_failed_evidence(self):
        self.panel.BACKUP_SCHEDULE_FILE = self.workspace / "backups" / "admin-panel" / "backup-schedule.json"
        successful = self.panel.BACKUP_ROOT / "maintenance" / "successful"
        failed = self.panel.BACKUP_ROOT / "maintenance" / "failed"
        successful.mkdir(parents=True)
        failed.mkdir(parents=True)
        state = self.panel.default_backup_schedule()
        state.update({
            "retentionDays": 1,
            "runs": [
                {"createdAt": 1, "ok": True, "path": successful.relative_to(self.panel.BACKUPS_ROOT).as_posix()},
                {"createdAt": 1, "ok": False, "path": failed.relative_to(self.panel.BACKUPS_ROOT).as_posix()},
            ],
        })

        removed = self.panel.prune_scheduled_backups(state, now=200000)

        self.assertFalse(successful.exists())
        self.assertTrue(failed.exists())
        self.assertEqual(["admin-panel/maintenance/successful"], removed)

    def test_admin_backup_artifacts_are_private(self):
        artifact = self.workspace / "backups" / "admin-panel" / "private.dump"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"private")
        directory = artifact.parent / "maintenance-run"
        directory.mkdir()
        self.panel.secure_admin_backup_path(artifact)
        self.panel.secure_admin_backup_path(directory, directory=True)
        self.assertEqual(0o600, artifact.stat().st_mode & 0o777)
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)

    def test_backup_operation_lock_is_cross_open_exclusive_and_private(self):
        self.panel.BACKUP_OPERATION_LOCK_FILE = self.workspace / "backups" / "admin-panel" / "operation.lock"

        with self.panel.backup_operation_lock():
            with self.assertRaisesRegex(self.panel.BackupOperationBusy, "owns the operation lock"):
                with self.panel.backup_operation_lock():
                    self.fail("a second open must not acquire the operation lock")

        self.assertEqual(0o600, self.panel.BACKUP_OPERATION_LOCK_FILE.stat().st_mode & 0o777)

    def test_restore_drill_status_is_read_only_and_execution_is_separately_gated(self):
        original_status = self.panel.restore_drill_public_status
        original_queue = self.panel.queue_restore_drill
        self.panel.restore_drill_public_status = lambda: {"ok": True, "latest": {"id": "proof"}}
        queued = []
        self.panel.queue_restore_drill = lambda source=None: queued.append(source) or {"ok": True, "queued": True}
        self.addCleanup(lambda: setattr(self.panel, "restore_drill_public_status", original_status))
        self.addCleanup(lambda: setattr(self.panel, "queue_restore_drill", original_queue))

        handler, captured = self.make_route_handler("/api/ops/restore-drill")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("proof", captured["json"]["latest"]["id"])
        self.assertEqual([], queued)

        self.patch_flag("RESTORE_DRILL_EXECUTION_ENABLED", False)
        denied = self.invoke_post_route("/api/ops/restore-drill", {"confirm": "RUN ISOLATED RESTORE DRILL"})
        self.assertEqual(401, denied["errors"][0]["status"])
        self.assertEqual([], queued)

    def test_restore_drill_post_requires_exact_confirmation_then_queues(self):
        original_queue = self.panel.queue_restore_drill
        queued = []
        self.panel.queue_restore_drill = lambda source=None: queued.append(source) or {"ok": True, "queued": True}
        self.addCleanup(lambda: setattr(self.panel, "queue_restore_drill", original_queue))
        self.patch_flag("RESTORE_DRILL_EXECUTION_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", True)

        rejected = self.invoke_post_route("/api/ops/restore-drill", {"confirm": "yes"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        accepted = self.invoke_post_route("/api/ops/restore-drill", {
            "source": "backups/current.dump", "confirm": "RUN ISOLATED RESTORE DRILL",
        })
        self.assertTrue(accepted["json"]["queued"])
        self.assertEqual(["backups/current.dump"], queued)

    def test_rabbitmq_restore_drill_status_is_read_only_and_execution_is_separately_gated(self):
        original_status = self.panel.rabbitmq_restore_drill_public_status
        original_queue = self.panel.queue_rabbitmq_restore_drill
        self.panel.rabbitmq_restore_drill_public_status = lambda: {"ok": True, "latest": {"id": "rabbit-proof"}}
        queued = []
        self.panel.queue_rabbitmq_restore_drill = lambda backup_set=None: queued.append(backup_set) or {"ok": True, "queued": True}
        self.addCleanup(lambda: setattr(self.panel, "rabbitmq_restore_drill_public_status", original_status))
        self.addCleanup(lambda: setattr(self.panel, "queue_rabbitmq_restore_drill", original_queue))

        handler, captured = self.make_route_handler("/api/ops/rabbitmq-restore-drill")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("rabbit-proof", captured["json"]["latest"]["id"])
        self.assertEqual([], queued)

        self.patch_flag("RABBITMQ_RESTORE_DRILL_EXECUTION_ENABLED", False)
        denied = self.invoke_post_route(
            "/api/ops/rabbitmq-restore-drill",
            {"confirm": "RUN NETWORKLESS RABBITMQ RESTORE DRILL"},
        )
        self.assertEqual(401, denied["errors"][0]["status"])
        self.assertEqual([], queued)

    def test_rabbitmq_restore_drill_post_requires_exact_confirmation_then_queues(self):
        self.assertEqual(
            "infrastructure.write",
            self.panel.access_control.required_capability("POST", "/api/ops/rabbitmq-restore-drill"),
        )
        original_queue = self.panel.queue_rabbitmq_restore_drill
        queued = []
        self.panel.queue_rabbitmq_restore_drill = lambda backup_set=None: queued.append(backup_set) or {"ok": True, "queued": True}
        self.addCleanup(lambda: setattr(self.panel, "queue_rabbitmq_restore_drill", original_queue))
        self.patch_flag("RABBITMQ_RESTORE_DRILL_EXECUTION_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", True)

        rejected = self.invoke_post_route("/api/ops/rabbitmq-restore-drill", {"confirm": "yes"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        accepted = self.invoke_post_route("/api/ops/rabbitmq-restore-drill", {
            "backupSet": "backups/complete", "confirm": "RUN NETWORKLESS RABBITMQ RESTORE DRILL",
        })
        self.assertTrue(accepted["json"]["queued"])
        self.assertEqual(["backups/complete"], queued)

    def test_rabbitmq_restore_drill_metrics_are_label_free_and_anchor_aware(self):
        original_status = self.panel.rabbitmq_restore_drill_public_status
        self.panel.rabbitmq_restore_drill_public_status = lambda: {
            "ok": True,
            "featureEnabled": True,
            "ready": True,
            "runtime": {"running": False},
            "history": {"ok": True},
            "latest": {
                "id": "private-receipt-id",
                "ok": True,
                "integrityOk": True,
                "receiptHashValid": True,
                "backupAgeSeconds": 42,
                "finishedAt": "2026-07-17T04:00:00Z",
            },
        }
        self.addCleanup(lambda: setattr(self.panel, "rabbitmq_restore_drill_public_status", original_status))
        metrics = self.panel.rabbitmq_restore_drill_prometheus()
        self.assertIn("dash_rabbitmq_restore_drill_ok 1\n", metrics)
        self.assertIn("dash_rabbitmq_restore_drill_integrity_ok 1\n", metrics)
        self.assertIn("dash_rabbitmq_restore_drill_backup_age_seconds 42.0\n", metrics)
        self.assertNotIn("private-receipt-id", metrics)
        self.assertNotIn("{", metrics)

    def test_operational_slo_status_and_metrics_are_read_only(self):
        original_status = self.panel.operational_slo_public_status
        original_store = self.panel.operational_slo_store
        self.panel.operational_slo_public_status = lambda: {"ok": True, "overall": "healthy"}
        self.panel.operational_slo_store = lambda: type("Store", (), {"prometheus": lambda self: "dash_slo_collector_up 1\n"})()
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_public_status", original_status))
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_store", original_store))

        handler, captured = self.make_route_handler("/api/ops/slo")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("healthy", captured["json"]["overall"])

        handler, captured = self.make_route_handler("/metrics/slo")
        handler.is_app_route = lambda path: False
        texts = []
        handler.text = lambda value, content_type="", **kwargs: texts.append((value, content_type))
        handler.require_token = lambda: self.fail("bounded Prometheus SLO metrics must not require an admin credential")
        handler.do_GET()
        self.assertIn("dash_slo_collector_up 1\n", texts[0][0])
        self.assertIn("dash_admin_metrics_document_cache_seconds 60\n", texts[0][0])
        self.assertIn("text/plain", texts[0][1])

    def test_operational_slo_mutations_are_gated_confirmed_and_scoped(self):
        calls = []
        fake = type("Store", (), {
            "acknowledge": lambda self, incident, actor, note: calls.append(("ack", incident, actor, note)) or {"ok": True},
            "add_note": lambda self, incident, actor, note: calls.append(("note", incident, actor, note)) or {"ok": True},
            "create_maintenance": lambda self, start, end, reason, actor: calls.append(("create", start, end, reason, actor)) or {"ok": True, "id": "m1"},
            "cancel_maintenance": lambda self, identifier, actor: calls.append(("cancel", identifier, actor)) or {"ok": True, "id": identifier},
        })()
        original_store = self.panel.operational_slo_store
        self.panel.operational_slo_store = lambda: fake
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_store", original_store))
        self.patch_flag("MUTATIONS_ENABLED", True)

        self.patch_flag("OPERATIONAL_SLO_MUTATIONS_ENABLED", False)
        denied = self.invoke_post_route("/api/ops/slo", {"action": "acknowledge", "incidentId": "i1", "confirm": "ACKNOWLEDGE SLO INCIDENT"})
        self.assertEqual(401, denied["errors"][0]["status"])
        self.assertFalse(calls)
        self.panel.OPERATIONAL_SLO_MUTATIONS_ENABLED = True
        rejected = self.invoke_post_route("/api/ops/slo", {"action": "acknowledge", "incidentId": "i1", "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        accepted = self.invoke_post_route("/api/ops/slo", {"action": "acknowledge", "incidentId": "i1", "note": "working", "confirm": "ACKNOWLEDGE SLO INCIDENT"})
        self.assertTrue(accepted["json"]["ok"])
        self.assertEqual(("ack", "i1", "owner-recovery", "working"), calls[-1])

    def test_operational_slo_collector_uses_real_health_surfaces_and_fails_closed(self):
        original_inventory = self.panel.docker_service_inventory
        original_restart = self.panel.restart_online_snapshot
        original_select = self.panel.restore_drill.select_dump
        original_restore_status = self.panel.restore_drill.status
        original_rabbitmq_restore_status = self.panel.rabbitmq_restore_drill.status
        original_meminfo = self.panel.read_meminfo
        original_desired_state_store = self.panel.desired_state_store
        original_change_intelligence_store = self.panel.change_intelligence_store
        original_token = self.panel.ADMIN_TOKEN
        dump = self.workspace / "backups" / "latest.dump"
        dump.parent.mkdir(parents=True)
        dump.write_bytes(b"dump")
        self.panel.docker_service_inventory = lambda: [{"service": name, "state": "running"} for name in ("postgres", "director", "gateway", "admin-rmq", "game-rmq", "text-router")]
        self.panel.restart_online_snapshot = lambda: {"ok": True, "expected": 2, "readyOnline": 2}
        self.panel.restore_drill.select_dump = lambda root: dump
        self.panel.restore_drill.status = lambda root, limit=1: {"latest": {"id": "proof", "ok": True, "integrityOk": True, "policyOk": True, "receiptHashValid": True, "finishedAt": self.panel.datetime.datetime.now(self.panel.datetime.timezone.utc).isoformat(), "liveDatabaseTouched": False, "timings": {"restoreSeconds": 1}}}
        self.panel.rabbitmq_restore_drill.status = lambda root, limit=1: {
            "ok": True,
            "history": {"ok": True},
            "latest": {
                "id": "rabbit-proof", "ok": True, "integrityOk": True,
                "policyOk": True, "receiptHashValid": True,
                "receiptChainValid": True,
                "finishedAt": self.panel.datetime.datetime.now(self.panel.datetime.timezone.utc).isoformat(),
                "liveRabbitMQTouched": False, "networkCreated": False,
                "brokers": {
                    "admin": {"ok": True, "isolation": {"verified": True}},
                    "game": {"ok": True, "isolation": {"verified": True}},
                },
            },
        }
        self.panel.read_meminfo = lambda: {"availableBytes": 32 * 1024**3, "totalBytes": 64 * 1024**3}
        self.panel.desired_state_store = lambda: type("Store", (), {
            "status": lambda self, **kwargs: {
                "state": "attested",
                "sealed": True,
                "openFindings": [],
                "integrity": {"ok": True},
            },
        })()
        self.panel.change_intelligence_store = lambda: type("Store", (), {
            "verify": lambda self: {"ok": True, "eventCount": 10},
        })()
        self.panel.ADMIN_TOKEN = "real-token"
        self.patch_flag("ADMIN_REQUIRE_TOKEN", True)
        self.patch_db(lambda sql, params=None: [{"ready": True}])
        self.addCleanup(lambda: setattr(self.panel, "docker_service_inventory", original_inventory))
        self.addCleanup(lambda: setattr(self.panel, "restart_online_snapshot", original_restart))
        self.addCleanup(lambda: setattr(self.panel.restore_drill, "select_dump", original_select))
        self.addCleanup(lambda: setattr(self.panel.restore_drill, "status", original_restore_status))
        self.addCleanup(lambda: setattr(self.panel.rabbitmq_restore_drill, "status", original_rabbitmq_restore_status))
        self.addCleanup(lambda: setattr(self.panel, "read_meminfo", original_meminfo))
        self.addCleanup(lambda: setattr(self.panel, "desired_state_store", original_desired_state_store))
        self.addCleanup(lambda: setattr(self.panel, "change_intelligence_store", original_change_intelligence_store))
        self.addCleanup(lambda: setattr(self.panel, "ADMIN_TOKEN", original_token))
        collected = self.panel.collect_operational_slo_signals()
        self.assertTrue(all(collected["signals"].values()), collected)
        self.assertTrue(collected["signals"]["rabbitmq_restore_proof_ready"])
        self.assertTrue(collected["context"]["rabbitmqRestoreProof"]["brokerCopiesReady"])
        proof = collected["context"]["rabbitmqRestoreProof"]
        self.assertTrue(self.panel._rabbitmq_restore_proof_ready(proof))
        for key in ("integrityOk", "policyOk", "receiptHashValid", "receiptChainValid", "historyValid", "brokerCopiesReady"):
            invalid = dict(proof)
            invalid[key] = False
            self.assertFalse(self.panel._rabbitmq_restore_proof_ready(invalid), key)
        stale = dict(proof)
        stale["ageSeconds"] = self.panel.OPERATIONAL_SLO_RABBITMQ_RESTORE_PROOF_MAX_AGE_HOURS * 3600 + 1
        self.assertFalse(self.panel._rabbitmq_restore_proof_ready(stale))
        self.panel.restart_online_snapshot = lambda: (_ for _ in ()).throw(RuntimeError("farm unavailable"))
        failed = self.panel.collect_operational_slo_signals()
        self.assertFalse(failed["signals"]["required_maps_ready"])
        self.assertIn("farm unavailable", failed["context"]["errors"]["requiredMaps"])
        self.panel.rabbitmq_restore_drill.status = lambda root, limit=1: {
            "ok": True, "history": {"ok": False},
            "latest": {"ok": True, "integrityOk": True, "policyOk": True, "receiptHashValid": True},
        }
        failed = self.panel.collect_operational_slo_signals()
        self.assertFalse(failed["signals"]["rabbitmq_restore_proof_ready"])

    def test_capacity_status_metrics_and_apply_route(self):
        original_status = self.panel.capacity_intelligence_public_status
        original_store = self.panel.capacity_intelligence_store
        original_apply = self.panel.capacity_apply_recommendations
        self.panel.capacity_intelligence_public_status = lambda: {"ok": True, "recommendations": {}}
        self.panel.capacity_intelligence_store = lambda: type("Store", (), {"prometheus": lambda self: "dash_capacity_collector_up 1\n"})()
        applied = []
        self.panel.capacity_apply_recommendations = lambda actor, source: applied.append((actor, source)) or {"ok": True, "applied": True, "changes": [{"service": "arrakeen"}], "receipt": {"id": "r1"}}
        self.addCleanup(lambda: setattr(self.panel, "capacity_intelligence_public_status", original_status))
        self.addCleanup(lambda: setattr(self.panel, "capacity_intelligence_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "capacity_apply_recommendations", original_apply))

        handler, captured = self.make_route_handler("/api/ops/capacity")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertTrue(captured["json"]["ok"])

        handler, captured = self.make_route_handler("/metrics/capacity")
        handler.is_app_route = lambda path: False
        texts = []
        handler.text = lambda value, content_type="", **kwargs: texts.append((value, content_type))
        handler.require_token = lambda: self.fail("bounded capacity metrics must not require an admin credential")
        handler.do_GET()
        self.assertIn("dash_capacity_collector_up 1\n", texts[0][0])
        self.assertIn("dash_capacity_prewarm_schedules 0\n", texts[0][0])
        self.assertIn("dash_capacity_prewarm_failures_total 0\n", texts[0][0])

        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("AUTOSCALER_MUTATIONS_ENABLED", True)
        rejected = self.invoke_post_route("/api/ops/capacity", {"action": "apply-recommendations", "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        accepted = self.invoke_post_route("/api/ops/capacity", {"action": "apply-recommendations", "confirm": "APPLY CAPACITY RECOMMENDATIONS"})
        self.assertTrue(accepted["json"]["applied"])
        self.assertEqual(("owner-recovery", "manual"), applied[-1])

    def test_capacity_collector_uses_autoscaler_inventory_players_and_readiness(self):
        original_state = self.panel.read_autoscaler_state
        original_inventory = self.panel.docker_service_inventory
        original_counts = self.panel.autoscaler_player_counts
        original_services = self.panel.GAME_MAP_SERVICES
        self.panel.GAME_MAP_SERVICES = ("survival", "arrakeen")
        self.panel.read_autoscaler_state = lambda: {
            "modes": {"survival": "always-on", "arrakeen": "dynamic"},
            "demand": {"arrakeen": time.time()}, "demandTtlSeconds": 900,
            "retentionByService": {"arrakeen": 600}, "retentionSeconds": 900,
        }
        self.panel.docker_service_inventory = lambda: [{"service": "survival", "state": "running"}, {"service": "arrakeen", "state": "running"}]
        self.panel.autoscaler_player_counts = lambda: {"survival": 1, "arrakeen": 0}
        self.patch_db(lambda sql, params=None: [{"partition_id": 1, "ready": True}, {"partition_id": 2, "ready": False}])
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_state))
        self.addCleanup(lambda: setattr(self.panel, "docker_service_inventory", original_inventory))
        self.addCleanup(lambda: setattr(self.panel, "autoscaler_player_counts", original_counts))
        self.addCleanup(lambda: setattr(self.panel, "GAME_MAP_SERVICES", original_services))
        rows = {row["service"]: row for row in self.panel.collect_capacity_intelligence_maps()}
        self.assertTrue(rows["survival"]["ready"])
        self.assertTrue(rows["arrakeen"]["demanded"])
        self.assertEqual(rows["arrakeen"]["retentionSeconds"], 600)

    def test_desired_state_status_metrics_and_mutations_are_gated_and_confirmed(self):
        original_status = self.panel.desired_state_public_status
        original_store = self.panel.desired_state_store
        original_seal = self.panel.desired_state_seal
        calls = []
        fake_store = type("Store", (), {
            "prometheus": lambda self: "dash_desired_state_collector_up 1\n",
            "acknowledge": lambda self, finding_id, actor, note: calls.append(("ack", finding_id, actor, note)) or {"ok": True, "id": finding_id},
        })()
        self.panel.desired_state_public_status = lambda: {"ok": True, "state": "attested"}
        self.panel.desired_state_store = lambda: fake_store
        self.panel.desired_state_seal = lambda actor, reason: calls.append(("seal", actor, reason)) or {"ok": True, "baselineId": "desired-1"}
        self.addCleanup(lambda: setattr(self.panel, "desired_state_public_status", original_status))
        self.addCleanup(lambda: setattr(self.panel, "desired_state_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "desired_state_seal", original_seal))

        handler, captured = self.make_route_handler("/api/ops/desired-state")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("attested", captured["json"]["state"])

        handler, captured = self.make_route_handler("/metrics/desired-state")
        handler.is_app_route = lambda path: False
        texts = []
        handler.text = lambda value, content_type="", **kwargs: texts.append((value, content_type))
        handler.require_token = lambda: self.fail("bounded desired-state metrics must not require an admin credential")
        handler.do_GET()
        self.assertIn("dash_desired_state_collector_up 1\n", texts[0][0])
        self.assertIn("dash_admin_metrics_document_cache_seconds 60\n", texts[0][0])

        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("DESIRED_STATE_MUTATIONS_ENABLED", False)
        disabled = self.invoke_post_route("/api/ops/desired-state", {"action": "seal", "reason": "reviewed", "confirm": "SEAL DESIRED STATE"})
        self.assertEqual(401, disabled["errors"][0]["status"])
        self.assertFalse(calls)
        self.panel.DESIRED_STATE_MUTATIONS_ENABLED = True
        rejected = self.invoke_post_route("/api/ops/desired-state", {"action": "seal", "reason": "reviewed", "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        accepted = self.invoke_post_route("/api/ops/desired-state", {"action": "seal", "reason": "reviewed deployment", "confirm": "SEAL DESIRED STATE"})
        self.assertEqual("desired-1", accepted["json"]["baselineId"])
        self.assertEqual(("seal", "owner-recovery", "reviewed deployment"), calls[-1])
        acknowledged = self.invoke_post_route("/api/ops/desired-state", {"action": "acknowledge", "findingId": "drift-1", "note": "owned", "confirm": "ACKNOWLEDGE CONFIGURATION DRIFT"})
        self.assertTrue(acknowledged["json"]["ok"])
        self.assertEqual(("ack", "drift-1", "owner-recovery", "owned"), calls[-1])

    def test_desired_state_container_collector_confines_project_and_detects_label_race(self):
        original_docker_api = self.panel.docker_api
        container_id = "a" * 64
        calls = []

        def docker_api(path, *args, **kwargs):
            calls.append(path)
            if path.startswith("/containers/json?"):
                return [{"Id": container_id}]
            return {"Config": {"Labels": {"com.docker.compose.project": self.panel.DOCKER_COMPOSE_PROJECT}}}

        self.panel.docker_api = docker_api
        self.addCleanup(lambda: setattr(self.panel, "docker_api", original_docker_api))
        rows = self.panel.desired_state_container_inspections()
        self.assertEqual(1, len(rows))
        self.assertIn("com.docker.compose.project", calls[0])
        self.assertEqual(f"/containers/{container_id}/json", calls[1])
        self.panel.docker_api = lambda path, *args, **kwargs: ([{"Id": container_id}] if path.startswith("/containers/json?") else {"Config": {"Labels": {"com.docker.compose.project": "other"}}})
        with self.assertRaisesRegex(RuntimeError, "label changed"):
            self.panel.desired_state_container_inspections()

    def test_desired_state_seal_emits_resolution_evidence_for_every_closed_finding(self):
        events = []
        observations = []
        snapshot = {"schemaVersion": 1, "files": {}, "containers": {}}
        fake_store = type("Store", (), {
            "status": lambda self, limit=1000: {"openFindings": [{"id": "drift-one"}, {"id": "drift-two"}]},
            "seal": lambda self, value, actor, reason: {"ok": True, "baselineId": "desired-new"},
            "observe": lambda self, value, maintenance_active=False: observations.append((value, maintenance_active)) or {"ok": True},
        })()
        originals = {
            "collect_desired_state_snapshot": self.panel.collect_desired_state_snapshot,
            "desired_state_store": self.panel.desired_state_store,
            "desired_state_maintenance_active": self.panel.desired_state_maintenance_active,
            "audit_event": self.panel.audit_event,
        }
        self.panel.collect_desired_state_snapshot = lambda: snapshot
        self.panel.desired_state_store = lambda: fake_store
        self.panel.desired_state_maintenance_active = lambda: False
        self.panel.audit_event = lambda action, ok=True, **data: events.append((action, ok, data))
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        result = self.panel.desired_state_seal("operator", "reviewed")
        self.assertEqual("desired-new", result["baselineId"])
        self.assertEqual([(snapshot, False)], observations)
        self.assertEqual(["drift-one", "drift-two"], [row[2]["finding_id"] for row in events])
        self.assertTrue(all(row[0] == "desired-state-drift-resolved" and row[1] for row in events))
        self.assertTrue(all(row[2]["resolution_source"] == "baseline-sealed" for row in events))

    def test_infrastructure_mounts_desired_state_review_workflow(self):
        source = self.panel.INDEX
        self.assertIn("mountInfrastructureDesiredState(desiredStateData)", source)
        self.assertIn("Acknowledgement records ownership in the signed event chain", source)
        self.assertIn("SEAL DESIRED STATE", source)
        self.assertIn("ACKNOWLEDGE CONFIGURATION DRIFT", source)

    def test_change_intelligence_status_metrics_and_capsule_are_read_only(self):
        original_status = self.panel.change_intelligence_public_status
        original_store = self.panel.change_intelligence_store
        original_deployment_store = self.panel.deployment_assurance_store
        original_deployment_status = self.panel.deployment_assurance_public_status
        original_update_store = self.panel.update_readiness_store
        original_update_snapshot = self.panel.update_readiness_metrics_snapshot
        original_update_status = self.panel.update_readiness_public_status
        original_maintenance_store = self.panel.maintenance_outcome_store
        fake_store = type("Store", (), {
            "prometheus": lambda self, status=None: "dash_change_intelligence_collector_up 1\n",
            "capsule": lambda self, key: {"ok": True, "incidentKey": key, "causalityClaimed": False},
            "signed_capsule": lambda self, key: {"schemaVersion": 2, "incidentKey": key, "signature": "a" * 64},
        })()
        fake_deployment_store = type("DeploymentStore", (), {"prometheus": lambda self: "dash_deployment_assurance_collector_up 1\n"})()
        fake_update_store = type("UpdateStore", (), {"prometheus": lambda self, snapshot: "dash_update_readiness_collector_up 1\n"})()
        fake_maintenance_store = type("MaintenanceStore", (), {"prometheus": lambda self: "dash_maintenance_outcome_collector_up 1\n"})()
        self.panel.change_intelligence_public_status = lambda: {"ok": True, "state": "active", "eventCount": 3}
        self.panel.change_intelligence_store = lambda: fake_store
        self.panel.deployment_assurance_store = lambda: fake_deployment_store
        self.panel.deployment_assurance_public_status = lambda: {"ok": True, "state": "active", "latestReady": True}
        self.panel.update_readiness_store = lambda: fake_update_store
        self.panel.update_readiness_metrics_snapshot = lambda: {}
        self.panel.update_readiness_public_status = lambda **kwargs: {"ok": True, "currentReceiptReady": True, "applyReady": True}
        self.panel.maintenance_outcome_store = lambda: fake_maintenance_store
        self.addCleanup(lambda: setattr(self.panel, "change_intelligence_public_status", original_status))
        self.addCleanup(lambda: setattr(self.panel, "change_intelligence_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "deployment_assurance_store", original_deployment_store))
        self.addCleanup(lambda: setattr(self.panel, "deployment_assurance_public_status", original_deployment_status))
        self.addCleanup(lambda: setattr(self.panel, "update_readiness_store", original_update_store))
        self.addCleanup(lambda: setattr(self.panel, "update_readiness_metrics_snapshot", original_update_snapshot))
        self.addCleanup(lambda: setattr(self.panel, "update_readiness_public_status", original_update_status))
        self.addCleanup(lambda: setattr(self.panel, "maintenance_outcome_store", original_maintenance_store))

        handler, captured = self.make_route_handler("/api/ops/change-intelligence")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("active", captured["json"]["state"])

        handler, captured = self.make_route_handler("/api/ops/change-intelligence/capsule?incidentKey=slo%3Aone")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("slo:one", captured["json"]["incidentKey"])
        self.assertFalse(captured["json"]["causalityClaimed"])

        handler, captured = self.make_route_handler("/api/ops/change-intelligence/capsule?incidentKey=slo%3Aone&signed=true")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual(2, captured["json"]["schemaVersion"])
        self.assertEqual("a" * 64, captured["json"]["signature"])

        handler, captured = self.make_route_handler("/api/ops/deployment-assurance")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertTrue(captured["json"]["latestReady"])

        handler, captured = self.make_route_handler("/api/ops/update-readiness")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertTrue(captured["json"]["currentReceiptReady"])

        handler, captured = self.make_route_handler("/metrics/change-intelligence")
        handler.is_app_route = lambda path: False
        texts = []
        handler.text = lambda value, content_type="", **kwargs: texts.append((value, content_type))
        handler.require_token = lambda: self.fail("bounded change-intelligence metrics must not require an admin credential")
        handler.do_GET()
        self.assertIn("dash_change_intelligence_collector_up 1\n", texts[0][0])
        self.assertIn("dash_deployment_assurance_collector_up 1\n", texts[0][0])
        self.assertIn("dash_change_approval_enabled 0\n", texts[0][0])
        self.assertIn("dash_change_approval_ledger_valid 1\n", texts[0][0])
        self.assertIn("dash_admin_audit_ledger_enabled 1\n", texts[0][0])
        self.assertIn("dash_admin_audit_ledger_valid 1\n", texts[0][0])
        self.assertIn("dash_change_contract_enabled 1\n", texts[0][0])
        self.assertIn("dash_change_contract_refused_total", texts[0][0])
        self.assertIn("dash_public_directory_enabled 0\n", texts[0][0])
        self.assertIn("dash_public_directory_entry_current 0\n", texts[0][0])
        self.assertIn("dash_update_readiness_collector_up 1\n", texts[0][0])
        self.assertIn("dash_maintenance_outcome_collector_up 1\n", texts[0][0])

    def test_change_intelligence_verified_status_is_single_flight_cached_and_forceable(self):
        original_store = self.panel.change_intelligence_store
        original_cache = dict(self.panel.CHANGE_INTELLIGENCE_STATUS_CACHE)
        calls = []
        fake_store = type("Store", (), {
            "status": lambda self: calls.append("status") or {
                "ok": True, "state": "active", "eventCount": 10000,
                "openIncidents": [], "recentEvents": [], "integrity": {"ok": True},
                "readinessCertification": None,
            },
        })()
        self.panel.change_intelligence_store = lambda: fake_store
        self.panel.CHANGE_INTELLIGENCE_STATUS_CACHE.update({"value": None, "updatedAt": 0.0})
        self.addCleanup(lambda: setattr(self.panel, "change_intelligence_store", original_store))
        self.addCleanup(lambda: self.panel.CHANGE_INTELLIGENCE_STATUS_CACHE.update(original_cache))

        first = self.panel.change_intelligence_public_status()
        second = self.panel.change_intelligence_public_status()
        forced = self.panel.change_intelligence_public_status(force=True)
        self.assertTrue(first["integrity"]["ok"] and second["integrity"]["ok"] and forced["integrity"]["ok"])
        self.assertEqual(["status", "status"], calls)
        self.assertEqual(self.panel.CHANGE_INTELLIGENCE_STATUS_CACHE_SECONDS, first["cacheSeconds"])

    def test_operational_slo_status_is_single_flight_cached_and_forceable(self):
        original_store = self.panel.operational_slo_store
        original_cache = dict(self.panel.OPERATIONAL_SLO_STATUS_CACHE)
        calls = []
        fake_store = type("Store", (), {
            "status": lambda self, limit=200: calls.append("status") or {
                "ok": True, "overall": "healthy", "openIncidents": [], "objectives": [],
            },
            "integrity_check": lambda self: calls.append("integrity") or {"ok": True},
        })()
        self.panel.operational_slo_store = lambda: fake_store
        self.panel.OPERATIONAL_SLO_STATUS_CACHE.update({"value": None, "updatedAt": 0.0})
        self.addCleanup(lambda: setattr(self.panel, "operational_slo_store", original_store))
        self.addCleanup(lambda: self.panel.OPERATIONAL_SLO_STATUS_CACHE.update(original_cache))

        first = self.panel.operational_slo_public_status()
        second = self.panel.operational_slo_public_status()
        forced = self.panel.operational_slo_public_status(force=True)
        self.assertTrue(first["integrity"]["ok"] and second["integrity"]["ok"] and forced["integrity"]["ok"])
        self.assertEqual(["status", "integrity", "status", "integrity"], calls)
        self.assertEqual(self.panel.OPERATIONAL_SLO_STATUS_CACHE_SECONDS, first["cacheSeconds"])

    def test_update_readiness_metrics_never_run_expensive_collection_inline(self):
        original_cache = dict(self.panel.UPDATE_READINESS_SNAPSHOT_CACHE)
        original_runtime = dict(self.panel.UPDATE_READINESS_REFRESH_RUNTIME)
        self.panel.UPDATE_READINESS_SNAPSHOT_CACHE.update({"at": 0.0, "value": None})
        self.panel.UPDATE_READINESS_REFRESH_RUNTIME.update({"running": False, "lastAt": None, "lastError": ""})
        self.addCleanup(lambda: self.panel.UPDATE_READINESS_SNAPSHOT_CACHE.update(original_cache))
        self.addCleanup(lambda: self.panel.UPDATE_READINESS_REFRESH_RUNTIME.update(original_runtime))
        with mock.patch.object(self.panel.threading, "Thread") as thread:
            result = self.panel.update_readiness_metrics_snapshot()
        self.assertIsNone(result)
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        self.assertEqual("update-readiness-refresh", thread.call_args.kwargs["name"])

    def test_update_readiness_latency_budget_is_visible_and_alerted(self):
        self.assertIn("evidenceCollection:readinessCollection.durationMs", self.panel.INDEX)
        self.assertIn("packageInspection:packageInspection.durationMs", self.panel.INDEX)
        rules = (ROOT / "config" / "metrics" / "rules" / "dash.yml").read_text(encoding="utf-8")
        self.assertIn("DashUpdateReadinessCollectionSlow", rules)
        self.assertIn("dash_update_readiness_collection_duration_seconds > 15", rules)
        self.assertIn("DashUpdateReadinessPackageInspectionSlow", rules)
        self.assertIn("dash_update_readiness_package_inspection_duration_seconds > 5", rules)
        self.assertIn("DashMaintenanceOutcomeCollectorInvalid", rules)
        self.assertIn("DashMaintenanceOutcomeFailed", rules)

    def test_audit_event_feeds_change_intelligence_and_protects_reserved_fields(self):
        events = []
        original_record = self.panel.change_intelligence_record_event
        original_enabled = self.panel.CHANGE_INTELLIGENCE_ENABLED
        original_dispatcher = self.panel.WEBHOOK_DISPATCHER
        original_log = self.panel.AUDIT_LOG
        self.panel.change_intelligence_record_event = lambda event: events.append(event) or {"ok": True, "id": "change-1"}
        self.panel.CHANGE_INTELLIGENCE_ENABLED = True
        self.panel.WEBHOOK_DISPATCHER = type("Dispatcher", (), {"enqueue": lambda self, event: None})()
        self.panel.AUDIT_LOG = self.workspace / "backups" / "audit.jsonl"
        self.addCleanup(lambda: setattr(self.panel, "change_intelligence_record_event", original_record))
        self.addCleanup(lambda: setattr(self.panel, "CHANGE_INTELLIGENCE_ENABLED", original_enabled))
        self.addCleanup(lambda: setattr(self.panel, "WEBHOOK_DISPATCHER", original_dispatcher))
        self.addCleanup(lambda: setattr(self.panel, "AUDIT_LOG", original_log))

        self.panel.audit_event("service-control", ok=True, ts="forged", eventId="forged", service="director")
        self.assertEqual(1, len(events))
        self.assertEqual("service-control", events[0]["action"])
        self.assertNotEqual("forged", events[0]["ts"])
        self.assertTrue(events[0]["eventId"].startswith("audit-"))
        self.assertEqual("director", events[0]["service"])
        persisted = json.loads(self.panel.AUDIT_LOG.read_text(encoding="utf-8"))
        self.assertEqual(events[0], persisted)

    def test_change_intelligence_catches_up_existing_audit_history_idempotently(self):
        policy = self.workspace / "config" / "change-intelligence.json"
        policy.write_text((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"), encoding="utf-8")
        secret_dir = self.workspace / "config" / "secrets"
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret = secret_dir / "change-intelligence-hmac.secret"
        secret.write_text("a" * 64 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        audit = self.workspace / "backups" / "admin-panel" / "audit.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text(
            json.dumps({"ts": "2026-07-16T10:00:00Z", "action": "settings-write", "ok": True, "method": "POST"}) + "\n" +
            json.dumps({"ts": "2026-07-16T10:05:00Z", "action": "slo-incident-opened", "ok": False, "incident_id": "old-incident"}) + "\n",
            encoding="utf-8",
        )
        originals = {
            "CHANGE_INTELLIGENCE_POLICY": self.panel.CHANGE_INTELLIGENCE_POLICY,
            "CHANGE_INTELLIGENCE_DATABASE": self.panel.CHANGE_INTELLIGENCE_DATABASE,
            "CHANGE_INTELLIGENCE_SECRET_FILE": self.panel.CHANGE_INTELLIGENCE_SECRET_FILE,
            "CHANGE_INTELLIGENCE_STORE": self.panel.CHANGE_INTELLIGENCE_STORE,
            "AUDIT_LOG": self.panel.AUDIT_LOG,
        }
        self.panel.CHANGE_INTELLIGENCE_POLICY = policy
        self.panel.CHANGE_INTELLIGENCE_DATABASE = self.workspace / "backups" / "change-intelligence" / "change.sqlite3"
        self.panel.CHANGE_INTELLIGENCE_SECRET_FILE = secret
        self.panel.CHANGE_INTELLIGENCE_STORE = None
        self.panel.AUDIT_LOG = audit
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        first = self.panel.change_intelligence_import_history()
        second = self.panel.change_intelligence_import_history()
        status = self.panel.change_intelligence_store().status()
        self.assertEqual(2, first["imported"])
        self.assertEqual(0, second["imported"])
        self.assertEqual(2, second["duplicates"])
        self.assertEqual(2, status["eventCount"])
        self.assertEqual("slo:old-incident", status["openIncidents"][0]["incidentKey"])

    def test_change_intelligence_reconciles_only_stale_authoritative_incidents(self):
        events = []
        fake_change = type("ChangeStore", (), {"status": lambda self: {"openIncidents": [
            {"incidentKey": "desired:drift-active"},
            {"incidentKey": "desired:drift-stale"},
            {"incidentKey": "slo:slo-stale"},
            {"incidentKey": "event:unmanaged"},
        ]}})()
        fake_desired = type("DesiredStore", (), {"status": lambda self, limit=1000: {"openFindings": [{"id": "drift-active"}]}})()
        fake_slo = type("SloStore", (), {"status": lambda self, limit=1000: {"openIncidents": []}})()
        originals = {
            "CHANGE_INTELLIGENCE_ENABLED": self.panel.CHANGE_INTELLIGENCE_ENABLED,
            "DESIRED_STATE_ENABLED": self.panel.DESIRED_STATE_ENABLED,
            "OPERATIONAL_SLO_ENABLED": self.panel.OPERATIONAL_SLO_ENABLED,
            "change_intelligence_store": self.panel.change_intelligence_store,
            "desired_state_store": self.panel.desired_state_store,
            "operational_slo_store": self.panel.operational_slo_store,
            "audit_event": self.panel.audit_event,
        }
        self.panel.CHANGE_INTELLIGENCE_ENABLED = True
        self.panel.DESIRED_STATE_ENABLED = True
        self.panel.OPERATIONAL_SLO_ENABLED = True
        self.panel.change_intelligence_store = lambda: fake_change
        self.panel.desired_state_store = lambda: fake_desired
        self.panel.operational_slo_store = lambda: fake_slo
        self.panel.audit_event = lambda action, ok=True, **data: events.append((action, ok, data))
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        result = self.panel.change_intelligence_reconcile_incidents()
        self.assertEqual(2, result["reconciled"])
        self.assertEqual(
            [("desired-state-drift-resolved", "drift-stale"), ("slo-incident-resolved", "slo-stale")],
            [(action, data.get("finding_id") or data.get("incident_id")) for action, _ok, data in events],
        )
        self.assertTrue(all(ok and data["resolution_source"] == "authoritative-startup-reconciliation" for _action, ok, data in events))

    def test_infrastructure_mounts_change_intelligence_without_causality_claim(self):
        source = self.panel.INDEX
        self.assertIn("mountInfrastructureChangeIntelligence(changeData)", source)
        self.assertIn("not a claim that the change caused the incident", source)
        self.assertIn("/api/ops/change-intelligence/capsule", source)
        self.assertIn("Download signed capsule", source)
        self.assertIn("infrastructureChangeCapsuleExport", source)
        self.assertIn("deterministic response plan", source)
        self.assertIn("responsePlanJump", source)
        self.assertIn("Response-plan diagnostic", source)
        self.assertIn("Fleet-wide response readiness", source)
        self.assertIn("Certify all runbooks", source)
        self.assertIn("/api/ops/change-intelligence/certify", source)
        self.assertIn("Assured Change Windows", source)
        self.assertIn("assured-control-plane-deploy.sh", source)
        self.assertIn("/api/ops/deployment-assurance", source)
        self.assertIn("mountInfrastructureRabbitMQRestoreDrill(rabbitmqDrillData)", source)
        self.assertIn("/api/ops/rabbitmq-restore-drill", source)
        self.assertIn("RUN NETWORKLESS RABBITMQ RESTORE DRILL", source)
        self.assertIn("names withheld", source)
        panel_source = (ROOT / "admin" / "admin_panel.py").read_text(encoding="utf-8")
        self.assertIn("result = restore_drill.run_drill(\n                ROOT,", panel_source)
        assurance_block = panel_source.split("def deployment_assurance_store():", 1)[1].split("def deployment_assurance_container_snapshot():", 1)[0]
        self.assertIn("DEPLOYMENT_ASSURANCE_WORKSPACE", assurance_block)
        self.assertNotIn("\n                ROOT,", assurance_block)

    def test_response_policy_reuses_only_existing_diagnostics_gates_and_confirmations(self):
        policy = self.panel.change_intelligence.load_policy(ROOT / "config" / "change-intelligence.json")
        confirmations = {
            self.panel.CONFIRM_SERVICE_CONTROL,
            self.panel.CONFIRM_RESTORE_DRILL,
            self.panel.CONFIRM_RABBITMQ_RESTORE_DRILL,
            self.panel.CONFIRM_CAPACITY_APPLY,
            self.panel.CONFIRM_DESIRED_STATE_SEAL,
            self.panel.CONFIRM_BACKUP_RESTORE,
        }
        mutation_steps = []
        for runbook in policy["response"]["runbooks"]:
            for step in runbook["steps"]:
                if step.get("commandId"):
                    self.assertIn(step["commandId"], self.panel.command_console.COMMANDS)
                if step["mutation"]:
                    mutation_steps.append(step)
                    self.assertIn(step["featureGate"], self.panel.ENV_KEY_DEFINITIONS)
                    if step.get("confirmation"):
                        self.assertIn(step["confirmation"], confirmations)
        self.assertGreaterEqual(len(mutation_steps), 9)
        self.assertTrue(all(step["requiredCapability"] != "read" for step in mutation_steps))

    def test_response_readiness_drill_runs_only_fixed_diagnostics_and_records_digest_receipt(self):
        plan = {
            "incidentKey": "slo:one", "runbookId": "control-plane-availability",
            "planSha256": "a" * 64, "policySha256": "b" * 64, "state": "requires-operator-review",
            "steps": [
                {"id": "diagnose", "commandId": "rmq-health", "mutation": False},
                {"id": "recover", "surface": "infrastructure:service-control", "requiredCapability": "infrastructure.write", "featureGate": "DUNE_ADMIN_SERVICE_CONTROL_ENABLED", "confirmation": "CONTROL SERVICE", "mutation": True},
            ],
        }
        events = []
        calls = []
        store = type("Store", (), {
            "capsule": lambda self, key: {"responsePlan": plan, "responseDrills": ([{"id": "change-drill", "action": "incident-response-drill", "data": {"drill_id": events[-1][2]["drill_id"]}}] if events else [])},
        })()
        originals = {
            "RESPONSE_DRILLS_ENABLED": self.panel.RESPONSE_DRILLS_ENABLED,
            "COMMAND_CONSOLE_ENABLED": self.panel.COMMAND_CONSOLE_ENABLED,
            "change_intelligence_store": self.panel.change_intelligence_store,
            "audit_event": self.panel.audit_event,
        }
        self.panel.RESPONSE_DRILLS_ENABLED = True
        self.panel.COMMAND_CONSOLE_ENABLED = True
        self.panel.change_intelligence_store = lambda: store
        self.panel.audit_event = lambda action, ok=True, **data: events.append((action, ok, data))
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))
        principal = {"id": "owner", "capabilities": ["*"]}

        def executor(command_id):
            calls.append(command_id)
            return {"ok": True, "bounded": True}

        with mock.patch.dict(os.environ, {"DUNE_ADMIN_SERVICE_CONTROL_ENABLED": "true"}):
            result = self.panel.run_response_readiness_drill("slo:one", "a" * 64, principal, executor)
        self.assertTrue(result["ready"])
        self.assertFalse(result["recoveryExecuted"])
        self.assertFalse(result["gameMutationExecuted"])
        self.assertEqual(["rmq-health"], calls)
        self.assertEqual(64, len(result["diagnostics"][0]["outputSha256"]))
        self.assertNotIn("output", result["diagnostics"][0])
        self.assertTrue(result["recoveryContracts"][0]["ready"])
        self.assertEqual("incident-response-drill", events[0][0])
        self.assertTrue(events[0][1])
        self.assertFalse(events[0][2]["recovery_executed"])
        self.assertEqual("change-drill", result["ledgerEvent"]["id"])
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.panel.run_response_readiness_drill("slo:one", "c" * 64, principal, executor)
        self.assertEqual(["rmq-health"], calls)

    def test_response_drill_route_requires_confirmation_and_passes_authenticated_principal(self):
        original = self.panel.run_response_readiness_drill
        calls = []
        self.panel.run_response_readiness_drill = lambda key, digest, principal, executor: calls.append((key, digest, principal, callable(executor))) or {"ready": True, "recoveryExecuted": False}
        self.addCleanup(lambda: setattr(self.panel, "run_response_readiness_drill", original))
        rejected = self.invoke_post_route("/api/ops/change-intelligence/drill", {"incidentKey": "slo:one", "planSha256": "a" * 64, "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertFalse(calls)
        accepted = self.invoke_post_route("/api/ops/change-intelligence/drill", {"incidentKey": "slo:one", "planSha256": "a" * 64, "confirm": "RUN RESPONSE READINESS DRILL"})
        self.assertTrue(accepted["json"]["ready"])
        self.assertEqual("slo:one", calls[0][0])
        self.assertIsInstance(calls[0][2], dict)
        self.assertTrue(calls[0][3])

    def test_policy_wide_readiness_certification_deduplicates_diagnostics_and_records_coverage(self):
        policy_path = self.workspace / "certification-change-intelligence.json"
        policy_path.write_text((ROOT / "config" / "change-intelligence.json").read_text(encoding="utf-8"), encoding="utf-8")
        secret = self.workspace / "certification-change-intelligence.secret"
        secret.write_text("c" * 64 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        store = self.panel.change_intelligence.Store(self.workspace / "certification.sqlite3", policy_path, secret)
        store.initialize()
        policy = store.policy["response"]
        events = []
        calls = []

        originals = {
            "RESPONSE_DRILLS_ENABLED": self.panel.RESPONSE_DRILLS_ENABLED,
            "COMMAND_CONSOLE_ENABLED": self.panel.COMMAND_CONSOLE_ENABLED,
            "change_intelligence_store": self.panel.change_intelligence_store,
            "audit_event": self.panel.audit_event,
        }
        self.panel.RESPONSE_DRILLS_ENABLED = True
        self.panel.COMMAND_CONSOLE_ENABLED = True
        self.panel.change_intelligence_store = lambda: store

        def retain_event(action, ok=True, **data):
            events.append((action, ok, data))
            store.record({"action": action, "ts": 1700000000, "ok": ok, **data}, ingested_at=1700000001)

        self.panel.audit_event = retain_event
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        def executor(command_id):
            calls.append(command_id)
            return {"ok": True, "bounded": True}

        gates = {step["featureGate"]: "true" for runbook in policy["runbooks"] for step in runbook["steps"] if step["mutation"]}
        with mock.patch.dict(os.environ, gates):
            result = self.panel.run_incident_readiness_certification(policy["policySha256"], {"id": "owner", "capabilities": ["*"]}, executor)
        self.assertTrue(result["ready"])
        self.assertEqual(13, result["summary"]["runbooksReady"])
        self.assertEqual(13, result["summary"]["runbooksTotal"])
        self.assertEqual(3, result["summary"]["diagnosticsTotal"])
        self.assertEqual(11, result["summary"]["recoveryContractsTotal"])
        self.assertEqual(["stack-status", "rmq-health", "storage-status"], calls)
        self.assertFalse(result["recoveryExecuted"])
        self.assertFalse(result["gameMutationExecuted"])
        self.assertNotIn("output", result["diagnostics"][0])
        self.assertEqual(64, len(result["receiptSha256"]))
        self.assertEqual("incident-readiness-certification", events[0][0])
        self.assertTrue(events[0][1])
        self.assertTrue(result["ledgerEvent"]["receiptVerification"]["ok"])
        self.assertEqual(result["id"], result["ledgerEvent"]["data"]["certification_id"])
        self.assertTrue(store.verify()["ok"])
        with self.assertRaisesRegex(ValueError, "policy changed"):
            self.panel.run_incident_readiness_certification("f" * 64, {"id": "owner", "capabilities": ["*"]}, executor)
        self.assertEqual(3, len(calls))

    def test_readiness_certification_route_requires_confirmation_and_passes_authenticated_principal(self):
        original = self.panel.run_incident_readiness_certification
        calls = []
        self.panel.run_incident_readiness_certification = lambda digest, principal, executor: calls.append((digest, principal, callable(executor))) or {"ready": True, "summary": {"runbooksTotal": 13}}
        self.addCleanup(lambda: setattr(self.panel, "run_incident_readiness_certification", original))
        rejected = self.invoke_post_route("/api/ops/change-intelligence/certify", {"policySha256": "a" * 64, "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertFalse(calls)
        accepted = self.invoke_post_route("/api/ops/change-intelligence/certify", {"policySha256": "a" * 64, "confirm": "CERTIFY INCIDENT RESPONSE READINESS"})
        self.assertTrue(accepted["json"]["ready"])
        self.assertEqual("a" * 64, calls[0][0])
        self.assertIsInstance(calls[0][1], dict)
        self.assertTrue(calls[0][2])

    def test_deployment_assurance_routes_require_exact_confirmations_and_principal(self):
        originals = {
            "deployment_assurance_start": self.panel.deployment_assurance_start,
            "deployment_assurance_finish": self.panel.deployment_assurance_finish,
            "deployment_assurance_cancel": self.panel.deployment_assurance_cancel,
        }
        calls = []
        self.panel.deployment_assurance_start = lambda body, principal: calls.append(("start", body, principal)) or {"id": "deployment-window-one"}
        self.panel.deployment_assurance_finish = lambda body, principal: calls.append(("finish", body, principal)) or {"verification": {"ok": True}}
        self.panel.deployment_assurance_cancel = lambda body, principal: calls.append(("cancel", body, principal)) or {"ok": True}
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        rejected = self.invoke_post_route("/api/ops/deployment-assurance", {"action": "start", "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertFalse(calls)
        accepted = self.invoke_post_route("/api/ops/deployment-assurance", {"action": "start", "confirm": "START ASSURED CHANGE WINDOW"})
        self.assertEqual("deployment-window-one", accepted["json"]["id"])
        self.assertIsInstance(calls[-1][2], dict)
        self.invoke_post_route("/api/ops/deployment-assurance", {"action": "finish", "confirm": "FINALIZE ASSURED CHANGE WINDOW"})
        self.invoke_post_route("/api/ops/deployment-assurance", {"action": "cancel", "confirm": "CANCEL ASSURED CHANGE WINDOW"})
        self.assertEqual(["start", "finish", "cancel"], [row[0] for row in calls])

    def test_deployment_assurance_finish_defers_without_sealing_unhealthy_receipt(self):
        originals = {
            "verify_backup_set": self.panel.verify_backup_set,
            "deployment_assurance_health": self.panel.deployment_assurance_health,
            "deployment_assurance_store": self.panel.deployment_assurance_store,
            "audit_event": self.panel.audit_event,
        }
        store_calls = []
        self.panel.verify_backup_set = lambda path: {"ok": True, "path": path, "exitCode": 0}
        self.panel.deployment_assurance_health = lambda backup: {
            "desiredStateAttested": True, "readinessCurrent": True, "sloHealthy": True,
            "changeIntegrity": True, "prometheusReadiness": False,
            "adminHealthy": True, "backupVerified": True,
        }
        self.panel.deployment_assurance_store = lambda: type("Store", (), {
            "finish": lambda *args, **kwargs: store_calls.append((args, kwargs)),
        })()
        self.panel.audit_event = lambda *args, **kwargs: None
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))

        result = self.panel.deployment_assurance_finish(
            {"windowId": "deployment-window-one", "backupPath": "backup-one"},
            {"id": "owner"},
        )
        self.assertFalse(result["finalized"])
        self.assertEqual("waiting-for-health", result["state"])
        self.assertEqual(["prometheusReadiness"], result["failedHealth"])
        self.assertFalse(store_calls)

    def test_update_readiness_route_requires_exact_confirmation_and_principal(self):
        original = self.panel.certify_update_readiness
        calls = []
        self.panel.certify_update_readiness = lambda principal: calls.append(principal) or {"verification": {"ok": True}, "document": {"receipt": {"scheduledReady": True}}}
        self.addCleanup(lambda: setattr(self.panel, "certify_update_readiness", original))
        rejected = self.invoke_post_route("/api/ops/update-readiness", {"confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertFalse(calls)
        accepted = self.invoke_post_route("/api/ops/update-readiness", {"confirm": "CERTIFY GAME UPDATE READINESS"})
        self.assertTrue(accepted["json"]["verification"]["ok"])
        self.assertIsInstance(calls[0], dict)

    def test_capacity_application_is_evidence_gated_gradual_and_mode_preserving(self):
        fake = type("Store", (), {
            "status": lambda self: {
                "policy": {"maximumApplyFraction": 0.5, "minimumRetentionSeconds": 60, "maximumRetentionSeconds": 3600},
                "recommendations": {
                    "arrakeen": {"eligible": True, "recommendedRetentionSeconds": 60, "confidence": "moderate", "revisitSamples": 5, "startSamples": 2},
                    "survival": {"eligible": True, "recommendedRetentionSeconds": 60, "confidence": "high", "revisitSamples": 30, "startSamples": 10},
                    "deep-desert": {"eligible": False, "recommendedRetentionSeconds": 3600, "confidence": "low", "revisitSamples": 1, "startSamples": 0},
                },
            },
            "record_application": lambda self, actor, source, changes: {"id": "receipt", "changes": changes},
        })()
        state = {
            "modes": {"arrakeen": "dynamic", "survival": "always-on", "deep-desert": "dynamic"},
            "retentionByService": {}, "retentionSeconds": 900, "profile": "balanced",
        }
        written = []
        original_store = self.panel.capacity_intelligence_store
        original_read = self.panel.read_autoscaler_state
        original_write = self.panel.write_autoscaler_state
        self.panel.capacity_intelligence_store = lambda: fake
        self.panel.read_autoscaler_state = lambda: state
        self.panel.write_autoscaler_state = lambda value: written.append(json.loads(json.dumps(value)))
        self.addCleanup(lambda: setattr(self.panel, "capacity_intelligence_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "read_autoscaler_state", original_read))
        self.addCleanup(lambda: setattr(self.panel, "write_autoscaler_state", original_write))
        result = self.panel.capacity_apply_recommendations(actor="operator", source="manual")
        self.assertTrue(result["applied"])
        self.assertEqual(result["changes"], [{
            "service": "arrakeen", "beforeSeconds": 900, "recommendedSeconds": 60,
            "appliedSeconds": 450, "confidence": "moderate", "revisitSamples": 5, "startSamples": 2,
        }])
        self.assertEqual(written[-1]["profile"], "adaptive")
        self.assertEqual(written[-1]["modes"], state["modes"])

    def test_capacity_automatic_apply_cooldown_survives_process_restart(self):
        applied = []
        now = time.time()
        fake = type("Store", (), {
            "observe": lambda self, rows: {"ok": True},
            "status": lambda self: {"applications": [{"source": "automatic", "appliedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}]},
        })()
        originals = {
            "capacity_intelligence_store": self.panel.capacity_intelligence_store,
            "collect_capacity_intelligence_maps": self.panel.collect_capacity_intelligence_maps,
            "capacity_apply_recommendations": self.panel.capacity_apply_recommendations,
        }
        self.panel.capacity_intelligence_store = lambda: fake
        self.panel.collect_capacity_intelligence_maps = lambda: []
        self.panel.capacity_apply_recommendations = lambda **kwargs: applied.append(kwargs) or {"ok": True, "applied": True}
        for name, value in originals.items():
            self.addCleanup(lambda name=name, value=value: setattr(self.panel, name, value))
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("AUTOSCALER_MUTATIONS_ENABLED", True)
        self.patch_flag("CAPACITY_AUTO_APPLY_ENABLED", True)
        self.panel.CAPACITY_INTELLIGENCE_RUNTIME["lastAutoApplyAt"] = None
        self.panel.capacity_intelligence_tick()
        self.assertFalse(applied)
        self.assertGreater(self.panel.CAPACITY_INTELLIGENCE_RUNTIME["lastAutoApplyAt"], now - 2)

    def test_landsraad_reward_and_contribution_plans_preserve_rollback(self):
        def fake_query(sql, params=None):
            if "landsraad_load_current_term" in sql:
                return [{"term_id": 7, "end_time": "2026-07-22T00:00:00Z", "testterm": False}]
            if "landsraad_decree_term" in sql:
                return [{"term_id": 7}]
            if "from pg_proc" in sql:
                return []
            if "landsraad_task_rewards" in sql:
                return [{"task_id": 44, "threshold": 10, "template_id": "OldReward", "amount": 1}]
            if "from dune.player_state" in sql:
                return [{"account_id": 10, "player_controller_id": 200, "player_pawn_id": 201, "online_status": "Offline"}]
            if "from dune.player_faction" in sql:
                return [{"faction_id": 2}]
            if "landsraad_task_player_contributions" in sql:
                return [{"player_id": 200, "task_id": 44, "amount": 25}]
            return []

        self.patch_db(fake_query)
        reward = self.handler.landsraad_mutation({
            "action": "reward-tier", "task_id": 44, "threshold": 10,
            "new_threshold": 20, "template_id": "NewReward", "amount": 2,
            "dry_run": True,
        })
        contribution = self.handler.landsraad_mutation({
            "action": "player-contribution", "task_id": 44,
            "account_id": 10, "amount": 50, "dry_run": True,
        })
        self.assertEqual(reward["plan"]["rollback"]["new_threshold"], 10)
        self.assertEqual(reward["plan"]["rollback"]["template_id"], "OldReward")
        self.assertEqual(contribution["plan"]["rollback"]["amount"], 25)
        self.assertEqual(contribution["plan"]["factionId"], 2)

    def test_bootstrap_status_reports_secrets_only_as_configured_flags(self):
        original_read_env = self.panel.read_env
        original_socket = self.panel.DOCKER_SOCKET
        self.panel.read_env = lambda: {
            "DUNE_STEAM_SERVER_DIR": "/srv/dune", "DUNE_IMAGE_TAG": "1",
            "WORLD_NAME": "world", "WORLD_UNIQUE_NAME": "unique",
            "WORLD_REGION": "us", "EXTERNAL_ADDRESS": "example.test",
            "FLS_SECRET": "private-secret", "POSTGRES_DUNE_PASSWORD": "private-db",
            "DUNE_ADMIN_TOKEN": "private-admin",
        }
        self.panel.DOCKER_SOCKET = str(self.workspace / "missing-docker.sock")
        self.patch_db(lambda sql, params=None: [{"database": "dune_sb_1_4_0_0", "schema_ready": True}])
        self.addCleanup(lambda: setattr(self.panel, "read_env", original_read_env))
        self.addCleanup(lambda: setattr(self.panel, "DOCKER_SOCKET", original_socket))

        status = self.panel.bootstrap_status()

        rendered = json.dumps(status)
        self.assertTrue(status["ok"])
        self.assertNotIn("private-secret", rendered)
        self.assertNotIn("private-db", rendered)
        self.assertNotIn("private-admin", rendered)

    def test_player_maintenance_previews_gear_and_login_queue(self):
        def fake_query(sql, params=None):
            if "join dune.accounts" in sql:
                return [{"account_id": 10, "player_pawn_id": 201, "player_controller_id": 301, "online_status": "Offline", "character_name": "Tester", "funcom_id": "FLS#123"}]
            if "from dune.items" in sql:
                return [{"id": 1, "template_id": "Knife", "stats": {"FItemStackAndDurabilityStats": [{}, {"MaxDurability": 100, "CurrentDurability": 25, "DecayedDurability": 20}]}}]
            return []

        self.patch_db(fake_query)
        original_service = self.panel.docker_service_container
        original_exec = self.panel.docker_container_exec
        self.panel.docker_service_container = lambda service, running=True: {"Id": "a" * 64}
        self.panel.docker_container_exec = lambda container, argv, timeout=20: {"ok": True, "output": "FLS#123_queue\t0\t1\trunning\n"}
        self.addCleanup(lambda: setattr(self.panel, "docker_service_container", original_service))
        self.addCleanup(lambda: setattr(self.panel, "docker_container_exec", original_exec))

        gear = self.handler.player_maintenance_mutation({"action": "repair-gear", "account_id": 10, "dry_run": True})
        queue = self.handler.player_maintenance_mutation({"action": "repair-login-queue", "account_id": 10, "dry_run": True})

        self.assertEqual(gear["plan"]["repairable"][0]["target"], 100)
        self.assertTrue(queue["plan"]["exists"])
        self.assertEqual(queue["plan"]["queue"]["messages"], 1)

    def test_player_progression_maintenance_previews_intel_recipe_and_research(self):
        def fake_query(sql, params=None):
            if "join dune.accounts" in sql:
                return [{"account_id": 10, "player_pawn_id": 201, "online_status": "Offline", "character_name": "Tester", "funcom_id": "FLS#123"}]
            if "m_TechKnowledgePoints" in sql:
                return [{"value": 2700}]
            if "select exists" in sql:
                return [{"found": True}]
            if "m_KnownItemRecipes' as values" in sql:
                return [{"values": []}]
            if "m_TechKnowledgeData' as values" in sql:
                return [{"values": [{"ItemKey": "RCP_Test", "UnlockedState": "Available"}]}]
            return []
        self.patch_db(fake_query)

        intel = self.handler.player_maintenance_mutation({"action": "add-intel", "account_id": 10, "amount": 100, "dry_run": True})
        recipe = self.handler.player_maintenance_mutation({"action": "unlock-recipe", "account_id": 10, "key": "Test_Recipe", "dry_run": True})
        research = self.handler.player_maintenance_mutation({"action": "unlock-research", "account_id": 10, "key": "RCP_Test", "dry_run": True})

        self.assertEqual(intel["plan"]["newValue"], 2779)
        self.assertEqual(recipe["plan"]["key"], "Test_Recipe")
        self.assertEqual(research["plan"]["key"], "RCP_Test")
        self.assertEqual(research["plan"]["recipeId"], "Test")
        self.assertTrue(research["plan"]["recipeMaterialized"])
        self.assertEqual(research["confirm"], "WRITE PLAYER PROGRESSION")

    def test_player_progression_previews_specialization_and_all_keystones(self):
        def fake_query(sql, params=None):
            if "join dune.accounts" in sql:
                return [{"account_id": 10, "player_pawn_id": 201, "player_controller_id": 301, "online_status": "Offline", "character_name": "Tester", "funcom_id": "FLS#123"}]
            if "enum_range" in sql:
                return [{"found": True}]
            if "from dune.specialization_tracks" in sql:
                return [{"xp_amount": 100, "level": 2}]
            if "specialization_keystones_map" in sql:
                return [{"available": 20, "purchased": 3}]
            return []
        self.patch_db(fake_query)

        maximum = self.handler.player_maintenance_mutation({"action": "specialization-max", "account_id": 10, "track_type": "Combat", "dry_run": True})
        reset = self.handler.player_maintenance_mutation({"action": "specialization-reset", "account_id": 10, "track_type": "Combat", "dry_run": True})
        keystones = self.handler.player_maintenance_mutation({"action": "keystones-grant-all", "account_id": 10, "dry_run": True})

        self.assertEqual(maximum["plan"]["xp"], 44182)
        self.assertEqual(maximum["plan"]["level"], 100)
        self.assertEqual(reset["plan"]["xp"], 0)
        self.assertEqual(keystones["plan"]["available"], 20)

    def test_world_storage_items_are_bounded_to_selected_actor(self):
        calls = []
        def fake_query(sql, params=None):
            calls.append((sql, params))
            if "from dune.placeables" in sql:
                return [{"id": 88, "building_type": "GenericContainer_Placeable", "owner_entity_id": 2}]
            if "from dune.inventories" in sql:
                return [{"id": 99, "actor_id": 88}]
            if "from dune.items" in sql:
                return [{"id": 100, "inventory_id": 99, "template_id": "Water"}]
            return []
        self.patch_db(fake_query)

        result = self.panel.world_storage_items(88)

        self.assertEqual(result["actorId"], 88)
        self.assertTrue(result["readOnly"])
        self.assertEqual(result["items"][0]["template_id"], "Water")
        self.assertTrue(all(params == (88,) for _, params in calls))

    def test_item_stack_and_quality_edit_is_offline_preserving_and_verified(self):
        state = {
            "id": 500,
            "inventory_id": 90,
            "stack_size": 2,
            "position_index": 3,
            "template_id": "WaterPack_Consumable",
            "is_new": True,
            "acquisition_time": 1234,
            "stats": {"kept": True},
            "quality_level": 1,
            "volume_override": None,
        }
        events = []

        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                events.append(("execute", normalized, params))
                if "select * from dune.items" in normalized:
                    self.result = dict(state)
                elif "from dune.inventories inv" in normalized:
                    self.result = {"account_id": 42, "character_name": "Tester", "online_status": "Offline"}
                elif "dune.save_item" in normalized:
                    state["stack_size"] = params[2]
                    state["quality_level"] = params[8]
                    self.result = None
                elif "dune.load_item" in normalized:
                    self.result = dict(state)
            def fetchone(self): return self.result

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def cursor(self, cursor_factory=None): return FakeCursor()
            def commit(self): events.append(("commit",))
            def rollback(self): events.append(("rollback",))

        self.patch_connect(lambda: FakeConn())
        handler = object.__new__(self.panel.Handler)
        result = handler.set_item_properties({"item_id": 500, "stack_size": 7, "quality_level": 4})

        self.assertEqual(7, result["stack_size"])
        self.assertEqual(4, result["quality_level"])
        self.assertTrue(result["offlineVerified"])
        save = next(event for event in events if event[0] == "execute" and "dune.save_item" in event[1])
        self.assertEqual({"kept": True}, json.loads(save[2][7]))
        self.assertEqual("WaterPack_Consumable", save[2][4])
        self.assertIn(("commit",), events)
        self.assertNotIn(("rollback",), events)

    def test_item_property_edit_rejects_online_owner(self):
        events = []
        item = {"id": 500, "inventory_id": 90, "stack_size": 2, "position_index": 3,
                "template_id": "WaterPack_Consumable", "is_new": True, "acquisition_time": 1234,
                "stats": {}, "quality_level": 1, "volume_override": None}

        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                events.append(("execute", normalized, params))
                self.result = {"account_id": 42, "online_status": "Online"} if "from dune.inventories inv" in normalized else dict(item)
            def fetchone(self): return self.result

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def cursor(self, cursor_factory=None): return FakeCursor()
            def commit(self): events.append(("commit",))
            def rollback(self): events.append(("rollback",))

        self.patch_connect(lambda: FakeConn())
        handler = object.__new__(self.panel.Handler)
        with self.assertRaisesRegex(ValueError, "must be offline"):
            handler.set_item_properties({"item_id": 500, "stack_size": 7, "quality_level": 4})
        self.assertIn(("rollback",), events)
        self.assertFalse(any("dune.save_item" in event[1] for event in events if event[0] == "execute"))

    def test_item_property_route_requires_new_confirmation_and_creates_backup(self):
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("ITEM_GRANTS_ENABLED", True)
        backups = []
        original_backup = self.panel.create_db_backup
        original_edit = self.panel.Handler.set_item_properties
        self.panel.create_db_backup = lambda: backups.append(True) or {"path": "backup.dump", "bytes": 1}
        self.panel.Handler.set_item_properties = lambda handler, body: {
            "ok": True, "item_id": int(body["item_id"]), "stack_size": int(body["stack_size"]),
            "quality_level": int(body["quality_level"]),
        }
        self.addCleanup(lambda: setattr(self.panel, "create_db_backup", original_backup))
        self.addCleanup(lambda: setattr(self.panel.Handler, "set_item_properties", original_edit))

        rejected = self.invoke_post_route("/api/admin/item/stack", {
            "item_id": 500, "stack_size": 7, "quality_level": 4, "confirm": "SET STACK",
        })
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertEqual([], backups)

        accepted = self.invoke_post_route("/api/admin/item/stack", {
            "item_id": 500, "stack_size": 7, "quality_level": 4, "confirm": "SET ITEM PROPERTIES",
        })
        self.assertEqual([], accepted["errors"])
        self.assertEqual({"path": "backup.dump", "bytes": 1}, accepted["json"]["backup"])
        self.assertEqual([True], backups)

    def test_base_retirement_routes_expose_preview_and_gate_native_archive(self):
        original_scan = self.panel.base_retirement.scan
        original_receipts = self.panel.base_retirement.list_receipts
        original_plan = self.panel.base_retirement.plan
        original_archive = self.panel.base_retirement.archive
        original_cooldown_plan = self.panel.base_retirement.cooldown_plan
        original_reset_cooldown = self.panel.base_retirement.reset_cooldown
        self.panel.base_retirement.scan = lambda query_fn, limit=500: [{"totemId": 44, "status": "owned", "lastBackupTimestamp": 123456}]
        self.panel.base_retirement.list_receipts = lambda root: [{"status": "committed", "baseBackupId": 77}]
        self.panel.base_retirement.plan = lambda query_fn, totem, recovery=None: {"ok": True, "canExecute": True, "expectedFingerprint": "f" * 64, "base": {"totemId": int(totem)}}
        self.panel.base_retirement.cooldown_plan = lambda query_fn, totem: {"ok": True, "canExecute": True, "expectedFingerprint": "c" * 64, "confirm": f"RESET BASE COOLDOWN {totem}", "base": {"totemId": int(totem)}, "remainingSecondsKnown": False}
        archive_calls = []
        cooldown_calls = []
        self.panel.base_retirement.archive = lambda *args, **kwargs: archive_calls.append(kwargs) or {"ok": True, "baseBackupId": 77}
        self.panel.base_retirement.reset_cooldown = lambda *args, **kwargs: cooldown_calls.append(kwargs) or {"ok": True, "verification": {"lastBackupTimestamp": 0}, "mapLifecycleInvoked": False}
        self.addCleanup(lambda: setattr(self.panel.base_retirement, "scan", original_scan))
        self.addCleanup(lambda: setattr(self.panel.base_retirement, "list_receipts", original_receipts))
        self.addCleanup(lambda: setattr(self.panel.base_retirement, "plan", original_plan))
        self.addCleanup(lambda: setattr(self.panel.base_retirement, "archive", original_archive))
        self.addCleanup(lambda: setattr(self.panel.base_retirement, "cooldown_plan", original_cooldown_plan))
        self.addCleanup(lambda: setattr(self.panel.base_retirement, "reset_cooldown", original_reset_cooldown))

        handler, captured = self.make_route_handler("/api/admin/base-retirement")
        handler.is_app_route = lambda path: False
        handler.do_GET()
        self.assertEqual("owned", captured["json"]["bases"][0]["status"])
        self.assertTrue(captured["json"]["gameRecoverable"])
        self.assertFalse(captured["json"]["destructiveDelete"])
        self.assertEqual("dune.totems.last_backup_timestamp", captured["json"]["cooldownColumn"])
        self.assertFalse(captured["json"]["cooldownRemainingSecondsKnown"])

        preview = self.invoke_post_route("/api/admin/base-retirement", {"action": "preview", "totemId": 44, "recoveryPlayerId": 46})
        self.assertEqual([], preview["errors"])
        self.assertTrue(preview["json"]["canExecute"])

        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("BASE_RETIREMENT_MUTATIONS_ENABLED", False)
        blocked = self.invoke_post_route("/api/admin/base-retirement", {"action": "archive", "totemId": 44, "recoveryPlayerId": 46, "expectedFingerprint": "f" * 64, "confirm": "ARCHIVE BASE 44"})
        self.assertEqual(401, blocked["errors"][0]["status"])
        self.assertEqual([], archive_calls)

        self.patch_flag("BASE_RETIREMENT_MUTATIONS_ENABLED", True)
        accepted = self.invoke_post_route("/api/admin/base-retirement", {"action": "archive", "totemId": 44, "recoveryPlayerId": 46, "expectedFingerprint": "f" * 64, "confirm": "ARCHIVE BASE 44"})
        self.assertEqual([], accepted["errors"])
        self.assertEqual(77, accepted["json"]["baseBackupId"])
        self.assertEqual(44, archive_calls[0]["totem_id"])

        cooldown_preview = self.invoke_post_route("/api/admin/base-retirement", {"action": "cooldown-preview", "totemId": 44})
        self.assertEqual([], cooldown_preview["errors"])
        self.assertEqual("RESET BASE COOLDOWN 44", cooldown_preview["json"]["confirm"])

        self.patch_flag("BASE_COOLDOWN_MUTATIONS_ENABLED", False)
        cooldown_blocked = self.invoke_post_route("/api/admin/base-retirement", {"action": "cooldown-reset", "totemId": 44, "expectedFingerprint": "c" * 64, "confirm": "RESET BASE COOLDOWN 44"})
        self.assertEqual(401, cooldown_blocked["errors"][0]["status"])
        self.assertEqual([], cooldown_calls)

        self.patch_flag("BASE_COOLDOWN_MUTATIONS_ENABLED", True)
        cooldown_accepted = self.invoke_post_route("/api/admin/base-retirement", {"action": "cooldown-reset", "totemId": 44, "expectedFingerprint": "c" * 64, "confirm": "RESET BASE COOLDOWN 44"})
        self.assertEqual([], cooldown_accepted["errors"])
        self.assertEqual(0, cooldown_accepted["json"]["verification"]["lastBackupTimestamp"])
        self.assertFalse(cooldown_accepted["json"]["mapLifecycleInvoked"])
        self.assertEqual(44, cooldown_calls[0]["totem_id"])

    def test_base_retirement_ui_names_native_recovery_safety_contract(self):
        self.assertIn("Recoverable Base Retirement", self.panel.INDEX)
        self.assertIn("base_backup_save_from_totem", self.panel.INDEX)
        self.assertIn("expectedFingerprint", self.panel.INDEX)
        self.assertIn("no destructive delete", self.panel.INDEX)
        self.assertIn("Base Pack-Up Cooldown", self.panel.INDEX)
        self.assertIn("last_backup_timestamp", self.panel.INDEX)
        self.assertIn("cooldown-preview", self.panel.INDEX)
        self.assertIn("cooldown-reset", self.panel.INDEX)

    def test_community_delivery_uses_offline_grant_path_and_completes_receipt(self):
        config_path = self.workspace / "config" / "community-rewards.json"
        config_path.write_text((ROOT / "config" / "community-rewards.example.json").read_text(), encoding="utf-8")
        store = self.panel.community_rewards.Store(self.workspace / "backups" / "community.sqlite3", config_path)
        store.initialize()
        store.credit(42, 100, "manual", "test:seed")
        order = store.purchase(42, "starter-water", 1, "test:purchase")
        original_store = self.panel.COMMUNITY_STORE
        original_initialized = self.panel.COMMUNITY_STORE_INITIALIZED
        original_config = self.panel.COMMUNITY_REWARDS_FILE
        self.panel.COMMUNITY_STORE = store
        self.panel.COMMUNITY_STORE_INITIALIZED = True
        self.panel.COMMUNITY_REWARDS_FILE = config_path
        self.addCleanup(lambda: setattr(self.panel, "COMMUNITY_STORE", original_store))
        self.addCleanup(lambda: setattr(self.panel, "COMMUNITY_STORE_INITIALIZED", original_initialized))
        self.addCleanup(lambda: setattr(self.panel, "COMMUNITY_REWARDS_FILE", original_config))
        self.patch_flag("COMMUNITY_REWARDS_ENABLED", True)
        self.patch_flag("COMMUNITY_DELIVERY_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("ITEM_GRANTS_ENABLED", True)
        self.patch_db(lambda sql, params=None: [{"online_status": "Offline", "account_id": 42, "character_name": "Tester"}])

        class GrantAdapter:
            def __init__(self):
                self.calls = []

            def grant_item(self, body):
                self.calls.append(dict(body))
                return {"ok": True, "dry_run": bool(body["dry_run"]), "item_id": None if body["dry_run"] else 99}

        adapter = GrantAdapter()
        result = self.panel.community_delivery_tick(adapter)
        self.assertEqual("delivered", result["delivery"]["status"])
        self.assertEqual([True, False], [row["dry_run"] for row in adapter.calls])
        self.assertEqual("delivered", store.status(42)["purchases"][0]["status"])
        self.assertEqual(order["id"], store.status(42)["purchases"][0]["id"])

    def test_community_webhook_requires_fresh_hmac_and_is_idempotent(self):
        config_path = self.workspace / "config" / "community-rewards.json"
        config = json.loads((ROOT / "config" / "community-rewards.example.json").read_text())
        config["webhooks"]["vote"]["enabled"] = True
        config_path.write_text(json.dumps(config), encoding="utf-8")
        store = self.panel.community_rewards.Store(self.workspace / "backups" / "community-webhook.sqlite3", config_path)
        store.initialize()
        original_store = self.panel.COMMUNITY_STORE
        original_initialized = self.panel.COMMUNITY_STORE_INITIALIZED
        original_config = self.panel.COMMUNITY_REWARDS_FILE
        self.panel.COMMUNITY_STORE = store
        self.panel.COMMUNITY_STORE_INITIALIZED = True
        self.panel.COMMUNITY_REWARDS_FILE = config_path
        self.addCleanup(lambda: setattr(self.panel, "COMMUNITY_STORE", original_store))
        self.addCleanup(lambda: setattr(self.panel, "COMMUNITY_STORE_INITIALIZED", original_initialized))
        self.addCleanup(lambda: setattr(self.panel, "COMMUNITY_REWARDS_FILE", original_config))
        self.patch_flag("COMMUNITY_REWARDS_ENABLED", True)
        old_secret = os.environ.get("DUNE_COMMUNITY_VOTE_WEBHOOK_SECRET")
        os.environ["DUNE_COMMUNITY_VOTE_WEBHOOK_SECRET"] = "test-secret"
        self.addCleanup(lambda: os.environ.pop("DUNE_COMMUNITY_VOTE_WEBHOOK_SECRET", None) if old_secret is None else os.environ.__setitem__("DUNE_COMMUNITY_VOTE_WEBHOOK_SECRET", old_secret))
        payload = {"eventId": "vote-1", "duneAccountId": 42, "amount": 5}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(b"test-secret", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()

        class Headers(dict):
            def get_all(self, key, default=None):
                return [self[key]] if key in self else (default or [])

        def request(sig):
            return types.SimpleNamespace(headers=Headers({"Content-Length": str(len(raw)), "Content-Type": "application/json", "X-DASH-Timestamp": timestamp, "X-DASH-Signature": sig}), rfile=io.BytesIO(raw))

        first = self.panel.community_webhook_request(request(signature), "vote")
        replay = self.panel.community_webhook_request(request(signature), "vote")
        self.assertFalse(first["idempotent"])
        self.assertTrue(replay["idempotent"])
        with self.assertRaises(PermissionError):
            self.panel.community_webhook_request(request("0" * 64), "vote")

    def test_community_canary_route_requires_mutations_and_exact_confirmation(self):
        original_store = self.panel.community_store
        original_run = self.panel.run_community_canary
        calls = []
        self.panel.community_store = lambda: object()
        self.panel.run_community_canary = lambda principal: calls.append(principal) or {
            "document": {"receipt": {"id": "community-canary-" + "a" * 32, "ready": True}},
            "verification": {"ok": True},
        }
        self.addCleanup(lambda: setattr(self.panel, "community_store", original_store))
        self.addCleanup(lambda: setattr(self.panel, "run_community_canary", original_run))
        self.patch_flag("COMMUNITY_REWARDS_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", False)

        gated = self.invoke_post_route("/api/community/rewards", {"action": "canary", "confirm": "RUN COMMUNITY REWARDS CANARY"})
        self.assertEqual(401, gated["errors"][0]["status"])
        self.assertFalse(calls)
        self.patch_flag("MUTATIONS_ENABLED", True)
        rejected = self.invoke_post_route("/api/community/rewards", {"action": "canary", "confirm": "wrong"})
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertFalse(calls)
        accepted = self.invoke_post_route("/api/community/rewards", {"action": "canary", "confirm": "RUN COMMUNITY REWARDS CANARY"})
        self.assertEqual([], accepted["errors"])
        self.assertTrue(accepted["json"]["document"]["receipt"]["ready"])
        self.assertEqual(1, len(calls))
        self.assertIn("Run isolated canary", self.panel.INDEX)
        self.assertIn("never opens the live community database", self.panel.INDEX)

    def test_creator_canary_route_requires_mutations_and_exact_confirmation(self):
        original_run = self.panel.run_creator_canary
        calls = []
        self.panel.run_creator_canary = lambda principal: calls.append(principal) or {
            "document": {"receipt": {"id": "creator-canary-" + "a" * 32, "ready": True}},
            "verification": {"ok": True},
        }
        self.addCleanup(lambda: setattr(self.panel, "run_creator_canary", original_run))
        self.patch_flag("BASE_CREATOR_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", False)

        gated = self.invoke_post_route(
            "/api/creator/bases",
            {"action": "canary", "confirm": "RUN CREATOR MODDING CANARY"},
        )
        self.assertEqual(401, gated["errors"][0]["status"])
        self.assertFalse(calls)
        self.patch_flag("MUTATIONS_ENABLED", True)
        rejected = self.invoke_post_route(
            "/api/creator/bases", {"action": "canary", "confirm": "wrong"},
        )
        self.assertEqual(401, rejected["errors"][0]["status"])
        self.assertFalse(calls)
        accepted = self.invoke_post_route(
            "/api/creator/bases",
            {"action": "canary", "confirm": "RUN CREATOR MODDING CANARY"},
        )
        self.assertEqual([], accepted["errors"])
        self.assertTrue(accepted["json"]["document"]["receipt"]["ready"])
        self.assertEqual(1, len(calls))
        self.assertIn("Creator and modding proof", self.panel.INDEX)
        self.assertIn("No live gallery, configuration, database, player, map, or network state is touched", self.panel.INDEX)

    def test_alert_inbox_public_status_rejects_stale_success(self):
        database = self.workspace / "backups" / "alert-inbox" / "inbox.sqlite3"
        store = self.panel.alert_inbox.Store(database).initialize()
        payload = {"status": "success", "data": {"alerts": []}}
        store.sync(payload, now=time.time() - 1000)
        self.patch_flag("ALERT_INBOX_ENABLED", True)
        self.patch_flag("ALERT_INBOX_STORE", store)
        self.patch_flag("ALERT_INBOX_POLL_SECONDS", 30)
        stale = self.panel.alert_inbox_public_status(limit=1)
        self.assertFalse(stale["ok"])
        self.assertEqual(90, stale["collector"]["staleAfterSeconds"])
        store.sync(payload, now=time.time())
        current = self.panel.alert_inbox_public_status(limit=1)
        self.assertTrue(current["ok"])

    def test_alert_acknowledgement_route_emits_only_the_first_transition(self):
        database = self.workspace / "backups" / "alert-inbox" / "ack.sqlite3"
        store = self.panel.alert_inbox.Store(database).initialize()
        created = store.sync({"status": "success", "data": {"alerts": [{
            "labels": {"alertname": "TestAlert", "severity": "warning"},
            "annotations": {"summary": "test"}, "state": "firing",
            "activeAt": "2026-07-17T12:00:00Z",
        }]}}, now=time.time())
        fingerprint = created["transitions"][0]["fingerprint"]
        self.patch_flag("ALERT_INBOX_ENABLED", True)
        self.patch_flag("ALERT_INBOX_MUTATIONS_ENABLED", True)
        self.patch_flag("MUTATIONS_ENABLED", True)
        self.patch_flag("ALERT_INBOX_STORE", store)
        body = {"action": "acknowledge", "fingerprint": fingerprint, "note": "owned", "confirm": "ACKNOWLEDGE ALERT"}
        first = self.invoke_post_route("/api/ops/alerts", body)
        second = self.invoke_post_route("/api/ops/alerts", body)
        self.assertEqual([], first["errors"])
        self.assertFalse(first["json"]["idempotent"])
        self.assertEqual(["prometheus-alert-acknowledged"], [row["action"] for row in first["audits"]])
        self.assertTrue(second["json"]["idempotent"])
        self.assertEqual([], second["audits"])


if __name__ == "__main__":
    unittest.main()
