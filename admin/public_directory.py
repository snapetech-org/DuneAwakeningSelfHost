#!/usr/bin/env python3
"""Signed, privacy-bounded public server directory protocol."""

from __future__ import annotations

import base64
import datetime as dt
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import urllib.parse


SCHEMA = "dash-public-directory-entry/v1"
CATALOG_SCHEMA = "dash-public-directory-catalog/v1"
SOURCES_SCHEMA = "dash-public-directory-sources/v1"
ALGORITHM = "Ed25519"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900
MAX_CLOCK_SKEW_SECONDS = 300
MAX_ENTRY_BYTES = 128 * 1024
MAX_SOURCES = 500
MAX_TEXT = 2000
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
ED25519_PUBLIC_DER_BYTES = 44
ED25519_SIGNATURE_BYTES = 64
ED25519_FIELD = 2**255 - 19
ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493
ED25519_D = (-121665 * pow(121666, ED25519_FIELD - 2, ED25519_FIELD)) % ED25519_FIELD
ED25519_I = pow(2, (ED25519_FIELD - 1) // 4, ED25519_FIELD)
REGIONS = (
    "Africa",
    "Asia",
    "Europe",
    "Middle East",
    "North America",
    "Oceania",
    "South America",
)
ENTRY_KEYS = {
    "schemaVersion", "serverId", "generatedAt", "expiresAt", "sourceUrl",
    "profile", "status", "signingKey", "signature",
}
PROFILE_KEYS = {
    "name", "description", "region", "websiteUrl", "discordInvite",
    "game", "software", "features",
}
STATUS_KEYS = {
    "state", "playersOnline", "capacity", "build", "sietches", "maps",
}


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso8601(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("directory timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("directory timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def bool_value(value, default=False):
    if value is None or str(value).strip() == "":
        return bool(default)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def bounded_int(value, low, high, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer from {low} to {high}") from exc
    if not low <= number <= high:
        raise ValueError(f"{label} must be from {low} to {high}")
    return number


def bounded_json_int(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be a JSON integer from {low} to {high}")
    return value


def clean_text(value, limit, label, required=False):
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError(f"{label} is required")
    if len(text) > limit or any(ord(char) < 32 for char in text):
        raise ValueError(f"{label} exceeds its public text bound")
    return text


def normalize_https_url(value, label, required=False):
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError(f"{label} is required")
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.port not in (None, 443) or parsed.query or parsed.fragment
    ):
        raise ValueError(f"{label} must be a query-free HTTPS URL")
    host = parsed.hostname.lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise ValueError(f"{label} must use a public hostname")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None:
        raise ValueError(f"{label} must use a public DNS hostname, not an IP literal")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


def normalize_discord_invite(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https" or parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Discord invite must be a canonical HTTPS invite")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "discord.gg" and len(parts) == 1:
        code = parts[0]
    elif host in ("discord.com", "www.discord.com") and len(parts) == 2 and parts[0].lower() == "invite":
        code = parts[1]
    else:
        raise ValueError("Discord invite must use discord.gg or discord.com/invite")
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,100}", code):
        raise ValueError("Discord invite code is invalid")
    return f"https://discord.gg/{code}"


def public_config(env=None, root=None):
    values = dict(os.environ if env is None else env)
    root = pathlib.Path(root or values.get("DUNE_ROOT") or "/workspace")
    enabled = bool_value(values.get("DUNE_PUBLIC_DIRECTORY_ENABLED"), False)
    ttl = bounded_int(values.get("DUNE_PUBLIC_DIRECTORY_TTL_SECONDS", "180"), MIN_TTL_SECONDS, MAX_TTL_SECONDS, "directory TTL")
    entry_url = normalize_https_url(values.get("DUNE_PUBLIC_DIRECTORY_ENTRY_URL", ""), "directory entry URL", required=enabled)
    site_url = normalize_https_url(values.get("DUNE_PUBLIC_SITE_URL", ""), "public site URL", required=enabled)
    region = clean_text(values.get("DUNE_PUBLIC_DIRECTORY_REGION") or values.get("WORLD_REGION"), 80, "directory region", required=enabled)
    if enabled and region not in REGIONS:
        raise ValueError("directory region must be one of: " + ", ".join(REGIONS))
    capacity = bounded_int(values.get("DUNE_PUBLIC_DIRECTORY_CAPACITY", "40"), 1, 1000, "directory capacity")
    key_file = pathlib.Path(values.get("DUNE_PUBLIC_DIRECTORY_KEY_FILE") or root / "config/secrets/public-directory-ed25519.pem")
    state_file = pathlib.Path(values.get("DUNE_PUBLIC_DIRECTORY_STATE_FILE") or root / "backups/public-directory/directory-entry.json")
    if not key_file.is_absolute() or not state_file.is_absolute():
        raise ValueError("directory key and state paths must be absolute")
    name_source = values.get("DUNE_PUBLIC_DIRECTORY_NAME") or values.get("PUBLIC_SERVER_NAME") or values.get("WORLD_NAME")
    description_source = values.get("DUNE_PUBLIC_DIRECTORY_DESCRIPTION") or values.get("PUBLIC_SERVER_DESCRIPTION") or values.get("DUNE_SERVER_DISPLAY_NAME")
    return {
        "enabled": enabled,
        "entryUrl": entry_url,
        "siteUrl": site_url,
        "region": region,
        "capacity": capacity,
        "ttlSeconds": ttl,
        "discordInvite": normalize_discord_invite(values.get("DUNE_PUBLIC_DIRECTORY_DISCORD_INVITE", "")),
        "name": clean_text(name_source, 120, "directory name", required=True) if enabled else "",
        "description": clean_text(description_source, 500, "directory description") if enabled else "",
        "build": clean_text(values.get("DUNE_IMAGE_TAG", "unknown"), 120, "directory build") or "unknown",
        "keyFile": key_file,
        "stateFile": state_file,
    }


def _run(command, *, input_bytes=None):
    try:
        result = subprocess.run(
            command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("OpenSSL Ed25519 operation failed") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError("OpenSSL Ed25519 operation failed" + (f": {message}" if message else ""))
    return result.stdout


def ensure_private_key(path, openssl="openssl"):
    path = pathlib.Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        lock_path = path.with_name(path.name + ".lock")
        with open(lock_path, "a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not path.exists():
                descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
                os.close(descriptor)
                try:
                    _run([openssl, "genpkey", "-algorithm", ALGORITHM, "-out", temporary])
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, path)
                finally:
                    pathlib.Path(temporary).unlink(missing_ok=True)
    details = path.lstat()
    is_systemd_credential = False
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if credential_dir and path.name == "public-directory-ed25519.pem":
        try:
            credential_root = pathlib.Path(credential_dir)
            credential_root_details = credential_root.lstat()
            is_systemd_credential = (
                path.parent.resolve(strict=True) == credential_root.resolve(strict=True)
                and stat.S_ISDIR(credential_root_details.st_mode)
                and credential_root_details.st_uid == 0
                and not credential_root_details.st_mode & 0o222
                and details.st_uid == 0
                and not details.st_mode & 0o222
                and stat.S_IMODE(details.st_mode) in (0o400, 0o440)
            )
        except OSError:
            is_systemd_credential = False
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise PermissionError("directory signing key must be a private regular file")
    if is_systemd_credential:
        if not details.st_mode & 0o444:
            raise PermissionError("systemd signing credential must be readable")
    elif details.st_mode & 0o077:
        raise PermissionError("directory signing key must be a private mode-0600 regular file")
    _run([openssl, "pkey", "-in", str(path), "-noout", "-check"])
    return path


def public_key_der(private_key, openssl="openssl"):
    return _run([openssl, "pkey", "-in", str(private_key), "-pubout", "-outform", "DER"])


def server_id_from_key(public_der):
    return "dash-" + hashlib.sha256(public_der).hexdigest()


def sign_payload(private_key, payload, openssl="openssl"):
    with tempfile.NamedTemporaryFile(prefix="dash-directory-payload.") as source:
        source.write(payload)
        source.flush()
        return _run([openssl, "pkeyutl", "-sign", "-rawin", "-inkey", str(private_key), "-in", source.name])


def _ed25519_decode_point(encoded):
    if len(encoded) != 32:
        return None
    value = int.from_bytes(encoded, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= ED25519_FIELD:
        return None
    y_squared = y * y % ED25519_FIELD
    x_squared = (y_squared - 1) * pow(ED25519_D * y_squared + 1, ED25519_FIELD - 2, ED25519_FIELD) % ED25519_FIELD
    x = pow(x_squared, (ED25519_FIELD + 3) // 8, ED25519_FIELD)
    if (x * x - x_squared) % ED25519_FIELD:
        x = x * ED25519_I % ED25519_FIELD
    if (x * x - x_squared) % ED25519_FIELD or (x == 0 and sign):
        return None
    if x & 1 != sign:
        x = ED25519_FIELD - x
    return (x, y, 1, x * y % ED25519_FIELD)


def _ed25519_add(left, right):
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = (y1 - x1) * (y2 - x2) % ED25519_FIELD
    b = (y1 + x1) * (y2 + x2) % ED25519_FIELD
    c = 2 * ED25519_D * t1 * t2 % ED25519_FIELD
    d = 2 * z1 * z2 % ED25519_FIELD
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % ED25519_FIELD, g * h % ED25519_FIELD, f * g % ED25519_FIELD, e * h % ED25519_FIELD)


def _ed25519_multiply(point, scalar):
    result = (0, 1, 1, 0)
    addend = point
    while scalar:
        if scalar & 1:
            result = _ed25519_add(result, addend)
        addend = _ed25519_add(addend, addend)
        scalar >>= 1
    return result


def _ed25519_equal(left, right):
    return (
        (left[0] * right[2] - right[0] * left[2]) % ED25519_FIELD == 0
        and (left[1] * right[2] - right[1] * left[2]) % ED25519_FIELD == 0
    )


def _verify_ed25519_python(public_der, payload, signature):
    """Strict RFC 8032 verification fallback for minimal vendor containers."""
    if (
        len(public_der) != ED25519_PUBLIC_DER_BYTES
        or not public_der.startswith(ED25519_SPKI_PREFIX)
        or len(signature) != ED25519_SIGNATURE_BYTES
    ):
        return False
    public_key = public_der[len(ED25519_SPKI_PREFIX):]
    encoded_r, encoded_s = signature[:32], signature[32:]
    scalar_s = int.from_bytes(encoded_s, "little")
    public_point = _ed25519_decode_point(public_key)
    point_r = _ed25519_decode_point(encoded_r)
    base_point = _ed25519_decode_point(bytes.fromhex("58" + "66" * 31))
    identity = (0, 1, 1, 0)
    if public_point is None or point_r is None or base_point is None or scalar_s >= ED25519_ORDER:
        return False
    if (
        not _ed25519_equal(_ed25519_multiply(public_point, ED25519_ORDER), identity)
        or not _ed25519_equal(_ed25519_multiply(point_r, ED25519_ORDER), identity)
        or _ed25519_equal(public_point, identity)
        or _ed25519_equal(point_r, identity)
    ):
        return False
    challenge = int.from_bytes(hashlib.sha512(encoded_r + public_key + payload).digest(), "little") % ED25519_ORDER
    return _ed25519_equal(
        _ed25519_multiply(base_point, scalar_s),
        _ed25519_add(point_r, _ed25519_multiply(public_point, challenge)),
    )


def verify_signature(public_der, payload, signature, openssl="openssl"):
    with tempfile.TemporaryDirectory(prefix="dash-directory-verify.") as temporary:
        root = pathlib.Path(temporary)
        public_path = root / "public.der"
        signature_path = root / "signature.bin"
        payload_path = root / "payload.json"
        public_path.write_bytes(public_der)
        signature_path.write_bytes(signature)
        payload_path.write_bytes(payload)
        try:
            result = subprocess.run(
                [openssl, "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey", str(public_path), "-sigfile", str(signature_path), "-in", str(payload_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        except OSError:
            return _verify_ed25519_python(public_der, payload, signature)
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0


def build_entry(snapshot, config, now=None, openssl="openssl"):
    if not config.get("enabled"):
        raise ValueError("public directory publication is disabled")
    now = (now or utc_now()).astimezone(dt.timezone.utc)
    key = ensure_private_key(config["keyFile"], openssl=openssl)
    public_der = public_key_der(key, openssl=openssl)
    maps = snapshot.get("mapHealth") or {}
    online_maps = bounded_int(maps.get("online", 0), 0, 1000, "online maps")
    offline_maps = bounded_int(maps.get("offline", 0), 0, 1000, "offline maps")
    state = "online" if snapshot.get("ok") and online_maps > 0 and offline_maps == 0 else "degraded" if online_maps > 0 else "offline"
    profile = {
        "name": config["name"],
        "description": config["description"],
        "region": config["region"],
        "websiteUrl": config["siteUrl"],
        "discordInvite": config["discordInvite"],
        "game": "Dune: Awakening",
        "software": "DASH",
        "features": ["dynamic-maps", "public-status", "signed-directory"],
    }
    status = {
        "state": state,
        "playersOnline": bounded_int(snapshot.get("onlineCount", 0), 0, config["capacity"], "online players"),
        "capacity": config["capacity"],
        "build": config["build"],
        "sietches": bounded_int(
            snapshot.get("sietches", sum(
                1 for row in (snapshot.get("mapStatus") or [])
                if str(row.get("map") or "").lower() in ("survival_1", "survival")
            )),
            0,
            1000,
            "directory sietches",
        ),
        "maps": {
            key_name: bounded_int(maps.get(key_name, 0), 0, 1000, f"{key_name} maps")
            for key_name in ("online", "warming", "onDemand", "offline", "total")
        },
    }
    document = {
        "schemaVersion": SCHEMA,
        "serverId": server_id_from_key(public_der),
        "generatedAt": iso8601(now),
        "expiresAt": iso8601(now + dt.timedelta(seconds=config["ttlSeconds"])),
        "sourceUrl": config["entryUrl"],
        "profile": profile,
        "status": status,
        "signingKey": {"algorithm": ALGORITHM, "publicKeyDerBase64": base64.b64encode(public_der).decode("ascii")},
    }
    payload = canonical(document)
    document["signature"] = {
        "algorithm": ALGORITHM,
        "payloadSha256": hashlib.sha256(payload).hexdigest(),
        "valueBase64": base64.b64encode(sign_payload(key, payload, openssl=openssl)).decode("ascii"),
    }
    return document


def verify_entry(document, expected_url=None, now=None, openssl="openssl"):
    if not isinstance(document, dict) or set(document) != ENTRY_KEYS or document.get("schemaVersion") != SCHEMA:
        raise ValueError("directory entry schema is invalid")
    if len(canonical(document)) > MAX_ENTRY_BYTES:
        raise ValueError("directory entry exceeds the size bound")
    source_url = normalize_https_url(document.get("sourceUrl"), "directory source URL", required=True)
    if document["sourceUrl"] != source_url:
        raise ValueError("directory entry source URL is not canonical")
    if expected_url and source_url != normalize_https_url(expected_url, "expected directory source URL", required=True):
        raise ValueError("directory entry source URL does not match its catalog source")
    profile = document.get("profile")
    status = document.get("status")
    key_info = document.get("signingKey")
    signature_info = document.get("signature")
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS or not isinstance(status, dict) or set(status) != STATUS_KEYS:
        raise ValueError("directory entry profile or status schema is invalid")
    if not isinstance(key_info, dict) or set(key_info) != {"algorithm", "publicKeyDerBase64"}:
        raise ValueError("directory signing key schema is invalid")
    if not isinstance(signature_info, dict) or set(signature_info) != {"algorithm", "payloadSha256", "valueBase64"}:
        raise ValueError("directory signature schema is invalid")
    if key_info["algorithm"] != ALGORITHM or signature_info["algorithm"] != ALGORITHM:
        raise ValueError("directory signature algorithm is unsupported")
    try:
        public_der = base64.b64decode(key_info["publicKeyDerBase64"], validate=True)
        signature = base64.b64decode(signature_info["valueBase64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("directory key or signature encoding is invalid") from exc
    if (
        len(public_der) != ED25519_PUBLIC_DER_BYTES
        or not public_der.startswith(ED25519_SPKI_PREFIX)
        or len(signature) != ED25519_SIGNATURE_BYTES
        or key_info["publicKeyDerBase64"] != base64.b64encode(public_der).decode("ascii")
        or signature_info["valueBase64"] != base64.b64encode(signature).decode("ascii")
        or document["serverId"] != server_id_from_key(public_der)
    ):
        raise ValueError("directory server identity does not match its public key")
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    payload = canonical(unsigned)
    if signature_info["payloadSha256"] != hashlib.sha256(payload).hexdigest():
        raise ValueError("directory payload digest does not match")
    if not verify_signature(public_der, payload, signature, openssl=openssl):
        raise ValueError("directory signature does not verify")
    generated = parse_time(document["generatedAt"])
    expires = parse_time(document["expiresAt"])
    now = (now or utc_now()).astimezone(dt.timezone.utc)
    if generated > now + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise ValueError("directory entry was generated in the future")
    lifetime = (expires - generated).total_seconds()
    if lifetime < MIN_TTL_SECONDS or lifetime > MAX_TTL_SECONDS:
        raise ValueError("directory entry lifetime is outside policy")
    if expires <= now:
        raise ValueError("directory entry is expired")
    if profile["name"] != clean_text(profile["name"], 120, "directory name", required=True):
        raise ValueError("directory name is not canonical")
    if profile["description"] != clean_text(profile["description"], 500, "directory description"):
        raise ValueError("directory description is not canonical")
    if profile["region"] not in REGIONS or profile["game"] != "Dune: Awakening":
        raise ValueError("directory profile region or game is invalid")
    if profile["websiteUrl"] != normalize_https_url(profile["websiteUrl"], "directory website", required=True):
        raise ValueError("directory website is not canonical")
    if profile["discordInvite"] != normalize_discord_invite(profile["discordInvite"]):
        raise ValueError("directory Discord invite is not canonical")
    if profile["software"] not in ("DASH", "Dune Docker Console", "Other"):
        raise ValueError("directory software label is invalid")
    features = profile["features"]
    if (
        not isinstance(features, list)
        or len(features) > 32
        or any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", value) for value in features)
        or len(set(features)) != len(features)
    ):
        raise ValueError("directory features are invalid")
    if status["state"] not in ("online", "degraded", "offline"):
        raise ValueError("directory status state is invalid")
    capacity = bounded_json_int(status["capacity"], 1, 1000, "directory capacity")
    bounded_json_int(status["playersOnline"], 0, capacity, "directory players online")
    bounded_json_int(status["sietches"], 0, 1000, "directory sietches")
    if not isinstance(status["maps"], dict) or set(status["maps"]) != {"online", "warming", "onDemand", "offline", "total"}:
        raise ValueError("directory map summary is invalid")
    for key, value in status["maps"].items():
        bounded_json_int(value, 0, 1000, f"directory {key} maps")
    if status["build"] != clean_text(status["build"], 120, "directory build", required=True):
        raise ValueError("directory build is not canonical")
    return document


def atomic_json(path, document, mode=0o644):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def publish(snapshot, config, output_file=None, now=None, openssl="openssl"):
    state_file = pathlib.Path(config["stateFile"])
    output_file = pathlib.Path(output_file) if output_file else None
    if not config.get("enabled"):
        state_file.unlink(missing_ok=True)
        if output_file:
            output_file.unlink(missing_ok=True)
        return {"enabled": False, "published": False}
    document = build_entry(snapshot, config, now=now, openssl=openssl)
    verify_entry(document, expected_url=config["entryUrl"], now=now, openssl=openssl)
    atomic_json(state_file, document)
    if output_file:
        atomic_json(output_file, document)
    return {"enabled": True, "published": True, "serverId": document["serverId"], "expiresAt": document["expiresAt"]}


def status(config, now=None, openssl="openssl"):
    output = {
        "enabled": bool(config.get("enabled")),
        "configured": bool(config.get("entryUrl") and config.get("siteUrl") and config.get("region")),
        "entryUrl": config.get("entryUrl") or "",
        "siteUrl": config.get("siteUrl") or "",
        "region": config.get("region") or "",
        "ttlSeconds": config.get("ttlSeconds"),
        "capacity": config.get("capacity"),
        "stateFile": str(config.get("stateFile") or ""),
        "valid": False,
        "current": False,
        "entry": None,
        "error": None,
    }
    path = pathlib.Path(config.get("stateFile") or "")
    if not output["enabled"]:
        output["state"] = "disabled"
        return output
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        verify_entry(document, expected_url=config.get("entryUrl"), now=now, openssl=openssl)
        output.update({"valid": True, "current": True, "state": document["status"]["state"], "entry": document})
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        output.update({"state": "invalid", "error": str(exc)})
    return output


def prometheus(public_status, now=None):
    now = now or utc_now()
    entry = public_status.get("entry") or {}
    try:
        expires = parse_time(entry.get("expiresAt"))
        remaining = max(0, int((expires - now).total_seconds()))
    except ValueError:
        remaining = 0
    return "\n".join([
        f"dash_public_directory_enabled {1 if public_status.get('enabled') else 0}",
        f"dash_public_directory_configured {1 if public_status.get('configured') else 0}",
        f"dash_public_directory_entry_valid {1 if public_status.get('valid') else 0}",
        f"dash_public_directory_entry_current {1 if public_status.get('current') else 0}",
        f"dash_public_directory_entry_expires_in_seconds {remaining}",
    ]) + "\n"


def require_public_dns(hostname):
    try:
        rows = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("directory source DNS resolution failed") from exc
    addresses = {row[4][0] for row in rows}
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("directory source resolves to a private or reserved address")
    return sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a prevalidated address while preserving hostname TLS checks."""

    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch_entry(url, timeout=5, connection_factory=PinnedHTTPSConnection):
    normalized = normalize_https_url(url, "directory source", required=True)
    parsed = urllib.parse.urlsplit(normalized)
    addresses = require_public_dns(parsed.hostname)
    connection = connection_factory(parsed.hostname, addresses[0], timeout)
    try:
        connection.request("GET", parsed.path or "/", headers={"User-Agent": "DASH-federated-directory/1", "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"directory source returned HTTP {response.status}")
        content_type = str(response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in ("application/json", "application/octet-stream", "text/json"):
            raise ValueError("directory source returned a non-JSON content type")
        length = response.getheader("Content-Length")
        if length and (not length.isdigit() or int(length) > MAX_ENTRY_BYTES):
            raise ValueError("directory source response exceeds the size bound")
        payload = response.read(MAX_ENTRY_BYTES + 1)
    finally:
        connection.close()
    if len(payload) > MAX_ENTRY_BYTES:
        raise ValueError("directory source response exceeds the size bound")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("directory source returned invalid JSON") from exc


def build_catalog(sources, entries, failures, now=None):
    now = now or utc_now()
    ordered = sorted(entries, key=lambda row: (row["profile"]["region"], row["profile"]["name"].casefold(), row["serverId"]))
    return {
        "schemaVersion": CATALOG_SCHEMA,
        "generatedAt": iso8601(now),
        "refreshAfter": iso8601(now + dt.timedelta(seconds=60)),
        "stats": {"configured": len(sources), "listed": len(ordered), "rejected": len(failures)},
        "servers": ordered,
        "rejected": sorted(failures, key=lambda row: row.get("source", "")),
    }
