#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${DUNE_CURRENT_HOST:-kspls0}"
RUN_LOCAL=false
EXPECTED_BUILD_ID="${DUNE_LOGOFF_TIMER_BUILD_ID:-6f8ca9ee5f3420c0b4c1ef7cefb412347bcba04b}"
AUTO_REMAP_ENABLED="${DUNE_LOGOFF_TIMER_AUTO_REMAP_ENABLED:-true}"
TARGET_VALUE="${DUNE_LOGOFF_TIMER_VALUE:-0.0}"
TARGET_CONTAINERS="${DUNE_LOGOFF_TIMER_CONTAINERS:-dune_server-survival-1 dune_server-deep-desert-1 dune_server-deep-desert-pvp-1}"
DIRECT_ARRAYS="${DUNE_LOGOFF_TIMER_DIRECT_ARRAYS:-false}"
BACKEND_ONLY="${DUNE_LOGOFF_TIMER_BACKEND_ONLY:-false}"

case "$EXPECTED_BUILD_ID" in
  50d73d6403583bbcad919119c08d5083181c4d51)
    DEFAULT_VALUE_A_OFFSET="0x1731ebf0"
    DEFAULT_VALUE_B_OFFSET="0x1731ec08"
    DEFAULT_DEADLINE_CLAMP_OFFSET="0x0da907d4"
    DEFAULT_TIMER_DURATION_ZERO_OFFSET="0x0da90be1"
    DEFAULT_UI_VALUE_A_OFFSET=""
    DEFAULT_UI_VALUE_B_OFFSET=""
    DEFAULT_UI_VALUE_C_OFFSET=""
    DEFAULT_DIALOG_TRIPLE_OFFSET=""
    DIRECT_ARRAYS="false"
    BACKEND_ONLY="true"
    ;;
  9bf5fbdef43a6d6d64459df973f3d252c01ab4ad)
    DEFAULT_VALUE_A_OFFSET="0x16521698"
    DEFAULT_VALUE_B_OFFSET="0x165216b0"
    DEFAULT_DEADLINE_CLAMP_OFFSET="0xd50f864"
    DEFAULT_TIMER_DURATION_ZERO_OFFSET="0xd50fcba"
    DEFAULT_UI_VALUE_A_OFFSET="0x16523ce0"
    DEFAULT_UI_VALUE_B_OFFSET="0x16523d10"
    DEFAULT_UI_VALUE_C_OFFSET="0x16523d28"
    DEFAULT_DIALOG_TRIPLE_OFFSET=""
    ;;
  caebf04f4447a65da2e3df7a1a6b1593937af793)
    DEFAULT_VALUE_A_OFFSET="0x1651e498"
    DEFAULT_VALUE_B_OFFSET="0x1651e4b0"
    DEFAULT_DEADLINE_CLAMP_OFFSET="0xd50d404"
    DEFAULT_TIMER_DURATION_ZERO_OFFSET="0xd50d85a"
    DEFAULT_UI_VALUE_A_OFFSET="0x16520ae0"
    DEFAULT_UI_VALUE_B_OFFSET="0x16520b10"
    DEFAULT_UI_VALUE_C_OFFSET="0x16520b28"
    DEFAULT_DIALOG_TRIPLE_OFFSET=""
    ;;
  6f8ca9ee5f3420c0b4c1ef7cefb412347bcba04b)
    DEFAULT_VALUE_A_OFFSET="0x1652e898"
    DEFAULT_VALUE_B_OFFSET="0x1652e8b0"
    DEFAULT_DEADLINE_CLAMP_OFFSET="0xd515424"
    DEFAULT_TIMER_DURATION_ZERO_OFFSET="0xd51587a"
    DEFAULT_UI_VALUE_A_OFFSET="0x16530ee0"
    DEFAULT_UI_VALUE_B_OFFSET="0x16530f10"
    DEFAULT_UI_VALUE_C_OFFSET="0x16530f28"
    DEFAULT_DIALOG_TRIPLE_OFFSET=""
    ;;
  427a3084dcc00057ad21f98555a7d17d5f3c1020)
    DEFAULT_VALUE_A_OFFSET="0x16649ab0"
    DEFAULT_VALUE_B_OFFSET="0x16649ac8"
    DEFAULT_DEADLINE_CLAMP_OFFSET=""
    DEFAULT_TIMER_DURATION_ZERO_OFFSET=""
    DEFAULT_UI_VALUE_A_OFFSET="0x16649ab0"
    DEFAULT_UI_VALUE_B_OFFSET="0x16649ac8"
    DEFAULT_UI_VALUE_C_OFFSET="0x16649ac8"
    DEFAULT_DIALOG_TRIPLE_OFFSET="0x16905c58"
    DIRECT_ARRAYS="${DUNE_LOGOFF_TIMER_DIRECT_ARRAYS:-true}"
    ;;
  cde875918e7dee23366c71e0d7bc20237810ea92)
    DEFAULT_VALUE_A_OFFSET="0x165a7488"
    DEFAULT_VALUE_B_OFFSET="0x165a74a0"
    DEFAULT_DEADLINE_CLAMP_OFFSET="0xd551604"
    DEFAULT_TIMER_DURATION_ZERO_OFFSET="0xd551a5a"
    DEFAULT_UI_VALUE_A_OFFSET="0x165a91d8"
    DEFAULT_UI_VALUE_B_OFFSET="0x165a9a78"
    DEFAULT_UI_VALUE_C_OFFSET="0x165a9aa8"
    DEFAULT_DIALOG_TRIPLE_OFFSET=""
    ;;
  *)
    if [[ -n "${DUNE_LOGOFF_TIMER_VALUE_A_OFFSET:-}" &&
          -n "${DUNE_LOGOFF_TIMER_VALUE_B_OFFSET:-}" &&
          -n "${DUNE_LOGOFF_TIMER_DEADLINE_CLAMP_OFFSET:-}" &&
          -n "${DUNE_LOGOFF_TIMER_DURATION_ZERO_OFFSET:-}" &&
          -n "${DUNE_LOGOFF_TIMER_UI_VALUE_A_OFFSET:-}" &&
          -n "${DUNE_LOGOFF_TIMER_UI_VALUE_B_OFFSET:-}" &&
          -n "${DUNE_LOGOFF_TIMER_UI_VALUE_C_OFFSET:-}" ]]; then
      DEFAULT_VALUE_A_OFFSET="$DUNE_LOGOFF_TIMER_VALUE_A_OFFSET"
      DEFAULT_VALUE_B_OFFSET="$DUNE_LOGOFF_TIMER_VALUE_B_OFFSET"
      DEFAULT_DEADLINE_CLAMP_OFFSET="$DUNE_LOGOFF_TIMER_DEADLINE_CLAMP_OFFSET"
      DEFAULT_TIMER_DURATION_ZERO_OFFSET="$DUNE_LOGOFF_TIMER_DURATION_ZERO_OFFSET"
      DEFAULT_UI_VALUE_A_OFFSET="$DUNE_LOGOFF_TIMER_UI_VALUE_A_OFFSET"
      DEFAULT_UI_VALUE_B_OFFSET="$DUNE_LOGOFF_TIMER_UI_VALUE_B_OFFSET"
      DEFAULT_UI_VALUE_C_OFFSET="$DUNE_LOGOFF_TIMER_UI_VALUE_C_OFFSET"
    elif [[ "$AUTO_REMAP_ENABLED" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ ]]; then
      DEFAULT_VALUE_A_OFFSET=""
      DEFAULT_VALUE_B_OFFSET=""
      DEFAULT_DEADLINE_CLAMP_OFFSET=""
      DEFAULT_TIMER_DURATION_ZERO_OFFSET=""
      DEFAULT_UI_VALUE_A_OFFSET=""
      DEFAULT_UI_VALUE_B_OFFSET=""
      DEFAULT_UI_VALUE_C_OFFSET=""
      DEFAULT_DIALOG_TRIPLE_OFFSET=""
    else
      : "${DUNE_LOGOFF_TIMER_VALUE_A_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_VALUE_A_OFFSET}"
      : "${DUNE_LOGOFF_TIMER_VALUE_B_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_VALUE_B_OFFSET}"
      : "${DUNE_LOGOFF_TIMER_DEADLINE_CLAMP_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_DEADLINE_CLAMP_OFFSET}"
      : "${DUNE_LOGOFF_TIMER_DURATION_ZERO_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_DURATION_ZERO_OFFSET}"
      : "${DUNE_LOGOFF_TIMER_UI_VALUE_A_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_UI_VALUE_A_OFFSET}"
      : "${DUNE_LOGOFF_TIMER_UI_VALUE_B_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_UI_VALUE_B_OFFSET}"
      : "${DUNE_LOGOFF_TIMER_UI_VALUE_C_OFFSET:?unknown build: set DUNE_LOGOFF_TIMER_UI_VALUE_C_OFFSET}"
    fi
    ;;
