#!/usr/bin/env python3
"""Patch selected build-cap checks in the Dune server binary.

This is the binary-patching counterpart to scripts/patch-building-piece-limit-pak.py
(which patches a cooked data table in a pak). Here the cap is enforced by a
C++ comparison inside DuneSandboxServer-Linux-Shipping, so we patch the
binary directly using a byte-pattern signature (so the patch survives
Funcom binary updates as long as the surrounding codegen is stable).

Usage on the server host:
    python3 scripts/patch-subfief-cap-binary.py \
        --binary /home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping \
        --target subfief \
        --new-cap 6

    python3 scripts/patch-subfief-cap-binary.py \
        --binary /home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping \
        --target building \
        --new-cap 7500

The script is idempotent: it detects an already-patched signature and exits 0.
Rollback requires restoring the original binary from backup.
"""
import argparse
import hashlib
import shutil
import struct
import sys
from pathlib import Path
from typing import NamedTuple

DEFAULT_BINARY = Path("/home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping")
OLD_CAP = 3


class PatchTarget(NamedTuple):
    name: str
    description: str
    signature: list
    je_offset: int
    expected_fail_enum: int

# ---------------------------------------------------------------------------
# Signature: the subfief-cap validity check inside the totem placement path.
#
#   mov  rdi, [r14 + 0x70]    49 8b 7e 70
#   call <validity helper>    e8 ?? ?? ?? ??       (rel32 wildcards)
#   test al, al               84 c0
#   je   <fail block>         0f 84 ?? ?? ?? ??    ← PATCH this je to 6x NOP
#
# Patching the `je` to six NOPs (0x90) makes the cap check always succeed:
# the fail-block that writes Fail_DisallowedBuildLimit (enum value 0x6b, see
# docs/subfief-cap-enum-table.md) is never reached. Ghidra confirmed this is
# the only path that writes the enum byte at struct offset +0x18 from this
# code path. The signature was originally identified on an older build and
# revalidated as a unique match on the current 1.5.3/2111270 server image.
#
# `new_cap` is accepted for API symmetry with the building-piece patcher but
# is ignored — the patch is a hard bypass, not a numeric cap raise. If
# `new_cap` equals OLD_CAP (3), this script is a no-op (rollback path uses
# the original je bytes).
# ---------------------------------------------------------------------------
SUBFIEF_SIGNATURE = [
    0xc5, 0xfc, 0x11, 0x43, 0x44,                # -10 vmovups [rbx+0x44], ymm0  (anchor)
    0xe9, None, None, None, None,                # -5  jmp <rel32>  (rel32 wildcard)
    0x49, 0x8b, 0x7e, 0x70,                      # +0  mov rdi, [r14+0x70]
    0xe8, None, None, None, None,                # +4  call <helper>  (rel32 wildcard)
    0x84, 0xc0,                                  # +9  test al, al
    0x0f, 0x84, None, None, None, None,          # +11 je <fail>  (rel32 wildcard)  ← PATCH
    0x48, 0x8b, 0x05, None, None, None, None,    # +17 mov rax, [rip+disp]
    0xc5, 0xf8, 0x28,                            # +24 vmovaps xmm0, ...
]


# Building-piece server-wide cap check. Ghidra/file offset 0xcf01466 is the
# `je` to a fail block that writes enum 0x7e
# (Fail_ReachedBuildableStructureLimitInServer).
BUILDING_SERVER_SIGNATURE = [
    0x48, 0x8b, 0xbd, 0x08, 0xff, 0xff, 0xff,    # mov rdi, [rbp-0xf8]
    0xe8, None, None, None, None,                # call 0xedf0f20
    0x84, 0xc0,                                  # test al, al
    0x0f, 0x84, None, None, None, None,          # je <fail>  (PATCH)
    0x48, 0x8b, 0x05, None, None, None, None,    # mov rax, [rip+disp]
    0xc5, 0xf8, 0x28, 0x05,                     # vmovaps xmm0, ...
]


# Building-piece composed/server cap check. Ghidra/file offset 0xcf011e4 is
# the `je` to a fail block that writes enum 0x80
# (Fail_ReachedBuildableStructureComposedLimitInServer).
BUILDING_COMPOSED_SERVER_SIGNATURE = [
    0x48, 0x89, 0xdf,                            # mov rdi, rbx
    0xe8, None, None, None, None,                # call 0xedf0f20
    0x84, 0xc0,                                  # test al, al
    0x0f, 0x84, None, None, None, None,          # je <fail>  (PATCH)
    0x48, 0x8b, 0x05, None, None, None, None,    # mov rax, [rip+disp]
    0xc5, 0xf8, 0x28, 0x05,                     # vmovaps xmm0, ...
]


