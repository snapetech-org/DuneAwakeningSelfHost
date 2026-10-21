#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/test-logoff-timer-remap.sh BINARY [BINARY...]

Offline check of the logoff timer auto-remap against DuneSandboxServer-Linux-Shipping
binaries (for example extracted from a staged image with `docker cp`). Run it on a new
package before restarting the farm. For each binary it:
  1. runs the remap as shipped, and
  2. runs it with the backend prologue anchor broken, which must still resolve to the
     same offsets via the anchor scan.
Exit 0 only when every binary resolves a backend timer and both runs agree.
USAGE
}

if [[ $# -lt 1 ]]; then usage; exit 2; fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
patcher="$script_dir/patch-logoff-timers-runtime.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

sed -n '/^import re$/,/^PY$/p' "$patcher" | sed '$d' > "$work/remap.py"
[[ -s "$work/remap.py" ]] || { echo "fail: could not extract remap block from $patcher" >&2; exit 1; }
# Break the prologue anchor so only the anchor scan can match.
sed 's/"55 48 89 e5 41 57 41 56 41 55 41 54 53 "/"de ad be ef 41 57 41 56 41 55 41 54 53 "/' "$work/remap.py" > "$work/remap-drift.py"
cmp -s "$work/remap.py" "$work/remap-drift.py" && { echo "fail: drift variant did not change the script" >&2; exit 1; }

rc=0
for bin in "$@"; do
  echo "== $bin"
  if ! shipped="$(python3 -I "$work/remap.py" "$bin")"; then
    echo "FAIL: shipped remap found no backend timer" >&2; rc=1; continue
  fi
  if ! drift="$(python3 -I "$work/remap-drift.py" "$bin" 2>/dev/null)"; then
    echo "FAIL: anchor-scan fallback found no backend timer" >&2; rc=1; continue
  fi
  echo "$shipped"
  if [[ "$(grep -E '^(VALUE|DEADLINE|TIMER)' <<<"$shipped")" == "$(grep -E '^(VALUE|DEADLINE|TIMER)' <<<"$drift")" ]]; then
    echo "ok: anchor scan agrees with shipped remap"
  else
    echo "FAIL: anchor scan disagrees with shipped remap" >&2; echo "$drift" >&2; rc=1
  fi
done
exit "$rc"
