#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'admin'), str(ROOT / 'scripts')]
spec = importlib.util.spec_from_file_location('rollout_cli', ROOT / 'scripts/rollout-runtime-security.py')
CLI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CLI)


class Fake:
    def __init__(self, running=True, current=False, fail=None):
        self.running, self.current, self.fail = running, current, fail
        self.calls = []

    def call(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise CLI.control.Blocked(name)

    def preflight(self):
        self.call('preflight')
        return {'running': self.running, 'alreadyCurrent': self.current, 'service': 'worker'}

    def backup(self):
        self.call('backup')
        return 'verified-point'

    def gates(self): self.call('gates')
    def recreate(self): self.call('recreate')
    def verify_candidate(self): self.call('verify')
    def prepare_network(self): self.call('network')
    def wait_healthy(self): self.call('health')
    def post_hooks(self): self.call('hooks')
    def reapprove(self): self.call('approve')
    def rollback(self): self.call('rollback')
    def attest_image_transition(self): self.call('attest')


class RolloutTests(unittest.TestCase):
    def test_missing_profile_spec_blocks_before_backup_or_lifecycle(self):
        runtime = object.__new__(CLI.Native)
        runtime.admission = mock.Mock()
        runtime.service = "deep-desert-pvp"
        runtime.qualification = mock.Mock(return_value={"vendorImageId": "sha256:" + "a"*64, "imageId": "sha256:" + "b"*64})
        runtime.inspect = mock.Mock(return_value={"Image": "sha256:" + "a"*64, "State": {"Running": True}})
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "compose.json").write_text('{"services":{}}')
            runtime.policy = {"composeFile": "compose.json"}
            runtime.admission.policy_path = root / "policy.json"
            with self.assertRaisesRegex(CLI.control.Blocked, "approved-game-compose-spec-unavailable"):
                runtime.preflight()
        runtime.admission.backup.assert_not_called()
        runtime.admission.readiness.assert_not_called()

    def test_readonly_and_already_current_never_recreate(self):
        for runtime, execute in ((Fake(), False), (Fake(current=True), True)):
            self.assertFalse(CLI.rollout(runtime, execute)['changed'])
            self.assertEqual(runtime.calls, ['preflight'])

    def test_live_worker_checks_health_before_and_after_native_hooks(self):
        runtime = Fake()
        self.assertTrue(CLI.rollout(runtime, True)['changed'])
        self.assertEqual(runtime.calls, ['preflight', 'backup', 'gates', 'recreate', 'verify', 'network', 'health', 'hooks', 'health', 'approve', 'attest'])

    def test_stopped_worker_is_replaced_without_start_or_post_start_hooks(self):
        runtime = Fake(running=False)
        self.assertFalse(CLI.rollout(runtime, True)['running'])
        self.assertEqual(runtime.calls, ['preflight', 'backup', 'gates', 'recreate', 'verify', 'approve', 'attest'])

    def test_every_pre_mutation_failure_preserves_worker(self):
        for failure in ('preflight', 'backup', 'gates'):
            runtime = Fake(fail=failure)
            with self.subTest(failure=failure), self.assertRaises(CLI.control.Blocked):
                CLI.rollout(runtime, True)
            self.assertNotIn('recreate', runtime.calls)
            self.assertNotIn('rollback', runtime.calls)

    def test_replacement_health_hook_or_approval_failure_enters_guarded_rollback(self):
        for failure in ('recreate', 'verify', 'network', 'health', 'hooks', 'approve'):
            runtime = Fake(fail=failure)
            with self.subTest(failure=failure), self.assertRaises(CLI.control.Blocked):
                CLI.rollout(runtime, True)
            self.assertEqual(runtime.calls[-1], 'rollback')

    def test_attestation_failure_preserves_healthy_candidate_and_drift(self):
        runtime = Fake(fail='attest')
        with self.assertRaises(CLI.control.Blocked):
            CLI.rollout(runtime, True)
        self.assertEqual(runtime.calls[-1], 'attest')
        self.assertNotIn('rollback', runtime.calls)

    def test_runtime_bound_receipt_is_required_before_lifecycle(self):
        runtime = object.__new__(CLI.Native)
        runtime.admission = mock.Mock()
        runtime.approval = {'imageId': 'sha256:'+'a'*64, 'vendorImageId': 'sha256:'+'b'*64}
        runtime.qualification = mock.Mock(return_value=runtime.approval)
        runtime.record = mock.Mock()
        runtime.admission.readiness.return_value = {'id': 'signed', 'candidate': {}}
        with self.assertRaisesRegex(CLI.control.Blocked, 'readiness-runtime-identity'):
            runtime.gates()
        expected = dict(runtime.approval)
        expected['approvalSha256'] = CLI.hashlib.sha256(json.dumps(runtime.approval, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        runtime.admission.readiness.return_value['candidate']['runtimeSecurity'] = expected
        runtime.gates()
        runtime.admission.readiness.assert_called_with(return_receipt=True)

    def test_changed_approval_between_admission_and_recreation_blocks(self):
        runtime = object.__new__(CLI.Native)
        runtime.admission = mock.Mock()
        runtime.service = 'worker'
        runtime.approval = {'imageId': 'approved'}
        runtime.qualification = mock.Mock(return_value={'imageId': 'changed'})
        runtime.compose_recreate = mock.Mock()
        with self.assertRaisesRegex(CLI.control.Blocked, 'runtime-approval-changed'):
            runtime.recreate()
        runtime.compose_recreate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
