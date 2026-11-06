#!/usr/bin/env python3
"""Generate and verify exact source manifests for assured DASH deployments."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "admin"))

import deployment_assurance


SCHEMA = "dune-deployment-manifest/v1"
SUPPORT_FILES = {
    # Admin owns the server-side backup verifier dispatch. Keep the entrypoint
    # manifest-bound with every assured deployment so a verifier schema update
    # cannot promote its helpers without promoting the running caller.
    "admin/admin_panel.py",
    "admin/audit_ledger.py",
    "admin/change_approvals.py",
    "admin/change_intelligence.py",
    "admin/community_canary.py",
    "admin/community_rewards.py",
    "admin/creator_canary.py",
    "admin/public_ip_canary.py",
    "admin/canary_autopilot.py",
    "admin/operations_briefing.py",
    "admin/operations_calendar.py",
    "admin/alert_inbox.py",
    "admin/peer_watch.py",
    "admin/player_life_recovery.py",
    "admin/offline_teleport.py",
    "docs/ecosystem-feature-parity-audit.md",
    "admin/addon_admin.py",
    "admin/base_creator.py",
    "admin/base_retirement.py",
    "admin/cosmetics_admin.py",
    "admin/gameplay_presets.py",
    "admin/credential_lifecycle.py",
    "admin/deployment_assurance.py",
    "admin/desired_state.py",
    "admin/feature_readiness_history.py",
    "admin/maintenance_planner.py",
    "admin/maintenance_outcomes.py",
    "admin/rabbitmq_restore_drill.py",
    "admin/restore_drill.py",
    "admin/update_readiness.py",
    "admin/runtime_security.py",
    "scripts/deployment-assurance.py",
    "scripts/deploy-admin-panel.sh",
    "scripts/push-assured-control-plane.sh",
    "scripts/rollout-runtime-security.py",
    "scripts/check-current-steam-build.py",
    "scripts/check-steam-update.sh",
    "scripts/snapshot-audit-ledger.py",
    "scripts/test-rollout-runtime-security.py",
    "scripts/check-admin-ingress.sh",
    "scripts/test-cosmetics-admin.py",
    "scripts/test-restore-drill.py",
    "scripts/test-operational-slo.py",
    "scripts/test-capacity-intelligence.py",
    "scripts/test-desired-state.py",
    "scripts/test-change-intelligence.py",
    "scripts/test-deployment-assurance.py",
    "scripts/test-admin-panel-safe-surfaces.py",
    "scripts/test-docker-api-proxy.py",
    "scripts/test-deploy-admin-panel.py",
    "scripts/assured-control-plane-deploy.sh",
    "scripts/lib/snape-curl-auth.sh",
    "scripts/secret_env.py",
    "scripts/http_url.py",
    "scripts/test-security-boundaries.py",
    "scripts/backup-state.sh",
    "scripts/snapshot_operator_evidence.py",
    "scripts/test-snapshot-operator-evidence.py",
    "scripts/restore-state.sh",
    "scripts/restore-alert-inbox.sh",
    "scripts/verify-backup.sh",
    "scripts/public-ip-monitor.sh",
    "scripts/generate-rabbitmq-cert.sh",
    "scripts/check-rabbitmq-cert-sans.sh",
    "scripts/restart-target.sh",
    "scripts/map_catalog.py",
    "scripts/install-public-ip-monitor.sh",
    "config/systemd/dune-public-ip-monitor.service",
    "config/systemd/dune-public-ip-monitor.timer",
}


def git(*arguments):
    completed = subprocess.run(["git", *arguments], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def git_bytes(*arguments):
    completed = subprocess.run(["git", *arguments], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        raise ValueError(completed.stderr.decode("utf-8", errors="replace").strip() or "git command failed")
    return completed.stdout


def resolve_commit(value):
    commit = git("rev-parse", "--verify", f"{value}^{{commit}}")
    if not deployment_assurance.COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("resolved Git commit id is invalid")
    return commit


def selected_files(commit, base=None, explicit=None):
    if explicit:
        raw = explicit
    else:
        parent = resolve_commit(base) if base else git("rev-parse", f"{commit}^")
        raw = git("diff", "--name-only", "--diff-filter=ACMRT", parent, commit).splitlines()
    files = sorted(set(deployment_assurance.safe_relative_path(value) for value in raw if str(value).strip()) | SUPPORT_FILES)
    if not files:
        raise ValueError("deployment manifest selection is empty")
    return files


def generate(commit="HEAD", base=None, explicit=None, reason=""):
    resolved = resolve_commit(commit)
    rows = []
    for relative in selected_files(resolved, base=base, explicit=explicit):
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"deployment manifest source is not a regular file: {relative}")
        committed = git_bytes("show", f"{resolved}:{relative}")
        committed_sha = hashlib.sha256(committed).hexdigest()
        current_sha = deployment_assurance.file_sha256(path)
        if current_sha != committed_sha:
            raise ValueError(f"workspace file does not match exact deployment commit: {relative}")
        rows.append({"path": relative, "sha256": committed_sha})
    normalized = deployment_assurance.normalize_manifest(ROOT, rows)
    files = [{key: row[key] for key in ("path", "sha256", "bytes")} for row in normalized]
    return {
        "schemaVersion": SCHEMA, "commit": resolved, "reason": str(reason or "")[:1000],
        "files": files, "manifestSha256": deployment_assurance.digest(files),
    }


def verify(document, workspace=ROOT):
    if not isinstance(document, dict) or set(document) - {"schemaVersion", "commit", "reason", "files", "manifestSha256"}:
        raise ValueError("deployment manifest fields are invalid")
    if document.get("schemaVersion") != SCHEMA or not deployment_assurance.COMMIT_PATTERN.fullmatch(str(document.get("commit") or "")):
        raise ValueError("deployment manifest schema or commit is invalid")
    normalized = deployment_assurance.normalize_manifest(workspace, document.get("files"))
    files = [{key: row[key] for key in ("path", "sha256", "bytes")} for row in normalized]
    expected = deployment_assurance.digest(files)
    if document.get("manifestSha256") != expected:
        raise ValueError("deployment manifest digest is invalid")
    return {"ok": True, "commit": document["commit"], "files": len(files), "manifestSha256": expected}


def validate_document(document):
    if not isinstance(document, dict) or set(document) - {"schemaVersion", "commit", "reason", "files", "manifestSha256"}:
        raise ValueError("deployment manifest fields are invalid")
    if document.get("schemaVersion") != SCHEMA or not deployment_assurance.COMMIT_PATTERN.fullmatch(str(document.get("commit") or "")):
        raise ValueError("deployment manifest schema or commit is invalid")
    files = deployment_assurance.validate_manifest_rows(document.get("files"), require_bytes=True)
    expected = deployment_assurance.digest(files)
    if document.get("manifestSha256") != expected:
        raise ValueError("deployment manifest digest is invalid")
    return files


def archive_rollback(document, workspace, output):
    files = validate_document(document)
    workspace = pathlib.Path(workspace).resolve()
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    descriptor, temporary = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
    os.close(descriptor)
    pathlib.Path(temporary).unlink(missing_ok=True)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for row in files:
                target = (workspace / row["path"]).resolve()
                try:
                    target.relative_to(workspace)
                except ValueError as exc:
                    raise ValueError(f"rollback path escapes workspace: {row['path']}") from exc
                existed = target.is_file() and not target.is_symlink()
                record = {"path": row["path"], "existed": existed}
                if existed:
                    if target.stat().st_size > deployment_assurance.MAX_FILE_BYTES:
                        raise ValueError(f"rollback source is oversized: {row['path']}")
                    record.update({"sha256": deployment_assurance.file_sha256(target), "bytes": target.stat().st_size, "mode": target.stat().st_mode & 0o777})
                    archive.add(target, arcname=f"files/{row['path']}", recursive=False)
                records.append(record)
            metadata = json.dumps({
                "schemaVersion": "dune-deployment-rollback/v1", "commit": document["commit"],
                "targetManifestSha256": document["manifestSha256"], "files": records,
            }, indent=2, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo("rollback-manifest.json")
            info.size = len(metadata)
            info.mode = 0o600
            info.mtime = 0
            import io
            archive.addfile(info, io.BytesIO(metadata))
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    return {"ok": True, "path": str(output), "bytes": output.stat().st_size, "sha256": deployment_assurance.file_sha256(output), "files": len(files)}


def apply_manifest(document, source, workspace):
    files = validate_document(document)
    source = pathlib.Path(source).resolve()
    workspace = pathlib.Path(workspace).resolve()
    verify(document, workspace=source)
    for row in files:
        source_path = (source / row["path"]).resolve()
        target = (workspace / row["path"]).resolve()
        try:
            source_path.relative_to(source)
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"deployment apply path escapes workspace: {row['path']}") from exc
        if target.is_symlink():
            raise ValueError(f"deployment apply target is a symlink: {row['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".deploy-" + os.urandom(8).hex())
        try:
            with source_path.open("rb") as input_handle, temporary.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            mode = source_path.stat().st_mode & 0o111
            os.chmod(temporary, 0o755 if mode else 0o644)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return verify(document, workspace=workspace)


def atomic_write(path, document):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


RUNTIME_WINDOW_QUERY = """
import json, os, pathlib, sys
root = pathlib.Path(os.environ.get("ADMIN_WORKSPACE", "/workspace"))
sys.path.insert(0, str(root / "admin"))
import deployment_assurance, change_intelligence
state = pathlib.Path(os.environ.get("DUNE_DEPLOYMENT_ASSURANCE_STATE_DIR", str(root / "backups/deployment-assurance")))
evidence = pathlib.Path(os.environ.get("DUNE_CHANGE_INTELLIGENCE_EVIDENCE_DIR", str(root / "backups/operator-evidence")))
secret = pathlib.Path(os.environ.get("DUNE_CHANGE_INTELLIGENCE_HMAC_SECRET_FILE", str(root / "config/secrets/change-intelligence-hmac.secret")))
if not secret.is_file() or secret.is_symlink():
    raise ValueError("configured verification material is unavailable")
