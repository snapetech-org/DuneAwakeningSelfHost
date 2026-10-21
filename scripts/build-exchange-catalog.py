#!/usr/bin/env python3
import argparse
import csv
import json
import os
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from secret_env import read_secret_env
from exchange_category_map import EXCHANGE_CATEGORY_MASKS

DEFAULT_MANUAL = ROOT / "config" / "artificial-exchange-prices.csv"
DEFAULT_SNAPSHOT_DIR = ROOT / "data" / "exchange-price-snapshots"
DEFAULT_OUTPUT_DIR = ROOT / "backups" / "admin-panel" / "artificial-exchange"
DEFAULT_SOURCE_CATEGORY_MAP = DEFAULT_OUTPUT_DIR / "source-category-map.json"
DEFAULT_CATEGORY_OVERRIDES = ROOT / "config" / "exchange-category-overrides.csv"
FIELDS = [
    "template_id",
    "display_name",
    "category",
    "category_mask",
    "category_depth",
    "sellable_status",
    "baseline_price",
    "max_buy_price",
    "price_floor",
    "price_ceiling",
    "liquidity_tier",
    "enabled",
    "source",
    "confidence",
    "notes",
]
CONFIDENCE = {"low", "moderate", "high"}
LIQUIDITY = {"low", "medium", "high"}


def read_env_file(path):
    values = {}
    try:
        for raw in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def should_read_env_files():
    return os.environ.get("DUNE_ARTIFICIAL_EXCHANGE_SKIP_DOTENV_FILE", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    )


FILE_ENV = {}
if should_read_env_files():
    for env_path in ("/workspace/.env", ROOT / ".env"):
        FILE_ENV.update(read_env_file(env_path))


def env(name, default=""):
    value = os.environ.get(name)
    if value not in (None, ""):
        return value
    return FILE_ENV.get(name, default)


def secret_env(name, default=""):
    if os.environ.get(f"{name}_FILE", "").strip():
        return read_secret_env(name, default)
    return env(name, default)


def db_default_host():
    return "postgres" if pathlib.Path("/workspace/.env").exists() else "127.0.0.1"


def db_default_port():
    return "5432" if pathlib.Path("/workspace/.env").exists() else "15431"


