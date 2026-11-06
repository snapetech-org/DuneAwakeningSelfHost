"""Two-phase, HMAC-signed deployment assurance receipts for DASH."""

import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import threading
import time


SIGNED_SCHEMA = "dune-deployment-assurance/v1"
WINDOW_SCHEMA = "dune-deployment-assurance-window/v1"
RECEIPT_SCHEMA = 1
PATH_PART = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
WINDOW_PATTERN = re.compile(r"^deployment-window-[0-9a-f]{32}$")
RECEIPT_PATTERN = re.compile(r"^deployment-assurance-[0-9a-f]{32}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 256
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_WINDOW_SECONDS = 6 * 3600
LOCK = threading.RLock()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def iso(epoch=None):
    return datetime.datetime.fromtimestamp(float(time.time() if epoch is None else epoch), datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def safe_relative_path(value):
    text = str(value or "").strip().replace("\\", "/")
    candidate = pathlib.PurePosixPath(text)
    if not text or candidate.is_absolute() or len(candidate.parts) > 12:
        raise ValueError("deployment manifest path must be a bounded relative path")
    if any(part in {"", ".", ".."} or not PATH_PART.fullmatch(part) for part in candidate.parts):
        raise ValueError(f"deployment manifest path is invalid: {text!r}")
    if candidate.parts[0] in {"backups", "captures", "data", ".git"} or text == ".env" or candidate.parts[:2] == ("config", "secrets"):
        raise ValueError(f"deployment manifest path is private or mutable state: {text!r}")
    return candidate.as_posix()


def file_sha256(path):
    result = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_manifest_rows(rows, *, require_bytes=False):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise ValueError(f"deployment manifest must contain 1..{MAX_FILES} files")
    seen = set()
    normalized = []
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("deployment manifest entries must be objects")
        relative = safe_relative_path(row.get("path"))
        if relative in seen:
            raise ValueError(f"deployment manifest path is duplicate: {relative}")
        seen.add(relative)
        expected = str(row.get("sha256") or "").strip().lower()
        if not HASH_PATTERN.fullmatch(expected):
            raise ValueError(f"deployment manifest SHA-256 is invalid: {relative}")
        declared_size = row.get("bytes")
        if declared_size is None and require_bytes:
            raise ValueError(f"deployment manifest byte count is required: {relative}")
        if declared_size is not None and (not isinstance(declared_size, int) or not 0 <= declared_size <= MAX_FILE_BYTES):
            raise ValueError(f"deployment manifest byte count is invalid: {relative}")
        if declared_size is not None:
            total += declared_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("deployment manifest exceeds 100 MiB")
        normalized.append({"path": relative, "sha256": expected, **({"bytes": declared_size} if declared_size is not None else {})})
    normalized.sort(key=lambda item: item["path"])
    return normalized


def normalize_manifest(root, rows, *, require_match=True):
    expected_rows = validate_manifest_rows(rows)
    root = pathlib.Path(root).resolve()
    normalized = []
    total = 0
    for row in expected_rows:
        relative = row["path"]
        expected = row["sha256"]
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"deployment manifest path escapes workspace: {relative}") from exc
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"deployment manifest path is not a regular file: {relative}")
        size = target.stat().st_size
        if not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError(f"deployment manifest file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("deployment manifest exceeds 100 MiB")
        actual = file_sha256(target)
        if require_match and (not hmac.compare_digest(actual, expected) or (row.get("bytes") is not None and row["bytes"] != size)):
            raise ValueError(f"deployment manifest digest does not match workspace: {relative}")
        normalized.append({"path": relative, "sha256": expected, "bytes": size, "declaredBytes": row.get("bytes"), "actualSha256": actual})
    normalized.sort(key=lambda item: item["path"])
    return normalized


def normalize_snapshot(snapshot, services):
    if not isinstance(snapshot, dict):
        raise ValueError("deployment container snapshot must be an object")
    output = {}
    for service in sorted(set(str(value) for value in services)):
        row = snapshot.get(service)
        if row is None:
            output[service] = {"present": False, "containerId": None, "state": "missing", "startedAt": None}
            continue
        if not isinstance(row, dict):
            raise ValueError(f"deployment container snapshot row is invalid: {service}")
        container_id = str(row.get("containerId") or "")
        if container_id and not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise ValueError(f"deployment container id is invalid: {service}")
        started = row.get("startedAt") or None
        if started:
            epoch(started)
        output[service] = {
            "present": bool(row.get("present", True)), "containerId": container_id or None,
            "state": str(row.get("state") or "unknown")[:32], "startedAt": str(started) if started else None,
        }
    return output


def compare_snapshots(before, after, strict_services):
    strict = set(strict_services)
    rows = []
    for service in sorted(set(before) | set(after)):
        old = before.get(service) or {}
        new = after.get(service) or {}
        same_container = old.get("present") == new.get("present") and old.get("containerId") == new.get("containerId")
        running_continuity = not (
            old.get("state") == "running" and new.get("state") == "running" and old.get("startedAt") != new.get("startedAt")
        )
        strict_ready = service not in strict or (
            old.get("state") == "running" and new.get("state") == "running" and old.get("startedAt") == new.get("startedAt")
        )
        rows.append({
            "service": service, "strict": service in strict, "sameContainer": same_container,
            "runningProcessUnrestarted": running_continuity, "strictContinuity": strict_ready,
            "before": old, "after": new,
        })
    return {
        "rows": rows,
        "containersUnrecreated": all(row["sameContainer"] for row in rows),
        "runningProcessesUnrestarted": all(row["runningProcessUnrestarted"] for row in rows),
        "strictServicesContinuous": all(row["strictContinuity"] for row in rows),
    }


def signed_document(receipt, secret, generated_at=None):
    body = dict(receipt)
    body.pop("receiptSha256", None)
    body["receiptSha256"] = digest(body)
    payload = {
        "schemaVersion": SIGNED_SCHEMA, "generatedAt": iso(generated_at),
        "signatureAlgorithm": "hmac-sha256", "signingKeyFingerprint": hashlib.sha256(secret).hexdigest(),
        "receipt": body,
    }
    signature = hmac.new(secret, canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return {**payload, "signature": signature}


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get("schemaVersion") != RECEIPT_SCHEMA or not RECEIPT_PATTERN.fullmatch(str(receipt.get("id") or "")):
        raise ValueError("deployment assurance receipt is invalid")
    if not WINDOW_PATTERN.fullmatch(str(receipt.get("windowId") or "")) or not COMMIT_PATTERN.fullmatch(str(receipt.get("commit") or "")):
        raise ValueError("deployment assurance receipt window or commit is invalid")
    if not 10 <= len(str(receipt.get("reason") or "")) <= 1000 or not str(receipt.get("principalId") or ""):
        raise ValueError("deployment assurance receipt reason or principal is invalid")
    if epoch(receipt.get("completedAt")) < epoch(receipt.get("startedAt")):
        raise ValueError("deployment assurance receipt times are invalid")
    files = receipt.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("deployment assurance receipt file manifest is invalid")
    seen = set()
    normalized_files = []
    total = 0
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise ValueError("deployment assurance receipt file row is invalid")
        path = safe_relative_path(row["path"])
        if path in seen or not HASH_PATTERN.fullmatch(str(row["sha256"] or "")):
            raise ValueError("deployment assurance receipt file path or digest is invalid")
        seen.add(path)
        size = row["bytes"]
        if not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("deployment assurance receipt file size is invalid")
        total += size
        normalized_files.append({"path": path, "sha256": row["sha256"], "bytes": size})
    if total > MAX_TOTAL_BYTES or files != sorted(normalized_files, key=lambda item: item["path"]):
        raise ValueError("deployment assurance receipt file manifest order or size is invalid")
    if not hmac.compare_digest(str(receipt.get("manifestSha256") or ""), digest(normalized_files)):
        raise ValueError("deployment assurance receipt manifest digest is invalid")
    protected = receipt.get("protectedServices")
    strict = receipt.get("strictServices")
    if not isinstance(protected, list) or not protected or protected != sorted(set(protected)):
        raise ValueError("deployment assurance protected services are invalid")
    if not isinstance(strict, list) or strict != sorted(set(strict)) or any(service not in protected for service in strict):
        raise ValueError("deployment assurance strict services are invalid")
    continuity = receipt.get("containerContinuity") or {}
    rows = continuity.get("rows")
    if not isinstance(rows, list) or [row.get("service") for row in rows] != protected:
        raise ValueError("deployment assurance container continuity rows are invalid")
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(key), bool) for key in ("strict", "sameContainer", "runningProcessUnrestarted", "strictContinuity")):
            raise ValueError("deployment assurance container continuity proof is invalid")
    aggregates = {
        "containersUnrecreated": all(row["sameContainer"] for row in rows),
        "runningProcessesUnrestarted": all(row["runningProcessUnrestarted"] for row in rows),
        "strictServicesContinuous": all(row["strictContinuity"] for row in rows),
    }
    if any(continuity.get(key) is not value for key, value in aggregates.items()):
        raise ValueError("deployment assurance container continuity aggregates are invalid")
    invariant_keys = {
        "sourceManifestMatches", "protectedContainersUnrecreated", "runningMapProcessesUnrestarted",
        "strictMapServicesContinuous", "desiredStateAttested", "readinessCurrent", "sloHealthy",
        "changeIntegrity", "prometheusReadiness", "adminHealthy", "backupVerified",
    }
    invariants = receipt.get("invariants")
    if not isinstance(invariants, dict) or set(invariants) != invariant_keys or any(not isinstance(value, bool) for value in invariants.values()):
        raise ValueError("deployment assurance invariant set is invalid")
    if invariants["protectedContainersUnrecreated"] is not aggregates["containersUnrecreated"] or invariants["runningMapProcessesUnrestarted"] is not aggregates["runningProcessesUnrestarted"] or invariants["strictMapServicesContinuous"] is not aggregates["strictServicesContinuous"]:
        raise ValueError("deployment assurance invariant continuity does not match proof")
    health = receipt.get("health")
    health_keys = invariant_keys - {"sourceManifestMatches", "protectedContainersUnrecreated", "runningMapProcessesUnrestarted", "strictMapServicesContinuous"}
    if not isinstance(health, dict) or set(health) != health_keys or any(health[key] is not invariants[key] for key in health_keys):
        raise ValueError("deployment assurance health does not match invariants")
    if bool((receipt.get("backup") or {}).get("verified")) is not invariants["backupVerified"] or not bool((receipt.get("preChangeBackup") or {}).get("verified")):
        raise ValueError("deployment assurance recovery backups are invalid")
    source_archive = receipt.get("preChangeSourceArchive") or {}
    if source_archive.get("verified") is not True or not HASH_PATTERN.fullmatch(str(source_archive.get("sha256") or "")) or not str(source_archive.get("path") or ""):
        raise ValueError("deployment assurance source rollback archive is invalid")
    if bool(receipt.get("ready")) is not all(invariants.values()):
        raise ValueError("deployment assurance readiness does not match invariants")
    if receipt.get("recoveryExecuted") is not False or receipt.get("gameMutationExecuted") is not False:
        raise ValueError("deployment assurance receipt claims a recovery or game mutation")
    return receipt