esac

VALUE_A_OFFSET="${DUNE_LOGOFF_TIMER_VALUE_A_OFFSET:-${DEFAULT_VALUE_A_OFFSET:-}}"
VALUE_B_OFFSET="${DUNE_LOGOFF_TIMER_VALUE_B_OFFSET:-${DEFAULT_VALUE_B_OFFSET:-}}"
DEADLINE_CLAMP_OFFSET="${DUNE_LOGOFF_TIMER_DEADLINE_CLAMP_OFFSET:-${DEFAULT_DEADLINE_CLAMP_OFFSET:-}}"
TIMER_DURATION_ZERO_OFFSET="${DUNE_LOGOFF_TIMER_DURATION_ZERO_OFFSET:-${DEFAULT_TIMER_DURATION_ZERO_OFFSET:-}}"
UI_VALUE_A_OFFSET="${DUNE_LOGOFF_TIMER_UI_VALUE_A_OFFSET:-${DEFAULT_UI_VALUE_A_OFFSET:-}}"
UI_VALUE_B_OFFSET="${DUNE_LOGOFF_TIMER_UI_VALUE_B_OFFSET:-${DEFAULT_UI_VALUE_B_OFFSET:-}}"
UI_VALUE_C_OFFSET="${DUNE_LOGOFF_TIMER_UI_VALUE_C_OFFSET:-${DEFAULT_UI_VALUE_C_OFFSET:-}}"
DIALOG_TRIPLE_OFFSET="${DUNE_LOGOFF_TIMER_DIALOG_TRIPLE_OFFSET:-${DEFAULT_DIALOG_TRIPLE_OFFSET:-}}"

MODE="apply"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host after --host}"
      shift 2
      ;;
    --local)
      RUN_LOCAL=true
      shift
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    *)
      echo "usage: $0 [--host HOST | --local] [--dry-run]" >&2
      exit 2
      ;;
  esac
done

env_command=(
  "EXPECTED_BUILD_ID='$EXPECTED_BUILD_ID'"
  "VALUE_A_OFFSET='$VALUE_A_OFFSET'"
  "VALUE_B_OFFSET='$VALUE_B_OFFSET'"
  "DEADLINE_CLAMP_OFFSET='$DEADLINE_CLAMP_OFFSET'"
  "TIMER_DURATION_ZERO_OFFSET='$TIMER_DURATION_ZERO_OFFSET'"
  "UI_VALUE_A_OFFSET='$UI_VALUE_A_OFFSET'"
  "UI_VALUE_B_OFFSET='$UI_VALUE_B_OFFSET'"
  "UI_VALUE_C_OFFSET='$UI_VALUE_C_OFFSET'"
  "DIALOG_TRIPLE_OFFSET='$DIALOG_TRIPLE_OFFSET'"
  "TARGET_VALUE='$TARGET_VALUE'"
  "TARGET_CONTAINERS='$TARGET_CONTAINERS'"
  "AUTO_REMAP_ENABLED='$AUTO_REMAP_ENABLED'"
  "DIRECT_ARRAYS='$DIRECT_ARRAYS'"
  "BACKEND_ONLY='$BACKEND_ONLY'"
  "MODE='$MODE'"
  "bash -s"
)
local_env=(
  "EXPECTED_BUILD_ID=$EXPECTED_BUILD_ID"
  "VALUE_A_OFFSET=$VALUE_A_OFFSET"
  "VALUE_B_OFFSET=$VALUE_B_OFFSET"
  "DEADLINE_CLAMP_OFFSET=$DEADLINE_CLAMP_OFFSET"
  "TIMER_DURATION_ZERO_OFFSET=$TIMER_DURATION_ZERO_OFFSET"
  "UI_VALUE_A_OFFSET=$UI_VALUE_A_OFFSET"
  "UI_VALUE_B_OFFSET=$UI_VALUE_B_OFFSET"
  "UI_VALUE_C_OFFSET=$UI_VALUE_C_OFFSET"
  "DIALOG_TRIPLE_OFFSET=$DIALOG_TRIPLE_OFFSET"
  "TARGET_VALUE=$TARGET_VALUE"
  "TARGET_CONTAINERS=$TARGET_CONTAINERS"
  "AUTO_REMAP_ENABLED=$AUTO_REMAP_ENABLED"
  "DIRECT_ARRAYS=$DIRECT_ARRAYS"
  "BACKEND_ONLY=$BACKEND_ONLY"
  "MODE=$MODE"
)

if [[ "$RUN_LOCAL" == "true" ]]; then
  runner=(env "${local_env[@]}" bash -s)
else
  runner=(ssh "$REMOTE_HOST" "${env_command[*]}")
fi

"${runner[@]}" <<'REMOTE'
set -euo pipefail

