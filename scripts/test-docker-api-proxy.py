#!/usr/bin/env python3
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "admin"))

from admin import rabbitmq_restore_drill, restore_drill  # noqa: E402
from scripts import docker_api_proxy as proxy  # noqa: E402


class DockerApiProxyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dune-docker-proxy-test-")
        base = pathlib.Path(self.temp.name)
        self.host_root = base / "host" / "backups" / "admin-panel"
        self.visible_root = base / "visible"
        self.host_root.mkdir(parents=True)
        self.visible_root.mkdir()
        proxy.RESTORE_HOST_ROOT = str(self.host_root)
        proxy.RESTORE_VISIBLE_ROOT = str(self.visible_root)

    def tearDown(self):
        self.temp.cleanup()

    def _file(self, relative):
        host_path = self.host_root / relative
        visible_path = self.visible_root / relative
        host_path.parent.mkdir(parents=True, exist_ok=True)
        visible_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(b"fixture")
        visible_path.write_bytes(b"fixture")
        return host_path

    def test_allows_bounded_postgres_restore_profile(self):
        source = self._file("restore-drill/.source-20261021T010203Z-a1b2c3d4e5f6.dump")
        passwd = self._file("restore-drill/.passwd-test")
        group = self._file("restore-drill/.group-test")
        spec = restore_drill.build_container_spec(
            source, host_passwd=passwd, host_group=group,
            run_uid=1000, run_gid=1000,
        )
        self.assertEqual(
            proxy.validate_restore_create("dash-restore-drill-a1b2c3d4e5f6", spec), "pg"
        )

    def test_rejects_privileged_or_host_network_restore_profile(self):
        source = self._file("restore-drill/.source-dump")
        passwd = self._file("restore-drill/.passwd-test")
        group = self._file("restore-drill/.group-test")
        spec = restore_drill.build_container_spec(
            source, host_passwd=passwd, host_group=group,
            run_uid=1000, run_gid=1000,
        )
        privileged = {**spec, "HostConfig": {**spec["HostConfig"], "Privileged": True}}
        host_network = {**spec, "HostConfig": {**spec["HostConfig"], "NetworkMode": "host"}}
        self.assertIsNone(proxy.validate_restore_create("dash-restore-drill-a1b2c3d4e5f6", privileged))
        self.assertIsNone(proxy.validate_restore_create("dash-restore-drill-a1b2c3d4e5f6", host_network))

    def test_rejects_root_equivalent_restart_helper_profile(self):
        helper = {
            "Image": "docker:27.5.1-cli",
            "Cmd": ["sh", "-lc", "id"],
            "Labels": {"com.snapetech.dune.role": "admin-restart-compose"},
            "HostConfig": {
                "NetworkMode": "host", "PidMode": "host", "Privileged": True,
                "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
            },
        }
        self.assertIsNone(proxy.validate_restore_create("dune-admin-restart-compose-test", helper))

    def test_rejects_restore_mount_outside_staged_backup_tree(self):
        source = self._file("restore-drill/.source-dump")
        passwd = self._file("restore-drill/.passwd-test")
        group = self._file("restore-drill/.group-test")
        spec = restore_drill.build_container_spec(
            source, host_passwd=passwd, host_group=group,
            run_uid=1000, run_gid=1000,
        )
        spec["HostConfig"]["Mounts"][0]["Source"] = "/etc/shadow"
        self.assertIsNone(proxy.validate_restore_create("dash-restore-drill-a1b2c3d4e5f6", spec))

    def test_allows_isolated_rabbitmq_restore_profile(self):
        state = self.host_root / "rabbitmq-restore-drill" / ".stage-test" / "admin-state"
        state.mkdir(parents=True)
        (self.visible_root / "rabbitmq-restore-drill" / ".stage-test" / "admin-state").mkdir(parents=True)
        config = self._file("rabbitmq-restore-drill/.stage-test/config/99-dune.conf")
        plugins = self._file("rabbitmq-restore-drill/.stage-test/config/enabled_plugins")
        passwd = self._file("rabbitmq-restore-drill/.stage-test/passwd")
        group = self._file("rabbitmq-restore-drill/.stage-test/group")
        spec = rabbitmq_restore_drill.build_container_spec(
            state, config, plugins, passwd, group,
            hostname="restore-admin", image="registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:2118731-0-shipping",
            run_uid=1000, run_gid=1000,
        )
        name = "dash-rmq-restore-drill-admin-a1b2c3d4e5"
        self.assertEqual(proxy.validate_restore_create(name, spec), "rabbitmq")

    def test_only_approved_rabbitmq_commands_are_available(self):
        self.assertTrue(proxy.allowed_rabbitmq_command([
            "rabbitmqctl", "-q", "list_queues", "name", "consumers", "messages", "state",
        ]))
        self.assertTrue(proxy.allowed_rabbitmq_command(["rabbitmqctl", "-q", "delete_queue", "12345_queue"]))
        eval_code = (
            'Outer = base64:decode(<<"eyJ4IjoxfQ==">>), '
            'XName = rabbit_misc:r(<<"/">>, exchange, <<"heartbeats">>), '
            'X = rabbit_exchange:lookup_or_die(XName), '
            'MsgId = list_to_binary("dash-servicebroadcast-" ++ integer_to_list(erlang:system_time(millisecond))), '
            'P = {list_to_atom("P_basic"), <<"Content">>, undefined, [], undefined, undefined, undefined, undefined, undefined, MsgId, undefined, undefined, <<"fls">>, <<"fls_backend">>, undefined}, '
            'Content = rabbit_basic:build_content(P, Outer), '
            '{ok, Msg} = rabbit_basic:message(XName, <<"notifications">>, Content), '
            'Result = rabbit_queue_type:publish_at_most_once(X, Msg), '
            'io:format("publish=~p exchange=heartbeats routing=notifications app_id=fls_backend user_id=fls~n", [Result]).'
        )
        self.assertTrue(proxy.allowed_rabbitmq_command(["rabbitmqctl", "eval", eval_code]))
        self.assertFalse(proxy.allowed_rabbitmq_command([
            "rabbitmqctl", "eval", eval_code.replace("rabbit_exchange:lookup_or_die(XName)", "os:cmd(\"id\")"),
        ]))
        self.assertFalse(proxy.allowed_rabbitmq_command(["rabbitmqctl", "eval", "os:cmd(\"id\")."]))
        self.assertFalse(proxy.allowed_rabbitmq_command(["/bin/sh", "-c", "id"]))

    def test_project_logs_are_readable_with_a_bounded_tail(self):
        handler = object.__new__(proxy.ProxyHandler)
        identifier = "a" * 64
        inspected = {"Config": {"Labels": {
            proxy.PROJECT_LABEL: proxy.PROJECT,
            proxy.SERVICE_LABEL: "gateway",
        }}}
        with patch.object(proxy, "project_container", return_value=(inspected, "project")):
            upstream_path, timeout, max_bytes, action = handler._authorize(
                "GET", f"/containers/{identifier}/logs",
                {"tail": ["10000"], "stdout": ["1"], "stderr": ["1"]}, b"",
            )
        self.assertEqual(timeout, 15)
        self.assertEqual(max_bytes, 4 * 1024 * 1024)
        self.assertIsNone(action)
        self.assertIn("tail=10000", upstream_path)
        self.assertIn("follow=0", upstream_path)
        self.assertEqual(proxy.ProxyHandler._route_name(f"/containers/{identifier}/logs"), "container-logs")

    def test_project_logs_reject_follow_and_unbounded_tail(self):
        handler = object.__new__(proxy.ProxyHandler)
        identifier = "b" * 64
        inspected = {"Config": {"Labels": {
            proxy.PROJECT_LABEL: proxy.PROJECT,
            proxy.SERVICE_LABEL: "gateway",
        }}}
        with patch.object(proxy, "project_container", return_value=(inspected, "project")):
            for query in ({"follow": ["1"]}, {"tail": ["10001"]}, {"tail": ["all"]}):
                with self.subTest(query=query), self.assertRaises(proxy.ProxyError) as error:
                    handler._authorize("GET", f"/containers/{identifier}/logs", query, b"")
                self.assertEqual(error.exception.status, 400)

    def test_broker_worker_and_backlog_limits_are_bounded(self):
        self.assertEqual(proxy.ProxyServer.max_workers, 48)
        self.assertEqual(proxy.ProxyServer.request_queue_size, 128)
        self.assertEqual(proxy.MAX_STATS_IN_FLIGHT, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
