#!/usr/bin/env python3
"""Signed disposable proof for the complete public-IP repair lifecycle."""

import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time


SCHEMA = "dune-public-ip-repair-canary/v1"
RECEIPT_SCHEMA = 1
ID_PATTERN = re.compile(r"^public-ip-canary-[0-9a-f]{32}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OLD_ADDRESS = "198.51.100.10"
NEW_ADDRESS = "198.51.100.20"
INPUT_FILES = (
    "admin/admin_panel.py",
    "admin/public_ip_canary.py",
    "scripts/public-ip-monitor.sh",
    "scripts/generate-rabbitmq-cert.sh",
    "scripts/check-rabbitmq-cert-sans.sh",
    "scripts/restart-target.sh",
    "scripts/map_catalog.py",
    "scripts/install-public-ip-monitor.sh",
    "config/systemd/dune-public-ip-monitor.service",
    "config/systemd/dune-public-ip-monitor.timer",
)
CHECK_NAMES = (
    "inputsBound", "dryRunPlan", "hostnameGuard", "currentNoop",
    "fullAddressRewrite", "environmentBackup", "tlsRotation",
    "restartHandoff", "restartRetry", "timerInstall",
    "sourceInputsUnchanged", "temporaryStateRemoved",
)
ISOLATION_KEYS = (
    "temporaryStateCreated", "temporaryStateRemoved",
    "liveEnvironmentWritten", "liveTlsWritten", "liveSystemdWritten",
    "liveStateDirectoryOpened", "gameMapLifecycleInvoked",
    "externalNetworkCalled",
)
RECEIPT_FIELDS = {
    "schemaVersion", "id", "principalId", "startedAt", "completedAt",
    "durationMs", "inputsSha256", "checks", "evidence", "isolation",
    "ready", "receiptSha256",
}
EVIDENCE_FIELDS = {
    "inputFiles", "oldAddress", "newAddress", "environmentBackups",
    "tlsBackupFiles", "tlsSans", "restartServices",
    "timerIntervalMinutes", "certificateSha256",
}
LOCK = threading.RLock()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def iso(value=None):
    value = time.time() if value is None else float(value)
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(value):
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("public-IP canary timestamp lacks a timezone")
    return parsed.timestamp()


def _file_sha256(path):
    path = pathlib.Path(path)
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 50 * 1024 * 1024:
        raise ValueError(f"public-IP canary input is not a bounded regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_manifest(root):
    root = pathlib.Path(root).resolve(strict=True)
    rows = []
    for relative in INPUT_FILES:
        candidate = root / relative
        if candidate.is_symlink():
            raise ValueError(f"public-IP canary input cannot be a symlink: {relative}")
        path = candidate.resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("public-IP canary input escapes the workspace") from exc
        rows.append({"path": relative, "sha256": _file_sha256(path), "bytes": path.stat().st_size})
    return {"files": rows, "sha256": hashlib.sha256(canonical(rows).encode()).hexdigest()}


def receipt_hash(receipt):
    return hashlib.sha256(canonical({key: value for key, value in receipt.items() if key != "receiptSha256"}).encode()).hexdigest()


def signed_document(receipt, secret, generated_at=None):
    receipt = json.loads(canonical(receipt))
    receipt["receiptSha256"] = receipt_hash(receipt)
    payload = {
        "schemaVersion": SCHEMA,
        "generatedAt": iso(generated_at),
        "signingKeyFingerprint": hashlib.sha256(secret).hexdigest(),
        "receipt": receipt,
    }
    return {**payload, "signature": hmac.new(secret, canonical(payload).encode(), hashlib.sha256).hexdigest()}


def verify_signed_document(document, secret, *, current_inputs_sha256=None, max_age_seconds=None, now=None):
    try:
        if not isinstance(document, dict) or set(document) != {
            "schemaVersion", "generatedAt", "signingKeyFingerprint", "receipt", "signature",
        }:
            raise ValueError("public-IP canary signed document fields are invalid")
        if document.get("schemaVersion") != SCHEMA:
            raise ValueError("public-IP canary schema is invalid")
        payload = {key: value for key, value in document.items() if key != "signature"}
        expected = hmac.new(secret, canonical(payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(document.get("signature") or ""), expected):
            raise ValueError("public-IP canary signature is invalid")
        if not hmac.compare_digest(str(document.get("signingKeyFingerprint") or ""), hashlib.sha256(secret).hexdigest()):
            raise ValueError("public-IP canary signing key fingerprint differs")
        generated = epoch(document.get("generatedAt"))
        receipt = document.get("receipt")
        if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS or receipt.get("schemaVersion") != RECEIPT_SCHEMA:
            raise ValueError("public-IP canary receipt fields are invalid")
        if not ID_PATTERN.fullmatch(str(receipt.get("id") or "")):
            raise ValueError("public-IP canary receipt id is invalid")
        principal = str(receipt.get("principalId") or "")
        if not 1 <= len(principal) <= 128 or any(ord(char) < 32 for char in principal):
            raise ValueError("public-IP canary principal is invalid")
        started, completed = epoch(receipt.get("startedAt")), epoch(receipt.get("completedAt"))
        if not 0 <= completed - started <= 300:
            raise ValueError("public-IP canary execution interval is invalid")
        duration = receipt.get("durationMs")
        if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 300_000:
            raise ValueError("public-IP canary duration evidence is invalid")
        if abs(duration - round((completed - started) * 1000)) > 2000:
            raise ValueError("public-IP canary duration does not match timestamps")
        if abs(generated - completed) > 2:
            raise ValueError("public-IP canary signed time does not match completion")
        inputs_sha = str(receipt.get("inputsSha256") or "")
        if not HASH_PATTERN.fullmatch(inputs_sha):
            raise ValueError("public-IP canary input digest is invalid")
        checks = receipt.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(CHECK_NAMES) or any(type(value) is not bool for value in checks.values()):
            raise ValueError("public-IP canary checks are invalid")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS:
            raise ValueError("public-IP canary evidence fields are invalid")
        for key in (
            "inputFiles", "environmentBackups", "tlsBackupFiles", "tlsSans",
            "restartServices", "timerIntervalMinutes",
        ):
            value = evidence.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
                raise ValueError("public-IP canary evidence count is invalid")
        if evidence.get("oldAddress") != OLD_ADDRESS or evidence.get("newAddress") != NEW_ADDRESS:
            raise ValueError("public-IP canary synthetic addresses are invalid")
        if not HASH_PATTERN.fullmatch(str(evidence.get("certificateSha256") or "")):
            raise ValueError("public-IP canary certificate digest is invalid")
        isolation = receipt.get("isolation")
        if not isinstance(isolation, dict) or set(isolation) != set(ISOLATION_KEYS) or any(type(value) is not bool for value in isolation.values()):
            raise ValueError("public-IP canary isolation fields are invalid")
        unsafe = any(isolation[key] for key in (
            "liveEnvironmentWritten", "liveTlsWritten", "liveSystemdWritten",
            "liveStateDirectoryOpened", "gameMapLifecycleInvoked", "externalNetworkCalled",
        ))
        isolation_ready = bool(isolation["temporaryStateCreated"] and isolation["temporaryStateRemoved"] and not unsafe)
        expected_ready = all(checks.values()) and isolation_ready
        if type(receipt.get("ready")) is not bool or receipt["ready"] is not expected_ready:
            raise ValueError("public-IP canary verdict is inconsistent")
        expected_hash = receipt_hash(receipt)
        if not HASH_PATTERN.fullmatch(str(receipt.get("receiptSha256") or "")) or not hmac.compare_digest(receipt["receiptSha256"], expected_hash):
            raise ValueError("public-IP canary receipt digest is invalid")
        reference = float(time.time() if now is None else now)
        if completed > reference + 300:
            raise ValueError("public-IP canary completion is implausibly in the future")
        inputs_current = current_inputs_sha256 is None or hmac.compare_digest(inputs_sha, str(current_inputs_sha256))
        age_seconds = max(0.0, reference - completed)
        age_current = max_age_seconds is None or age_seconds <= float(max_age_seconds)
        return {
            "ok": True, "signatureValid": True, "receiptValid": True,
            "receiptId": receipt["id"], "receiptSha256": receipt["receiptSha256"],
            "ready": receipt["ready"], "inputsCurrent": inputs_current,
            "ageCurrent": age_current, "ageSeconds": round(age_seconds, 3),
            "currentReady": bool(receipt["ready"] and inputs_current and age_current),
        }
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"ok": False, "signatureValid": False, "receiptValid": False, "currentReady": False, "error": str(exc)}


class Store:
    def __init__(self, evidence_root, secret, *, retention=200, max_age_seconds=7 * 86400, owner_uid=None, owner_gid=None):
        self.evidence_root = pathlib.Path(evidence_root)
        self.secret = bytes(secret)
        self.retention = max(10, min(int(retention), 2000))
        self.max_age_seconds = max(300, min(int(max_age_seconds), 90 * 86400))
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None

    def _secure(self, path):
        path = pathlib.Path(path)
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None:
            os.chown(path, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)

    def initialize(self):
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self._secure(self.evidence_root)
        for path in self._paths(initializing=True):
            self._secure(path)

    def _paths(self, initializing=False):
        if not initializing:
            self.initialize()
        return sorted(
            (path for path in self.evidence_root.glob("public-ip-canary-*.signed.json") if path.is_file() and not path.is_symlink()),
            key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
        )

    def _write(self, path, document):
        temporary = path.with_suffix(path.suffix + ".tmp-" + secrets.token_hex(8))
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._secure(temporary)
            temporary.replace(path)
            self._secure(path)
        finally:
            temporary.unlink(missing_ok=True)

    def record(self, receipt, completed_epoch):
        document = signed_document(receipt, self.secret, generated_at=completed_epoch)
        path = self.evidence_root / f"{receipt['id']}.signed.json"
        with LOCK:
            self.initialize()
            self._write(path, document)
            for stale in self._paths()[self.retention:]:
                stale.unlink(missing_ok=True)
        return {"document": document, "evidencePath": str(path), "verification": verify_signed_document(document, self.secret)}

    def status(self, inputs_sha256, limit=20, now=None):
        all_rows = []
        for path in self._paths():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                verification = verify_signed_document(
                    document, self.secret, current_inputs_sha256=inputs_sha256,
                    max_age_seconds=self.max_age_seconds, now=now,
                )
                receipt = document.get("receipt") or {}
                row = {
                    "id": receipt.get("id"), "completedAt": receipt.get("completedAt"),
                    "durationMs": receipt.get("durationMs"), "ready": bool(receipt.get("ready")),
                    "inputsSha256": receipt.get("inputsSha256"), "checks": receipt.get("checks"),
                    "evidence": receipt.get("evidence"), "isolation": receipt.get("isolation"),
                    "receiptSha256": receipt.get("receiptSha256"), "file": path.name,
                    "verification": verification,
                }
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                row = {"id": path.stem, "ready": False, "file": path.name, "verification": {"ok": False, "currentReady": False, "error": str(exc)}}
            all_rows.append(row)
        rows = all_rows[:max(1, min(int(limit), self.retention))]
        latest = all_rows[0] if all_rows else None
        return {
            "ok": all((row.get("verification") or {}).get("ok") for row in all_rows),
            "currentReady": bool(latest and (latest.get("verification") or {}).get("currentReady")),
            "latest": latest, "receipts": rows, "retention": self.retention,
            "maxAgeSeconds": self.max_age_seconds,
            "summary": {"retained": len(all_rows), "returned": len(rows)},
        }

    def prometheus(self, inputs_sha256, now=None):
        status = self.status(inputs_sha256, now=now)
        latest = status.get("latest") or {}
        verification = latest.get("verification") or {}
        completed = epoch(latest["completedAt"]) if latest.get("completedAt") and verification.get("ok") else "NaN"
        return "\n".join([
            f"dash_public_ip_canary_collector_up {1 if status.get('ok') else 0}",
            f"dash_public_ip_canary_current_ready {1 if status.get('currentReady') else 0}",
            f"dash_public_ip_canary_last_completion_timestamp_seconds {completed}",
            f"dash_public_ip_canary_age_seconds {float(verification.get('ageSeconds') or 0):.3f}",
            f"dash_public_ip_canary_retained_receipts {int((status.get('summary') or {}).get('retained') or 0)}",
        ]) + "\n"


def _run(arguments, *, cwd, environment, timeout=60, check=True, runner=None):
    normalized = [str(value) for value in arguments]
    if runner is None:
        completed = subprocess.run(
            normalized, cwd=str(cwd), env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    else:
        completed = runner(normalized, cwd=str(cwd), environment=dict(environment), timeout=timeout)
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout or "subprocess failed")[-2000:]
        raise ValueError(f"public-IP canary subprocess failed ({completed.returncode}): {detail}")
    return completed


def _write_env(path, values):
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    os.chmod(path, 0o600)


def _read_values(path):
    result = {}
    for raw in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            result[key] = value
    return result


def _state(path):
    return _read_values(path / "public-ip-monitor.state")


def _copy_workspace(root, stage):
    for relative in INPUT_FILES:
        source, target = root / relative, stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in (
        "scripts/public-ip-monitor.sh", "scripts/generate-rabbitmq-cert.sh",
        "scripts/check-rabbitmq-cert-sans.sh", "scripts/restart-target.sh",
        "scripts/map_catalog.py",
        "scripts/install-public-ip-monitor.sh",
    ):
        os.chmod(stage / relative, 0o700)


def run_canary(root, receipt_store, *, principal_id="system", now=time.time, work_root=None, runner=None):
    root = pathlib.Path(root).resolve(strict=True)
    inputs = input_manifest(root)
    temporary_parent = None
    if work_root is not None:
        temporary_parent = pathlib.Path(work_root)
        if temporary_parent.is_symlink():
            raise ValueError("public-IP canary work root cannot be a symlink")
        temporary_parent.mkdir(parents=True, exist_ok=True)
        if temporary_parent.is_symlink() or not temporary_parent.is_dir():
            raise ValueError("public-IP canary work root must be a directory")
        os.chmod(temporary_parent, 0o700)
    started_epoch = float(now())
    checks = {name: False for name in CHECK_NAMES}
    evidence = {
        "inputFiles": len(inputs["files"]), "oldAddress": OLD_ADDRESS,
        "newAddress": NEW_ADDRESS, "environmentBackups": 0,
        "tlsBackupFiles": 0, "tlsSans": 0, "restartServices": 0,
        "timerIntervalMinutes": 7, "certificateSha256": "0" * 64,
    }
    temporary_created = False
    temporary_removed = False
    failure = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="dash-public-ip-canary-",
            dir=str(temporary_parent) if temporary_parent is not None else None,
        ) as directory:
            stage = pathlib.Path(directory) / "workspace"
            stage.mkdir(mode=0o700)
            temporary_created = True
            _copy_workspace(root, stage)
            checks["inputsBound"] = len(inputs["files"]) == len(INPUT_FILES) and HASH_PATTERN.fullmatch(inputs["sha256"]) is not None

            hostname = os.uname().nodename.split(".", 1)[0]
            base_values = {
                "EXTERNAL_ADDRESS": OLD_ADDRESS,
                "GAME_RMQ_PUBLIC_HOST": OLD_ADDRESS,
                "DUNE_PUBLIC_IP_MONITOR_ENABLED": "true",
                "DUNE_PUBLIC_IP_MONITOR_ALLOWED_HOST": hostname,
                "DUNE_PUBLIC_IP_MONITOR_INTERVAL_MINUTES": "7",
                "DUNE_PUBLIC_IP_MONITOR_DRY_RUN": "true",
                "DUNE_WORLD_PARTITION_COUNT": "30",
            }
            fixture_env = stage / "fixture.env"
            _write_env(fixture_env, base_values)
            original_env = fixture_env.read_bytes()

            bin_dir = stage / "canary-bin"
            bin_dir.mkdir(mode=0o700)
            command_log = stage / "commands.log"
            for name, body in {
                "docker": (
                    "#!/usr/bin/env python3\nimport json, os, sys\n"
                    "args = sys.argv[1:]\n"
                    "if (args[:1] == ['compose'] and args[-4:] == ['config', '--format', 'json', '--no-interpolate']\n"
                    "        and '--env-file' in args and args[args.index('--env-file') + 1] == os.devnull):\n"
                    "    print(json.dumps({'services': {'survival': {'command': ['-PartitionIndex=1'],\n"
                    "        'labels': {'dune.map.database-name': 'Survival_1'}}}}))\n"
                    "    raise SystemExit(0)\n"
                    "with open(os.environ['DUNE_PUBLIC_IP_CANARY_COMMAND_LOG'], 'a') as log:\n"
                    "    log.write('unexpected docker invocation\\n')\n"
                    "raise SystemExit(97)\n"
                ),
                "systemctl": "#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >>\"$DUNE_PUBLIC_IP_CANARY_COMMAND_LOG\"\n",
                "sudo": "#!/bin/sh\nexec \"$@\"\n",
            }.items():
                path = bin_dir / name
                path.write_text(body, encoding="utf-8")
                os.chmod(path, 0o700)
            announce_log = stage / "announce.log"
            announce = stage / "scripts" / "announce.sh"
            announce.write_text(
                "#!/bin/sh\nset -eu\nprintf '%s|%s\\n' \"$DUNE_ANNOUNCE_JOB_ID\" \"$DUNE_ANNOUNCE_MESSAGE\" >>\"$DUNE_PUBLIC_IP_CANARY_ANNOUNCE_LOG\"\n",
                encoding="utf-8",
            )
            os.chmod(announce, 0o700)
            environment = {
                "HOME": str(stage), "LANG": "C", "LC_ALL": "C",
                "PATH": str(bin_dir) + os.pathsep + "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "DUNE_PUBLIC_IP_CANARY_COMMAND_LOG": str(command_log),
                "DUNE_PUBLIC_IP_CANARY_ANNOUNCE_LOG": str(announce_log),
                "DUNE_RESTART_DRY_RUN": "true",
                "DUNE_RESTART_CHECK_STEAM_UPDATE": "false",
                "DUNE_MAP_WATCHDOG_CONTROL": "false",
            }

            dry_state = stage / "dry-state"
            dry_environment = {**environment, "DUNE_PUBLIC_IP_MONITOR_STATE_DIR": str(dry_state), "DUNE_PUBLIC_IP_MONITOR_DETECTED_IP": NEW_ADDRESS}
            planned = _run([stage / "scripts/public-ip-monitor.sh", fixture_env, "check"], cwd=stage, environment=dry_environment, runner=runner)
            checks["dryRunPlan"] = bool(
                "dry-run: would update EXTERNAL_ADDRESS" in planned.stdout
                and _state(dry_state).get("status") == "dry-run"
                and fixture_env.read_bytes() == original_env
            )

            guard_env = stage / "guard.env"
            _write_env(guard_env, {**base_values, "DUNE_PUBLIC_IP_MONITOR_ALLOWED_HOST": "not-this-host"})
            guard_state = stage / "guard-state"
            guarded = _run(
                [stage / "scripts/public-ip-monitor.sh", guard_env, "check"], cwd=stage,
                environment={**environment, "DUNE_PUBLIC_IP_MONITOR_STATE_DIR": str(guard_state), "DUNE_PUBLIC_IP_MONITOR_DETECTED_IP": NEW_ADDRESS},
                check=False, runner=runner,
            )
            checks["hostnameGuard"] = guarded.returncode == 77 and _state(guard_state).get("status") == "refused"

            current_env = stage / "current.env"
            _write_env(current_env, base_values)
            current_state = stage / "current-state"
            current = _run(
                [stage / "scripts/public-ip-monitor.sh", current_env, "check"], cwd=stage,
                environment={**environment, "DUNE_PUBLIC_IP_MONITOR_STATE_DIR": str(current_state), "DUNE_PUBLIC_IP_MONITOR_DETECTED_IP": OLD_ADDRESS},
                runner=runner,
            )
            checks["currentNoop"] = bool(
                "public IP unchanged" in current.stdout and _state(current_state).get("status") == "current"
                and _read_values(current_env).get("EXTERNAL_ADDRESS") == OLD_ADDRESS
            )

            _write_env(fixture_env, {**base_values, "DUNE_PUBLIC_IP_MONITOR_DRY_RUN": "false"})
            tls = stage / "config/tls/rabbitmq"
            tls.mkdir(parents=True, mode=0o700)
            for name in ("ca.crt", "server.crt", "server.key"):
                (tls / name).write_text("old-" + name + "\n", encoding="utf-8")
            full_state = stage / "full-state"
            full_environment = {**environment, "DUNE_PUBLIC_IP_MONITOR_STATE_DIR": str(full_state), "DUNE_PUBLIC_IP_MONITOR_DETECTED_IP": NEW_ADDRESS}
            repaired = _run([stage / "scripts/public-ip-monitor.sh", fixture_env, "check"], cwd=stage, environment=full_environment, timeout=120, runner=runner)
            values = _read_values(fixture_env)
            checks["fullAddressRewrite"] = values.get("EXTERNAL_ADDRESS") == NEW_ADDRESS and values.get("GAME_RMQ_PUBLIC_HOST") == NEW_ADDRESS and _state(full_state).get("status") == "restarted"

            env_backups = list(full_state.glob("public-ip-change-*.env"))
            evidence["environmentBackups"] = len(env_backups)
            checks["environmentBackup"] = bool(
                len(env_backups) == 1
                and _read_values(env_backups[0]).get("EXTERNAL_ADDRESS") == OLD_ADDRESS
                and env_backups[0].stat().st_mode & 0o077 == 0
            )
            tls_backups = list(full_state.glob("rabbitmq-tls-before-public-ip-*"))
            backup_files = list(tls_backups[0].iterdir()) if len(tls_backups) == 1 else []
            evidence["tlsBackupFiles"] = len(backup_files)
            certificate = tls / "server.crt"
            certificate_text = _run(["openssl", "x509", "-in", certificate, "-noout", "-ext", "subjectAltName"], cwd=stage, environment=environment, runner=runner).stdout
            sans = [item.strip() for item in certificate_text.replace("\n", " ").split(",") if ":" in item]
            evidence["tlsSans"] = len(sans)
            evidence["certificateSha256"] = _file_sha256(certificate)
            checks["tlsRotation"] = bool(
                evidence["tlsBackupFiles"] == 3
                and all((tls_backups[0] / name).read_text(encoding="utf-8").startswith("old-") for name in ("ca.crt", "server.crt", "server.key"))
                and f"IP Address:{NEW_ADDRESS}" in certificate_text
                and f"IP Address:{OLD_ADDRESS}" not in certificate_text
            )

            restart_rows = []
            for line in repaired.stdout.splitlines():
                if line.startswith("{"):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("target") == "all" and row.get("phase") == "restart":
                        restart_rows.append(row)
            restart = restart_rows[-1] if restart_rows else {}
            services = str(restart.get("services") or "").split()
            evidence["restartServices"] = len(services)
            checks["restartHandoff"] = bool(
                restart.get("ok") and restart.get("dryRun") and restart.get("action") == "restart"
                and "survival" in services and "gateway" in services
                and announce_log.is_file() and "Server address changed" in announce_log.read_text(encoding="utf-8")
                and not command_log.exists()
            )

            state_path = full_state / "public-ip-monitor.state"
            state_values = _read_values(state_path)
            _write_env(state_path, {**state_values, "status": "restarting"})
            retried = _run([stage / "scripts/public-ip-monitor.sh", fixture_env, "check"], cwd=stage, environment=full_environment, runner=runner)
            checks["restartRetry"] = "retrying the incomplete farm restart" in retried.stdout and _state(full_state).get("status") == "restarted"

            units = stage / "units"
            units.mkdir(mode=0o700)
            installer = _run(
                [stage / "scripts/install-public-ip-monitor.sh", fixture_env, units], cwd=stage,
                environment={**environment, "DUNE_SERVICE_USER": "canary-user", "DUNE_SERVICE_GROUP": "canary-group"},
                runner=runner,
            )
            service_text = (units / "dune-public-ip-monitor.service").read_text(encoding="utf-8")
            timer_text = (units / "dune-public-ip-monitor.timer").read_text(encoding="utf-8")
            command_text = command_log.read_text(encoding="utf-8") if command_log.exists() else ""
            checks["timerInstall"] = bool(
                "installed and enabled" in installer.stdout
                and f"WorkingDirectory={stage}" in service_text
                and f"ExecStart={stage}/scripts/public-ip-monitor.sh {fixture_env} check" in service_text
                and "User=canary-user" in service_text and "Group=canary-group" in service_text
                and "OnUnitInactiveSec=7min" in timer_text
                and "systemctl daemon-reload" in command_text
                and "systemctl enable --now dune-public-ip-monitor.timer" in command_text
            )
        temporary_removed = not pathlib.Path(directory).exists()
        checks["temporaryStateRemoved"] = temporary_removed
    except Exception as exc:
        failure = str(exc)[-2000:]
        temporary_removed = "directory" not in locals() or not pathlib.Path(directory).exists()
        checks["temporaryStateRemoved"] = temporary_removed

    try:
        checks["sourceInputsUnchanged"] = hmac.compare_digest(inputs["sha256"], input_manifest(root)["sha256"])
    except (OSError, ValueError):
        checks["sourceInputsUnchanged"] = False
    completed_epoch = float(now())
    isolation = {
        "temporaryStateCreated": temporary_created,
        "temporaryStateRemoved": temporary_removed,
        "liveEnvironmentWritten": False,
        "liveTlsWritten": False,
        "liveSystemdWritten": False,
        "liveStateDirectoryOpened": False,
        "gameMapLifecycleInvoked": False,
        "externalNetworkCalled": False,
    }
    ready = all(checks.values()) and temporary_created and temporary_removed and failure is None
    receipt = {
        "schemaVersion": RECEIPT_SCHEMA,
        "id": "public-ip-canary-" + secrets.token_hex(16),
        "principalId": str(principal_id or "system")[:128],
        "startedAt": iso(started_epoch), "completedAt": iso(completed_epoch),
        "durationMs": int(round((completed_epoch - started_epoch) * 1000)),
        "inputsSha256": inputs["sha256"], "checks": checks,
        "evidence": evidence, "isolation": isolation, "ready": ready,
    }
    result = receipt_store.record(receipt, completed_epoch)
    result["error"] = failure
    return result
