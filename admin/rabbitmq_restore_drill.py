"""Networkless RabbitMQ recovery rehearsals for complete DASH backup sets."""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import stat
import tarfile
import time
import urllib.parse

import restore_drill


DEFAULT_IMAGE = "registry.funcom.com/funcom/self-hosting/seabass-server-rabbitmq:2036754-0-shipping"
CONTAINER_PREFIX = "dash-rmq-restore-drill-"
LABEL_KEY = "com.dash.rabbitmq-restore-drill"
ANCHOR_NAME = "head.anchor.json"
HMAC_KEY_NAME = ".receipt-hmac.key"
BROKERS = {
    "admin": {"archive": "rabbitmq-admin.tgz", "hostname": "admin-rmq", "config": "config/rabbitmq-admin.conf", "tls": False},
    "game": {"archive": "rabbitmq-game.tgz", "hostname": "game-rmq", "config": "config/rabbitmq-game.conf", "tls": True},
}
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_MEMBER_BYTES = 2 * 1024**3
HOST_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,127}")
IMAGE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9./_:@+-]{0,511}")


class RabbitMQRestoreDrillError(RuntimeError):
    pass


class RabbitMQRestoreDrillBusy(RabbitMQRestoreDrillError):
    pass


def _utc(epoch=None):
    value = time.time() if epoch is None else float(epoch)
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path, root):
    return path == root or root in path.parents


def _reject_symlink_components(path, root):
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("backup-set path may not contain symlinks")


def _bounded_regular(path, maximum):
    path = pathlib.Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    size = path.stat().st_size
    return 0 < size <= int(maximum)


def select_backup_set(workspace, requested=None):
    workspace = pathlib.Path(workspace).resolve(strict=True)
    backup_root = (workspace / "backups").resolve(strict=True)
    if requested:
        raw = pathlib.Path(requested)
        if not raw.is_absolute():
            raw = workspace / raw
        unresolved = raw.absolute()
        try:
            unresolved.relative_to(backup_root)
        except ValueError as exc:
            raise ValueError("backup set is outside the workspace backup root") from exc
        candidates = [raw.resolve(strict=True)]
        _reject_symlink_components(unresolved, backup_root)
    else:
        candidates = []
        for candidate in backup_root.iterdir():
            try:
                if candidate.is_symlink() or not candidate.is_dir() or not (candidate / "manifest.txt").is_file():
                    continue
                candidates.append(candidate.resolve(strict=True))
            except OSError:
                continue
        candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    for candidate in candidates:
        if not _within(candidate, backup_root) or candidate.is_symlink() or not candidate.is_dir():
            continue
        bounds = {
            "manifest.txt": 1024 * 1024, "config.tgz": 256 * 1024**2,
            "config-tls.tgz": 64 * 1024**2, "rabbitmq-admin.tgz": 8 * 1024**3,
            "rabbitmq-game.tgz": 8 * 1024**3,
        }
        if all(_bounded_regular(candidate / name, maximum) for name, maximum in bounds.items()):
            return candidate
    raise FileNotFoundError("no complete RabbitMQ backup set exists beneath backups/")


