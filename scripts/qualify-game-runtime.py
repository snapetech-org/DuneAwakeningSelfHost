#!/usr/bin/env python3
"""Build, inspect, and scan one version-bound Dune game runtime image.

This command stages evidence only. It never edits Compose configuration or
restarts the farm. An approval is written only after every independent check
passes, and the existing runtime-security verifier reads that approval.
"""

from __future__ import annotations

import argparse
import fcntl
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
IMAGE_DIR = ROOT / "images" / "game-security"
sys.path.insert(0, str(ROOT / "admin"))
import runtime_security

TRIVY_IMAGE = "aquasec/trivy:0.76.2"
TRIVY_ID = "sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"
SITE = "home/dune/server/Engine/Binaries/ThirdParty/Python3/Linux/lib/python3.9/site-packages/"
ALLOWED = re.compile(r"^(?:setuptools(?:/|-)|_distutils_hack/|pkg_resources/|distutils-precedence\.pth$)")
SERVER = "home/dune/server/"
GAME_BINARY = "/home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping"
EMBEDDED_LIB = "/home/dune/server/Engine/Binaries/ThirdParty/Python3/Linux/lib/libpython3.9.so.1.0"


def run(*args: str, output: pathlib.Path | None = None, **kwargs) -> str:
    if output is None:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, **kwargs).strip()
    with output.open("wb") as handle:
        subprocess.run(args, check=True, stdout=handle, **kwargs)
    return ""


def image_inspect(reference: str) -> dict:
    return json.loads(run("docker", "image", "inspect", reference))[0]


