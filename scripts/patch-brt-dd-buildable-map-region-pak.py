#!/usr/bin/env python3
"""Patch the cooked BRT buildable-region table for Deep Desert.

Modes:

- ``swap-map-rows``: swap only the HaggaBasin and DeepDesert ``m_Map`` values.
  This is the original coarse-grained compatibility patch.

- ``dd-totem-groups``: keep the DeepDesert row mapped to DeepDesert, disable its
  PVP-region override, and inject only Hagga's non-empty Totem/Totem_Small
  buildable-group modifiers into DeepDesert's default-region data. Deep
  Desert's map-area restriction array is left untouched so we do not import
  Hagga's area geometry into DD.
"""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import struct
import sys


PAK_MAGIC = 0x5A6F12E1
DEFAULT_PAK = Path("/home/dune/server/DuneSandbox/Content/Paks/pakchunk0-LinuxServer.pak")
DEFAULT_OODLE = Path("/tmp/oodle/liboodle-data-shared.so")
TARGET_TABLE = b"/Game/Dune/Systems/Building/Data/DT_BuildableMapRegion"
TARGET_UASSET_REL = Path("DuneSandbox/Content/Dune/Systems/Building/Data/DT_BuildableMapRegion.uasset")
TARGET_UEXP_REL = Path("DuneSandbox/Content/Dune/Systems/Building/Data/DT_BuildableMapRegion.uexp")
REQUIRED_NAMES = {
    "DeepDesert",
    "EDuneMapId",
    "HaggaBasin",
    "m_Map",
    "StructProperty",
}

MODE_SWAP_MAP_ROWS = "swap-map-rows"
MODE_DD_TOTEM_GROUPS = "dd-totem-groups"
MODE_DD_FULL_REGION = "dd-full-region"
NONE_NAME_INDEX = 29


def is_steam_client_pak(path):
    try:
        normalized = str(path.expanduser().resolve()).replace("\\", "/")
    except OSError:
        normalized = str(path.expanduser()).replace("\\", "/")
    return "/steamapps/common/DuneAwakening/DuneSandbox/Content/Paks/" in normalized


def refuse_client_write(path, dry_run):
    if not dry_run and is_steam_client_pak(path):
        raise SystemExit("refusing to modify Steam client pak; server-side pak edits only")


def write_overlay_assets(output_dir, uasset_blob, uexp_blob):
    uasset_path = output_dir / TARGET_UASSET_REL
    uexp_path = output_dir / TARGET_UEXP_REL
    uasset_path.parent.mkdir(parents=True, exist_ok=True)
    uasset_path.write_bytes(uasset_blob)
    uexp_path.write_bytes(uexp_blob)
    return uasset_path, uexp_path


def read_footer(data):
    magic_bytes = struct.pack("<I", PAK_MAGIC)
    magic_pos = data.rfind(magic_bytes, max(0, len(data) - 512))
    if magic_pos < 20:
        raise ValueError("pak footer magic not found near end of file")
    base = magic_pos - 20
    magic = struct.unpack_from("<I", data, base + 20)[0]
    if magic != PAK_MAGIC:
        raise ValueError(f"unexpected pak footer magic 0x{magic:08x}")
    return {
        "version": struct.unpack_from("<I", data, base + 24)[0],
        "index_offset": struct.unpack_from("<Q", data, base + 28)[0],
        "index_size": struct.unpack_from("<Q", data, base + 36)[0],
    }


def plausible_entry(data, index_offset, rel):
    pos = index_offset + rel
    if pos < index_offset or pos + 16 > len(data):
        return None
    bitfield = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    compression = (bitfield >> 23) & 0x3F
    block_count = (bitfield >> 6) & 0xFFFF
    if compression > 5 or block_count > 128 or bitfield & (1 << 22):
        return None
    block_uncompressed = (bitfield & 0x3F) << 11
    if (bitfield & 0x3F) == 0x3F:
        if pos + 4 > len(data):
            return None
        block_uncompressed = struct.unpack_from("<I", data, pos)[0]
        pos += 4

    def read_size(bit):
        nonlocal pos
        if bitfield & (1 << bit):
            if pos + 4 > len(data):
                raise ValueError("short encoded entry")
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            return value
        if pos + 8 > len(data):
            raise ValueError("short encoded entry")
        value = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
        return value

    try:
        offset = read_size(31)
        uncompressed = read_size(30)
        compressed = read_size(29) if compression else uncompressed
    except ValueError:
        return None
    if not (0 < offset < len(data) and 0 < uncompressed < 64 * 1024 * 1024 and 0 < compressed < 64 * 1024 * 1024):
        return None

    block_sizes = []
    if compression and block_count > 1:
        if pos + block_count * 4 > len(data):
            return None
        for _ in range(block_count):
            block_sizes.append(struct.unpack_from("<I", data, pos)[0])
            pos += 4
    elif compression:
        block_sizes.append(compressed)
    else:
        block_sizes.append(compressed)
    return {
        "rel": rel,
        "compression": compression,
        "block_count": block_count,
        "block_uncompressed": block_uncompressed,
        "offset": offset,
        "uncompressed": uncompressed,
        "compressed": compressed,
        "block_sizes": block_sizes,
        "encoded_size": pos - (index_offset + rel),
    }