has_sys_ptrace_capability() {
  local cap_eff
  cap_eff="$(awk '$1 == "CapEff:" {print $2; exit}' /proc/self/status 2>/dev/null || true)"
  [[ "$cap_eff" =~ ^[[:xdigit:]]+$ ]] || return 1
  (( (16#$cap_eff & 524288) != 0 ))
}

sudo() {
  if [[ "${1:-}" == "-n" ]] && has_sys_ptrace_capability; then
    shift
    "$@"
  else
    command sudo "$@"
  fi
}

truthy() {
  [[ "${1:-}" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ ]]
}

offsets_complete() {
  if truthy "${BACKEND_ONLY:-false}"; then
    [[ -n "${VALUE_A_OFFSET:-}" &&
       -n "${VALUE_B_OFFSET:-}" &&
       -n "${DEADLINE_CLAMP_OFFSET:-}" &&
       -n "${TIMER_DURATION_ZERO_OFFSET:-}" ]]
    return
  fi
  [[ -n "${VALUE_A_OFFSET:-}" &&
     -n "${VALUE_B_OFFSET:-}" &&
     -n "${UI_VALUE_A_OFFSET:-}" &&
     -n "${UI_VALUE_B_OFFSET:-}" &&
     -n "${UI_VALUE_C_OFFSET:-}" ]] || return 1
  if truthy "${DIRECT_ARRAYS:-false}"; then
    return 0
  fi
  [[ -n "${DEADLINE_CLAMP_OFFSET:-}" &&
     -n "${TIMER_DURATION_ZERO_OFFSET:-}" ]]
}

auto_remap_offsets() {
  local exe_path="$1"

  sudo -n python3 - "$exe_path" <<'PY'
import re
import struct
import sys

path = sys.argv[1]
data = open(path, "rb").read()

if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
    raise SystemExit("not an ELF64 little-endian binary")

eh = struct.unpack_from("<16sHHIQQQIHHHHHH", data, 0)
e_shoff, e_shentsize, e_shnum, e_shstrndx = eh[6], eh[11], eh[12], eh[13]
raw_sections = []
for idx in range(e_shnum):
    raw_sections.append(struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + idx * e_shentsize))

shstr = raw_sections[e_shstrndx]
shstr_data = data[shstr[4]:shstr[4] + shstr[5]]
sections = {}
for section in raw_sections:
    name_offset = section[0]
    name_end = shstr_data.find(b"\0", name_offset)
    name = shstr_data[name_offset:name_end].decode("utf-8", "replace")
    sections[name] = {
        "type": section[1],
        "addr": section[3],
        "off": section[4],
        "size": section[5],
    }

def s32(chunk):
    return struct.unpack("<i", chunk)[0]

def va_to_file_offset(va):
    for section in sections.values():
        if section["type"] == 8:
            continue
        if section["addr"] <= va < section["addr"] + section["size"]:
            return section["off"] + (va - section["addr"])
    return None

def rip_target(insn_va, insn_len, disp):
    return insn_va + insn_len + disp

text = sections.get(".text")
if not text:
    raise SystemExit("missing .text section")

text_bytes = data[text["off"]:text["off"] + text["size"]]
text_va = text["addr"]

backend_prefix = bytes.fromhex(
    "55 48 89 e5 41 57 41 56 41 55 41 54 53 "
    "48 81 ec a8 02 00 00 49 89 d7 49 89 f5 48 89 fb e8"
)
deadline_context = bytes.fromhex("c4 e1 fb 2c c8 48 01 c1")
duration_reload = bytes.fromhex("c5 fa 10 45 d4")
lea_rip_encodings = (
    bytes.fromhex("4c 8d 3d"),  # lea disp32(%rip), %r15
    bytes.fromhex("48 8d 05"),  # lea disp32(%rip), %rax
)

backend_candidates = []
search_at = 0
while True:
    idx = text_bytes.find(backend_prefix, search_at)
    if idx < 0:
        break

    func_va = text_va + idx
    window = text_bytes[idx:idx + 0x800]
    deadline_rel = window.find(deadline_context)
    duration_rels = [m.start() for m in re.finditer(re.escape(duration_reload), window)]

    call_off = idx + len(backend_prefix) - 1
    call_rel = s32(text_bytes[call_off + 1:call_off + 5])
    accessor_va = text_va + call_off + 5 + call_rel
    accessor_off = va_to_file_offset(accessor_va)

    if deadline_rel >= 0 and duration_rels and accessor_off is not None:
        accessor = data[accessor_off:accessor_off + 0x90]
        leas = []
        for lea_rip in lea_rip_encodings:
            encoding_leas = []
            lea_at = 0
            while True:
                lea_rel = accessor.find(lea_rip, lea_at)
                if lea_rel < 0:
                    break
                lea_va = accessor_va + lea_rel
                target = rip_target(lea_va, 7, s32(accessor[lea_rel + 3:lea_rel + 7]))
                encoding_leas.append(target)
                lea_at = lea_rel + 1
            if len(encoding_leas) >= 2:
                leas = encoding_leas
                break

        if len(leas) >= 2:
            backend_candidates.append({
                "value_a": leas[1],
                "value_b": leas[0],
                "deadline": func_va + deadline_rel + 5,
                "duration": func_va + duration_rels[-1],
            })

    search_at = idx + 1

def accessor_leas(accessor_va):
    accessor_off = va_to_file_offset(accessor_va)
    if accessor_off is None:
        return []
    accessor = data[accessor_off:accessor_off + 0x90]
    for lea_rip in lea_rip_encodings:
        found = []
        lea_at = 0
        while True:
            lea_rel = accessor.find(lea_rip, lea_at)
            if lea_rel < 0:
                break
            found.append(rip_target(accessor_va + lea_rel, 7, s32(accessor[lea_rel + 3:lea_rel + 7])))
            lea_at = lea_rel + 1
        if len(found) >= 2:
            return found
    return []

if not backend_candidates:
    # The function prologue drifted. Anchor on the deadline clamp and the
    # duration reload instead, then walk back to the accessor call that
    # returns the two timer-array globals.
    anchored = {}
    at = 0
    while True:
        anchor = text_bytes.find(deadline_context, at)
        if anchor < 0:
            break
        at = anchor + 1
        ahead = text_bytes[anchor:anchor + 0x800]
        duration_rels = [m.start() for m in re.finditer(re.escape(duration_reload), ahead)]
        if not duration_rels:
            continue
        for call_off in range(anchor - 1, max(0, anchor - 0x100), -1):
            if text_bytes[call_off] != 0xE8:
                continue
            call_rel = s32(text_bytes[call_off + 1:call_off + 5])
            leas = accessor_leas(text_va + call_off + 5 + call_rel)
            # The two timer-array globals are adjacent; this rejects 0xE8 bytes
            # that are really operand data.
            if leas and 0 < abs(leas[0] - leas[1]) <= 0x100:
                anchored[anchor] = {
                    "value_a": leas[1],
                    "value_b": leas[0],
                    "deadline": text_va + anchor + 5,
                    "duration": text_va + anchor + duration_rels[-1],
                }
                break
    backend_candidates = list(anchored.values())
    if backend_candidates:
        print("note: backend timer found by anchor scan; function prologue drifted", file=sys.stderr)

if len(backend_candidates) != 1:
    raise SystemExit(f"expected one backend timer candidate, found {len(backend_candidates)}")

mov_rbx_rip = bytes.fromhex("48 8b 1d")
ui_load_context = bytes.fromhex("31 c0 c5 fa 10 04 83")
ui_candidates = []
search_at = 0
while True:
    idx = text_bytes.find(mov_rbx_rip, search_at)
    if idx < 0:
        break

    local = text_bytes[idx:idx + 0x100]
    if len(local) < 0x80 or local[7] != 0xE8 or local.find(ui_load_context, 0, 0x40) < 0:
        search_at = idx + 1
        continue

    movs = []
    mov_at = 0
    while True:
        mov_rel = local.find(mov_rbx_rip, mov_at)
        if mov_rel < 0 or mov_rel >= 0x80:
            break
        mov_va = text_va + idx + mov_rel
        target = rip_target(mov_va, 7, s32(local[mov_rel + 3:mov_rel + 7]))
        movs.append((mov_rel, target))
        mov_at = mov_rel + 1

    if len(movs) >= 3:
        ui_a = movs[0][1]
        ui_c = movs[1][1]
        ui_b = movs[2][1]
        if ui_a < ui_b < ui_c and ui_c - ui_a <= 0x200:
            ui_candidates.append({
                "ui_a": ui_a,
                "ui_b": ui_b,
                "ui_c": ui_c,
            })

    search_at = idx + 1

backend = backend_candidates[0]
if len(ui_candidates) == 1:
    ui = ui_candidates[0]
else:
    # Client-visible timer globals are optional; the server-side countdown is
    # enforced by the backend pair. Patch backend-only rather than leaving the
    # build entirely unpatched.
    print(f"note: expected one UI timer candidate, found {len(ui_candidates)}; remapping backend-only", file=sys.stderr)
    ui = None
print(f"VALUE_A_OFFSET=0x{backend['value_a']:x}")
print(f"VALUE_B_OFFSET=0x{backend['value_b']:x}")
print(f"DEADLINE_CLAMP_OFFSET=0x{backend['deadline']:x}")
print(f"TIMER_DURATION_ZERO_OFFSET=0x{backend['duration']:x}")
if ui:
    print(f"UI_VALUE_A_OFFSET=0x{ui['ui_a']:x}")
    print(f"UI_VALUE_B_OFFSET=0x{ui['ui_b']:x}")
    print(f"UI_VALUE_C_OFFSET=0x{ui['ui_c']:x}")
else:
    print("UI_VALUE_A_OFFSET=''")
    print("UI_VALUE_B_OFFSET=''")
    print("UI_VALUE_C_OFFSET=''")
    print("BACKEND_ONLY=true")
PY
}

resolve_offsets_for_build() {
  local container="$1"
  local exe_path="$2"
  local build_id="$3"
  local remap_output

  case "$build_id" in
    50d73d6403583bbcad919119c08d5083181c4d51)
      VALUE_A_OFFSET="0x1731ebf0"
      VALUE_B_OFFSET="0x1731ec08"
      DEADLINE_CLAMP_OFFSET="0x0da907d4"
      TIMER_DURATION_ZERO_OFFSET="0x0da90be1"
      UI_VALUE_A_OFFSET=""
      UI_VALUE_B_OFFSET=""
      UI_VALUE_C_OFFSET=""
      DIALOG_TRIPLE_OFFSET=""
      DIRECT_ARRAYS="false"
      BACKEND_ONLY="true"
      EXPECTED_BUILD_ID="$build_id"
      return
      ;;
    39e7aabe1c6201e43b81254e1c54631f8f16c8df)
      VALUE_A_OFFSET="0x173089f0"
      VALUE_B_OFFSET="0x17308a08"
      DEADLINE_CLAMP_OFFSET="0x0da84274"
      TIMER_DURATION_ZERO_OFFSET="0x0da84681"
      UI_VALUE_A_OFFSET=""
      UI_VALUE_B_OFFSET=""
      UI_VALUE_C_OFFSET=""
      DIALOG_TRIPLE_OFFSET=""
      DIRECT_ARRAYS="false"
      BACKEND_ONLY="true"
      EXPECTED_BUILD_ID="$build_id"
      return
      ;;
    cde875918e7dee23366c71e0d7bc20237810ea92)
      VALUE_A_OFFSET="0x165a7488"
      VALUE_B_OFFSET="0x165a74a0"
      DEADLINE_CLAMP_OFFSET="0xd551604"
      TIMER_DURATION_ZERO_OFFSET="0xd551a5a"
      UI_VALUE_A_OFFSET="0x165a91d8"
      UI_VALUE_B_OFFSET="0x165a9a78"
      UI_VALUE_C_OFFSET="0x165a9aa8"
      DIALOG_TRIPLE_OFFSET=""
      DIRECT_ARRAYS="false"
      EXPECTED_BUILD_ID="$build_id"
      return
      ;;
    1d03e11f7a9161d3755e3aec4cbda3eb924193ae)
      VALUE_A_OFFSET="0x172fed50"
      VALUE_B_OFFSET="0x172fed68"
      DEADLINE_CLAMP_OFFSET="0xda7dac4"
      TIMER_DURATION_ZERO_OFFSET="0xda7ded1"
      UI_VALUE_A_OFFSET="0x174eef00"
      UI_VALUE_B_OFFSET="0x174eef18"
      UI_VALUE_C_OFFSET="0x174eef30"
      DIALOG_TRIPLE_OFFSET=""
      DIRECT_ARRAYS="false"
      EXPECTED_BUILD_ID="$build_id"
      return
      ;;
    427a3084dcc00057ad21f98555a7d17d5f3c1020)
      VALUE_A_OFFSET="0x16649ab0"
      VALUE_B_OFFSET="0x16649ac8"
      DEADLINE_CLAMP_OFFSET=""
      TIMER_DURATION_ZERO_OFFSET=""
      UI_VALUE_A_OFFSET="0x16649ab0"
      UI_VALUE_B_OFFSET="0x16649ac8"
      UI_VALUE_C_OFFSET="0x16649ac8"
      DIALOG_TRIPLE_OFFSET="${DUNE_LOGOFF_TIMER_DIALOG_TRIPLE_OFFSET:-0x16905c58}"
      DIRECT_ARRAYS="${DUNE_LOGOFF_TIMER_DIRECT_ARRAYS:-true}"
      EXPECTED_BUILD_ID="$build_id"
      return
      ;;
  esac

  if [[ "$build_id" == "$EXPECTED_BUILD_ID" ]] && offsets_complete; then
    return
  fi

  if ! truthy "$AUTO_REMAP_ENABLED"; then
    if [[ "$build_id" != "$EXPECTED_BUILD_ID" ]]; then
      echo "$container: refusing build $build_id; expected $EXPECTED_BUILD_ID" >&2
    else
      echo "$container: missing logoff timer offsets and auto-remap is disabled" >&2
    fi
    exit 1
  fi

  if ! remap_output="$(auto_remap_offsets "$exe_path")"; then
    echo "$container: auto-remap failed for build $build_id" >&2
    exit 1
  fi

  eval "$remap_output"
  EXPECTED_BUILD_ID="$build_id"
  echo "$container: auto-remapped logoff timer offsets for build $build_id: value_a=$VALUE_A_OFFSET value_b=$VALUE_B_OFFSET clamp=$DEADLINE_CLAMP_OFFSET duration_zero=$TIMER_DURATION_ZERO_OFFSET ui=$UI_VALUE_A_OFFSET,$UI_VALUE_B_OFFSET,$UI_VALUE_C_OFFSET"
}

