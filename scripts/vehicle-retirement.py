#!/usr/bin/env python3
"""Preview or execute guarded native multi-vehicle recovery retirement through DASH."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from secret_env import read_secret_env


def read_env(path):
    values = {}
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request_json(method, url, token="", body=None, timeout=240):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"admin API failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"admin API unavailable: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=pathlib.Path, default=ROOT / ".env")
    parser.add_argument("--base-url", default="", help="Admin panel base URL. Default: http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}")
    parser.add_argument("--token", default="", help="Admin token. Default: DUNE_ADMIN_TOKEN from env/.env")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON")
    sub = parser.add_subparsers(dest="action", required=True)

    scan = sub.add_parser("list", help="List parked vehicles and prior receipts")
    scan.add_argument("--account-id", type=int)
    scan.add_argument("--vehicle-id", type=int)
    scan.add_argument("--limit", type=int, default=100)

    preview = sub.add_parser("preview", help="Create a fingerprint-bound archive preview")
    preview.add_argument("--vehicle-id", dest="vehicle_ids", action="append", type=int, required=True)
    preview.add_argument("--account-id", type=int)
    preview.add_argument("--allow-inventory-wipe", action="store_true", help="Request native recovery's ordinary vehicle-cargo deletion (requires the exact wipe confirmation)")
    preview.add_argument("--any-vehicle", action="store_true", help="Do not require Ornithopter class")

    archive = sub.add_parser("archive", help="Execute an exact native vehicle-recovery preview")
    archive.add_argument("--vehicle-id", dest="vehicle_ids", action="append", type=int, required=True)
    archive.add_argument("--account-id", type=int, required=True)
    archive.add_argument("--expected-fingerprint", required=True)
    archive.add_argument("--confirm", required=True)
    archive.add_argument("--allow-inventory-wipe", action="store_true", help="Request native recovery's ordinary vehicle-cargo deletion")
    archive.add_argument("--any-vehicle", action="store_true")

    args = parser.parse_args(argv)
    env_file = read_env(args.env_file)
    base = (args.base_url or f"http://127.0.0.1:{os.environ.get('DUNE_ADMIN_HOST_PORT') or env_file.get('DUNE_ADMIN_HOST_PORT', '18080')}").rstrip("/")
    token = args.token or read_secret_env("DUNE_ADMIN_TOKEN", env_file.get("DUNE_ADMIN_TOKEN", ""))

    if args.action == "list":
        query = []
        if args.account_id is not None:
            query.append(f"account_id={args.account_id}")
        if args.vehicle_id is not None:
            query.append(f"vehicle_id={args.vehicle_id}")
        query.append(f"limit={args.limit}")
        suffix = "?" + "&".join(query)
        result = request_json("GET", f"{base}/api/admin/vehicle-retirement{suffix}", token=token, timeout=60)
    else:
        body = {
            "action": args.action,
            "vehicleIds": args.vehicle_ids,
            "accountId": args.account_id,
            "allowInventoryWipe": bool(args.allow_inventory_wipe),
            "requireOrnithopters": not bool(args.any_vehicle),
        }
        if args.action == "archive":
            body.update({"expectedFingerprint": args.expected_fingerprint, "confirm": args.confirm})
        result = request_json("POST", f"{base}/api/admin/vehicle-retirement", token=token, body=body)
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
