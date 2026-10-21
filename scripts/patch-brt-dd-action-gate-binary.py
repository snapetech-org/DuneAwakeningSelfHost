#!/usr/bin/env python3
"""Bypass the Deep Desert BRT action gate before placement validation.

The existing BRT Deep Desert patch changes the placement verdict returned by
BuildingBlueprintBrush::PerformCanBePlaced. Live DD1 still rejected the Base
Reconstruction Tool before placement, in UGameItemBaseBackupToolActions. This
patch bypasses the matching can-use gate and its invalid-map reason path.

The script is idempotent. Rollback requires restoring the original binary from
the container image or a pre-patch backup.
"""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path
from typing import NamedTuple

DEFAULT_BINARY = Path("/home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping")


class PatchSite(NamedTuple):
    name: str
    patterns: tuple[bytes, ...]
    offset: int
    original: bytes
    patched: bytes


PATCHES = [
    PatchSite(
        name="BRT can-use Deep Desert map-area guard",
        patterns=(
            bytes.fromhex(
                "48 85 c0 74 0a 41 b6 01 41 80 7f 55 01 75 03"
            ),
            # 1.5.3 / 2111270 moved the same byte-field from +0x55 to
            # +0x51; the branch shape and bypass remain unchanged.
            bytes.fromhex(
                "48 85 c0 74 0a 41 b6 01 41 80 7f 51 01 75 03"
            ),
        ),
        offset=13,
        original=bytes.fromhex("75 03"),
        patched=bytes.fromhex("eb 03"),
    ),
    PatchSite(
        name="BRT invalid-map reason guard",
        patterns=(
            bytes.fromhex(
                "41 b6 32 84 c0 0f 85 f7 fe ff ff 41 b6 03"
            ),
            bytes.fromhex(
                "41 b6 32 84 c0 0f 85 f3 fe ff ff 41 b6 03"
            ),
        ),
        # The 1.5.3 / 2111270 function moved the branch target by four
        # bytes while retaining the invalid-map guard semantics.
        offset=5,
        original=bytes.fromhex("0f 85 f7 fe ff ff"),
        patched=bytes.fromhex("90 90 90 90 90 90"),
    ),
]


def find_site(data: bytes, site: PatchSite) -> tuple[int, bool]:
    hits = []
    for pattern in site.patterns:
        prefix = pattern[:site.offset]
        suffix = pattern[site.offset + len(site.original):]
        pos = 0
        while True:
            hit = data.find(prefix, pos)
            if hit < 0:
                break
            patch_offset = hit + site.offset
            current = data[patch_offset:patch_offset + len(site.original)]
            suffix_offset = patch_offset + len(site.original)
            pattern_original = pattern[site.offset:suffix_offset - hit]
            if (
                current in (site.original, site.patched, pattern_original)
                and data[suffix_offset:suffix_offset + len(suffix)] == suffix
            ):
                hits.append(hit)
            pos = hit + 1
    hits = sorted(set(hits))
    if len(hits) != 1:
        raise SystemExit(
            f"{site.name}: expected one signature, found {len(hits)}: "
            f"{[hex(hit) for hit in hits]}"
        )

    patch_offset = hits[0] + site.offset
    current = data[patch_offset:patch_offset + len(site.original)]
    if current == site.original:
        return patch_offset, False
    if current == site.patched:
        return patch_offset, True
    raise SystemExit(
        f"{site.name}: unexpected bytes at 0x{patch_offset:x}: "
        f"{current.hex(' ')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup", type=Path, default=None)
    args = parser.parse_args()

    data = args.binary.read_bytes()
    patch_offsets = []
    already_patched = []
    for site in PATCHES:
        offset, is_patched = find_site(data, site)
        if is_patched:
            already_patched.append((site, offset))
        else:
            patch_offsets.append((site, offset))

    for site, offset in already_patched:
        print(f"already patched @ 0x{offset:x}: {site.name}")
    if not patch_offsets:
        print("BRT action-gate bypass already patched; nothing to do")
        return

    sha_before = hashlib.sha256(data).hexdigest()
    patched = bytearray(data)
    for site, offset in patch_offsets:
        patched[offset:offset + len(site.original)] = site.patched
        print(
            f"patch @ 0x{offset:x}: {site.name}: "
            f"{site.original.hex(' ')} -> {site.patched.hex(' ')}"
        )
    sha_after = hashlib.sha256(bytes(patched)).hexdigest()

    if args.dry_run:
        print(f"DRY RUN: would patch {len(patch_offsets)} site(s).")
        print(f"  sha256 before: {sha_before}")
        print(f"  sha256 after:  {sha_after}")
        return

    if args.backup and not args.backup.exists():
        shutil.copy2(args.binary, args.backup)
        print(f"backup written: {args.backup}")
    args.binary.write_bytes(bytes(patched))
    print(f"patched OK: {args.binary}")
    print(f"  sha256 before: {sha_before}")
    print(f"  sha256 after:  {sha_after}")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        sys.exit(str(exc))
