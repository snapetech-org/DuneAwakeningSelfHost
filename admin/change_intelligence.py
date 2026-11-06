#!/usr/bin/env python3
"""Tamper-evident operational change timeline and incident correlation."""

from __future__ import annotations

import datetime as _datetime
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import uuid


KINDS = {"change", "incident-open", "incident-resolved", "evidence", "observation"}
IMPACTS = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
RESPONSE_STEP_KINDS = {"evidence", "diagnostic", "review", "recovery"}
RESPONSE_PREDICATES = {"ledger-verified", "incident-resolved", "candidate-review", "followup-review", "always-pending"}
SENSITIVE_KEY = re.compile(r"(?:password|passwd|secret|token|cookie|authorization|private.?key|credential)", re.I)
IDENTITY_KEY = re.compile(r"(?:^|_)(?:account|fls|player|character|peer|client|target|subject)(?:_|$)", re.I)
ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
URL_CREDENTIALS = re.compile(r"(https?://)[^/@:\s]+:[^/@\s]+@", re.I)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _iso(epoch):
    return _datetime.datetime.fromtimestamp(float(epoch), _datetime.timezone.utc).isoformat()


def _epoch(value=None):
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _datetime.datetime.fromisoformat(text).timestamp()


def _bounded_int(value, name, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_policy(path):
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if raw.get("schemaVersion") != 1:
        raise ValueError("change-intelligence policy schemaVersion must be 1")
    rules = raw.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= 256:
        raise ValueError("change-intelligence rules must contain 1..256 entries")
    normalized = []
    for row in rules:
        if not isinstance(row, dict):
            raise ValueError("each change-intelligence rule must be an object")
        pattern = str(row.get("pattern") or "")
        kind = str(row.get("kind") or "")
        category = str(row.get("category") or "")
        impact = str(row.get("impact") or "")
        if not pattern or len(pattern) > 128 or kind not in KINDS or impact not in IMPACTS or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", category):
            raise ValueError("invalid change-intelligence rule")
        normalized.append({"pattern": pattern, "kind": kind, "category": category, "impact": impact})
    response = raw.get("response")
    if not isinstance(response, dict) or response.get("schemaVersion") != 1:
        raise ValueError("change-intelligence response policy must be a schemaVersion 1 object")

    def normalize_steps(steps, location):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
            raise ValueError(f"{location} must contain 1..32 response steps")
        seen = set()
        output = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError(f"{location} steps must be objects")
            step_id = str(step.get("id") or "")
            title = str(step.get("title") or "")
            description = str(step.get("description") or "")
            kind = str(step.get("kind") or "")
            predicate = str(step.get("predicate") or "")
            surface = str(step.get("surface") or "")
            capability = str(step.get("requiredCapability") or "read")
            gate = str(step.get("featureGate") or "")
            confirmation = str(step.get("confirmation") or "")
            command_id = str(step.get("commandId") or "")
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", step_id) or step_id in seen:
                raise ValueError(f"{location} response step id is invalid or duplicate")
            seen.add(step_id)
            if not 3 <= len(title) <= 128 or not 10 <= len(description) <= 1000:
                raise ValueError(f"{location} response step text is invalid")
            if kind not in RESPONSE_STEP_KINDS or predicate not in RESPONSE_PREDICATES:
                raise ValueError(f"{location} response step kind or predicate is invalid")
            if not re.fullmatch(r"[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)?", surface):
                raise ValueError(f"{location} response surface is invalid")
            if not re.fullmatch(r"(?:\*|[a-z][a-z0-9.*-]{1,63})", capability):
                raise ValueError(f"{location} response capability is invalid")
            if gate and not re.fullmatch(r"DUNE_[A-Z0-9_]{3,120}", gate):
                raise ValueError(f"{location} response feature gate is invalid")
            if confirmation and (len(confirmation) > 128 or not re.fullmatch(r"[A-Z0-9][A-Z0-9 _-]+", confirmation)):
                raise ValueError(f"{location} response confirmation is invalid")
            if command_id and not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", command_id):
                raise ValueError(f"{location} response command id is invalid")
            mutation = bool(step.get("mutation", False))
            if mutation != (kind == "recovery") or (command_id and (kind != "diagnostic" or mutation)):
                raise ValueError(f"{location} response mutation/command contract is invalid")
            output.append({
                "id": step_id, "title": title, "description": description,
                "kind": kind, "predicate": predicate, "surface": surface,
                "requiredCapability": capability, "mutation": mutation,
                **({"featureGate": gate} if gate else {}),
                **({"confirmation": confirmation} if confirmation else {}),
                **({"commandId": command_id} if command_id else {}),
            })
        return output

    common_steps = normalize_steps(response.get("commonSteps"), "response.commonSteps")
    runbooks = response.get("runbooks")
    if not isinstance(runbooks, list) or not 1 <= len(runbooks) <= 64:
        raise ValueError("change-intelligence response.runbooks must contain 1..64 entries")
    normalized_runbooks = []
    seen_runbooks = set()
    seen_matches = set()
    for index, runbook in enumerate(runbooks):
        if not isinstance(runbook, dict):
            raise ValueError("change-intelligence response runbooks must be objects")
        runbook_id = str(runbook.get("id") or "")
        title = str(runbook.get("title") or "")
        match = runbook.get("match") or {}
        prefix = str(match.get("incidentPrefix") or "*")
        objective = str(match.get("objectivePattern") or "*")
        action = str(match.get("actionPattern") or "*")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", runbook_id) or runbook_id in seen_runbooks:
            raise ValueError("change-intelligence response runbook id is invalid or duplicate")
        seen_runbooks.add(runbook_id)
        if not 3 <= len(title) <= 128 or prefix not in {"slo:", "desired:", "event:", "*"}:
            raise ValueError("change-intelligence response runbook title or prefix is invalid")
        for pattern in (objective, action):
            if not pattern or len(pattern) > 128 or not re.fullmatch(r"[A-Za-z0-9_.*:/-]+", pattern):
                raise ValueError("change-intelligence response runbook pattern is invalid")
        match_document = {"incidentPrefix": prefix, "objectivePattern": objective, "actionPattern": action}
        match_key = (prefix, objective, action)
        if match_key in seen_matches:
            raise ValueError("change-intelligence response runbook match is duplicate")
        seen_matches.add(match_key)
        runbook_steps = normalize_steps(runbook.get("steps"), f"response.runbooks[{index}].steps")
        if {step["id"] for step in common_steps} & {step["id"] for step in runbook_steps}:
            raise ValueError("change-intelligence response common/runbook step ids overlap")
        normalized_runbooks.append({
            "id": runbook_id, "title": title, "match": match_document, "steps": runbook_steps,
        })
    if normalized_runbooks[-1]["match"] != {"incidentPrefix": "*", "objectivePattern": "*", "actionPattern": "*"}:
        raise ValueError("last change-intelligence response runbook must be the generic fallback")
    normalized_response = {"schemaVersion": 1, "commonSteps": common_steps, "runbooks": normalized_runbooks}
    normalized_response["policySha256"] = hashlib.sha256(_canonical(normalized_response).encode()).hexdigest()
    return {
        "schemaVersion": 1,
        "maxEvents": _bounded_int(raw.get("maxEvents", 1000000), "maxEvents", 1000, 10000000),
        "maxPayloadBytes": _bounded_int(raw.get("maxPayloadBytes", 32768), "maxPayloadBytes", 1024, 1048576),
        "correlationWindowBeforeSeconds": _bounded_int(raw.get("correlationWindowBeforeSeconds", 3600), "correlationWindowBeforeSeconds", 60, 86400),
        "correlationWindowAfterSeconds": _bounded_int(raw.get("correlationWindowAfterSeconds", 1800), "correlationWindowAfterSeconds", 60, 86400),
        "statusEventLimit": _bounded_int(raw.get("statusEventLimit", 200), "statusEventLimit", 10, 1000),
        "candidateLimit": _bounded_int(raw.get("candidateLimit", 20), "candidateLimit", 1, 100),
        "capsuleEvidenceLimit": _bounded_int(raw.get("capsuleEvidenceLimit", 200), "capsuleEvidenceLimit", 10, 1000),
        "historyImportLimit": _bounded_int(raw.get("historyImportLimit", 10000), "historyImportLimit", 0, 100000),
        "rules": normalized,
        "response": normalized_response,
    }


def verify_response_plan(plan):
    try:
        expected_keys = {"schemaVersion", "runbookId", "title", "incidentKey", "objectiveId", "incidentAction", "policySha256", "inputSha256", "state", "summary", "steps", "executesAutomatically", "causalityClaimed", "interpretation", "planSha256"}
        if not isinstance(plan, dict) or set(plan) != expected_keys or plan.get("schemaVersion") != 1:
            raise ValueError("response plan schema is invalid")
        validate_incident_key(plan.get("incidentKey"))
        if plan.get("executesAutomatically") is not False or plan.get("causalityClaimed") is not False:
            raise ValueError("response plan execution/causality contract is invalid")
        if plan.get("state") not in {"blocked", "requires-operator-review", "verified"}:
            raise ValueError("response plan state is invalid")
        for key in ("policySha256", "inputSha256", "planSha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(plan.get(key) or "")):
                raise ValueError(f"response plan {key} is invalid")
        steps = plan.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
            raise ValueError("response plan steps are invalid")
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or step.get("order") != index or step.get("kind") not in RESPONSE_STEP_KINDS or step.get("status") not in {"verified", "pending", "not-applicable", "blocked"}:
                raise ValueError("response plan step structure is invalid")
            if bool(step.get("mutation")) != (step.get("kind") == "recovery"):
                raise ValueError("response plan step mutation contract is invalid")
            expected_execution = "manual-gated" if step.get("mutation") else "read-only-catalog" if step.get("commandId") else "operator-review"
            if step.get("execution") != expected_execution or (step.get("mutation") and (not step.get("featureGate") or step.get("requiredCapability") == "read")):
                raise ValueError("response plan step execution contract is invalid")
        digest = str(plan.get("planSha256") or "")
        payload = {key: value for key, value in plan.items() if key != "planSha256"}
        valid = hmac.compare_digest(digest, hashlib.sha256(_canonical(payload).encode()).hexdigest())
        return {"ok": valid, "planSha256": digest, **({} if valid else {"error": "response plan digest does not match"})}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}


def compile_response_plan(capsule, policy, ledger=None):
    """Compile one deterministic, non-executing response plan from signed evidence."""
    if not isinstance(capsule, dict) or not isinstance(policy, dict):
        raise ValueError("capsule and policy are required for response planning")
    key = validate_incident_key(capsule.get("incidentKey"))
    opened = capsule.get("opened") or {}
    opened_data = opened.get("data") or {}
    objective = str(opened_data.get("objective_id") or opened_data.get("objectiveId") or "unknown")[:128]
    action = str(opened.get("action") or "")[:128]
    response = policy.get("response") or {}
    selected = None
    for runbook in response.get("runbooks") or []:
        match = runbook["match"]
        if match["incidentPrefix"] != "*" and not key.startswith(match["incidentPrefix"]):
            continue
        if not fnmatch.fnmatchcase(objective, match["objectivePattern"]):
            continue
        if not fnmatch.fnmatchcase(action, match["actionPattern"]):
            continue
        selected = runbook
        break
    if not selected:
        raise ValueError("response policy has no matching fallback runbook")
    candidates = capsule.get("candidateChanges") or []
    followup = capsule.get("followupEvidence") or []
    ledger = ledger or {}
    ledger_ok = ledger.get("sqlite") == "ok" and ledger.get("appendOnlyTriggers") is True and ledger.get("eventChainValid") is True

    def evaluate(step):
        predicate = step["predicate"]
        if predicate == "ledger-verified":
            return ("verified" if ledger_ok else "blocked", f"SQLite, append-only triggers, and HMAC chain {'verified' if ledger_ok else 'are not all verified'} at {int(ledger.get('eventCount') or 0)} events.")
        if predicate == "incident-resolved":
            resolved = capsule.get("status") == "resolved"
            return ("verified" if resolved else "pending", "The authoritative incident has a retained resolution event." if resolved else "The incident remains open in the retained evidence timeline.")
        if predicate == "candidate-review":
            return (("pending", f"Review {len(candidates)} ranked preceding change candidate(s); ranking is not causality.") if candidates else ("not-applicable", "No preceding recorded change met the bounded correlation policy."))
        if predicate == "followup-review":
            return (("pending", f"Review {len(followup)} bounded follow-up evidence event(s).") if followup else ("not-applicable", "No follow-up event falls inside the bounded evidence window."))
        return ("pending", "This operator step is intentionally never auto-completed by DASH.")

    steps = []
    for order, source in enumerate([*(response.get("commonSteps") or []), *selected["steps"]], 1):
        status, evidence = evaluate(source)
        steps.append({"order": order, **source, "status": status, "evidence": evidence, "execution": "manual-gated" if source["mutation"] else "read-only-catalog" if source.get("commandId") else "operator-review"})
    counts = {status: sum(1 for step in steps if step["status"] == status) for status in ("verified", "pending", "not-applicable", "blocked")}
    inputs = {
        "incidentKey": key, "openedEventId": opened.get("id"),
        "resolvedEventId": (capsule.get("resolved") or {}).get("id"),
        "candidateEventIds": [row.get("id") for row in candidates],
        "followupEventIds": [row.get("id") for row in followup],
        "responseDrillEventIds": [row.get("id") for row in capsule.get("responseDrills") or []],
        "readinessCertificationEventId": (capsule.get("readinessCertification") or {}).get("id"),
        "ledgerEventCount": ledger.get("eventCount"), "ledgerHeadSignature": ledger.get("lastEventSignature"),
    }
    plan = {
        "schemaVersion": 1, "runbookId": selected["id"], "title": selected["title"],
        "incidentKey": key, "objectiveId": objective, "incidentAction": action,
        "policySha256": response["policySha256"], "inputSha256": hashlib.sha256(_canonical(inputs).encode()).hexdigest(),
        "state": "blocked" if counts["blocked"] else "requires-operator-review" if counts["pending"] else "verified",
        "summary": {**counts, "mutationSteps": sum(1 for step in steps if step["mutation"])},
        "steps": steps, "executesAutomatically": False, "causalityClaimed": False,
        "interpretation": "This deterministic runbook organizes evidence and existing guarded surfaces; it does not identify a root cause or execute recovery.",
    }
    plan["planSha256"] = hashlib.sha256(_canonical(plan).encode()).hexdigest()
    return plan


def read_secret(path):
    path = pathlib.Path(path)
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 64:
        raise ValueError("change-intelligence HMAC secret must contain at least 64 encoded characters")
    if path.stat().st_mode & 0o077:
        raise PermissionError("change-intelligence HMAC secret must not be group/world accessible")
    return value.encode("utf-8")


def verify_signed_capsule(document, secret):
    """Verify a portable capsule without requiring its source database."""
    expected_keys = {"schemaVersion", "generatedAt", "incidentKey", "signatureAlgorithm", "signingKeyFingerprint", "ledger", "capsule", "signature"}
    try:
        if not isinstance(document, dict) or set(document) != expected_keys:
            raise ValueError("signed capsule fields are invalid")
        schema = document.get("schemaVersion")
        if schema not in {1, 2} or document.get("signatureAlgorithm") != "hmac-sha256":
            raise ValueError("signed capsule schema or algorithm is unsupported")
        key = validate_incident_key(document.get("incidentKey"))
        _epoch(document.get("generatedAt"))
        ledger = document.get("ledger")
        capsule = document.get("capsule")
        if not isinstance(ledger, dict) or not isinstance(capsule, dict):
            raise ValueError("signed capsule ledger and capsule must be objects")
        if capsule.get("incidentKey") != key or capsule.get("causalityClaimed") is not False:
            raise ValueError("signed capsule incident or causality contract is invalid")
        plan_verification = None
        if schema == 2:
            plan_verification = verify_response_plan(capsule.get("responsePlan"))
            if not plan_verification.get("ok"):
                raise ValueError("signed capsule response plan is invalid: " + str(plan_verification.get("error") or "digest mismatch"))
            if capsule["responsePlan"].get("incidentKey") != key:
                raise ValueError("signed capsule response plan incident does not match")
            drills = capsule.get("responseDrills") or []
            if not isinstance(drills, list):
                raise ValueError("signed capsule response drills must be a list")
            for drill in drills:
                verification = verify_readiness_receipt(drill)
                if not verification.get("ok"):
                    raise ValueError("signed capsule response drill receipt is invalid")
            certification = capsule.get("readinessCertification")
            if certification and not verify_readiness_receipt(certification).get("ok"):
                raise ValueError("signed capsule readiness certification receipt is invalid")
        elif "responsePlan" in capsule:
            raise ValueError("schema 1 signed capsule must not contain a response plan")
        if not isinstance(ledger.get("eventCount"), int) or ledger["eventCount"] < 1:
            raise ValueError("signed capsule eventCount is invalid")
        head = str(ledger.get("lastEventSignature") or "")
        signature = str(document.get("signature") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", head) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError("signed capsule signatures are invalid")
        fingerprint = hashlib.sha256(secret).hexdigest()
        if not hmac.compare_digest(str(document.get("signingKeyFingerprint") or ""), fingerprint):
            raise ValueError("signed capsule key fingerprint does not match")
        payload = {key: value for key, value in document.items() if key != "signature"}
        expected = hmac.new(secret, _canonical(payload).encode(), hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(signature, expected)
        return {
            "ok": valid, "schemaVersion": schema, "incidentKey": key,
            "signatureValid": valid, "responsePlanValid": True if schema == 2 else None,
            "readinessReceiptsValid": True if schema == 2 else None,
            "responsePlanSha256": plan_verification["planSha256"] if plan_verification else None,
            "legacyWithoutResponsePlan": schema == 1, "signingKeyFingerprint": fingerprint,
            "payloadSha256": hashlib.sha256(_canonical(payload).encode()).hexdigest(),
            **({} if valid else {"error": "signed capsule HMAC is invalid"}),
        }
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return {"ok": False, "signatureValid": False, "error": str(exc)}


def _hmac_value(secret, value):
    return hmac.new(secret, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_readiness_receipt(event):
    """Recompute the nested readiness receipt digest carried by an HMAC event."""
    try:
        if not isinstance(event, dict) or event.get("action") not in {"incident-response-drill", "incident-readiness-certification"}:
            raise ValueError("event is not a readiness receipt")
        data = event.get("data") or {}
        if event["action"] == "incident-response-drill":
            receipt = {
                "id": data.get("drill_id"), "incidentKey": event.get("incidentKey"), "runbookId": data.get("runbook_id"),
                "planSha256": data.get("plan_sha256"), "policySha256": data.get("policy_sha256"),
                "principalId": event.get("actor"), "diagnostics": data.get("diagnostics") or [],
                "recoveryContracts": data.get("recovery_contracts") or [],
                "diagnosticsReady": data.get("diagnostics_ready"), "recoveryContractsReady": data.get("recovery_contracts_ready"),
                "ready": bool(event.get("ok")), "recoveryExecuted": data.get("recovery_executed"),
                "gameMutationExecuted": data.get("game_mutation_executed"),
            }
        else:
            receipt = {
                "id": data.get("certification_id"), "policySha256": data.get("policy_sha256"),
                "principalId": event.get("actor"), "diagnostics": data.get("diagnostics") or [],
                "runbooks": data.get("runbooks") or [], "summary": data.get("summary") or {},
                "ready": bool(event.get("ok")), "recoveryExecuted": data.get("recovery_executed"),
                "gameMutationExecuted": data.get("game_mutation_executed"),
            }
        expected = hashlib.sha256(_canonical(receipt).encode()).hexdigest()
        actual = str(data.get("receipt_sha256") or "")
        valid = bool(re.fullmatch(r"[0-9a-f]{64}", actual)) and hmac.compare_digest(actual, expected)
        return {"ok": valid, "receiptSha256": actual, "expectedSha256": expected, **({} if valid else {"error": "readiness receipt digest does not match"})}
    except (ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


def sanitize(value, secret, *, key="", depth=0):
    if depth > 6:
        return "<depth-limit>"
    if SENSITIVE_KEY.search(str(key)):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            str(child_key)[:128]: sanitize(child, secret, key=str(child_key), depth=depth + 1)
            for child_key, child in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize(child, secret, key=key, depth=depth + 1) for child in list(value)[:32]]
    text = str(value)[:2000]
    if IDENTITY_KEY.search(str(key)):
        return "hmac:" + _hmac_value(secret, text)
    if str(key).lower() in {"ip", "ipaddress", "remoteaddr", "remote_address"}:
        try:
            ipaddress.ip_address(text)
            return "hmac:" + _hmac_value(secret, text)
        except ValueError:
            pass
    if ABSOLUTE_PATH.match(text) and not text.startswith(("/api/", "/metrics/", "/health")):
        return "path-hmac:" + _hmac_value(secret, text)
    text = URL_CREDENTIALS.sub(r"\1<redacted>@", text)
    text = BEARER.sub(r"\1<redacted>", text)
    return text[:500] + ("...[truncated]" if len(text) > 500 else "")


def classify(action, event, policy):
    for rule in policy["rules"]:
        if fnmatch.fnmatchcase(action, rule["pattern"]):
            return dict(rule)
    method = str(event.get("method") or "").upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return {"pattern": "<write-method>", "kind": "change", "category": "administration", "impact": "medium"}
    return {"pattern": "<default>", "kind": "observation", "category": "operations", "impact": "info"}


def incident_key(action, event, kind):
    if action == "incident-response-drill" and event.get("response_incident_key"):
        return validate_incident_key(event["response_incident_key"])
    if kind not in {"incident-open", "incident-resolved"}:
        return None
    if event.get("finding_id"):
        return "desired:" + str(event["finding_id"])[:128]
    if event.get("incident_id"):
        return "slo:" + str(event["incident_id"])[:128]
    return "event:" + hashlib.sha256((action + _canonical(event)).encode()).hexdigest()[:32]


def validate_incident_key(value):
    value = str(value or "").strip()
    if len(value) > 256 or not re.fullmatch(r"(?:slo|desired|event):[A-Za-z0-9_.:-]{1,224}", value):
        raise ValueError("invalid change-intelligence incident key")
    return value


def event_scope(action, event, secret):
    values = {"action:" + action}
    for key in ("path", "target", "service", "map", "category", "subject", "objective_id", "desired_state_action", "capacity_action", "slo_action", "response_incident_key", "runbook_id"):
        value = event.get(key)
        if value is not None and str(value).strip():
            if key in {"target", "subject"}:
                normalized = "hmac:" + _hmac_value(secret, str(value).strip())
            else:
                normalized = sanitize(value, secret, key=key)
            values.add(f"{key}:{str(normalized)[:160]}")
    return sorted(values)


class Store:
    def __init__(self, database, policy_path, secret_path, owner_uid=None, owner_gid=None):
        self.database = pathlib.Path(database)
        self.policy_path = pathlib.Path(policy_path)
        self.secret_path = pathlib.Path(secret_path)
        self.policy = load_policy(self.policy_path)
        self.secret = read_secret(self.secret_path)
        self.owner_uid = int(owner_uid) if owner_uid not in (None, "") else None
        self.owner_gid = int(owner_gid) if owner_gid not in (None, "") else None
        self.lock = threading.RLock()
        self._verified_integrity = None
        self._verified_artifacts = None

    def _artifact_signature(self):
        signature = []
        for index, path in enumerate((self.database.parent, self.database, self.database.with_name(self.database.name + "-wal"), self.policy_path, self.secret_path)):
            try:
                stat_result = path.lstat()
            except FileNotFoundError:
                signature.append(None)
                continue
            stable = (
                stat_result.st_dev, stat_result.st_ino, stat_result.st_mode,
                stat_result.st_uid, stat_result.st_gid,
            )
            # Sibling Admin state changes the shared directory mtime. Preserve
            # parent ownership/mode validation without turning unrelated writes
            # into full retained-chain verification work.
            signature.append(stable if index == 0 else stable + (
                stat_result.st_size, stat_result.st_mtime_ns,
            ))
        return tuple(signature)

    def _cached_verification(self):
        if self._verified_integrity is None or self._verified_artifacts is None:
            return None
        try:
            if self._artifact_signature() != self._verified_artifacts:
                return None
        except OSError:
            return None
        return dict(self._verified_integrity)

    def _remember_verification(self, integrity):
        self._verified_integrity = dict(integrity)
        self._verified_artifacts = self._artifact_signature()

    def _forget_verification(self):
        self._verified_integrity = None
        self._verified_artifacts = None

    def _secure(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.database.parent, 0o700)
        if self.database.exists():
            os.chmod(self.database, 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None:
            os.chown(self.database.parent, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)
            if self.database.exists():
                os.chown(self.database, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)

    def connect(self, readonly=False):
        self._secure()
        if readonly:
            connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True, timeout=10)
        else:
            connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        # Bound read-only page mappings; retain all SQL, HMAC and write checks.
        # Unsupported SQLite builds silently retain ordinary page reads.
        connection.execute("pragma mmap_size=1073741824")
        connection.execute("pragma foreign_keys=on")
        connection.execute("pragma busy_timeout=10000")
        if not readonly:
            connection.execute("pragma journal_mode=wal")
            connection.execute("pragma synchronous=full")
        return connection

    def initialize(self):
        connection = self.connect()
        try:
            connection.executescript("""
                create table if not exists events (
                  sequence integer primary key autoincrement,
                  id text not null unique,
                  occurred_at real not null,
                  ingested_at real not null,
                  action text not null,
                  kind text not null,
                  category text not null,
                  impact text not null,
                  ok integer not null check(ok in (0,1)),
                  actor text,
                  source text not null,
                  source_fingerprint text not null unique,
                  incident_key text,
                  is_change integer not null check(is_change in (0,1)),
                  scope_json text not null,
                  data_json text not null,
                  previous_signature text,
                  signature text not null
                );
                create index if not exists change_events_time on events(occurred_at);
                create index if not exists change_events_incident on events(incident_key,occurred_at);
                create index if not exists change_events_changes on events(is_change,occurred_at);
                create trigger if not exists change_events_no_update before update on events begin select raise(abort,'change-intelligence events are append-only'); end;
                create trigger if not exists change_events_no_delete before delete on events begin select raise(abort,'change-intelligence events are append-only'); end;
                create table if not exists metadata (key text primary key, value text not null);
            """)
            connection.execute("insert into metadata(key,value) values('schema_version','1') on conflict(key) do update set value=excluded.value")
            connection.commit()
        finally:
            connection.close()
        self._secure()
        return self.verify(force=True)

    def initialize_if_needed(self):
        if not self.database.exists():
            self.initialize()

    def _sign(self, document):
        return hmac.new(self.secret, _canonical(document).encode(), hashlib.sha256).hexdigest()

    def source_fingerprint(self, raw_event):
        if not isinstance(raw_event, dict):
            raise ValueError("change-intelligence source event must be an object")
        return _hmac_value(self.secret, _canonical(raw_event))

    def existing_source_fingerprints(self, values):
        values = list(dict.fromkeys(str(value) for value in values if value))
        if not values:
            return set()
        self.initialize_if_needed()
        connection = self.connect(readonly=True)
        try:
            found = set()
            for offset in range(0, len(values), 500):
                chunk = values[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                found.update(row["source_fingerprint"] for row in connection.execute(f"select source_fingerprint from events where source_fingerprint in ({placeholders})", chunk))
            return found
        finally:
            connection.close()

    @staticmethod
    def _document(row):
        return {
            "id": row["id"], "occurredAt": row["occurred_at"], "ingestedAt": row["ingested_at"],
            "action": row["action"], "kind": row["kind"], "category": row["category"],
            "impact": row["impact"], "ok": bool(row["ok"]), "actor": row["actor"], "source": row["source"],
            "sourceFingerprint": row["source_fingerprint"],
            "incidentKey": row["incident_key"], "isChange": bool(row["is_change"]),
            "scope": json.loads(row["scope_json"]), "data": json.loads(row["data_json"]),
            "previousSignature": row["previous_signature"],
        }

    def _prepare(self, raw_event, source, ingested_at):
        if not isinstance(raw_event, dict):
            raise ValueError("change-intelligence event must be an object")
        action = str(raw_event.get("action") or "").strip()[:128]
        if not action or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", action):
            raise ValueError("change-intelligence action is invalid")
        classification = classify(action, raw_event, self.policy)
        occurred = _epoch(raw_event.get("ts"))
        ingested = _epoch(ingested_at)
        payload = sanitize({key: value for key, value in raw_event.items() if key not in {"action", "ts", "ok", "actor", "principal", "principal_id"}}, self.secret)
        encoded = _canonical(payload)
        if len(encoded.encode()) > self.policy["maxPayloadBytes"]:
            raise ValueError("change-intelligence payload exceeds maxPayloadBytes")
        actor = raw_event.get("actor") or raw_event.get("principal_id") or raw_event.get("principal")
        if isinstance(actor, dict):
            actor = actor.get("id")
        actor = str(actor).strip()[:128] if actor else None
        if actor and not re.fullmatch(r"[A-Za-z0-9_.:@/-]+", actor):
            actor = "hmac:" + _hmac_value(self.secret, actor)
        key = incident_key(action, raw_event, classification["kind"])
        source = str(source or "unknown").strip()[:128]
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", source):
            raise ValueError("change-intelligence source is invalid")
        scope = event_scope(action, raw_event, self.secret)
        source_fingerprint = self.source_fingerprint(raw_event)
        return {
            "raw": raw_event, "action": action, "classification": classification,
            "occurred": occurred, "ingested": ingested, "payload": payload,
            "encoded": encoded, "actor": actor, "key": key, "source": source,
            "scope": scope, "sourceFingerprint": source_fingerprint,
            "eventId": "change-" + uuid.uuid4().hex,
        }

    def _insert_prepared(self, connection, prepared, previous):
        classification = prepared["classification"]
        document = {
            "id": prepared["eventId"], "occurredAt": prepared["occurred"], "ingestedAt": prepared["ingested"],
            "action": prepared["action"], "kind": classification["kind"], "category": classification["category"],
            "impact": classification["impact"], "ok": bool(prepared["raw"].get("ok", True)),
            "actor": prepared["actor"], "source": prepared["source"], "sourceFingerprint": prepared["sourceFingerprint"],
            "incidentKey": prepared["key"], "isChange": classification["kind"] == "change",
            "scope": prepared["scope"], "data": prepared["payload"], "previousSignature": previous,
        }
        signature = self._sign(document)
        connection.execute(
            "insert into events(id,occurred_at,ingested_at,action,kind,category,impact,ok,actor,source,source_fingerprint,incident_key,is_change,scope_json,data_json,previous_signature,signature) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (prepared["eventId"], prepared["occurred"], prepared["ingested"], prepared["action"], classification["kind"], classification["category"], classification["impact"], int(document["ok"]), prepared["actor"], prepared["source"], prepared["sourceFingerprint"], prepared["key"], int(document["isChange"]), _canonical(prepared["scope"]), prepared["encoded"], previous, signature),
        )
        return {
            "ok": True, "id": prepared["eventId"], "occurredAt": _iso(prepared["occurred"]),
            **classification, "incidentKey": prepared["key"], "signature": signature,
        }

    def record(self, raw_event, *, source="admin-audit", ingested_at=None):
        prepared = self._prepare(raw_event, source, ingested_at)
        with self.lock:
            cached_integrity = self._cached_verification()
            connection = self.connect()
            try:
                connection.execute("begin immediate")
                duplicate = connection.execute("select * from events where source_fingerprint=?", (prepared["sourceFingerprint"],)).fetchone()
                if duplicate:
                    connection.rollback()
                    result = self._public(duplicate)
                    result.update({"duplicate": True, "pattern": prepared["classification"]["pattern"]})
                    return result
                count = connection.execute("select count(*) from events").fetchone()[0]
                if count >= self.policy["maxEvents"]:
                    raise RuntimeError("change-intelligence maxEvents reached; archive and rotate the ledger")
                prior = connection.execute("select signature from events order by sequence desc limit 1").fetchone()
                previous = prior["signature"] if prior else None
                result = self._insert_prepared(connection, prepared, previous)
                connection.commit()
            except Exception:
                connection.rollback()
                self._forget_verification()
                raise
            finally:
                connection.close()
            if cached_integrity is not None:
                cached_integrity.update({"eventCount": count + 1, "lastEventSignature": result["signature"]})
                self._remember_verification(cached_integrity)
            else:
                self._forget_verification()
        if prepared["classification"]["kind"] == "incident-open":
            result["candidates"] = self.correlate(prepared["key"])
        return result

    def record_many(self, raw_events, *, source="admin-audit-history", ingested_at=None, skip_invalid=False):
        raw_events = list(raw_events)
        if len(raw_events) > self.policy["historyImportLimit"]:
            raise ValueError("change-intelligence batch exceeds historyImportLimit")
        prepared_rows = []
        errors = 0
        for event in raw_events:
            try:
                prepared_rows.append(self._prepare(event, source, ingested_at))
            except (ValueError, TypeError, OverflowError):
                if not skip_invalid:
                    raise
                errors += 1
        with self.lock:
            cached_integrity = self._cached_verification()
            connection = self.connect()
            inserted = []
            duplicates = 0
            try:
                connection.execute("begin immediate")
                count = connection.execute("select count(*) from events").fetchone()[0]
                prior = connection.execute("select signature from events order by sequence desc limit 1").fetchone()
                previous = prior["signature"] if prior else None
                fingerprints = list(dict.fromkeys(row["sourceFingerprint"] for row in prepared_rows))
                known = set()
                for offset in range(0, len(fingerprints), 500):
                    chunk = fingerprints[offset:offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    known.update(row["source_fingerprint"] for row in connection.execute(f"select source_fingerprint from events where source_fingerprint in ({placeholders})", chunk))
                for prepared in prepared_rows:
                    if prepared["sourceFingerprint"] in known:
                        duplicates += 1
                        continue
                    if count >= self.policy["maxEvents"]:
                        raise RuntimeError("change-intelligence maxEvents reached; archive and rotate the ledger")
                    result = self._insert_prepared(connection, prepared, previous)
                    inserted.append(result)
                    known.add(prepared["sourceFingerprint"])
                    previous = result["signature"]
                    count += 1
                connection.commit()
            except Exception:
                connection.rollback()
                self._forget_verification()
                raise
            finally:
                connection.close()
            if cached_integrity is not None and inserted:
                cached_integrity.update({"eventCount": count, "lastEventSignature": previous})
                self._remember_verification(cached_integrity)
            elif cached_integrity is None:
                self._forget_verification()
        return {"ok": errors == 0, "inserted": inserted, "insertedCount": len(inserted), "duplicates": duplicates, "errors": errors}

    @staticmethod
    def _public(row):
        event = {
            "sequence": row["sequence"], "id": row["id"], "occurredAt": _iso(row["occurred_at"]),
            "ingestedAt": _iso(row["ingested_at"]), "action": row["action"], "kind": row["kind"],
            "category": row["category"], "impact": row["impact"], "ok": bool(row["ok"]),
            "actor": row["actor"], "source": row["source"], "incidentKey": row["incident_key"], "isChange": bool(row["is_change"]),
            "scope": json.loads(row["scope_json"]), "data": json.loads(row["data_json"]),
            "signature": row["signature"],
        }
        if event["action"] in {"incident-response-drill", "incident-readiness-certification"}:
            event["receiptVerification"] = verify_readiness_receipt(event)
        return event

    def _certification_public(self, row):
        if not row:
            return None
        event = self._public(row)
        policy_current = hmac.compare_digest(
            str((event.get("data") or {}).get("policy_sha256") or ""),
            str(self.policy["response"]["policySha256"]),
        )
        event["policyCurrent"] = policy_current
        event["currentReady"] = bool(event.get("ok") and event["receiptVerification"].get("ok") and policy_current)
        return event

    def certification_status(self):
        """Verify the latest readiness event, without asserting timeline integrity.

        Deployment still requires the independent complete-ledger integrity gate.
        This bounded receipt read keeps metrics independent of timeline writes.
        """
        connection = self.connect(readonly=True)
        try:
            row = connection.execute(
                "select * from events where action='incident-readiness-certification' order by occurred_at desc,sequence desc limit 1"
            ).fetchone()
            if row is None:
                return None
            if not hmac.compare_digest(self._sign(self._document(row)), row["signature"]):
                raise ValueError("readiness certification event HMAC is invalid")
            return self._certification_public(row)
        finally:
            connection.close()

    def _correlate_connection(self, connection, key):
        incident = connection.execute("select * from events where incident_key=? and kind='incident-open' order by occurred_at desc limit 1", (str(key),)).fetchone()
        if not incident:
            return []
        before = self.policy["correlationWindowBeforeSeconds"]
        rows = connection.execute(
            "select * from events where is_change=1 and occurred_at<=? and occurred_at>=? order by occurred_at desc limit 1000",
            (incident["occurred_at"], incident["occurred_at"] - before),
        ).fetchall()
        incident_scope = set(json.loads(incident["scope_json"]))
        candidates = []
        for row in rows:
            age = max(0.0, incident["occurred_at"] - row["occurred_at"])
            overlap = sorted(incident_scope & set(json.loads(row["scope_json"])))
            recency = max(0.0, 1.0 - age / before)
            score = IMPACTS[row["impact"]] * 2.0 + recency * 2.0 + min(3, len(overlap)) * 1.5
            reasons = [f"{int(age)}s before incident", f"{row['impact']} impact"]
            if overlap:
                reasons.append("shared scope: " + ", ".join(overlap[:3]))
            public = self._public(row)
            public.pop("data", None)
            public.pop("signature", None)
            candidates.append({**public, "ageSeconds": age, "score": round(score, 3), "reasons": reasons})
        candidates.sort(key=lambda row: (-row["score"], row["ageSeconds"], -row["sequence"]))
        return candidates[: self.policy["candidateLimit"]]

    def correlate(self, key):
        self.initialize_if_needed()
        key = validate_incident_key(key)
        connection = self.connect(readonly=True)
        try:
            return self._correlate_connection(connection, key)
        finally:
            connection.close()

    def _capsule_connection(self, connection, key, now=None, ledger=None):
        rows = connection.execute("select * from events where incident_key=? order by occurred_at,sequence", (str(key),)).fetchall()
        if not rows:
            raise ValueError("change-intelligence incident does not exist")
        opened = next((row for row in reversed(rows) if row["kind"] == "incident-open"), None)
        if not opened:
            raise ValueError("change-intelligence incident has no open event")
        resolved = next((row for row in reversed(rows) if row["kind"] == "incident-resolved" and row["occurred_at"] >= opened["occurred_at"]), None)
        end = min(
            resolved["occurred_at"] if resolved else float(now if now is not None else time.time()),
            opened["occurred_at"] + self.policy["correlationWindowAfterSeconds"],
        )
        followup = connection.execute(
            "select * from events where occurred_at>? and occurred_at<=? order by occurred_at,sequence limit ?",
            (opened["occurred_at"], end, self.policy["capsuleEvidenceLimit"]),
        ).fetchall()
        drills = connection.execute(
            "select * from events where incident_key=? and action='incident-response-drill' order by occurred_at desc,sequence desc limit 20",
            (str(key),),
        ).fetchall()
        certification = connection.execute(
            "select * from events where action='incident-readiness-certification' order by occurred_at desc,sequence desc limit 1"
        ).fetchone()
        capsule = {
            "ok": True, "incidentKey": key, "status": "resolved" if resolved else "open",
            "opened": self._public(opened), "resolved": self._public(resolved) if resolved else None,
            "candidateChanges": self._correlate_connection(connection, key), "followupEvidence": [self._public(row) for row in followup],
            "responseDrills": [self._public(row) for row in drills],
            "readinessCertification": self._certification_public(certification),
            "causalityClaimed": False,
            "interpretation": "Candidates are ranked temporal/scope correlations, not proof of causality.",
        }
        capsule["responsePlan"] = compile_response_plan(capsule, self.policy, ledger)
        return capsule

    def capsule(self, key):
        self.initialize_if_needed()
        key = validate_incident_key(key)
        connection = self.connect(readonly=True)
        try:
            connection.execute("begin")
            integrity = self._verify_connection(connection)
            return self._capsule_connection(connection, key, ledger=integrity)
        finally:
            connection.close()

    def signed_capsule(self, key, at=None):
        """Freeze one bounded capsule and bind it to the verified ledger head."""
        self.initialize_if_needed()
        key = validate_incident_key(key)
        generated = _epoch(at)
        connection = self.connect(readonly=True)
        try:
            connection.execute("begin")
            integrity = self._verify_connection(connection)
            if not integrity.get("ok"):
                raise RuntimeError("cannot export a capsule from an invalid change-intelligence ledger")
            capsule = self._capsule_connection(connection, key, now=generated, ledger=integrity)
        finally:
            connection.close()
        payload = {
            "schemaVersion": 2,
            "generatedAt": _iso(generated),
            "incidentKey": capsule["incidentKey"],
            "signatureAlgorithm": "hmac-sha256",
            "signingKeyFingerprint": hashlib.sha256(self.secret).hexdigest(),
            "ledger": {
                "eventCount": integrity["eventCount"],
                "lastEventSignature": integrity["lastEventSignature"],
                "sqlite": integrity["sqlite"],
                "appendOnlyTriggers": integrity["appendOnlyTriggers"],
                "eventChainValid": integrity["eventChainValid"],
            },
            "capsule": capsule,
        }
        signature = hmac.new(self.secret, _canonical(payload).encode(), hashlib.sha256).hexdigest()
        return {**payload, "signature": signature}

    def status(self, limit=None):
        self.initialize_if_needed()
        limit = max(1, min(int(limit or self.policy["statusEventLimit"]), 1000))
        connection = self.connect(readonly=True)
        try:
            recent = connection.execute("select * from events order by occurred_at desc,sequence desc limit ?", (limit,)).fetchall()
            all_incident_rows = connection.execute("select * from events where incident_key is not null order by occurred_at,sequence").fetchall()
            certification = connection.execute(
                "select * from events where action='incident-readiness-certification' order by occurred_at desc,sequence desc limit 1"
            ).fetchone()
            total = connection.execute("select count(*) from events").fetchone()[0]
        finally:
            connection.close()
        incidents = {}
        for row in all_incident_rows:
            key = row["incident_key"]
            state = incidents.setdefault(key, {"incidentKey": key, "opened": None, "resolved": None})
            if row["kind"] == "incident-open":
                state["opened"] = self._public(row)
                state["resolved"] = None
            elif row["kind"] == "incident-resolved":
                state["resolved"] = self._public(row)
        incident_rows = []
        for state in incidents.values():
            if not state["opened"]:
                continue
            state["status"] = "resolved" if state["resolved"] else "open"
            incident_rows.append(state)
        incident_rows.sort(key=lambda row: row["opened"]["occurredAt"], reverse=True)
        relevant = [row for row in incident_rows if row["status"] == "open"]
        relevant.extend(row for row in incident_rows if row["status"] != "open" and row not in relevant and len(relevant) < limit)
        for state in relevant:
            state["candidateChanges"] = self.correlate(state["incidentKey"])
        integrity = self.verify()
        return {
            "ok": integrity["ok"], "state": "invalid" if not integrity["ok"] else "active",
            "eventCount": total, "openIncidents": [row for row in relevant if row["status"] == "open"],
            "incidents": relevant[:limit], "recentEvents": [self._public(row) for row in recent],
            "readinessCertification": self._certification_public(certification),
            "policy": self.policy, "integrity": integrity,
        }

    def _verify_connection(self, connection):
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        triggers = {row["name"] for row in connection.execute("select name from sqlite_master where type='trigger'")}
        required = {"change_events_no_update", "change_events_no_delete"}
        previous = None
        valid = True
        count = 0
        for row in connection.execute("select * from events order by sequence"):
            count += 1
            if row["previous_signature"] != previous or not hmac.compare_digest(self._sign(self._document(row)), row["signature"]):
                valid = False
            previous = row["signature"]
        ok = integrity == "ok" and required.issubset(triggers) and valid
        return {"ok": ok, "sqlite": integrity, "appendOnlyTriggers": required.issubset(triggers), "eventChainValid": valid, "eventCount": count, "lastEventSignature": previous}

    def verify(self, *, force=False):
        if not self.database.exists():
            return {"ok": False, "sqlite": "missing", "eventChainValid": False}
        with self.lock:
            if not force:
                cached = self._cached_verification()
                if cached is not None:
                    return cached
            connection = self.connect(readonly=True)
            try:
                integrity = self._verify_connection(connection)
                if integrity.get("ok"):
                    self._remember_verification(integrity)
                else:
                    self._forget_verification()
                return integrity
            except (sqlite3.Error, ValueError, json.JSONDecodeError, OSError) as exc:
                self._forget_verification()
                return {"ok": False, "sqlite": "error", "eventChainValid": False, "error": str(exc)}
            finally:
                connection.close()

    def metadata(self, key, default=None):
        self.initialize_if_needed()
        connection = self.connect(readonly=True)
        try:
            row = connection.execute("select value from metadata where key=?", (str(key),)).fetchone()
            return row["value"] if row else default
        finally:
            connection.close()

    def set_metadata(self, key, value):
        self.initialize_if_needed()
        connection = self.connect()
        try:
            connection.execute("insert into metadata(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (str(key), str(value)))
            connection.commit()
        finally:
            connection.close()

    def backup(self, target):
        self.initialize_if_needed()
        target = pathlib.Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        source = self.connect(readonly=True)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
        if os.geteuid() == 0 and self.owner_uid is not None:
            os.chown(target, self.owner_uid, self.owner_gid if self.owner_gid is not None else -1)
        verification = Store(target, self.policy_path, self.secret_path, self.owner_uid, self.owner_gid).verify(force=True)
        if not verification.get("ok"):
            target.unlink(missing_ok=True)
            raise RuntimeError(f"change-intelligence backup verification failed: {verification}")
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(target), "bytes": target.stat().st_size, "sha256": digest.hexdigest(), "integrity": verification}

    def prometheus(self, status=None):
        status = status or self.status(limit=10)
        events = status["recentEvents"]
        latest = _epoch(events[0]["occurredAt"]) if events else "NaN"
        with_candidates = sum(1 for row in status["openIncidents"] if row["candidateChanges"])
        connection = self.connect(readonly=True)
        try:
            row = connection.execute("select * from events where action='incident-response-drill' order by occurred_at desc,sequence desc limit 1").fetchone()
            latest_drill = self._public(row) if row else None
        finally:
            connection.close()
        certification = status.get("readinessCertification")
        summary = ((certification or {}).get("data") or {}).get("summary") or {}
        runbooks_total = int(summary.get("runbooksTotal") or 0)
        runbooks_ready = int(summary.get("runbooksReady") or 0)
        coverage = runbooks_ready / runbooks_total if runbooks_total else 0
        return "\n".join([
            "# HELP dash_change_intelligence_collector_up Change timeline SQLite, triggers, and HMAC chain verify.",
            "# TYPE dash_change_intelligence_collector_up gauge",
            f"dash_change_intelligence_collector_up {1 if status['integrity']['ok'] else 0}",
            f"dash_change_intelligence_events_total {status['eventCount']}",
            f"dash_change_intelligence_open_incidents {len(status['openIncidents'])}",
            f"dash_change_intelligence_open_incidents_with_candidate_changes {with_candidates}",
            f"dash_change_intelligence_last_event_timestamp_seconds {latest}",
            f"dash_incident_response_latest_drill_ready {1 if latest_drill and latest_drill.get('ok') and latest_drill.get('receiptVerification', {}).get('ok') else 0}",
            f"dash_incident_response_last_drill_timestamp_seconds {_epoch(latest_drill['occurredAt']) if latest_drill else 'NaN'}",
            f"dash_incident_readiness_certification_ready {1 if certification and certification.get('currentReady') else 0}",
            f"dash_incident_readiness_last_certification_timestamp_seconds {_epoch(certification['occurredAt']) if certification else 'NaN'}",
            f"dash_incident_readiness_runbooks_ready {runbooks_ready}",
            f"dash_incident_readiness_runbooks_total {runbooks_total}",
            f"dash_incident_readiness_coverage_ratio {coverage:.12g}",
            f"dash_incident_readiness_diagnostics_ready {int(summary.get('diagnosticsReady') or 0)}",
            f"dash_incident_readiness_diagnostics_total {int(summary.get('diagnosticsTotal') or 0)}",
            f"dash_incident_readiness_recovery_contracts_ready {int(summary.get('recoveryContractsReady') or 0)}",
            f"dash_incident_readiness_recovery_contracts_total {int(summary.get('recoveryContractsTotal') or 0)}",
        ]) + "\n"
