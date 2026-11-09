#!/usr/bin/env python3
"""Exercise alert transitions without contacting systemd, Docker, or mail."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class HealthMonitorTests(unittest.TestCase):
    def test_retry_and_skipped_checks_do_not_send_recovery(self):
        with tempfile.TemporaryDirectory(prefix="dune-health-test-") as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            (root / "bin").mkdir()
            (root / "state").mkdir()
            (root / ".env").touch()
            shutil.copy(Path(__file__).with_name("dune-health-monitor.sh"), scripts)

            def executable(path, body):
                path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
                path.chmod(0o755)

            executable(root / "bin/systemctl", '''
service="${2:-}"
case "$1" in
  show)
    case "$*" in
      *LoadState*) echo loaded ;;
      *ActiveState*)
        if [[ "$service" == dune-hotfix-auto-update.service ]]; then
          cat "$TEST_ROOT/updater"
        elif [[ "$service" == dune-public-ip-monitor.service ]]; then
          cat "$TEST_ROOT/ip"
        else echo inactive; fi ;;
      *Result*) echo exit-code ;;
      *ExecMainStatus*) echo 1 ;;
    esac ;;
  is-active) exit 0 ;;
  is-failed)
    [[ "${@: -1}" == dune-hotfix-auto-update.service ]] &&
      [[ "$(cat "$TEST_ROOT/updater")" == failed ]] ;;
  list-timers) echo 'Tue next-run' ;;
esac
''')
            executable(scripts / "check-steam-update.sh", '''
echo steam >> "$TEST_ROOT/checks"
exit "$(cat "$TEST_ROOT/steam")"
''')
            executable(scripts / "status.sh", '''
echo farm >> "$TEST_ROOT/checks"
if [[ "$(cat "$TEST_ROOT/farm")" == bad ]]; then
  echo 'WARN: required always-on partition readiness is incomplete.'
  echo 'WARN: FLS publication health is degraded.'
else echo 'OK: required always-on partitions are ready and connected.'; fi
''')
            (root / "mail.sh").write_text('''snape_mail_send() {
  printf '%s\\n' "$1" >> "$TEST_ROOT/mail"
}
''')
            env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}",
                       TEST_ROOT=str(root), STATE_DIRECTORY=str(root / "state"),
                       DUNE_MAIL_HELPER=str(root / "mail.sh"))

            def run(updater, ip="inactive", steam="2", farm="bad"):
                for key, value in dict(updater=updater, ip=ip, steam=steam, farm=farm).items():
                    (root / key).write_text(value)
                subprocess.run(["bash", str(scripts / "dune-health-monitor.sh")],
                               env=env, check=True)

            def mail():
                return (root / "mail").read_text().splitlines()

            run("failed")
            original = (root / "state/findings.txt").read_text()
            self.assertEqual(len(mail()), 1)
            for state in ("activating", "active", "reloading", "failed"):
                run(state)
                self.assertEqual((root / "state/findings.txt").read_text(), original)
                self.assertEqual(len(mail()), 1)

            # A successful retry clears its service failure, but the running IP
            # monitor cannot clear unchecked farm failures.
            run("inactive", ip="activating")
            self.assertEqual(len(mail()), 2)
            self.assertIn("publication is degraded", (root / "state/findings.txt").read_text())
            run("inactive", ip="active")
            self.assertEqual(len(mail()), 2)
            run("inactive", steam="0", farm="good")
            self.assertEqual(mail()[-1], "[Dune] Server health recovered")
            self.assertFalse((root / "state/findings.txt").exists())
            run("inactive", steam="0", farm="good")
            self.assertEqual(len(mail()), 3)

            # An overlapping invocation must not inspect or notify.
            with (root / "state/monitor.lock").open("w") as lock:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = (root / "checks").read_text()
                run("failed")
                self.assertEqual((root / "checks").read_text(), before)
                self.assertEqual(len(mail()), 3)


if __name__ == "__main__":
    unittest.main()
