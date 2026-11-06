"""Admission and client for exact-service recovery through the host socket."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import pathlib
import pwd
import re
import socket
import subprocess
import stat
import tempfile

import change_intelligence
import update_readiness
from secret_env import read_secret_env


PROTOCOL = 2
STATEFUL = frozenset({"postgres", "admin-rmq", "game-rmq"})
CONTROL = frozenset({"director", "gateway", "text-router", "rmq-auth-shim"})
JOB_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
GUARDS = ("signed-readiness", "current-and-recent-players", "verified-backup", "same-image", "native-post-hooks", "serialized", "host-approved-configuration", "existing-containers")
PLAYER_FIELDS = ("active_farm_connected_players", "raw_farm_connected_players", "online_controller_ids", "online_or_recently_disconnected")
PLAYER_SQL = """begin read only; set local statement_timeout='15s';
select row_to_json(counts) from (select
coalesce((select sum(fs.connected_players) from dune.world_partition wp
join dune.farm_state fs on fs.server_id=wp.server_id
join dune.active_server_ids asi on asi.server_id=wp.server_id where fs.alive),0) as active_farm_connected_players,
coalesce((select sum(connected_players) from dune.farm_state),0) as raw_farm_connected_players,
(select count(*) from dune.get_online_player_controller_ids_on_farm()) as online_controller_ids,
(select count(*) from dune.get_all_online_or_recently_disconnected_player_online_state()) as online_or_recently_disconnected) counts;
rollback;"""


class Blocked(RuntimeError):
    """A named admission failure; never includes credential or command output."""


def validate_request(payload, maps):
    if not isinstance(payload, dict) or set(payload) != {"service", "action", "jobId"}:
        raise ValueError("service-control request fields are invalid")
    service = payload["service"]
    if not isinstance(service, str) or service not in set(maps) | STATEFUL | CONTROL:
        raise ValueError("service-control target is outside the recovery allowlist")
    if not isinstance(payload["action"], str) or payload["action"] not in {"start", "stop", "restart"}:
        raise ValueError("service-control action is invalid")
    if not isinstance(payload["jobId"], str) or not JOB_PATTERN.fullmatch(payload["jobId"]):
        raise ValueError("service-control job identifier is invalid")
    return dict(payload)


def read_environment(path):
    raw = pathlib.Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise Blocked("host-environment-too-large")
    values = {}
    for line in raw.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def enabled(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def assert_empty_farm(counts):
    if not isinstance(counts, dict) or any(type(counts.get(key)) is not int for key in PLAYER_FIELDS):
        raise Blocked("player-activity-unknown")
    if any(counts[key] != 0 for key in PLAYER_FIELDS):
        raise Blocked("current-or-recent-players-present")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request_socket(path, method, route, body=None, timeout=30):
    connection = UnixConnection(path, timeout)
    try:
        connection.request(method, route, body=json.dumps(body).encode() if body is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise Blocked("host-control-response-too-large")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise Blocked("host-control-response-invalid")
        if response.status not in {200, 202, 409}:
            raise Blocked("host-control-unavailable")
        return result
    finally:
        connection.close()


def approve_configuration(root, env_file, state, maps, hostname):
    """Called only by the native host installer, never by an HTTP route."""
    root, env_file, state = pathlib.Path(root).resolve(), pathlib.Path(env_file), pathlib.Path(state)
    if state.is_symlink():
        raise Blocked("host-control-state-directory-unsafe")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = root.stat()
    os.chmod(state, 0o700)
    os.chown(state, owner.st_uid, owner.st_gid)
    raw = env_file.read_bytes()
    values = read_environment(env_file)
    project = values.get("DUNE_RESTART_COMPOSE_PROJECT") or values.get("COMPOSE_PROJECT_NAME") or "dune_server"
    if not IDENTIFIER.fullmatch(project):
        raise Blocked("host-project-invalid")
    def run(command):
        if os.geteuid() == 0 and owner.st_uid != 0:
            command = ["runuser", "-u", pwd.getpwuid(owner.st_uid).pw_name, "--", *command]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode:
            raise Blocked("host-control-approval-check-failed")
        return result.stdout
    files = run([str(root / "scripts/compose-files.sh"), str(env_file)]).strip().split(":")
    command = ["docker", "compose", "--project-name", project, "--project-directory", str(root)]
    for name in files:
        command += ["-f", str((root / name).resolve())]
    rendered = json.loads(run(command + ["--profile", "*", "--env-file", str(env_file), "config", "--format", "json"]))
    allowed = set(maps) | STATEFUL | CONTROL
    identifiers = run(["docker", "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project]).split()
    records = {}
    for identifier in identifiers:
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise Blocked("host-control-container-identity-invalid")
        template = '{{.Id}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}'
        parts = run(["docker", "inspect", "--format", template, identifier]).strip().split("|")
        if len(parts) != 4 or parts[2] != project:
            raise Blocked("host-control-container-project-invalid")
        if parts[3] in allowed:
            if parts[3] in records or not re.fullmatch(r"sha256:[0-9a-f]{64}", parts[1]):
                raise Blocked("host-control-container-selection-ambiguous")
            records[parts[3]] = {"containerId": parts[0], "imageId": parts[1]}
    if not (STATEFUL | CONTROL) <= set(records):
        raise Blocked("host-control-required-services-missing")
    if not set(records) <= set(rendered.get("services") or {}):
        raise Blocked("host-control-approved-service-spec-missing")
    compose = json.dumps(rendered, sort_keys=True).encode() + b"\n"
    if env_file.read_bytes() != raw:
        raise Blocked("host-environment-changed-during-approval")
    policy = {
        "schemaVersion": PROTOCOL, "host": hostname, "project": project,
        "environmentPath": str(env_file.resolve()), "environmentSha256": hashlib.sha256(raw).hexdigest(),
        "environmentFile": "approved.env", "composeFile": "approved-compose.json",
        "composeSha256": hashlib.sha256(compose).hexdigest(),
        "secretDirectory": str((root / "config/secrets").resolve()), "services": records,
    }
    for name, content in (("approved.env", raw), ("approved-compose.json", compose),
                          ("approved-control.json", json.dumps(policy, sort_keys=True).encode() + b"\n")):
        descriptor, temporary = tempfile.mkstemp(prefix=".approve-", dir=state)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chown(temporary, owner.st_uid, owner.st_gid)
            os.replace(temporary, state / name)
        finally:
            pathlib.Path(temporary).unlink(missing_ok=True)
    return {"ok": True, "approvedExistingServices": len(records), "protocol": PROTOCOL}


def enable_approved_recovery(root, env_file, state, maps, hostname):
    """Native installer activation, after the hardened host protocol is loaded."""
    live = health("/run/dune-restart-api/restart.sock")
    if live.get("qualified") is not True or live.get("busy") is not False:
        raise Blocked("qualified-idle-host-control-required")
    admission = Admission(root, env_file, hostname, pathlib.Path(state) / "approved-control.json")
    admission.require_policy()
    target = pathlib.Path(env_file).resolve(strict=True)
    metadata = target.stat()
    if metadata.st_uid != pathlib.Path(root).stat().st_uid or metadata.st_mode & 0o077:
        raise Blocked("private-host-environment-required")
    raw = target.read_bytes()
    flags = {"DUNE_ADMIN_SERVICE_CONTROL_ENABLED", "DUNE_ADMIN_STATEFUL_SERVICE_CONTROL_ENABLED"}
    lines = raw.decode().splitlines()
    updated = [line for line in lines if line.partition("=")[0] not in flags]
    updated.extend(key + "=true" for key in sorted(flags))
    descriptor, temporary = tempfile.mkstemp(prefix=".recovery-enable-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write("\n".join(updated) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        if target.read_bytes() != raw:
            raise Blocked("host-environment-changed-during-activation")
        os.replace(temporary, target)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    # Until this succeeds, the changed dotenv fails the loaded protocol's policy
    # check. Activation cannot fall back to the previous, less constrained route.
    return approve_configuration(root, env_file, state, maps, hostname)


class Admission:
    def __init__(self, root, env_file, expected_hostname, policy_path=None):
        self.root = pathlib.Path(root).resolve()
        self.env_file = pathlib.Path(env_file)
        self.values = read_environment(env_file)
        self.expected_hostname = expected_hostname
        self.policy_path = pathlib.Path(policy_path or "/var/lib/dune-restart-api/approved-control.json")
        self.policy = None
        self.environment_digest = hashlib.sha256(self.env_file.read_bytes()).digest()
        self.project = self.values.get("DUNE_RESTART_COMPOSE_PROJECT") or self.values.get("COMPOSE_PROJECT_NAME") or "dune_server"
        if not IDENTIFIER.fullmatch(self.project):
            raise Blocked("host-project-invalid")

    def require_gate(self, service):
        if not self.expected_hostname or socket.gethostname().split(".")[0] != self.expected_hostname:
            raise Blocked("host-identity-not-approved")
        if not enabled(self.values.get("DUNE_ADMIN_SERVICE_CONTROL_ENABLED")):
            raise Blocked("service-control-disabled")
        if service in STATEFUL and not enabled(self.values.get("DUNE_ADMIN_STATEFUL_SERVICE_CONTROL_ENABLED")):
            raise Blocked("stateful-control-disabled")

    def run(self, arguments, timeout=60, env=None):
        result = subprocess.run(arguments, cwd=self.root, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode:
            raise Blocked("native-host-check-failed")
        return result.stdout

    def container(self, service):
        ids = self.run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={self.project}",
                        "--filter", f"label=com.docker.compose.service={service}"]).split()
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
            raise Blocked("exact-project-container-unavailable")
        return ids[0]

    def players(self):
        database = self.values.get("DUNE_GAME_DB_NAME") or self.values.get("DUNE_ADMIN_DB_NAME")
        role = self.values.get("DUNE_ADMIN_DB_USER") or "dune"
        if not database or not IDENTIFIER.fullmatch(database) or not IDENTIFIER.fullmatch(role):
            raise Blocked("database-identity-unavailable")
        output = self.run(["docker", "exec", "--user", "postgres", self.container("postgres"), "psql", "-XAtq",
                           "-v", "ON_ERROR_STOP=1", "-U", role, "-d", database, "-c", PLAYER_SQL], timeout=30)
        counts = json.loads(output)
        assert_empty_farm(counts)
        return counts

    def require_policy(self):
        metadata = self.policy_path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != self.root.stat().st_uid
                or metadata.st_mode & 0o077 or not 0 < metadata.st_size <= 128 * 1024):
            raise Blocked("host-control-policy-unavailable")
        policy = json.loads(self.policy_path.read_text())
        if (policy.get("schemaVersion") != PROTOCOL or policy.get("project") != self.project
                or policy.get("host") != self.expected_hostname
                or policy.get("environmentPath") != str(self.env_file.resolve())
                or policy.get("environmentSha256") != hashlib.sha256(self.env_file.read_bytes()).hexdigest()):
            raise Blocked("host-control-configuration-requires-approval")
        for name, field in (("environment", "environmentFile"), ("compose", "composeFile")):
            if policy.get(field) != {"environment": "approved.env", "compose": "approved-compose.json"}[name]:
                raise Blocked("host-control-approved-file-name-invalid")
            path = self.policy_path.parent / policy[field]
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.root.stat().st_uid or info.st_mode & 0o077
                    or hashlib.sha256(path.read_bytes()).hexdigest() != policy[name + "Sha256"]):
                raise Blocked("host-control-approved-files-changed")
        if str((self.root / "config/secrets").resolve()) != policy.get("secretDirectory"):
            raise Blocked("host-control-secret-source-changed")
        self.policy = policy
        self.values = read_environment(self.policy_path.parent / policy["environmentFile"])
        self.environment_digest = bytes.fromhex(policy["environmentSha256"])
        return policy

    def approved_environment(self, environment):
        policy = self.require_policy()
        result = dict(environment)
        result.update({
            "ENV_FILE": str(self.policy_path.parent / policy["environmentFile"]),
            "COMPOSE_PROJECT_NAME": self.project,
            "COMPOSE_FILES": str(self.policy_path.parent / policy["composeFile"]),
            "DUNE_COMPOSE_APPROVED_CONFIGURATION_FILE": str(self.policy_path.parent / policy["composeFile"]),
            "DUNE_SECRET_SOURCE_DIRECTORY": policy["secretDirectory"],
        })
        for key in ("DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED", "DUNE_LOGOFF_TIMER_AUTO_REMAP_ENABLED"):
            if key in self.values:
                result[key] = self.values[key]
        return result

    def same_image(self, service):
        policy = self.require_policy()
        record = (policy.get("services") or {}).get(service)
        if not isinstance(record, dict) or record.get("containerId") != self.container(service):
            raise Blocked("existing-container-identity-requires-approval")
        current = self.run(["docker", "inspect", "--format", "{{.Image}}", record["containerId"]]).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", current) or current != record.get("imageId"):
            raise Blocked("existing-container-image-requires-approval")
        return current

    def recover_existing(self, service, action, environment, *, dry_run=False, post_hooks=True):
        if action not in {"start", "stop", "restart"}:
            raise Blocked("existing-container-action-invalid")
        self.same_image(service)
        identifier = self.policy["services"][service]["containerId"]
        if dry_run:
            return {"ok": True, "exitCode": 0, "postHooks": False}
        if action in {"start", "restart"}:
            self.run([str(self.root / "scripts/validate-landsraad-coriolis-cycle.sh"), environment["ENV_FILE"]], env=environment)
        command = ["docker", action]
        if action in {"restart", "stop"}:
            command += ["--time", "30"]
        self.run(command + [identifier], timeout=1800, env=environment)
        if post_hooks and action in {"start", "restart"}:
            self.post_start_hooks(environment)
        self.same_image(service)
        return {"ok": True, "exitCode": 0, "postHooks": action in {"start", "restart"}}

    def post_start_hooks(self, environment):
        self.run([str(self.root / "scripts/restart-post-start-health.sh")], timeout=1800, env=environment)
        patch = self.root / "scripts/patch-logoff-timers-runtime.sh"
        if patch.is_file() and enabled(self.values.get("DUNE_LOGOFF_TIMER_RUNTIME_PATCH_ENABLED", "true")):
            self.run([str(patch), "--local", "--dry-run"], timeout=180, env=environment)

    def readiness(self, *, return_receipt=False):
        configured = dict(self.values)
        if configured.get("DUNE_ADMIN_TOKEN_FILE"):
            configured["DUNE_ADMIN_TOKEN_FILE"] = str(self.host_path(configured["DUNE_ADMIN_TOKEN_FILE"], ""))
        token = read_secret_env("DUNE_ADMIN_TOKEN", environment=configured)
        port = self.values.get("DUNE_ADMIN_HOST_PORT") or "18080"
        if not token or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise Blocked("native-admin-configuration-unavailable")
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=1800)
        try:
            connection.request("GET", "/api/ops/update-readiness?refresh=true", headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 4 * 1024 * 1024:
                raise Blocked("fresh-readiness-unavailable")
            status = json.loads(raw)
        finally:
            connection.close()
        evaluation = status.get("evaluation") or {}
        latest = status.get("latest") or {}
        if status.get("applyReady") is not True or status.get("currentReceiptReady") is not True or evaluation.get("immediateReady") is not True:
            raise Blocked("fresh-readiness-not-ready")
        filename = latest.get("file") or ""
        if not re.fullmatch(r"update-readiness-[0-9a-f]{32}\.signed\.json", filename):
            raise Blocked("readiness-receipt-unavailable")
        evidence = self.host_path(self.values.get("DUNE_CHANGE_INTELLIGENCE_EVIDENCE_DIR"), "backups/operator-evidence")
        path = evidence / filename
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2 * 1024 * 1024:
            raise Blocked("readiness-receipt-unsafe")
        key = self.host_path(self.values.get("DUNE_CHANGE_INTELLIGENCE_HMAC_SECRET_FILE"), "config/secrets/change-intelligence-hmac.secret")
        document = json.loads(path.read_text())
        verification = update_readiness.verify_signed_document(document, change_intelligence.read_secret(key))
        receipt = document.get("receipt") or {}
        if not verification.get("ok") or verification.get("expired") or receipt.get("id") != latest.get("id"):
            raise Blocked("readiness-signature-invalid-or-expired")
        if (receipt.get("candidate") or {}).get("fingerprint") != (evaluation.get("candidate") or {}).get("fingerprint"):
            raise Blocked("readiness-candidate-changed")
        assurance = (evaluation.get("details") or {}).get("deploymentAssurance") or {}
        if not receipt.get("sourceCommit") or receipt.get("sourceCommit") != assurance.get("commit"):
            raise Blocked("readiness-source-changed")
        return receipt if return_receipt else receipt.get("id")

    def host_path(self, configured, fallback):
        path = pathlib.Path(configured or fallback)
        if path == pathlib.Path("/workspace-secret-files") or str(path).startswith("/workspace-secret-files/"):
            relative = path.relative_to("/workspace-secret-files")
            if ".." in relative.parts:
                raise Blocked("host-secret-path-escape")
            source = pathlib.Path(self.require_policy()["secretDirectory"]).resolve(strict=True)
            resolved = (source / relative).resolve(strict=True)
            if not resolved.is_relative_to(source):
                raise Blocked("host-secret-path-escape")
            return resolved
        if str(path).startswith("/workspace/"):
            path = self.root / path.relative_to("/workspace")
        return path if path.is_absolute() else self.root / path

    def backup(self, environment):
        output = self.run([str(self.root / "scripts/backup-state.sh"), environment["ENV_FILE"]], timeout=1800, env=environment)
        paths = [line.removeprefix("backup complete: ").strip() for line in output.splitlines() if line.startswith("backup complete: ")]
        if len(paths) != 1:
            raise Blocked("fresh-backup-unavailable")
        path = (self.root / paths[0]).resolve()
        if not path.is_dir() or not path.is_relative_to((self.root / "backups").resolve()):
            raise Blocked("fresh-backup-path-invalid")
        self.run([str(self.root / "scripts/verify-backup.sh"), str(path)], timeout=1800, env=environment)
        return path.name

    def unchanged_environment(self):
        if hashlib.sha256(self.env_file.read_bytes()).digest() != self.environment_digest:
            raise Blocked("host-configuration-changed-during-admission")


def health(socket_path):
    try:
        result = request_socket(socket_path, "GET", "/v1/health", timeout=5)
        result["qualified"] = (result.get("ok") is True and result.get("protocol") == PROTOCOL
                               and set(result.get("guards") or ()) == set(GUARDS))
        return result
    except (OSError, ValueError, Blocked, http.client.HTTPException):
        return {"qualified": False, "serviceControlReady": False, "statefulControlReady": False}


def control(socket_path, service, action, timeout=1800):
    import secrets
    import time
    request = {"service": service, "action": action, "jobId": secrets.token_hex(16)}
    # Submission is never automatically retried; callers can inspect the durable job ID.
    result = request_socket(socket_path, "POST", "/v1/service-control", request)
    deadline = time.monotonic() + timeout
    while result.get("state") not in {"completed", "failed", "blocked"}:
        if result.get("jobId") != request["jobId"]:
            raise Blocked("host-control-job-identity-invalid")
        if time.monotonic() >= deadline:
            raise Blocked("host-control-still-running:" + request["jobId"])
        time.sleep(1)
        result = request_socket(socket_path, "GET", "/v1/service-control/" + request["jobId"])
    if result.get("jobId") != request["jobId"] or result.get("service") != service or result.get("action") != action:
        raise Blocked("host-control-job-identity-invalid")
    if result.get("ok") is not True:
        raise Blocked(str(result.get("error") or "native-service-control-failed"))
    return result
