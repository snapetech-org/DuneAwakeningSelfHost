#!/usr/bin/env python3
import fcntl
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "admin" / "restore_drill.py"
SPEC = importlib.util.spec_from_file_location("restore_drill", MODULE_PATH)
restore_drill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore_drill)

CLI_SPEC = importlib.util.spec_from_file_location("backup_restore_drill_cli", pathlib.Path(__file__).resolve().parent / "backup-restore-drill.py")
restore_drill_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(restore_drill_cli)


class FakeDocker:
    def __init__(self, failure=None, stale=None, cleanup_failure=False):
        self.failure = failure
        self.stale = list(stale or [])
        self.cleanup_failure = cleanup_failure
        self.created = []
        self.removed = []
        self.spec = None
        self.commands = []

    def list_drill_containers(self):
        return self.stale

    def create_container(self, name, spec):
        if self.failure == "create":
            raise RuntimeError("create failed")
        self.created.append(name)
        self.spec = spec
        return "a" * 64

    def start_container(self, identifier):
        if self.failure == "start":
            raise RuntimeError("start failed")

    def inspect_container(self, identifier):
        host = dict(self.spec["HostConfig"])
        return {
            "HostConfig": host,
            "Config": {"User": self.spec["User"]},
            "Mounts": [{"Destination": item["Target"], "RW": not item["ReadOnly"]} for item in host["Mounts"]],
        }

    def logs(self, identifier):
        return "fake logs"

    def exec(self, identifier, argv, timeout=60):
        self.commands.append(list(argv))
        joined = " ".join(argv)
        if self.failure == "restore" and argv and argv[0] == "pg_restore" and "--dbname=drill" in argv:
            return 1, "restore exploded"
        if argv and argv[0] == "pg_isready":
            return 0, ""
        if argv and argv[0] == "psql" and "missingTables" in joined:
            return 0, json.dumps({
                "database": "drill", "postgresVersion": "17.4", "missingTables": [],
                "missingFunctions": [], "invalidIndexes": 0,
                "unvalidatedConstraints": 0, "databaseBytes": 987654,
            }) + "\n"
        if argv and argv[0] == "psql" and "dash_life_candidate" in joined:
            if self.failure == "life-contract":
                return 0, json.dumps({
                    "transactionRolledBack": True, "candidateFound": True,
                    "deadTransitionVerified": True, "aliveTransitionVerified": False,
                    "testedAccountCount": 1,
                }) + "\n"
            return 0, json.dumps({
                "transactionRolledBack": True, "candidateFound": True,
                "deadTransitionVerified": True, "aliveTransitionVerified": True,
                "nativeFunction": "dune.update_death_location(actordescription,serverinfo,playerlifestate)",
                "testedAccountCount": 1,
            }) + "\n"
        if argv and argv[0] == "psql" and "dash_teleport_candidate" in joined:
            if self.failure == "teleport-contract":
                return 0, json.dumps({
                    "transactionRolledBack": True, "candidateFound": True,
                    "moveVerified": False, "testedAccountCount": 1,
                }) + "\n"
            return 0, json.dumps({
                "transactionRolledBack": True, "candidateFound": True,
                "moveVerified": True,
                "nativeFunction": "dune.admin_move_offline_player_to_partition(text,bigint,vector)",
                "testedAccountCount": 1,
            }) + "\n"
        if argv and argv[0] == "psql" and "dash_transfer_candidate" in joined:
            if self.failure == "transfer-contract":
                return 0, json.dumps({
                    "transactionRolledBack": True, "candidateFound": True,
                    "exportVerified": True, "importVerified": False,
                    "testedAccountCount": 1,
                }) + "\n"
            return 0, json.dumps({
                "transactionRolledBack": True, "candidateFound": True,
                "exportVerified": True, "importVerified": True,
                "nativeExportFunction": "dune.character_transfer_export(text)",
                "nativeImportFunction": "dune.character_transfer_import(jsonb,text,text)",
                "testedAccountCount": 1,
            }) + "\n"
        if argv and argv[0] == "psql":
            return 0, json.dumps({
                "actors": 30, "player_state": 4, "world_partition": 30,
                "farm_state": 30, "items": 50, "inventories": 20,
                "building_instances": 2, "base_backups": 1,
            }) + "\n"
        if argv[:3] == ["stat", "-c", "%s"]:
            return 0, "12345\n"
        if argv[:3] == ["stat", "-c", "%a"]:
            return 0, "400\n"
        if argv and argv[0] == "sha256sum":
            return 0, hashlib.sha256(b"PGDMP fixture").hexdigest() + "  /drill/source.dump\n"
        return 0, "ok\n"

    def remove_container(self, identifier, force=True):
        if self.cleanup_failure and identifier == "a" * 64:
            raise RuntimeError("cleanup failed")
        self.removed.append(identifier)


class RestoreDrillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = pathlib.Path(self.temp.name)
        self.backups = self.workspace / "backups"
        self.backups.mkdir()
        self.dump = self.backups / "admin-panel" / "maintenance" / "latest" / "world.dump"
        self.dump.parent.mkdir(parents=True)
        self.dump.write_bytes(b"PGDMP fixture")
        self.receipts = self.backups / "admin-panel" / "restore-drills"

    def tearDown(self):
        self.temp.cleanup()

    def test_dump_selection_prefers_newest_and_rejects_escape_and_symlink(self):
        older = self.backups / "older.dump"
        older.write_bytes(b"old")
        os.utime(older, (1, 1))
        self.assertEqual(self.dump.resolve(), restore_drill.select_dump(self.workspace))
        outside = self.workspace / "outside.dump"
        outside.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            restore_drill.select_dump(self.workspace, outside)
        linked = self.backups / "linked.dump"
        linked.symlink_to(self.dump)
        with self.assertRaises(ValueError):
            restore_drill.select_dump(self.workspace, linked)
        staging = self.backups / "admin-panel" / "restore-drills" / ".source-newest.dump"
        staging.parent.mkdir(parents=True)
        staging.write_bytes(b"private staging")
        os.utime(staging, None)
        self.assertEqual(self.dump.resolve(), restore_drill.select_dump(self.workspace))
        with self.assertRaises(ValueError):
            restore_drill.select_dump(self.workspace, staging)

    def test_selection_never_descends_into_private_restore_sources(self):
        private = self.backups / "admin-panel" / "restore-drills" / "deep"
        private.mkdir(parents=True)
        (private / "newest.dump").write_bytes(b"private")
        original = os.scandir
        visited = []
        def checked_scandir(path):
            visited.append(pathlib.Path(path))
            if "restore-drills" in pathlib.Path(path).parts:
                raise AssertionError("private recovery tree was traversed")
            return original(path)
        from unittest.mock import patch
        with patch.object(os, "scandir", checked_scandir):
            self.assertEqual(self.dump.resolve(), restore_drill.select_dump(self.workspace))
        self.assertTrue(visited)

    def test_container_spec_is_networkless_bounded_and_read_only(self):
        spec = restore_drill.build_container_spec("/host/private.dump", host_passwd="/host/passwd", host_group="/host/group", run_uid=1234, run_gid=1235, memory_bytes=1024, cpu_count=1.5, pids_limit=77, pgdata_bytes=2048)
        host = spec["HostConfig"]
        self.assertEqual("none", host["NetworkMode"])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(["ALL"], host["CapDrop"])
        self.assertIn("no-new-privileges:true", host["SecurityOpt"])
        self.assertEqual(77, host["PidsLimit"])
        self.assertEqual(1_500_000_000, host["NanoCpus"])
        self.assertEqual(1024, host["Memory"])
        self.assertTrue(host["Mounts"][0]["ReadOnly"])
        self.assertEqual("/host/private.dump", host["Mounts"][0]["Source"])
        self.assertEqual(["/drill/source.dump", "/etc/passwd", "/etc/group"], [item["Target"] for item in host["Mounts"]])
        self.assertEqual("1234:1235", spec["User"])
        self.assertNotIn("ExposedPorts", spec)

    def test_cli_resolves_relative_workspace_before_building_receipt_path(self):
        captured = {}
        original = restore_drill_cli.restore_drill.run_drill
        previous = pathlib.Path.cwd()
        try:
            restore_drill_cli.restore_drill.run_drill = lambda workspace, **kwargs: captured.update({"workspace": workspace, **kwargs}) or {"ok": True}
            os.chdir(self.workspace)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, restore_drill_cli.main(["--workspace", "."]))
        finally:
            os.chdir(previous)
            restore_drill_cli.restore_drill.run_drill = original
        self.assertTrue(pathlib.Path(captured["workspace"]).is_absolute())
        self.assertTrue(pathlib.Path(captured["receipt_root"]).is_absolute())
        self.assertEqual(self.workspace.resolve(), pathlib.Path(captured["workspace"]))

    def run_success(self, docker=None, **kwargs):
        run_uid = os.getuid() or 70
        run_gid = os.getgid() if os.getuid() else 70
        return restore_drill.run_drill(
            self.workspace, host_workspace="/host/dash", receipt_root=self.receipts,
            docker=docker or FakeDocker(), max_backup_age_seconds=10**12,
            max_restore_seconds=10**6, run_uid=run_uid, run_gid=run_gid,
            sleep=lambda _: None, **kwargs,
        )

    def test_success_runs_restore_invariants_roundtrip_cleanup_and_private_receipt(self):
        docker = FakeDocker()
        result = self.run_success(docker)
        self.assertTrue(result["ok"])
        self.assertTrue(result["integrityOk"])
        self.assertTrue(result["isolation"]["verified"])
        self.assertTrue(result["cleanup"]["removedCurrent"])
        self.assertIn("a" * 64, docker.removed)
        self.assertRegex(docker.spec["HostConfig"]["Mounts"][0]["Source"], r"^/host/dash/backups/admin-panel/restore-drills/\.source-.*\.dump$")
        self.assertTrue(result["isolation"]["sourceSha256Verified"])
        self.assertEqual("400", result["isolation"]["sourceMode"])
        self.assertTrue(result["cleanup"]["removedStagedSource"])
        self.assertTrue(result["cleanup"]["removedIdentityFiles"])
        self.assertFalse(list(self.receipts.glob(".source-*.dump")))
        self.assertFalse(list(self.receipts.glob(".passwd-*")))
        self.assertFalse(list(self.receipts.glob(".group-*")))
        self.assertTrue(any(command and command[0] == "vacuumdb" for command in docker.commands))
        self.assertTrue(any(command and command[0] == "pg_dump" for command in docker.commands))
        self.assertTrue(result["validation"]["playerLifeRecoveryContract"]["aliveTransitionVerified"])
        self.assertTrue(result["validation"]["offlineTeleportContract"]["moveVerified"])
        self.assertTrue(result["validation"]["characterTransferContract"]["importVerified"])
        self.assertTrue(any(command and command[0] == "psql" and "dash_life_candidate" in " ".join(command) for command in docker.commands))
        self.assertTrue(any(command and command[0] == "psql" and "dash_teleport_candidate" in " ".join(command) for command in docker.commands))
        self.assertTrue(any(command and command[0] == "psql" and "dash_transfer_candidate" in " ".join(command) for command in docker.commands))
        for command in docker.commands:
            connects = command and (command[0] in {"psql", "vacuumdb", "pg_dump"} or (command[0] == "pg_restore" and "--dbname=drill" in command))
            if connects:
                self.assertTrue(any(value in ("-U", "--username=dune") for value in command), command)
        receipt_files = [path for path in self.receipts.glob("*.json") if path.name != "latest.json"]
        self.assertEqual(1, len(receipt_files))
        self.assertEqual(0o600, receipt_files[0].stat().st_mode & 0o777)
        self.assertEqual(0o700, self.receipts.stat().st_mode & 0o777)
        status = restore_drill.status(self.receipts)
        self.assertTrue(status["ok"])
        self.assertTrue(status["latest"]["receiptHashValid"])

    def test_receipts_form_a_hash_chain(self):
        first = self.run_success()
        second = self.run_success()
        self.assertEqual(first["receiptSha256"], second["previousReceiptSha256"])
        rows = restore_drill.list_receipts(self.receipts)
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["receiptHashValid"] for row in rows))

    def test_restore_failure_is_receipted_and_container_is_removed(self):
        docker = FakeDocker(failure="restore")
        result = self.run_success(docker)
        self.assertFalse(result["ok"])
        self.assertIn("restore exploded", result["error"])
        self.assertIn("a" * 64, docker.removed)
        self.assertFalse(restore_drill.status(self.receipts)["ok"])

    def test_native_life_contract_failure_fails_integrity(self):
        result = self.run_success(FakeDocker(failure="life-contract"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["integrityOk"])
        self.assertIn("life-state recovery contract failed", result["error"])

    def test_native_offline_teleport_contract_failure_fails_integrity(self):
        result = self.run_success(FakeDocker(failure="teleport-contract"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["integrityOk"])
        self.assertIn("offline teleport contract failed", result["error"])

    def test_cleanup_failure_fails_closed(self):
        result = self.run_success(FakeDocker(cleanup_failure=True))
        self.assertFalse(result["ok"])
        self.assertFalse(result["integrityOk"])
        self.assertIn("cleanup failed", result["cleanup"]["error"])

    def test_policy_failure_is_distinct_from_restore_integrity(self):
        result = restore_drill.run_drill(
            self.workspace, host_workspace="/host/dash", receipt_root=self.receipts,
            docker=FakeDocker(), max_backup_age_seconds=0, max_restore_seconds=10**6,
            run_uid=os.getuid() or 70, run_gid=os.getgid() if os.getuid() else 70,
            sleep=lambda _: None,
        )
        self.assertTrue(result["integrityOk"])
        self.assertFalse(result["policyOk"])
        self.assertFalse(result["ok"])

    def test_exact_label_and_prefix_stale_cleanup(self):
        stale_id = "b" * 64
        docker = FakeDocker(stale=[
            {"Id": stale_id, "Names": ["/dash-restore-drill-old"], "Labels": {restore_drill.LABEL_KEY: "true"}, "State": "exited", "Created": 1},
            {"Id": "c" * 64, "Names": ["/someone-else"], "Labels": {restore_drill.LABEL_KEY: "true"}, "State": "exited", "Created": 1},
            {"Id": "d" * 64, "Names": ["/dash-restore-drill-not-ours"], "Labels": {}, "State": "exited", "Created": 1},
        ])
        result = self.run_success(docker)
        self.assertTrue(result["ok"])
        self.assertIn(stale_id, docker.removed)
        self.assertNotIn("c" * 64, docker.removed)
        self.assertNotIn("d" * 64, docker.removed)

    def test_only_exact_old_private_staging_files_are_pruned(self):
        self.receipts.mkdir(parents=True)
        stale = self.receipts / ".source-abandoned.dump"
        fresh = self.receipts / ".passwd-current"
        unrelated = self.receipts / "operator-note.txt"
        for path in (stale, fresh, unrelated):
            path.write_text("x", encoding="utf-8")
        os.utime(stale, (1, 1))
        removed = restore_drill._clean_stale_files(self.receipts, now_epoch=7 * 3600)
        self.assertEqual([stale.name], removed)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_concurrent_drill_lock_fails_fast(self):
        self.receipts.mkdir(parents=True)
        lock_path = self.receipts / ".restore-drill.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(restore_drill.RestoreDrillBusy):
                self.run_success()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