# Building-piece map-wide cap check. Ghidra/file offset 0xcf027e6 is the `je`
# to a fail block that writes enum 0x7f
# (Fail_ReachedBuildableStructureLimitInMap).
BUILDING_MAP_SIGNATURE = [
    0xc4, 0xc1, 0x7c, 0x11, 0x44, 0x24, 0x44,    # vmovups [r12+0x44], ymm0
    0xe9, None, None, None, None,                # jmp <rel32>
    0x4c, 0x89, 0xf7,                            # mov rdi, r14
    0xe8, None, None, None, None,                # call 0xedf0f20
    0x84, 0xc0,                                  # test al, al
    0x0f, 0x84, None, None, None, None,          # je <fail>  (PATCH)
    0x48, 0x8b, 0x05, None, None, None, None,    # mov rax, [rip+disp]
    0xc5, 0xf8, 0x28, 0x05,                     # vmovaps xmm0, ...
]


# Building-piece composed/map cap check. Ghidra/file offset 0xcf02a62 is the
# `je` to a fail block that writes enum 0x81
# (Fail_ReachedBuildableStructureComposedLimitInMap).
BUILDING_COMPOSED_MAP_SIGNATURE = [
    0x48, 0x8b, 0xbd, 0xf8, 0xfe, 0xff, 0xff,    # mov rdi, [rbp-0x108]
    0xe8, None, None, None, None,                # call 0xedf0f20
    0x84, 0xc0,                                  # test al, al
    0x0f, 0x84, None, None, None, None,          # je <fail>  (PATCH)
    0x48, 0x8b, 0x05, None, None, None, None,    # mov rax, [rip+disp]
    0xc5, 0xf8, 0x28, 0x05,                     # vmovaps xmm0, ...
]


TARGETS = {
    "subfief": PatchTarget(
        name="subfief",
        description="per-player subfief/totem placement cap",
        signature=SUBFIEF_SIGNATURE,
        je_offset=21,
        expected_fail_enum=0x6b,
    ),
    "building-server": PatchTarget(
        name="building-server",
        description="server-wide building-piece structure cap",
        signature=BUILDING_SERVER_SIGNATURE,
        je_offset=14,
        expected_fail_enum=0x7e,
    ),
    "building-composed-server": PatchTarget(
        name="building-composed-server",
        description="server-wide composed building-piece cap",
        signature=BUILDING_COMPOSED_SERVER_SIGNATURE,
        je_offset=10,
        expected_fail_enum=0x80,
    ),
    "building-map": PatchTarget(
        name="building-map",
        description="map-wide building-piece structure cap",
        signature=BUILDING_MAP_SIGNATURE,
        je_offset=22,
        expected_fail_enum=0x7f,
    ),
    "building-composed-map": PatchTarget(
        name="building-composed-map",
        description="map-wide composed building-piece cap",
        signature=BUILDING_COMPOSED_MAP_SIGNATURE,
        je_offset=14,
        expected_fail_enum=0x81,
    ),
}


def find_all_signature(data: bytes, signature: list) -> list:
    """Return ALL match offsets (not just one). Used so the caller can apply
    a post-filter to disambiguate when multiple sites share the same outer
    pattern but differ in their downstream fail block."""
    n = len(signature)
    if n == 0:
        raise ValueError("empty signature")
    anchor_i = next((i for i, b in enumerate(signature) if b is not None), None)
    if anchor_i is None:
        raise ValueError("signature is all wildcards")
    anchor_b = signature[anchor_i]
    hits = []
    pos = 0
    while True:
        p = data.find(bytes([anchor_b]), pos)
        if p < 0 or p - anchor_i < 0 or p - anchor_i + n > len(data):
            break
        ok = True
        for i, b in enumerate(signature):
            if b is None:
                continue
            if data[p - anchor_i + i] != b:
                ok = False
                break
        if ok:
            hits.append(p - anchor_i)
        pos = p + 1
    return hits


def patched_signature(target: PatchTarget) -> list:
    signature = list(target.signature)
    for off in range(target.je_offset, target.je_offset + 6):
        signature[off] = 0x90
    return signature


def fail_enum_at_site(data: bytes, site_off: int, je_offset: int) -> int | None:
    """Compute the je target and return the fail enum written at status +0x18."""
    je_addr = site_off + je_offset
    if je_addr + 6 > len(data):
        return None
    disp = struct.unpack_from("<i", data, je_addr + 2)[0]
    target = je_addr + 6 + disp
    if target < 0 or target >= len(data):
        return None

    # Search the fail block for common encodings of:
    #   mov byte ptr [<result storage> + 0x18], <enum>
    fb = data[target:target + 500]
    for i in range(len(fb) - 4):
        if fb[i:i + 3] in (b"\xc6\x43\x18", b"\xc6\x45\x18"):
            return fb[i + 3]
        if fb[i:i + 4] == b"\xc6\x44\x24\x18":
            return fb[i + 4]
        if i + 6 < len(fb) and fb[i:i + 2] == b"\xc6\x85":
            if struct.unpack_from("<i", fb, i + 2)[0] == 0x18:
                return fb[i + 6]
    return None