def _safe_name(raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("archive member name is invalid")
    name = raw
    while name.startswith("./"):
        name = name[2:]
    if name in ("", "."):
        return pathlib.PurePosixPath(".")
    pure = pathlib.PurePosixPath(name)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError("archive member escapes the staging root")
    return pure


def safe_extract_state(archive_path, destination):
    archive_path = pathlib.Path(archive_path)
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    members = files = total = 0
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                members += 1
                if members > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("RabbitMQ archive exceeds the member bound")
                pure = _safe_name(member.name)
                if pure == pathlib.PurePosixPath("."):
                    if not member.isdir():
                        raise ValueError("RabbitMQ archive root member is invalid")
                    continue
                target = destination.joinpath(*pure.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(target, 0o700)
                    continue
                if not member.isfile():
                    raise ValueError("RabbitMQ archive may contain only regular files and directories")
                if member.size < 0 or member.size > MAX_MEMBER_BYTES or total + member.size > MAX_ARCHIVE_BYTES:
                    raise ValueError("RabbitMQ archive exceeds the byte bound")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("RabbitMQ archive member is unreadable")
                with target.open("xb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("RabbitMQ archive member is truncated")
                        output.write(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        raise ValueError("RabbitMQ archive member exceeds its declared size")
                os.chmod(target, 0o600)
                total += member.size
                files += 1
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    if not (destination / "mnesia").is_dir() or files == 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise ValueError("RabbitMQ archive lacks Mnesia state")
    return {"members": members, "files": files, "bytes": total}


def _extract_exact(archive_path, member_name, destination, maximum=1024 * 1024):
    with tarfile.open(archive_path, "r:gz") as archive:
        matching = [member for member in archive.getmembers() if member.name == member_name]
        if not matching:
            raise ValueError(f"backup archive lacks required member {member_name}")
        if len(matching) != 1:
            raise ValueError(f"backup archive contains duplicate required member {member_name}")
        member = matching[0]
        if not member.isfile() or member.size <= 0 or member.size > maximum:
            raise ValueError(f"backup member {member_name} has an invalid type or size")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"backup member {member_name} is unreadable")
        value = source.read(maximum + 1)
        if len(value) != member.size or len(value) > maximum:
            raise ValueError(f"backup member {member_name} is truncated or oversized")
    destination = pathlib.Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_bytes(value)
    os.chmod(destination, 0o400)
    return destination


def _node_hostname(state_root):
    candidates = []
    for path in (pathlib.Path(state_root) / "mnesia").iterdir():
        node_type = path / "node-type.txt"
        if (
            path.is_dir() and not path.is_symlink() and path.name.startswith("rabbit@")
            and node_type.is_file() and not node_type.is_symlink()
        ):
            value = path.name.split("@", 1)[1]
            if HOST_RE.fullmatch(value):
                candidates.append(value)
    if len(set(candidates)) != 1:
        raise ValueError("RabbitMQ Mnesia state does not contain exactly one valid node directory")
    return candidates[0]


def _remove_stale_pid(state_root, hostname):
    pid = pathlib.Path(state_root) / "mnesia" / f"rabbit@{hostname}.pid"
    existed = pid.is_file() and not pid.is_symlink()
    if existed:
        pid.unlink()
    return existed


def _stage_identity(path, run_uid, run_gid, kind):
    content = (
        f"root:x:0:0:root:/root:/bin/sh\nrabbitmq:x:{run_uid}:{run_gid}:RabbitMQ:/var/lib/rabbitmq:/sbin/nologin\n"
        if kind == "passwd" else f"root:x:0:\nrabbitmq:x:{run_gid}:\n"
    )
    pathlib.Path(path).write_text(content, encoding="utf-8")
    os.chmod(path, 0o400)


def _handoff_tree(root, run_uid, run_gid, *, read_only=False):
    root = pathlib.Path(root)
    paths = [root, *root.rglob("*")]
    for path in paths:
        if path.is_symlink():
            raise ValueError("RabbitMQ staging tree may not contain symlinks")
        mode = 0o700 if path.is_dir() else 0o400 if read_only else 0o600
        os.chmod(path, mode)
        if os.geteuid() == 0:
            os.chown(path, int(run_uid), int(run_gid))
        elif path.stat().st_uid != int(run_uid):
            raise PermissionError("RabbitMQ drill cannot hand staged state to its configured container UID")


def build_container_spec(host_state, host_config, host_plugins, host_passwd, host_group, *,
                         hostname, image=DEFAULT_IMAGE, run_uid=None, run_gid=None,
                         tls_files=None, memory_bytes=1024**3, cpu_count=1.0, pids_limit=256):
    run_uid = os.getuid() if run_uid is None else int(run_uid)
    run_gid = os.getgid() if run_gid is None else int(run_gid)
    if not HOST_RE.fullmatch(hostname):
        raise ValueError("RabbitMQ drill hostname is invalid")
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise ValueError("RabbitMQ drill image reference is invalid")
    mounts = [
        {"Type": "bind", "Source": str(host_state), "Target": "/var/lib/rabbitmq", "ReadOnly": False, "BindOptions": {"Propagation": "rprivate"}},
        {"Type": "bind", "Source": str(host_config), "Target": "/etc/rabbitmq/conf.d/99-dune.conf", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}},
        {"Type": "bind", "Source": str(host_plugins), "Target": "/etc/rabbitmq/enabled_plugins", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}},
        {"Type": "bind", "Source": str(host_passwd), "Target": "/etc/passwd", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}},
        {"Type": "bind", "Source": str(host_group), "Target": "/etc/group", "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}},
    ]
    for source, target in (tls_files or {}).items():
        mounts.append({"Type": "bind", "Source": str(source), "Target": target, "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"}})
    return {
        "Image": image,
        "Hostname": hostname,
        "User": f"{run_uid}:{run_gid}",
        "Env": [
            f"HOME=/var/lib/rabbitmq", f"RABBITMQ_NODENAME=rabbit@{hostname}",
            "RABBITMQ_USE_LONGNAME=false", "RABBITMQ_LOGS=-", "RABBITMQ_SASL_LOGS=-",
            "RMQ_AUTH_BACKEND_1=cache", "RMQ_HTTP_TOKEN_AUTH_SECRET=restore-drill-no-network",
            f"FuncomLiveServices__RmqTlsEnabled={'true' if tls_files else 'false'}",
        ],
        "Labels": {LABEL_KEY: "true"},
        "StopTimeout": 30,
        "HostConfig": {
            "NetworkMode": "none", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": int(pids_limit),
            "NanoCpus": int(float(cpu_count) * 1_000_000_000), "Memory": int(memory_bytes),
            "MemorySwap": int(memory_bytes), "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Mounts": mounts,
            "Tmpfs": {
                "/tmp": f"rw,noexec,nosuid,size=268435456,uid={run_uid},gid={run_gid},mode=0700",
                "/var/log/rabbitmq": f"rw,noexec,nosuid,size=67108864,uid={run_uid},gid={run_gid},mode=0700",
            },
        },
    }