def load_oodle(path):
    lib = ctypes.CDLL(str(path))
    decompress = lib.OodleLZ_Decompress
    decompress.restype = ctypes.c_int
    decompress.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint64,
        ctypes.c_size_t, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint,
    ]
    compress = lib.OodleLZ_Compress
    compress.restype = ctypes.c_int
    compress.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_size_t,
    ]
    return decompress, compress


def pak_entry_payload_start(entry):
    return entry["offset"] + 53 + (4 + 16 * entry["block_count"] if entry["compression"] else 0)


def decompress_entry(data, entry, decompress):
    start = pak_entry_payload_start(entry)
    if not entry["compression"]:
        return bytes(data[start:start + entry["compressed"]])
    output = bytearray()
    pos = start
    for i, size in enumerate(entry["block_sizes"]):
        raw = data[pos:pos + size]
        pos += size
        out_len = entry["uncompressed"] if len(entry["block_sizes"]) == 1 else min(
            entry["block_uncompressed"],
            entry["uncompressed"] - i * entry["block_uncompressed"],
        )
        out = (ctypes.c_ubyte * out_len)()
        inp = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        written = decompress(inp, len(raw), out, out_len, 1, 1, 0, 0, 0, 0, 0, None, 0, 3)
        if written != out_len:
            raise RuntimeError(f"oodle decompression failed for index entry 0x{entry['rel']:x}")
        output.extend(bytes(out))
    return bytes(output)


def compress_blob(blob, max_size, compress, decompress):
    attempts = [(8, 6), (8, 7), (8, 8), (8, 9), (12, 6), (13, 8), (13, 9)]
    inp = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    for compressor, level in attempts:
        out = (ctypes.c_ubyte * (len(blob) + 65536))()
        size = compress(compressor, inp, len(blob), out, level, None, None, None, None, 0)
        if size <= 0 or size > max_size:
            continue
        compressed = bytes(out[:size])
        padded = compressed + (b"\0" * (max_size - size))
        verify_out = (ctypes.c_ubyte * len(blob))()
        verify_in = (ctypes.c_ubyte * len(padded)).from_buffer_copy(padded)
        written = decompress(verify_in, len(padded), verify_out, len(blob), 1, 1, 0, 0, 0, 0, 0, None, 0, 3)
        if written == len(blob) and bytes(verify_out) == blob:
            return padded, size, compressor, level
    raise RuntimeError(f"recompressed row did not round-trip within original {max_size}-byte slot")


def compress_blob_unbounded(blob, compress, decompress):
    attempts = [(8, 6), (8, 7), (8, 8), (8, 9), (12, 6), (13, 8), (13, 9)]
    inp = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    best = None
    for compressor, level in attempts:
        out = (ctypes.c_ubyte * (len(blob) + 65536))()
        size = compress(compressor, inp, len(blob), out, level, None, None, None, None, 0)
        if size <= 0:
            continue
        compressed = bytes(out[:size])
        verify_out = (ctypes.c_ubyte * len(blob))()
        verify_in = (ctypes.c_ubyte * len(compressed)).from_buffer_copy(compressed)
        written = decompress(verify_in, len(compressed), verify_out, len(blob), 1, 1, 0, 0, 0, 0, 0, None, 0, 3)
        if written == len(blob) and bytes(verify_out) == blob:
            if best is None or size < best[1]:
                best = (compressed, size, compressor, level)
    if best is None:
        raise RuntimeError("recompressed row did not round-trip")
    return best


def parse_uasset_names(blob):
    marker = b"\x0e\x00\x00\x00ArrayProperty\x00"
    pos = blob.find(marker)
    if pos < 0:
        raise RuntimeError("uasset name table start not found")
    names = []
    while pos + 8 <= len(blob):
        length = struct.unpack_from("<i", blob, pos)[0]
        pos += 4
        if length == 0 or abs(length) > 512:
            break
        if length < 0:
            byte_len = -length * 2
            raw = blob[pos:pos + byte_len]
            pos += byte_len
            if len(raw) != byte_len:
                break
            name = raw[:-2].decode("utf-16le", "replace")
        else:
            raw = blob[pos:pos + length]
            pos += length
            if len(raw) != length:
                break
            name = raw[:-1].decode("utf-8", "replace")
        if pos + 4 > len(blob):
            break
        pos += 4
        names.append(name)
        if REQUIRED_NAMES.issubset(names):
            # Keep parsing until the current asset's name table tail so indices
            # remain stable if required names appear early.
            if name == "ScriptStruct":
                break
    missing = sorted(REQUIRED_NAMES.difference(names))
    if missing:
        raise RuntimeError(f"uasset name table missing required names: {missing}")
    return names