def env_value(path: pathlib.Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("\"'")
    return ""


def require_private_directory(path: pathlib.Path) -> None:
    if path.is_symlink():
        raise ValueError(f"directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f"directory is not owner-private: {path}")


def export_image(image: str, target: pathlib.Path) -> None:
    container = run("docker", "create", image)
    try:
        run("docker", "export", "-o", str(target), container)
    finally:
        subprocess.run(["docker", "rm", container], stdout=subprocess.DEVNULL, check=True)


def server_entries(archive: pathlib.Path) -> dict[str, tuple]:
    entries: dict[str, tuple] = {}
    with tarfile.open(archive, "r:") as stream:
        for member in stream:
            name = member.name.lstrip("./")
            if not name.startswith(SERVER) or member.isdir():
                continue
            if member.isfile():
                source = stream.extractfile(member)
                if source is None:
                    raise ValueError(f"unreadable image member: {name}")
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                value = ("file", member.mode, digest.hexdigest())
            elif member.issym() or member.islnk():
                value = ("link", member.mode, member.linkname)
            else:
                value = ("other", member.mode, member.type)
            if name in entries:
                raise ValueError(f"duplicate image member: {name}")
            entries[name] = value
    return entries


def compare_payload(vendor_tar: pathlib.Path, candidate_tar: pathlib.Path) -> tuple[int, int]:
    vendor = server_entries(vendor_tar)
    candidate = server_entries(candidate_tar)
    if not vendor or not candidate or GAME_BINARY.lstrip("/") not in vendor:
        raise ValueError("server payload or game binary is missing")
    changed = [name for name in vendor.keys() | candidate.keys() if vendor.get(name) != candidate.get(name)]
    unexpected = [name for name in changed if not name.startswith(SITE) or not ALLOWED.match(name[len(SITE):])]
    if unexpected:
        raise ValueError(f"game payload changed outside embedded packaging: {unexpected[:8]}")
    if not changed:
        raise ValueError("candidate did not replace embedded packaging")
    return len(vendor), len(changed)


def check_config(vendor: dict, candidate: dict) -> None:
    base = vendor.get("RootFS", {}).get("Layers") or []
    layers = candidate.get("RootFS", {}).get("Layers") or []
    if not base or layers[:len(base)] != base or len(layers) <= len(base):
        raise ValueError("candidate does not preserve the exact vendor base layers")
    if vendor.get("Config") != candidate.get("Config"):
        raise ValueError("candidate changed image runtime configuration")


def check_native(image: str) -> None:
    for binary in (GAME_BINARY, EMBEDDED_LIB):
        output = run("docker", "run", "--rm", "--network", "none", "--entrypoint", "ldd", image, binary)
        if "not found" in output:
            raise ValueError(f"native dependency missing for {binary}")


def check_embedded(image: str, scratch: pathlib.Path) -> None:
    binary = scratch / "embedded-python-check"
    run("gcc", "-O2", "-Wall", "-Wextra", str(IMAGE_DIR / "embedded-python-check.c"), "-ldl", "-o", str(binary))
    run("docker", "run", "--rm", "--network", "none", "--read-only",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{binary}:/qualify/embedded-python-check:ro",
        "--entrypoint", "/qualify/embedded-python-check", image)


def scan_image(image: str, scratch: pathlib.Path) -> tuple[dict, pathlib.Path, str]:
    scanner = image_inspect(TRIVY_IMAGE)
    if scanner["Id"] != TRIVY_ID:
        raise ValueError("pinned Trivy scanner image is unavailable or changed")
    tar_path = scratch / "candidate-image.tar"
    run("docker", "save", "-o", str(tar_path), image)
    with tarfile.open(tar_path, "r:") as archive:
        manifest_file = archive.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("Docker image archive has no manifest")
        manifest = json.load(manifest_file)
        if len(manifest) != 1 or image not in manifest[0].get("RepoTags", []):
            raise ValueError("scanner input does not identify the candidate tag")
        config_path = manifest[0].get("Config", "")
        if not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", config_path):
            raise ValueError("scanner input has no immutable image config")
        config_file = archive.extractfile(config_path)
        if config_file is None:
            raise ValueError("scanner input image config is missing")
        config_digest = "sha256:" + hashlib.sha256(config_file.read()).hexdigest()
        if config_digest != "sha256:" + config_path.rsplit("/", 1)[-1]:
            raise ValueError("scanner input image config digest is invalid")
    report_path = scratch / "scan.json"
    cache = pathlib.Path(os.environ.get("TRIVY_CACHE_DIR") or pathlib.Path.home() / ".cache" / "trivy")
    cache.mkdir(parents=True, exist_ok=True)
    run("docker", "run", "--rm", "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp", "-e", "TRIVY_CACHE_DIR=/cache", "-v", f"{scratch}:/work:ro",
        "-v", f"{cache}:/cache", TRIVY_IMAGE, "image", "--input", "/work/candidate-image.tar",
        "--scanners", "vuln", "--pkg-types", "os,library", "--severity", "HIGH,CRITICAL",
        "--format", "json", "--timeout", "30m", output=report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report, report_path, config_digest


def check_scan(report: dict, image_id: str) -> None:
    metadata = report.get("Metadata") or {}
    results = report.get("Results") or []
    if metadata.get("ImageID") != image_id or not metadata.get("OS") or report.get("Errors"):
        raise ValueError("scan does not cover the candidate image or operating system")
    if not any(item.get("Class") == "os-pkgs" for item in results):
        raise ValueError("scan omitted operating-system packages")
    findings = [(v.get("VulnerabilityID"), v.get("Severity")) for item in results
                for v in item.get("Vulnerabilities") or []]
    if findings:
        raise ValueError(f"candidate has {len(findings)} HIGH/CRITICAL findings: {findings[:8]}")


def write_json(path: pathlib.Path, value: dict) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > 8 * 1024 * 1024:
        raise ValueError(f"evidence exceeds runtime-security bound: {path.name}")
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("xb") as handle:
        os.chmod(temp, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def evidence_record(path: pathlib.Path) -> dict:
    return {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def qualify(env_file: pathlib.Path, scratch_root: pathlib.Path) -> dict:
    if os.uname().nodename != "kspls0":
        raise ValueError("qualification must run on kspls0 where the Steam vendor image is loaded")
    tag = env_value(env_file, "DUNE_IMAGE_TAG")
    if not re.fullmatch(r"[0-9]+-[0-9]+-shipping", tag):
        raise ValueError("vendor image tag is missing or invalid")
    vendor_image = f"registry.funcom.com/funcom/self-hosting/seabass-server:{tag}"
    vendor = image_inspect(vendor_image)
    vendor_id = vendor["Id"]
    candidate_image = f"snape/dune-server:qualified-{tag}-{vendor_id[7:19]}"
    output_dir = ROOT / "backups" / "admin-panel" / "runtime-security-auto" / f"auto-{tag}-{vendor_id[7:19]}"
    require_private_directory(output_dir.parent)
    require_private_directory(output_dir)
    require_private_directory(scratch_root)
    approval_path = output_dir / "approval.json"
    if approval_path.exists():
        settings = {"DUNE_IMAGE_TAG": tag, "DUNE_GAME_IMAGE": candidate_image,
                    "DUNE_RUNTIME_SECURITY_APPROVAL_FILE": str(approval_path.relative_to(ROOT))}
        verified = runtime_security.configuration(ROOT, settings)
        runtime_security.verify_loaded_images(verified, image_inspect(candidate_image)["Id"], vendor_id)
        return {"vendorTag": tag, "vendorImageId": vendor_id, "image": candidate_image,
                "imageId": verified["imageId"], "approvalFile": str(approval_path.relative_to(ROOT)),
                "cached": True}
    with tempfile.TemporaryDirectory(prefix="game-qualification-", dir=scratch_root) as directory:
        scratch = pathlib.Path(directory)
        context = scratch / "context"
        context.mkdir()
        run(sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
            "--require-hashes", "-r", str(IMAGE_DIR / "setuptools-requirements.txt"), "-d", str(context))
        wheels = list(context.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("expected exactly one pinned setuptools wheel")
        wheels[0].rename(context / "setuptools.whl")
        shutil.copy2(IMAGE_DIR / "upgrade-embedded-setuptools.py", context)
        shutil.copy2(IMAGE_DIR / "prune-build-packages.sh", context)
        run("docker", "build", "--pull=false", "--build-arg", f"BASE_IMAGE={vendor_image}",
            "--file", str(IMAGE_DIR / "Dockerfile"), "--tag", candidate_image, str(context))
        candidate = image_inspect(candidate_image)
        candidate_id = candidate["Id"]
        check_config(vendor, candidate)
        vendor_tar = scratch / "vendor-rootfs.tar"
        candidate_tar = scratch / "candidate-rootfs.tar"
        export_image(vendor_image, vendor_tar)
        export_image(candidate_image, candidate_tar)
        files_checked, changed_files = compare_payload(vendor_tar, candidate_tar)
        vendor_tar.unlink()
        candidate_tar.unlink()
        check_native(candidate_image)
        check_embedded(candidate_image, scratch)
        report, report_path, scan_config_id = scan_image(candidate_image, scratch)
        check_scan(report, scan_config_id)
        if image_inspect(vendor_image)["Id"] != vendor_id:
            raise ValueError("vendor image changed during qualification")
        if report_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("scan report exceeds runtime-security evidence bound")
        qualification = {
            "candidate_image": candidate_id, "vendor_image": vendor_id,
            "scan_image_config_id": scan_config_id,
            "vendor_files_checked": files_checked, "changed_files": changed_files,
            "game_binary_assets_and_embedded_interpreter_unchanged": True,
            "only_python_packaging_files_changed": True,
            "native_binary_linker_passed": True,
            "image_runtime_configuration_unchanged": True,
            "embedded_python_3_9_setuptools_80_10_2_compatibility_passed": True,
        }
        qualification_path = output_dir / "qualification.json"
        scan_path = output_dir / "scan.json"
        write_json(qualification_path, qualification)
        shutil.copyfile(report_path, scan_path)
        os.chmod(scan_path, 0o600)
        approval = {"schemaVersion": 2, "vendorTag": tag, "image": candidate_image,
                    "imageId": candidate_id, "vendorImageId": vendor_id,
                    "scanImageId": scan_config_id,
                    "scan": {"critical": 0, "high": 0, "errors": 0},
                    "gameBinaryAssetsAndInterpreterUnchanged": True,
                    "qualification": evidence_record(qualification_path),
                    "scanReport": evidence_record(scan_path)}
        write_json(approval_path, approval)
        settings = {"DUNE_IMAGE_TAG": tag, "DUNE_GAME_IMAGE": candidate_image,
                    "DUNE_RUNTIME_SECURITY_APPROVAL_FILE": str(approval_path.relative_to(ROOT))}
        verified = runtime_security.configuration(ROOT, settings)
        runtime_security.verify_loaded_images(verified, candidate_id, vendor_id)
        return {"vendorTag": tag, "vendorImageId": vendor_id, "image": candidate_image,
                "imageId": candidate_id, "approvalFile": str(approval_path.relative_to(ROOT)),
                "filesChecked": files_checked, "changedFiles": changed_files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", nargs="?", default=".env", type=pathlib.Path)
    parser.add_argument("--scratch-root", type=pathlib.Path,
                        default=pathlib.Path.home() / ".cache" / "dune-runtime-qualification")
    args = parser.parse_args()
    env_file = args.env_file.resolve(strict=True)
    require_private_directory(args.scratch_root)
    with (args.scratch_root / "qualification.lock").open("a+") as lock:
        os.chmod(args.scratch_root / "qualification.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = qualify(env_file, args.scratch_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"game runtime qualification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