validate_patch_targets() {
  local pid="$1"
  local addr_a="$2"
  local addr_b="$3"
  local addr_ui_a="$4"
  local addr_ui_b="$5"
  local addr_ui_c="$6"
  local addr_clamp="$7"
  local addr_duration_zero="$8"
  local validation_output
  local ptr_line
  local float_line
  local byte_line
  local _
  local p1 p2 ui1 ui2 ui3
  local p10 p11 p12 p13 p20 p21 p22 p23
  local u10 u11 u12 u13 u20 u21 u22 u23 u30 u31 u32 u33
  local c0 c1 c2 d0 d1 d2 d3 d4

  if truthy "${BACKEND_ONLY:-false}"; then
    validation_output="$(
      sudo -n gdb -q -batch -p "$pid" \
        -ex "set confirm off" \
        -ex "set pagination off" \
        -ex "set \$p1 = *(void**)$addr_a" \
        -ex "set \$p2 = *(void**)$addr_b" \
        -ex "printf \"LOGOFF_BACKEND_PTR %p %p\\n\", \$p1, \$p2" \
        -ex "printf \"LOGOFF_BACKEND_FLOATS %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g\\n\", *(float*)\$p1, *(float*)((char*)\$p1 + 4), *(float*)((char*)\$p1 + 8), *(float*)((char*)\$p1 + 12), *(float*)\$p2, *(float*)((char*)\$p2 + 4), *(float*)((char*)\$p2 + 8), *(float*)((char*)\$p2 + 12)" \
        -ex "set \$clamp = (unsigned char*)$addr_clamp" \
        -ex "set \$duration = (unsigned char*)$addr_duration_zero" \
        -ex "printf \"LOGOFF_BYTES %02x %02x %02x %02x %02x %02x %02x %02x\\n\", \$clamp[0], \$clamp[1], \$clamp[2], \$duration[0], \$duration[1], \$duration[2], \$duration[3], \$duration[4]"
    )"
    ptr_line="$(awk '/^LOGOFF_BACKEND_PTR / {line=$0} END {print line}' <<< "$validation_output")"
    float_line="$(awk '/^LOGOFF_BACKEND_FLOATS / {line=$0} END {print line}' <<< "$validation_output")"
    byte_line="$(awk '/^LOGOFF_BYTES / {line=$0} END {print line}' <<< "$validation_output")"
    if [[ -z "$ptr_line" || -z "$float_line" || -z "$byte_line" ]]; then
      printf 'invalid backend-only logoff timer validation output\n%s\n' "$validation_output" >&2
      return 1
    fi
    read -r _ p1 p2 <<< "$ptr_line"
    if [[ "$p1" == "(nil)" || "$p1" == "0x0" || "$p2" == "(nil)" || "$p2" == "0x0" ]]; then
      printf 'invalid backend logoff timer pointers: %s %s\n' "$p1" "$p2" >&2
      return 1
    fi
    read -r _ p10 p11 p12 p13 p20 p21 p22 p23 <<< "$float_line"
    read -r _ c0 c1 c2 d0 d1 d2 d3 d4 <<< "$byte_line"
    float_close() {
      awk -v a="$1" -v b="$2" 'BEGIN { d = a - b; if (d < 0) d = -d; exit(d <= 0.001 ? 0 : 1) }'
    }
    float_allowed() {
      local value="$1"
      shift
      local allowed
      for allowed in "$@"; do
        if float_close "$value" "$allowed"; then return 0; fi
      done
      return 1
    }
    for value in "$p10" "$p11"; do
      float_allowed "$value" "$TARGET_VALUE" 0 30 || { printf 'invalid backend 30-second timer value: %s\n' "$value" >&2; return 1; }
    done
    for value in "$p12" "$p13" "$p22" "$p23"; do
      float_allowed "$value" 0 || { printf 'invalid backend timer tail: %s\n' "$value" >&2; return 1; }
    done
    for value in "$p20" "$p21"; do
      float_allowed "$value" "$TARGET_VALUE" 0 300 || { printf 'invalid backend 300-second timer value: %s\n' "$value" >&2; return 1; }
    done
    if [[ "$c0" != "48" || "$c2" != "c1" || ( "$c1" != "01" && "$c1" != "89" ) ]]; then
      printf 'invalid deadline clamp bytes: %s %s %s\n' "$c0" "$c1" "$c2" >&2
      return 1
    fi
    if [[ ! ( "$d0" == "c5" && "$d1" == "fa" && "$d2" == "10" && "$d3" == "45" && "$d4" == "d4" ) &&
          ! ( "$d0" == "c5" && "$d1" == "f8" && "$d2" == "57" && "$d3" == "c0" && "$d4" == "90" ) ]]; then
      printf 'invalid timer duration bytes: %s %s %s %s %s\n' "$d0" "$d1" "$d2" "$d3" "$d4" >&2
      return 1
    fi
    return 0
  fi

  if truthy "${DIRECT_ARRAYS:-false}"; then
    validation_output="$(
      sudo -n gdb -q -batch -p "$pid" \
        -ex "set confirm off" \
        -ex "set pagination off" \
        -ex "set \$p1 = (void*)$addr_a" \
        -ex "set \$p2 = (void*)$addr_b" \
        -ex "set \$ui1 = (void*)$addr_ui_a" \
        -ex "set \$ui2 = (void*)$addr_ui_b" \
        -ex "set \$ui3 = (void*)$addr_ui_c" \
        -ex "printf \"LOGOFF_PTR %p %p %p %p %p\\n\", \$p1, \$p2, \$ui1, \$ui2, \$ui3" \
        -ex "printf \"LOGOFF_FLOATS %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g\\n\", *(float*)\$p1, *(float*)((char*)\$p1 + 4), *(float*)((char*)\$p1 + 8), *(float*)((char*)\$p1 + 12), *(float*)\$p2, *(float*)((char*)\$p2 + 4), *(float*)((char*)\$p2 + 8), *(float*)((char*)\$p2 + 12), *(float*)\$ui1, *(float*)((char*)\$ui1 + 4), *(float*)((char*)\$ui1 + 8), *(float*)((char*)\$ui1 + 12), *(float*)\$ui2, *(float*)((char*)\$ui2 + 4), *(float*)((char*)\$ui2 + 8), *(float*)((char*)\$ui2 + 12), *(float*)\$ui3, *(float*)((char*)\$ui3 + 4), *(float*)((char*)\$ui3 + 8), *(float*)((char*)\$ui3 + 12)" \
        -ex "printf \"LOGOFF_BYTES skipped skipped skipped skipped skipped skipped skipped skipped\\n\""
    )"
  else
    validation_output="$(
      sudo -n gdb -q -batch -p "$pid" \
        -ex "set confirm off" \
        -ex "set pagination off" \
        -ex "set \$p1 = *(void**)$addr_a" \
        -ex "set \$p2 = *(void**)$addr_b" \
        -ex "set \$ui1 = *(void**)$addr_ui_a" \
        -ex "set \$ui2 = *(void**)$addr_ui_b" \
        -ex "set \$ui3 = *(void**)$addr_ui_c" \
        -ex "printf \"LOGOFF_PTR %p %p %p %p %p\\n\", \$p1, \$p2, \$ui1, \$ui2, \$ui3" \
        -ex "printf \"LOGOFF_FLOATS %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g\\n\", *(float*)\$p1, *(float*)((char*)\$p1 + 4), *(float*)((char*)\$p1 + 8), *(float*)((char*)\$p1 + 12), *(float*)\$p2, *(float*)((char*)\$p2 + 4), *(float*)((char*)\$p2 + 8), *(float*)((char*)\$p2 + 12), *(float*)\$ui1, *(float*)((char*)\$ui1 + 4), *(float*)((char*)\$ui1 + 8), *(float*)((char*)\$ui1 + 12), *(float*)\$ui2, *(float*)((char*)\$ui2 + 4), *(float*)((char*)\$ui2 + 8), *(float*)((char*)\$ui2 + 12), *(float*)\$ui3, *(float*)((char*)\$ui3 + 4), *(float*)((char*)\$ui3 + 8), *(float*)((char*)\$ui3 + 12)" \
        -ex "set \$clamp = (unsigned char*)$addr_clamp" \
        -ex "set \$duration = (unsigned char*)$addr_duration_zero" \
        -ex "printf \"LOGOFF_BYTES %02x %02x %02x %02x %02x %02x %02x %02x\\n\", \$clamp[0], \$clamp[1], \$clamp[2], \$duration[0], \$duration[1], \$duration[2], \$duration[3], \$duration[4]"
    )"
  fi

  ptr_line="$(awk '/^LOGOFF_PTR / {line=$0} END {print line}' <<< "$validation_output")"
  float_line="$(awk '/^LOGOFF_FLOATS / {line=$0} END {print line}' <<< "$validation_output")"
  byte_line="$(awk '/^LOGOFF_BYTES / {line=$0} END {print line}' <<< "$validation_output")"
  if [[ -z "$ptr_line" || -z "$float_line" || -z "$byte_line" ]]; then
    printf 'invalid logoff timer validation output\n%s\n' "$validation_output" >&2
    return 1
  fi

  read -r _ p1 p2 ui1 ui2 ui3 <<< "$ptr_line"
  for ptr in "$p1" "$p2" "$ui1" "$ui2" "$ui3"; do
    if [[ "$ptr" == "(nil)" || "$ptr" == "0x0" ]]; then
      printf 'invalid logoff timer pointer: %s\n' "$ptr" >&2
      return 1
    fi
  done

  read -r _ p10 p11 p12 p13 p20 p21 p22 p23 u10 u11 u12 u13 u20 u21 u22 u23 u30 u31 u32 u33 <<< "$float_line"
  read -r _ c0 c1 c2 d0 d1 d2 d3 d4 <<< "$byte_line"

  float_close() {
    awk -v a="$1" -v b="$2" 'BEGIN { d = a - b; if (d < 0) d = -d; exit(d <= 0.001 ? 0 : 1) }'
  }

  float_allowed() {
    local value="$1"
    shift
    local allowed
    for allowed in "$@"; do
      if float_close "$value" "$allowed"; then
        return 0
      fi
    done
    return 1
  }

  for value in "$p10" "$p11"; do
    float_allowed "$value" "$TARGET_VALUE" 0 30 || { printf 'invalid backend 30-second timer value: %s\n' "$value" >&2; return 1; }
  done
  for value in "$p12" "$p13"; do
    float_allowed "$value" 0 || { printf 'invalid backend 30-second timer tail: %s\n' "$value" >&2; return 1; }
  done
  for value in "$p20" "$p21"; do
    float_allowed "$value" "$TARGET_VALUE" 0 300 || { printf 'invalid backend 300-second timer value: %s\n' "$value" >&2; return 1; }
  done
  for value in "$p22" "$p23"; do
    float_allowed "$value" 0 || { printf 'invalid backend 300-second timer tail: %s\n' "$value" >&2; return 1; }
  done
  for value in "$u10" "$u11" "$u20" "$u21" "$u30" "$u31"; do
    float_allowed "$value" "$TARGET_VALUE" 0 300 || { printf 'invalid UI timer value: %s\n' "$value" >&2; return 1; }
  done
  for value in "$u12" "$u13" "$u22" "$u23" "$u32" "$u33"; do
    float_allowed "$value" 0 || { printf 'invalid UI timer tail: %s\n' "$value" >&2; return 1; }
  done

  if truthy "${DIRECT_ARRAYS:-false}"; then
    return 0
  fi

  if [[ "$c0" != "48" || "$c2" != "c1" || ( "$c1" != "01" && "$c1" != "89" ) ]]; then
    printf 'invalid deadline clamp bytes: %s %s %s\n' "$c0" "$c1" "$c2" >&2
    return 1
  fi

  if [[ ! ( "$d0" == "c5" && "$d1" == "fa" && "$d2" == "10" && "$d3" == "45" && "$d4" == "d4" ) &&
        ! ( "$d0" == "c5" && "$d1" == "f8" && "$d2" == "57" && "$d3" == "c0" && "$d4" == "90" ) ]]; then
    printf 'invalid timer duration bytes: %s %s %s %s %s\n' "$d0" "$d1" "$d2" "$d3" "$d4" >&2
    return 1
  fi
}