def find_map_value_offsets(uexp, names):
    idx = {name: i for i, name in enumerate(names)}
    pattern = (
        struct.pack("<ii", idx["m_Map"], 0)
        + struct.pack("<ii", idx["StructProperty"], 0)
        + struct.pack("<ii", 8, 0)
        + struct.pack("<ii", idx["EDuneMapId"], 0)
        + (b"\0" * 16)
        + b"\0"
    )
    offsets = []
    pos = 0
    while True:
        hit = uexp.find(pattern, pos)
        if hit < 0:
            break
        offsets.append(hit + len(pattern))
        pos = hit + 1
    if len(offsets) != 2:
        raise RuntimeError(f"expected two m_Map value offsets, found {len(offsets)}: {[hex(o) for o in offsets]}")
    return offsets


def find_bool_property_value_offset(uexp, names, prop_name):
    idx = {name: i for i, name in enumerate(names)}
    pattern = (
        struct.pack("<ii", idx[prop_name], 0)
        + struct.pack("<ii", idx["BoolProperty"], 0)
        + struct.pack("<q", 0)
    )
    offsets = []
    pos = 0
    while True:
        hit = uexp.find(pattern, pos)
        if hit < 0:
            break
        offsets.append(hit + len(pattern))
        pos = hit + 1
    if len(offsets) != 2:
        raise RuntimeError(f"expected two {prop_name} bool value offsets, found {len(offsets)}")
    return offsets


def parse_rows(uexp, names):
    def fname(off):
        name_idx, number = struct.unpack_from("<ii", uexp, off)
        if not (0 <= name_idx < len(names)):
            raise RuntimeError(f"invalid FName index {name_idx} at 0x{off:x}")
        return names[name_idx], number

    rows = []
    row_count = struct.unpack_from("<i", uexp, 0x29)[0]
    off = 0x2D
    for _ in range(row_count):
        row_name, row_num = fname(off)
        prop_off = off + 8
        props = {}
        while True:
            name, _ = fname(prop_off)
            if name == "None":
                rows.append({
                    "row_name": row_name,
                    "row_num": row_num,
                    "row_name_off": off,
                    "start": off,
                    "end": prop_off + 8,
                    "props": props,
                })
                off = prop_off + 8
                break

            prop_type, _ = fname(prop_off + 8)
            size = struct.unpack_from("<q", uexp, prop_off + 16)[0]
            if prop_type == "StructProperty":
                struct_name, _ = fname(prop_off + 24)
                value_off = prop_off + 49
                extra = {"struct_name": struct_name}
            elif prop_type == "ArrayProperty":
                inner_type, _ = fname(prop_off + 24)
                value_off = prop_off + 33
                extra = {"inner_type": inner_type, "count": struct.unpack_from("<i", uexp, value_off)[0]}
            elif prop_type == "BoolProperty":
                value_off = prop_off + 26
                extra = {"bool_value": uexp[prop_off + 24]}
            else:
                value_off = prop_off + 25
                extra = {}

            prop_end = value_off + size
            props[name] = {
                "type": prop_type,
                "size": size,
                "tag_off": prop_off,
                "value_off": value_off,
                "end": prop_end,
                **extra,
            }
            prop_off = prop_end
    return rows


def encode_struct_property(tag_prefix, value_blob):
    patched = bytearray(tag_prefix)
    struct.pack_into("<q", patched, 16, len(value_blob))
    return bytes(patched) + value_blob


def encode_bool_property(tag_prefix, value):
    patched = bytearray(tag_prefix)
    patched[24] = 1 if value else 0
    return bytes(patched)


def build_empty_map_region_struct():
    return struct.pack("<ii", 29, 0)


def parse_struct_value_props(value_blob, names):
    """Parse a nested StructProperty value by wrapping it as one synthetic row."""
    row_name_idx = names.index("NewRow") if "NewRow" in names else 0
    prefix = b"\0" * 0x29 + struct.pack("<i", 1) + struct.pack("<ii", row_name_idx, 0)
    props = parse_rows(prefix + value_blob, names)[0]["props"]
    return props, len(prefix)


def nested_value_offset(prop, key, base_off):
    offset = prop[key] - base_off
    if offset < 0:
        raise RuntimeError(f"nested property offset underflow for {key}: {prop[key]} < {base_off}")
    return offset


