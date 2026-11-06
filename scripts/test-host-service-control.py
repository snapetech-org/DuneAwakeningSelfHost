#!/usr/bin/env python3
"""Fail-closed admission, protocol, identity and side-effect ordering tests."""
import importlib.util
import json
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'admin')]
import host_service_control as CONTROL
from secret_env import read_secret_env

spec = importlib.util.spec_from_file_location('restart_api_under_test', ROOT / 'scripts/dune_restart_api.py')
API = importlib.util.module_from_spec(spec)
spec.loader.exec_module(API)


class GuardTests(unittest.TestCase):
    def test_secret_provider_accepts_an_explicit_environment_without_global_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = pathlib.Path(directory) / 'secret'
            secret.write_text('fixture-value\n')
            self.assertEqual(read_secret_env('TEST_TOKEN', environment={'TEST_TOKEN_FILE': str(secret)}), 'fixture-value')
            self.assertEqual(read_secret_env('TEST_TOKEN', environment={'TEST_TOKEN': 'inline-fixture'}), 'inline-fixture')
            with self.assertRaises(ValueError):
                read_secret_env('TEST_TOKEN', environment={'TEST_TOKEN_FILE': str(secret) + '-missing', 'TEST_TOKEN': 'no-fallback'})

    def test_exact_allowlist_rejects_untrusted_fields_and_values(self):
        valid = {'service': 'postgres', 'action': 'restart', 'jobId': 'a' * 32}
        self.assertEqual(CONTROL.validate_request(valid, {'arrakeen'}), valid)
        for patch in ({'service': 'admin-panel'}, {'service': 'another-project'}, {'action': ['restart']},
                      {'action': 'update'}, {'jobId': '../job'}, {'command': 'sh'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                CONTROL.validate_request(valid | patch, {'arrakeen'})

    def test_unknown_nonzero_negative_and_boolean_player_counts_block(self):
        empty = dict.fromkeys(CONTROL.PLAYER_FIELDS, 0)
        CONTROL.assert_empty_farm(empty)
        for field in CONTROL.PLAYER_FIELDS:
            for value in (None, True, '0', 1, -1):
                with self.subTest(field=field, value=value), self.assertRaises(CONTROL.Blocked):
                    CONTROL.assert_empty_farm(empty | {field: value})
        with self.assertRaises(CONTROL.Blocked):
            CONTROL.assert_empty_farm({})

    def test_stateful_and_host_identity_are_independent_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = root / '.env'
            env.write_text('DUNE_ADMIN_SERVICE_CONTROL_ENABLED=true\n')
            admission = CONTROL.Admission(root, env, 'approved')
            with mock.patch.object(socket, 'gethostname', return_value='approved'):
                admission.require_gate('arrakeen')
                with self.assertRaisesRegex(CONTROL.Blocked, 'stateful-control-disabled'):
                    admission.require_gate('postgres')
            with mock.patch.object(socket, 'gethostname', return_value='other'):
                with self.assertRaisesRegex(CONTROL.Blocked, 'host-identity'):
                    admission.require_gate('arrakeen')
            env.write_text('DUNE_ADMIN_SERVICE_CONTROL_ENABLED=false\n')
            with self.assertRaisesRegex(CONTROL.Blocked, 'configuration-changed'):
                admission.unchanged_environment()

    def exchange(self, request):
        server, client = socket.socketpair()
        with mock.patch.object(API, 'peer_allowed', return_value=True):
            worker = threading.Thread(target=API.handle_connection, args=(server,))
            worker.start()
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := client.recv(65536):
                chunks.append(chunk)
            worker.join(5)
            self.assertFalse(worker.is_alive())
        client.close()
        header, body = b''.join(chunks).split(b'\r\n\r\n', 1)
        return int(header.split()[1]), json.loads(body)

    def test_health_is_read_only_and_no_body_is_required(self):
        with mock.patch.object(API, 'control_health', return_value={'ok': True, 'protocol': 1}), mock.patch.object(API, 'execute_restart') as mutate:
            code, body = self.exchange(b'GET /v1/health HTTP/1.1\r\nHost: local\r\n\r\n')
        self.assertEqual(code, 200)
        self.assertEqual(body['protocol'], 1)
        mutate.assert_not_called()

    def test_framing_and_unknown_routes_fail_before_execution(self):
        with mock.patch.object(API, 'execute_restart') as mutate, mock.patch.object(API, 'submit_control') as submit:
            for request in (
                b'POST /v1/service-control HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}',
                b'POST /v1/service-control HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
                b'POST /v1/service-control HTTP/1.1\r\nContent-Length: 20000\r\n\r\n',
                b'GET /v1/health HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}',
                b'POST /v1/command HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}',
                b'POST /v1/service-control HTTP/1.1\r\nContent-Length: 13\r\n\r\n{"x":1,"x":2}',
            ):
                code, _ = self.exchange(request)
                self.assertIn(code, {400, 404, 413})
        mutate.assert_not_called()
        submit.assert_not_called()

    def test_blocked_admission_never_runs_backup_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(API, 'CONTROL_STATE', pathlib.Path(directory)), mock.patch.object(API.host_service_control, 'Admission') as admission, mock.patch.object(API.subprocess, 'run') as mutate:
            admission.return_value.require_gate.side_effect = CONTROL.Blocked('service-control-disabled')
            job = {'jobId': 'a' * 32, 'service': 'postgres', 'action': 'restart'}
            API.execute_control(job)
            self.assertEqual(job['state'], 'blocked')
            admission.return_value.backup.assert_not_called()
            mutate.assert_not_called()

    def test_durable_identity_prevents_retry_or_changed_operation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(API, 'CONTROL_STATE', pathlib.Path(directory)), mock.patch.object(API, 'configured_map_services', return_value={'arrakeen'}), mock.patch.object(API.CONTROL_POOL, 'submit') as submit:
            job = {'jobId': 'b' * 32, 'service': 'postgres', 'action': 'restart', 'state': 'executing'}
            API.save_control_job(job)
            self.assertEqual(API.submit_control({key: job[key] for key in ('jobId', 'service', 'action')}), job)
            with self.assertRaises(API.RequestError):
                API.submit_control({'jobId': 'b' * 32, 'service': 'postgres', 'action': 'stop'})
            with self.assertRaises(API.RequestError):
                API.submit_control({'jobId': 'c' * 32, 'service': 'arrakeen', 'action': 'restart'})
            submit.assert_not_called()

    def test_native_mutation_requires_both_admissions_and_a_verified_backup(self):
        for failure in (None, 'players', 'readiness', 'same_image', 'backup', 'unchanged_environment'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                events = []
                admission = mock.Mock()
                for name, value in (('require_gate', None), ('players', {}), ('readiness', 'receipt'),
                                    ('same_image', 'image'), ('backup', 'verified-backup'), ('unchanged_environment', None)):
                    def call(*args, name=name, value=value, **kwargs):
                        events.append(name)
                        if name == failure:
                            raise CONTROL.Blocked('test-admission-blocked')
                        return value
                    getattr(admission, name).side_effect = call
                def execute(*args, **kwargs):
                    events.append('mutation')
                    self.assertEqual(kwargs['env']['DUNE_OPERATION_LOCK_HELD'], 'true')
                    self.assertEqual(kwargs['env']['DUNE_RESTART_ALLOW_STATEFUL'], 'true')
                    return mock.Mock(returncode=0, stdout='', stderr='')
                with mock.patch.object(API, 'ROOT', root), mock.patch.object(API, 'CONTROL_STATE', root / 'jobs'), mock.patch.object(API.host_service_control, 'Admission', return_value=admission), mock.patch.object(API.subprocess, 'run', side_effect=execute):
                    job = {'jobId': 'd' * 32, 'service': 'postgres', 'action': 'restart'}
                    API.execute_control(job)
                if failure:
                    self.assertEqual(job['state'], 'blocked')
                    self.assertNotIn('mutation', events)
                else:
                    self.assertTrue(job['ok'])
                    self.assertEqual(events, ['require_gate', 'players', 'readiness', 'same_image', 'backup',
                                              'unchanged_environment', 'players', 'readiness', 'same_image', 'mutation'])

    def test_same_image_change_during_backup_blocks_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.object(API, 'ROOT', root), mock.patch.object(API, 'CONTROL_STATE', root / 'jobs'), mock.patch.object(API.host_service_control, 'Admission') as admission, mock.patch.object(API.subprocess, 'run') as mutate:
                admission.return_value.readiness.return_value = 'receipt'
                admission.return_value.same_image.side_effect = ['image-one', 'image-two']
                admission.return_value.backup.return_value = 'backup'
                job = {'jobId': 'e' * 32, 'service': 'postgres', 'action': 'restart'}
                API.execute_control(job)
                self.assertEqual(job['state'], 'blocked')
                self.assertEqual(job['error'], 'admission-changed-during-backup')
                mutate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