def connect_db():
    import psycopg2
    import psycopg2.extras

    return psycopg2.connect(
        host=env("DUNE_ADMIN_DB_HOST", db_default_host()),
        port=env("DUNE_ADMIN_DB_PORT", db_default_port()),
        user=env("DUNE_ADMIN_DB_USER", "dune"),
        password=secret_env("DUNE_ADMIN_DB_PASSWORD") or secret_env("POSTGRES_DUNE_PASSWORD"),
        dbname=env("DUNE_ADMIN_DB_NAME", env("DUNE_DATABASE", "dune_sb_1_4_0_0")),
        connect_timeout=5,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def parse_bool(value, *, row_name):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{row_name}: enabled must be true or false")


def parse_int(value, field, row_name):
    text = str(value).strip()
    if text == "":
        return None
    try:
        out = int(text)
    except ValueError as exc:
        raise ValueError(f"{row_name}: {field} must be an integer") from exc
    if out < 0:
        raise ValueError(f"{row_name}: {field} must be non-negative")
    return out


def clean_row(raw, source_name):
    row = {field: (raw.get(field, "") if raw.get(field, "") is not None else "") for field in FIELDS}
    tid = str(row["template_id"]).strip()
    if not tid:
        raise ValueError(f"{source_name}: template_id is required")
    row["template_id"] = tid
    row["display_name"] = str(row["display_name"] or tid).strip()
    row["category"] = str(row["category"] or "unknown").strip()
    row["category_mask"] = parse_int(row["category_mask"], "category_mask", source_name)
    row["category_depth"] = parse_int(row["category_depth"], "category_depth", source_name)
    if row["category_mask"] is None:
        row["category_mask"] = 0
    if row["category_depth"] is None:
        row["category_depth"] = 0
    if row["category_depth"] > 4:
        raise ValueError(f"{source_name}: category_depth must be between 0 and 4")
    row["sellable_status"] = str(row["sellable_status"] or "known").strip()
    row["baseline_price"] = parse_int(row["baseline_price"], "baseline_price", source_name)
    row["max_buy_price"] = parse_int(row["max_buy_price"], "max_buy_price", source_name)
    row["price_floor"] = parse_int(row["price_floor"], "price_floor", source_name)
    row["price_ceiling"] = parse_int(row["price_ceiling"], "price_ceiling", source_name)
    row["liquidity_tier"] = str(row["liquidity_tier"] or "low").strip().lower()
    if row["liquidity_tier"] not in LIQUIDITY:
        raise ValueError(f"{source_name}: liquidity_tier must be one of {sorted(LIQUIDITY)}")
    row["enabled"] = parse_bool(row["enabled"] if row["enabled"] != "" else "false", row_name=source_name)
    row["source"] = str(row["source"] or "manual").strip()
    row["confidence"] = str(row["confidence"] or "low").strip().lower()
    if row["confidence"] not in CONFIDENCE:
        raise ValueError(f"{source_name}: confidence must be one of {sorted(CONFIDENCE)}")
    row["notes"] = str(row["notes"] or "").strip()
    if row["baseline_price"] is None and row["max_buy_price"] is not None:
        row["baseline_price"] = row["max_buy_price"]
    if row["max_buy_price"] is None and row["baseline_price"] is not None:
        row["max_buy_price"] = int(row["baseline_price"] * 0.8)
    if row["price_floor"] is None and row["baseline_price"] is not None:
        row["price_floor"] = row["baseline_price"]
    if row["price_ceiling"] is None and row["baseline_price"] is not None:
        row["price_ceiling"] = row["baseline_price"]
    return row


def read_csv_rows(path, *, required=False):
    rows = []
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return rows
    with path.open(newline="", encoding="utf-8") as fh:
        filtered = (line for line in fh if line.strip() and not line.lstrip().startswith("#"))
        reader = csv.DictReader(filtered)
        if not reader.fieldnames:
            return rows
        missing = [field for field in ("template_id",) if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"{path}: missing required columns: {', '.join(missing)}")
        seen = set()
        for number, raw in enumerate(reader, start=2):
            if not raw or not raw.get("template_id"):
                continue
            row = clean_row(raw, f"{path}:{number}")
            if row["template_id"] in seen:
                raise ValueError(f"{path}:{number}: duplicate template_id {row['template_id']}")
            seen.add(row["template_id"])
            rows.append(row)
    return rows


def observed_from_db():
    rows = []
    try:
        conn = connect_db()
    except Exception as exc:
        return rows, str(exc)
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            select
                o.template_id,
                max(o.template_id) as display_name,
                'observed' as category,
                'observed' as sellable_status,
                percentile_cont(0.5) within group (order by o.item_price)::bigint as baseline_price,
                greatest(1, (percentile_cont(0.5) within group (order by o.item_price) * 0.80)::bigint) as max_buy_price,
                count(*) as observed_orders
            from dune.dune_exchange_orders o
            join dune.dune_exchange_sell_orders s on s.order_id = o.id
            where o.template_id is not null and o.item_price is not null and o.item_price > 0
            group by o.template_id
            order by o.template_id
            """
        )
        for raw in cur.fetchall():
            count = int(raw["observed_orders"] or 0)
            rows.append(clean_row({
                "template_id": raw["template_id"],
                "display_name": raw["display_name"],
                "category": raw["category"],
                "category_mask": 0,
                "category_depth": 0,
                "sellable_status": raw["sellable_status"],
                "baseline_price": raw["baseline_price"],
                "max_buy_price": raw["max_buy_price"],
                "liquidity_tier": "medium" if count >= 3 else "low",
                "enabled": "false",
                "source": "local-db",
                "confidence": "moderate" if count >= 3 else "low",
                "notes": f"observed_orders={count}; disabled until reviewed",
            }, "local-db"))
    conn.close()
    return rows, None


def price_from_values(values):
    values = sorted(int(v) for v in values if int(v) > 0)
    if not values:
        return None
    if len(values) >= 10:
        trim = max(1, len(values) // 10)
        values = values[trim:-trim] or values
    return int(statistics.median(values))


def merge_snapshot_rows(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["template_id"], {"row": row, "prices": []})
        grouped[row["template_id"]]["prices"].append(row["baseline_price"] or row["max_buy_price"] or 0)
    out = []
    for tid, item in sorted(grouped.items()):
        base = dict(item["row"])
        price = price_from_values(item["prices"])
        if price:
            base["baseline_price"] = price
            base["max_buy_price"] = int(price * 0.8)
        count = len(item["prices"])
        base["source"] = "snapshot"
        base["confidence"] = "moderate" if count >= 3 else "low"
        base["liquidity_tier"] = "medium" if count >= 3 else "low"
        base["enabled"] = False
        base["notes"] = f"snapshot_count={count}; disabled until reviewed"
        out.append(base)
    return out


def load_source_category_map(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"source category map missing: {path}"
    except json.JSONDecodeError as exc:
        return {}, f"source category map invalid: {path}: {exc}"
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        return {}, f"source category map has no items object: {path}"
    return items, None


def reconcile_source_categories(rows, source_map):
    if not source_map:
        return rows, {"updated": 0, "missing": 0}
    updated = 0
    missing = 0
    out = []
    for row in rows:
        source = source_map.get(row["template_id"])
        if not source:
            missing += 1
            out.append(row)
            continue
        next_row = dict(row)
        changed = False
        for field in ("category", "category_mask", "category_depth"):
            if field not in source:
                continue
            value = source[field]
            if field in ("category_mask", "category_depth"):
                value = int(value)
            if next_row.get(field) != value:
                next_row[field] = value
                changed = True
        if changed:
            notes = next_row.get("notes") or ""
            marker = "category reconciled from source-category-map"
            next_row["notes"] = f"{notes}; {marker}".strip("; ")
            updated += 1
        out.append(next_row)
    return out, {"updated": updated, "missing": missing}


def reconcile_known_category_masks(rows):
    updated = 0
    missing = 0
    out = []
    for row in rows:
        expected = EXCHANGE_CATEGORY_MASKS.get(row["category"])
        if not expected:
            missing += 1
            out.append(row)
            continue
        expected_mask, expected_depth = expected
        if row.get("category_mask") == expected_mask and row.get("category_depth") == expected_depth:
            out.append(row)
            continue
        next_row = dict(row)
        next_row["category_mask"] = expected_mask
        next_row["category_depth"] = expected_depth
        notes = next_row.get("notes") or ""
        marker = "category mask reconciled from exchange_category_map"
        if marker not in notes:
            next_row["notes"] = f"{notes}; {marker}".strip("; ")
        updated += 1
        out.append(next_row)
    return out, {"updated": updated, "missing": missing}


def category_from_identity(row):
    template_id = str(row.get("template_id") or "").lower()
    display_name = str(row.get("display_name") or "").lower()
    text = f"{template_id} {display_name}"
    is_schematic = "schematic" in template_id or "schematic" in display_name

    if is_schematic and ("socialclothing" in text or "social clothing" in text):
        return "schematics/armor/social"
    if is_schematic and ("consumables_spice" in text or "spiced food" in text or "spiced drink" in text):
        return "schematics/utility"
    if is_schematic and any(token in text for token in ("respawnbeacon", "respawn beacon", "stilltent", "still tent")):
        return "schematics/utility/deployables"
    if not is_schematic and template_id.startswith("spiceaddictionconsumable"):
        return "consumables/spice"
    if not is_schematic and template_id.startswith("d_harkar_"):
        return "weapons/ranged"
    if not is_schematic and "armorpack_heavy" in template_id:
        return "armor/heavy"
    if not is_schematic and "armorpack_med" in template_id:
        return "armor/light"
    if any(token in text for token in ("dewreaper", "dew reaper", "bloodsack", "bloodbag", "bodyfluidextractor", "exsanguination")):
        return "schematics/utility/hydration" if is_schematic else "tools/hydration"
    if any(token in text for token in ("miningtool", "cutteray", "cutter ray", "cutterray")):
        return "schematics/utility/gathering" if is_schematic else "tools/gathering"
    if any(token in text for token in ("scanner", "surveyprobe", "survey probe", "seismicprobe", "sesmicprobe", "seismic probe", "hand scanner")):
        if any(vehicle in text for vehicle in ("sandbike", "ornithopter", "orni", "thopter")) and is_schematic:
            return None
        return "schematics/utility/cartography" if is_schematic else "tools/cartography"
    if any(token in text for token in ("respawnbeacon", "respawn beacon", "stilltent", "still tent")):
        return "schematics/utility/deployables" if is_schematic else "tools/deployables"
    return None


def load_category_overrides(path):
    """Reviewed template_id -> category overrides applied last, winning over the
    source-category-map and identity heuristics. Use for templates whose correct
    Exchange bucket is known but the generated source map mis-tags them (e.g.
    fuel commodities the wiki tags as generic refined resources)."""
    if not path or not path.exists():
        return {}
    overrides = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            tid = (raw.get("template_id") or "").strip()
            category = (raw.get("category") or "").strip()
            if tid and category:
                overrides[tid] = category
    return overrides


def apply_category_overrides(rows, overrides):
    if not overrides:
        return rows, {"updated": 0}
    updated = 0
    out = []
    for row in rows:
        category = overrides.get(row["template_id"])
        if not category or row.get("category") == category:
            out.append(row)
            continue
        next_row = dict(row)
        next_row["category"] = category
        # Clear mask/depth so reconcile_known_category_masks recomputes them from
        # the reviewed category instead of the stale source value.
        next_row["category_mask"] = 0
        next_row["category_depth"] = 0
        notes = next_row.get("notes") or ""
        marker = "category set from reviewed override"
        if marker not in notes:
            next_row["notes"] = f"{notes}; {marker}".strip("; ")
        updated += 1
        out.append(next_row)
    return out, {"updated": updated}


def reconcile_identity_categories(rows):
    updated = 0
    out = []
    for row in rows:
        category = category_from_identity(row)
        if not category or row.get("category") == category:
            out.append(row)
            continue
        next_row = dict(row)
        next_row["category"] = category
        notes = next_row.get("notes") or ""
        marker = "category reconciled from item identity"
        if marker not in notes:
            next_row["notes"] = f"{notes}; {marker}".strip("; ")
        updated += 1
        out.append(next_row)
    return out, {"updated": updated}


def write_outputs(rows, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    latest_json = output_dir / "catalog.json"
    latest_csv = output_dir / "catalog.csv"
    stamped = output_dir / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-catalog.json"
    payload = {"generatedAt": int(time.time()), "items": rows}
    latest_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    stamped.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with latest_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return latest_json, latest_csv


def main():
    parser = argparse.ArgumentParser(description="Build conservative artificial Exchange catalog.")
    parser.add_argument("--manual", type=pathlib.Path, default=DEFAULT_MANUAL)
    parser.add_argument("--snapshot-dir", type=pathlib.Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--source-category-map", type=pathlib.Path, default=DEFAULT_SOURCE_CATEGORY_MAP)
    parser.add_argument("--category-overrides", type=pathlib.Path, default=DEFAULT_CATEGORY_OVERRIDES)
    parser.add_argument("--no-source-category-reconcile", action="store_true")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    merged = {}
    warnings = []
    if not args.no_db:
        db_rows, error = observed_from_db()
        if error:
            warnings.append(f"DB observation skipped: {error}")
        for row in db_rows:
            merged[row["template_id"]] = row
    snapshot_rows = []
    for path in sorted(args.snapshot_dir.glob("*.csv")) if args.snapshot_dir.exists() else []:
        snapshot_rows.extend(read_csv_rows(path))
    for row in merge_snapshot_rows(snapshot_rows):
        merged.setdefault(row["template_id"], row)
    for row in read_csv_rows(args.manual):
        merged[row["template_id"]] = row
    rows = [merged[tid] for tid in sorted(merged)]
    category_reconcile = {"updated": 0, "missing": 0}
    if not args.no_source_category_reconcile:
        source_map, error = load_source_category_map(args.source_category_map)
        if error:
            warnings.append(error)
        rows, category_reconcile = reconcile_source_categories(rows, source_map)
    rows, identity_reconcile = reconcile_identity_categories(rows)
    overrides = load_category_overrides(args.category_overrides)
    rows, override_reconcile = apply_category_overrides(rows, overrides)
    rows, mask_reconcile = reconcile_known_category_masks(rows)
    latest_json, latest_csv = write_outputs(rows, args.output_dir)
    print(json.dumps({
        "ok": True,
        "items": len(rows),
        "enabled": sum(1 for row in rows if row["enabled"]),
        "catalog": str(latest_json),
        "csv": str(latest_csv),
        "categoryReconcile": category_reconcile,
        "identityReconcile": identity_reconcile,
        "categoryOverrides": override_reconcile,
        "categoryMaskReconcile": mask_reconcile,
        "warnings": warnings,
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
