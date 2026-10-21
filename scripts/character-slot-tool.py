#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
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


def env(name, default="", env_file=None):
    if os.environ.get(name):
        return os.environ[name]
    if env_file is not None:
        return env_file.get(name, default)
    return default


def request_json(method, url, token="", body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"admin API failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"admin API unavailable: {exc}") from exc


def summarize(result):
    if not isinstance(result, dict):
        return {"ok": False, "summary": "unexpected non-object API response"}
    if "plan" in result:
        plan = result.get("plan") or {}
        return {
            "ok": bool(result.get("ok")),
            "dryRun": result.get("dryRun"),
            "accountId": result.get("accountId"),
            "action": result.get("action"),
            "targetAccountId": result.get("targetAccountId"),
            "executable": result.get("executable"),
            "blockers": plan.get("blockers") or [],
            "nativeCall": plan.get("nativeCall"),
            "transactionSafety": plan.get("transactionSafety"),
        }
    if "withCandidates" in result and "accounts" in result:
        return {
            "ok": bool(result.get("ok")),
            "query": result.get("query", ""),
            "scanned": result.get("scanned", 0),
            "withCandidates": result.get("withCandidates", 0),
            "accounts": result.get("accounts") or [],
        }
    return {
        "ok": bool(result.get("ok", True)),
        "accountId": result.get("accountId"),
        "offline": result.get("offline"),
        "candidateCount": len(result.get("candidates") or []),
        "safeNativeSwapPath": (result.get("contract") or {}).get("safeNativeSwapPath"),
        "safeNativeSwapAction": (result.get("contract") or {}).get("safeNativeSwapAction"),
        "confidence": (result.get("contract") or {}).get("confidence"),
        "errors": result.get("errors") or {},
    }


def scan_accounts(base, token, query="", limit=0):
    if query:
        roster_query = urllib.parse.urlencode({"q": query})
        rows = request_json("GET", f"{base}/api/characters?{roster_query}", token=token)
        if not isinstance(rows, list):
            raise SystemExit("admin API returned unexpected character search payload")
    else:
        roster = request_json("GET", f"{base}/api/characters/roster", token=token)
        if not isinstance(roster, dict):
            raise SystemExit("admin API returned unexpected roster payload")
        rows = list(roster.get("online") or []) + list(roster.get("offline") or [])
    if limit:
        rows = rows[:limit]
    scanned = []
    with_candidates = []
    for row in rows:
        account_id = row.get("account_id")
        if account_id is None:
            continue
        slot_query = urllib.parse.urlencode({"account_id": account_id})
        slots = request_json("GET", f"{base}/api/admin/character-slots?{slot_query}", token=token)
        summary = summarize(slots)
        summary["characterName"] = row.get("character_name")
        summary["onlineStatus"] = row.get("online_status")
        scanned.append(summary)
        if summary.get("candidateCount", 0) > 0:
            with_candidates.append(summary)
    return {
        "ok": True,
        "query": query,
        "scanned": len(scanned),
        "withCandidates": len(with_candidates),
        "accounts": with_candidates,
    }


def admin_base(args, env_file):
    if args.base_url:
        return args.base_url.rstrip("/")
    port = env("DUNE_ADMIN_HOST_PORT", "18080", env_file)
    return f"http://127.0.0.1:{port}"


def main_with_argv(argv=None):
    parser = argparse.ArgumentParser(description="Inspect, plan, or execute DASH character slot switch/restore operations.")
    parser.add_argument("--env-file", type=pathlib.Path, default=ROOT / ".env")
    parser.add_argument("--base-url", default="", help="Admin panel base URL. Default: http://127.0.0.1:${DUNE_ADMIN_HOST_PORT:-18080}")
    parser.add_argument("--token", default="", help="Admin token. Default: DUNE_ADMIN_TOKEN from env/.env when configured.")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--target-account-id", type=int)
    parser.add_argument("--action", choices=("scan", "inspect", "new-character", "switch-character", "restore-character"), default="inspect")
    parser.add_argument("--query", default="", help="Roster search query for --action scan.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum roster rows to inspect for --action scan. Default: all returned rows.")
    parser.add_argument("--execute", action="store_true", help="Execute switch-character/restore-character through the admin API.")
    parser.add_argument("--confirm", default="", help='Required for execution: "SWAP CHARACTER".')
    parser.add_argument("--summary", action="store_true", help="Print a compact operator summary instead of the full response.")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    env_file = read_env(args.env_file)
    base = admin_base(args, env_file)
    token = args.token or read_secret_env("DUNE_ADMIN_TOKEN", env_file.get("DUNE_ADMIN_TOKEN", ""))

    if args.action == "scan":
        result = scan_accounts(base, token, query=args.query, limit=args.limit)
    else:
        if args.account_id is None:
            raise SystemExit("--account-id is required unless --action scan")
    if args.action == "inspect":
        query = urllib.parse.urlencode({"account_id": args.account_id})
        result = request_json("GET", f"{base}/api/admin/character-slots?{query}", token=token)
    elif args.action != "scan":
        if args.action in ("switch-character", "restore-character") and args.target_account_id is None:
            raise SystemExit("--target-account-id is required for switch-character and restore-character")
        body = {
            "dry_run": not args.execute,
            "account_id": args.account_id,
            "action": args.action,
            "target_account_id": args.target_account_id,
        }
        if args.execute:
            if args.action == "new-character":
                raise SystemExit("new-character execution is intentionally blocked; use plan/inspect only")
            if args.confirm != "SWAP CHARACTER":
                raise SystemExit('execution requires --confirm "SWAP CHARACTER"')
            body["confirm"] = args.confirm
            endpoint = "execute"
        else:
            endpoint = "plan"
        result = request_json("POST", f"{base}/api/admin/character-slots/{endpoint}", token=token, body=body)

    output = summarize(result) if args.summary else result
    if args.pretty or args.summary:
        json.dump(output, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        json.dump(output, sys.stdout, separators=(",", ":"), sort_keys=True)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main_with_argv()