def validate_dd_totem_group_patch(patched_uexp, names):
    rows = parse_rows(patched_uexp, names)
    map_offsets = find_map_value_offsets(patched_uexp, names)
    dd_row = None
    for row, map_value_off in zip(rows, map_offsets):
        map_name_idx, map_num = struct.unpack_from("<ii", patched_uexp, map_value_off)
        map_name = names[map_name_idx] if 0 <= map_name_idx < len(names) else f"<idx {map_name_idx}>"
        if map_name == "DeepDesert" and map_num == 0:
            dd_row = row
            break
    if dd_row is None:
        raise RuntimeError("patched validation failed: DeepDesert row not found")

    dd_default = dd_row["props"]["m_DefaultRegionData"]
    dd_pvp = dd_row["props"]["m_PvpRegionData"]
    dd_bool = dd_row["props"]["m_bOverridePvpRegionData"]
    if dd_bool.get("bool_value") != 0:
        raise RuntimeError("patched validation failed: DeepDesert PVP override remains enabled")
    if dd_pvp["size"] != len(build_empty_map_region_struct()):
        raise RuntimeError(f"patched validation failed: unexpected PVP region size {dd_pvp['size']}")

    default_value = patched_uexp[dd_default["value_off"]:dd_default["end"]]
    default_props, _ = parse_struct_value_props(default_value, names)
    expected_default_props = {
        "m_DefaultModifiers",
        "m_MapAreaModifiersData",
        "m_BuildableGroupsModifiersData",
    }
    actual_default_props = set(default_props)
    if actual_default_props != expected_default_props:
        raise RuntimeError(
            "patched validation failed: unexpected nested DeepDesert default props "
            f"{sorted(actual_default_props)}"
        )
    group_mods = default_props["m_BuildableGroupsModifiersData"]
    if group_mods.get("count") != 1:
        raise RuntimeError(
            "patched validation failed: expected one DeepDesert buildable group modifier, "
            f"found {group_mods.get('count')}"
        )
    return {
        "dd_buildable_group_modifier_count": group_mods.get("count"),
        "dd_buildable_group_modifier_size": group_mods["size"],
    }


def patch_uexp(uexp, names):
    idx = {name: i for i, name in enumerate(names)}
    hagga = struct.pack("<ii", idx["HaggaBasin"], 0)
    deep = struct.pack("<ii", idx["DeepDesert"], 0)
    first, second = find_map_value_offsets(uexp, names)
    current = (uexp[first:first + 8], uexp[second:second + 8])
    if current == (deep, hagga):
        return uexp, (first, second), True
    if current != (hagga, deep):
        def describe(value):
            name_idx, number = struct.unpack("<ii", value)
            name = names[name_idx] if 0 <= name_idx < len(names) else f"<idx {name_idx}>"
            return f"{name}:{number}"
        raise RuntimeError(
            "unexpected m_Map values: "
            f"first={describe(current[0])} second={describe(current[1])}"
        )
    patched = bytearray(uexp)
    patched[first:first + 8] = deep
    patched[second:second + 8] = hagga
    return bytes(patched), (first, second), False