patch_dialog_triple() {
  local pid="$1"
  local addr_dialog="$2"
  local validation_output
  local line
  local _
  local v0 v1 v2 v3 v4 v5

  validation_output="$(
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set confirm off" \
      -ex "set pagination off" \
      -ex "printf \"LOGOFF_DIALOG %.9g %.9g %.9g %.9g %.9g %.9g\\n\", *(float*)$addr_dialog, *(float*)($addr_dialog + 4), *(float*)($addr_dialog + 8), *(float*)($addr_dialog + 12), *(float*)($addr_dialog + 16), *(float*)($addr_dialog + 20)"
  )"
  line="$(awk '/^LOGOFF_DIALOG / {line=$0} END {print line}' <<< "$validation_output")"
  if [[ -z "$line" ]]; then
    printf 'invalid logoff dialog validation output\n%s\n' "$validation_output" >&2
    return 1
  fi

  read -r _ v0 v1 v2 v3 v4 v5 <<< "$line"
  float_close() {
    awk -v a="$1" -v b="$2" 'BEGIN { d = a - b; if (d < 0) d = -d; exit(d <= 0.001 ? 0 : 1) }'
  }

  if ! { float_close "$v0" 30 || float_close "$v0" "$TARGET_VALUE"; } ||
     ! { float_close "$v1" 30 || float_close "$v1" "$TARGET_VALUE"; } ||
     ! { float_close "$v2" 30 || float_close "$v2" "$TARGET_VALUE"; } ||
     ! float_close "$v3" 0 ||
     ! float_close "$v4" 0 ||
     ! float_close "$v5" 0; then
    printf 'invalid logoff dialog timer signature at %s: %s %s %s %s %s %s\n' "$addr_dialog" "$v0" "$v1" "$v2" "$v3" "$v4" "$v5" >&2
    return 1
  fi

  if [[ "$MODE" == "dry-run" ]]; then
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "printf \"dialog triple before addr=%p\\n\", (void*)$addr_dialog" \
      -ex "x/6fw $addr_dialog"
  else
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "printf \"dialog triple before addr=%p\\n\", (void*)$addr_dialog" \
      -ex "x/6fw $addr_dialog" \
      -ex "set {float}$addr_dialog = $TARGET_VALUE" \
      -ex "set {float}($addr_dialog + 4) = $TARGET_VALUE" \
      -ex "set {float}($addr_dialog + 8) = $TARGET_VALUE" \
      -ex "printf \"dialog triple after addr=%p\\n\", (void*)$addr_dialog" \
      -ex "x/6fw $addr_dialog"
  fi
}

