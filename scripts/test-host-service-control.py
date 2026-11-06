#!/usr/bin/env python3
"""Fail-closed admission, protocol, identity and side-effect ordering tests."""
import hashlib
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

    def test_native_secret_alias_maps_only_inside_the_approved_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = root / '.env'
            env.write_text('')
            provider = root / 'private-provider'
            provider.mkdir()
            key = provider / 'key'
            key.write_text('fixture')
            outside = root / 'outside'
            outside.write_text('fixture')
            (provider / 'escape').symlink_to(outside)
            admission = CONTROL.Admission(root, env, 'approved')
            with mock.patch.object(admission, 'require_policy', return_value={'secretDirectory': str(provider)}):
                self.assertEqual(key, admission.host_path('/workspace-secret-files/key', ''))
                for value in ('/workspace-secret-files/../outside', '/workspace-secret-files/escape'):
                    with self.assertRaisesRegex(CONTROL.Blocked, 'secret-path-escape'):
                        admission.host_path(value, '')
            self.assertEqual(root / 'backups/evidence', admission.host_path('/workspace/backups/evidence', ''))

    def test_disabled_runtime_patch_is_not_probed_after_native_network_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = root / '.env'
            env.write_text('DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED=false\n')
            (root / 'scripts').mkdir()
            (root / 'scripts/patch-logoff-timers-runtime.sh').write_text('fixture')
            admission = CONTROL.Admission(root, env, 'approved')
            with mock.patch.object(admission, 'run') as run:
                admission.post_start_hooks({})
                self.assertEqual(1, run.call_count)
                self.assertTrue(run.call_args.args[0][0].endswith('/restart-post-start-health.sh'))
            admission.values['DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED'] = 'true'
            with mock.patch.object(admission, 'run') as run:
                admission.post_start_hooks({})
                self.assertEqual(2, run.call_count)
                self.assertIn('--dry-run', run.call_args.args[0])

    def test_approval_renders_disabled_profiles_without_expanding_authority(self):
        services = sorted(CONTROL.STATEFUL | CONTROL.CONTROL | {"deep-desert-pvp"})
        identifiers = {f"{i+1:064x}": name for i, name in enumerate(services)}
        rendered = {"services": {name: {"image": "fixture"} for name in services}}
        rendered["services"]["deep-desert-pvp"]["profiles"] = ["disabled-deep-desert-pvp"]
        rendered["services"]["outside-authority"] = {"image": "fixture"}
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[0].endswith("compose-files.sh"):
                output = "compose.yaml"
            elif "config" in command:
                output = json.dumps(rendered)
            elif "ps" in command:
                output = "\n".join(identifiers)
            elif "inspect" in command:
                key = command[-1]
                output = f"{key}|sha256:{key}|fixture|{identifiers[key]}"
            else:
                raise AssertionError(command)
            return mock.Mock(returncode=0, stdout=output)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory); env = root / ".env"
            env.write_text("COMPOSE_PROJECT_NAME=fixture\n")
            state = root / "state"
            with mock.patch.object(CONTROL.subprocess, "run", side_effect=run):
                CONTROL.approve_configuration(root, env, state, {"deep-desert-pvp"}, "approved")
            policy = json.loads((state / "approved-control.json").read_text())
            self.assertEqual(set(services), set(policy["services"]))
            self.assertTrue(any("--profile" in c and "*" in c and "config" in c for c in commands))
            self.assertIn("deep-desert-pvp", json.loads((state / "approved-compose.json").read_text())["services"])
            rendered["services"].pop("deep-desert-pvp")
            with mock.patch.object(CONTROL.subprocess, "run", side_effect=run), self.assertRaisesRegex(CONTROL.Blocked, "approved-service-spec-missing"):
                CONTROL.approve_configuration(root, env, state, {"deep-desert-pvp"}, "approved")

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

    def policy_fixture(self, root):
        env = root / '.env'
        env.write_text('DUNE_ADMIN_SERVICE_CONTROL_ENABLED=true\n')
        state = root / 'host-private-state'
        state.mkdir(mode=0o700)
        approved = state / 'approved.env'
        approved.write_bytes(env.read_bytes())
        approved.chmod(0o600)
        compose = state / 'approved-compose.json'
        compose.write_text('{}')
        compose.chmod(0o600)
        policy = {'schemaVersion': CONTROL.PROTOCOL, 'host': 'approved', 'project': 'dune_server',
                  'environmentPath': str(env.resolve()), 'environmentSha256': hashlib.sha256(env.read_bytes()).hexdigest(),
                  'environmentFile': approved.name, 'composeFile': compose.name,
                  'composeSha256': hashlib.sha256(compose.read_bytes()).hexdigest(),
                  'secretDirectory': str((root / 'config/secrets').resolve()),
                  'services': {'arrakeen': {'containerId': 'a' * 64, 'imageId': 'sha256:' + 'b' * 64}}}
        path = state / 'approved-control.json'
        path.write_text(json.dumps(policy))
        path.chmod(0o600)
        return CONTROL.Admission(root, env, 'approved', path)

    def test_policy_rejects_unapproved_environment_and_modified_private_files(self):
        for target in ('source', 'environment', 'compose', 'permissions', 'symlink'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                admission = self.policy_fixture(pathlib.Path(directory))
                admission.require_policy()
                if target == 'source':
                    admission.env_file.write_text('DUNE_ADMIN_SERVICE_CONTROL_ENABLED=true\nCOMPOSE_FILES=unapproved.yaml\n')
                elif target == 'permissions':
                    admission.policy_path.chmod(0o644)
                elif target == 'symlink':
                    saved = admission.policy_path.with_suffix('.saved')
                    admission.policy_path.rename(saved)
                    admission.policy_path.symlink_to(saved)
                else:
                    name = 'approved.env' if target == 'environment' else 'approved-compose.json'
                    (admission.policy_path.parent / name).write_text('changed')
                with self.assertRaises(CONTROL.Blocked):
                    admission.require_policy()

    def test_existing_recovery_uses_only_pinned_ids_and_keeps_post_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            admission = self.policy_fixture(pathlib.Path(directory))
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if command[:2] == ['docker', 'ps']:
                    return 'a' * 64
                if command[:2] == ['docker', 'inspect']:
                    return 'sha256:' + 'b' * 64
                return ''
            with mock.patch.object(admission, 'run', side_effect=run):
                environment = admission.approved_environment({})
                self.assertEqual(environment['COMPOSE_FILES'], str(admission.policy_path.parent / 'approved-compose.json'))
                admission.recover_existing('arrakeen', 'restart', environment)
            self.assertIn(['docker', 'restart', '--time', '30', 'a' * 64], calls)
            self.assertIn([str(admission.root / 'scripts/restart-post-start-health.sh')], calls)
            self.assertFalse(any('compose' in command or 'up' in command for command in calls))
            with mock.patch.object(admission, 'container', return_value='c' * 64), mock.patch.object(admission, 'run') as mutate:
                with self.assertRaisesRegex(CONTROL.Blocked, 'identity-requires-approval'):
                    admission.recover_existing('arrakeen', 'start', environment)
                mutate.assert_not_called()
            with mock.patch.object(admission, 'container', return_value='a' * 64), mock.patch.object(admission, 'run', return_value='sha256:' + 'd' * 64) as mutate:
                with self.assertRaisesRegex(CONTROL.Blocked, 'image-requires-approval'):
                    admission.recover_existing('arrakeen', 'start', environment)
                self.assertEqual(len(mutate.call_args_list), 1)

    def test_native_approval_pins_existing_services_and_writes_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = root / '.env'
            env.write_text('COMPOSE_PROJECT_NAME=fixture\n')
            services = sorted(CONTROL.STATEFUL | CONTROL.CONTROL | {'arrakeen'})
            identifiers = {str(index + 1).zfill(64): service for index, service in enumerate(services)}
            def run(command, **kwargs):
                if command[0].endswith('compose-files.sh'):
                    output = 'compose.yaml\n'
                elif command[:2] == ['docker', 'compose']:
                    output = json.dumps({'name': 'fixture', 'services': {name: {} for name in services}})
                elif command[:2] == ['docker', 'ps']:
                    output = '\n'.join(identifiers)
                else:
                    identifier = command[-1]
                    output = identifier + '|sha256:' + 'b' * 64 + '|fixture|' + identifiers[identifier]
                return mock.Mock(returncode=0, stdout=output)
            with mock.patch.object(CONTROL.subprocess, 'run', side_effect=run):
                result = CONTROL.approve_configuration(root, env, root / 'host-private-state', {'arrakeen'}, 'approved')
            admission = CONTROL.Admission(root, env, 'approved', root / 'host-private-state/approved-control.json')
            policy = admission.require_policy()
            self.assertEqual(result['approvedExistingServices'], len(services))
            self.assertEqual(policy['services']['arrakeen']['imageId'], 'sha256:' + 'b' * 64)
            self.assertEqual(admission.policy_path.stat().st_mode & 0o777, 0o600)

    def test_legacy_route_cannot_fall_back_to_compose_or_skip_approval(self):
        request = {'target': 'arrakeen', 'services': ['arrakeen'], 'phase': 'start',
                   'action': 'restart', 'jobId': 'autoscaler-fixture', 'dryRun': False, 'fastDynamicStart': True}
        with mock.patch.object(API.host_service_control, 'Admission') as factory, mock.patch.object(API.subprocess, 'run') as run:
            factory.return_value.require_policy.side_effect = CONTROL.Blocked('missing-policy')
            result = API.execute_restart(request)
            self.assertFalse(result['ok'])
            factory.return_value.recover_existing.assert_not_called()
            run.assert_not_called()
        with mock.patch.object(API.host_service_control, 'Admission') as factory, mock.patch.object(API.socket, 'gethostname', return_value='approved'), mock.patch.object(API.subprocess, 'run') as run:
            admission = factory.return_value
            admission.expected_hostname = 'approved'
            admission.approved_environment.return_value = {'ENV_FILE': '/host/private/approved.env'}
            result = API.execute_restart(request)
            self.assertTrue(result['ok'])
            admission.same_image.assert_called_once_with('arrakeen')
            admission.recover_existing.assert_called_once_with('arrakeen', 'start', {'ENV_FILE': '/host/private/approved.env'}, dry_run=False, post_hooks=False)
            admission.post_start_hooks.assert_called_once()
            admission.players.assert_not_called()  # Cold starts must not stop other occupied maps.
            run.assert_not_called()

    def test_activation_requires_loaded_hardened_protocol_and_keeps_dotenv_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            admission = self.policy_fixture(root)
            target = root / 'private.env'
            admission.env_file.rename(target)
            target.chmod(0o600)
            admission.env_file.symlink_to(target)
            policy = json.loads(admission.policy_path.read_text())
            policy['environmentPath'] = str(target)
            admission.policy_path.write_text(json.dumps(policy))
            original = target.read_bytes()
            for health in ({'qualified': False, 'busy': False}, {'qualified': True, 'busy': True}):
                with mock.patch.object(CONTROL, 'health', return_value=health), mock.patch.object(CONTROL, 'approve_configuration') as approve:
                    with self.assertRaisesRegex(CONTROL.Blocked, 'qualified-idle'):
                        CONTROL.enable_approved_recovery(root, admission.env_file, admission.policy_path.parent, {'arrakeen'}, 'approved')
                    approve.assert_not_called()
                    self.assertEqual(target.read_bytes(), original)
            with mock.patch.object(CONTROL, 'health', return_value={'qualified': True, 'busy': False}), mock.patch.object(CONTROL, 'approve_configuration', return_value={'ok': True}) as approve:
                self.assertEqual(CONTROL.enable_approved_recovery(root, admission.env_file, admission.policy_path.parent, {'arrakeen'}, 'approved'), {'ok': True})
                approve.assert_called_once()
            self.assertTrue(admission.env_file.is_symlink())
            values = CONTROL.read_environment(target)
            self.assertTrue(CONTROL.enabled(values['DUNE_ADMIN_SERVICE_CONTROL_ENABLED']))
            self.assertTrue(CONTROL.enabled(values['DUNE_ADMIN_STATEFUL_SERVICE_CONTROL_ENABLED']))
            self.assertNotIn('DUNE_ADMIN_UPDATE_MUTATIONS_ENABLED', values)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(CONTROL.Blocked):
                admission.require_policy()  # Until host reapproval, the changed environment remains blocked.

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
                admission.approved_environment.side_effect = lambda env: env
                def execute(service, action, environment):
                    events.append('mutation')
                    self.assertEqual(environment['DUNE_OPERATION_LOCK_HELD'], 'true')
                    self.assertEqual(environment['DUNE_RESTART_ALLOW_STATEFUL'], 'true')
                    return {'ok': True, 'exitCode': 0, 'postHooks': True}
                admission.recover_existing.side_effect = execute
                with mock.patch.object(API, 'ROOT', root), mock.patch.object(API, 'CONTROL_STATE', root / 'jobs'), mock.patch.object(API.host_service_control, 'Admission', return_value=admission):
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