store = deployment_assurance.Store(state, evidence, root, change_intelligence.read_secret(secret))
path = store._window_path(sys.argv[1])
if not path.is_file() or path.is_symlink() or not 1 <= path.stat().st_size <= 4 * 1024 * 1024:
    raise ValueError("window state is unavailable or outside its size bound")
window = store._load_window(sys.argv[1])
print(json.dumps({key: window.get(key) for key in ("id", "commit", "reason", "manifestSha256", "status")}))
"""


def runtime_window_metadata(window_id, container):
    """Read existing signed runtime metadata; no key or signature leaves it."""
    if not deployment_assurance.WINDOW_PATTERN.fullmatch(str(window_id)):
        raise ValueError("deployment assurance window id is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(container)):
        raise ValueError("Admin container name is invalid")
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-", window_id],
        input=RUNTIME_WINDOW_QUERY, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if result.returncode:
        raise ValueError("runtime window HMAC verification failed")
    document = json.loads(result.stdout)
    if (not isinstance(document, dict) or document.get("id") != window_id
            or not deployment_assurance.HASH_PATTERN.fullmatch(str(document.get("manifestSha256") or ""))):
        raise ValueError("verified runtime window metadata is invalid")
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest = subparsers.add_parser("manifest", help="Generate an exact commit-bound source manifest")
    manifest.add_argument("--commit", default="HEAD")
    manifest.add_argument("--base")
    manifest.add_argument("--file", action="append", dest="files")
    manifest.add_argument("--reason", default="")
    manifest.add_argument("--output", required=True)
    verify_parser = subparsers.add_parser("verify", help="Verify a manifest against the current workspace")
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--workspace", default=str(ROOT))
    archive_parser = subparsers.add_parser("archive", help="Archive current target files for rollback before applying a manifest")
    archive_parser.add_argument("--manifest", required=True)
    archive_parser.add_argument("--workspace", default=str(ROOT))
    archive_parser.add_argument("--output", required=True)
    apply_parser = subparsers.add_parser("apply", help="Atomically apply staged manifest files into a target workspace")
    apply_parser.add_argument("--manifest", required=True)
    apply_parser.add_argument("--source", required=True)
    apply_parser.add_argument("--workspace", required=True)
    runtime_parser = subparsers.add_parser("runtime-window", help="Read HMAC-verified window metadata from an existing Admin runtime")
    runtime_parser.add_argument("--window-id", required=True)
    runtime_parser.add_argument("--container", required=True)
    args = parser.parse_args()
    try:
        if args.command == "manifest":
            document = generate(args.commit, base=args.base, explicit=args.files, reason=args.reason)
            atomic_write(args.output, document)
            result = verify(document)
            result["path"] = str(pathlib.Path(args.output))
        elif args.command == "verify":
            result = verify(json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8")), workspace=args.workspace)
        elif args.command == "runtime-window":
            result = runtime_window_metadata(args.window_id, args.container)
        elif args.command == "archive":
            result = archive_rollback(json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8")), args.workspace, args.output)
        else:
            result = apply_manifest(json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8")), args.source, args.workspace)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