if [[ -n "$TARGET_CONTAINERS" ]]; then
  mapfile -t containers < <(
    docker ps --format '{{.Names}}' |
      awk -v targets="$TARGET_CONTAINERS" '
        BEGIN {
          split(targets, names, /[[:space:]]+/)
          for (idx in names) {
            if (names[idx] != "") {
              wanted[names[idx]] = 1
            }
          }
        }
        wanted[$0]
      '
  )
else
  mapfile -t containers < <(
    docker ps --format '{{.Names}}' |
      while IFS= read -r container; do
        if docker top "$container" 2>/dev/null | grep -q 'DuneSandboxServer-Linux-Shipping'; then
          printf '%s\n' "$container"
        fi
      done
  )
fi

if [[ "${#containers[@]}" -eq 0 ]]; then
  echo "no active DuneSandboxServer containers found" >&2
  exit 1
fi

processed_containers=0
for container in "${containers[@]}"; do
  pid="$(docker top "$container" -eo pid,args | awk '/DuneSandboxServer-Linux-Shipping/ {print $1; exit}')"
  if [[ -z "$pid" ]]; then
    echo "$container: no DuneSandboxServer-Linux-Shipping process found" >&2
    continue
  fi

  exe_path="$(sudo -n readlink "/proc/$pid/exe")"
  build_id="$(sudo -n readelf -n "/proc/$pid/exe" | awk '/Build ID:/ {print $3; exit}')"
  resolve_offsets_for_build "$container" "/proc/$pid/exe" "$build_id"

  base="$(sudo -n awk -v exe="$exe_path" '
    $2 ~ /r.xp/ && $3 == "00000000" {
      path = $6
      for (i = 7; i <= NF; i++) {
        path = path " " $i
      }
      if (path == exe || path == exe " (deleted)" || path ~ /DuneSandboxServer-Linux-Shipping/) {
        split($1,a,"-")
        print "0x" a[1]
        exit
      }
    }
  ' "/proc/$pid/maps")"
  if [[ -z "$base" ]]; then
    echo "$container: could not find PIE base" >&2
    exit 1
  fi

  addr_a="$(printf '0x%x' $((base + VALUE_A_OFFSET)))"
  addr_b="$(printf '0x%x' $((base + VALUE_B_OFFSET)))"
  addr_clamp="0x0"
  addr_duration_zero="0x0"
  if [[ -n "${DEADLINE_CLAMP_OFFSET:-}" ]]; then
    addr_clamp="$(printf '0x%x' $((base + DEADLINE_CLAMP_OFFSET)))"
  fi
  if [[ -n "${TIMER_DURATION_ZERO_OFFSET:-}" ]]; then
    addr_duration_zero="$(printf '0x%x' $((base + TIMER_DURATION_ZERO_OFFSET)))"
  fi
  addr_ui_a=""
  addr_ui_b=""
  addr_ui_c=""
  if [[ -n "${UI_VALUE_A_OFFSET:-}" ]]; then addr_ui_a="$(printf '0x%x' $((base + UI_VALUE_A_OFFSET)))"; fi
  if [[ -n "${UI_VALUE_B_OFFSET:-}" ]]; then addr_ui_b="$(printf '0x%x' $((base + UI_VALUE_B_OFFSET)))"; fi
  if [[ -n "${UI_VALUE_C_OFFSET:-}" ]]; then addr_ui_c="$(printf '0x%x' $((base + UI_VALUE_C_OFFSET)))"; fi
  addr_dialog=""
  if [[ -n "${DIALOG_TRIPLE_OFFSET:-}" ]]; then
    addr_dialog="$(printf '0x%x' $((base + DIALOG_TRIPLE_OFFSET)))"
  fi
  if truthy "${BACKEND_ONLY:-false}"; then
    echo "$container: pid=$pid base=$base backend_pointers=$addr_a,$addr_b ui=not-targeted clamp=$addr_clamp duration_zero=$addr_duration_zero mode=$MODE"
  else
    echo "$container: pid=$pid base=$base pointers=$addr_a,$addr_b ui_pointers=$addr_ui_a,$addr_ui_b,$addr_ui_c dialog_triple=${addr_dialog:-none} clamp=$addr_clamp duration_zero=$addr_duration_zero mode=$MODE"
  fi

  validate_patch_targets "$pid" "$addr_a" "$addr_b" "$addr_ui_a" "$addr_ui_b" "$addr_ui_c" "$addr_clamp" "$addr_duration_zero"

  if truthy "${BACKEND_ONLY:-false}" && [[ "$MODE" == "dry-run" ]]; then
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "set \$p1 = *(void**)$addr_a" \
      -ex "set \$p2 = *(void**)$addr_b" \
      -ex "printf \"backend arrays before p1=%p p2=%p\\n\", \$p1, \$p2" \
      -ex "x/4fw \$p1" \
      -ex "x/4fw \$p2" \
      -ex "printf \"deadline clamp bytes: \"" \
      -ex "x/3xb $addr_clamp" \
      -ex "printf \"timer duration bytes: \"" \
      -ex "x/5xb $addr_duration_zero"
  elif truthy "${BACKEND_ONLY:-false}"; then
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "set \$p1 = *(void**)$addr_a" \
      -ex "set \$p2 = *(void**)$addr_b" \
      -ex "printf \"backend arrays before p1=%p p2=%p\\n\", \$p1, \$p2" \
      -ex "x/4fw \$p1" \
      -ex "x/4fw \$p2" \
      -ex "set {float}\$p1 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$p1 + 4) = $TARGET_VALUE" \
      -ex "set {float}\$p2 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$p2 + 4) = $TARGET_VALUE" \
      -ex "set \$clamp = (unsigned char*)$addr_clamp" \
      -ex "printf \"deadline clamp before: %02x %02x %02x\\n\", \$clamp[0], \$clamp[1], \$clamp[2]" \
      -ex "set \$clamp[1] = 0x89" \
      -ex "set \$duration = (unsigned char*)$addr_duration_zero" \
      -ex "printf \"timer duration before: %02x %02x %02x %02x %02x\\n\", \$duration[0], \$duration[1], \$duration[2], \$duration[3], \$duration[4]" \
      -ex "set \$duration[0] = 0xc5" \
      -ex "set \$duration[1] = 0xf8" \
      -ex "set \$duration[2] = 0x57" \
      -ex "set \$duration[3] = 0xc0" \
      -ex "set \$duration[4] = 0x90" \
      -ex "printf \"backend arrays after p1=%p p2=%p\\n\", \$p1, \$p2" \
      -ex "x/4fw \$p1" \
      -ex "x/4fw \$p2" \
      -ex "printf \"deadline clamp bytes: \"" \
      -ex "x/3xb $addr_clamp" \
      -ex "printf \"timer duration bytes: \"" \
      -ex "x/5xb $addr_duration_zero"
  elif [[ "$MODE" == "dry-run" && "${DIRECT_ARRAYS:-false}" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ ]]; then
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "printf \"direct arrays before p1=%p p2=%p\\n\", (void*)$addr_a, (void*)$addr_b" \
      -ex "x/4fw $addr_a" \
      -ex "x/4fw $addr_b" \
      -ex "printf \"direct ui before ui1=%p ui2=%p ui3=%p\\n\", (void*)$addr_ui_a, (void*)$addr_ui_b, (void*)$addr_ui_c" \
      -ex "x/4fw $addr_ui_a" \
      -ex "x/4fw $addr_ui_b" \
      -ex "x/4fw $addr_ui_c"
  elif [[ "${DIRECT_ARRAYS:-false}" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Yy])$ ]]; then
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "printf \"direct arrays before p1=%p p2=%p\\n\", (void*)$addr_a, (void*)$addr_b" \
      -ex "x/4fw $addr_a" \
      -ex "x/4fw $addr_b" \
      -ex "printf \"direct ui before ui1=%p ui2=%p ui3=%p\\n\", (void*)$addr_ui_a, (void*)$addr_ui_b, (void*)$addr_ui_c" \
      -ex "x/4fw $addr_ui_a" \
      -ex "x/4fw $addr_ui_b" \
      -ex "x/4fw $addr_ui_c" \
      -ex "set {float}$addr_a = $TARGET_VALUE" \
      -ex "set {float}($addr_a + 4) = $TARGET_VALUE" \
      -ex "set {float}$addr_b = $TARGET_VALUE" \
      -ex "set {float}($addr_b + 4) = $TARGET_VALUE" \
      -ex "set {float}$addr_ui_a = $TARGET_VALUE" \
      -ex "set {float}($addr_ui_a + 4) = $TARGET_VALUE" \
      -ex "set {float}$addr_ui_b = $TARGET_VALUE" \
      -ex "set {float}($addr_ui_b + 4) = $TARGET_VALUE" \
      -ex "set {float}$addr_ui_c = $TARGET_VALUE" \
      -ex "set {float}($addr_ui_c + 4) = $TARGET_VALUE" \
      -ex "printf \"direct arrays after p1=%p p2=%p\\n\", (void*)$addr_a, (void*)$addr_b" \
      -ex "x/4fw $addr_a" \
      -ex "x/4fw $addr_b" \
      -ex "printf \"direct ui after ui1=%p ui2=%p ui3=%p\\n\", (void*)$addr_ui_a, (void*)$addr_ui_b, (void*)$addr_ui_c" \
      -ex "x/4fw $addr_ui_a" \
      -ex "x/4fw $addr_ui_b" \
      -ex "x/4fw $addr_ui_c"
  elif [[ "$MODE" == "dry-run" ]]; then
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "set \$p1 = *(void**)$addr_a" \
      -ex "set \$p2 = *(void**)$addr_b" \
      -ex "set \$ui1 = *(void**)$addr_ui_a" \
      -ex "set \$ui2 = *(void**)$addr_ui_b" \
      -ex "set \$ui3 = *(void**)$addr_ui_c" \
      -ex "printf \"before p1=%p p2=%p\\n\", \$p1, \$p2" \
      -ex "x/4fw \$p1" \
      -ex "x/4fw \$p2" \
      -ex "printf \"ui before ui1=%p ui2=%p ui3=%p\\n\", \$ui1, \$ui2, \$ui3" \
      -ex "x/4fw \$ui1" \
      -ex "x/4fw \$ui2" \
      -ex "x/4fw \$ui3" \
      -ex "printf \"deadline clamp bytes: \"" \
      -ex "x/3xb $addr_clamp" \
      -ex "printf \"timer duration bytes: \"" \
      -ex "x/5xb $addr_duration_zero"
  else
    sudo -n gdb -q -batch -p "$pid" \
      -ex "set pagination off" \
      -ex "set \$p1 = *(void**)$addr_a" \
      -ex "set \$p2 = *(void**)$addr_b" \
      -ex "set \$ui1 = *(void**)$addr_ui_a" \
      -ex "set \$ui2 = *(void**)$addr_ui_b" \
      -ex "set \$ui3 = *(void**)$addr_ui_c" \
      -ex "printf \"before p1=%p p2=%p\\n\", \$p1, \$p2" \
      -ex "x/4fw \$p1" \
      -ex "x/4fw \$p2" \
      -ex "printf \"ui before ui1=%p ui2=%p ui3=%p\\n\", \$ui1, \$ui2, \$ui3" \
      -ex "x/4fw \$ui1" \
      -ex "x/4fw \$ui2" \
      -ex "x/4fw \$ui3" \
      -ex "set {float}\$p1 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$p1 + 4) = $TARGET_VALUE" \
      -ex "set {float}\$p2 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$p2 + 4) = $TARGET_VALUE" \
      -ex "set {float}\$ui1 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$ui1 + 4) = $TARGET_VALUE" \
      -ex "set {float}\$ui2 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$ui2 + 4) = $TARGET_VALUE" \
      -ex "set {float}\$ui3 = $TARGET_VALUE" \
      -ex "set {float}((char*)\$ui3 + 4) = $TARGET_VALUE" \
      -ex "set \$clamp = (unsigned char*)$addr_clamp" \
      -ex "printf \"deadline clamp before: %02x %02x %02x\\n\", \$clamp[0], \$clamp[1], \$clamp[2]" \
      -ex "set \$clamp[1] = 0x89" \
      -ex "set \$duration = (unsigned char*)$addr_duration_zero" \
      -ex "printf \"timer duration before: %02x %02x %02x %02x %02x\\n\", \$duration[0], \$duration[1], \$duration[2], \$duration[3], \$duration[4]" \
      -ex "set \$duration[0] = 0xc5" \
      -ex "set \$duration[1] = 0xf8" \
      -ex "set \$duration[2] = 0x57" \
      -ex "set \$duration[3] = 0xc0" \
      -ex "set \$duration[4] = 0x90" \
      -ex "printf \"after p1=%p p2=%p\\n\", \$p1, \$p2" \
      -ex "x/4fw \$p1" \
      -ex "x/4fw \$p2" \
      -ex "printf \"ui after ui1=%p ui2=%p ui3=%p\\n\", \$ui1, \$ui2, \$ui3" \
      -ex "x/4fw \$ui1" \
      -ex "x/4fw \$ui2" \
      -ex "x/4fw \$ui3" \
      -ex "printf \"deadline clamp bytes: \"" \
      -ex "x/3xb $addr_clamp" \
      -ex "printf \"timer duration bytes: \"" \
      -ex "x/5xb $addr_duration_zero"
  fi

  if [[ -n "$addr_dialog" ]]; then
    patch_dialog_triple "$pid" "$addr_dialog"
  fi
  processed_containers=$((processed_containers + 1))
done

if [[ "$processed_containers" -ne "${#containers[@]}" ]]; then
  echo "patched/verified $processed_containers of ${#containers[@]} selected running containers" >&2
  exit 1
fi
REMOTE