def patch_uexp_dd_totem_groups(uexp, names):
    rows = parse_rows(uexp, names)
    if len(rows) != 2:
        raise RuntimeError(f"expected 2 DT_BuildableMapRegion rows, found {len(rows)}")

    map_offsets = find_map_value_offsets(uexp, names)
    dd_row = None
    hagga_row = None
    for row, map_value_off in zip(rows, map_offsets):
        map_name_idx, map_num = struct.unpack_from("<ii", uexp, map_value_off)
        map_name = names[map_name_idx] if 0 <= map_name_idx < len(names) else f"<idx {map_name_idx}>"
        if map_num != 0:
            raise RuntimeError(f"unexpected enum number {map_num} for {map_name}")
        if map_name == "DeepDesert":
            dd_row = row
        elif map_name == "HaggaBasin":
            hagga_row = row
    if dd_row is None or hagga_row is None:
        raise RuntimeError("failed to locate DeepDesert and HaggaBasin rows")

    dd_default = dd_row["props"]["m_DefaultRegionData"]
    hagga_default = hagga_row["props"]["m_DefaultRegionData"]
    dd_pvp = dd_row["props"]["m_PvpRegionData"]
    dd_bool = dd_row["props"]["m_bOverridePvpRegionData"]

    dd_default_value = uexp[dd_default["value_off"]:dd_default["end"]]
    hagga_default_value = uexp[hagga_default["value_off"]:hagga_default["end"]]
    dd_pvp_tag = uexp[dd_pvp["tag_off"]:dd_pvp["value_off"]]
    dd_default_tag = uexp[dd_default["tag_off"]:dd_default["value_off"]]
    dd_bool_tag = uexp[dd_bool["tag_off"]:dd_bool["end"]]

    dd_rows_default, dd_nested_base = parse_struct_value_props(dd_default_value, names)
    hagga_rows_default, hagga_nested_base = parse_struct_value_props(hagga_default_value, names)

    dd_modifiers = dd_default_value[
        :nested_value_offset(dd_rows_default["m_DefaultModifiers"], "end", dd_nested_base)
    ]
    dd_area_mods = dd_default_value[
        nested_value_offset(dd_rows_default["m_MapAreaModifiersData"], "tag_off", dd_nested_base):
        nested_value_offset(dd_rows_default["m_MapAreaModifiersData"], "end", dd_nested_base)
    ]
    hagga_group_mods = hagga_default_value[
        nested_value_offset(hagga_rows_default["m_BuildableGroupsModifiersData"], "tag_off", hagga_nested_base):
        nested_value_offset(hagga_rows_default["m_BuildableGroupsModifiersData"], "end", hagga_nested_base)
    ]
    dd_default_none = dd_default_value[
        nested_value_offset(dd_rows_default["m_BuildableGroupsModifiersData"], "end", dd_nested_base):
    ]

    new_dd_default_value = dd_modifiers + dd_area_mods + hagga_group_mods + dd_default_none
    new_dd_default_prop = encode_struct_property(dd_default_tag, new_dd_default_value)
    new_dd_pvp_prop = encode_struct_property(dd_pvp_tag, build_empty_map_region_struct())
    new_dd_bool_prop = encode_bool_property(dd_bool_tag, False)

    prefix = uexp[:dd_default["tag_off"]]
    suffix = uexp[dd_row["end"]:]
    mid = (
        new_dd_default_prop
        + new_dd_bool_prop
        + new_dd_pvp_prop
        + struct.pack("<ii", 29, 0)
    )
    new_blob = prefix + mid + suffix
    if len(new_blob) > len(uexp):
        raise RuntimeError(
            f"dd-totem-groups patch grew uexp by {len(new_blob) - len(uexp)} bytes; cannot fit"
        )
    padded = new_blob + (b"\0" * (len(uexp) - len(new_blob)))
    validation = validate_dd_totem_group_patch(padded, names)
    return padded, {
        "dd_default_size": len(new_dd_default_value),
        "dd_pvp_size": len(build_empty_map_region_struct()),
        "dd_override_pvp": False,
        **validation,
    }


def validate_dd_full_region_patch(patched_uexp, names, expected_default_size, expected_pvp_size):
    rows = parse_rows(patched_uexp, names)
    map_offsets = find_map_value_offsets(patched_uexp, names)
    dd_row = None
    for row, map_value_off in zip(rows, map_offsets):
        map_name_idx, map_num = struct.unpack_from("<ii", patched_uexp, map_value_off)
        map_name = names[map_name_idx] if 0 <= map_name_idx < len(names) else f"<idx {map_name_idx}>"
        if map_name == "DeepDesert" and map_num == 0:
            dd_row = row
            break
    if dd_row is None:
        raise RuntimeError("full-region validation failed: DeepDesert row not found")
    dd_default = dd_row["props"]["m_DefaultRegionData"]
    dd_pvp = dd_row["props"]["m_PvpRegionData"]
    if dd_default["size"] != expected_default_size:
        raise RuntimeError(
            f"full-region validation failed: DeepDesert default region size {dd_default['size']} "
            f"!= Hagga {expected_default_size}"
        )
    if dd_pvp["size"] != expected_pvp_size:
        raise RuntimeError(
            f"full-region validation failed: DeepDesert PVP region size {dd_pvp['size']} "
            f"!= Hagga {expected_pvp_size}"
        )
    return {"dd_default_size": dd_default["size"], "dd_pvp_size": dd_pvp["size"]}


