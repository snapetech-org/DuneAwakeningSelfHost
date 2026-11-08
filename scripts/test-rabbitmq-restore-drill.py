#!/usr/bin/env python3

import datetime
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))
import rabbitmq_restore_drill as drill


def archive(path, entries):
    with tarfile.open(path, "w:gz") as output:
        for name, value in entries.items():
            if isinstance(value, tarfile.TarInfo):
                output.addfile(value)
                continue
            data = value.encode() if isinstance(value, str) else bytes(value)
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            output.addfile(member, io.BytesIO(data))


class FakeDocker:
    def __init__(self, ping=True, remove_error=False):
        self.ping = ping
        self.remove_error = remove_error
        self.specs = {}
        self.removed = []
        self.commands = []
        self.stale = []

    def list_rabbitmq_drill_containers(self):
        return list(self.stale)

    def create_container(self, name, spec):
        identifier = f"container-{len(self.specs) + 1:02d}-fixture"
        self.specs[identifier] = spec
        return identifier

    def start_container(self, identifier):
        return None

    def inspect_container(self, identifier):
        spec = self.specs[identifier]
        return {
            "Config": {"User": spec["User"], "Hostname": spec["Hostname"]},
            "HostConfig": dict(spec["HostConfig"]),
            "Mounts": [
                {"Destination": row["Target"], "RW": not row.get("ReadOnly", False)}
                for row in spec["HostConfig"]["Mounts"]
            ],
        }

    def exec(self, identifier, argv, timeout=60):
        self.commands.append(list(argv))
        text = " ".join(argv)
        if "rabbitmq-diagnostics" in text:
            return (0, "Ping succeeded\n") if self.ping else (1, "not ready\n")
        if argv[:2] == ["/bin/sh", "-c"]:
            return 0, "1 3\n" if argv[4] == "messages" else "1\n"
        if " list_vhosts " in f" {text} ":
            return 0, "/\n"
        if " list_users " in f" {text} ":
            return 0, "fixture-user\n"
        if " list_queues " in f" {text} ":
            return 0, "private-queue 3\n"
        if " list_exchanges " in f" {text} ":
            return 0, "private-exchange direct true\n"
        if " list_bindings " in f" {text} ":
            return 0, "private-source private-destination\n"
        return 0, "status output\n"

    def logs(self, identifier):
        return "private-log-secret-must-not-leak"

    def remove_container(self, identifier, force=True):
        if self.remove_error:
            raise RuntimeError("cleanup refused")
        self.removed.append(identifier)


class RabbitMQRestoreDrillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.backup = self.root / "backups" / stamp
        self.backup.mkdir(parents=True)
        (self.backup / "manifest.txt").write_text(f"created_utc={stamp}\n", encoding="utf-8")
        archive(self.backup / "config.tgz", {
            "config/rabbitmq-enabled-plugins": "[rabbitmq_management].\n",
            "config/rabbitmq-admin.conf": "auth_backends.1 = internal\n",
            "config/rabbitmq-game.conf": "listeners.tcp = none\nlisteners.ssl.default = 5672\n",
        })
        archive(self.backup / "config-tls.tgz", {
            "config/tls/rabbitmq/ca.crt": "fixture-ca",
            "config/tls/rabbitmq/server.crt": "fixture-cert",
            "config/tls/rabbitmq/server.key": "fixture-key",
        })
        for name, hostname in (("admin", "admin-rmq"), ("game", "game-rmq")):
            archive(self.backup / f"rabbitmq-{name}.tgz", {
                f"mnesia/rabbit@{hostname}/node-type.txt": "disc\n",
                f"mnesia/rabbit@{hostname}/rabbit_vhost.DCD": b"fixture-vhost-state",
                f"mnesia/rabbit@{hostname}/rabbit_user.DCD": b"fixture-user-state",
                f"mnesia/rabbit@{hostname}.pid": "123\n",
                f"mnesia/rabbit@{hostname}-plugins-expand/plugin.marker": "expanded\n",
                ".erlang.cookie": "fixture-cookie",
            })

    def make_scheduled_backup(self, stamp=None):
        stamp = stamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = self.root / "backups" / "admin-panel" / "maintenance" / f"{stamp}-browser-fixture"
        candidate.mkdir(parents=True)
        archive(candidate / "config-and-env.tgz", {
            "config/rabbitmq-enabled-plugins": "[rabbitmq_management].\n",
            "config/rabbitmq-admin.conf": "auth_backends.1 = internal\n",
            "config/rabbitmq-game.conf": "listeners.tcp = none\nlisteners.ssl.default = 5672\n",
            "config/tls/rabbitmq/ca.crt": "fixture-ca",
            "config/tls/rabbitmq/server.crt": "fixture-cert",
            "config/tls/rabbitmq/server.key": "fixture-key",
        })
        state = {}
        for name, hostname in (("admin", "admin-rmq"), ("game", "game-rmq")):
            prefix = f"rabbitmq/{name}"
            state.update({
                f"{prefix}/mnesia/rabbit@{hostname}/node-type.txt": "disc\n",
                f"{prefix}/mnesia/rabbit@{hostname}/rabbit_vhost.DCD": b"fixture-vhost-state",
                f"{prefix}/mnesia/rabbit@{hostname}/rabbit_user.DCD": b"fixture-user-state",
                f"{prefix}/mnesia/rabbit@{hostname}.pid": "123\n",
                f"{prefix}/.erlang.cookie": "fixture-cookie",
            })
        archive(candidate / "rabbitmq.tgz", state)
        created = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        manifest = {
            "action": "backup", "target": "all", "createdAt": created,
            "coverage": {
                "complete": True, "missing": [],
                "captured": ["config", "rabbitmq"], "required": ["config", "rabbitmq"],
            },
            "artifacts": {
                "config": {
                    "path": f"/workspace/backups/{candidate.name}/config-and-env.tgz",
                    "bytes": (candidate / "config-and-env.tgz").stat().st_size,
                    "added": [{"archiveName": "config"}], "skipped": [],
                },
                "rabbitmq": {
                    "path": f"/workspace/backups/{candidate.name}/rabbitmq.tgz",
                    "bytes": (candidate / "rabbitmq.tgz").stat().st_size,
                    "added": [{"archiveName": "rabbitmq"}], "skipped": [],
                },
            },
        }
        (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return candidate

    def run_drill(self, docker=None, **updates):
        options = {
            "host_workspace": self.root,
            "backup_set": self.backup,
            "receipt_root": self.root / "backups" / "admin-panel" / "rabbitmq-restore-drills",
            "docker": docker or FakeDocker(),
            "run_uid": os.getuid(), "run_gid": os.getgid(),
            "sleep": lambda _: None, "readiness_seconds": 0.01,
        }
        options.update(updates)
        return drill.run_drill(self.root, **options)

    def test_selects_newest_complete_confined_set(self):
        self.assertEqual(self.backup, drill.select_backup_set(self.root))
        outside = self.root.parent / "outside-rmq-backup"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        with self.assertRaisesRegex(ValueError, "outside"):
            drill.select_backup_set(self.root, outside)
        link = self.root / "backups" / "linked"
        link.symlink_to(self.backup, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            drill.select_backup_set(self.root, link)

    def test_selects_and_rehearses_current_scheduled_full_backup(self):
        scheduled = self.make_scheduled_backup()
        self.assertEqual(scheduled, drill.select_backup_set(self.root))
        result = self.run_drill(backup_set=scheduled)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual("scheduled-full", result["backupLayout"])
        self.assertEqual(
            result["brokers"]["admin"]["sourceSha256"],
            result["brokers"]["game"]["sourceSha256"],
        )
        self.assertEqual(
            result["configurationArchives"]["configSha256"],
            result["configurationArchives"]["tlsSha256"],
        )

    def test_incomplete_scheduled_manifest_is_rejected(self):
        scheduled = self.make_scheduled_backup()
        manifest = json.loads((scheduled / "manifest.json").read_text(encoding="utf-8"))
        manifest["coverage"]["complete"] = False
        (scheduled / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(FileNotFoundError, "no complete"):
            drill.select_backup_set(self.root, scheduled)

    def test_receipt_root_is_confined_before_creation(self):
        outside = self.root.parent / "outside-rabbitmq-receipts"
        if outside.exists():
            self.fail(f"fixture path unexpectedly exists: {outside}")
        with self.assertRaisesRegex(ValueError, "beneath workspace/backups"):
            self.run_drill(receipt_root=outside)
        self.assertFalse(outside.exists())

    def test_directory_touch_cannot_select_an_older_backup(self):
        old = self.root / "backups" / "20200101T000000Z"
        shutil.copytree(self.backup, old)
        (old / "manifest.txt").write_text("created_utc=20200101T000000Z\n")
        os.utime(old, (2000000000, 2000000000))
        self.assertEqual(self.backup, drill.select_backup_set(self.root))
        self.assertLess(drill.backup_created_epoch(old), 1600000000)

    def test_fresh_file_copy_cannot_refresh_old_manifest_age(self):
        (self.backup / "manifest.txt").write_text("created_utc=20200101T000000Z\n")
        self.assertLess(drill.backup_created_epoch(self.backup), 1600000000)

    def test_old_layer_limits_backup_freshness(self):
        os.utime(self.backup / "rabbitmq-game.tgz", (1000000000, 1000000000))
        self.assertEqual(1000000000, drill.backup_created_epoch(self.backup))

    def test_large_queue_inventory_is_counted_inside_the_container(self):
        executable = self.root / "rabbitmqctl"
        executable.write_text("#!/bin/sh\nawk 'BEGIN { for(i=0;i<5000;i++) print 2 }'\n")
        executable.chmod(0o700)
        output = subprocess.check_output(
            ["/bin/sh", "-c", drill.COUNT_TOPOLOGY_SCRIPT, "fixture", "messages", "list_queues"],
            env={**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"]}, text=True)
        self.assertEqual("5000 10000\n", output)

    def test_invalid_count_output_fails_closed(self):
        for output in ["unexpected table\n", "-1 3\n", "1 2\n3 4\n"]:
            docker = FakeDocker()
            docker.exec = lambda *args, **kwargs: (0, output)
            with self.subTest(output=output), self.assertRaises(drill.RabbitMQRestoreDrillError):
                drill._topology_count(docker, "fixture", ["list_queues"], "queues", messages=True)

    def test_safe_extraction_rejects_traversal_links_and_bounds(self):
        malicious = self.root / "traversal.tgz"
        archive(malicious, {"../../escape": "bad"})
        with self.assertRaisesRegex(ValueError, "escapes"):
            drill.safe_extract_state(malicious, self.root / "traversal")
        link = tarfile.TarInfo("mnesia/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        malicious = self.root / "link.tgz"
        archive(malicious, {"mnesia/link": link})
        with self.assertRaisesRegex(ValueError, "regular files"):
            drill.safe_extract_state(malicious, self.root / "link")
        bounded = self.root / "bounded.tgz"
        archive(bounded, {"mnesia/rabbit@admin-rmq/a": "a", "mnesia/rabbit@admin-rmq/b": "b"})
        with mock.patch.object(drill, "MAX_ARCHIVE_MEMBERS", 1), self.assertRaisesRegex(ValueError, "member bound"):
            drill.safe_extract_state(bounded, self.root / "bounded")
        consolidated = self.root / "consolidated.tgz"
        archive(consolidated, {
            "rabbitmq/admin/mnesia/rabbit@admin-rmq/node-type.txt": "disc\n",
            "rabbitmq/game/mnesia/rabbit@game-rmq/node-type.txt": "disc\n",
        })
        extracted = self.root / "prefixed"
        drill.safe_extract_state(consolidated, extracted, "rabbitmq/admin")
        self.assertTrue((extracted / "mnesia" / "rabbit@admin-rmq" / "node-type.txt").is_file())
        self.assertFalse((extracted / "rabbitmq").exists())
        self.assertFalse((self.root / "escape").exists())

    def test_required_configuration_members_must_be_unique(self):
        duplicate = self.backup / "config.tgz"
        with tarfile.open(duplicate, "w:gz") as output:
            for value in (b"first", b"second"):
                member = tarfile.TarInfo("config/rabbitmq-admin.conf")
                member.size = len(value)
                output.addfile(member, io.BytesIO(value))
        result = self.run_drill()
        self.assertFalse(result["ok"])
        self.assertIn("duplicate required member", result["error"])

    def test_container_spec_has_no_network_ports_or_live_mount(self):
        paths = [self.root / name for name in ("state", "config", "plugins", "passwd", "group")]
        spec = drill.build_container_spec(*paths, hostname="admin-rmq", run_uid=1000, run_gid=1001)
        host = spec["HostConfig"]
        self.assertEqual("none", host["NetworkMode"])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(["ALL"], host["CapDrop"])
        self.assertNotIn("PortBindings", host)
        self.assertEqual("1000:1001", spec["User"])
        self.assertEqual("admin-rmq", spec["Hostname"])
        self.assertEqual(str(paths[0]), host["Mounts"][0]["Source"])
        self.assertFalse(host["Mounts"][0]["ReadOnly"])
        with self.assertRaisesRegex(ValueError, "image"):
            drill.build_container_spec(*paths, hostname="admin-rmq", image="bad image", run_uid=1000, run_gid=1001)

    def test_success_boots_both_copies_and_emits_name_free_private_receipt(self):
        docker = FakeDocker()
        result = self.run_drill(docker)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["integrityOk"])
        self.assertFalse(result["liveRabbitMQTouched"])
        self.assertFalse(result["networkCreated"])
        self.assertEqual(2, len(docker.specs))
        self.assertEqual(set(docker.specs), set(docker.removed))
        readiness_commands = [
            command for command in docker.commands
            if command[:2] == ["rabbitmq-diagnostics", "-q"]
        ]
        self.assertEqual(
            [["rabbitmq-diagnostics", "-q", "check_running"]] * 2,
            readiness_commands,
        )
        user_commands = [command for command in docker.commands if "list_users" in command]
        self.assertEqual(2, len(user_commands))
        for command in user_commands:
            self.assertEqual("rows", command[4])
            self.assertEqual(["list_users"], command[5:])
            self.assertIn("--no-table-headers", command[2])
        for name in ("admin", "game"):
            row = result["brokers"][name]
            self.assertTrue(row["isolation"]["verified"])
            self.assertEqual({"vhosts": 1, "users": 1, "queues": 1, "exchanges": 1, "bindings": 1, "messages": 3}, row["topology"])
            self.assertTrue(row["stalePidRemoved"])
            self.assertRegex(row["sourceSha256"], r"^[0-9a-f]{64}$")
        encoded = json.dumps(result, sort_keys=True)
        for secret_name in ("private-queue", "private-exchange", "private-source", "fixture-user", "fixture-cookie"):
            self.assertNotIn(secret_name, encoded)
        receipt_root = self.root / "backups" / "admin-panel" / "rabbitmq-restore-drills"
        self.assertEqual(0o600, (receipt_root / "latest.json").stat().st_mode & 0o777)
        self.assertEqual([], list(receipt_root.glob(".stage-*")))
        self.assertTrue(drill.status(receipt_root)["ok"])

    def test_failed_readiness_is_receipted_without_log_disclosure_and_cleans(self):
        docker = FakeDocker(ping=False)
        result = self.run_drill(docker)
        self.assertFalse(result["ok"])
        self.assertIn("logSha256=", result["error"])
        self.assertNotIn("private-log-secret", json.dumps(result))
        self.assertEqual(1, len(docker.removed))
        self.assertTrue(result["cleanup"]["stageRemoved"])
        status = drill.status(self.root / "backups" / "admin-panel" / "rabbitmq-restore-drills")
        self.assertFalse(status["ok"])

    def test_receipt_hash_detects_tampering(self):
        result = self.run_drill()
        receipt_root = self.root / "backups" / "admin-panel" / "rabbitmq-restore-drills"
        path = receipt_root / f"{result['id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["ok"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertFalse(drill.status(receipt_root)["ok"])
        self.assertFalse(drill.status(receipt_root)["latest"]["receiptHashValid"])

    def test_receipt_chain_detects_removed_or_relinked_predecessor(self):
        first = self.run_drill()
        second = self.run_drill()
        receipt_root = self.root / "backups" / "admin-panel" / "rabbitmq-restore-drills"
        first_path = receipt_root / f"{first['id']}.json"
        first_path.unlink()
        status = drill.status(receipt_root)
        self.assertFalse(status["ok"])
        self.assertFalse(status["history"]["anchorBindingValid"])
        with self.assertRaisesRegex(drill.RabbitMQRestoreDrillError, "chain is invalid"):
            self.run_drill()

    def test_only_owned_stale_containers_and_old_private_stages_are_removed(self):
        docker = FakeDocker()
        now = int(__import__("time").time())
        docker.stale = [
            {"Id": "a" * 64, "Names": ["/dash-rmq-restore-drill-old"], "Labels": {drill.LABEL_KEY: "true"}, "State": "exited", "Created": now},
            {"Id": "b" * 64, "Names": ["/unrelated"], "Labels": {drill.LABEL_KEY: "true"}, "State": "exited", "Created": now},
            {"Id": "c" * 64, "Names": ["/dash-rmq-restore-drill-live"], "Labels": {drill.LABEL_KEY: "true"}, "State": "running", "Created": now},
        ]
        receipt_root = self.root / "backups" / "admin-panel" / "rabbitmq-restore-drills"
        old = receipt_root / ".stage-old"
        old.mkdir(parents=True)
        os.utime(old, (now - 7 * 3600, now - 7 * 3600))
        result = self.run_drill(docker)
        self.assertTrue(result["ok"])
        self.assertIn(("a" * 64), docker.removed)
        self.assertNotIn(("b" * 64), docker.removed)
        self.assertNotIn(("c" * 64), docker.removed)
        self.assertEqual([".stage-old"], result["cleanup"]["staleStagesRemoved"])

    def test_wrong_node_identity_and_cleanup_failure_fail_closed(self):
        wrong = self.backup / "rabbitmq-admin.tgz"
        archive(wrong, {"mnesia/rabbit@wrong-host/node-type.txt": "disc"})
        result = self.run_drill()
        self.assertFalse(result["ok"])
        self.assertIn("node identity", result["error"])
        self.setUp_fixture_again()
        docker = FakeDocker(remove_error=True)
        result = self.run_drill(docker)
        self.assertFalse(result["ok"])
        self.assertIn("cleanup refused", result["error"])

    def setUp_fixture_again(self):
        archive(self.backup / "rabbitmq-admin.tgz", {
            "mnesia/rabbit@admin-rmq/node-type.txt": "disc\n",
            "mnesia/rabbit@admin-rmq/rabbit_vhost.DCD": b"fixture-vhost-state",
            "mnesia/rabbit@admin-rmq/rabbit_user.DCD": b"fixture-user-state",
            "mnesia/rabbit@admin-rmq.pid": "123\n",
        })


if __name__ == "__main__":
    unittest.main()