def _verify_isolation(inspected, expected):
    host = inspected.get("HostConfig") or {}
    config = inspected.get("Config") or {}
    mounts = inspected.get("Mounts") or []
    state = next((row for row in mounts if row.get("Destination") == "/var/lib/rabbitmq"), {})
    read_only_targets = {"/etc/rabbitmq/conf.d/99-dune.conf", "/etc/rabbitmq/enabled_plugins", "/etc/passwd", "/etc/group"}
    mounted = {row.get("Destination"): row for row in mounts}
    result = {
        "networkMode": host.get("NetworkMode"), "readOnlyRootfs": bool(host.get("ReadonlyRootfs")),
        "user": config.get("User"), "hostname": config.get("Hostname"),
        "capDrop": sorted(host.get("CapDrop") or []), "securityOpt": sorted(host.get("SecurityOpt") or []),
        "pidsLimit": host.get("PidsLimit"), "memoryBytes": host.get("Memory"), "nanoCpus": host.get("NanoCpus"),
        "stateCopyWritable": bool(state.get("RW")), "publishedPorts": bool(host.get("PortBindings") or {}),
        "configurationReadOnly": all(target in mounted and not mounted[target].get("RW", True) for target in read_only_targets),
    }
    result["verified"] = bool(
        result["networkMode"] == "none" and result["readOnlyRootfs"] and result["user"] == expected["User"]
        and result["hostname"] == expected["Hostname"] and "ALL" in result["capDrop"]
        and "no-new-privileges:true" in result["securityOpt"] and result["stateCopyWritable"]
        and result["configurationReadOnly"] and not result["publishedPorts"]
        and int(result["pidsLimit"] or 0) == int(expected["HostConfig"]["PidsLimit"])
        and int(result["memoryBytes"] or 0) == int(expected["HostConfig"]["Memory"])
        and int(result["nanoCpus"] or 0) == int(expected["HostConfig"]["NanoCpus"])
    )
    if not result["verified"]:
        raise RabbitMQRestoreDrillError("Docker did not apply every RabbitMQ drill isolation control")
    return result