def verify_signed_document(document, secret):
    try:
        expected_keys = {"schemaVersion", "generatedAt", "signatureAlgorithm", "signingKeyFingerprint", "receipt", "signature"}
        if not isinstance(document, dict) or set(document) != expected_keys or document.get("schemaVersion") != SIGNED_SCHEMA:
            raise ValueError("deployment assurance signed fields or schema are invalid")
        if document.get("signatureAlgorithm") != "hmac-sha256":
            raise ValueError("deployment assurance signature algorithm is invalid")
        epoch(document.get("generatedAt"))
        receipt = validate_receipt(document.get("receipt"))
        actual_receipt = str(receipt.get("receiptSha256") or "")
        unsigned_receipt = {key: value for key, value in receipt.items() if key != "receiptSha256"}
        expected_receipt = digest(unsigned_receipt)
        if not HASH_PATTERN.fullmatch(actual_receipt) or not hmac.compare_digest(actual_receipt, expected_receipt):
            raise ValueError("deployment assurance nested receipt digest is invalid")
        fingerprint = hashlib.sha256(secret).hexdigest()
        if not hmac.compare_digest(str(document.get("signingKeyFingerprint") or ""), fingerprint):
            raise ValueError("deployment assurance signing key fingerprint does not match")
        signature = str(document.get("signature") or "")
        payload = {key: value for key, value in document.items() if key != "signature"}
        expected = hmac.new(secret, canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()
        valid = bool(HASH_PATTERN.fullmatch(signature)) and hmac.compare_digest(signature, expected)
        return {
            "ok": valid, "signatureValid": valid, "receiptValid": True,
            "receiptId": receipt["id"], "ready": bool(receipt.get("ready")),
            "commit": receipt.get("commit"), "receiptSha256": actual_receipt,
            **({} if valid else {"error": "deployment assurance HMAC is invalid"}),
        }
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"ok": False, "signatureValid": False, "receiptValid": False, "error": str(exc)}


