#!/usr/bin/env python3
"""Build the deterministic, redistributable DASH source release and SPDX SBOM."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import tarfile


TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SCHEMA = "dash-release-metadata/v1"
SPDX_SCHEMA = "SPDX-2.3"
REPOSITORY = "https://github.com/snapetech/DuneAwakeningSelfHost"
MAX_MEMBERS = 20_000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def git(root: pathlib.Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def utc(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def assert_publishable_path(relative: pathlib.PurePosixPath, *, is_directory: bool = False) -> None:
    if not relative.parts:
        return
    path = relative.as_posix()
    blocked_roots = {"backups", "captures", "build", "dist"}
    if relative.parts[0] in blocked_roots or path == ".env" or path.startswith("config/tls/") or path.startswith("config/secrets/"):
        raise ValueError(f"private/runtime path is not publishable: {path}")
    if relative.parts[0] == "data" and path != "data" and not (
        is_directory and path == "data/exchange-price-snapshots"
    ) and not path.startswith("data/exchange-price-snapshots/"):
        raise ValueError(f"runtime data path is not publishable: {path}")


def validate_inputs(root: pathlib.Path, tag: str, commit: str) -> tuple[str, int]:
    if not TAG_RE.fullmatch(tag):
        raise ValueError("version must be a SemVer tag such as v0.1.0-beta.1")
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("ref must be a full lowercase 40-hex Git commit")
    resolved = git(root, "rev-parse", f"{commit}^{{commit}}")
    if resolved != commit:
        raise ValueError(f"ref resolves to an unexpected commit: {resolved}")
    version_file = git(root, "show", f"{commit}:VERSION")
    if version_file != tag.removeprefix("v"):
        raise ValueError(f"VERSION contains {version_file!r}, expected {tag.removeprefix('v')!r}")
    epoch = int(git(root, "show", "-s", "--format=%ct", commit))
    return resolved, epoch


def release_metadata(tag: str, commit: str, epoch: int) -> dict:
    return {
        "schemaVersion": SCHEMA,
        "name": "DuneAwakeningSelfHost",
        "version": tag.removeprefix("v"),
        "tag": tag,
        "commit": commit,
        "source": f"{REPOSITORY}/tree/{commit}",
        "createdAt": utc(epoch),
        "sourceDateEpoch": epoch,
        "platform": {
            "serverOperatingSystem": "linux",
            "architecture": "x86_64",
            "requiredCpuFeature": "avx2",
            "runtime": "Docker Engine with Compose plugin",
        },
        "contents": {
            "funcomArtifactsIncluded": False,
            "runtimeStateIncluded": False,
            "secretsIncluded": False,
            "operatorMustSupplyOfficialSteamPackage": True,
        },
    }


def source_members(root: pathlib.Path, tag: str, commit: str, epoch: int):
    package_root = f"dash-{tag}-linux-x86_64"
    archive_bytes = git(root, "archive", "--format=tar", f"--prefix={package_root}/", commit, binary=True)
    rows = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as source:
        members = source.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise ValueError("Git archive is empty or exceeds the member limit")
        for member in members:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != package_root:
                raise ValueError(f"unsafe source archive path: {member.name}")
            relative = pathlib.PurePosixPath(*path.parts[1:])
            assert_publishable_path(relative, is_directory=member.isdir())
            if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported source archive member: {member.name}")
            data = b""
            if member.isfile():
                extracted = source.extractfile(member)
                if extracted is None:
                    raise ValueError(f"cannot read source archive member: {member.name}")
                data = extracted.read()
                total += len(data)
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("source archive exceeds the expanded-size limit")
            rows.append((member.name.rstrip("/") + ("/" if member.isdir() else ""), member.mode, member.isdir(), data))
    metadata = json_bytes(release_metadata(tag, commit, epoch))
    rows.append((f"{package_root}/RELEASE-METADATA.json", 0o644, False, metadata))
    return package_root, sorted(rows, key=lambda row: row[0])


def write_archive(path: pathlib.Path, rows, epoch: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                for name, mode, is_directory, data in rows:
                    info = tarfile.TarInfo(name=name)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    info.mode = mode
                    if is_directory:
                        info.type = tarfile.DIRTYPE
                        info.size = 0
                        output.addfile(info)
                    else:
                        info.type = tarfile.REGTYPE
                        info.size = len(data)
                        output.addfile(info, io.BytesIO(data))
    os.replace(temporary, path)


def spdx_document(tag: str, commit: str, epoch: int, archive_name: str, archive_sha: str, rows) -> dict:
    files = []
    relationships = [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": "SPDXRef-Package-DASH"}]
    sha1_values = []
    for name, _mode, is_directory, data in rows:
        if is_directory:
            continue
        relative = name.split("/", 1)[1]
        file_sha256 = sha256_bytes(data)
        file_sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
        sha1_values.append(file_sha1)
        spdx_id = "SPDXRef-File-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
        license_info = "NOASSERTION"
        if relative in {"vendor/bin/busybox", "vendor/source/busybox-1.36.1.tar.bz2", "vendor/source/busybox-1.36.1.config", "vendor/build-busybox.sh", "vendor/licenses/BUSYBOX-GPL-2.0.txt"}:
            license_info = "GPL-2.0-only"
        elif relative in {"vendor/bin/curl", "vendor/licenses/CURL.txt"}:
            license_info = "curl"
        elif relative in {"vendor/bin/jq", "vendor/licenses/JQ-MIT.txt"}:
            license_info = "MIT"
        elif relative in {"vendor/bin/rg", "vendor/licenses/RIPGREP-MIT.txt", "vendor/licenses/RIPGREP-UNLICENSE.txt"}:
            license_info = "MIT OR Unlicense"
        files.append({
            "SPDXID": spdx_id,
            "fileName": f"./{relative}",
            "checksums": [
                {"algorithm": "SHA256", "checksumValue": file_sha256},
                {"algorithm": "SHA1", "checksumValue": file_sha1},
            ],
            "licenseConcluded": "NOASSERTION",
            "licenseInfoInFiles": [license_info],
            "copyrightText": "NOASSERTION",
        })
        relationships.append({"spdxElementId": "SPDXRef-Package-DASH", "relationshipType": "CONTAINS", "relatedSpdxElement": spdx_id})
    verification = hashlib.sha1("".join(sorted(sha1_values)).encode("ascii"), usedforsecurity=False).hexdigest()
    return {
        "spdxVersion": SPDX_SCHEMA,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"DASH-{tag}",
        "documentNamespace": f"{REPOSITORY}/releases/tag/{tag}/spdx/{commit}",
        "creationInfo": {"created": utc(epoch), "creators": ["Tool: DASH scripts/build-release.py"]},
        "packages": [{
            "name": "DuneAwakeningSelfHost",
            "SPDXID": "SPDXRef-Package-DASH",
            "versionInfo": tag.removeprefix("v"),
            "downloadLocation": f"{REPOSITORY}/releases/download/{tag}/{archive_name}",
            "filesAnalyzed": True,
            "packageVerificationCode": {"packageVerificationCodeValue": verification},
            "checksums": [{"algorithm": "SHA256", "checksumValue": archive_sha}],
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "See LICENSE in the package",
            "supplier": "Organization: DASH contributors",
        }],
        "files": files,
        "relationships": relationships,
    }


def build(root: pathlib.Path, output: pathlib.Path, tag: str, commit: str) -> dict:
    commit, epoch = validate_inputs(root, tag, commit)
    _package_root, rows = source_members(root, tag, commit, epoch)
    archive = output / f"dash-{tag}-linux-x86_64.tar.gz"
    write_archive(archive, rows, epoch)
    digest = sha256_file(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    sbom = output / f"dash-{tag}-linux-x86_64.spdx.json"
    sbom.write_bytes(json_bytes(spdx_document(tag, commit, epoch, archive.name, digest, rows)))
    return {"ok": True, "tag": tag, "commit": commit, "archive": str(archive), "sha256": digest, "sbom": str(sbom)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="SemVer tag, for example v0.1.0-beta.1")
    parser.add_argument("--ref", required=True, help="Exact full Git commit")
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = build(args.root.resolve(), args.output_dir.resolve(), args.version, args.ref)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