class DockerClient(restore_drill.DockerSocketClient):
    def list_rabbitmq_drill_containers(self):
        filters = urllib.parse.quote(json.dumps({"label": [f"{LABEL_KEY}=true"]}, separators=(",", ":")))
        return json.loads(self.request("GET", f"/containers/json?all=1&filters={filters}").decode("utf-8") or "[]")


def _clean_stale_containers(docker, now_epoch, stale_seconds=6 * 3600):
    removed = []
    for row in docker.list_rabbitmq_drill_containers():
        identifier = str(row.get("Id") or "")
        names = [str(value).lstrip("/") for value in (row.get("Names") or [])]
        labels = row.get("Labels") or {}
        if len(identifier) < 12 or labels.get(LABEL_KEY) != "true" or not any(name.startswith(CONTAINER_PREFIX) for name in names):
            continue
        if row.get("State") != "running" or int(row.get("Created") or 0) <= int(now_epoch - stale_seconds):
            docker.remove_container(identifier, force=True)
            removed.append(identifier[:12])
    return removed


def _clean_stale_stages(receipt_root, now_epoch, stale_seconds=6 * 3600):
    removed = []
    for path in pathlib.Path(receipt_root).glob(".stage-*"):
        try:
            if path.is_symlink() or not path.is_dir() or path.stat().st_mtime > now_epoch - stale_seconds:
                continue
            shutil.rmtree(path)
            removed.append(path.name)
        except OSError:
            continue
    return sorted(removed)


def _exec(docker, container, argv, label, timeout=60):
    code, output = docker.exec(container, argv, timeout=timeout)
    if code != 0:
        raise RabbitMQRestoreDrillError(f"isolated RabbitMQ {label} failed")
    return output


def _bounded_lines(value, limit=512):
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    if len(lines) > limit or any(len(line) > 512 for line in lines):
        raise RabbitMQRestoreDrillError("isolated RabbitMQ topology output exceeded its bound")
    return lines


def _topology(docker, container):
    vhosts = _bounded_lines(_exec(docker, container, ["rabbitmqctl", "-q", "list_vhosts", "name"], "vhost inventory"), 128)
    # RabbitMQ's list_users command has a fixed user/tags result shape and does
    # not accept a column selector (unlike the other list_* commands).
    users = _bounded_lines(_exec(docker, container, ["rabbitmqctl", "-q", "list_users"], "user inventory"), 256)
    if not vhosts or not users:
        raise RabbitMQRestoreDrillError("restored RabbitMQ lacks required vhost or user state")
    totals = {"vhosts": len(vhosts), "users": len(users), "queues": 0, "exchanges": 0, "bindings": 0, "messages": 0}
    for vhost in vhosts:
        queues = _bounded_lines(_exec(docker, container, ["rabbitmqctl", "-q", "list_queues", "-p", vhost, "name", "messages"], "queue inventory"), 4096)
        exchanges = _bounded_lines(_exec(docker, container, ["rabbitmqctl", "-q", "list_exchanges", "-p", vhost, "name", "type", "durable"], "exchange inventory"), 4096)
        bindings = _bounded_lines(_exec(docker, container, ["rabbitmqctl", "-q", "list_bindings", "-p", vhost, "source_name", "destination_name"], "binding inventory"), 8192)
        totals["queues"] += len(queues)
        totals["exchanges"] += len(exchanges)
        totals["bindings"] += len(bindings)
        for line in queues:
            try:
                totals["messages"] += max(0, int(line.rsplit(None, 1)[1]))
            except (IndexError, ValueError):
                continue
    return totals


