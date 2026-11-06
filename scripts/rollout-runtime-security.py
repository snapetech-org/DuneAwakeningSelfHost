#!/usr/bin/env python3
"""Operator-only, one-worker runtime canary; no HTTP promotion authority."""
import argparse
import copy
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'admin'), str(ROOT / 'scripts')]
import change_intelligence
import host_service_control as control
import runtime_security


def rollout(runtime, execute=False):
    plan = runtime.preflight()
    if not execute or plan['alreadyCurrent']:
        return {**plan, 'changed': False}
    backup = runtime.backup()
    runtime.gates()
    try:
        runtime.recreate()
        runtime.verify_candidate()
        if plan['running']:
            runtime.prepare_network()
            runtime.wait_healthy()
            runtime.post_hooks()
            runtime.wait_healthy()
        runtime.reapprove()
    except Exception:
        # A new player, unknown count or unexpected container identity blocks
        # rollback as well. Never overwrite live work to manufacture success.
        runtime.rollback()
        raise
    # A failed attestation leaves the qualified, healthy image in place. It
    # must not roll back a worker to hide unrelated configuration drift.
    runtime.attest_image_transition()
    return {**plan, 'changed': True, 'backup': backup, 'running': plan['running']}


class Native:
    def __init__(self, root, env_file, service, state, reason, required_host):
        self.root, self.env_file = root.resolve(), env_file
        if socket.gethostname().split('.')[0] != required_host:
            raise control.Blocked('production-host-required')
        if os.geteuid() != self.root.stat().st_uid:
            raise control.Blocked('configuration-owner-required')
        os.environ['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
        for name in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
            os.environ.pop(name, None)
        self.admission = control.Admission(self.root, env_file, required_host)
        self.policy = self.admission.require_policy()
        self.maps = set(self.policy['services']) - control.STATEFUL - control.CONTROL
        if service not in self.maps or not control.IDENTIFIER.fullmatch(service):
            raise control.Blocked('approved-game-worker-required')
        self.service, self.reason = service, reason
        self.state = state.resolve(strict=True)
        info = self.state.stat()
        if state.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise control.Blocked('private-operator-state-required')
        self.job = secrets.token_hex(16)
        self.document = {'schemaVersion': 'dune-runtime-rollout/v1', 'jobId': self.job,
                         'service': service, 'reason': reason, 'events': []}
        self.key = change_intelligence.read_secret(self.admission.host_path(
            self.admission.values.get('DUNE_CHANGE_INTELLIGENCE_HMAC_SECRET_FILE'),
            'config/secrets/change-intelligence-hmac.secret'))
        self.original = None
        self.new_id = None

    def record(self, phase, **fields):
        self.document['events'].append({'at': time.time(), 'phase': phase, **fields})
        payload = json.dumps(self.document, sort_keys=True, separators=(',', ':')).encode()
        output = {'record': self.document, 'hmacSha256': hmac.new(self.key, payload, hashlib.sha256).hexdigest()}
        target = self.state / (self.job + '.json')
        temporary = target.with_suffix('.tmp')
        with temporary.open('x') as handle:
            os.chmod(temporary, 0o600)
            json.dump(output, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    def inspect(self):
        identifier = self.admission.container(self.service)
        return json.loads(self.admission.run(['docker', 'inspect', identifier]))[0]

    def qualification(self):
        approval = runtime_security.configuration(self.root, self.admission.values)
        if approval is None:
            raise control.Blocked('enabled-qualified-runtime-required')
        def image_id(ref):
            return self.admission.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref]).strip()
        vendor = 'registry.funcom.com/funcom/self-hosting/seabass-server:' + approval['vendorTag']
        runtime_security.verify_loaded_images(approval, image_id(approval['image']), image_id(vendor))
        return approval

    def gates(self):
        self.admission.require_policy()
        self.admission.unchanged_environment()
        self.admission.players()
        approval = self.qualification()
        if approval != self.approval:
            raise control.Blocked('runtime-approval-changed')
        receipt = self.admission.readiness(return_receipt=True)
        expected = {key: approval[key] for key in ('imageId', 'vendorImageId')}
        expected['approvalSha256'] = hashlib.sha256(json.dumps(approval, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if receipt.get('candidate', {}).get('runtimeSecurity') != expected:
            raise control.Blocked('readiness-runtime-identity-mismatch')
        self.admission.players()
        self.record('admitted', readinessReceipt=receipt['id'])

    def preflight(self):
        self.approval = self.qualification()
        self.admission.same_image(self.service)
        self.original = self.inspect()
        if self.original['Image'] not in (self.approval['vendorImageId'], self.approval['imageId']):
            raise control.Blocked('unexpected-existing-game-image')
        self.running = bool(self.original['State']['Running'])
        self.compose = json.loads((self.admission.policy_path.parent / self.policy['composeFile']).read_text())
        spec = (self.compose.get('services') or {}).get(self.service)
        if not isinstance(spec, dict) or not spec:
            raise control.Blocked('approved-game-compose-spec-unavailable')
        if spec['image'] != self.approval['image']:
            raise control.Blocked('approved-compose-runtime-mismatch')
        partitions = [re.fullmatch(r'-PartitionIndex=([0-9]+)', str(value)) for value in spec.get('command', [])]
        partitions = [int(match[1]) for match in partitions if match]
        if len(partitions) != 1:
            raise control.Blocked('unambiguous-map-partition-required')
        self.partition = partitions[0]
        for key in ('DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED', 'DUNE_LOGOFF_TIMER_AUTO_REMAP_ENABLED'):
            if control.enabled(self.admission.values.get(key, 'true')):
                raise control.Blocked('protected-runtime-patch-settings-required')
        self.environment = self.admission.approved_environment(dict(os.environ))
        self.environment.update(DUNE_OPERATION_LOCK_HELD='true', DUNE_RESTART_TARGET=self.service,
                                DUNE_RESTART_PHASE='restart', DUNE_RESTART_CHECK_STEAM_UPDATE='false')
        self.gates()
        self.record('preflight', originalContainer=self.original['Id'], originalImage=self.original['Image'],
                    candidateImage=self.approval['imageId'], running=self.running)
        return {'service': self.service, 'running': self.running, 'alreadyCurrent': self.original['Image'] == self.approval['imageId'], 'jobId': self.job}

    def backup(self):
        point = self.admission.backup(self.environment)
        self.record('backup-verified', backup=point)
        return point

    def compose_recreate(self, image):
        document = copy.deepcopy(self.compose)
        document['services'][self.service]['image'] = image
        path = self.state / (self.job + '-compose.json')
        with path.open('w') as handle:
            os.chmod(path, 0o600)
            json.dump(document, handle)
        command = ['docker', 'compose', '--project-name', self.admission.project, '--project-directory', str(self.root), '-f', str(path)]
        for profile in document['services'][self.service].get('profiles', []):
            command += ['--profile', profile]
        command += ['--env-file', str(self.admission.policy_path.parent / self.policy['environmentFile']),
                    'up', '--no-deps', '--pull', 'never', '--force-recreate']
        command += ['-d'] if self.running else ['--no-start']
        self.admission.run(command + [self.service], timeout=1800, env=self.environment)

    def recreate(self):
        self.admission.same_image(self.service)
        self.admission.players()
        self.admission.unchanged_environment()
        if self.qualification() != self.approval:
            raise control.Blocked('runtime-approval-changed')
        self.record('recreate-requested')
        self.compose_recreate(self.approval['image'])

    def verify_candidate(self):
        current = self.inspect()
        self.new_id = current['Id']
        if current['Image'] != self.approval['imageId'] or bool(current['State']['Running']) != self.running:
            raise control.Blocked('candidate-runtime-state-mismatch')
        self.record('candidate-loaded', containerId=self.new_id, running=self.running)

    def wait_healthy(self):
        database = self.admission.values.get('DUNE_GAME_DB_NAME') or self.admission.values.get('DUNE_ADMIN_DB_NAME')
        role = self.admission.values.get('DUNE_ADMIN_DB_USER') or 'dune'
        if not database or not control.IDENTIFIER.fullmatch(database) or not control.IDENTIFIER.fullmatch(role):
            raise control.Blocked('database-identity-unavailable')
        sql = ('select coalesce(bool_and(fs.alive and asi.server_id is not null and not coalesce(wp.blocked,false)),false) '
               'from dune.world_partition wp left join dune.farm_state fs on fs.server_id=wp.server_id '
               'left join dune.active_server_ids asi on asi.server_id=wp.server_id where wp.partition_id=' + str(self.partition))
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current = self.inspect()
            if current['Id'] != self.new_id:
                raise control.Blocked('canary-container-identity-changed')
            if current['State']['Running']:
                logs = subprocess.check_output(['docker', 'logs', '--tail', '2000', current['Id']], stderr=subprocess.STDOUT, text=True, timeout=30)
                alive = self.admission.run(['docker', 'exec', '--user', 'postgres', self.admission.container('postgres'),
                    'psql', '-XAtq', '-v', 'ON_ERROR_STOP=1', '-U', role, '-d', database, '-c', sql], timeout=30).strip()
                if 'Server farm is READY' in logs and alive == 't':
                    self.record('native-game-health-passed')
                    return
            time.sleep(5)
        raise control.Blocked('native-game-health-timeout')

    def prepare_network(self):
        # A replacement endpoint needs bridge/reflection/neighbor setup before
        # peers can finish the native farm-ready handshake.
        self.admission.run([str(self.root / 'scripts/restart-post-start-health.sh')],
                           timeout=1800, env=self.environment)
        self.record('native-network-post-start-passed')

    def post_hooks(self):
        self.admission.post_start_hooks(self.environment)
        self.record('native-post-hooks-passed')

    def reapprove(self):
        control.approve_configuration(self.root, self.env_file, self.admission.policy_path.parent, self.maps, self.admission.expected_hostname)
        self.admission.same_image(self.service)
        self.record('recovery-approval-refreshed', running=self.running)

    def attest_image_transition(self):
        self.admission.same_image(self.service)
        configured = dict(self.admission.values)
        if configured.get('DUNE_ADMIN_TOKEN_FILE'):
            configured['DUNE_ADMIN_TOKEN_FILE'] = str(self.admission.host_path(configured['DUNE_ADMIN_TOKEN_FILE'], ''))
        token = control.read_secret_env('DUNE_ADMIN_TOKEN', environment=configured)
        port = configured.get('DUNE_ADMIN_HOST_PORT') or '18080'
        if not token or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise control.Blocked('native-admin-configuration-unavailable')
        transition = {'service': self.service, 'fromImageId': self.original['Image'],
                      'fromImageRef': self.original['Config']['Image'],
                      'toImageId': self.approval['imageId'], 'toImageRef': self.approval['image']}
        payload = {'action': 'seal', 'confirm': 'SEAL DESIRED STATE',
                   'reason': 'Verified native runtime rollout ' + self.job + ': ' + self.reason,
                   'runtimeImageTransition': transition}
        connection = http.client.HTTPConnection('127.0.0.1', int(port), timeout=1800)
        try:
            connection.request('POST', '/api/ops/desired-state', body=json.dumps(payload),
                               headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 2 * 1024 * 1024:
                raise control.Blocked('runtime-image-attestation-rejected')
            result = json.loads(raw)
            if result.get('ok') is not True or not result.get('baselineId'):
                raise control.Blocked('runtime-image-attestation-unverified')
        finally:
            connection.close()
        self.record('runtime-image-transition-attested', baselineId=result['baselineId'])

    def rollback(self):
        self.admission.players()
        ids = self.admission.run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + self.admission.project, '--filter', 'label=com.docker.compose.service=' + self.service]).split()
        if len(ids) > 1:
            raise control.Blocked('ambiguous-container-blocks-image-rollback')
        current = self.inspect() if ids else None
        if current and current['Id'] == self.original['Id'] and current['Image'] == self.original['Image']:
            self.record('failed-before-replacement')
            return
        if current and (current['Image'] != self.approval['imageId'] or (self.new_id and current['Id'] != self.new_id)):
            raise control.Blocked('unexpected-container-blocks-image-rollback')
        old_ref = self.original['Config']['Image']
        old_id = self.admission.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', old_ref]).strip()
        if old_id != self.original['Image']:
            raise control.Blocked('rollback-image-identity-changed')
        self.record('image-only-rollback-requested')
        self.compose_recreate(old_ref)
        restored = self.inspect()
        if restored['Image'] != old_id or bool(restored['State']['Running']) != self.running:
            raise control.Blocked('image-rollback-did-not-converge')
        self.new_id = restored['Id']
        if self.running:
            self.prepare_network()
            self.wait_healthy()
            self.post_hooks()
        self.reapprove()
        self.record('rolled-back')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True)
    parser.add_argument('--workspace', type=pathlib.Path, default=ROOT)
    parser.add_argument('--env-file', type=pathlib.Path)
    parser.add_argument('--state-dir', type=pathlib.Path, default=pathlib.Path('/var/lib/dune-runtime-rollout'))
    parser.add_argument('--required-host', default='kspls0')
    parser.add_argument('--reason', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not 10 <= len(args.reason) <= 1000:
        parser.error('--reason must contain 10..1000 characters')
    root = args.workspace.resolve()
    env_file = args.env_file or root / '.env'
    if not env_file.is_absolute():
        env_file = root / env_file
    values = control.read_environment(env_file)
    lock = pathlib.Path(values.get('DUNE_OPERATION_LOCK_FILE') or root / 'backups/admin-panel/operation.lock')
    if not lock.is_absolute():
        lock = root / lock
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime = Native(root, env_file, args.service, args.state_dir, args.reason, args.required_host)
        try:
            result = rollout(runtime, args.execute)
            runtime.record('operator-result', **result)
            print(json.dumps(result, sort_keys=True))
        except Exception as exc:
            runtime.record('blocked-or-failed', errorType=type(exc).__name__)
            raise SystemExit('runtime rollout blocked or failed: ' + type(exc).__name__)


if __name__ == '__main__':
    main()