class Store:
    def __init__(self, root, evidence_root, workspace, secret, owner_uid=None, owner_gid=None):
        self.root = pathlib.Path(root)
        self.evidence_root = pathlib.Path(evidence_root)
        self.workspace = pathlib.Path(workspace)
        self.secret = bytes(secret)
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None

    def _secure_owner(self, path):
        os.chmod(path, 0o700 if pathlib.Path(path).is_dir() else 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None:
            os.chown(path, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)

    def initialize(self):
        for path in (self.root, self.evidence_root):
            path.mkdir(parents=True, exist_ok=True)
            self._secure_owner(path)
        for path in self.root.glob("deployment-window-*.json"):
            if path.is_file() and not path.is_symlink():
                self._secure_owner(path)
        for path in self.evidence_root.glob("deployment-assurance-*.signed.json"):
            if path.is_file() and not path.is_symlink():
                self._secure_owner(path)

    def _window_path(self, window_id):
        if not WINDOW_PATTERN.fullmatch(str(window_id or "")):
            raise ValueError("deployment assurance window id is invalid")
        return self.root / f"{window_id}.json"

    def _write(self, path, document):
        self.initialize()
        temporary = path.with_suffix(path.suffix + ".tmp-" + secrets.token_hex(8))
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            self._secure_owner(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _sign_window(self, window):
        payload = {
            "schemaVersion": WINDOW_SCHEMA, "signingKeyFingerprint": hashlib.sha256(self.secret).hexdigest(),
            "window": window,
        }
        return {**payload, "signature": hmac.new(self.secret, canonical(payload).encode(), hashlib.sha256).hexdigest()}

    def _load_window(self, window_id):
        path = self._window_path(window_id)
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"schemaVersion", "signingKeyFingerprint", "window", "signature"} or document.get("schemaVersion") != WINDOW_SCHEMA:
            raise ValueError("deployment assurance window state is invalid")
        payload = {key: value for key, value in document.items() if key != "signature"}
        expected = hmac.new(self.secret, canonical(payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(document.get("signature") or ""), expected):
            raise ValueError("deployment assurance window HMAC is invalid")
        if not hmac.compare_digest(str(document.get("signingKeyFingerprint") or ""), hashlib.sha256(self.secret).hexdigest()):
            raise ValueError("deployment assurance window key fingerprint does not match")
        window = document.get("window")
        if not isinstance(window, dict) or window.get("id") != window_id:
            raise ValueError("deployment assurance window identity is invalid")
        return window

    def start(self, *, commit, reason, manifest, principal_id, snapshot, protected_services, strict_services, recovery_backup, source_rollback, workspace_matches=True, now=None):
        commit = str(commit or "").strip().lower()
        if not COMMIT_PATTERN.fullmatch(commit):
            raise ValueError("deployment assurance commit must be an exact 40- or 64-hex id")
        reason = str(reason or "").strip()
        if not 10 <= len(reason) <= 1000:
            raise ValueError("deployment assurance reason must contain 10..1000 characters")
        protected = sorted(set(str(value) for value in protected_services))
        strict = sorted(set(str(value) for value in strict_services))
        if not protected or any(value not in protected for value in strict):
            raise ValueError("deployment assurance protected/strict services are invalid")
        if not isinstance(recovery_backup, dict) or not recovery_backup.get("ok") or not str(recovery_backup.get("path") or ""):
            raise ValueError("deployment assurance requires a verified pre-change recovery backup")
        if not isinstance(source_rollback, dict) or not source_rollback.get("verified") or not HASH_PATTERN.fullmatch(str(source_rollback.get("sha256") or "")) or not str(source_rollback.get("path") or ""):
            raise ValueError("deployment assurance requires a verified pre-change source rollback archive")
        if workspace_matches:
            checked = normalize_manifest(self.workspace, manifest)
            files = [{key: row[key] for key in ("path", "sha256", "bytes")} for row in checked]
        else:
            files = validate_manifest_rows(manifest, require_bytes=True)
        current = float(time.time() if now is None else now)
        window_id = "deployment-window-" + secrets.token_hex(16)
        window = {
            "schemaVersion": 1, "id": window_id, "commit": commit, "reason": reason,
            "principalId": str(principal_id or "unknown")[:128], "startedAt": iso(current),
            "expiresAt": iso(current + MAX_WINDOW_SECONDS), "status": "open",
            "manifest": files,
            "manifestSha256": digest(files),
            "protectedServices": protected, "strictServices": strict,
            "preChangeBackup": {
                "path": str(recovery_backup.get("path"))[:500], "verified": True,
                "exitCode": int(recovery_backup.get("exitCode") or 0),
            },
            "preChangeSourceArchive": {
                "path": str(source_rollback.get("path"))[:500], "sha256": source_rollback["sha256"],
                "bytes": int(source_rollback.get("bytes") or 0), "verified": True,
            },
            "before": normalize_snapshot(snapshot, protected),
        }
        with LOCK:
            self._write(self._window_path(window_id), self._sign_window(window))
        return {key: value for key, value in window.items() if key != "manifest"} | {"files": len(files)}

    def finish(self, window_id, *, principal_id, snapshot, health, backup, now=None):
        current = float(time.time() if now is None else now)
        with LOCK:
            window = self._load_window(window_id)
            if window.get("status") != "open":
                raise ValueError("deployment assurance window is already finalized")
            if current > epoch(window.get("expiresAt")):
                raise ValueError("deployment assurance window expired")
            files = normalize_manifest(self.workspace, window["manifest"], require_match=False)
            after = normalize_snapshot(snapshot, window["protectedServices"])
            comparison = compare_snapshots(window["before"], after, window["strictServices"])
            required_health = {
                "desiredStateAttested", "readinessCurrent", "sloHealthy", "changeIntegrity",
                "prometheusReadiness", "adminHealthy", "backupVerified",
            }
            normalized_health = {key: bool((health or {}).get(key)) for key in sorted(required_health)}
            invariants = {
                "sourceManifestMatches": all(row["sha256"] == row["actualSha256"] and row["bytes"] == expected["bytes"] for row, expected in zip(files, window["manifest"])),
                "protectedContainersUnrecreated": comparison["containersUnrecreated"],
                "runningMapProcessesUnrestarted": comparison["runningProcessesUnrestarted"],
                "strictMapServicesContinuous": comparison["strictServicesContinuous"],
                **normalized_health,
            }
            ready = all(invariants.values())
            receipt_id = "deployment-assurance-" + secrets.token_hex(16)
            receipt = {
                "schemaVersion": RECEIPT_SCHEMA, "id": receipt_id, "windowId": window_id,
                "commit": window["commit"], "reason": window["reason"],
                "principalId": str(principal_id or "unknown")[:128],
                "startedAt": window["startedAt"], "completedAt": iso(current),
                "manifestSha256": window["manifestSha256"],
                "files": window["manifest"],
                "protectedServices": window["protectedServices"], "strictServices": window["strictServices"],
                "containerContinuity": comparison, "health": normalized_health,
                "backup": {
                    "path": str((backup or {}).get("path") or "")[:500],
                    "verified": bool((backup or {}).get("ok")),
                    "exitCode": int((backup or {}).get("exitCode") or 0),
                },
                "preChangeBackup": window["preChangeBackup"],
                "preChangeSourceArchive": window["preChangeSourceArchive"],
                "invariants": invariants, "ready": ready,
                "recoveryExecuted": False, "gameMutationExecuted": False,
            }
            document = signed_document(receipt, self.secret, generated_at=current)
            evidence_path = self.evidence_root / f"{receipt_id}.signed.json"
            self._write(evidence_path, document)
            window.update({"status": "completed", "completedAt": iso(current), "receiptId": receipt_id, "ready": ready})
            self._write(self._window_path(window_id), self._sign_window(window))
        return {"document": document, "evidencePath": str(evidence_path), "verification": verify_signed_document(document, self.secret)}

    def cancel(self, window_id, *, principal_id, reason, now=None):
        reason = str(reason or "").strip()
        if not 5 <= len(reason) <= 1000:
            raise ValueError("deployment assurance cancellation reason must contain 5..1000 characters")
        with LOCK:
            window = self._load_window(window_id)
            if window.get("status") != "open":
                raise ValueError("deployment assurance window is already finalized")
            window.update({"status": "cancelled", "cancelledAt": iso(now), "cancelledBy": str(principal_id or "unknown")[:128], "cancellationReason": reason})
            self._write(self._window_path(window_id), self._sign_window(window))
        return {"ok": True, "id": window_id, "status": "cancelled"}

    def status(self, now=None):
        self.initialize()
        current = float(time.time() if now is None else now)
        windows = []
        for path in sorted(self.root.glob("deployment-window-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:200]:
            try:
                row = self._load_window(path.stem)
                windows.append({key: row.get(key) for key in ("id", "commit", "reason", "manifestSha256", "principalId", "startedAt", "expiresAt", "status", "completedAt", "receiptId", "ready")})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                windows.append({"id": path.stem, "status": "invalid", "error": str(exc)})
        receipts = []
        for path in sorted(self.evidence_root.glob("deployment-assurance-*.signed.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:200]:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                verification = verify_signed_document(document, self.secret)
                receipt = document.get("receipt") or {}
                receipts.append({
                    "id": receipt.get("id"), "commit": receipt.get("commit"), "reason": receipt.get("reason"),
                    "startedAt": receipt.get("startedAt"), "completedAt": receipt.get("completedAt"),
                    "ready": bool(receipt.get("ready")), "invariants": receipt.get("invariants") or {},
                    "backup": receipt.get("backup") or {}, "receiptSha256": receipt.get("receiptSha256"),
                    "file": path.name, "verification": verification,
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                receipts.append({"id": path.stem, "ready": False, "file": path.name, "verification": {"ok": False, "error": str(exc)}})
        open_windows = [row for row in windows if row.get("status") == "open"]
        overdue = [row for row in open_windows if epoch(row.get("expiresAt")) <= current]
        latest = receipts[0] if receipts else None
        latest_ready = bool(latest and latest.get("ready") and (latest.get("verification") or {}).get("ok"))
        return {
            "ok": all((row.get("verification") or {}).get("ok") for row in receipts) and not any(row.get("status") == "invalid" for row in windows),
            "state": "invalid" if any(row.get("status") == "invalid" for row in windows) or any(not (row.get("verification") or {}).get("ok") for row in receipts) else "overdue" if overdue else "active",
            "latest": latest, "receipts": receipts, "windows": windows,
            "openWindows": open_windows, "overdueWindows": overdue, "latestReady": latest_ready,
        }

    def prometheus(self, now=None):
        status = self.status(now=now)
        latest = status.get("latest") or {}
        timestamp = epoch(latest["completedAt"]) if latest.get("completedAt") else "NaN"
        return "\n".join([
            f"dash_deployment_assurance_collector_up {1 if status['ok'] else 0}",
            f"dash_deployment_assurance_latest_ready {1 if status['latestReady'] else 0}",
            f"dash_deployment_assurance_last_completion_timestamp_seconds {timestamp}",
            f"dash_deployment_assurance_open_windows {len(status['openWindows'])}",
            f"dash_deployment_assurance_overdue_windows {len(status['overdueWindows'])}",
        ]) + "\n"