def patch_uexp_dd_full_region(uexp, names):
    """Copy Hagga's full buildable-region data into the DeepDesert row.

    DeepDesert keeps m_Map=DeepDesert but receives Hagga's complete
    m_DefaultRegionData, m_PvpRegionData, and m_bOverridePvpRegionData. The
    HaggaBasin row is left untouched. This is the maximal permissive-region
    candidate: if the BRT region gate is server-authoritative, DD becomes as
    buildable as Hagga for both save and place.
    """
    rows = parse_rows(uexp, names)
    if len(rows) != 2:
        raise RuntimeError(f"expected 2 DT_BuildableMapRegion rows, found {len(rows)}")

    map_offsets = find_map_value_offsets(uexp, names)
    dd_row = None
    hagga_row = None
    for row, map_value_off in zip(rows, map_offsets):
        map_name_idx, map_num = struct.unpack_from("<ii", uexp, map_value_off)
        map_name = names[map_name_idx] if 0 <= map_name_idx < len(names) else f"<idx {map_name_idx}>"
        if map_num != 0:
            raise RuntimeError(f"unexpected enum number {map_num} for {map_name}")
        if map_name == "DeepDesert":
            dd_row = row
        elif map_name == "HaggaBasin":
            hagga_row = row
    if dd_row is None or hagga_row is None:
        raise RuntimeError("failed to locate DeepDesert and HaggaBasin rows")

    dd_default = dd_row["props"]["m_DefaultRegionData"]
    dd_pvp = dd_row["props"]["m_PvpRegionData"]
    dd_bool = dd_row["props"]["m_bOverridePvpRegionData"]
    hagga_default = hagga_row["props"]["m_DefaultRegionData"]
    hagga_pvp = hagga_row["props"]["m_PvpRegionData"]
    hagga_bool = hagga_row["props"]["m_bOverridePvpRegionData"]

    hagga_default_value = uexp[hagga_default["value_off"]:hagga_default["end"]]
    hagga_pvp_value = uexp[hagga_pvp["value_off"]:hagga_pvp["end"]]
    hagga_bool_value = bool(hagga_bool.get("bool_value"))

    dd_default_tag = uexp[dd_default["tag_off"]:dd_default["value_off"]]
    dd_pvp_tag = uexp[dd_pvp["tag_off"]:dd_pvp["value_off"]]
    dd_bool_tag = uexp[dd_bool["tag_off"]:dd_bool["end"]]

    new_dd_default_prop = encode_struct_property(dd_default_tag, hagga_default_value)
    new_dd_pvp_prop = encode_struct_property(dd_pvp_tag, hagga_pvp_value)
    new_dd_bool_prop = encode_bool_property(dd_bool_tag, hagga_bool_value)

    prefix = uexp[:dd_default["tag_off"]]
    suffix = uexp[dd_row["end"]:]
    mid = (
        new_dd_default_prop
        + new_dd_bool_prop
        + new_dd_pvp_prop
        + struct.pack("<ii", NONE_NAME_INDEX, 0)
    )
    new_blob = prefix + mid + suffix
    # Overlay packing recompresses, so the uexp may grow vs the source slot.
    validation = validate_dd_full_region_patch(
        new_blob, names, len(hagga_default_value), len(hagga_pvp_value)
    )
    return new_blob, {
        "dd_default_size": len(hagga_default_value),
        "dd_pvp_size": len(hagga_pvp_value),
        "dd_override_pvp": hagga_bool_value,
        "grewBy": len(new_blob) - len(uexp),
        **validation,
    }


def build_replacement_entry_blob(old_blob, compressed_blob, uncompressed_size):
    if len(old_blob) < 73:
        raise RuntimeError("old pak entry blob too small to template")
    header = bytearray(old_blob[:73])
    struct.pack_into("<Q", header, 8, len(compressed_blob))
    struct.pack_into("<Q", header, 16, uncompressed_size)
    header[28:48] = __import__("hashlib").sha1(compressed_blob, usedforsecurity=False).digest()
    struct.pack_into("<Q", header, 60, 73 + len(compressed_blob))
    return bytes(header) + compressed_blob


def rewrite_encoded_index_entry_at(data, index_offset, entry, new_offset, new_uncompressed, new_compressed):
    pos = index_offset + entry["rel"]
    encoded = bytearray(data[pos:pos + entry["encoded_size"]])
    if len(encoded) != entry["encoded_size"]:
        raise RuntimeError("short encoded index entry")

    bitfield = struct.unpack_from("<I", encoded, 0)[0]
    cursor = 4
    if (bitfield & 0x3F) == 0x3F:
        cursor += 4

    fields = [("offset", 31), ("uncompressed", 30)]
    if entry["compression"]:
        fields.append(("compressed", 29))

    values = {
        "offset": new_offset,
        "uncompressed": new_uncompressed,
        "compressed": new_compressed,
    }
    for field_name, bit in fields:
        width = 4 if (bitfield & (1 << bit)) else 8
        value = values[field_name]
        if width == 4:
            if not (0 <= value <= 0xFFFFFFFF):
                raise RuntimeError(f"{field_name} value {value} does not fit packed uint32")
            struct.pack_into("<I", encoded, cursor, value)
        else:
            struct.pack_into("<Q", encoded, cursor, value)
        cursor += width

    data[pos:pos + entry["encoded_size"]] = encoded


def patch_entry_header_in_place(data, entry_offset, compressed_blob, uncompressed_size):
    header = bytearray(data[entry_offset:entry_offset + 73])
    if len(header) != 73:
        raise RuntimeError("short entry header")
    struct.pack_into("<Q", header, 8, len(compressed_blob))
    struct.pack_into("<Q", header, 16, uncompressed_size)
    header[28:48] = hashlib.sha1(compressed_blob, usedforsecurity=False).digest()
    struct.pack_into("<Q", header, 60, 73 + len(compressed_blob))
    data[entry_offset:entry_offset + 73] = header


def find_footer_base(data):
    magic_bytes = struct.pack("<I", PAK_MAGIC)
    magic_pos = data.rfind(magic_bytes, max(0, len(data) - 512))
    if magic_pos < 20:
        raise RuntimeError("pak footer magic not found near end of file")
    return magic_pos - 20


