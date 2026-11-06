"""Validate an explicitly approved, immutable game runtime replacement."""

import hashlib
import json
import pathlib
import re


KEYS = ("DUNE_IMAGE_TAG", "DUNE_GAME_IMAGE", "DUNE_RUNTIME_SECURITY_APPROVAL_FILE")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def evidence_file(root, record):
    if not isinstance(record, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256") or "")):
        raise ValueError("runtime security evidence requires a pinned digest")
    raw = pathlib.Path(str(record.get("path") or ""))
    if not raw.is_absolute():
        raw = root / raw
    if raw.is_symlink():
        raise ValueError("runtime security evidence must not be a symlink")
    path = raw.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("runtime security evidence must remain inside the workspace") from exc
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("runtime security evidence exceeds its file bound")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError("runtime security evidence changed after approval")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("runtime security evidence must be an object")
    return value


def settings_from_file(path):
    settings = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in KEYS:
            settings[key.strip()] = value.strip().strip("\"'")
    return settings


def configuration(workspace, settings, expected_tag=None):
    tag = str(expected_tag or settings.get("DUNE_IMAGE_TAG") or "").strip()
    image = str(settings.get("DUNE_GAME_IMAGE") or "").strip()
    vendor = f"registry.funcom.com/funcom/self-hosting/seabass-server:{tag}"
    if not image or image == vendor:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag):
        raise ValueError("runtime security requires an identified vendor game version")
    if not re.fullmatch(r"snape/dune-server:[A-Za-z0-9][A-Za-z0-9._-]{0,127}", image):
        raise ValueError("runtime security image reference is invalid")
    root = pathlib.Path(workspace).resolve(strict=True)
    raw = pathlib.Path(str(settings.get("DUNE_RUNTIME_SECURITY_APPROVAL_FILE") or ""))
    if not str(settings.get("DUNE_RUNTIME_SECURITY_APPROVAL_FILE") or "").strip():
        raise ValueError("runtime security image requires an explicit approval file")
    if not raw.is_absolute():
        raw = root / raw
    if raw.is_symlink():
        raise ValueError("runtime security approval must not be a symlink")
    resolved = raw.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("runtime security approval must remain inside the workspace") from exc
    if not resolved.is_file() or resolved.stat().st_size > 128 * 1024:
        raise ValueError("runtime security approval exceeds its file bound")
    approval = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(approval, dict) or type(approval.get("schemaVersion")) is not int or approval["schemaVersion"] != 1:
        raise ValueError("runtime security approval schema is invalid")
    if approval.get("vendorTag") != tag or approval.get("image") != image:
        raise ValueError("runtime security approval does not cover the target game version/image")
    for key in ("imageId", "vendorImageId"):
        if not isinstance(approval.get(key), str) or not IMAGE_ID.fullmatch(approval[key]):
            raise ValueError("runtime security approval requires immutable image IDs")
    scan = approval.get("scan") or {}
    if not isinstance(scan, dict) or any(type(scan.get(key)) is not int or scan[key] != 0 for key in ("critical", "high", "errors")):
        raise ValueError("runtime security approval requires a complete clean image scan")
    if approval.get("gameBinaryAssetsAndInterpreterUnchanged") is not True:
        raise ValueError("runtime security approval lacks vendor payload qualification")
    qualification = evidence_file(root, approval.get("qualification"))
    required = ("game_binary_assets_and_embedded_interpreter_unchanged",
                "only_python_packaging_files_changed", "native_binary_linker_passed",
                "image_runtime_configuration_unchanged")
    if (qualification.get("candidate_image") != approval["imageId"]
            or any(qualification.get(key) is not True for key in required)
            or not any(key.startswith("embedded_python_") and key.endswith("_compatibility_passed")
                       and value is True for key, value in qualification.items())):
        raise ValueError("runtime security qualification does not cover this payload")
    report = evidence_file(root, approval.get("scanReport"))
    metadata = report.get("Metadata") or {}
    results = report.get("Results")
    if (not isinstance(metadata, dict) or metadata.get("ImageID") != approval["imageId"]
            or not metadata.get("OS") or report.get("Errors") or not isinstance(results, list)
            or not results or any(not isinstance(result, dict) for result in results)):
        raise ValueError("runtime security scan is incomplete or covers another image")
    if not any(result.get("Class") == "os-pkgs" for result in results):
        raise ValueError("runtime security scan lacks operating-system coverage")
    for result in results:
        vulnerabilities = result.get("Vulnerabilities") or []
        if (result.get("Errors") or not isinstance(vulnerabilities, list)
                or any(not isinstance(v, dict) or v.get("Severity") in ("CRITICAL", "HIGH")
                       for v in vulnerabilities)):
            raise ValueError("runtime security scan contains unresolved findings")
    return approval


def verify_loaded_images(approval, runtime_id, vendor_id):
    if runtime_id != approval["imageId"] or vendor_id != approval["vendorImageId"]:
        raise ValueError("runtime or vendor image changed since qualification")
