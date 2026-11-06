#!/usr/bin/env python3
"""Tamper-evident, candidate-bound game-update readiness receipts."""

import datetime
import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import threading
import time


LEGACY_SCHEMA = "dune-update-readiness/v1"
SCHEMA = "dune-update-readiness/v2"
SCHEMAS = frozenset((LEGACY_SCHEMA, SCHEMA))
RECEIPT_SCHEMA = 2
RECEIPT_PATTERN = re.compile(r"^update-readiness-[0-9a-f]{32}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BUILD_PATTERN = re.compile(r"^[0-9]{1,32}$")
LOCK = threading.RLock()
REQUIRED_CHECKS = (
    "backupVerified",
    "changeIntegrity",
    "composeValid",
    "coriolisSafe",
    "deploymentAssuranceReady",
    "desiredStateAttested",
    "packageComplete",
    "packageIdentified",
    "postStartHooksReady",
    "readinessCurrent",
    "rabbitmqRestoreProofReady",
    "restoreProofReady",
    "sloHealthy",
    "steamSettled",
)
LEGACY_REQUIRED_CHECKS = tuple(key for key in REQUIRED_CHECKS if key != "rabbitmqRestoreProofReady")
DOCUMENT_SCHEMA_BY_RECEIPT = {1: LEGACY_SCHEMA, RECEIPT_SCHEMA: SCHEMA}
REQUIRED_CHECKS_BY_RECEIPT = {1: LEGACY_REQUIRED_CHECKS, RECEIPT_SCHEMA: REQUIRED_CHECKS}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def iso(value=None):
    stamp = float(time.time() if value is None else value)
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(value):
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(text).timestamp()


def bounded_metric_seconds(value):
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return 0.0
    return seconds if math.isfinite(seconds) and 0 <= seconds <= 86400 else 0.0


def normalize_candidate(candidate):
    if not isinstance(candidate, dict):
        raise ValueError("update candidate must be an object")
    tag = str(candidate.get("imageTag") or "").strip()
    current = str(candidate.get("currentImageTag") or "").strip()
    status = str(candidate.get("status") or "").strip()
    if not TAG_PATTERN.fullmatch(tag) or not TAG_PATTERN.fullmatch(current):
        raise ValueError("update candidate image tags are invalid")
    if status not in {"current", "update-available", "reload-required", "unknown"}:
        raise ValueError("update candidate status is invalid")
    normalized = {"imageTag": tag, "currentImageTag": current, "status": status}
    for key in ("installedBuildId", "targetBuildId", "loadedBuildId"):
        value = str(candidate.get(key) or "").strip()
        if value and not BUILD_PATTERN.fullmatch(value):
            raise ValueError(f"update candidate {key} is invalid")
        normalized[key] = value or None
    runtime = candidate.get("runtimeSecurity")
    if runtime is not None:
        if not isinstance(runtime, dict) or set(runtime) != {"imageId", "vendorImageId", "approvalSha256"}:
            raise ValueError("update candidate runtime security identity is invalid")
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", str(runtime[key])) for key in ("imageId", "vendorImageId")) or not HASH_PATTERN.fullmatch(str(runtime["approvalSha256"])):
            raise ValueError("update candidate runtime security digest is invalid")
        normalized["runtimeSecurity"] = dict(runtime)
    normalized["updateRequired"] = status in {"update-available", "reload-required"}
    normalized["fingerprint"] = hashlib.sha256(canonical({key: value for key, value in normalized.items() if key != "fingerprint"}).encode()).hexdigest()
    return normalized


def normalize_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("update readiness snapshot must be an object")
    candidate = normalize_candidate(snapshot.get("candidate"))
    checks = {key: bool((snapshot.get("checks") or {}).get(key)) for key in REQUIRED_CHECKS}
    online_players = int(snapshot.get("onlinePlayers") or 0)
    if online_players < 0 or online_players > 100000:
        raise ValueError("update readiness online-player count is invalid")
    details = snapshot.get("details") or {}
    if not isinstance(details, dict):
        raise ValueError("update readiness details must be an object")
    bounded_details = json.loads(canonical(details))
    if len(canonical(bounded_details).encode()) > 128 * 1024:
        raise ValueError("update readiness details exceed 128 KiB")
    scheduled_ready = all(checks.values())
    return {
        "candidate": candidate,
        "checks": checks,
        "failedChecks": [key for key, value in checks.items() if not value],
        "scheduledReady": scheduled_ready,
        "immediateReady": scheduled_ready and online_players == 0,
        "onlinePlayers": online_players,
        "details": bounded_details,
    }


def receipt_hash(receipt):
    return hashlib.sha256(canonical({key: value for key, value in receipt.items() if key != "receiptSha256"}).encode()).hexdigest()


def signed_document(receipt, secret, generated_at=None):
    receipt = json.loads(canonical(receipt))
    schema = DOCUMENT_SCHEMA_BY_RECEIPT.get(receipt.get("schemaVersion"))
    if not schema:
        raise ValueError("update readiness receipt schema is invalid")
    receipt["receiptSha256"] = receipt_hash(receipt)
    payload = {
        "schemaVersion": schema,
        "generatedAt": iso(generated_at),
        "signingKeyFingerprint": hashlib.sha256(secret).hexdigest(),
        "receipt": receipt,
    }
    return {**payload, "signature": hmac.new(secret, canonical(payload).encode(), hashlib.sha256).hexdigest()}


def verify_signed_document(document, secret, now=None):
    try:
        if not isinstance(document, dict) or set(document) != {"schemaVersion", "generatedAt", "signingKeyFingerprint", "receipt", "signature"}:
            raise ValueError("update readiness signed document fields are invalid")
        if document.get("schemaVersion") not in SCHEMAS:
            raise ValueError("update readiness schema is invalid")
        payload = {key: value for key, value in document.items() if key != "signature"}
        expected_signature = hmac.new(secret, canonical(payload).encode(), hashlib.sha256).hexdigest()
        signature_valid = hmac.compare_digest(str(document.get("signature") or ""), expected_signature)
        if not signature_valid:
            raise ValueError("update readiness signature is invalid")
        if not hmac.compare_digest(str(document.get("signingKeyFingerprint") or ""), hashlib.sha256(secret).hexdigest()):
            raise ValueError("update readiness signing key fingerprint does not match")
        receipt = document.get("receipt")
        receipt_schema = receipt.get("schemaVersion") if isinstance(receipt, dict) else None
        required_checks = REQUIRED_CHECKS_BY_RECEIPT.get(receipt_schema)
        if (
            not isinstance(receipt, dict) or required_checks is None
            or document.get("schemaVersion") != DOCUMENT_SCHEMA_BY_RECEIPT.get(receipt_schema)
            or not RECEIPT_PATTERN.fullmatch(str(receipt.get("id") or ""))
        ):
            raise ValueError("update readiness receipt identity is invalid")
        expected_fields = {
            "schemaVersion", "id", "candidate", "checks", "scheduledReady", "immediateReady",
            "onlinePlayers", "details", "principalId", "sourceCommit", "certifiedAt", "expiresAt",
            "updateExecuted", "gameMutationExecuted", "receiptSha256",
        }
        if set(receipt) != expected_fields:
            raise ValueError("update readiness receipt fields are invalid")
        candidate = normalize_candidate(receipt.get("candidate"))
        if candidate != receipt.get("candidate"):
            raise ValueError("update readiness candidate is not canonical")
        checks = receipt.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(required_checks) or any(type(value) is not bool for value in checks.values()):
            raise ValueError("update readiness checks are invalid")
        online_players = int(receipt.get("onlinePlayers") or 0)
        if online_players < 0 or online_players > 100000:
            raise ValueError("update readiness receipt player count is invalid")
        details = receipt.get("details")
        if not isinstance(details, dict) or len(canonical(details).encode()) > 128 * 1024:
            raise ValueError("update readiness receipt details are invalid")
        principal_id = str(receipt.get("principalId") or "")
        if not 1 <= len(principal_id) <= 128:
            raise ValueError("update readiness receipt principal is invalid")
        source_commit = receipt.get("sourceCommit")
        if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(source_commit)):
            raise ValueError("update readiness receipt source commit is invalid")
        certified = epoch(receipt.get("certifiedAt"))
        expires = epoch(receipt.get("expiresAt"))
        if not certified < expires <= certified + 86400:
            raise ValueError("update readiness receipt lifetime is invalid")
        scheduled = all(checks.values())
        immediate = scheduled and online_players == 0
        if receipt.get("scheduledReady") is not scheduled or receipt.get("immediateReady") is not immediate:
            raise ValueError("update readiness verdict does not match its checks")
        if receipt.get("gameMutationExecuted") is not False or receipt.get("updateExecuted") is not False:
            raise ValueError("update readiness receipt cannot claim execution")
        expected_hash = receipt_hash(receipt)
        receipt_valid = HASH_PATTERN.fullmatch(str(receipt.get("receiptSha256") or "")) and hmac.compare_digest(receipt["receiptSha256"], expected_hash)
        if not receipt_valid:
            raise ValueError("update readiness receipt digest is invalid")
        expired = float(time.time() if now is None else now) > expires
        return {
            "ok": True, "signatureValid": True, "receiptValid": True,
            "receiptId": receipt["id"], "receiptSha256": receipt["receiptSha256"],
            "candidateFingerprint": candidate["fingerprint"], "scheduledReady": scheduled,
            "immediateReady": immediate, "expired": expired,
        }
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"ok": False, "signatureValid": False, "receiptValid": False, "error": str(exc)}


