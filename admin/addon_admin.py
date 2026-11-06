"""Bounded community UI-addon lifecycle adapted from Red-Blink's MIT implementation."""
import hashlib
import json
import pathlib
import re
import shutil
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

INDEX_URL = "https://raw.githubusercontent.com/Red-Blink/dune-docker-addons/main/index.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$", re.I)
PERMISSIONS = {"players:read", "ops:read", "database:read", "database:write", "admin:grant-items", "server:status", "server:restart", "files:addon-data", "broadcast:send"}
BLOCKED_LIFECYCLES = {"unsupported", "removed", "blocked"}
ALLOWED_HOSTS = {"raw.githubusercontent.com", "github.com", "codeload.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


def _text(value, name, optional=False):
    value = str(value or "").strip()
    if not value and not optional:
        raise ValueError(f"{name} is required")
    if len(value) > 1024:
        raise ValueError(f"{name} is too long")
    return value


def _url(value, name):
    value = _text(value, name)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an allowed HTTPS GitHub URL")
    return value


def _permissions(value):
    raw = []
    if isinstance(value, list):
        raw.extend(value)
    elif isinstance(value, dict):
        for category, actions in value.items():
            if not isinstance(actions, list):
                raise ValueError(f"addon permissions.{category} must be an array")
            raw.extend(f"{category}:{action}" for action in actions)
    else:
        raise ValueError("addon permissions must be an array or object")
    result = sorted({_text(item, "permission").lower() for item in raw})
    unknown = set(result) - PERMISSIONS
    if unknown:
        raise ValueError(f"unsupported addon permissions: {sorted(unknown)}")
    return result


def _relative(value, name):
    value = _text(value, name).replace("\\", "/")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or len(value) > 512:
        raise ValueError(f"unsafe {name}")
    return path.as_posix()


def normalize_manifest(value, remote=False):
    if not isinstance(value, dict) or int(value.get("schemaVersion", 1)) != 1:
        raise ValueError("unsupported addon manifest")
    addon_id = _text(value.get("id"), "id")
    if not ID_RE.fullmatch(addon_id):
        raise ValueError("invalid addon id")
    if _text(value.get("type"), "type") != "ui":
        raise ValueError("only UI addons are supported")
    result = {"schemaVersion": 1, "id": addon_id, "name": _text(value.get("name"), "name"), "description": _text(value.get("description"), "description", True), "author": _text(value.get("author"), "author", True), "version": _text(value.get("version"), "version"), "type": "ui", "permissions": _permissions(value.get("permissions") or [])}
    if remote:
        result.update({"sourceUrl": _url(value.get("sourceUrl"), "sourceUrl"), "downloadUrl": _url(value.get("downloadUrl"), "downloadUrl"), "sha256": _text(value.get("sha256"), "sha256").lower()})
        if not SHA_RE.fullmatch(result["sha256"]):
            raise ValueError("invalid addon SHA-256")
    else:
        entry = value.get("entry") if isinstance(value.get("entry"), dict) else {}
        result["entry"] = {"navigation": _text(entry.get("navigation"), "entry.navigation", True), "path": _relative(entry.get("path"), "entry.path")}
    return result


def _fetch(url, max_bytes, timeout=10):
    url = _url(url, "URL")
    request = urllib.request.Request(url, headers={"Accept": "application/json, application/zip", "User-Agent": "DASH-Addon/1.0"})
    class AllowedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # Check the destination before contacting it, not after download.
            _url(newurl, "redirected URL")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(AllowedRedirect())
    with opener.open(request, timeout=timeout) as response:
        _url(response.geturl(), "redirected URL")
        content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("addon response exceeds size limit")
    return content


def fetch_index(index_url=INDEX_URL, fetcher=None):
    fetcher = fetcher or _fetch
    data = json.loads(fetcher(index_url, 2 * 1024 * 1024))
    if not isinstance(data, dict) or int(data.get("schemaVersion", 0)) != 1 or not isinstance(data.get("addons"), list) or len(data["addons"]) > 200:
        raise ValueError("invalid community addon index")
    seen = set()
    rows = []
    for raw in data["addons"]:
        addon_id = _text(raw.get("id"), "id")
        if not ID_RE.fullmatch(addon_id) or addon_id in seen:
            raise ValueError("invalid or duplicate community addon id")
        seen.add(addon_id)
        row = {"id": addon_id, "name": _text(raw.get("name"), "name"), "description": _text(raw.get("description"), "description", True), "author": _text(raw.get("author"), "author", True), "version": _text(raw.get("version"), "version"), "manifestUrl": _url(raw.get("manifestUrl"), "manifestUrl"), "lifecycle": _text(raw.get("lifecycle") or "active", "lifecycle"), "lifecycleMessage": _text(raw.get("lifecycleMessage") or raw.get("lifecycleReason"), "lifecycleMessage", True), "permissions": _permissions(raw.get("permissions") or [])}
        if not row["permissions"]:
            try:
                manifest = normalize_manifest(json.loads(fetcher(row["manifestUrl"], 1024 * 1024)), remote=True)
                if manifest["id"] == row["id"]:
                    row.update({"permissions": manifest["permissions"], "sourceUrl": manifest["sourceUrl"]})
            except Exception:
                pass
        rows.append(row)
    return {"schemaVersion": 1, "sourceUrl": index_url, "updatedAt": data.get("updatedAt"), "addons": rows}


def _paths(root):
    root = pathlib.Path(root)
    return root, root / "installed", root / "staging", root / "downloads", root / "state.json"


def _state(root):
    path = _paths(root)[4]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(root, value):
    path = _paths(root)[4]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def list_installed(root):
    _, installed, _, _, _ = _paths(root)
    state = _state(root)
    rows = []
    if not installed.exists():
        return {"addons": []}
    for directory in sorted(installed.iterdir()):
        if not directory.is_dir() or not ID_RE.fullmatch(directory.name):
            continue
        try:
            manifest = normalize_manifest(json.loads((directory / "addon.json").read_text(encoding="utf-8")))
            saved = state.get(manifest["id"], {})
            rows.append({**manifest, "entryPath": manifest["entry"]["path"], "enabled": bool(saved.get("enabled")), "status": "Enabled" if saved.get("enabled") else "Disabled", "approvedPermissions": saved.get("approvedPermissions") or [], "lifecycle": saved.get("lifecycle", "active"), "provenance": saved.get("provenance") or {}})
        except Exception as exc:
            rows.append({"id": directory.name, "name": directory.name, "status": "Invalid", "enabled": False, "error": str(exc)})
    return {"addons": rows}


def install(root, addon_id, approved_permissions, index_url=INDEX_URL, fetcher=None):
    fetcher = fetcher or _fetch
    index = fetch_index(index_url, fetcher=fetcher)
    summary = next((row for row in index["addons"] if row["id"] == addon_id), None)
    if not summary:
        raise ValueError("community addon not found")
    if summary["lifecycle"] in BLOCKED_LIFECYCLES:
        raise PermissionError(f"addon lifecycle blocks installation: {summary['lifecycle']}")
    remote = normalize_manifest(json.loads(fetcher(summary["manifestUrl"], 1024 * 1024)), remote=True)
    if remote["id"] != summary["id"] or remote["version"] != summary["version"]:
        raise ValueError("community index and manifest identity do not match")
    approved = _permissions(approved_permissions or [])
    missing = set(remote["permissions"]) - set(approved)
    if missing:
        raise PermissionError(f"permissions require explicit approval: {sorted(missing)}")
    archive = fetcher(remote["downloadUrl"], 50 * 1024 * 1024, timeout=30)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != remote["sha256"]:
        raise ValueError("addon archive SHA-256 does not match manifest")
    root, installed, staging, downloads, _ = _paths(root)
    for path in (installed, staging, downloads):
        path.mkdir(parents=True, exist_ok=True)
    archive_path = downloads / f"{remote['id']}-{remote['version']}.zip"
    archive_path.write_bytes(archive)
    stage = staging / f"{remote['id']}-{digest[:12]}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    with zipfile.ZipFile(archive_path) as handle:
        infos = handle.infolist()
        if not infos or len(infos) > 10000 or sum(info.file_size for info in infos) > 256 * 1024 * 1024:
            raise ValueError("addon archive exceeds extraction limits")
        names = [_relative(info.filename, "zip entry") for info in infos]
        if "addon.json" not in names:
            raise ValueError("addon archive must contain addon.json at root")
        for info in infos:
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("addon archive cannot contain symbolic links")
        handle.extractall(stage)
    installed_manifest = normalize_manifest(json.loads((stage / "addon.json").read_text(encoding="utf-8")))
    if installed_manifest["id"] != remote["id"] or installed_manifest["version"] != remote["version"] or not (stage / installed_manifest["entry"]["path"]).is_file():
        raise ValueError("installed archive identity or entry path is invalid")
    destination = installed / remote["id"]
    recovery = root / "recovery"
    recovery.mkdir(exist_ok=True)
    if destination.exists():
        shutil.move(destination, recovery / f"{remote['id']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    stage.replace(destination)
    state = _state(root)
    state[remote["id"]] = {"enabled": False, "approvedPermissions": approved, "lifecycle": summary["lifecycle"], "installedAt": datetime.now(timezone.utc).isoformat(), "provenance": {"indexUrl": index_url, "manifestUrl": summary["manifestUrl"], "sourceUrl": remote["sourceUrl"], "downloadUrl": remote["downloadUrl"], "version": remote["version"], "sha256": digest}}
    _write_state(root, state)
    return {"ok": True, "sha256": digest, "addon": next(row for row in list_installed(root)["addons"] if row["id"] == remote["id"])}


def set_enabled(root, addon_id, enabled):
    addon_id = _text(addon_id, "id")
    installed = {row["id"]: row for row in list_installed(root)["addons"]}
    if addon_id not in installed or installed[addon_id].get("status") == "Invalid":
        raise ValueError("installed addon not found or invalid")
    state = _state(root)
    saved = state.get(addon_id, {})
    if enabled and (saved.get("lifecycle") in BLOCKED_LIFECYCLES or set(installed[addon_id]["permissions"]) - set(saved.get("approvedPermissions") or [])):
        raise PermissionError("addon lifecycle or permission approval blocks enablement")
    saved["enabled"] = bool(enabled)
    state[addon_id] = saved
    _write_state(root, state)
    return {"ok": True, "addon": next(row for row in list_installed(root)["addons"] if row["id"] == addon_id)}


def remove(root, addon_id):
    addon_id = _text(addon_id, "id")
    if not ID_RE.fullmatch(addon_id):
        raise ValueError("invalid addon id")
    root, installed, _, _, _ = _paths(root)
    source = installed / addon_id
    if not source.is_dir():
        raise ValueError("installed addon not found")
    recovery = root / "recovery"
    recovery.mkdir(parents=True, exist_ok=True)
    target = recovery / f"{addon_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    shutil.move(source, target)
    state = _state(root)
    state.pop(addon_id, None)
    _write_state(root, state)
    return {"ok": True, "id": addon_id, "recoveryPath": str(target)}


def content_path(root, addon_id, relative):
    addon = next((row for row in list_installed(root)["addons"] if row["id"] == addon_id), None)
    if not addon or not addon.get("enabled"):
        raise PermissionError("addon is not enabled")
    base = _paths(root)[1] / addon_id
    target = (base / _relative(relative, "content path")).resolve()
    target.relative_to(base.resolve())
    if not target.is_file():
        raise FileNotFoundError("addon content not found")
    return target


def assert_permission(root, addon_id, permission):
    addon = next((row for row in list_installed(root)["addons"] if row["id"] == addon_id), None)
    if not addon or not addon.get("enabled"):
        raise PermissionError("addon is not installed and enabled")
    if permission not in addon.get("permissions", []) or permission not in addon.get("approvedPermissions", []):
        raise PermissionError(f"addon is not approved for {permission}")
    if addon.get("lifecycle") == "blocked":
        raise PermissionError("addon lifecycle is blocked")
    return addon