def find_patch_site(data: bytes, target: PatchTarget) -> tuple[int, bool]:
    """Return (site offset, already patched)."""
    candidates = find_all_signature(data, target.signature)
    filtered = [
        s for s in candidates
        if fail_enum_at_site(data, s, target.je_offset) == target.expected_fail_enum
    ]
    if len(filtered) == 1:
        return filtered[0], False
    if len(filtered) > 1:
        raise SystemExit(
            f"{target.name}: multiple sites lead to a "
            f"0x{target.expected_fail_enum:02x} fail block: "
            f"{[hex(s) for s in filtered]} - narrow the signature further"
        )

    already_patched = find_all_signature(data, patched_signature(target))
    if len(already_patched) == 1:
        return already_patched[0], True
    if len(already_patched) > 1:
        raise SystemExit(
            f"{target.name}: patched signature is ambiguous: "
            f"{[hex(s) for s in already_patched]}"
        )

    if candidates:
        enums = {
            hex(s): fail_enum_at_site(data, s, target.je_offset)
            for s in candidates
        }
        raise SystemExit(
            f"{target.name}: matched {len(candidates)} candidate sites but none "
            f"lead to a 0x{target.expected_fail_enum:02x} fail block: {enums}"
        )
    return -1, False


def selected_targets(name: str) -> list[PatchTarget]:
    if name == "all":
        return [
            TARGETS["subfief"],
            TARGETS["building-server"],
            TARGETS["building-composed-server"],
            TARGETS["building-map"],
            TARGETS["building-composed-map"],
        ]
    if name == "building":
        return [
            TARGETS["building-server"],
            TARGETS["building-composed-server"],
            TARGETS["building-map"],
            TARGETS["building-composed-map"],
        ]
    return [TARGETS[name]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--target", choices=sorted([*TARGETS.keys(), "building", "all"]),
                        default="subfief",
                        help="Which binary cap check to bypass (default: subfief).")
    parser.add_argument("--new-cap", type=int, required=True,
                        help="Legacy cap knob. Any value > OLD_CAP applies the bypass.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Find the site and report what would change without writing.")
    parser.add_argument("--backup", type=Path, default=None,
                        help="Optional path for a one-time backup before first patch.")
    args = parser.parse_args()

    if not (1 <= args.new_cap <= 100000):
        sys.exit(f"new-cap out of range: {args.new_cap}")

    # The patch is a hard bypass of the cap check (changes a 6-byte `je rel32`
    # to 6 NOPs). It does not raise a numeric cap, so any new_cap > OLD_CAP
    # produces the same patch. new_cap == OLD_CAP (3) is treated as "do
    # nothing" (rollback requires restoring the original bytes from a backup).
    if args.new_cap <= OLD_CAP:
        print(f"new-cap {args.new_cap} <= OLD_CAP {OLD_CAP}; nothing to patch "
              f"(restore from backup to fully roll back the bypass)")
        return

    data = args.binary.read_bytes()
    plan = []
    for target in selected_targets(args.target):
        base, already_patched = find_patch_site(data, target)
        if base < 0:
            sys.exit(
                f"{target.name}: signature not found in {args.binary} - "
                f"Funcom may have changed the codegen"
            )
        je_addr = base + target.je_offset
        if already_patched:
            print(f"{target.name}: already patched at 0x{je_addr:x}; nothing to do")
            continue
        plan.append((target, base))

    if not plan:
        print(f"all selected targets already patched (target={args.target}, new_cap={args.new_cap})")
        return

    # Pre-patch backup
    if args.backup and not args.backup.exists():
        shutil.copy2(args.binary, args.backup)
        print(f"backup written: {args.backup}")

    sha_before = hashlib.sha256(data).hexdigest()
    patched = bytearray(data)
    for target, base in plan:
        print(f"{target.name}: {target.description}")
        for off in range(target.je_offset, target.je_offset + 6):
            old_byte = patched[base + off]
            patched[base + off] = 0x90
            print(f"  patch @ 0x{base+off:x}: 0x{old_byte:02x} -> 0x90")
    sha_after = hashlib.sha256(bytes(patched)).hexdigest()

    if args.dry_run:
        print(f"DRY RUN: would patch {len(plan)} target(s).")
        print(f"  sha256 before: {sha_before}")
        print(f"  sha256 after:  {sha_after}")
        return

    args.binary.write_bytes(bytes(patched))
    print(f"patched OK: {args.binary}")
    print(f"  sha256 before: {sha_before}")
    print(f"  sha256 after:  {sha_after}")


if __name__ == "__main__":
    main()