def rewrite_footer_index_hash(data, footer_base, footer):
    index_offset = footer["index_offset"]
    index_size = footer["index_size"]
    index_hash = hashlib.sha1(bytes(data[index_offset:index_offset + index_size]), usedforsecurity=False).digest()
    data[footer_base + 44:footer_base + 64] = index_hash


def rewrite_footer_offsets(data, footer_base, new_index_offset, index_size):
    struct.pack_into("<Q", data, footer_base + 28, new_index_offset)
    struct.pack_into("<Q", data, footer_base + 36, index_size)


def rewrite_pak_with_shift(data, footer, all_entries, target_entry, compressed_blob, patched_uexp):
    payload_start = pak_entry_payload_start(target_entry)
    payload_end = payload_start + target_entry["compressed"]
    delta = len(compressed_blob) - target_entry["compressed"]
    if delta <= 0:
        raise RuntimeError("rewrite_pak_with_shift expects a grown payload")

    patch_entry_header_in_place(data, target_entry["offset"], compressed_blob, len(patched_uexp))
    data[payload_start:payload_end] = compressed_blob

    new_index_offset = footer["index_offset"] + delta
    new_footer_base = find_footer_base(data)
    rewrite_footer_offsets(data, new_footer_base, new_index_offset, footer["index_size"])

    for entry in all_entries:
        if entry["rel"] == target_entry["rel"]:
            rewrite_encoded_index_entry_at(
                data,
                new_index_offset,
                entry,
                entry["offset"],
                len(patched_uexp),
                len(compressed_blob),
            )
        elif entry["offset"] > target_entry["offset"]:
            rewrite_encoded_index_entry_at(
                data,
                new_index_offset,
                entry,
                entry["offset"] + delta,
                entry["uncompressed"],
                entry["compressed"],
            )

    updated_footer = {
        "version": footer["version"],
        "index_offset": new_index_offset,
        "index_size": footer["index_size"],
    }
    rewrite_footer_index_hash(data, new_footer_base, updated_footer)
    return data


def suppress_stderr(enabled):
    if not enabled:
        return None
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)
    return saved


def restore_stderr(saved):
    if saved is not None:
        os.dup2(saved, 2)
        os.close(saved)


