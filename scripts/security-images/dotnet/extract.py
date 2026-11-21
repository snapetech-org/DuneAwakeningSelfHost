#!/usr/bin/env python3
import hashlib
import json
import pathlib
import struct
import sys
import zlib
from collections import Counter

if len(sys.argv) != 3:
    raise SystemExit(f"usage: {sys.argv[0]} INPUT_BUNDLE OUTPUT_DIR")
source = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
if source.stat().st_size > 512 * 1024**2:
    raise SystemExit("bundle exceeds size limit")
data = source.read_bytes()
signature = bytes.fromhex("8b1202b96a612038727b930214d7a03213f5b9e6efae3318ee3b2dce24b36aae")
markers = []
pos = 0
while (found := data.find(signature, pos)) >= 0:
    markers.append(found)
    pos = found + 1
if len(markers) != 1:
    raise SystemExit(f"expected one bundle signature, found {len(markers)}")
marker = markers[0]
header_offset = struct.unpack_from("<q", data, marker - 8)[0]
if not marker < header_offset < len(data):
    raise SystemExit(f"invalid bundle header offset {header_offset}")
pos = header_offset

def read(fmt):
    global pos
    size = struct.calcsize(fmt)
    if pos + size > len(data):
        raise SystemExit("truncated manifest")
    value = struct.unpack_from(fmt, data, pos)[0]
    pos += size
    return value

def read_string():
    global pos
    length = 0
    shift = 0
    while shift < 35:
        if pos >= len(data):
            raise SystemExit("truncated string length")
        octet = data[pos]
        pos += 1
        length |= (octet & 0x7F) << shift
        if octet < 0x80:
            break
        shift += 7
    else:
        raise SystemExit("invalid 7-bit string length")
    if length > len(data) - pos:
        raise SystemExit("string length exceeds manifest")
    raw = data[pos:pos + length]
    pos += length
    return raw.decode("utf-8")

major = read("<I")
minor = read("<I")
count = read("<i")
bundle_id = read_string()
if not 1 <= major <= 6 or minor != 0 or not 0 < count <= 100000:
    raise SystemExit(f"unsupported manifest header {major}.{minor}, entries={count}")
if major >= 2:
    deps_offset, deps_size, runtime_offset, runtime_size = (read("<q"), read("<q"), read("<q"), read("<q"))
    flags = read("<Q")
else:
    deps_offset = deps_size = runtime_offset = runtime_size = flags = 0
entries = []
seen = set()
for _ in range(count):
    offset = read("<q")
    size = read("<q")
    compressed = read("<q") if major >= 6 else 0
    kind = read("<B")
    name = read_string()
    rel = pathlib.PurePosixPath(name)
    if not name or name != rel.as_posix() or name in {".", "extraction-manifest.json"} or rel.is_absolute() or ".." in rel.parts or "\\" in name or name in seen:
        raise SystemExit(f"unsafe or duplicate path {name!r}")
    seen.add(name)
    stored = compressed or size
    if offset < 0 or size < 0 or compressed < 0 or offset + stored > header_offset:
        raise SystemExit(f"invalid content range for {name!r}")
    entries.append({"offset": offset, "size": size, "compressed": compressed, "kind": kind, "name": name})
if sum(entry["size"] for entry in entries) > 1024**3:
    raise SystemExit("expanded bundle exceeds size limit")
if pos > len(data) or header_offset < min(e["offset"] for e in entries):
    raise SystemExit("manifest ordering or content offsets invalid")
for key, off, size in (("deps", deps_offset, deps_size), ("runtimeconfig", runtime_offset, runtime_size)):
    if size and not any(e["offset"] == off and e["size"] == size for e in entries):
        raise SystemExit(f"header {key} reference does not match an entry")
if output.exists():
    raise SystemExit(f"output already exists: {output}")
output.mkdir(parents=True, mode=0o700)
for entry in entries:
    start = entry["offset"]
    stored = entry["compressed"] or entry["size"]
    payload = data[start:start + stored]
    if entry["compressed"]:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        payload = decoder.decompress(payload, entry["size"] + 1)
        if not decoder.eof or decoder.unused_data:
            raise SystemExit("invalid compressed entry")
    if len(payload) != entry["size"]:
        raise SystemExit(f"size mismatch for {entry['name']}: {len(payload)} != {entry['size']}")
    target = output.joinpath(*pathlib.PurePosixPath(entry["name"]).parts)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(payload)
    target.chmod(0o600)
result = {
    "source_sha256": hashlib.sha256(data).hexdigest(),
    "bundle_version": f"{major}.{minor}",
    "bundle_id": bundle_id,
    "header_offset": header_offset,
    "manifest_end": pos,
    "bundle_bytes": len(data),
    "embedded_file_count": count,
    "file_type_counts": dict(Counter(e["kind"] for e in entries)),
    "files": [e["name"] for e in entries],
}
(output / "extraction-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
(output / "extraction-manifest.json").chmod(0o600)
print(json.dumps({k: v for k, v in result.items() if k != "files"}, indent=2))
