#!/usr/bin/env python3
import argparse
import copy
import json
import math
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import traceback
import difflib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from secret_env import read_secret_env
from exchange_category_map import EXCHANGE_CATEGORY_MASKS
from dune_whisper_route import whisper_route_for_fls_id

STATE_DIR = ROOT / "backups" / "admin-panel" / "artificial-exchange"
CATALOG_PATH = STATE_DIR / "catalog.json"
AUDIT_PATH = STATE_DIR / "bot-audit.jsonl"
STATE_PATH = STATE_DIR / "bot-state.json"
SOURCE_CATEGORY_MAP_PATH = STATE_DIR / "source-category-map.json"
VERIFIED_CATEGORY_MAP_PATH = STATE_DIR / "verified-category-map.json"
STATS_LIBRARY_PATH = STATE_DIR / "stats-library.json"
SOURCE_CATEGORY_MAP_CACHE = {}
VERIFIED_CATEGORY_MAP_CACHE = {}
STATS_LIBRARY_CACHE = {}
CONFIRM = "RUN ARTIFICIAL EXCHANGE"
CLAIM_CONFIRM = "CLAIM ARTIFICIAL EXCHANGE"
FUND_CONFIRM = "FUND ARTIFICIAL EXCHANGE"
POPULATE_CONFIRM = "POPULATE ARTIFICIAL EXCHANGE"
CATEGORY_MASKS = EXCHANGE_CATEGORY_MASKS
PRICE_CATEGORY_MULTIPLIERS = {
    "armor/combat": 2.2,
    "armor/heavy": 2.2,
    "armor/light": 2.0,
    "armor/social": 1.5,
    "armor/stillsuit": 2.2,
    "building/patents": 1.0,
    "consumables/medical": 0.75,
    "consumables/spice": 0.75,
    "resources/components": 1.5,
    "resources/fuel": 1.8,
    "resources/raw": 2.0,
    "resources/refined": 2.2,
    "schematics/armor": 1.0,
    "schematics/vehicles": 1.0,
    "schematics/weapons": 1.0,
    "tools/cartography": 1.5,
    "tools/deployables": 1.5,
    "tools/gathering": 1.5,
    "tools/hydration": 1.5,
    "tools/mining": 1.5,
    "tools/utility": 2.0,
    "vehicles/ammunition": 1.5,
    "vehicles/buggy": 2.0,
    "vehicles/light_ornithopter": 2.0,
    "vehicles/medium_ornithopter": 2.0,
    "vehicles/ornithopter": 2.0,
    "vehicles/parts": 2.0,
    "vehicles/sandbike": 2.0,
    "vehicles/sandcrawler": 2.0,
    "vehicles/transport_ornithopter": 2.0,
    "weapons/ammunition": 1.5,
    "weapons/melee": 2.4,
    "weapons/ranged": 3.0,
}
# Verified uniform stats payload on genuine, learnable schematic/recipe items
# (kspls0 dune_sb_1_4_5_0: identical across all 308 player-owned samples spanning
# 148 distinct schematic templates). The template_id carries the recipe identity;
# this payload only marks the item as a real, full-durability learnable token.
SCHEMATIC_DURABILITY_STATS = {"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}
DEFAULT_STACKABLE_CATEGORIES = {
    "consumables/medical",
    "consumables/spice",
    "resources/components",
    "resources/fuel",
    "resources/raw",
    "resources/refined",
    "vehicles/ammunition",
    "weapons/ammunition",
}
SOURCE_MAP_FALLBACK_CATEGORIES = {
    "building/patents",
    "armor/social",
    "contracts",
    "tools/cartography",
    "tools/deployables",
    "tools/gathering",
    "tools/hydration",
    "tools/mining",
}
PRICE_CATEGORY_FLOORS = {
    # Commodity floors keep game-file placeholder rows (baseline_price=1) from
    # seeding as ~1-Solari junk. Applied to the pre-multiplier anchor, so real
    # market-priced commodities (Spice Melange, Plasteel, etc.) keep their price.
    "resources/raw": 20,
    "resources/refined": 100,
    "resources/fuel": 120,
    "resources/components": 250,
    "armor/combat": 1000,
    "armor/heavy": 1000,
    "armor/light": 1000,
    "armor/social": 1000,
    "armor/stillsuit": 1000,
    "building/patents": 2500,
    "contracts": 1000,
    "tools/cartography": 1000,
    "tools/deployables": 1000,
    "tools/gathering": 1000,
    "tools/hydration": 1000,
    "tools/mining": 1000,
    "tools/utility": 1000,
    "vehicles/ammunition": 1000,
    "vehicles/buggy": 1000,
    "vehicles/light_ornithopter": 1000,
    "vehicles/medium_ornithopter": 1000,
    "vehicles/ornithopter": 1000,
    "vehicles/parts": 1000,
    "vehicles/sandbike": 1000,
    "vehicles/sandcrawler": 1000,
    "vehicles/transport_ornithopter": 1000,
    "weapons/ammunition": 1000,
    "weapons/melee": 1000,
    "weapons/ranged": 1000,
}


def is_blueprint_category(category):
    text = str(category or "")
    return text == "building/patents" or text.startswith("schematics/")


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


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def apply_service_confirmation(args):
    """Supply the gated confirmation for a live long-running buyer service."""
    if getattr(args, "loop", False) and not getattr(args, "dry_run", True) and not getattr(args, "confirm", ""):
        args.confirm = env("DUNE_ARTIFICIAL_EXCHANGE_SERVICE_CONFIRM", CONFIRM)
    return args


def db_default_host():
    return "postgres" if pathlib.Path("/workspace/.env").exists() else "127.0.0.1"


def db_default_port():
    return "5432" if pathlib.Path("/workspace/.env").exists() else "15431"


def connect_db():
    import psycopg2
    import psycopg2.extras

    host = env("DUNE_ADMIN_DB_HOST", db_default_host())
    port = env("DUNE_ADMIN_DB_PORT", db_default_port())
    dbname = env("DUNE_ADMIN_DB_NAME", env("DUNE_DATABASE", "dune_sb_1_4_0_0"))
    log_event("db-connect-attempt", host=host, port=port, dbname=dbname)
    conn = psycopg2.connect(
        host=host,
        port=port,
        user=env("DUNE_ADMIN_DB_USER", "dune"),
        password=secret_env("DUNE_ADMIN_DB_PASSWORD") or secret_env("POSTGRES_DUNE_PASSWORD"),
        dbname=dbname,
        connect_timeout=5,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    log_event("db-connect-ok", host=host, port=port, dbname=dbname)
    return conn


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def audit(event):
    event = dict(event)
    event["ts"] = int(time.time())
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def log_event(event, **fields):
    print(json.dumps({"event": event, "ts": int(time.time()), **fields}, sort_keys=True, default=str), flush=True)


def log_failure(event, exc, **fields):
    payload = {
        "event": event,
        "ok": False,
        "error": str(exc),
        "exceptionType": type(exc).__name__,
        "traceback": traceback.format_exc(),
        **fields,
    }
    log_event(event, **{key: value for key, value in payload.items() if key != "event"})
    audit(payload)


def today_key():
    return time.strftime("%Y-%m-%d", time.localtime())


def load_catalog(path):
    payload = load_json(path, {"items": []})
    items = {}
    for row in payload.get("items", []):
        if row.get("template_id"):
            items[row["template_id"]] = row
    log_event("catalog-loaded", path=str(path), items=len(items), enabledItems=sum(1 for row in items.values() if row.get("enabled")))
    return items


def load_state():
    state = load_json(STATE_PATH, {})
    today = today_key()
    if state.get("day") != today:
        state = {"day": today, "spent_global": 0, "spent_by_seller": {}, "spent_by_template": {}, "seen_completed": [], "claimed_settlements": []}
    state.setdefault("claimed_settlements", [])
    return state


def spend_available(state, order, catalog_row):
    unit_price = int(order["item_price"])
    total_price = purchase_total_price(order)
    global_cap = int(env("DUNE_ARTIFICIAL_EXCHANGE_DAILY_SOLARI_CAP", "50000"))
    seller_cap = int(env("DUNE_ARTIFICIAL_EXCHANGE_DAILY_SELLER_CAP", "10000"))
    template_cap = int(env("DUNE_ARTIFICIAL_EXCHANGE_DAILY_TEMPLATE_CAP", "15000"))
    max_buy_price = int(catalog_row["max_buy_price"])
    max_buy_price_tolerance_pct = max(0.0, float(env("DUNE_ARTIFICIAL_EXCHANGE_MAX_BUY_PRICE_TOLERANCE_PCT", "10")))
    tolerated_max_buy_price = math.floor(max_buy_price * (1.0 + max_buy_price_tolerance_pct / 100.0))
    seller = str(order["owner_id"])
    template = order["template_id"]
    if state.get("spent_global", 0) + total_price > global_cap:
        return False, "global daily cap"
    if state["spent_by_seller"].get(seller, 0) + total_price > seller_cap:
        return False, "seller daily cap"
    if state["spent_by_template"].get(template, 0) + total_price > template_cap:
        return False, "template daily cap"
    if unit_price > tolerated_max_buy_price:
        return False, "above max_buy_price tolerance"
    return True, ""


def record_spend(state, order):
    price = purchase_total_price(order)
    seller = str(order["owner_id"])
    template = order["template_id"]
    state["spent_global"] = state.get("spent_global", 0) + price
    state["spent_by_seller"][seller] = state["spent_by_seller"].get(seller, 0) + price
    state["spent_by_template"][template] = state["spent_by_template"].get(template, 0) + price


def buy_probability(tier):
    defaults = {"low": "0.0004", "medium": "0.0004", "high": "0.0004"}
    return float(env(f"DUNE_ARTIFICIAL_EXCHANGE_{tier.upper()}_BUY_PROBABILITY", defaults.get(tier, "0.05")))


def blocked_sellers():
    return {item.strip() for item in env("DUNE_ARTIFICIAL_EXCHANGE_BLOCKED_SELLERS", "").split(",") if item.strip()}


def populator_owner_ids():
    ids = {item.strip() for item in env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_IDS", "").split(",") if item.strip()}
    owner_id = env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID", "").strip()
    if owner_id:
        ids.add(owner_id)
    return ids


def buyer_identity_conflict(args):
    """Return whether the buyer also owns the seeded populator listings."""
    buyer_id = int(getattr(args, "buyer_controller_id", 0) or 0)
    return buyer_id > 0 and str(buyer_id) in populator_owner_ids()


def buyer_skip_reason(order, args):
    if order.get("is_npc_order") and not args.include_npc_test_orders:
        return "npc order skipped"
    if str(order.get("owner_id")) in populator_owner_ids() and not args.include_npc_test_orders:
        return "populator owner skipped"
    return ""


def populator_catalog_rows(catalog):
    rows = []
    require_market_price = env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_MARKET_PRICE", True)
    allow_unpriced = env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ALLOW_UNPRICED_SEEDING", False)
    min_baseline_price = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_BASELINE_PRICE", "1"))
    for row in catalog.values():
        if not row.get("enabled"):
            continue
        baseline_price = row.get("baseline_price")
        if baseline_price in (None, "", 0):
            continue
        if int(baseline_price) < min_baseline_price and not is_blueprint_category(row.get("category")):
            continue
        if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_VALIDATED", True) and row.get("sellable_status") != "validated":
            continue
        tier = catalog_tier(row)
        if tier is None and (row.get("category") in SOURCE_MAP_FALLBACK_CATEGORIES or is_blueprint_category(row.get("category"))) and has_reconciled_game_category(row):
            tier = 2
        if tier is None or tier < int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_TIER", "2")):
            continue
        if (require_market_price or not allow_unpriced) and not catalog_has_market_price(row):
            continue
        rows.append(row)
    return rows


def catalog_tier(row):
    value = row.get("tier")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    notes = str(row.get("notes") or "")
    match = re.search(r"(?:^|[;,\s])tier\s*=\s*(\d+)(?:$|[;,\s])", notes, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:^|[^A-Za-z0-9])T(?:ier)?[_ -]?(\d+)(?:$|[^A-Za-z0-9])", str(row.get("template_id") or ""), re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def catalog_has_market_price(row):
    if str(row.get("source") or "").startswith("dune.exchange"):
        return True
    notes = str(row.get("notes") or "").lower()
    return "price_ceiling=dune.exchange" in notes or "price_source=market_price" in notes


def has_reconciled_game_category(row):
    notes = str(row.get("notes") or "").lower()
    return (
        "category mask reconciled from exchange_category_map" in notes
        or "category reconciled from source-category-map" in notes
        or "category reconciled from item identity" in notes
    )


def load_source_category_map(path=None):
    source_path = pathlib.Path(path or SOURCE_CATEGORY_MAP_PATH)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    cache_key = str(source_path)
    if cache_key in SOURCE_CATEGORY_MAP_CACHE:
        return SOURCE_CATEGORY_MAP_CACHE[cache_key]
    payload = load_json(source_path, {"items": {}})
    SOURCE_CATEGORY_MAP_CACHE[cache_key] = payload.get("items", {})
    return SOURCE_CATEGORY_MAP_CACHE[cache_key]


def load_verified_category_map(path=None):
    source_path = pathlib.Path(path or VERIFIED_CATEGORY_MAP_PATH)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    cache_key = str(source_path)
    if cache_key in VERIFIED_CATEGORY_MAP_CACHE:
        return VERIFIED_CATEGORY_MAP_CACHE[cache_key]
    payload = load_json(source_path, {"items": {}})
    VERIFIED_CATEGORY_MAP_CACHE[cache_key] = payload.get("items", {})
    return VERIFIED_CATEGORY_MAP_CACHE[cache_key]


def stats_library_path(path=None):
    source_path = pathlib.Path(path or env("DUNE_ARTIFICIAL_EXCHANGE_STATS_LIBRARY", str(STATS_LIBRARY_PATH)))
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    return source_path


def load_stats_library(path=None):
    source_path = stats_library_path(path)
    cache_key = str(source_path)
    if cache_key in STATS_LIBRARY_CACHE:
        return STATS_LIBRARY_CACHE[cache_key]
    payload = load_json(source_path, {"items": {}})
    items = payload.get("items", {})
    STATS_LIBRARY_CACHE[cache_key] = items if isinstance(items, dict) else {}
    return STATS_LIBRARY_CACHE[cache_key]


def stats_library_row(row):
    return load_stats_library().get(row["template_id"])


def stats_payload_for_row(row):
    library_row = stats_library_row(row)
    if not library_row:
        return None
    sample = library_row.get("selected") or {}
    stats = sample.get("stats")
    return stats if isinstance(stats, dict) and stats else None


def verified_category_row(row):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_SEEDING_VERIFIED", False):
        return None
    verified_map = load_verified_category_map(pathlib.Path(env("DUNE_ARTIFICIAL_EXCHANGE_VERIFIED_CATEGORY_MAP", str(VERIFIED_CATEGORY_MAP_PATH))))
    return verified_map.get(row["template_id"])


def is_augment_template(row):
    text = " ".join(str(row.get(key) or "") for key in ("template_id", "display_name", "category", "notes")).lower()
    if "module" in text:
        return False
    return any(token in text for token in ("weaponmod", "toolmod", "_mod_", " augment", "augment_"))


def inferred_catalog_category(row):
    template_id = str(row.get("template_id") or "")
    low_id = template_id.lower()
    name = str(row.get("display_name") or "").lower()
    notes = str(row.get("notes") or "").lower()
    haystack = f"{low_id} {name} {notes}"
    if "patent" in haystack:
        return "building/patents"
    if "schematic" in low_id or "unique_schematic=yes" in notes:
        if "socialclothing" in haystack or "social clothing" in haystack:
            return "schematics/armor/social"
        if "consumables_spice" in haystack or "spiced food" in haystack or "spiced drink" in haystack:
            return "schematics/utility"
        if any(token in haystack for token in ("respawnbeacon", "respawn beacon", "stilltent", "still tent")):
            return "schematics/utility/deployables"
        if any(token in haystack for token in ("sandcrawler", "crawler")):
            return "schematics/vehicles/sandcrawler"
        if any(token in haystack for token in ("transportorni", "transport ornithopter", "carrier ornithopter")):
            return "schematics/vehicles/transport_ornithopter"
        if any(token in haystack for token in ("mediumorni", "medium ornithopter")):
            return "schematics/vehicles/medium_ornithopter"
        if any(token in haystack for token in ("lightorni", "light ornithopter")):
            return "schematics/vehicles/light_ornithopter"
        if "sandbike" in haystack:
            return "schematics/vehicles/sandbike"
        if "buggy" in haystack:
            return "schematics/vehicles/buggy"
        if any(token in haystack for token in ("stillsuit", "still suit")):
            return "schematics/armor/stillsuit"
        if "social_" in haystack or "social " in haystack:
            return "schematics/armor/social"
        if "heavy" in haystack:
            return "schematics/armor/heavy"
        if "light" in haystack or "scout" in haystack or "combat_" in haystack:
            return "schematics/armor/light"
        if any(token in haystack for token in ("bloodbag", "bloodsack", "extractor", "dewreaper", "exsanguination")):
            return "schematics/utility/hydration"
        if any(token in haystack for token in ("miningtool", "cutteray", "cutter ray", "cutterray", "compactor")):
            return "schematics/utility/gathering"
        if any(token in haystack for token in ("scanner", "surveyprobe", "survey probe", "seismicprobe", "sesmicprobe", "seismic probe", "hand scanner")):
            return "schematics/utility/cartography"
        if any(token in haystack for token in ("suspensor", "shield")):
            return "schematics/utility"
        if any(token in haystack for token in ("dirk", "rapier", "sword", "kindjal", "blade")):
            return "schematics/weapons/melee"
        return "schematics/weapons/ranged"
    if any(token in haystack for token in ("building", "placeable")):
        return "building/placeables"
    if "contract" in haystack:
        return "contracts"
    if any(token in haystack for token in ("customization", "swatch", "dyepack", "dye")):
        return "customization"
    if "stillsuit" in haystack:
        return "armor/stillsuit"
    if "social_" in low_id:
        return "armor/social"
    if low_id.startswith("d_harkar_"):
        return "weapons/ranged"
    if "armorpack_heavy" in low_id:
        return "armor/heavy"
    if "armorpack_med" in low_id:
        return "armor/light"
    if "combat_" in low_id or "garment" in haystack or "armor" in haystack:
        return "armor/combat"
    if "sandbike" in haystack:
        return "vehicles/sandbike"
    if "ornithopter" in haystack:
        return "vehicles/ornithopter"
    if any(token in haystack for token in ("vehicle", "sandcrawler", "buggy", "tank")):
        return "vehicles/parts"
    if any(token in haystack for token in ("kindjal", "dirk", "sword", "rapier", "knife", "blade")):
        return "weapons/melee"
    if any(token in haystack for token in ("pistol", "smg", "rifle", "carbine", "scattergun", "lmg", "dart", "ammo", "disruptor", "lasgun", "flamethrower")):
        return "weapons/ranged"
    if any(token in haystack for token in ("dewreaper", "dew reaper", "bodyfluidextractor", "bloodsack", "bloodbag")):
        return "tools/hydration"
    if any(token in haystack for token in ("miningtool", "cutteray", "cutter ray", "cutterray")):
        return "tools/gathering"
    if any(token in haystack for token in ("scanner", "surveyprobe", "survey probe", "seismicprobe", "sesmicprobe", "seismic probe", "hand scanner")):
        return "tools/cartography"
    if any(token in haystack for token in ("respawnbeacon", "respawn beacon", "stilltent", "still tent")):
        return "tools/deployables"
    if any(token in haystack for token in ("powerpack", "binocular", "survey", "shield", "suspensor", "beacon", "glide", "backup")):
        return "tools/utility"
    if any(token in haystack for token in ("miningtool", "cutter", "compactor", "bodyfluidextractor", "repairtool", "dewreaper")):
        return "tools/mining"
    if any(token in haystack for token in ("healthpack", "bloodsack", "literjon", "decajon", "detox", "consumable")):
        return "consumables/medical"
    if low_id.startswith("spiceaddictionconsumable"):
        return "consumables/spice"
    if any(token in haystack for token in ("bar", "ingot", "paste", "lubricant", "filter", "fuelcanister", "silicone", "plastone", "flour")):
        return "resources/refined"
    if any(token in haystack for token in ("component", "part", "dust", "capacitor", "actuator", "core", "plating", "rangefinder", "welding", "servok")):
        return "resources/components"
    if any(token in haystack for token in ("ore", "stone", "fiber", "plant", "corpse", "spice", "seed", "raw")):
        return "resources/raw"
    return ""


def has_blueprint_identity(row):
    if str(row.get("template_id") or "") == "BuildingBlueprint_CopyDevice":
        return False
    text = " ".join(str(row.get(key) or "") for key in ("template_id", "display_name", "category")).lower()
    return any(token in text for token in ("schematic", "patent", "blueprint"))


def populator_category_skip_reason(row):
    category = str(row.get("category") or "")
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_BLUEPRINT_CATEGORIES", False) and is_blueprint_category(category):
        return "blueprint category seeding disabled"
    verified_row = verified_category_row(row)
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_SEEDING_VERIFIED", False) and not verified_row:
        return "missing verified category"
    source_row = None
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY", True):
        source_map = load_source_category_map(pathlib.Path(env("DUNE_ARTIFICIAL_EXCHANGE_SOURCE_CATEGORY_MAP", str(SOURCE_CATEGORY_MAP_PATH))))
        source_row = source_map.get(row["template_id"])
        if not source_row:
            source = str(row.get("source") or "").lower()
            source_category = (
                source.startswith("awakening-wiki-game-files")
                and category != "unknown"
                and has_reconciled_game_category(row)
            )
            if not source_category and not (category in SOURCE_MAP_FALLBACK_CATEGORIES and has_reconciled_game_category(row)):
                return "missing source category"
        elif source_row.get("category") != category:
            return f"source category mismatch expected {source_row.get('category')}"
        if source_row and not verified_row and (int(source_row.get("category_mask") or -1) != populator_category_mask(row) or int(source_row.get("category_depth") or -1) != populator_category_depth(row)):
            return f"source category mask mismatch expected {source_row.get('category_mask')}/{source_row.get('category_depth')}"
    if verified_row:
        verified_mask = int(verified_row.get("category_mask") or -1)
        verified_depth = int(verified_row.get("category_depth") or -1)
        if verified_mask < 0 or verified_depth < 0:
            return "invalid verified category"
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SKIP_UNKNOWN_CATEGORY", True):
        if category == "unknown" or (populator_category_mask(row) == 0 and populator_category_depth(row) == 0):
            return "unknown category"
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_DETERMINISTIC_CATEGORY", False) and not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_SOURCE_CATEGORY", True):
        inferred = inferred_catalog_category(row)
        if not inferred:
            return "unclassified category"
        if inferred != category:
            return f"category mismatch expected {inferred}"
        expected_mask, expected_depth = CATEGORY_MASKS.get(category, (None, None))
        if expected_mask is None:
            return "unmapped category"
        if populator_category_mask(row) != expected_mask or populator_category_depth(row) != expected_depth:
            return f"category mask mismatch expected {expected_mask}/{expected_depth}"
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_CATEGORY_REVIEW", True):
        notes = str(row.get("notes") or "").lower()
        source = str(row.get("source") or "").lower()
        source_map = load_source_category_map(pathlib.Path(env("DUNE_ARTIFICIAL_EXCHANGE_SOURCE_CATEGORY_MAP", str(SOURCE_CATEGORY_MAP_PATH))))
        source_row = source_map.get(row["template_id"])
        source_backed_category = (
            source_row
            and source_row.get("category") == category
            and int(source_row.get("category_mask") or -1) == populator_category_mask(row)
            and int(source_row.get("category_depth") or -1) == populator_category_depth(row)
        )
        if ("heuristic category" in notes or source == "local-bootstrap") and not source_backed_category:
            return "heuristic category"
    if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PROTECT_AUGMENTS_CATEGORY", True):
        category_mask = populator_category_mask(row)
        category_depth = populator_category_depth(row)
        augment_masks = {
            item.strip()
            for item in env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_AUGMENTS_CATEGORY_MASKS", "117506048").split(",")
            if item.strip()
        }
        if category_depth == 2 and str(category_mask) in augment_masks and not is_augment_template(row):
            return "non-augment in augments category"
    if has_blueprint_identity(row) and not is_blueprint_category(category):
        return "blueprint outside blueprint category"
    if populator_requires_stats(row) and not stats_payload_for_row(row):
        return "stateful item stats unavailable"
    return ""


def populator_eligible_rows(catalog):
    rows = []
    skipped = {}
    for row in populator_catalog_rows(catalog):
        reason = populator_category_skip_reason(row)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        rows.append(row)
    if skipped:
        log_event("populator-category-gate", eligible=len(rows), skipped=skipped)
    return rows


def jitter_price(baseline_price, jitter_pct):
    baseline = int(baseline_price)
    pct = max(0, int(jitter_pct))
    low = max(1, int(round(baseline * (100 - pct) / 100)))
    high = max(low, int(round(baseline * (100 + pct) / 100)))
    return random.randint(low, high)


def populator_price_multiplier(row=None):
    global_multiplier = float(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_MULTIPLIER", "1.0"))
    if global_multiplier <= 0:
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_MULTIPLIER must be positive")
    if not row:
        return global_multiplier
    category = str(row.get("category") or "")
    return global_multiplier * PRICE_CATEGORY_MULTIPLIERS.get(category, 1.5)


def scaled_price(value, row=None):
    multiplier = populator_price_multiplier(row)
    return max(1, int(round(float(value) * multiplier)))


def jitter_price_bounds(baseline_price, jitter_pct, row=None):
    baseline = scaled_price(baseline_price, row)
    pct = max(0, int(jitter_pct))
    low = max(1, int(round(baseline * (100 - pct) / 100)))
    high = max(low, int(round(baseline * (100 + pct) / 100)))
    return low, high


def game_file_price(row):
    match = re.search(r"(?:^|; )game_file_price=(\d+)", str(row.get("notes") or ""))
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def populator_price_anchor(row):
    floor = row.get("price_floor")
    ceiling = row.get("price_ceiling")
    baseline_price = row.get("baseline_price")
    if baseline_price not in (None, ""):
        anchor = int(baseline_price)
    elif floor not in (None, "") and ceiling not in (None, ""):
        anchor = int(round((int(floor) + int(ceiling)) / 2))
    else:
        raise RuntimeError(f"missing baseline_price or price floor/ceiling for {row.get('template_id', 'unknown template')}")
    game_price = game_file_price(row)
    outlier_ratio = float(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_GAME_FILE_OUTLIER_RATIO", "8"))
    if game_price and outlier_ratio > 0 and anchor / game_price > outlier_ratio:
        anchor = math.sqrt(anchor * game_price)
    if is_blueprint_category(row.get("category")):
        tier = catalog_tier(row) or 2
        floor = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_BLUEPRINT_PRICE_FLOOR", "2500"))
        tier_step = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_BLUEPRINT_PRICE_TIER_STEP", "1500"))
        anchor = max(anchor, floor + max(0, tier - 2) * tier_step)
    category_floor = PRICE_CATEGORY_FLOORS.get(str(row.get("category") or ""))
    if category_floor:
        anchor = max(anchor, category_floor)
    return anchor


def populator_price_bounds(row, jitter_pct):
    min_span = max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_PRICE_SPAN", "1")))
    jitter_pct = max(0, int(jitter_pct))
    anchor = populator_price_anchor(row)
    low, high = jitter_price_bounds(anchor, jitter_pct, row)
    if high - low + 1 < min_span:
        high = low + min_span - 1
    return low, high


def planned_unique_price(row, jitter_pct, used_prices):
    low, high = populator_price_bounds(row, jitter_pct)
    template_id = row["template_id"]
    used = used_prices.setdefault(template_id, set())
    span = high - low + 1
    if len(used) >= span:
        raise RuntimeError(f"not enough unique prices for {template_id} in jitter range {low}-{high}")
    if span <= 10000:
        available = [price for price in range(low, high + 1) if price not in used]
        price = random.choice(available)
        used.add(price)
        return price
    for _ in range(100):
        price = random.randint(low, high)
        if price not in used:
            used.add(price)
            return price
    for price in range(low, high + 1):
        if price not in used:
            used.add(price)
            return price
    raise RuntimeError(f"not enough unique prices for {template_id} in jitter range {low}-{high}")


def jitter_expiration(now, min_seconds, max_seconds):
    min_seconds = int(min_seconds)
    max_seconds = max(min_seconds, int(max_seconds))
    return int(now) + random.randint(min_seconds, max_seconds)


def desired_seed_count(active_count, target_min, target_max):
    forced_count = env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FORCE_COUNT", "").strip()
    if forced_count:
        return max(0, int(forced_count))
    target_min = int(target_min)
    target_max = max(target_min, int(target_max))
    if active_count >= target_min:
        return 0
    return random.randint(target_min - active_count, target_max - active_count)


def expire_probability_selected(order_ids, probability):
    probability = max(0.0, min(1.0, float(probability)))
    return [order_id for order_id in order_ids if random.random() < probability]


def cleanup_candidate_ids(active_orders, target_max_orders, expire_probability, target_min_orders=0):
    target_max_orders = max(0, int(target_max_orders))
    target_min_orders = max(0, int(target_min_orders))
    over_cap = max(0, len(active_orders) - target_max_orders)
    over_cap_ids = [row["id"] for row in active_orders[:over_cap]]
    random_expire_capacity = max(0, len(active_orders) - len(over_cap_ids) - target_min_orders)
    random_ids = expire_probability_selected([row["id"] for row in active_orders[over_cap:]], expire_probability)[:random_expire_capacity]
    return sorted(set(over_cap_ids + random_ids))


def template_category_key(row):
    return (row["template_id"], row.get("category") or "unknown", populator_category_mask(row), populator_category_depth(row))


def populator_active_fetch_limit(args, eligible_count=0):
    hard_max = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS", "20000"))
    target_max = int(getattr(args, "populator_target_max_orders", 0) or 0)
    limit = int(getattr(args, "limit", 0) or 0)
    eligible_count = int(eligible_count or 0)
    return max(limit, hard_max, target_max, eligible_count + hard_max)


def select_populator_rows(eligible, planned_count, active_orders, max_per_template):
    max_per_template = max(1, int(max_per_template))
    category_by_template_mask = {
        (row["template_id"], populator_category_mask(row), populator_category_depth(row)): row.get("category") or "unknown"
        for row in eligible
    }
    counts = {}
    for order in active_orders:
        mask = int(order.get("category_mask") or 0)
        depth = int(order.get("category_depth") or 0)
        category = order.get("category") or category_by_template_mask.get((order.get("template_id"), mask, depth)) or "unknown"
        key = (order.get("template_id"), category, mask, depth)
        counts[key] = counts.get(key, 0) + 1

    selected = []
    for _ in range(max(0, int(planned_count))):
        candidates = []
        for row in eligible:
            row_key = template_category_key(row)
            order_key = (row_key[0], row_key[1], row_key[2], row_key[3])
            if counts.get(order_key, 0) < max_per_template:
                candidates.append(row)
        if not candidates:
            break
        row = random.choice(candidates)
        row_key = template_category_key(row)
        order_key = (row_key[0], row_key[1], row_key[2], row_key[3])
        counts[order_key] = counts.get(order_key, 0) + 1
        selected.append(row)
    return selected


def free_position_candidates(occupied_positions, needed_count, start=0, max_position=100000):
    occupied = {int(pos) for pos in occupied_positions if pos is not None}
    positions = []
    position = int(start)
    while len(positions) < int(needed_count) and position <= int(max_position):
        if position not in occupied:
            positions.append(position)
        position += 1
    if len(positions) < int(needed_count):
        raise RuntimeError(f"not enough free staging inventory positions: needed {needed_count}, found {len(positions)}")
    return positions


def populator_quality_level(row):
    configured = int(row.get("quality_level") or catalog_tier(row) or env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_QUALITY_LEVEL", "2"))
    minimum = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MIN_QUALITY_LEVEL", "1"))
    if configured < minimum:
        raise RuntimeError(f"populator quality_level {configured} is below minimum {minimum}")
    return configured


def populator_stackable_categories():
    raw = env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_CATEGORIES", ",".join(sorted(DEFAULT_STACKABLE_CATEGORIES)))
    return {item.strip() for item in raw.split(",") if item.strip()}


def populator_is_stackable(row):
    return str(row.get("category") or "") in populator_stackable_categories()


def populator_stack_size(row):
    if populator_is_stackable(row):
        return max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE", "100")))
    return max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACK_SIZE", "1")))


def populator_stateful_stat_categories():
    raw = env(
        "DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STATEFUL_STAT_CATEGORIES",
        "armor/,tools/,vehicles/,weapons/",
    )
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def populator_requires_stats(row):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_STATS_FOR_STATEFUL_ITEMS", True):
        return False
    category = str(row.get("category") or "")
    if is_blueprint_category(category) or populator_is_stackable(row):
        return False
    return category.startswith(populator_stateful_stat_categories())


def populator_max_stack_size(row):
    if populator_is_stackable(row):
        return max(populator_stack_size(row), int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_FULL_STACK_SIZE", "100")))
    return max(populator_stack_size(row), int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_STACK_SIZE", "1")))


def populator_category_mask(row):
    verified_row = verified_category_row(row)
    if verified_row:
        return int(verified_row.get("category_mask") or 0)
    return int(row.get("category_mask") if row.get("category_mask") is not None else env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_MASK", "0"))


def populator_category_depth(row):
    verified_row = verified_category_row(row)
    if verified_row:
        return int(verified_row.get("category_depth") or 0)
    return int(row.get("category_depth") if row.get("category_depth") is not None else env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_DEPTH", "0"))


def fetch_orders(conn, exchange_id, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                o.id, o.exchange_id, o.access_point_id, o.owner_id, o.item_id,
                o.template_id, o.item_price, o.quality_level, o.expiration_time,
                o.durability_cur, o.durability_max, s.initial_stack_size,
                s.wear_normalized_price, i.stack_size,
                o.revision,
                o.is_npc_order,
                ps.character_name as seller_character_name,
                ps.online_status::text as seller_online_status,
                acc."user" as seller_fls_id
            from dune.dune_exchange_orders o
            join dune.dune_exchange_sell_orders s on s.order_id = o.id
            left join dune.items i on i.id = o.item_id
            left join dune.player_state ps on ps.player_controller_id = o.owner_id
            left join dune.accounts acc on acc.id = ps.account_id
            where o.exchange_id = %s
              and o.template_id is not null
              and o.item_price is not null
            order by o.id
            limit %s
            """,
            (exchange_id, limit),
        )
        return list(cur.fetchall())


def fetch_completed(conn, limit):
    with conn.cursor() as cur:
        cur.execute("select to_regclass('dune.dune_exchange_fulfilled_orders') as rel")
        if not cur.fetchone()["rel"]:
            return []
        cur.execute("select * from dune.dune_exchange_fulfilled_orders order by 1 desc limit %s", (limit,))
        return list(cur.fetchall())


def fetch_seeded_orders(conn, exchange_id, owner_id, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                o.id, o.exchange_id, o.access_point_id, o.owner_id, o.item_id,
                o.template_id, o.item_price, o.expiration_time, o.quality_level,
                o.category_mask, o.category_depth, i.stack_size
            from dune.dune_exchange_orders o
            left join dune.items i on i.id=o.item_id
            where o.exchange_id=%s
              and o.owner_id=%s
              and o.is_npc_order=true
            order by o.expiration_time, o.id
            limit %s
            """,
            (exchange_id, owner_id, limit),
        )
        return list(cur.fetchall())


def audit_seeded_stats(conn, args, catalog):
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                o.id, o.owner_id, o.template_id, o.item_price, o.quality_level as order_quality_level,
                i.id as item_id, i.quality_level as item_quality_level, i.stack_size,
                coalesce(i.stats, '{}'::jsonb) = '{}'::jsonb as empty_stats,
                o.category_mask, o.category_depth
            from dune.dune_exchange_orders o
            left join dune.items i on i.id=o.item_id
            where o.exchange_id=%s
              and o.is_npc_order=true
              and o.item_id is not null
              and (%s <= 0 or o.owner_id=%s)
            order by o.id desc
            limit %s
            """,
            (args.exchange_id, args.populator_owner_id, args.populator_owner_id, args.limit),
        )
        rows = [dict(row) for row in cur.fetchall()]

    by_category = {}
    unsafe = []
    mismatched_quality = []
    for row in rows:
        catalog_row = catalog.get(row.get("template_id")) or {}
        category = catalog_row.get("category") or "unknown"
        requires_stats = populator_requires_stats(catalog_row) if catalog_row else False
        key = category
        stats = by_category.setdefault(key, {"category": key, "orders": 0, "emptyStats": 0, "requiresStats": 0})
        stats["orders"] += 1
        if row.get("empty_stats"):
            stats["emptyStats"] += 1
        if requires_stats:
            stats["requiresStats"] += 1
        if row.get("empty_stats") and requires_stats:
            unsafe.append({**row, "category": category, "reason": "stateful item has empty stats"})
        if row.get("order_quality_level") != row.get("item_quality_level"):
            mismatched_quality.append({**row, "category": category, "reason": "order/item quality mismatch"})

    return {
        "ok": True,
        "dryRun": True,
        "exchangeId": args.exchange_id,
        "ownerId": args.populator_owner_id if args.populator_owner_id > 0 else None,
        "ordersChecked": len(rows),
        "emptyStats": sum(1 for row in rows if row.get("empty_stats")),
        "unsafeStatefulEmptyStats": len(unsafe),
        "qualityMismatches": len(mismatched_quality),
        "byCategory": sorted(by_category.values(), key=lambda item: (-item["emptyStats"], item["category"])),
        "unsafeExamples": unsafe[: args.report_skips],
        "qualityMismatchExamples": mismatched_quality[: args.report_skips],
    }


def stat_keys(stats):
    return sorted(stats.keys()) if isinstance(stats, dict) else []


def normalized_seed_stats(stats):
    if not isinstance(stats, dict):
        return {}
    cloned = copy.deepcopy(stats)
    durability = cloned.get("FItemStackAndDurabilityStats")
    if isinstance(durability, list) and len(durability) >= 2 and isinstance(durability[1], dict):
        values = durability[1]
        max_candidates = [
            values.get("MaxDurability"),
            values.get("DecayedMaxDurability"),
            values.get("CurrentDurability"),
        ]
        numeric = [float(value) for value in max_candidates if isinstance(value, (int, float))]
        if numeric:
            max_value = max(numeric)
            if "MaxDurability" in values:
                values["MaxDurability"] = max_value
            if "DecayedMaxDurability" in values:
                values["DecayedMaxDurability"] = max_value
            if "CurrentDurability" in values:
                values["CurrentDurability"] = max_value
    return cloned


def generalized_inferred_stats(stats):
    cloned = normalized_seed_stats(stats)
    customization = cloned.get("FCustomizationStats")
    if isinstance(customization, list) and len(customization) >= 2 and isinstance(customization[1], dict):
        customization[1] = {}
    return cloned


def stats_sample_score(row):
    stats = row.get("stats") or {}
    keys = set(stat_keys(stats))
    score = len(keys) * 10
    durability = stats.get("FItemStackAndDurabilityStats")
    if isinstance(durability, list) and len(durability) >= 2 and isinstance(durability[1], dict):
        values = durability[1]
        for key in ("MaxDurability", "DecayedMaxDurability", "CurrentDurability"):
            if key in values:
                score += 1
    return score


def template_similarity(left, right):
    def parts(value):
        return [part for part in re.split(r"[^A-Za-z0-9]+", str(value).lower()) if part and part not in {"d", "t"}]

    left_parts = set(parts(left))
    right_parts = set(parts(right))
    overlap = len(left_parts & right_parts) / max(1, len(left_parts | right_parts))
    ratio = difflib.SequenceMatcher(None, str(left).lower(), str(right).lower()).ratio()
    return overlap * 0.65 + ratio * 0.35


def top_level_category(category):
    return str(category or "unknown").split("/", 1)[0]


def catalog_rows_requiring_stats(catalog):
    return {
        row["template_id"]: row
        for row in catalog.values()
        if row.get("enabled") and populator_requires_stats(row)
    }


def fetch_stats_samples(conn, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                i.id as item_id,
                i.template_id,
                i.quality_level,
                i.stack_size,
                i.stats,
                inv.id as inventory_id,
                inv.actor_id,
                inv.exchange_id,
                exists (
                    select 1 from dune.dune_exchange_orders o where o.item_id=i.id
                ) as active_exchange_order_item
            from dune.items i
            left join dune.inventories inv on inv.id=i.inventory_id
            where i.template_id is not null
              and i.stats <> '{}'::jsonb
              and not exists (
                  select 1
                  from dune.dune_exchange_orders o
                  where o.item_id=i.id
                    and coalesce(o.is_npc_order, false)=true
              )
            order by i.id desc
            limit %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def merge_stats_library(existing, samples_by_template, catalog, source_label, samples_per_template):
    payload = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    items = payload.setdefault("items", {})
    now = int(time.time())
    for template_id, samples in sorted(samples_by_template.items()):
        catalog_row = catalog.get(template_id) or {}
        row = items.setdefault(template_id, {
            "templateId": template_id,
            "category": catalog_row.get("category"),
            "samples": [],
        })
        if catalog_row.get("category"):
            row["category"] = catalog_row.get("category")
        seen = {int(sample.get("itemId") or 0) for sample in row.get("samples", [])}
        for sample in samples:
            if int(sample["itemId"]) in seen:
                continue
            row.setdefault("samples", []).append(sample)
            seen.add(int(sample["itemId"]))
        row["samples"] = sorted(
            row.get("samples", []),
            key=lambda sample: (-int(sample.get("score") or 0), int(sample.get("itemId") or 0)),
        )[:samples_per_template]
        if row["samples"]:
            row["selected"] = row["samples"][0]
            row["qualityLevels"] = sorted({int(sample.get("qualityLevel") or 0) for sample in row["samples"]})
            row["statKeys"] = sorted({key for sample in row["samples"] for key in sample.get("statKeys", [])})
        row["updatedAt"] = now
        sources = set(row.get("sources", []))
        sources.add(source_label)
        row["sources"] = sorted(sources)
    payload["generatedAt"] = now
    return payload


def build_stats_library(conn, args, catalog):
    path = stats_library_path(args.stats_library)
    existing = load_json(path, {"items": {}}) if args.merge_stats_library else {"items": {}}
    required = catalog_rows_requiring_stats(catalog)
    rows = fetch_stats_samples(conn, args.stats_sample_limit)
    samples_by_template = {}
    for row in rows:
        template_id = row.get("template_id")
        if not template_id:
            continue
        stats = row.get("stats") or {}
        if not isinstance(stats, dict) or not stats:
            continue
        catalog_row = catalog.get(template_id) or {}
        if catalog_row and not populator_requires_stats(catalog_row):
            continue
        sample = {
            "itemId": int(row["item_id"]),
            "templateId": template_id,
            "qualityLevel": int(row.get("quality_level") or 0),
            "stackSize": int(row.get("stack_size") or 0),
            "inventoryId": row.get("inventory_id"),
            "actorId": row.get("actor_id"),
            "source": args.stats_source_label,
            "statKeys": stat_keys(stats),
            "score": stats_sample_score(row),
            "stats": normalized_seed_stats(stats),
        }
        samples_by_template.setdefault(template_id, []).append(sample)

    merged = merge_stats_library(existing, samples_by_template, catalog, args.stats_source_label, args.stats_samples_per_template)
    covered = set(merged.get("items", {}))
    missing_required = sorted(set(required) - covered)
    payload_items = merged.get("items", {})
    summary = {
        "ok": True,
        "dryRun": args.dry_run,
        "path": str(path),
        "sourceLabel": args.stats_source_label,
        "dbRowsScanned": len(rows),
        "templatesWithSamplesFromThisRun": len(samples_by_template),
        "libraryTemplates": len(payload_items),
        "requiredStatefulTemplates": len(required),
        "coveredRequiredStatefulTemplates": len(set(required) & covered),
        "missingRequiredStatefulTemplates": len(missing_required),
        "missingExamples": missing_required[: args.report_skips],
    }
    merged["summary"] = summary
    if not args.dry_run:
        save_json(path, merged)
        STATS_LIBRARY_CACHE.clear()
    return summary


def stats_library_report(args, catalog):
    items = load_stats_library(args.stats_library)
    required = catalog_rows_requiring_stats(catalog)
    covered = set(items)
    missing = sorted(set(required) - covered)
    by_category = {}
    for template_id, row in required.items():
        category = row.get("category") or "unknown"
        bucket = by_category.setdefault(category, {"category": category, "required": 0, "covered": 0, "missing": 0})
        bucket["required"] += 1
        if template_id in covered:
            bucket["covered"] += 1
        else:
            bucket["missing"] += 1
    return {
        "ok": True,
        "dryRun": True,
        "libraryTemplates": len(items),
        "requiredStatefulTemplates": len(required),
        "coveredRequiredStatefulTemplates": len(set(required) & covered),
        "missingRequiredStatefulTemplates": len(missing),
        "missingExamples": missing[: args.report_skips],
        "byCategory": sorted(by_category.values(), key=lambda row: (-row["missing"], row["category"])),
    }


def derive_stats_library(args, catalog):
    path = stats_library_path(args.stats_library)
    payload = load_json(path, {"items": {}})
    items = payload.setdefault("items", {})
    required = catalog_rows_requiring_stats(catalog)
    missing = [row for template_id, row in sorted(required.items()) if template_id not in items]
    candidates_by_category = {}
    for template_id, library_row in items.items():
        category = library_row.get("category") or (catalog.get(template_id) or {}).get("category")
        sample = library_row.get("selected") or {}
        stats = sample.get("stats")
        if not category or not isinstance(stats, dict) or not stats:
            continue
        candidates_by_category.setdefault(category, []).append((template_id, library_row, sample))

    derived = []
    skipped = []
    now = int(time.time())
    for row in missing:
        category = row.get("category") or "unknown"
        inference = "same-category-template-similarity"
        candidates = candidates_by_category.get(category) or []
        if not candidates:
            family = top_level_category(category)
            candidates = [
                candidate
                for candidate_category, category_candidates in candidates_by_category.items()
                if top_level_category(candidate_category) == family
                for candidate in category_candidates
            ]
            inference = "same-family-template-similarity"
        if not candidates:
            skipped.append({"templateId": row["template_id"], "category": category, "reason": "no category sample"})
            continue
        ranked = sorted(
            candidates,
            key=lambda item: (
                template_similarity(row["template_id"], item[0]),
                int((item[2] or {}).get("score") or 0),
            ),
            reverse=True,
        )
        source_template, source_row, source_sample = ranked[0]
        similarity = template_similarity(row["template_id"], source_template)
        stats = generalized_inferred_stats(source_sample["stats"])
        items[row["template_id"]] = {
            "templateId": row["template_id"],
            "category": category,
            "derived": True,
            "confidence": "derived-category",
            "inference": inference,
            "inferredFromTemplate": source_template,
            "inferredFromCategory": source_row.get("category") or (catalog.get(source_template) or {}).get("category"),
            "similarity": round(similarity, 4),
            "statKeys": stat_keys(stats),
            "qualityLevels": [populator_quality_level(row)],
            "sources": sorted(set((source_row.get("sources") or []) + [args.stats_source_label])),
            "selected": {
                "templateId": row["template_id"],
                "source": args.stats_source_label,
                "derivedFromTemplate": source_template,
                "derivedFromCategory": source_row.get("category") or (catalog.get(source_template) or {}).get("category"),
                "derivedFromItemId": source_sample.get("itemId"),
                "qualityLevel": populator_quality_level(row),
                "statKeys": stat_keys(stats),
                "score": int(source_sample.get("score") or 0),
                "stats": stats,
            },
            "samples": [],
            "updatedAt": now,
        }
        derived.append({
            "templateId": row["template_id"],
            "category": category,
            "fromTemplate": source_template,
            "similarity": round(similarity, 4),
        })

    required_after = catalog_rows_requiring_stats(catalog)
    covered_after = set(items) & set(required_after)
    still_missing = sorted(set(required_after) - set(items))
    result = {
        "ok": True,
        "dryRun": args.dry_run,
        "path": str(path),
        "sourceLabel": args.stats_source_label,
        "derived": len(derived),
        "skipped": len(skipped),
        "libraryTemplates": len(items),
        "requiredStatefulTemplates": len(required_after),
        "coveredRequiredStatefulTemplates": len(covered_after),
        "missingRequiredStatefulTemplates": len(still_missing),
        "derivedExamples": derived[: args.report_skips],
        "skippedExamples": skipped[: args.report_skips],
        "missingExamples": still_missing[: args.report_skips],
    }
    payload["generatedAt"] = now
    payload["deriveSummary"] = result
    if not args.dry_run:
        save_json(path, payload)
        STATS_LIBRARY_CACHE.clear()
    return result


def seeded_order_ids(rows):
    return {int(row["id"]) for row in rows}


def fetch_free_inventory_positions(conn, inventory_id, needed_count):
    if needed_count <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            select position_index
            from dune.items
            where inventory_id=%s
            """,
            (inventory_id,),
        )
        occupied = [row["position_index"] for row in cur.fetchall()]
    return free_position_candidates(
        occupied,
        needed_count,
        start=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_POSITION_START", "0")),
        max_position=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_POSITION_MAX", "100000")),
    )


def populator_preflight(conn, args, catalog=None, eligible=None, *, require_source=True):
    checks = []
    owner_id = int(args.populator_owner_id or 0)
    source_inventory_id = int(args.populator_source_inventory_id or 0)
    min_orders = int(args.populator_target_min_orders)
    max_orders = int(args.populator_target_max_orders)
    hard_max = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS", "20000"))
    stack_size = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACK_SIZE", "1"))
    max_stack_size = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_STACK_SIZE", "1"))

    checks.append({"name": "ownerConfigured", "ok": owner_id > 0, "ownerId": owner_id})
    checks.append({"name": "targetRange", "ok": 0 <= min_orders <= max_orders <= hard_max, "targetMin": min_orders, "targetMax": max_orders, "hardMax": hard_max})
    checks.append({"name": "stackRange", "ok": 1 <= stack_size <= max_stack_size, "stackSize": stack_size, "maxStackSize": max_stack_size})
    checks.append({"name": "priceJitter", "ok": 0 <= int(args.populator_price_jitter_pct) <= 100, "priceJitterPct": int(args.populator_price_jitter_pct)})
    checks.append({"name": "expiryRange", "ok": 60 <= int(args.populator_expiry_min_seconds) <= int(args.populator_expiry_max_seconds), "expiryMinSeconds": int(args.populator_expiry_min_seconds), "expiryMaxSeconds": int(args.populator_expiry_max_seconds)})

    with conn.cursor() as cur:
        cur.execute(
            """
            select player_controller_id, player_pawn_id, character_name
            from dune.player_state
            where player_controller_id=%s
            """,
            (owner_id,),
        )
        owner = cur.fetchone()
        checks.append({"name": "ownerExists", "ok": bool(owner), "owner": dict(owner) if owner else None})

        if require_source:
            cur.execute(
                """
                select id, actor_id, exchange_id, inventory_type
                from dune.inventories
                where id=%s
                """,
                (source_inventory_id,),
            )
            source = cur.fetchone()
            source_ok = bool(source) and source.get("actor_id") is None and int(source.get("exchange_id") or 0) == int(args.exchange_id)
            checks.append({"name": "sourceInventoryConfigured", "ok": source_inventory_id > 0, "sourceInventoryId": source_inventory_id})
            checks.append({"name": "sourceInventoryIsExchangeStaging", "ok": source_ok, "sourceInventory": dict(source) if source else None, "exchangeId": args.exchange_id})

            cur.execute("select dune.get_exchange_inventory_id(%s) as inventory_id", (args.exchange_id,))
            native = cur.fetchone()
            native_inventory_id = int(native["inventory_id"]) if native and native.get("inventory_id") is not None else 0
            checks.append({
                "name": "sourceInventoryMatchesNativeExchangeInventory",
                "ok": source_inventory_id == native_inventory_id,
                "sourceInventoryId": source_inventory_id,
                "nativeExchangeInventoryId": native_inventory_id,
            })

    if catalog is not None:
        enabled = [row for row in catalog.values() if row.get("enabled")]
        checks.append({"name": "catalogEnabledRows", "ok": bool(enabled), "enabledRows": len(enabled)})
    if eligible is not None:
        checks.append({"name": "eligibleValidatedRows", "ok": bool(eligible), "eligibleRows": len(eligible), "requireValidated": env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_REQUIRE_VALIDATED", True)})
        invalid_categories = [
            row["template_id"]
            for row in eligible
            if populator_category_mask(row) < 0 or populator_category_depth(row) < 0
        ]
        checks.append({"name": "categoryValues", "ok": not invalid_categories, "invalidTemplates": invalid_categories[:20]})

    ok = all(check["ok"] for check in checks)
    result = {"ok": ok, "checks": checks}
    log_event("populator-preflight", ok=ok, checks=len(checks), ownerId=owner_id, sourceInventoryId=source_inventory_id)
    if not ok:
        audit({"event": "populator-preflight-failed", **result})
    return result


def require_populator_preflight(conn, args, catalog=None, eligible=None, *, require_source=True):
    result = populator_preflight(conn, args, catalog, eligible, require_source=require_source)
    if not result["ok"]:
        failed = [check for check in result["checks"] if not check["ok"]]
        raise RuntimeError(f"populator preflight failed: {failed}")
    return result


def execute_native_expiry(cur, exchange_id, now):
    cur.execute(
        """
        select to_regprocedure('dune.dune_exchange_expire_orders(bigint,bigint,bigint,bigint)') as rel
        """
    )
    if not cur.fetchone()["rel"]:
        return {"called": False, "reason": "function missing"}
    purge_time = int(now) + int(env("DUNE_ARTIFICIAL_EXCHANGE_PURGE_SECONDS", "2419200"))
    expired_completion_type = int(env("DUNE_ARTIFICIAL_EXCHANGE_EXPIRED_COMPLETION_TYPE", "2"))
    cur.execute(
        """
        select dune.dune_exchange_expire_orders(%s, %s, %s, %s) as result
        """,
        (exchange_id, int(now), purge_time, expired_completion_type),
    )
    row = cur.fetchone()
    return {"called": True, "result": row["result"] if row else None}


def delete_seeded_orders(cur, order_ids, exchange_id, owner_id):
    if not order_ids:
        return []
    cur.execute(
        """
        select id, item_id, template_id, item_price
        from dune.dune_exchange_orders
        where id = any(%s)
          and exchange_id=%s
          and owner_id=%s
          and is_npc_order=true
        for update
        """,
        (order_ids, exchange_id, owner_id),
    )
    rows = [dict(row) for row in cur.fetchall()]
    item_id_by_order = {int(row["id"]): int(row["item_id"]) for row in rows if row.get("item_id") not in (None, 0, "")}
    cur.execute(
        """
        delete from dune.dune_exchange_orders
        where id = any(%s)
          and exchange_id=%s
          and owner_id=%s
          and is_npc_order=true
        returning id, item_id, template_id, item_price
        """,
        (order_ids, exchange_id, owner_id),
    )
    deleted_orders = [dict(row) for row in cur.fetchall()]
    deleted_order_ids = {int(row["id"]) for row in deleted_orders}
    candidate_item_ids = [item_id for order_id, item_id in item_id_by_order.items() if order_id in deleted_order_ids]
    if candidate_item_ids:
        cur.execute(
            """
            delete from dune.items i
            where i.id = any(%s)
              and i.inventory_id = dune.get_exchange_inventory_id(%s)
              and not exists (
                  select 1
                  from dune.dune_exchange_orders o
                  where o.item_id = i.id
              )
            returning id
            """,
            (candidate_item_ids, exchange_id),
        )
        deleted_items = [row["id"] for row in cur.fetchall()]
    else:
        deleted_items = []
    for row in deleted_orders:
        item_id = item_id_by_order.get(int(row["id"]))
        row["deletedItemIds"] = [item_id] if item_id in deleted_items else []
    return deleted_orders


def staging_stats_for_row(row):
    if populator_requires_stats(row):
        return stats_payload_for_row(row)
    if is_blueprint_category(row.get("category")):
        # Genuine learnable schematics/recipes carry this uniform durability
        # payload (verified across 148 player-owned templates on kspls0
        # dune_sb_1_4_5_0). Seeding empty stats produces an inert "package"
        # that cannot be learned, so blueprint rows always get the real payload.
        return copy.deepcopy(SCHEMATIC_DURABILITY_STATS)
    return {}


def create_staging_item(cur, row, source_inventory_id, position_index, stack_size):
    item_id = None
    cur.execute("select dune.advance_items_id_sequencer(1) as item_id")
    item_id = int(cur.fetchone()["item_id"])
    quality_level = populator_quality_level(row)
    stats = staging_stats_for_row(row)
    cur.execute(
        """
        select dune.save_item((
            %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s
        )::dune.inventoryitem)
        """,
        (
            item_id,
            source_inventory_id,
            stack_size,
            position_index,
            row["template_id"],
            True,
            int(time.time() * 1000),
            json.dumps(stats),
            quality_level,
            None,
        ),
    )
    return item_id, quality_level


def execute_seed_listing(cur, row, args, price, expiration_time, position_index):
    category_reason = populator_category_skip_reason(row)
    if category_reason:
        raise RuntimeError(f"refusing to seed {row['template_id']}: {category_reason}")
    stack_size = populator_stack_size(row)
    max_stack_size = populator_max_stack_size(row)
    item_id, quality_level = create_staging_item(cur, row, args.populator_source_inventory_id, position_index, stack_size)
    cur.execute(
        """
        select dune.dune_exchange_update_recurring_sell_order(
            %s::bigint, %s::bigint, %s::bigint, %s::bigint, %s::bigint,
            %s::bigint, %s::bigint, %s::integer, %s::smallint, %s::real,
            %s::real, %s::bigint, %s::bigint, %s::bigint
        ) as increment_added
        """,
        (
            args.exchange_id,
            expiration_time,
            args.populator_access_point_id,
            args.populator_owner_id,
            item_id,
            stack_size,
            max_stack_size,
            populator_category_mask(row),
            populator_category_depth(row),
            float(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DURABILITY_CUR", "1")),
            float(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DURABILITY_MAX", "1")),
            price,
            price,
            quality_level,
        ),
    )
    result = cur.fetchone()
    increment_added = result["increment_added"] if result else None
    if int(increment_added or 0) <= 0:
        raise RuntimeError(f"seed listing did not add inventory for {row['template_id']} at price {price}")
    return {"itemId": item_id, "incrementAdded": increment_added, "stackSize": stack_size, "maxStackSize": max_stack_size}


def plan_seed_events(args, eligible, active, positions):
    now = int(time.time())
    used_prices = {}
    for order in active:
        used_prices.setdefault(order["template_id"], set()).add(int(order["item_price"]))
    planned = []
    for index, row in enumerate(eligible):
        price = args.populator_force_price + index if args.populator_force_price is not None else planned_unique_price(row, args.populator_price_jitter_pct, used_prices)
        unit_low, unit_high = populator_price_bounds(row, args.populator_price_jitter_pct)
        stack_size = populator_stack_size(row)
        position_index = positions[index]
        planned.append({
            "row": row,
            "event": {
                "templateId": row["template_id"],
                "baselinePrice": row["baseline_price"],
                "price": price,
                "unitPriceBounds": [unit_low, unit_high],
                "stackSize": stack_size,
                "maxStackSize": populator_max_stack_size(row),
                "totalStackPrice": price * stack_size,
                "qualityLevel": populator_quality_level(row),
                "category": row.get("category"),
                "categoryMask": populator_category_mask(row),
                "categoryDepth": populator_category_depth(row),
                "expirationTime": jitter_expiration(now, args.populator_expiry_min_seconds, args.populator_expiry_max_seconds),
                "ownerId": args.populator_owner_id,
                "exchangeId": args.exchange_id,
                "accessPointId": args.populator_access_point_id,
                "sourceInventoryId": args.populator_source_inventory_id,
                "sourcePositionIndex": position_index,
                "plannedItemCreation": True,
                "dryRun": args.dry_run,
            },
        })
    return planned


def apply_seed_events(conn, args, planned):
    events = []
    for item in planned:
        event = item["event"]
        if not args.dry_run:
            if args.confirm != POPULATE_CONFIRM:
                raise RuntimeError(f"confirmation phrase required: {POPULATE_CONFIRM}")
            with conn.cursor() as cur:
                event.update(execute_seed_listing(cur, item["row"], args, event["price"], event["expirationTime"], event["sourcePositionIndex"]))
        events.append(event)
        audit({"event": "populator-seed-planned", **event})
    return events


def desired_populate_all_rows(eligible, active, catalog):
    stackable_target = max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_STACKABLE_TARGET_ORDERS", "13")))
    singleton_category_target = max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SINGLETON_CATEGORY_TARGET_ORDERS", "125")))
    max_per_template = max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY", "8")))
    active_by_template = {}
    active_by_category = {}
    active_by_template_category = {}
    for order in active:
        active_by_template[order["template_id"]] = active_by_template.get(order["template_id"], 0) + 1
        row = catalog.get(order["template_id"])
        if not row:
            continue
        category_key = (row.get("category") or "unknown", int(order.get("category_mask") or 0), int(order.get("category_depth") or 0))
        active_by_category[category_key] = active_by_category.get(category_key, 0) + 1
        template_key = template_category_key(row)
        active_by_template_category[template_key] = active_by_template_category.get(template_key, 0) + 1

    selected = []
    singleton_groups = {}
    for row in eligible:
        if populator_is_stackable(row):
            needed = max(0, stackable_target - active_by_template.get(row["template_id"], 0))
            selected.extend([row] * needed)
        else:
            key = (row.get("category") or "unknown", populator_category_mask(row), populator_category_depth(row))
            singleton_groups.setdefault(key, []).append(row)

    for key, rows in sorted(singleton_groups.items()):
        needed = max(0, singleton_category_target - active_by_category.get(key, 0))
        counts = {template_category_key(row): active_by_template_category.get(template_category_key(row), 0) for row in rows}
        for _ in range(needed):
            candidates = [row for row in rows if counts.get(template_category_key(row), 0) < max_per_template]
            if not candidates:
                break
            row = random.choice(candidates)
            counts[template_category_key(row)] = counts.get(template_category_key(row), 0) + 1
            selected.append(row)
    return selected


def expire_seeded_once(args):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED is false")
    if args.populator_owner_id <= 0:
        raise RuntimeError("--populator-owner-id is required")
    now = int(time.time())
    conn = connect_db()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            require_populator_preflight(conn, args, require_source=False)
            native = execute_native_expiry(cur, args.exchange_id, now)
            active = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, args.limit)
            candidates = cleanup_candidate_ids(active, args.populator_target_max_orders, args.populator_expire_probability, args.populator_target_min_orders)
            if args.dry_run:
                conn.rollback()
                deleted = []
            else:
                if args.confirm != POPULATE_CONFIRM:
                    raise RuntimeError(f"confirmation phrase required: {POPULATE_CONFIRM}")
                deleted = delete_seeded_orders(cur, candidates, args.exchange_id, args.populator_owner_id)
                conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return {"ok": True, "dryRun": args.dry_run, "nativeExpiry": native, "cleanupCandidates": candidates, "deleted": deleted}


def populate_once(args):
    started = time.time()
    log_event(
        "populate-start",
        dryRun=args.dry_run,
        exchangeId=args.exchange_id,
        ownerId=args.populator_owner_id,
        sourceInventoryId=args.populator_source_inventory_id,
    )
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED is false")
    if args.populator_owner_id <= 0:
        raise RuntimeError("--populator-owner-id is required")
    if args.populator_source_inventory_id <= 0:
        raise RuntimeError("--populator-source-inventory-id is required")
    catalog = load_catalog(args.catalog)
    eligible = populator_eligible_rows(catalog)
    if not eligible:
        return {"ok": True, "dryRun": args.dry_run, "planned": [], "reason": "no eligible catalog rows"}
    conn = connect_db()
    conn.autocommit = False
    planned = []
    cleanup = None
    try:
        require_populator_preflight(conn, args, catalog, eligible)
        active = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, args.limit)
        if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_USE_CATEGORY_TARGETS", True):
            selected_rows = desired_populate_all_rows(eligible, active, catalog)
            ceiling = min(args.populator_target_max_orders, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_HARD_MAX_ORDERS", "20000")))
            selected_rows = selected_rows[:max(0, ceiling - len(active))]
        else:
            count = args.populator_force_count if args.populator_force_count is not None else desired_seed_count(len(active), args.populator_target_min_orders, args.populator_target_max_orders)
            max_per_template = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY", "8"))
            selected_rows = select_populator_rows(eligible, count, active, max_per_template)
        positions = fetch_free_inventory_positions(conn, args.populator_source_inventory_id, len(selected_rows))
        planned = apply_seed_events(conn, args, plan_seed_events(args, selected_rows, active, positions))
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    if args.expire_seeded:
        cleanup = expire_seeded_once(args)
    result = {"ok": True, "dryRun": args.dry_run, "activeSeededOrders": len(active), "eligibleCatalogRows": len(eligible), "planned": planned, "cleanup": cleanup}
    log_event(
        "populate-complete",
        dryRun=args.dry_run,
        activeSeededOrders=len(active),
        eligibleCatalogRows=len(eligible),
        planned=len(planned),
        durationMs=int((time.time() - started) * 1000),
    )
    return result


def populate_categories_once(args):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CATEGORY_SEEDING_VERIFIED", False):
        raise RuntimeError("category seeding is disabled until Exchange category masks are verified from client/game data")
    started = time.time()
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED is false")
    if args.populator_owner_id <= 0:
        raise RuntimeError("--populator-owner-id is required")
    if args.populator_source_inventory_id <= 0:
        raise RuntimeError("--populator-source-inventory-id is required")
    catalog = load_catalog(args.catalog)
    eligible = populator_eligible_rows(catalog)
    grouped = {}
    for row in eligible:
        key = (row.get("category") or "unknown", populator_category_mask(row), populator_category_depth(row))
        grouped.setdefault(key, []).append(row)
    conn = connect_db()
    conn.autocommit = False
    planned = []
    summary = []
    try:
        require_populator_preflight(conn, args, catalog, eligible)
        active = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, max(args.limit, 10000))
        active_by_category = {}
        active_by_template_category = {}
        for order in active:
            row = catalog.get(order["template_id"])
            if not row:
                continue
            key = (row.get("category") or "unknown", int(order.get("category_mask") or 0), int(order.get("category_depth") or 0))
            active_by_category[key] = active_by_category.get(key, 0) + 1
            template_key = template_category_key(row)
            active_by_template_category[template_key] = active_by_template_category.get(template_key, 0) + 1
        for key, rows in sorted(grouped.items()):
            active_count = active_by_category.get(key, 0)
            needed = max(0, args.populator_target_min_orders - active_count)
            summary.append({"category": key[0], "categoryMask": key[1], "categoryDepth": key[2], "active": active_count, "planned": needed, "templates": len(rows)})
        total_needed = sum(row["planned"] for row in summary)
        positions = fetch_free_inventory_positions(conn, args.populator_source_inventory_id, total_needed)
        offset = 0
        planned_items = []
        for row in summary:
            if row["planned"] <= 0:
                continue
            key = (row["category"], row["categoryMask"], row["categoryDepth"])
            rows = grouped[key]
            category_active = [
                order for order in active
                if catalog.get(order["template_id"])
                and (catalog[order["template_id"]].get("category") or "unknown", int(order.get("category_mask") or 0), int(order.get("category_depth") or 0)) == key
            ]
            max_per_template = max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY", "8")))
            selected_rows = []
            counts = {template_category_key(item): active_by_template_category.get(template_category_key(item), 0) for item in rows}
            for _ in range(row["planned"]):
                candidates = [item for item in rows if counts.get(template_category_key(item), 0) < max_per_template]
                if not candidates:
                    break
                selected = random.choice(candidates)
                counts[template_category_key(selected)] = counts.get(template_category_key(selected), 0) + 1
                selected_rows.append(selected)
            row["planned"] = len(selected_rows)
            planned_items.extend(plan_seed_events(args, selected_rows, category_active, positions[offset:offset + row["planned"]]))
            offset += len(selected_rows)
        planned = apply_seed_events(conn, args, planned_items)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    result = {"ok": True, "dryRun": args.dry_run, "categories": summary, "planned": planned, "totalPlanned": len(planned)}
    log_event("populate-categories-complete", dryRun=args.dry_run, categories=len(summary), planned=len(planned), durationMs=int((time.time() - started) * 1000))
    return result


def populate_all_once(args):
    started = time.time()
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED is false")
    if args.populator_owner_id <= 0:
        raise RuntimeError("--populator-owner-id is required")
    if args.populator_source_inventory_id <= 0:
        raise RuntimeError("--populator-source-inventory-id is required")
    catalog = load_catalog(args.catalog)
    eligible = populator_eligible_rows(catalog)
    conn = connect_db()
    conn.autocommit = False
    planned = []
    try:
        require_populator_preflight(conn, args, catalog, eligible)
        active = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, max(args.limit, len(eligible) * 3 + 10000))
        selected_rows = desired_populate_all_rows(eligible, active, catalog)
        positions = fetch_free_inventory_positions(conn, args.populator_source_inventory_id, len(selected_rows))
        planned = apply_seed_events(conn, args, plan_seed_events(args, selected_rows, active, positions))
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    result = {
        "ok": True,
        "dryRun": args.dry_run,
        "activeSeededOrders": len(active),
        "eligibleCatalogRows": len(eligible),
        "missingCatalogRows": len(selected_rows),
        "planned": planned,
        "totalPlanned": len(planned),
    }
    log_event("populate-all-complete", dryRun=args.dry_run, activeSeededOrders=len(active), eligibleCatalogRows=len(eligible), planned=len(planned), durationMs=int((time.time() - started) * 1000))
    return result


def populate_templates_once(args):
    started = time.time()
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED is false")
    if args.populator_owner_id <= 0:
        raise RuntimeError("--populator-owner-id is required")
    if args.populator_source_inventory_id <= 0:
        raise RuntimeError("--populator-source-inventory-id is required")
    catalog = load_catalog(args.catalog)
    eligible = populator_eligible_rows(catalog)
    target = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TEMPLATE_TARGET_ORDERS", str(args.populator_target_min_orders)))
    conn = connect_db()
    conn.autocommit = False
    planned = []
    summary = []
    try:
        require_populator_preflight(conn, args, catalog, eligible)
        active = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, max(args.limit, len(eligible) * max(1, target) + 10000))
        active_by_template = {}
        active_by_template_category = {}
        for order in active:
            active_by_template[order["template_id"]] = active_by_template.get(order["template_id"], 0) + 1
            row = catalog.get(order["template_id"])
            if row:
                key = template_category_key(row)
                active_by_template_category[key] = active_by_template_category.get(key, 0) + 1
        selected_rows = []
        max_per_template = max(1, int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_MAX_PER_TEMPLATE_PER_CATEGORY", "8")))
        for row in eligible:
            active_count = active_by_template.get(row["template_id"], 0)
            category_count = active_by_template_category.get(template_category_key(row), 0)
            needed = max(0, min(target - active_count, max_per_template - category_count))
            summary.append({"templateId": row["template_id"], "active": active_count, "planned": needed, "tier": catalog_tier(row), "category": row.get("category")})
            selected_rows.extend([row] * needed)
        positions = fetch_free_inventory_positions(conn, args.populator_source_inventory_id, len(selected_rows))
        planned = apply_seed_events(conn, args, plan_seed_events(args, selected_rows, active, positions))
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    result = {
        "ok": True,
        "dryRun": args.dry_run,
        "targetPerTemplate": target,
        "activeSeededOrders": len(active),
        "eligibleCatalogRows": len(eligible),
        "templates": summary,
        "planned": planned,
        "totalPlanned": len(planned),
    }
    log_event("populate-templates-complete", dryRun=args.dry_run, eligibleCatalogRows=len(eligible), planned=len(planned), durationMs=int((time.time() - started) * 1000))
    return result


def validate_populator_once(args):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_LIVE_VALIDATION_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_LIVE_VALIDATION_ENABLED is false")
    if args.dry_run:
        validation_args = copy.copy(args)
        validation_args.populator_force_count = 1
        validation_args.expire_seeded = False
        planned = populate_once(validation_args)
        return {"ok": True, "dryRun": True, "planned": planned, "applied": False}
    if args.confirm != POPULATE_CONFIRM:
        raise RuntimeError(f"confirmation phrase required: {POPULATE_CONFIRM}")

    conn = connect_db()
    try:
        before = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, args.limit)
    finally:
        conn.close()

    validation_args = copy.copy(args)
    validation_args.populator_force_count = 1
    validation_args.populator_force_price = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_VALIDATION_PRICE", str(900000000 + int(time.time()) % 100000000)))
    validation_args.expire_seeded = False
    populate_result = populate_once(validation_args)

    conn = connect_db()
    cleanup = []
    try:
        after = fetch_seeded_orders(conn, args.exchange_id, args.populator_owner_id, args.limit)
        new_ids = sorted(seeded_order_ids(after) - seeded_order_ids(before))
        new_orders = [dict(row) for row in after if int(row["id"]) in new_ids]
        skip_args = copy.copy(args)
        skip_args.dry_run = True
        skip_args.ignore_enabled_gate = True
        skip_args.include_npc_test_orders = False
        skip_args.limit = max(args.limit, 10000)
        skip_args.report_skips = max(args.report_skips, 10000)
        buyer_result = scan_once(skip_args)
        skipped_new = [
            row for row in buyer_result.get("skipped", [])
            if int(row.get("orderId", 0)) in new_ids and row.get("reason") in ("npc order skipped", "populator owner skipped")
        ]
        with conn.cursor() as cur:
            cleanup = delete_seeded_orders(cur, new_ids, args.exchange_id, args.populator_owner_id)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    ok = bool(new_orders) and len(skipped_new) == len(new_orders) and len(cleanup) == len(new_orders)
    return {
        "ok": ok,
        "dryRun": False,
        "populate": populate_result,
        "newOrders": new_orders,
        "buyerSkippedNewOrders": skipped_new,
        "cleanup": cleanup,
    }


def settlement_status(row):
    completion_type = int(row.get("completion_type") if row.get("completion_type") is not None else -1)
    sold_completion_type = int(env("DUNE_ARTIFICIAL_EXCHANGE_SOLD_COMPLETION_TYPE", "1"))
    purchased_completion_type = int(env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASED_COMPLETION_TYPE", "0"))
    if completion_type == purchased_completion_type:
        return "purchased_item_storage"
    if completion_type != sold_completion_type:
        return "unknown_completion_type"
    if row.get("item_id") not in (None, 0, ""):
        return "seller_claim_has_item_id"
    if row.get("currency_balance") is None:
        return "unsafe_missing_base_solaris_balance"
    return "seller_solari_claim_ready"


def settlement_claim_safe(row):
    return settlement_status(row) == "seller_solari_claim_ready"


def settlement_report(conn, limit):
    with conn.cursor() as cur:
        cur.execute("select to_regclass('dune.dune_exchange_fulfilled_orders') as rel")
        if not cur.fetchone()["rel"]:
            return []
        cur.execute(
            """
            select
                o.id as order_id,
                o.owner_id,
                ps.character_name as owner_character_name,
                o.item_id,
                o.template_id,
                o.item_price,
                f.completion_type,
                f.stack_size,
                f.source_order_id,
                f.original_order_id,
                b.balance as currency_balance,
                (o.item_price * f.stack_size) as expected_solari
            from dune.dune_exchange_fulfilled_orders f
            join dune.dune_exchange_orders o on o.id = f.order_id
            left join dune.player_state ps on ps.player_controller_id = o.owner_id
            left join dune.player_virtual_currency_balances b
              on b.player_controller_id = o.owner_id
             and b.currency_id = 'Solaris'::dune.virtualwallettype
            order by o.id desc
            limit %s
            """,
            (limit,),
        )
        rows = []
        for raw in cur.fetchall():
            row = dict(raw)
            row["status"] = settlement_status(row)
            row["claimSafe"] = settlement_claim_safe(row)
            rows.append(row)
        return rows


def settlement_row(conn, order_id):
    rows = settlement_report(conn, 1000)
    for row in rows:
        if int(row["order_id"]) == int(order_id):
            return row
    return None


def settlement_claim_key(row):
    return ":".join(str(row.get(key) or "") for key in ("order_id", "source_order_id", "original_order_id", "completion_type"))


def read_exchange_balance(conn, owner_id):
    with conn.cursor() as cur:
        cur.execute("select dune.dune_exchange_retrieve_solari_balance(%s) as balance", (owner_id,))
        row = cur.fetchone()
    return int(row["balance"]) if row and row.get("balance") is not None else 0


def credit_solaris_bank(cur, controller_id, amount, owner_id=None):
    """Credit the player's visible bank balance, never carried inventory."""
    controller_id = int(controller_id)
    owner_id = controller_id if owner_id is None else int(owner_id)
    amount = int(amount)
    if amount <= 0:
        raise ValueError("bank Solari credit must be positive")

    # The row may not exist for a newly-created player. Create a zero row first,
    # then lock it so the credit and its verification are serialized.
    cur.execute(
        """
        insert into dune.player_virtual_currency_balances(player_controller_id, currency_id, balance)
        values(%s, 'Solaris'::dune.virtualwallettype, 0)
        on conflict (player_controller_id, currency_id) do nothing
        """,
        (controller_id,),
    )
    cur.execute(
        """
        select balance
        from dune.player_virtual_currency_balances
        where player_controller_id=%s and currency_id='Solaris'::dune.virtualwallettype
        for update
        """,
        (controller_id,),
    )
    before_row = cur.fetchone()
    if not before_row:
        raise RuntimeError(f"seller controller {controller_id} has no Solaris bank balance row")
    before_balance = int(before_row["balance"])

    cur.execute(
        """
        update dune.player_virtual_currency_balances
        set balance = balance + %s
        where player_controller_id=%s and currency_id='Solaris'::dune.virtualwallettype
        returning balance
        """,
        (amount, controller_id),
    )
    after_row = cur.fetchone()
    if not after_row:
        raise RuntimeError("Solaris bank credit did not update a balance row")
    after_balance = int(after_row["balance"])

    # Keep the Exchange user's visible Solari mirror aligned with the bank
    # balance, matching the established bank-grant path.
    cur.execute(
        """
        with ensured as (
            select dune.dune_exchange_get_user_id(%s) as id
        )
        update dune.dune_exchange_users
        set solari_balance=%s
        where id=(select id from ensured)
        returning solari_balance
        """,
        (owner_id, after_balance),
    )
    mirror_row = cur.fetchone()
    if not mirror_row or int(mirror_row["solari_balance"]) != after_balance:
        raise RuntimeError("Exchange Solari mirror verification failed")

    cur.execute(
        """
        select balance
        from dune.player_virtual_currency_balances
        where player_controller_id=%s and currency_id='Solaris'::dune.virtualwallettype
        """,
        (controller_id,),
    )
    verified_row = cur.fetchone()
    if not verified_row or int(verified_row["balance"]) != after_balance:
        raise RuntimeError("Solaris bank balance verification failed")
    return {
        "before_balance": before_balance,
        "after_balance": after_balance,
        "credited": after_balance - before_balance,
    }




def revision_matches(conn, order):
    with conn.cursor() as cur:
        cur.execute(
            "select revision, item_price from dune.dune_exchange_orders where id=%s for share",
            (order["id"],),
        )
        row = cur.fetchone()
    return bool(row and int(row["revision"]) == int(order["revision"]) and int(row["item_price"]) == int(order["item_price"]))


def fulfill_signature(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select pg_get_function_identity_arguments(p.oid) as args
            from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            where n.nspname='dune' and p.proname='dune_exchange_fulfill_sell_order'
            order by p.pronargs
            """
        )
        return [row["args"] for row in cur.fetchall()]


def purchase_postcondition(conn, order_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            select exists(
                select 1
                from dune.dune_exchange_orders
                where id=%s
            ) as active_order_exists,
            (
                select item_id
                from dune.dune_exchange_orders
                where id=%s
            ) as active_item_id,
            (
                select count(*)
                from dune.dune_exchange_fulfilled_orders
                where order_id=%s or original_order_id=%s
            ) as fulfilled_rows
            """,
            (order_id, order_id, order_id, order_id),
        )
        row = cur.fetchone() or {}
    return {
        "activeOrderExists": bool(row.get("active_order_exists")),
        "activeItemId": row.get("active_item_id"),
        "fulfilledRows": int(row.get("fulfilled_rows") or 0),
    }


def purchase_postcondition_ok(postcondition):
    """Require the requested full listing quantity to be finalized."""
    if not postcondition or int(postcondition.get("fulfilledRows") or 0) <= 0:
        return False
    return not postcondition.get("activeOrderExists")


def execute_purchase(conn, order, buyer_controller_id):
    quantity = purchase_quantity(order)
    total_price = purchase_total_price(order)
    max_orders_per_player = int(env("DUNE_ARTIFICIAL_EXCHANGE_MAX_ORDERS_PER_PLAYER", "50"))
    log_event(
        "purchase-attempt",
        orderId=order["id"],
        templateId=order["template_id"],
        sellerId=order["owner_id"],
        unitPrice=order["item_price"],
        quantity=quantity,
        totalPrice=total_price,
        buyerControllerId=buyer_controller_id,
        revision=order["revision"],
    )
    signatures = fulfill_signature(conn)
    supported = [args for args in signatures if "in_order_id bigint" in args and "in_order_revision bigint" in args]
    if not supported:
        raise RuntimeError(f"unsupported fulfill function signature(s): {signatures}")
    purge_time = int(time.time()) + int(env("DUNE_ARTIFICIAL_EXCHANGE_PURGE_SECONDS", "2419200"))
    with conn.cursor() as cur:
        cur.execute(
            """
            select (dune.dune_exchange_fulfill_sell_order(
                in_exchange_id => %s,
                in_max_orders_per_player => %s,
                in_purchased_completion_type => %s,
                in_sold_completion_type => %s,
                in_instigator_id => %s,
                in_order_id => %s,
                in_order_revision => %s,
                in_dst_inventory_id => null,
                in_dst_index => 0,
                in_count => %s,
                in_solaris_fee => 0,
                in_purge_time => %s
            )).*
            """,
            (
                order["exchange_id"],
                max_orders_per_player,
                int(env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASED_COMPLETION_TYPE", "0")),
                int(env("DUNE_ARTIFICIAL_EXCHANGE_SOLD_COMPLETION_TYPE", "1")),
                buyer_controller_id,
                order["id"],
                order["revision"],
                quantity,
                purge_time,
            ),
        )
        result = cur.fetchone()
    result = dict(result) if result else {}
    postcondition = purchase_postcondition(conn, order["id"])
    result["postcondition"] = postcondition
    result["maxOrdersPerPlayer"] = max_orders_per_player
    result["ok"] = purchase_postcondition_ok(postcondition)
    if not result["ok"]:
        if int(result.get("order_slots_used") or 0) >= max_orders_per_player:
            result["reason"] = "native fulfill blocked by order-slot limit"
        else:
            result["reason"] = "native fulfill did not finalize order"
        log_event("purchase-postcondition-failed", orderId=order["id"], result=result)
    log_event("purchase-result", orderId=order["id"], result=result)
    return result


def purchase_result_ok(result):
    return bool(result and result.get("ok"))


def purchase_quantity(order):
    for key in ("stack_size", "initial_stack_size"):
        value = order.get(key)
        if value not in (None, ""):
            return max(1, int(value))
    return 1


def purchase_total_price(order):
    return int(order.get("item_price") or 0) * purchase_quantity(order)


def render_purchase_notice(order):
    template = env(
        "DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_TEMPLATE",
        "Your Exchange listing was purchased: {count}x {template_id} for {price} Solari. The Solari will be in your bank after your next relog.",
    )
    return template.format(
        order_id=order.get("id"),
        template_id=order.get("template_id", "item"),
        item=order.get("template_id", "item"),
        count=purchase_quantity(order),
        price=purchase_total_price(order),
        total_price=purchase_total_price(order),
        unit_price=int(order.get("item_price") or 0),
        seller=order.get("seller_character_name") or "seller",
        server_name=env("DUNE_SERVER_DISPLAY_NAME", env("WORLD_NAME", "this server")),
    )


def notify_purchase_seller(order, message=None):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_ENABLED", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}
    if str(order.get("seller_online_status") or "").lower() != "online":
        return {"ok": True, "skipped": True, "reason": "seller offline"}
    fls_id = str(order.get("seller_fls_id") or "").strip()
    seller_name = str(order.get("seller_character_name") or order.get("owner_id") or "").strip()
    route = whisper_route_for_fls_id(fls_id)
    if not route["ok"]:
        return {"ok": False, "error": route["error"], "seller": seller_name, "orderId": order.get("id")}
    command = env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_COMMAND", env("DUNE_ADMIN_ANNOUNCE_COMMAND", str(ROOT / "scripts" / "announce.sh")))
    if command.startswith("/workspace/"):
        command = str(ROOT / command.removeprefix("/workspace/"))
    notice = message or render_purchase_notice(order)
    timeout = int(env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_TIMEOUT_SECONDS", env("DUNE_ADMIN_ANNOUNCEMENT_COMMAND_TIMEOUT_SECONDS", "45")))
    child_env = os.environ.copy()
    child_env.update(FILE_ENV)
    child_env["DUNE_ANNOUNCE_MESSAGE"] = notice
    child_env["DUNE_ANNOUNCE_JOB_ID"] = "artificial-exchange-purchase-notice"
    child_env["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"] = "true"
    child_env["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"] = "false"
    child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"] = env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_EXCHANGE", "chat.whispers")
    child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"] = env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_CHANNEL", "Whispers")
    child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = seller_name
    child_env["DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS"] = route["routingKey"]
    child_env["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"] = route["queue"]
    child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"] = env("DUNE_ARTIFICIAL_EXCHANGE_PURCHASE_NOTIFY_ROUTING_KEY", route["routingKey"]) or route["routingKey"]
    child_env["DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES"] = "false"
    child_env["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"] = "true"
    result = subprocess.run([command, notice], cwd=ROOT, env=child_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "seller": seller_name,
        "targetFlsId": route["routingKey"],
        "targetQueue": route["queue"],
        "channel": child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"],
        "message": notice,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }


def inspect_settlement(conn, limit):
    rows = settlement_report(conn, limit)
    for row in rows:
        audit({"event": "completed-order-observed", "row": row})
    return rows


def print_settlement_report(args):
    conn = connect_db()
    with conn:
        rows = settlement_report(conn, args.settlement_limit)
    conn.close()
    unsafe = [row for row in rows if not row["claimSafe"] and row["status"].startswith("unsafe")]
    return {
        "ok": True,
        "dryRun": True,
        "autoClaimEnabled": env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED", False),
        "claimable": sum(1 for row in rows if row["claimSafe"]),
        "unsafe": len(unsafe),
        "rows": rows,
    }


def execute_direct_seller_claim(cur, row):
    expected = int(row["expected_solari"])
    cur.execute(
        """
        select
            o.id as order_id,
            o.owner_id,
            o.item_id,
            o.item_price,
            f.completion_type,
            f.stack_size,
            f.original_order_id
        from dune.dune_exchange_orders o
        join dune.dune_exchange_fulfilled_orders f on f.order_id=o.id
        where o.id=%s
        for update
        """,
        (row["order_id"],),
    )
    locked = cur.fetchone()
    if not locked:
        raise RuntimeError(f"settlement order {row['order_id']} vanished before claim")
    if int(locked["owner_id"]) != int(row["owner_id"]):
        raise RuntimeError("settlement owner changed before claim")
    if locked["item_id"] not in (None, 0, ""):
        raise RuntimeError("settlement order has item_id; refusing Solari claim")
    if int(locked["completion_type"]) != int(env("DUNE_ARTIFICIAL_EXCHANGE_SOLD_COMPLETION_TYPE", "1")):
        raise RuntimeError("settlement completion type is not a seller Solari claim")
    actual_value = int(locked["item_price"]) * int(locked["stack_size"])
    if actual_value != expected:
        raise RuntimeError(f"settlement value changed before claim: expected {expected}, got {actual_value}")
    bank_result = credit_solaris_bank(cur, row["owner_id"], expected, owner_id=row["owner_id"])
    cur.execute("delete from dune.dune_exchange_orders where id=%s", (row["order_id"],))
    cur.execute(
        """
        select exists(
            select 1
            from dune.dune_exchange_orders o
            left join dune.dune_exchange_fulfilled_orders f on f.order_id=o.id
            where o.id=%s or f.order_id=%s
        ) as still_exists
        """,
        (row["order_id"], row["order_id"]),
    )
    still_exists = bool(cur.fetchone()["still_exists"])
    return {
        "total_item_value": expected,
        "original_order_id": locked["original_order_id"],
        "before_bank_balance": bank_result["before_balance"],
        "after_bank_balance": bank_result["after_balance"],
        "credited": bank_result["credited"],
        "still_exists": still_exists,
        "method": "validated_bank_solaris_balance",
    }


def readiness_check(args):
    checks = []
    catalog = load_catalog(args.catalog)
    checks.append({
        "name": "catalog",
        "ok": bool(catalog),
        "items": len(catalog),
        "enabledItems": sum(1 for row in catalog.values() if row.get("enabled")),
        "path": str(args.catalog),
    })
    checks.append({
        "name": "buyerGate",
        "ok": env_bool("DUNE_ARTIFICIAL_EXCHANGE_ENABLED", True),
        "enabled": env_bool("DUNE_ARTIFICIAL_EXCHANGE_ENABLED", True),
        "dryRun": env_bool("DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN", True),
        "purchasesEnabled": env_bool("DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED", False),
    })
    checks.append({
        "name": "settlementGate",
        "ok": True,
        "autoClaimEnabled": env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED", False),
        "autoClaimAfterScan": env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN", False),
    })
    checks.append({
        "name": "populatorGate",
        "ok": True,
        "enabled": env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False),
        "dryRun": env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN", True),
        "ownerId": int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID", "0") or "0"),
        "sourceInventoryId": int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_INVENTORY_ID", "0") or "0"),
    })
    try:
        conn = connect_db()
        with conn:
            orders = fetch_orders(conn, args.exchange_id, min(args.limit, 20))
            settlements = settlement_report(conn, args.settlement_limit)
            buyer_balance = read_exchange_balance(conn, args.buyer_controller_id) if args.buyer_controller_id > 0 else None
            buyer_used_slots = None
            buyer_max_orders = int(env("DUNE_ARTIFICIAL_EXCHANGE_MAX_ORDERS_PER_PLAYER", "50"))
            if args.buyer_controller_id > 0:
                with conn.cursor() as cur:
                    cur.execute("select dune.get_dune_exchange_used_order_slots(%s) as used_slots", (args.buyer_controller_id,))
                    row = cur.fetchone() or {}
                buyer_used_slots = int(row.get("used_slots") or 0)
            purchases_enabled = env_bool("DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED", False) and not env_bool("DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN", True)
            identity_conflict = buyer_identity_conflict(args)
            checks.append({
                "name": "buyerIdentity",
                "ok": not identity_conflict,
                "buyerControllerId": args.buyer_controller_id,
                "populatorOwnerIds": sorted(populator_owner_ids()),
                "reason": "buyer controller must be separate from populator owner" if identity_conflict else None,
            })
            checks.append({
                "name": "buyerOrderCapacity",
                "ok": not purchases_enabled or args.buyer_controller_id <= 0 or (buyer_used_slots is not None and buyer_used_slots < buyer_max_orders),
                "buyerControllerId": args.buyer_controller_id,
                "usedOrderSlots": buyer_used_slots,
                "maxOrdersPerPlayer": buyer_max_orders,
                "requiredForApply": purchases_enabled,
            })
            if env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_ENABLED", False) or args.populator_owner_id > 0 or args.populator_source_inventory_id > 0:
                eligible = populator_eligible_rows(catalog)
                preflight = populator_preflight(conn, args, catalog, eligible)
                checks.append({"name": "populatorSafety", **preflight})
        conn.close()
        checks.append({"name": "database", "ok": True, "exchangeId": args.exchange_id, "ordersSeen": len(orders), "settlementsSeen": len(settlements)})
        checks.append({"name": "settlementState", "ok": True, "claimable": sum(1 for row in settlements if row["claimSafe"]), "unsafe": sum(1 for row in settlements if row["status"].startswith("unsafe"))})
        purchase_enabled = env_bool("DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED", False) and not env_bool("DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN", True)
        checks.append({
            "name": "buyerFunding",
            "ok": (not purchase_enabled) or (args.buyer_controller_id > 0 and buyer_balance is not None and buyer_balance > 0),
            "buyerControllerId": args.buyer_controller_id,
            "exchangeBalance": buyer_balance,
            "requiredForApply": purchase_enabled,
        })
    except Exception as exc:
        checks.append({"name": "database", "ok": False, "error": str(exc)})
    ok = all(check["ok"] for check in checks if check["name"] not in ("buyerGate",))
    return {"ok": ok, "checks": checks}


def claim_settlement_once(args):
    log_event("settlement-claim-start", orderId=args.claim_settlement)
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED is false")
    if args.confirm != CLAIM_CONFIRM:
        raise RuntimeError(f"confirmation phrase required: {CLAIM_CONFIRM}")
    state = load_state()
    conn = connect_db()
    conn.autocommit = False
    try:
        row = settlement_row(conn, args.claim_settlement)
        if not row:
            raise RuntimeError(f"settlement order {args.claim_settlement} was not found")
        key = settlement_claim_key(row)
        if key in state["claimed_settlements"]:
            conn.rollback()
            return {"ok": True, "dryRun": True, "alreadyClaimed": True, "row": row}
        if not settlement_claim_safe(row):
            audit({"event": "settlement-claim-preflight-repair", "key": key, "row": row})
        with conn.cursor() as cur:
            result = execute_direct_seller_claim(cur, row)
            before_bank_balance = result["before_bank_balance"]
            after_bank_balance = result["after_bank_balance"]
            still_exists = result["still_exists"]
        expected = int(row["expected_solari"])
        credited = after_bank_balance - before_bank_balance if after_bank_balance is not None and before_bank_balance is not None else None
        valid = (
            result.get("total_item_value") is not None
            and int(result["total_item_value"]) == expected
            and int(result.get("original_order_id") or 0) == int(row["original_order_id"] or 0)
            and credited == expected
            and not still_exists
        )
        if not valid:
            conn.rollback()
            audit({
                "event": "settlement-claim-rolled-back",
                "key": key,
                "row": row,
                "result": result,
                "beforeBankBalance": before_bank_balance,
                "afterBankBalance": after_bank_balance,
                "credited": credited,
                "expected": expected,
                "stillExists": still_exists,
            })
            return {
                "ok": False,
                "dryRun": False,
                "rolledBack": True,
                "reason": "native claim failed validation",
                "row": row,
                "result": result,
                "beforeBankBalance": before_bank_balance,
                "afterBankBalance": after_bank_balance,
                "credited": credited,
                "expected": expected,
                "stillExists": still_exists,
            }
        conn.commit()
        state["claimed_settlements"].append(key)
        audit({"event": "settlement-claimed", "key": key, "row": row, "result": result, "beforeBankBalance": before_bank_balance, "afterBankBalance": after_bank_balance})
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    save_json(STATE_PATH, state)
    response = {"ok": True, "dryRun": False, "claimed": key, "row": row, "result": result, "beforeBankBalance": before_bank_balance, "afterBankBalance": after_bank_balance}
    log_event("settlement-claim-complete", orderId=args.claim_settlement, credited=result.get("credited"), beforeBankBalance=before_bank_balance, afterBankBalance=after_bank_balance)
    return response


def claim_all_settlements(args, *, require_confirm=True, dry_run=None):
    if dry_run is None:
        dry_run = bool(getattr(args, "dry_run", False))
    log_event("settlement-auto-claim-start", settlementLimit=args.settlement_limit, requireConfirm=require_confirm, dryRun=dry_run)
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED is false")
    if require_confirm and args.confirm != CLAIM_CONFIRM:
        raise RuntimeError(f"confirmation phrase required: {CLAIM_CONFIRM}")
    state = load_state()
    conn = connect_db()
    claimed = []
    skipped = []
    sold_completion_type = int(env("DUNE_ARTIFICIAL_EXCHANGE_SOLD_COMPLETION_TYPE", "1"))
    try:
        rows = settlement_report(conn, args.settlement_limit)
        for row in rows:
            key = settlement_claim_key(row)
            if key in state["claimed_settlements"]:
                skipped.append({"orderId": row["order_id"], "reason": "already claimed"})
                continue
            if int(row.get("completion_type") or -1) != sold_completion_type or row.get("item_id") not in (None, 0, ""):
                skipped.append({"orderId": row["order_id"], "reason": row["status"]})
                continue
            with conn.cursor() as cur:
                result = execute_direct_seller_claim(cur, row)
            expected = int(row["expected_solari"])
            valid = (
                int(result["total_item_value"]) == expected
                and int(result.get("original_order_id") or 0) == int(row["original_order_id"] or 0)
                and result["credited"] == expected
                and not result["still_exists"]
            )
            if not valid:
                conn.rollback()
                audit({"event": "settlement-auto-claim-rolled-back", "key": key, "row": row, "result": result})
                skipped.append({"orderId": row["order_id"], "reason": "claim failed validation", "result": result})
                continue
            event = {"orderId": row["order_id"], "key": key, "row": row, "result": result}
            if dry_run:
                conn.rollback()
                event["dryRun"] = True
                claimed.append(event)
                audit({"event": "settlement-auto-claim-dry-run", **event})
            else:
                conn.commit()
                state["claimed_settlements"].append(key)
                claimed.append(event)
                audit({"event": "settlement-auto-claimed", **event})
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    if not dry_run:
        save_json(STATE_PATH, state)
    result = {"ok": True, "dryRun": dry_run, "claimed": claimed, "skipped": skipped}
    log_event("settlement-auto-claim-complete", claimed=len(claimed), skipped=len(skipped))
    return result


def fund_buyer(args):
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_FUNDING_ENABLED", False):
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_FUNDING_ENABLED is false")
    if args.confirm != FUND_CONFIRM:
        raise RuntimeError(f"confirmation phrase required: {FUND_CONFIRM}")
    if args.buyer_controller_id <= 0:
        raise RuntimeError("--buyer-controller-id is required")
    if args.fund_buyer <= 0:
        raise RuntimeError("--fund-buyer amount must be positive")
    conn = connect_db()
    with conn:
        before = read_exchange_balance(conn, args.buyer_controller_id)
        with conn.cursor() as cur:
            cur.execute(
                "select dune.dune_exchange_modify_user_solari_balance(%s,%s)",
                (args.buyer_controller_id, args.fund_buyer),
            )
        after = read_exchange_balance(conn, args.buyer_controller_id)
    conn.close()
    result = {
        "ok": True,
        "buyerControllerId": args.buyer_controller_id,
        "delta": args.fund_buyer,
        "beforeBalance": before,
        "afterBalance": after,
    }
    audit({"event": "buyer-funded", **result})
    return result


def scan_once(args):
    started = time.time()
    log_event(
        "scan-start",
        dryRun=args.dry_run,
        exchangeId=args.exchange_id,
        limit=args.limit,
        autoClaimAfterScan=args.auto_claim_after_scan,
    )
    if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_ENABLED", True) and not args.ignore_enabled_gate:
        raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_ENABLED is false")
    if not args.dry_run and buyer_identity_conflict(args):
        raise RuntimeError("buyer controller must be separate from populator owner; native Exchange counts seeded orders against the buyer order-slot limit")
    catalog = load_catalog(args.catalog)
    state = load_state()
    conn = connect_db()
    selected = []
    skipped = []
    auto_claim = None
    with conn:
        completed = inspect_settlement(conn, args.settlement_limit)
        if args.auto_claim_after_scan and env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_ENABLED", False):
            auto_claim = claim_all_settlements(args, require_confirm=False, dry_run=args.dry_run)
            if not args.dry_run:
                state["claimed_settlements"] = load_state().get("claimed_settlements", [])
        orders = fetch_orders(conn, args.exchange_id, args.limit)
        for order in orders:
            skip_reason = buyer_skip_reason(order, args)
            if skip_reason:
                skipped.append({"orderId": order["id"], "templateId": order["template_id"], "reason": skip_reason})
                continue
            row = catalog.get(order["template_id"])
            if not row:
                skipped.append({"orderId": order["id"], "reason": "template not in catalog"})
                continue
            if not row.get("enabled"):
                skipped.append({"orderId": order["id"], "templateId": order["template_id"], "reason": "template disabled"})
                continue
            if str(order["owner_id"]) in blocked_sellers():
                skipped.append({"orderId": order["id"], "reason": "seller blocked"})
                continue
            ok, reason = spend_available(state, order, row)
            if not ok:
                skipped.append({"orderId": order["id"], "templateId": order["template_id"], "reason": reason})
                continue
            probability = buy_probability(str(row.get("liquidity_tier", "low")))
            if random.random() > probability:
                skipped.append({"orderId": order["id"], "templateId": order["template_id"], "reason": "probability skip", "probability": probability})
                continue
            if not revision_matches(conn, order):
                skipped.append({"orderId": order["id"], "templateId": order["template_id"], "reason": "stale revision"})
                continue
            decision = {
                "orderId": order["id"],
                "templateId": order["template_id"],
                "sellerId": order["owner_id"],
                "unitPrice": int(order["item_price"]),
                "quantity": purchase_quantity(order),
                "totalPrice": purchase_total_price(order),
            }
            if args.dry_run:
                decision["dryRun"] = True
            else:
                if not env_bool("DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED", False):
                    raise RuntimeError("DUNE_ARTIFICIAL_EXCHANGE_PURCHASES_ENABLED is false")
                if args.confirm != CONFIRM:
                    raise RuntimeError(f"confirmation phrase required: {CONFIRM}")
                result = execute_purchase(conn, order, args.buyer_controller_id)
                if not purchase_result_ok(result):
                    reason = (result or {}).get("reason") or "purchase failed"
                    skipped.append({"orderId": order["id"], "templateId": order["template_id"], "reason": reason, "result": result})
                    audit({"event": "purchase-rejected", "orderId": order["id"], "templateId": order["template_id"], "sellerId": order["owner_id"], "price": order["item_price"], "reason": reason, "result": result})
                    continue
                decision["dryRun"] = False
                decision["result"] = result
                record_spend(state, order)
                if result:
                    try:
                        notice = notify_purchase_seller(order)
                    except Exception as exc:
                        notice = {"ok": False, "error": str(exc), "exceptionType": type(exc).__name__}
                    decision["sellerNotification"] = notice
                    audit({"event": "purchase-seller-notification", "orderId": order.get("id"), "sellerId": order.get("owner_id"), "notice": notice})
            selected.append(decision)
            audit({"event": "purchase-selected", **decision})
    conn.close()
    save_json(STATE_PATH, state)
    result = {"ok": True, "dryRun": args.dry_run, "selected": selected, "skipped": skipped[: args.report_skips], "completedObserved": len(completed), "autoClaim": auto_claim}
    log_event(
        "scan-complete",
        dryRun=args.dry_run,
        selected=len(selected),
        skipped=len(skipped),
        completedObserved=len(completed),
        autoClaimed=len((auto_claim or {}).get("claimed", [])) if isinstance(auto_claim, dict) else 0,
        durationMs=int((time.time() - started) * 1000),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Conservative artificial buyer for player Exchange sell orders.")
    parser.add_argument("--catalog", type=pathlib.Path, default=CATALOG_PATH)
    parser.add_argument("--exchange-id", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_ID", "2")))
    parser.add_argument("--buyer-controller-id", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_BUYER_CONTROLLER_ID", "0")))
    parser.add_argument("--limit", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_LIMIT", "25000")))
    parser.add_argument("--settlement-limit", type=int, default=50)
    parser.add_argument("--report-skips", type=int, default=50)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--populate-once", action="store_true")
    parser.add_argument("--populate-categories-once", action="store_true")
    parser.add_argument("--populate-all-once", action="store_true")
    parser.add_argument("--populate-templates-once", action="store_true")
    parser.add_argument("--populate-loop", action="store_true")
    parser.add_argument("--expire-seeded", action="store_true")
    parser.add_argument("--validate-populator-once", action="store_true")
    parser.add_argument("--audit-seeded-stats", action="store_true")
    parser.add_argument("--build-stats-library", action="store_true")
    parser.add_argument("--derive-stats-library", action="store_true")
    parser.add_argument("--stats-library-report", action="store_true")
    parser.add_argument("--stats-library", type=pathlib.Path, default=STATS_LIBRARY_PATH)
    parser.add_argument("--stats-source-label", default=env("DUNE_ARTIFICIAL_EXCHANGE_STATS_SOURCE_LABEL", "local-db"))
    parser.add_argument("--stats-sample-limit", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_STATS_SAMPLE_LIMIT", "50000")))
    parser.add_argument("--stats-samples-per-template", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_STATS_SAMPLES_PER_TEMPLATE", "5")))
    parser.add_argument("--merge-stats-library", action="store_true", default=env_bool("DUNE_ARTIFICIAL_EXCHANGE_STATS_LIBRARY_MERGE", True))
    parser.add_argument("--confirm", default="")
    parser.add_argument("--ignore-enabled-gate", action="store_true")
    parser.add_argument("--include-npc-test-orders", action="store_true")
    parser.add_argument("--settlement-report", action="store_true")
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--claim-settlement", type=int)
    parser.add_argument("--claim-all-settlements", action="store_true")
    parser.add_argument("--fund-buyer", type=int, default=0)
    parser.add_argument("--auto-claim-after-scan", action="store_true", default=env_bool("DUNE_ARTIFICIAL_EXCHANGE_AUTO_CLAIM_AFTER_SCAN", False))
    parser.add_argument("--populator-owner-id", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_OWNER_ID", "0") or "0"))
    parser.add_argument("--populator-source-inventory-id", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_SOURCE_INVENTORY_ID", "0") or "0"))
    parser.add_argument("--populator-access-point-id", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_ACCESS_POINT_ID", "1")))
    parser.add_argument("--populator-target-min-orders", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MIN_ORDERS", "4000")))
    parser.add_argument("--populator-target-max-orders", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_TARGET_MAX_ORDERS", "20000")))
    parser.add_argument("--populator-price-jitter-pct", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_PRICE_JITTER_PCT", "20")))
    parser.add_argument("--populator-expiry-min-seconds", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRY_MIN_SECONDS", "3600")))
    parser.add_argument("--populator-expiry-max-seconds", type=int, default=int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRY_MAX_SECONDS", "86400")))
    parser.add_argument("--populator-expire-probability", type=float, default=float(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_EXPIRE_PROBABILITY", "0.10")))
    parser.add_argument("--populator-force-count", type=int)
    parser.add_argument("--populator-force-price", type=int)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    parser.add_argument("--apply", dest="dry_run", action="store_false")
    args = parser.parse_args()
    if args.stats_library:
        FILE_ENV["DUNE_ARTIFICIAL_EXCHANGE_STATS_LIBRARY"] = str(args.stats_library)
        STATS_LIBRARY_CACHE.clear()
    populator_mode = (
        args.populate_once
        or args.populate_categories_once
        or args.populate_all_once
        or args.populate_templates_once
        or args.populate_loop
        or args.validate_populator_once
        or args.expire_seeded
    )
    if args.dry_run is None:
        if populator_mode:
            args.dry_run = env_bool("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_DRY_RUN", True)
        else:
            args.dry_run = env_bool("DUNE_ARTIFICIAL_EXCHANGE_DRY_RUN", True)
    # A long-running buyer service is an explicitly gated apply surface. Give
    # it the same confirmation used by a one-shot apply; otherwise the first
    # eligible listing crashes the service before native fulfill is called.
    apply_service_confirmation(args)
    if populator_mode and not args.confirm:
        args.confirm = env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_CONFIRM", "")
    log_event(
        "bot-start",
        argv=sys.argv[1:],
        dryRun=args.dry_run,
        loop=args.loop,
        populateLoop=args.populate_loop,
        exchangeId=args.exchange_id,
    )
    if args.settlement_report:
        print(json.dumps(print_settlement_report(args), indent=2, default=str))
        return
    if args.audit_seeded_stats:
        catalog = load_catalog(args.catalog)
        conn = connect_db()
        try:
            print(json.dumps(audit_seeded_stats(conn, args, catalog), indent=2, default=str))
        finally:
            conn.close()
        return
    if args.build_stats_library:
        catalog = load_catalog(args.catalog)
        conn = connect_db()
        try:
            print(json.dumps(build_stats_library(conn, args, catalog), indent=2, default=str))
        finally:
            conn.close()
        return
    if args.derive_stats_library:
        catalog = load_catalog(args.catalog)
        print(json.dumps(derive_stats_library(args, catalog), indent=2, default=str))
        return
    if args.stats_library_report:
        catalog = load_catalog(args.catalog)
        print(json.dumps(stats_library_report(args, catalog), indent=2, default=str))
        return
    if args.check_ready:
        result = readiness_check(args)
        print(json.dumps(result, indent=2, default=str))
        if not result["ok"]:
            raise SystemExit(1)
        return
    if args.claim_settlement is not None:
        print(json.dumps(claim_settlement_once(args), indent=2, default=str))
        return
    if args.claim_all_settlements:
        print(json.dumps(claim_all_settlements(args), indent=2, default=str))
        return
    if args.validate_populator_once:
        print(json.dumps(validate_populator_once(args), indent=2, default=str))
        return
    if args.populate_categories_once:
        print(json.dumps(populate_categories_once(args), indent=2, default=str))
        return
    if args.populate_all_once:
        print(json.dumps(populate_all_once(args), indent=2, default=str))
        return
    if args.populate_templates_once:
        print(json.dumps(populate_templates_once(args), indent=2, default=str))
        return
    if args.expire_seeded and not (args.populate_once or args.populate_loop):
        print(json.dumps(expire_seeded_once(args), indent=2, default=str))
        return
    if args.loop and not args.dry_run and args.buyer_controller_id <= 0:
        raise RuntimeError("--buyer-controller-id is required for buyer apply mode")
    if args.populate_loop and args.loop:
        iteration = 0
        while True:
            iteration += 1
            try:
                log_event("loop-iteration-start", mode="combined", iteration=iteration)
                result = {"populate": populate_once(args), "buyer": scan_once(args)}
                print(json.dumps({"event": "loop-result", "mode": "combined", "iteration": iteration, "result": result}, sort_keys=True, default=str), flush=True)
            except Exception as exc:
                log_failure("loop-iteration-failed", exc, mode="combined", iteration=iteration)
                raise
            sleep_min = int(env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MIN_SECONDS", "180"))
            sleep_max = int(env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MAX_SECONDS", "420"))
            sleep_seconds = random.randint(sleep_min, max(sleep_min, sleep_max))
            log_event("loop-sleep", mode="combined", iteration=iteration, seconds=sleep_seconds)
            time.sleep(sleep_seconds)
        return
    if args.populate_once or args.populate_loop:
        iteration = 0
        while True:
            iteration += 1
            try:
                log_event("loop-iteration-start", mode="populator", iteration=iteration)
                result = populate_once(args)
                print(json.dumps({"event": "loop-result", "mode": "populator", "iteration": iteration, "result": result}, sort_keys=True, default=str), flush=True)
            except Exception as exc:
                log_failure("loop-iteration-failed", exc, mode="populator", iteration=iteration)
                raise
            if not args.populate_loop:
                break
            sleep_min = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_INTERVAL_MIN_SECONDS", env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MIN_SECONDS", "180")))
            sleep_max = int(env("DUNE_ARTIFICIAL_EXCHANGE_POPULATOR_INTERVAL_MAX_SECONDS", env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MAX_SECONDS", "420")))
            sleep_seconds = random.randint(sleep_min, max(sleep_min, sleep_max))
            log_event("loop-sleep", mode="populator", iteration=iteration, seconds=sleep_seconds)
            time.sleep(sleep_seconds)
        return
    if args.fund_buyer:
        print(json.dumps(fund_buyer(args), indent=2, default=str))
        return
    if not args.dry_run and args.buyer_controller_id <= 0:
        raise RuntimeError("--buyer-controller-id is required for apply mode")
    iteration = 0
    while True:
        iteration += 1
        try:
            log_event("loop-iteration-start", mode="buyer", iteration=iteration)
            result = scan_once(args)
            print(json.dumps({"event": "loop-result", "mode": "buyer", "iteration": iteration, "result": result}, sort_keys=True, default=str), flush=True)
        except Exception as exc:
            log_failure("loop-iteration-failed", exc, mode="buyer", iteration=iteration)
            raise
        if not args.loop:
            break
        sleep_min = int(env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MIN_SECONDS", "180"))
        sleep_max = int(env("DUNE_ARTIFICIAL_EXCHANGE_SCAN_INTERVAL_MAX_SECONDS", "420"))
        sleep_seconds = random.randint(sleep_min, max(sleep_min, sleep_max))
        log_event("loop-sleep", mode="buyer", iteration=iteration, seconds=sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_failure("bot-fatal", exc)
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