def _atomic_json(path, payload, owner_uid=None, owner_gid=None):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n")
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if os.geteuid() == 0 and owner_uid is not None and owner_gid is not None:
            os.chown(temporary, int(owner_uid), int(owner_gid))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt_secret(root, owner_uid=None, owner_gid=None, *, create=False):
    path = pathlib.Path(root) / HMAC_KEY_NAME
    if create and not path.exists():
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, secrets.token_bytes(32))
            os.fsync(descriptor)
            if os.geteuid() == 0 and owner_uid is not None and owner_gid is not None:
                os.fchown(descriptor, int(owner_uid), int(owner_gid))
        finally:
            os.close(descriptor)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RabbitMQRestoreDrillError("RabbitMQ recovery receipt HMAC key is missing or not private")
    value = path.read_bytes()
    if len(value) != 32:
        raise RabbitMQRestoreDrillError("RabbitMQ recovery receipt HMAC key must contain exactly 32 bytes")
    return value


def _anchor_document(rows, secret):
    document = {
        "anchorSchema": 1,
        "headId": rows[0]["id"] if rows else None,
        "headSha256": rows[0]["receiptSha256"] if rows else None,
        "retainedCount": len(rows),
        "oldestRetainedSha256": rows[-1]["receiptSha256"] if rows else None,
        "oldestPreviousSha256": rows[-1].get("previousReceiptSha256") if rows else None,
        "updatedAt": _utc(),
    }
    document["hmacSha256"] = hmac.new(secret, _canonical(document), hashlib.sha256).hexdigest()
    return document


def verify_history(root):
    root = pathlib.Path(root)
    rows = list_receipts(root, 100_000)
    anchor_path = root / ANCHOR_NAME
    if not rows and not anchor_path.exists():
        return {"ok": True, "receipts": 0, "anchored": False}
    try:
        secret = _receipt_secret(root)
        if anchor_path.is_symlink() or not anchor_path.is_file() or not 1 <= anchor_path.stat().st_size <= 64 * 1024:
            raise ValueError("anchor is not a bounded regular file")
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        claimed = anchor.get("hmacSha256")
        unsigned = dict(anchor)
        unsigned.pop("hmacSha256", None)
        signature_valid = bool(
            isinstance(claimed, str)
            and re.fullmatch(r"[0-9a-f]{64}", claimed)
            and hmac.compare_digest(claimed, hmac.new(secret, _canonical(unsigned), hashlib.sha256).hexdigest())
        )
        expected = {
            "headId": rows[0].get("id") if rows else None,
            "headSha256": rows[0].get("receiptSha256") if rows else None,
            "retainedCount": len(rows),
            "oldestRetainedSha256": rows[-1].get("receiptSha256") if rows else None,
            "oldestPreviousSha256": rows[-1].get("previousReceiptSha256") if rows else None,
        }
        binding_valid = all(anchor.get(key) == value for key, value in expected.items())
        chain_valid = all(row.get("receiptHashValid") and row.get("receiptChainValid") for row in rows)
        return {
            "ok": bool(signature_valid and binding_valid and chain_valid),
            "receipts": len(rows),
            "anchored": True,
            "anchorHmacValid": signature_valid,
            "anchorBindingValid": binding_valid,
            "receiptChainValid": chain_valid,
        }
    except (OSError, ValueError, json.JSONDecodeError, RabbitMQRestoreDrillError):
        return {"ok": False, "receipts": len(rows), "anchored": anchor_path.exists(), "error": "receipt history anchor is invalid"}