def find_target_entries(data, footer, decompress):
    decoded = []
    target_uasset = None
    index_offset = footer["index_offset"]
    index_size = footer["index_size"]
    for rel in range(0, index_size - 16):
        entry = plausible_entry(data, index_offset, rel)
        if not entry:
            continue
        if decoded and rel < decoded[-1][0]["rel"] + decoded[-1][0]["encoded_size"]:
            continue
        try:
            blob = decompress_entry(data, entry, decompress)
        except Exception:
            continue
        decoded.append((entry, blob))
        if TARGET_TABLE in blob:
            target_uasset = (entry, blob)
            continue
        if target_uasset is not None:
            names = parse_uasset_names(target_uasset[1])
            try:
                find_map_value_offsets(blob, names)
            except Exception:
                continue
            return target_uasset[0], target_uasset[1], entry, blob

    raise RuntimeError("target DT_BuildableMapRegion uasset/uexp pair was not found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pak", type=Path, default=DEFAULT_PAK)
    parser.add_argument("--oodle", type=Path, default=Path(os.environ.get("DUNE_OODLE_LIBRARY", DEFAULT_OODLE)))
    parser.add_argument(
        "--mode",
        choices=[MODE_SWAP_MAP_ROWS, MODE_DD_TOTEM_GROUPS, MODE_DD_FULL_REGION],
        default=MODE_SWAP_MAP_ROWS,
    )
    parser.add_argument(
        "--emit-overlay-dir",
        type=Path,
        help="write patched DT_BuildableMapRegion.uasset/.uexp under this root instead of rewriting the source pak",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet-oodle", action="store_true", default=True)
    args = parser.parse_args()

    if not args.pak.exists():
        raise SystemExit(f"missing pak: {args.pak}")
    if not args.oodle.exists():
        raise SystemExit(f"missing Oodle library: {args.oodle}")
    refuse_client_write(args.pak, args.dry_run)

    data = bytearray(args.pak.read_bytes())
    footer = read_footer(data)
    if footer["version"] != 11:
        raise SystemExit(f"unsupported pak version {footer['version']}; expected 11")

    decompress, compress = load_oodle(args.oodle)
    saved_stderr = suppress_stderr(args.quiet_oodle)
    try:
        uasset_entry, uasset_blob, uexp_entry, uexp_blob = find_target_entries(data, footer, decompress)
        all_entries = []
        for rel in range(0, footer["index_size"] - 16):
            maybe = plausible_entry(data, footer["index_offset"], rel)
            if not maybe:
                continue
            if all_entries and rel < all_entries[-1]["rel"] + all_entries[-1]["encoded_size"]:
                continue
            all_entries.append(maybe)
        names = parse_uasset_names(uasset_blob)
        if args.mode == MODE_SWAP_MAP_ROWS:
            patched_uexp, offsets, already_patched = patch_uexp(uexp_blob, names)
            summary_bits = [
                f"mode={args.mode}",
                f"mapValueOffsets={hex(offsets[0])},{hex(offsets[1])}",
                "HaggaBasin<->DeepDesert",
            ]
            allow_append_rewrite = False
        elif args.mode == MODE_DD_FULL_REGION:
            patched_uexp, summary = patch_uexp_dd_full_region(uexp_blob, names)
            already_patched = patched_uexp == uexp_blob
            summary_bits = [
                f"mode={args.mode}",
                f"ddDefaultRegionSize={summary['dd_default_size']}",
                f"ddPvpRegionSize={summary['dd_pvp_size']}",
                f"ddOverridePvp={summary['dd_override_pvp']}",
                f"grewBy={summary['grewBy']}",
            ]
            allow_append_rewrite = True
        else:
            patched_uexp, summary = patch_uexp_dd_totem_groups(uexp_blob, names)
            already_patched = patched_uexp == uexp_blob
            summary_bits = [
                f"mode={args.mode}",
                f"ddDefaultRegionSize={summary['dd_default_size']}",
                f"ddPvpRegionSize={summary['dd_pvp_size']}",
                f"ddOverridePvp={summary['dd_override_pvp']}",
                f"ddBuildableGroupModifiers={summary['dd_buildable_group_modifier_count']}",
                f"ddBuildableGroupModifiersSize={summary['dd_buildable_group_modifier_size']}",
            ]
            allow_append_rewrite = True
        if already_patched:
            compressed_size = uexp_entry["compressed"]
            compressor = "already-patched"
            level = "already-patched"
            replacement_entry_blob = None
            rewrite_mode = "in-place"
        else:
            rewrite_mode = "in-place"
            replacement_entry_blob = None
            try:
                compressed, compressed_size, compressor, level = compress_blob(
                    patched_uexp,
                    uexp_entry["compressed"],
                    compress,
                    decompress,
                )
            except RuntimeError as exc:
                if not allow_append_rewrite or "within original" not in str(exc):
                    raise
                compressed, compressed_size, compressor, level = compress_blob_unbounded(
                    patched_uexp,
                    compress,
                    decompress,
                )
                rewrite_mode = "in-place-shift"
    finally:
        restore_stderr(saved_stderr)

    payload_start = pak_entry_payload_start(uexp_entry)
    payload_end = payload_start + uexp_entry["compressed"]
    print(
        "BRT Deep Desert buildable-region pak patch:",
        f"pak={args.pak}",
        f"uassetIndexRel=0x{uasset_entry['rel']:x}",
        f"uexpIndexRel=0x{uexp_entry['rel']:x}",
        f"uexpPakOffset={uexp_entry['offset']}",
        *summary_bits,
        f"compressed={compressed_size}/{uexp_entry['compressed']}",
        f"compressor={compressor}",
        f"level={level}",
        f"rewrite={rewrite_mode}",
        *( [f"overlayDir={args.emit_overlay_dir}"] if args.emit_overlay_dir else [] ),
    )
    if args.dry_run:
        print("dry-run: pak not modified")
        return 0
    if args.emit_overlay_dir is not None:
        uasset_path, uexp_path = write_overlay_assets(args.emit_overlay_dir, uasset_blob, patched_uexp)
        print(
            "wrote overlay assets successfully",
            f"uasset={uasset_path}",
            f"uexp={uexp_path}",
        )
        return 0
    if already_patched:
        print("pak already patched")
        return 0

    if replacement_entry_blob is None and rewrite_mode != "in-place-shift":
        data[payload_start:payload_end] = compressed
        footer_base = find_footer_base(data)
        rewrite_footer_index_hash(data, footer_base, footer)
        out = data
    elif rewrite_mode == "in-place-shift":
        out = rewrite_pak_with_shift(data, footer, all_entries, uexp_entry, compressed, patched_uexp)
    else:
        footer_base = find_footer_base(data)
        new_offset = footer_base
        rewrite_encoded_index_entry_at(
            data,
            footer["index_offset"],
            uexp_entry,
            new_offset,
            len(patched_uexp),
            compressed_size,
        )
        rewrite_footer_index_hash(data, footer_base, footer)
        out = data[:footer_base] + replacement_entry_blob + data[footer_base:]
    args.pak.write_bytes(out)
    print("patched pak successfully")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"patch-brt-dd-buildable-map-region-pak: {exc}", file=sys.stderr)
        raise SystemExit(1)