class Store:
    def __init__(self, evidence_root, secret, ttl_seconds=3600, owner_uid=None, owner_gid=None):
        self.evidence_root = pathlib.Path(evidence_root)
        self.secret = bytes(secret)
        self.ttl_seconds = max(300, min(int(ttl_seconds), 86400))
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None

    def initialize(self):
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.evidence_root, 0o700)
        if os.geteuid() == 0 and self.owner_uid is not None:
            os.chown(self.evidence_root, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)

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
            os.chmod(path, 0o600)
            if os.geteuid() == 0 and self.owner_uid is not None:
                os.chown(path, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)
        finally:
            temporary.unlink(missing_ok=True)

    def certify(self, snapshot, principal_id, source_commit=None, now=None):
        current = float(time.time() if now is None else now)
        evaluated = normalize_snapshot(snapshot)
        if not evaluated["scheduledReady"]:
            raise ValueError("update readiness cannot certify failed checks: " + ", ".join(evaluated["failedChecks"]))
        source_commit = str(source_commit or "").strip().lower()
        if source_commit and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit):
            raise ValueError("update readiness source commit is invalid")
        receipt_id = "update-readiness-" + secrets.token_hex(16)
        receipt = {
            "schemaVersion": RECEIPT_SCHEMA, "id": receipt_id,
            "candidate": evaluated["candidate"], "checks": evaluated["checks"],
            "scheduledReady": True, "immediateReady": evaluated["immediateReady"],
            "onlinePlayers": evaluated["onlinePlayers"], "details": evaluated["details"],
            "principalId": str(principal_id or "unknown")[:128], "sourceCommit": source_commit or None,
            "certifiedAt": iso(current), "expiresAt": iso(current + self.ttl_seconds),
            "updateExecuted": False, "gameMutationExecuted": False,
        }
        document = signed_document(receipt, self.secret, generated_at=current)
        path = self.evidence_root / f"{receipt_id}.signed.json"
        with LOCK:
            self._write(path, document)
        return {"document": document, "evidencePath": str(path), "verification": verify_signed_document(document, self.secret, now=current)}

    def status(self, snapshot, now=None):
        evaluated = normalize_snapshot(snapshot)
        receipts = []
        self.initialize()
        for path in sorted(self.evidence_root.glob("update-readiness-*.signed.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:200]:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                verification = verify_signed_document(document, self.secret, now=now)
                receipt = document.get("receipt") or {}
                receipts.append({
                    "id": receipt.get("id"), "candidate": receipt.get("candidate"),
                    "scheduledReady": bool(receipt.get("scheduledReady")), "immediateReady": bool(receipt.get("immediateReady")),
                    "onlinePlayers": receipt.get("onlinePlayers"), "certifiedAt": receipt.get("certifiedAt"),
                    "expiresAt": receipt.get("expiresAt"), "receiptSha256": receipt.get("receiptSha256"),
                    "sourceCommit": receipt.get("sourceCommit"), "file": path.name, "verification": verification,
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                receipts.append({"id": path.stem, "file": path.name, "scheduledReady": False, "verification": {"ok": False, "error": str(exc)}})
        latest = receipts[0] if receipts else None
        current_receipt = bool(
            latest and (latest.get("verification") or {}).get("ok") and not (latest.get("verification") or {}).get("expired")
            and ((latest.get("candidate") or {}).get("fingerprint") == evaluated["candidate"]["fingerprint"])
            and latest.get("scheduledReady")
        )
        return {
            "ok": all((row.get("verification") or {}).get("ok") for row in receipts),
            "evaluation": evaluated, "latest": latest, "receipts": receipts,
            "currentReceiptReady": current_receipt,
            "applyReady": current_receipt and evaluated["scheduledReady"],
        }

    def prometheus(self, snapshot, now=None):
        status = self.status(snapshot, now=now)
        evaluation = status["evaluation"]
        latest = status.get("latest") or {}
        timestamp = epoch(latest["certifiedAt"]) if latest.get("certifiedAt") else "NaN"
        details = evaluation.get("details") or {}
        collection_seconds = bounded_metric_seconds((details.get("collection") or {}).get("durationMs"))
        package_seconds = bounded_metric_seconds((((details.get("package") or {}).get("inspection") or {}).get("durationMs")))
        return "\n".join([
            f"dash_update_readiness_collector_up {1 if status['ok'] else 0}",
            f"dash_update_readiness_scheduled_ready {1 if evaluation['scheduledReady'] else 0}",
            f"dash_update_readiness_immediate_ready {1 if evaluation['immediateReady'] else 0}",
            f"dash_update_readiness_candidate_update_required {1 if evaluation['candidate']['updateRequired'] else 0}",
            f"dash_update_readiness_receipt_current {1 if status['currentReceiptReady'] else 0}",
            f"dash_update_readiness_online_players {evaluation['onlinePlayers']}",
            f"dash_update_readiness_collection_duration_seconds {collection_seconds:.6f}",
            f"dash_update_readiness_package_inspection_duration_seconds {package_seconds:.6f}",
            f"dash_update_readiness_last_certification_timestamp_seconds {timestamp}",
        ]) + "\n"