def _write_receipt(root, receipt, owner_uid=None, owner_gid=None, retention=1000):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    if os.geteuid() == 0 and owner_uid is not None and owner_gid is not None:
        os.chown(root, int(owner_uid), int(owner_gid))
    latest = root / "latest.json"
    history = verify_history(root)
    existing = list_receipts(root, 100_000)
    if existing and not history.get("ok"):
        raise RabbitMQRestoreDrillError("existing RabbitMQ recovery receipt chain is invalid")
    previous = existing[0].get("receiptSha256") if existing else None
    receipt["previousReceiptSha256"] = previous
    receipt["receiptSha256"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    _atomic_json(root / f"{receipt['id']}.json", receipt, owner_uid, owner_gid)
    _atomic_json(latest, receipt, owner_uid, owner_gid)
    files = sorted(path for path in root.glob("*.json") if path.name not in {"latest.json", ANCHOR_NAME})
    for path in files[:max(0, len(files) - max(10, int(retention)))]:
        path.unlink(missing_ok=True)
    retained = list_receipts(root, 100_000)
    secret = _receipt_secret(root, owner_uid, owner_gid, create=True)
    _atomic_json(root / ANCHOR_NAME, _anchor_document(retained, secret), owner_uid, owner_gid)


def verify_receipt_document(payload):
    if not isinstance(payload, dict):
        return False
    claimed = payload.get("receiptSha256")
    unsigned = dict(payload)
    unsigned.pop("receiptSha256", None)
    return bool(
        isinstance(payload.get("id"), str)
        and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", payload["id"])
        and isinstance(claimed, str)
        and re.fullmatch(r"[0-9a-f]{64}", claimed)
        and hashlib.sha256(_canonical(unsigned)).hexdigest() == claimed
    )


def list_receipts(root, limit=20):
    rows = []
    paths = sorted((item for item in pathlib.Path(root).glob("*.json") if item.name not in {"latest.json", ANCHOR_NAME}), reverse=True)
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 2 * 1024 * 1024:
                raise ValueError("receipt is not a bounded regular file")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["receiptHashValid"] = bool(
                verify_receipt_document(payload)
                and path.name == f"{payload['id']}.json"
            )
            rows.append(payload)
        except (OSError, ValueError, json.JSONDecodeError):
            rows.append({"id": path.stem, "ok": False, "receiptHashValid": False, "error": "receipt is unreadable"})
    for index, row in enumerate(rows):
        adjacent_valid = index + 1 >= len(rows) or row.get("previousReceiptSha256") == rows[index + 1].get("receiptSha256")
        row["receiptChainValid"] = bool(row.get("receiptHashValid") and adjacent_valid)
    return rows[:max(1, min(int(limit), 100_000))]


def status(root, limit=20):
    rows = list_receipts(root, limit)
    latest = rows[0] if rows else None
    history = verify_history(root)
    return {
        "ok": bool(latest and latest.get("ok") and latest.get("receiptHashValid") and latest.get("receiptChainValid") and history.get("ok")),
        "latest": latest,
        "receipts": rows,
        "history": history,
    }


def _run_one(docker, name, state, stage, host_stage, image, run_uid, run_gid,
             memory_bytes, cpu_count, pids_limit, readiness_seconds, sleep):
    definition = BROKERS[name]
    hostname = _node_hostname(state)
    if hostname != definition["hostname"]:
        raise RabbitMQRestoreDrillError(f"{name} backup node identity does not match the expected broker")
    stale_pid_removed = _remove_stale_pid(state, hostname)
    config_dir = stage / f"{name}-config"
    config_dir.mkdir(mode=0o700)
    config_file = _extract_exact(stage / "config.tgz", definition["config"], config_dir / "99-dune.conf")
    plugins = _extract_exact(stage / "config.tgz", "config/rabbitmq-enabled-plugins", config_dir / "enabled_plugins")
    passwd = config_dir / "passwd"; group = config_dir / "group"
    _stage_identity(passwd, run_uid, run_gid, "passwd"); _stage_identity(group, run_uid, run_gid, "group")
    tls = {}
    if definition["tls"]:
        tls_dir = config_dir / "tls"; tls_dir.mkdir(mode=0o700)
        for source_name, target_name in (("ca.crt", "cacert.pem"), ("server.crt", "cert.pem"), ("server.key", "key.pem")):
            local = _extract_exact(stage / "config-tls.tgz", f"config/tls/rabbitmq/{source_name}", tls_dir / source_name, 1024 * 1024)
            tls[local] = f"/etc/rabbitmq/{target_name}"
    _handoff_tree(state, run_uid, run_gid)
    _handoff_tree(config_dir, run_uid, run_gid, read_only=True)
    relative = lambda path: host_stage / pathlib.Path(path).relative_to(stage)
    spec = build_container_spec(
        relative(state), relative(config_file), relative(plugins), relative(passwd), relative(group),
        hostname=hostname, image=image, run_uid=run_uid, run_gid=run_gid,
        tls_files={relative(source): target for source, target in tls.items()}, memory_bytes=memory_bytes,
        cpu_count=cpu_count, pids_limit=pids_limit,
    )
    container_name = f"{CONTAINER_PREFIX}{name}-{secrets.token_hex(5)}"
    container = docker.create_container(container_name, spec)
    started = time.monotonic()
    try:
        docker.start_container(container)
        isolation = _verify_isolation(docker.inspect_container(container), spec)
        ready = False
        while time.monotonic() - started < readiness_seconds:
            # `ping` only proves that the Erlang VM is reachable.  The RabbitMQ
            # application can still be completing its boot sequence, in which
            # case `rabbitmqctl status` and every topology query will fail.
            # `check_running` does not succeed until the recovered `rabbit`
            # application itself is running.
            code, _ = docker.exec(container, ["rabbitmq-diagnostics", "-q", "check_running"], timeout=60)
            if code == 0:
                ready = True; break
            sleep(1)
        if not ready:
            log_hash = hashlib.sha256(docker.logs(container).encode("utf-8", errors="replace")).hexdigest()
            raise RabbitMQRestoreDrillError(f"isolated {name} RabbitMQ did not become ready; logSha256={log_hash}")
        _exec(docker, container, ["rabbitmqctl", "-q", "status"], "status", timeout=30)
        topology = _topology(docker, container)
        return {"ok": True, "readySeconds": round(time.monotonic() - started, 3), "isolation": isolation,
                "topology": topology, "stalePidRemoved": stale_pid_removed}
    finally:
        docker.remove_container(container, force=True)


def run_drill(workspace, *, host_workspace=None, backup_set=None, receipt_root=None, docker=None,
              docker_socket="/var/run/docker.sock", image=DEFAULT_IMAGE, max_backup_age_seconds=36 * 3600,
              readiness_seconds=180, memory_bytes=1024**3, cpu_count=1.0, pids_limit=256,
              run_uid=None, run_gid=None, retention=1000, sleep=time.sleep):
    workspace = pathlib.Path(workspace).resolve(strict=True)
    host_workspace = pathlib.Path(host_workspace or workspace)
    if not host_workspace.is_absolute():
        raise ValueError("host workspace path must be absolute")
    run_uid = os.getuid() if run_uid is None else int(run_uid)
    run_gid = os.getgid() if run_gid is None else int(run_gid)
    if run_uid <= 0 or run_gid < 0:
        raise ValueError("RabbitMQ restore drill must run as a non-root UID/GID")
    backup_root = (workspace / "backups").resolve(strict=True)
    receipt_root = pathlib.Path(receipt_root or backup_root / "admin-panel" / "rabbitmq-restore-drills")
    if not receipt_root.is_absolute():
        receipt_root = workspace / receipt_root
    unresolved_receipt_root = receipt_root.absolute()
    try:
        unresolved_receipt_root.relative_to(backup_root)
    except ValueError as exc:
        raise ValueError("RabbitMQ drill receipts must remain beneath workspace/backups") from exc
    existing_parent = unresolved_receipt_root
    while not existing_parent.exists() and existing_parent != backup_root:
        existing_parent = existing_parent.parent
    if existing_parent.is_symlink() or not _within(existing_parent.resolve(strict=True), backup_root):
        raise ValueError("RabbitMQ drill receipt path contains an unsafe parent")
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_root = receipt_root.resolve(strict=True)
    if not _within(receipt_root, backup_root):
        raise ValueError("RabbitMQ drill receipts must remain beneath workspace/backups")
    os.chmod(receipt_root, 0o700)
    if os.geteuid() == 0:
        os.chown(receipt_root, run_uid, run_gid)
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(receipt_root / ".rabbitmq-restore-drill.lock", lock_flags, 0o600)
    if os.geteuid() == 0:
        os.fchown(lock_fd, run_uid, run_gid)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RabbitMQRestoreDrillBusy("another RabbitMQ restore drill is running") from exc
        selected = select_backup_set(workspace, backup_set)
        started_epoch = time.time(); started_mono = time.monotonic()
        identifier = datetime.datetime.fromtimestamp(started_epoch, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
        stage = receipt_root / f".stage-{identifier}"
        stage.mkdir(mode=0o700)
        host_stage = host_workspace / stage.relative_to(workspace)
        age = max(0.0, started_epoch - selected.stat().st_mtime)
        receipt = {"receiptSchema": 1, "id": identifier, "startedAt": _utc(started_epoch),
                   "backupSet": selected.relative_to(workspace).as_posix(), "backupAgeSeconds": round(age, 3),
                   "image": image, "liveRabbitMQTouched": False, "networkCreated": False,
                   "brokers": {}, "integrityOk": False, "policyOk": False, "ok": False}
        docker = docker or DockerClient(docker_socket)
        try:
            receipt["cleanup"] = {
                "staleContainersRemoved": _clean_stale_containers(docker, started_epoch),
                "staleStagesRemoved": _clean_stale_stages(receipt_root, started_epoch),
            }
            shutil.copyfile(selected / "config.tgz", stage / "config.tgz")
            shutil.copyfile(selected / "config-tls.tgz", stage / "config-tls.tgz")
            receipt["configurationArchives"] = {
                "configSha256": _sha256(selected / "config.tgz"),
                "tlsSha256": _sha256(selected / "config-tls.tgz"),
            }
            for name, definition in BROKERS.items():
                archive = selected / definition["archive"]
                archive_sha = _sha256(archive)
                state = stage / f"{name}-state"
                extraction = safe_extract_state(archive, state)
                proof = _run_one(docker, name, state, stage, host_stage, image, run_uid, run_gid,
                                 memory_bytes, cpu_count, pids_limit, readiness_seconds, sleep)
                receipt["brokers"][name] = {**proof, "sourceSha256": archive_sha, "extraction": extraction}
                shutil.rmtree(state)
            receipt["integrityOk"] = all(row.get("ok") and (row.get("isolation") or {}).get("verified") for row in receipt["brokers"].values())
            receipt["policy"] = {"maxBackupAgeSeconds": int(max_backup_age_seconds), "backupAgeWithinTarget": age <= max_backup_age_seconds}
            receipt["policyOk"] = receipt["policy"]["backupAgeWithinTarget"]
            receipt["ok"] = receipt["integrityOk"] and receipt["policyOk"]
        except Exception as exc:
            receipt["error"] = str(exc)[-2000:]
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            receipt.setdefault("cleanup", {})["stageRemoved"] = not stage.exists()
            if not receipt["cleanup"]["stageRemoved"]:
                receipt["ok"] = False; receipt["integrityOk"] = False
            receipt["finishedAt"] = _utc(); receipt["durationSeconds"] = round(time.monotonic() - started_mono, 3)
            _write_receipt(receipt_root, receipt, run_uid, run_gid, retention)
        return receipt
    finally:
        os.close(lock_fd)
