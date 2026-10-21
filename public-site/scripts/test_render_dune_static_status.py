#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import time
import unittest


SCRIPT = pathlib.Path(__file__).with_name("render-dune-static-status.sh")


class StaticStatusRenderTest(unittest.TestCase):
    def render_status(self, restart_state, status_lines=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            dune_root = root / "dune"
            static_dir = root / "static"
            bin_dir = root / "bin"
            status_file = static_dir / "status.html"
            index_file = static_dir / "index.html"
            restart_state_file = dune_root / "backups" / "admin-panel" / "restart-jobs.json"
            scripts_dir = dune_root / "scripts"
            source_static = dune_root / "public-site" / "static"
            scripts_dir.mkdir(parents=True)
            source_static.mkdir(parents=True)
            static_dir.mkdir()
            bin_dir.mkdir()
            restart_state_file.parent.mkdir(parents=True)
            (source_static / "style.css").write_text("body { color: #fff; }\n", encoding="utf-8")
            (source_static / "app.js").write_text("window.__dunePublicSiteTest = true;\n", encoding="utf-8")
            restart_state_file.write_text(json.dumps(restart_state), encoding="utf-8")
            index_file.write_text(
                "<html><body><!-- STATUS_BEGIN --><div id=\"server-status\"></div><!-- STATUS_END --></body></html>",
                encoding="utf-8",
            )
            (dune_root / ".env").write_text("DUNE_WORLD_PARTITION_COUNT=31\n", encoding="utf-8")
            status_sh = scripts_dir / "status.sh"
            if status_lines is None:
                status_lines = (
                    "current_ready_alive=3 current_alive_active=3 active_servers=3 partitions=31 "
                    "game_sg_connections=6 admin_sg_connections=4\n"
                    "core_ready_alive=2 core_alive_active=2 core_active_servers=2 core_partitions=2\n"
                    "FLS publication health: healthy\n"
                )
            status_sh.write_text(
                "#!/bin/sh\n"
                f"cat <<'EOF'\n{status_lines}EOF\n",
                encoding="utf-8",
            )
            status_sh.chmod(0o755)
            fake_docker = bin_dir / "docker"
            fake_docker.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import datetime
                    import json
                    import sys
                    if sys.argv[1] == "ps":
                        for service in ("survival", "deep-desert", "deep-desert-pvp"):
                            print(f"id-{service}\\t{service}")
                    elif sys.argv[1] == "inspect":
                        print(json.dumps([{
                            "State": {"Status": "running", "StartedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()},
                            "RestartCount": 0,
                        }]))
                    else:
                        raise SystemExit(2)
                    """
                ),
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "DUNE_ROOT": str(dune_root),
                "INDEX_FILE": str(index_file),
                "STATUS_FILE": str(status_file),
                "STATIC_DIR": str(static_dir),
                "SOURCE_INDEX_FILE": str(root / "missing-source-index.html"),
                "CONFIGURE_SCRIPT": str(root / "missing-configure.sh"),
                "DRIFT_CHECK_SCRIPT": str(root / "missing-drift.sh"),
                "DUNE_PUBLIC_RESTART_STATE_FILE": str(restart_state_file),
            }
            subprocess.run([str(SCRIPT)], env=env, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return status_file.read_text(encoding="utf-8")

    def test_empty_browser_assets_are_seeded_before_version_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            dune_root = root / "dune"
            static_dir = root / "static"
            source_static = dune_root / "public-site" / "static"
            scripts_dir = dune_root / "scripts"
            bin_dir = root / "bin"
            restart_state_file = dune_root / "backups" / "admin-panel" / "restart-jobs.json"
            for directory in (source_static, scripts_dir, static_dir, bin_dir, restart_state_file.parent):
                directory.mkdir(parents=True, exist_ok=True)
            (source_static / "style.css").write_text("body { background: #101010; }\n", encoding="utf-8")
            (source_static / "app.js").write_text("window.__assetSeeded = true;\n", encoding="utf-8")
            (static_dir / "style.css").write_text("", encoding="utf-8")
            (static_dir / "app.js").write_text("", encoding="utf-8")
            (static_dir / "index.html").write_text(
                "<html><head><link rel=\"stylesheet\" href=\"style.css\"></head>\n"
                "<body>\n"
                "<!-- STATUS_BEGIN -->\n"
                "<div id=\"server-status\"></div>\n"
                "<!-- STATUS_END -->\n"
                "<script src=\"app.js\" defer></script>\n"
                "</body></html>\n",
                encoding="utf-8",
            )
            restart_state_file.write_text('{"jobs":[]}', encoding="utf-8")
            (dune_root / ".env").write_text("DUNE_WORLD_PARTITION_COUNT=31\n", encoding="utf-8")
            status_sh = scripts_dir / "status.sh"
            status_sh.write_text(
                "#!/bin/sh\n"
                "echo 'current_ready_alive=31 current_alive_active=31 active_servers=31 partitions=31 game_sg_connections=31 admin_sg_connections=1'\n",
                encoding="utf-8",
            )
            status_sh.chmod(0o755)
            fake_docker = bin_dir / "docker"
            fake_docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "DUNE_ROOT": str(dune_root),
                "INDEX_FILE": str(static_dir / "index.html"),
                "STATUS_FILE": str(static_dir / "status.html"),
                "STATIC_DIR": str(static_dir),
                "SOURCE_INDEX_FILE": str(root / "missing-source-index.html"),
                "CONFIGURE_SCRIPT": str(root / "missing-configure.sh"),
                "DRIFT_CHECK_SCRIPT": str(root / "missing-drift.sh"),
                "DUNE_PUBLIC_RESTART_STATE_FILE": str(restart_state_file),
            }

            subprocess.run([str(SCRIPT)], env=env, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            self.assertGreater((static_dir / "style.css").stat().st_size, 0)
            self.assertGreater((static_dir / "app.js").stat().st_size, 0)
            index_html = (static_dir / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("e3b0c44298fc", index_html)
            self.assertRegex(index_html, r'style\.css\?v=[0-9a-f]{12}')
            self.assertRegex(index_html, r'app\.js\?v=[0-9a-f]{12}')

    def test_live_index_is_not_replaced_by_template_while_health_check_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            dune_root = root / "dune"
            static_dir = root / "static"
            source_static = dune_root / "public-site" / "static"
            scripts_dir = dune_root / "scripts"
            bin_dir = root / "bin"
            marker = root / "health-check-started"
            for directory in (source_static, scripts_dir, static_dir, bin_dir):
                directory.mkdir(parents=True, exist_ok=True)
            (source_static / "style.css").write_text("body { color: #fff; }\n", encoding="utf-8")
            (source_static / "app.js").write_text("window.test = true;\n", encoding="utf-8")
            (source_static / "index.html").write_text(
                "<html><body><!-- STATUS_BEGIN --><div>PLACEHOLDER_STATUS</div><!-- STATUS_END --></body></html>",
                encoding="utf-8",
            )
            live_index = static_dir / "index.html"
            live_index.write_text(
                "<html><body><!-- STATUS_BEGIN --><div>LIVE_STATUS_SENTINEL</div><!-- STATUS_END --></body></html>",
                encoding="utf-8",
            )
            status_sh = scripts_dir / "status.sh"
            status_sh.write_text(
                "#!/bin/sh\n"
                f"touch '{marker}'\n"
                "sleep 1\n"
                "echo 'current_ready_alive=2 current_alive_active=2 active_servers=2 partitions=31 game_sg_connections=4 admin_sg_connections=2'\n"
                "echo 'core_ready_alive=2 core_alive_active=2 core_active_servers=2 core_partitions=2'\n"
                "echo 'FLS publication health: healthy'\n",
                encoding="utf-8",
            )
            status_sh.chmod(0o755)
            fake_docker = bin_dir / "docker"
            fake_docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_docker.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "DUNE_ROOT": str(dune_root),
                "INDEX_FILE": str(live_index),
                "STATUS_FILE": str(static_dir / "status.html"),
                "STATIC_DIR": str(static_dir),
                "SOURCE_INDEX_FILE": str(source_static / "index.html"),
                "CONFIGURE_SCRIPT": str(root / "missing-configure.sh"),
                "DRIFT_CHECK_SCRIPT": str(root / "missing-drift.sh"),
            }

            process = subprocess.Popen(
                [str(SCRIPT)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            during_render = live_index.read_text(encoding="utf-8")
            self.assertIn("LIVE_STATUS_SENTINEL", during_render)
            self.assertNotIn("PLACEHOLDER_STATUS", during_render)
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("<dd>Online</dd>", live_index.read_text(encoding="utf-8"))

    def test_elapsed_time_uses_completed_maintenance_job(self):
        executed_at = time.time() - 3600
        html = self.render_status({
            "jobs": [{
                "id": "daily",
                "status": "executed",
                "action": "restart",
                "execute": True,
                "target": "all",
                "targetLabel": "All services",
                "backup": True,
                "executedAt": executed_at,
            }]
        })

        self.assertIn("Since maintenance", html)
        self.assertIn("<strong>Last maintenance</strong>", html)
        self.assertIn("<strong>Backup</strong> requested", html)
        self.assertIn("Most recent container restart", html)

    def test_sleeping_dynamic_maps_do_not_degrade_public_status(self):
        html = self.render_status({"jobs": []})

        self.assertIn("<dd>Online</dd>", html)
        self.assertIn("<dd>Healthy</dd>", html)
        self.assertIn("<dd>Available</dd>", html)
        self.assertIn("Travel destinations start automatically when requested.", html)

    def test_missing_required_core_map_marks_server_recovering(self):
        html = self.render_status(
            {"jobs": []},
            status_lines=(
                "current_ready_alive=1 current_alive_active=1 active_servers=1 partitions=31 "
                "game_sg_connections=4 admin_sg_connections=2\n"
                "core_ready_alive=1 core_alive_active=1 core_active_servers=1 core_partitions=2\n"
                "FLS publication health: healthy\n"
            ),
        )

        self.assertIn("<dd>Recovering</dd>", html)
        self.assertIn("<dd>Degraded</dd>", html)
        self.assertIn("Required core services are recovering.", html)

    def test_unpublished_fls_limits_access_without_degrading_world(self):
        html = self.render_status(
            {"jobs": []},
            status_lines=(
                "current_ready_alive=2 current_alive_active=2 active_servers=2 partitions=31 "
                "game_sg_connections=4 admin_sg_connections=2\n"
                "core_ready_alive=2 core_alive_active=2 core_active_servers=2 core_partitions=2\n"
                "FLS publication health: degraded\n"
            ),
        )

        self.assertIn("<dd>Online</dd>", html)
        self.assertIn("<dd>Healthy</dd>", html)
        self.assertIn("<dd>Limited</dd>", html)
        self.assertIn("Server-browser publication is catching up. Existing players remain connected.", html)

    def test_runtime_detail_describes_warm_on_demand_policy(self):
        html = self.render_status({"jobs": []})

        self.assertIn("<strong>Warm maps</strong>", html)
        self.assertIn("<strong>Map policy</strong> destinations start on demand", html)
        self.assertNotIn("<strong>Running maps</strong>", html)

    def test_elapsed_time_uses_failed_maintenance_attempt(self):
        executed_at = time.time() - 7200
        html = self.render_status({
            "jobs": [{
                "id": "daily",
                "status": "failed",
                "action": "restart",
                "execute": True,
                "target": "all",
                "targetLabel": "All services",
                "backup": True,
                "executedAt": executed_at,
            }]
        })

        self.assertIn("Maintenance failed", html)
        self.assertNotIn("Since maintenance", html)
        self.assertIn("<strong>Last maintenance attempt</strong>", html)
        self.assertIn("<strong>Status</strong> failed", html)
        self.assertIn("<strong>Backup</strong> requested", html)

    def test_failed_readiness_timeout_is_cleared_after_farm_recovers(self):
        html = self.render_status(
            {
                "jobs": [{
                    "id": "daily",
                    "status": "failed",
                    "action": "restart",
                    "execute": True,
                    "target": "all",
                    "targetLabel": "All services",
                    "backup": True,
                    "executedAt": time.time() - 60,
                }]
            },
            status_lines=(
                "current_ready_alive=31 current_alive_active=31 active_servers=31 partitions=31 "
                "game_sg_connections=31 admin_sg_connections=4\n"
                "core_ready_alive=2 core_alive_active=2 core_active_servers=2 core_partitions=2\n"
                "FLS publication health: healthy\n"
            ),
        )

        self.assertIn("Since maintenance", html)
        self.assertNotIn("Maintenance failed", html)
        self.assertIn("recovered after readiness timeout", html)
        self.assertIn("Farm reached ready/active after the maintenance readiness timeout.", html)

    def test_upcoming_maintenance_is_visible_before_stale_failure(self):
        html = self.render_status({
            "jobs": [
                {
                    "id": "failed-daily",
                    "status": "failed",
                    "action": "restart",
                    "execute": True,
                    "target": "all",
                    "targetLabel": "All services",
                    "backup": True,
                    "executedAt": time.time() - (48 * 3600),
                },
                {
                    "id": "next-daily",
                    "status": "scheduled",
                    "action": "restart",
                    "execute": True,
                    "target": "all",
                    "targetLabel": "All services",
                    "backup": True,
                    "runAt": time.time() + 3600,
                },
            ]
        })

        self.assertIn("Maintenance scheduled", html)
        self.assertIn("<strong>Next maintenance</strong>", html)
        self.assertIn("<strong>Backup</strong> requested before restart", html)
        self.assertIn("<strong>Last maintenance attempt</strong>", html)
        self.assertIn("<strong>Last result</strong> failed", html)
        self.assertNotIn("<strong>Schedule</strong> stale", html)

    def test_overdue_maintenance_is_visible_instead_of_old_failure(self):
        html = self.render_status({
            "jobs": [
                {
                    "id": "failed-daily",
                    "status": "failed",
                    "action": "restart",
                    "execute": True,
                    "target": "all",
                    "targetLabel": "All services",
                    "backup": True,
                    "executedAt": time.time() - (48 * 3600),
                },
                {
                    "id": "stuck-daily",
                    "status": "scheduled",
                    "action": "restart",
                    "execute": True,
                    "target": "all",
                    "targetLabel": "All services",
                    "backup": True,
                    "runAt": time.time() - 3600,
                },
            ]
        })

        self.assertIn("Maintenance overdue", html)
        self.assertIn("overdue by", html)
        self.assertIn("<strong>Scheduled maintenance</strong>", html)
        self.assertIn("<strong>Status</strong> scheduled", html)
        self.assertNotIn("Maintenance failed", html)
        self.assertIn("<strong>Last maintenance attempt</strong>", html)

    def test_stale_maintenance_marks_schedule_stale(self):
        executed_at = time.time() - (48 * 3600)
        html = self.render_status({
            "jobs": [{
                "id": "daily",
                "status": "executed",
                "action": "restart",
                "execute": True,
                "target": "all",
                "targetLabel": "All services",
                "backup": True,
                "executedAt": executed_at,
            }]
        })

        self.assertIn("Since maintenance", html)
        self.assertIn("<strong>Schedule</strong> stale", html)
        self.assertIn("status-dot status-warn", html)

    def test_missing_maintenance_job_falls_back_to_restart_label(self):
        html = self.render_status({"jobs": []})

        self.assertIn("Since restart", html)
        self.assertIn("<strong>Most recent restart</strong>", html)

    def test_render_includes_support_links(self):
        html = self.render_status({"jobs": []})

        self.assertIn("donations%40snape.tech", html)
        self.assertIn("https://ko-fi.com/snapetech", html)
        self.assertIn("Support this server", html)


if __name__ == "__main__":
    unittest.main()
