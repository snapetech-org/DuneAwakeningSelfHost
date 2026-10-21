#!/usr/bin/env python3
import argparse
import collections
import csv
import difflib
import importlib.util
import inspect
import http.client
import json
import os
import pathlib
import re
import shlex
import ssl
import subprocess
import sys
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "vendor"))

import pika
import psycopg2
import psycopg2.extras
from secret_env import read_secret_env

from dune_gm_command import build_envelope, publish_command, publish_command_management
from dune_whisper_route import whisper_route_for_fls_id

GM_CATALOG_PATH = pathlib.Path(__file__).with_name("gm-command-catalog.py")
GM_CATALOG_SPEC = importlib.util.spec_from_file_location("gm_command_catalog", GM_CATALOG_PATH)
gm_command_catalog = importlib.util.module_from_spec(GM_CATALOG_SPEC)
GM_CATALOG_SPEC.loader.exec_module(gm_command_catalog)

ROOT = pathlib.Path(__file__).resolve().parents[1]
GM_LOCATION_FILE = ROOT / "backups" / "admin-panel" / "gm-locations.json"
TELEPORT_SLOT_FILE = ROOT / "backups" / "admin-panel" / "teleport-slots.json"
ONLINE_GM_TELEPORT_ARM_FILE = ROOT / "backups" / "admin-panel" / "online-gm-teleport-arm.json"
DD1_RESTORE_AUDIT_DIR = ROOT / "backups" / "admin-chat-dd1-restore"
ITEM_CATALOG = ROOT / "config" / "artificial-exchange-prices.csv"
SPAM_STATE = {}
AUCTION_CONFIRMATIONS = {}
DD1_RESTORE_CONFIRMATIONS = {}
LAST_ANNOUNCE_RESULT = None
ITEM_DISPLAY_NAMES = None


def env(name, default=""):
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    return default


def secret_env(name, default=""):
    if os.environ.get(f"{name}_FILE", "").strip():
        return read_secret_env(name, default)
    value = FILE_ENV.get(name)
    if value is not None and value != "":
        return value
    return read_secret_env(name, default)


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def env_chat_or_announce(chat_name, announce_name, default=""):
    value = secret_env(chat_name, "")
    if value and value != "dash-admin-test":
        return value
    value = secret_env(announce_name, default)
    return "" if value == "dash-admin-test" else value


def split_csv(value):
    out = []
    for item in value.split(","):
        item = item.strip()
        if item:
            out.append(item)
    return out


def split_routing_keys(value):
    out = []
    for item in value.split(","):
        item = item.strip()
        if item in ("<empty>", "empty", "EMPTY"):
            item = ""
        if item or item == "":
            out.append(item)
    return out


def item_display_names():
    global ITEM_DISPLAY_NAMES
    if ITEM_DISPLAY_NAMES is not None:
        return ITEM_DISPLAY_NAMES
    names = {}
    try:
        with ITEM_CATALOG.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                template_id = row.get("template_id")
                display_name = row.get("display_name")
                if template_id and display_name:
                    names[template_id] = display_name
    except OSError:
        pass
    ITEM_DISPLAY_NAMES = names
    return ITEM_DISPLAY_NAMES


def auction_usage_message():
    return 'usage: &auction --item-id <item-id> <count> <price> or &auction "<item code/template>" <count> <price>; use &inv_list for item codes'


def db_default_host():
    return "postgres" if pathlib.Path("/workspace/.env").exists() else "127.0.0.1"


def db_default_port():
    return "5432" if pathlib.Path("/workspace/.env").exists() else "15431"


def db_default_name():
    return env("DUNE_GAME_DB_NAME", env("DUNE_DATABASE", env("DUNE_DB_NAME", "dune_sb_1_4_0_0")))


def connect_db():
    db_host = env("DUNE_ADMIN_DB_HOST", db_default_host())
    db_port = env("DUNE_ADMIN_DB_PORT", db_default_port())
    if not pathlib.Path("/workspace/.env").exists() and db_host in ("postgres", "admin-postgres"):
        db_host = "127.0.0.1"
        db_port = "15431"
    return psycopg2.connect(
        host=db_host,
        port=db_port,
        user=env("DUNE_ADMIN_DB_USER", "dune"),
        password=secret_env("DUNE_ADMIN_DB_PASSWORD") or secret_env("POSTGRES_DUNE_PASSWORD"),
        dbname=env("DUNE_ADMIN_DB_NAME", db_default_name()),
        connect_timeout=5,
    )


def rollback_quietly(conn):
    try:
        conn.rollback()
    except AttributeError:
        pass


def character_row(conn, name):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            with candidate as (
                select
                    ps.account_id,
                    ps.character_name,
                    ps.online_status::text as online_status,
                    ps.life_state::text as life_state,
                    ps.server_id,
                    ps.player_controller_id,
                    ps.player_pawn_id,
                    acc.user as fls_id,
                    acc.funcom_id,
                    act.map as actor_map,
                    act.partition_id,
                    act.dimension_index,
                    wp.label as partition_label,
                    wp.map as partition_map,
                    ((act.transform).location).x::float8 as x,
                    ((act.transform).location).y::float8 as y,
                    ((act.transform).location).z::float8 as z,
                    case when lower(ps.character_name) = lower(%s) then 0 else 1 end as match_rank
                from dune.player_state ps
                join dune.accounts acc on acc.id = ps.account_id
                left join dune.actors act on act.id = ps.player_pawn_id
                left join dune.world_partition wp on wp.partition_id = act.partition_id
                where lower(ps.character_name) = lower(%s)
                   or lower(ps.character_name) like lower(%s) || '%%'
            )
            select *
            from candidate
            order by match_rank, character_name
            limit 5
            """,
            (name, name, name),
        )
        rows = cur.fetchall()
    if not rows:
        return None, []
    exact = [row for row in rows if row["character_name"].lower() == name.lower()]
    if exact:
        return exact[0], rows
    if len(rows) == 1:
        return rows[0], rows
    return None, rows


def character_by_fls_id(conn, fls_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select ps.character_name, acc.user as fls_id, acc.funcom_id
            from dune.accounts acc
            left join dune.player_state ps on ps.account_id = acc.id
            where acc.user = %s
            order by ps.character_name nulls last
            limit 1
            """,
            (fls_id,),
        )
        return cur.fetchone()


def fls_id_for_sender(conn, sender_name):
    if not sender_name:
        return ""
    if not hasattr(conn, "cursor"):
        return ""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select acc.user as fls_id
                from dune.accounts acc
                left join dune.player_state ps on ps.account_id = acc.id
                where acc.user = %s
                   or lower(acc.funcom_id) = lower(%s)
                   or lower(ps.character_name) = lower(%s)
                order by case when lower(ps.character_name) = lower(%s) then 0 else 1 end,
                         ps.character_name nulls last
                limit 1
                """,
                (sender_name, sender_name, sender_name, sender_name),
            )
            row = cur.fetchone()
    except (AttributeError, TypeError):
        return ""
    return (row or {}).get("fls_id") or ""


def resolve_sender_character(conn, sender_name, sender_fls_id):
    if sender_fls_id:
        resolved = character_by_fls_id(conn, sender_fls_id)
        if resolved and resolved.get("character_name"):
            return resolved["character_name"]
    if sender_name:
        resolved = character_by_fls_id(conn, sender_name)
        if resolved and resolved.get("character_name"):
            return resolved["character_name"]
    if sender_name:
        target, _ = character_row(conn, sender_name)
        if target:
            return target["character_name"]
    return sender_name or sender_fls_id or "unknown"


def is_admin(conn, sender_name, sender_fls_id):
    names = {item.lower() for item in split_csv(env("DUNE_CHAT_COMMAND_ADMINS", "AdminUser"))}
    fls_ids = set(split_csv(env("DUNE_CHAT_COMMAND_ADMIN_FLS_IDS", "TEST_FLS_ID")))
    resolved = None
    if sender_fls_id:
        resolved = character_by_fls_id(conn, sender_fls_id)
    if not resolved and sender_name:
        resolved = character_by_fls_id(conn, sender_name)
    resolved_name = (resolved or {}).get("character_name") or sender_name or ""
    sender_ids = {item for item in (sender_fls_id, sender_name) if item}
    allowed = bool((resolved_name and resolved_name.lower() in names) or (sender_ids & fls_ids))
    return allowed, resolved_name


def compact_location(row):
    return {
        "partitionId": row["partition_id"],
        "partitionLabel": row["partition_label"],
        "map": row["actor_map"] or row["partition_map"],
        "dimensionIndex": row["dimension_index"],
        "x": row["x"],
        "y": row["y"],
        "z": row["z"],
    }


def compact_character(row):
    return {
        "accountId": row["account_id"],
        "characterName": row["character_name"],
        "flsId": row["fls_id"],
        "funcomId": row["funcom_id"],
        "onlineStatus": row["online_status"],
        "lifeState": row["life_state"],
        "serverId": row["server_id"],
        "playerControllerId": row["player_controller_id"],
        "playerPawnId": row["player_pawn_id"],
        "location": compact_location(row),
    }


def format_location(row):
    label = row["partition_label"] or row["actor_map"] or "unknown"
    if row["x"] is None:
        return f"{label} position unknown"
    return f"{label} x={row['x']:.1f} y={row['y']:.1f} z={row['z']:.1f}"


def online_player_list(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select
                coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name,
                coalesce(nullif(wp.label, ''), nullif(act.map, ''), nullif(wp.map, ''), 'Unknown') as map_label
            from dune.player_state ps
            left join dune.actors act on act.id = ps.player_pawn_id
            left join dune.world_partition wp on wp.partition_id = act.partition_id
            where ps.online_status::text = 'Online'
            order by character_name;
            """
        )
        return cur.fetchall()


def format_online_player_list(rows):
    if not rows:
        return "no players online"
    return "online players: " + ", ".join(
        f"{row.get('character_name') or 'unknown'} ({display_map_label(row.get('map_label'))})"
        for row in rows
    )


def display_map_label(map_label):
    if not map_label:
        return "Unknown"
    if map_label == "Abbir":
        return "Hagga"
    return map_label


def route_key_from_map_partition(map_name, partition_id):
    if map_name == "HaggaBasin":
        map_name = "Survival_1"
    if map_name and partition_id not in (None, ""):
        return f"{map_name}{partition_id}"
    return None


def location_from_character(row, name="location0"):
    if row is None or row.get("partition_id") is None or row.get("x") is None:
        raise ValueError("character location is unavailable")
    return {
        "name": name,
        "characterName": row.get("character_name"),
        "serverId": row.get("server_id"),
        "partitionId": row.get("partition_id"),
        "partitionLabel": row.get("partition_label"),
        "map": row.get("partition_map") or row.get("actor_map"),
        "dimensionIndex": row.get("dimension_index"),
        "x": float(row.get("x")),
        "y": float(row.get("y")),
        "z": float(row.get("z")),
        "savedAt": int(time.time()),
    }


def teleport_slot_from_character(row, slot, name, creator):
    location = location_from_character(row, name or f"slot{slot}")
    location["slot"] = slot
    location["creator"] = creator
    location["dimension"] = row.get("dimension_index")
    return location


def parse_teleport_slot(value):
    if not re.fullmatch(r"\d+", value or ""):
        raise ValueError("slot must be a non-negative integer")
    return int(value)


def read_teleport_slots():
    try:
        data = json.loads(TELEPORT_SLOT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"slots": {}}
    if not isinstance(data, dict):
        return {"slots": {}}
    slots = data.get("slots")
    if not isinstance(slots, dict):
        data["slots"] = {}
    return data


def write_teleport_slots(data):
    TELEPORT_SLOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TELEPORT_SLOT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(TELEPORT_SLOT_FILE)


def load_teleport_slot(slot):
    return read_teleport_slots().get("slots", {}).get(str(slot))


def list_teleport_slots():
    slots = read_teleport_slots().get("slots", {})
    return [
        slots[key]
        for key in sorted((key for key in slots if re.fullmatch(r"\d+", str(key))), key=lambda item: int(item))
        if isinstance(slots.get(key), dict)
    ]


def next_free_teleport_slot(slots=None):
    slots = slots if slots is not None else read_teleport_slots().get("slots", {})
    occupied = {int(key) for key in slots if re.fullmatch(r"\d+", str(key))}
    slot = 0
    while slot in occupied:
        slot += 1
    return slot


def save_teleport_slot(slot, name, row, creator, replace=False):
    data = read_teleport_slots()
    slots = data.setdefault("slots", {})
    key = str(slot)
    if key in slots and not replace:
        return None, slots[key], next_free_teleport_slot(slots)
    location = teleport_slot_from_character(row, slot, name, creator)
    slots[key] = location
    write_teleport_slots(data)
    return location, None, None


def delete_teleport_slot(slot):
    data = read_teleport_slots()
    location = data.get("slots", {}).pop(str(slot), None)
    if location:
        write_teleport_slots(data)
    return location


def read_gm_locations():
    try:
        data = json.loads(GM_LOCATION_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"locations": {}}
    if not isinstance(data, dict):
        return {"locations": {}}
    data.setdefault("locations", {})
    return data


def write_gm_locations(data):
    GM_LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GM_LOCATION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(GM_LOCATION_FILE)


def read_online_gm_teleport_arm():
    try:
        data = json.loads(ONLINE_GM_TELEPORT_ARM_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"arms": []}
    if not isinstance(data, dict):
        return {"arms": []}
    arms = data.get("arms")
    if not isinstance(arms, list):
        data["arms"] = []
    return data


def write_online_gm_teleport_arm(data):
    ONLINE_GM_TELEPORT_ARM_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ONLINE_GM_TELEPORT_ARM_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(ONLINE_GM_TELEPORT_ARM_FILE)


def online_gm_teleport_arm_seconds():
    return max(5, int(env("DUNE_CHAT_COMMAND_ONLINE_GM_TELEPORT_ARM_SECONDS", "60")))


def online_gm_teleport_arm_entry(action, admin_name, target_name, route, command_text):
    return {
        "action": action,
        "adminPlayer": admin_name,
        "targetPlayer": target_name,
        "route": route,
        "commandText": command_text,
        "expiresAt": int(time.time()) + online_gm_teleport_arm_seconds(),
    }


def arm_online_gm_teleport(action, admin_name, target_name, route, command_text):
    now = int(time.time())
    data = read_online_gm_teleport_arm()
    entry = online_gm_teleport_arm_entry(action, admin_name, target_name, route, command_text)
    data["arms"] = [
        arm for arm in data.get("arms", [])
        if int(arm.get("expiresAt") or 0) > now
        and not (
            arm.get("action") == action
            and arm.get("adminPlayer") == admin_name
            and arm.get("targetPlayer") == target_name
        )
    ]
    data["arms"].append(entry)
    write_online_gm_teleport_arm(data)
    return entry


def consume_online_gm_teleport_arm(action, admin_name, target_name, route, command_text):
    if not env_bool("DUNE_CHAT_COMMAND_ONLINE_GM_TELEPORT_REQUIRE_ARM", True):
        return True, "online GM teleport arming disabled"
    now = int(time.time())
    data = read_online_gm_teleport_arm()
    kept = []
    matched = None
    for arm in data.get("arms", []):
        if int(arm.get("expiresAt") or 0) <= now:
            continue
        if (
            arm.get("action") == action
            and arm.get("adminPlayer") == admin_name
            and arm.get("targetPlayer") == target_name
            and arm.get("route") == route
            and arm.get("commandText") == command_text
            and matched is None
        ):
            matched = arm
            continue
        kept.append(arm)
    if matched:
        data["arms"] = kept
        write_online_gm_teleport_arm(data)
        return True, f"armed until {matched.get('expiresAt')}"
    data["arms"] = kept
    write_online_gm_teleport_arm(data)
    return False, f"online GM teleport is not armed; run &arm{action} {target_name} first"


def save_gm_location(admin_name, location_name, row):
    data = read_gm_locations()
    locations = data.setdefault("locations", {})
    key = f"{admin_name.lower()}:{location_name.lower()}"
    location = location_from_character(row, location_name)
    locations[key] = location
    write_gm_locations(data)
    return location


def load_gm_location(admin_name, location_name):
    key = f"{admin_name.lower()}:{location_name.lower()}"
    return read_gm_locations().get("locations", {}).get(key)


def list_gm_locations(admin_name):
    prefix = f"{admin_name.lower()}:"
    locations = []
    for key, location in read_gm_locations().get("locations", {}).items():
        if key.startswith(prefix):
            locations.append(location)
    return sorted(locations, key=lambda item: item.get("name", "").lower())


def delete_gm_location(admin_name, location_name):
    data = read_gm_locations()
    key = f"{admin_name.lower()}:{location_name.lower()}"
    location = data.get("locations", {}).pop(key, None)
    if location:
        write_gm_locations(data)
    return location


def format_saved_location(location):
    if not location:
        return "location not found"
    label = location.get("partitionLabel") or location.get("map") or "unknown"
    return f"{location.get('name', 'location')} {label} x={location['x']:.1f} y={location['y']:.1f} z={location['z']:.1f}"


def format_teleport_slot(location):
    if not location:
        return "slot not found"
    label = location.get("partitionLabel") or location.get("map") or "unknown"
    name = location.get("name") or f"slot{location.get('slot')}"
    return f"slot {location.get('slot')}: {name} {label} x={location['x']:.1f} y={location['y']:.1f} z={location['z']:.1f}"


def format_teleport_slot_collision(location):
    if not location:
        return "slot not found"
    label = location.get("partitionLabel") or location.get("map") or "unknown"
    name = location.get("name") or f"slot{location.get('slot')}"
    return f"slot {location.get('slot')} exists: {name} {label} x={location['x']:.1f} y={location['y']:.1f} z={location['z']:.1f}"


def gm_route_for(conn, row):
    route_key = route_key_from_map_partition(row.get("partition_map"), row.get("partition_id"))
    if route_key:
        return route_key
    map_name = row.get("partition_map") or row.get("actor_map")
    if map_name:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select server_id
                from dune.farm_state
                where map = %s and server_id is not null
                order by ready desc, alive desc, server_id
                limit 1
                """,
                (map_name,),
            )
            farm = cur.fetchone()
        if farm and farm.get("server_id"):
            return farm["server_id"]
    return env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")


def gm_route_for_saved_location(conn, location):
    route_key = route_key_from_map_partition(location.get("map"), location.get("partitionId"))
    if route_key:
        return route_key
    return env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")


def gm_command_preview(command_text, target_player, admin_player, route):
    mode = env("DUNE_GM_COMMAND_ENVELOPE_MODE", "service-message")
    return {
        "route": route,
        "mode": mode,
        "commandText": command_text,
        "targetPlayer": target_player,
        "adminPlayer": admin_player,
        "envelope": build_envelope(mode, command_text, target_player=target_player, admin_player=admin_player),
    }


def gm_execution_allowed():
    return (
        env_bool("DUNE_ADMIN_GM_COMMANDS_ENABLED", False)
        and env_bool("DUNE_GM_COMMAND_PAYLOAD_VERIFIED", False)
        and env_bool("DUNE_CHAT_COMMAND_EXECUTE_ONLINE_GM_TELEPORT", False)
    )


def online_gm_teleport_safety(conn, action, admin, target):
    if admin is None:
        return False, "admin player not found", None, None
    if (admin.get("online_status") or "").lower() != "online":
        return False, f"admin {admin.get('character_name')} is {admin.get('online_status')}; online GM teleport needs the admin online", None, None
    if target is None:
        return False, "target player not found", None, None
    if (target.get("online_status") or "").lower() != "online":
        return False, f"{target.get('character_name')} is {target.get('online_status')}; online GM teleport only supports online targets", None, None
    if action in ("bring", "unstuck") and admin.get("x") is None:
        return False, f"admin location unavailable for {admin.get('character_name')}", None, None

    admin_route = gm_route_for(conn, admin)
    target_route = gm_route_for(conn, target)
    if not admin_route or not target_route:
        return False, "could not resolve admin and target GM routes", admin_route, target_route
    if env_bool("DUNE_CHAT_COMMAND_ONLINE_GM_TELEPORT_REQUIRE_SAME_ROUTE", True) and admin_route != target_route:
        return (
            False,
            f"admin route {admin_route} differs from target route {target_route}; same-route live teleport guard is enabled",
            admin_route,
            target_route,
        )
    return True, "online GM teleport safety checks passed", admin_route, target_route


def player_disconnect_allowed():
    return (
        env_bool("DUNE_ADMIN_GM_COMMANDS_ENABLED", False)
        and env_bool("DUNE_GM_COMMAND_PAYLOAD_VERIFIED", False)
        and env_bool("DUNE_CHAT_COMMAND_EXECUTE_PLAYER_DISCONNECT", False)
    )


def send_gm_command(command_text, target_player, admin_player, route):
    if not gm_execution_allowed():
        return {"ok": False, "blocked": True, "preview": gm_command_preview(command_text, target_player, admin_player, route)}
    if env("DUNE_GM_COMMAND_TRANSPORT", "amqp") == "management":
        return publish_command_management(command_text, route, target_player=target_player, admin_player=admin_player)
    return publish_command(command_text, route, target_player=target_player, admin_player=admin_player)


def send_online_gm_teleport(action, command_text, target_player, admin_player, route):
    armed, arm_reason = consume_online_gm_teleport_arm(action, admin_player, target_player, route, command_text)
    if not armed:
        return {
            "ok": False,
            "blocked": True,
            "reason": arm_reason,
            "preview": gm_command_preview(command_text, target_player, admin_player, route),
        }
    result = send_gm_command(command_text, target_player, admin_player, route)
    if isinstance(result, dict):
        result.setdefault("arm", {"ok": True, "reason": arm_reason})
    return result


def send_player_disconnect(command_text, target_player, admin_player, route):
    if not player_disconnect_allowed():
        return {"ok": False, "blocked": True, "preview": gm_command_preview(command_text, target_player, admin_player, route)}
    if env("DUNE_GM_COMMAND_TRANSPORT", "amqp") == "management":
        return publish_command_management(command_text, route, target_player=target_player, admin_player=admin_player)
    return publish_command(command_text, route, target_player=target_player, admin_player=admin_player)


def _admin_json_request(path, payload):
    """Call only the loopback Admin API; never send the owner token elsewhere."""
    try:
        port = int(env("DUNE_CHAT_COMMAND_ADMIN_PORT", env("DUNE_ADMIN_HOST_PORT", "18080")))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("DUNE_CHAT_COMMAND_ADMIN_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise RuntimeError("DUNE_CHAT_COMMAND_ADMIN_PORT must be within 1..65535")
    token = str(secret_env("DUNE_ADMIN_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("guarded chat teleport requires DUNE_ADMIN_TOKEN")
    try:
        timeout = int(env("DUNE_CHAT_COMMAND_ADMIN_TIMEOUT_SECONDS", "180"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("DUNE_CHAT_COMMAND_ADMIN_TIMEOUT_SECONDS must be an integer") from exc
    timeout = max(5, min(timeout, 300))
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "X-Admin-Token": token,
        })
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("Admin teleport response exceeded 1 MiB")
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Admin teleport API returned invalid JSON with HTTP {response.status}") from exc
        if response.status < 200 or response.status >= 300:
            detail = result.get("error") or result.get("message") or f"HTTP {response.status}"
            raise RuntimeError(f"guarded Admin teleport request refused: {str(detail)[:500]}")
        return result
    finally:
        connection.close()


def guarded_offline_player_teleport(account_id, partition_id, x, y, z, execute=False):
    payload = {
        "dryRun": True,
        "accountId": int(account_id),
        "partitionId": int(partition_id),
        "location": {"x": float(x), "y": float(y), "z": float(z)},
    }
    preview = _admin_json_request("/api/admin/player-recovery/offline-teleport", payload)
    if not execute:
        return preview
    if not preview.get("canExecute"):
        blockers = ((preview.get("plan") or {}).get("blockers") or ["preview is not executable"])
        raise RuntimeError("guarded offline teleport is blocked: " + "; ".join(map(str, blockers)))
    execute_payload = {
        **payload,
        "dryRun": False,
        "expectedFingerprint": preview.get("expectedFingerprint"),
        "confirm": preview.get("confirm"),
    }
    result = _admin_json_request("/api/admin/player-recovery/offline-teleport", execute_payload)
    if not (result.get("ok") and result.get("verified") and result.get("receipt")):
        detail = result.get("error") or result.get("message") or "execution did not return verified receipt evidence"
        raise RuntimeError(f"guarded offline teleport did not execute: {str(detail)[:500]}")
    return result


def chat_guarded_offline_teleport(conn, target, partition_id, x, y, z, execute=False):
    rollback_quietly(conn)
    try:
        result = guarded_offline_player_teleport(
            target["account_id"], partition_id, x, y, z, execute=execute,
        )
    except Exception as exc:
        return None, f"guarded teleport refused: {str(exc)[:400]}"
    if not execute and not result.get("canExecute"):
        blockers = ((result.get("plan") or {}).get("blockers") or ["preview is not executable"])
        return result, "guarded teleport blocked: " + "; ".join(map(str, blockers))
    return result, None


def player_disconnect_command(target_name):
    raise ValueError(
        "native targeted disconnect is rejected for this build; Ghidra/config show no shipped GM kick command"
    )


def chat_auction_enabled():
    return env_bool("DUNE_CHAT_COMMAND_AUCTION_ENABLED", False)


def chat_auction_base_storage_enabled():
    return env_bool("DUNE_CHAT_COMMAND_AUCTION_BASE_STORAGE_ENABLED", False)


def chat_exchange_cashout_enabled():
    return env_bool("DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_ENABLED", False)


def chat_dd1_backup_enabled():
    return env_bool("DUNE_CHAT_COMMAND_DD1_BRT_BACKUP_ENABLED", False)


def chat_dd1_restore_enabled():
    return env_bool("DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ENABLED", False)


def chat_dd1_restore_dry_run():
    override = env("DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_DRY_RUN", "")
    if override != "":
        return override.lower() in ("1", "true", "yes", "on")
    return env_bool("DUNE_CHAT_COMMAND_DRY_RUN", True)


def chat_dd1_restore_copy_source_enabled():
    return env_bool("DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_COPY_SOURCE_ENABLED", False)


def chat_dd1_restore_execution_allowed(player, sender_fls_id=""):
    allowed_fls_ids = split_csv(env("DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ALLOWED_FLS_IDS", ""))
    allowed_player_ids = split_csv(env("DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ALLOWED_PLAYER_IDS", ""))
    if "*" in allowed_fls_ids or "*" in allowed_player_ids:
        return True
    if not allowed_fls_ids and not allowed_player_ids:
        return False
    player_fls_id = player.get("fls_id") or ""
    player_id = str(player.get("player_controller_id") or "")
    return bool(
        (sender_fls_id and sender_fls_id in allowed_fls_ids)
        or (player_fls_id and player_fls_id in allowed_fls_ids)
        or (player_id and player_id in allowed_player_ids)
    )


def dd1_partition_id():
    return int(env("DUNE_CHAT_COMMAND_DD1_PARTITION_ID", env("DUNE_DD1_PARTITION_ID", "8")))


def dd1_map_names():
    names = split_csv(env("DUNE_CHAT_COMMAND_DD1_MAP_NAMES", "DeepDesert,DeepDesert_1"))
    return names or ["DeepDesert", "DeepDesert_1"]


def dd1_restore_allowed_source_maps():
    value = env("DUNE_CHAT_COMMAND_DD1_RESTORE_ALLOWED_SOURCE_MAPS", "*")
    if value.strip() == "*":
        return None
    return split_csv(value)


def dd1_restore_confirmation_ttl():
    return int(env("DUNE_CHAT_COMMAND_DD1_RESTORE_CONFIRM_SECONDS", "120"))


def dd1_restore_max_connected_players():
    return int(env("DUNE_CHAT_COMMAND_DD1_RESTORE_MAX_DD_CONNECTED_PLAYERS", "1"))


def dd1_restore_recent_backup_required():
    return env_bool("DUNE_CHAT_COMMAND_DD1_RESTORE_REQUIRE_RECENT_BACKUP", True)


def dd1_restore_backup_max_age_seconds():
    return int(env("DUNE_CHAT_COMMAND_DD1_RESTORE_BACKUP_MAX_AGE_SECONDS", "86400"))


def dd1_restore_collision_guard_enabled():
    return env_bool("DUNE_CHAT_COMMAND_DD1_RESTORE_COLLISION_GUARD_ENABLED", True)


def dd1_restore_collision_padding():
    return float(env("DUNE_CHAT_COMMAND_DD1_RESTORE_COLLISION_PADDING", "1000"))


def dd1_restore_confirmation_key(sender_name="", sender_fls_id="", resolved_name=""):
    return (sender_fls_id or resolved_name or sender_name or "unknown").lower()


def dd1_base_containment_padding():
    return float(env("DUNE_CHAT_COMMAND_DD1_BASE_CONTAINMENT_PADDING", "1500"))


def dd1_restore_offset():
    return {
        "x": float(env("DUNE_CHAT_COMMAND_DD1_RESTORE_OFFSET_X", "3000")),
        "y": float(env("DUNE_CHAT_COMMAND_DD1_RESTORE_OFFSET_Y", "0")),
        "z": float(env("DUNE_CHAT_COMMAND_DD1_RESTORE_OFFSET_Z", "0")),
    }


def normalize_item_search(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def auction_confirmation_key(sender_name="", sender_fls_id="", resolved_name=""):
    return (sender_fls_id or resolved_name or sender_name or "unknown").lower()


def auction_confirmation_ttl():
    return int(env("DUNE_CHAT_COMMAND_AUCTION_CONFIRM_SECONDS", "120"))


def parse_positive_int(value, name):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer")
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def player_inventory_ids(conn, player):
    actor_ids = [player.get("player_pawn_id"), player.get("player_controller_id")]
    actor_ids = [int(actor_id) for actor_id in actor_ids if actor_id is not None]
    if not actor_ids:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, actor_id, inventory_type, max_item_count
            from dune.inventories
            where actor_id = any(%s)
            order by
              case when actor_id=%s then 0 else 1 end,
              inventory_type nulls last,
              id
            """,
            (actor_ids, player.get("player_pawn_id")),
        )
        return cur.fetchall()


def base_storage_inventory_ids(conn, player):
    actor_ids = [player.get("player_pawn_id"), player.get("player_controller_id")]
    actor_ids = [int(actor_id) for actor_id in actor_ids if actor_id is not None]
    if not actor_ids:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            with permitted_totems as (
                select t.id as totem_id, tp.owner_entity_id
                from dune.totems t
                join dune.placeables tp on tp.id=t.id
                join dune.permission_actor_rank par on par.permission_actor_id=t.id
                where par.player_id = any(%s)
            )
            select inv.id, inv.actor_id, inv.inventory_type, inv.max_item_count,
                   a.class, pa.actor_name, pt.totem_id
            from permitted_totems pt
            join dune.placeables child on child.owner_entity_id=pt.owner_entity_id and child.id<>pt.totem_id
            join dune.actors a on a.id=child.id
            join dune.inventories inv on inv.actor_id=child.id
            left join dune.permission_actor pa on pa.actor_id=child.id
            where a.class not like '%%/Characters/Player/%%'
            order by pt.totem_id, pa.actor_name nulls last, inv.id
            """,
            (actor_ids,),
        )
        return cur.fetchall()


def auction_source_inventories(conn, player, source="personal", explicit_inventory_id=None):
    personal = player_inventory_ids(conn, player)
    if source == "personal":
        return personal
    if source in ("base", "storage"):
        if not chat_auction_base_storage_enabled():
            raise ValueError("base/storage auction source is disabled")
        return base_storage_inventory_ids(conn, player)
    if source == "inventory":
        if not chat_auction_base_storage_enabled():
            raise ValueError("explicit inventory auction source is disabled")
        base = base_storage_inventory_ids(conn, player)
        allowed = {int(row["id"]) for row in personal + base}
        inventory_id = int(explicit_inventory_id)
        if inventory_id not in allowed:
            raise ValueError(f"inventory {inventory_id} is not an allowed personal/base inventory for this player")
        return [row for row in personal + base if int(row["id"]) == inventory_id]
    raise ValueError(f"unknown auction source: {source}")


def inventory_item_rows(conn, inventory_ids, explicit_item_id=None):
    if explicit_item_id is not None:
        where_clause = "i.id=%s and i.inventory_id = any(%s)"
        params = (int(explicit_item_id), inventory_ids)
    else:
        where_clause = "i.inventory_id = any(%s)"
        params = (inventory_ids,)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            select i.id, i.inventory_id, i.stack_size, i.position_index, i.template_id,
                   i.quality_level, i.stats, inv.actor_id, inv.inventory_type
            from dune.items i
            join dune.inventories inv on inv.id = i.inventory_id
            where {where_clause}
            order by i.template_id, i.quality_level desc, i.stack_size desc, i.id
            """,
            params,
        )
        return cur.fetchall()


def player_inventory_list_rows(conn, player):
    inventories = auction_source_inventories(conn, player, source="personal")
    if not inventories:
        return []
    inventory_ids = [row["id"] for row in inventories]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select i.id, i.inventory_id, i.stack_size, i.position_index, i.template_id,
                   i.quality_level, inv.actor_id, inv.inventory_type
            from dune.items i
            join dune.inventories inv on inv.id = i.inventory_id
            where i.inventory_id = any(%s)
            order by i.inventory_id, i.position_index nulls last, i.id
            """,
            (inventory_ids,),
        )
        return cur.fetchall()


def format_inventory_item_list(rows):
    if not rows:
        return "inventory is empty"
    names = item_display_names()
    parts = []
    for row in rows:
        template_id = row.get("template_id") or "unknown"
        display_name = names.get(template_id, template_id)
        parts.append(
            f"{int(row.get('stack_size') or 0)}x {display_name} code={template_id} item-id={row.get('id')}"
        )
    return "inventory: " + "; ".join(parts)


def inventory_list_for_player(conn, player):
    rows = player_inventory_list_rows(conn, player)
    return rows, format_inventory_item_list(rows)


def parse_exchange_list_limit(args):
    default_limit = int(env("DUNE_CHAT_COMMAND_EXCHANGE_LIST_LIMIT", "25"))
    max_limit = int(env("DUNE_CHAT_COMMAND_EXCHANGE_LIST_MAX_LIMIT", "100"))
    if not args:
        return min(default_limit, max_limit)
    if len(args) > 1:
        raise ValueError("usage: &exchange_list [limit]")
    return min(parse_positive_int(args[0], "limit"), max_limit)


def exchange_fulfilled_orders_available(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("select to_regclass('dune.dune_exchange_fulfilled_orders') as rel")
        row = cur.fetchone() or {}
    return bool(row.get("rel"))


def player_exchange_list_rows(conn, player, limit):
    exchange_id = int(env("DUNE_CHAT_COMMAND_AUCTION_EXCHANGE_ID", "2"))
    owner_id = int(player["player_controller_id"])
    has_fulfilled = exchange_fulfilled_orders_available(conn)
    fulfilled_join = "left join dune.dune_exchange_fulfilled_orders f on f.order_id=o.id" if has_fulfilled else ""
    fulfilled_where = "and f.order_id is null" if has_fulfilled else ""
    base_where = f"""
        o.exchange_id=%s
        and o.owner_id=%s
        and coalesce(o.is_npc_order, false)=false
        and o.template_id is not null
        and o.item_price is not null
        {fulfilled_where}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            select count(*) as count
            from dune.dune_exchange_orders o
            join dune.dune_exchange_sell_orders s on s.order_id=o.id
            {fulfilled_join}
            where {base_where}
            """,
            (exchange_id, owner_id),
        )
        total = int((cur.fetchone() or {}).get("count") or 0)
        cur.execute(
            f"""
            select
                o.id, o.owner_id, o.template_id, o.item_price, o.expiration_time,
                coalesce(i.stack_size, s.initial_stack_size, 1) as stack_size
            from dune.dune_exchange_orders o
            join dune.dune_exchange_sell_orders s on s.order_id=o.id
            left join dune.items i on i.id=o.item_id
            {fulfilled_join}
            where {base_where}
            order by o.id desc
            limit %s
            """,
            (exchange_id, owner_id, int(limit)),
        )
        return cur.fetchall(), total


def format_player_exchange_list(rows, total):
    if total <= 0:
        return "exchange: you have no active sales"
    names = item_display_names()
    parts = []
    for row in rows:
        template_id = row.get("template_id") or "unknown"
        display_name = names.get(template_id, template_id)
        parts.append(
            f"order {row.get('id')}: {int(row.get('stack_size') or 0)}x {display_name} code={template_id} price={int(row.get('item_price') or 0)}"
        )
    prefix = f"exchange: showing {len(rows)}/{total} active sales"
    return prefix + ": " + "; ".join(parts)


def player_exchange_cashout_limit():
    return int(env("DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_LIMIT", "50"))


def player_exchange_cashout_candidates(conn, player, limit=None):
    owner_id = int(player["player_controller_id"])
    sold_completion_type = int(env("DUNE_ARTIFICIAL_EXCHANGE_SOLD_COMPLETION_TYPE", "1"))
    limit = int(limit if limit is not None else player_exchange_cashout_limit())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("select to_regclass('dune.dune_exchange_fulfilled_orders') as rel")
        if not (cur.fetchone() or {}).get("rel"):
            return [], 0
        cur.execute(
            """
            select count(*) as count
            from dune.dune_exchange_fulfilled_orders f
            join dune.dune_exchange_orders o on o.id=f.order_id
            where o.owner_id=%s
              and f.completion_type=%s
              and o.item_id is null
            """,
            (owner_id, sold_completion_type),
        )
        total = int((cur.fetchone() or {}).get("count") or 0)
        cur.execute(
            """
            select
                o.id as order_id,
                o.owner_id,
                o.item_id,
                o.template_id,
                o.item_price,
                f.completion_type,
                f.stack_size,
                f.source_order_id,
                f.original_order_id,
                (o.item_price * f.stack_size) as expected_solari
            from dune.dune_exchange_fulfilled_orders f
            join dune.dune_exchange_orders o on o.id=f.order_id
            where o.owner_id=%s
              and f.completion_type=%s
              and o.item_id is null
            order by o.id
            limit %s
            """,
            (owner_id, sold_completion_type, limit),
        )
        return cur.fetchall(), total


def ensure_solaris_balance_row(cur, controller_id):
    cur.execute(
        """
        insert into dune.player_virtual_currency_balances(player_controller_id, currency_id, balance)
        values(%s, dune.get_solaris_id(), 0)
        on conflict do nothing
        """,
        (controller_id,),
    )


def read_locked_solaris_balance(cur, controller_id):
    cur.execute(
        """
        select balance
        from dune.player_virtual_currency_balances
        where player_controller_id=%s and currency_id=dune.get_solaris_id()
        for update
        """,
        (controller_id,),
    )
    row = cur.fetchone()
    return int(row["balance"]) if row and row.get("balance") is not None else None


def execute_player_exchange_cashout_row(cur, row, owner_id):
    expected = int(row["expected_solari"])
    ensure_solaris_balance_row(cur, owner_id)
    before_balance = read_locked_solaris_balance(cur, owner_id)
    if before_balance is None:
        raise RuntimeError("Solaris balance row is unavailable for cashout")
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
        where o.id=%s and o.owner_id=%s
        for update
        """,
        (row["order_id"], owner_id),
    )
    locked = cur.fetchone()
    if not locked:
        raise RuntimeError(f"settlement order {row['order_id']} was not found")
    if locked["item_id"] not in (None, 0, ""):
        raise RuntimeError("settlement order has item_id; refusing Solari cashout")
    if int(locked["completion_type"]) != int(env("DUNE_ARTIFICIAL_EXCHANGE_SOLD_COMPLETION_TYPE", "1")):
        raise RuntimeError("settlement completion type is not a seller Solari claim")
    actual_value = int(locked["item_price"]) * int(locked["stack_size"])
    if actual_value != expected:
        raise RuntimeError(f"settlement value changed before cashout: expected {expected}, got {actual_value}")
    cur.execute(
        """
        update dune.player_virtual_currency_balances
        set balance = balance + %s
        where player_controller_id=%s and currency_id=dune.get_solaris_id()
        returning balance
        """,
        (expected, owner_id),
    )
    after_balance = int(cur.fetchone()["balance"])
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
    credited = after_balance - before_balance
    if credited != expected or still_exists:
        raise RuntimeError("cashout failed validation")
    return {
        "orderId": int(row["order_id"]),
        "templateId": row.get("template_id"),
        "stackSize": int(row["stack_size"]),
        "credited": credited,
        "beforeBalance": before_balance,
        "afterBalance": after_balance,
    }


def player_exchange_cashout(conn, player, dry_run=True):
    owner_id = int(player["player_controller_id"])
    rows, total = player_exchange_cashout_candidates(conn, player)
    expected_total = sum(int(row["expected_solari"]) for row in rows)
    if dry_run:
        return {"ok": True, "dryRun": True, "claimed": [], "candidateCount": len(rows), "total": total, "expectedSolari": expected_total}
    claimed = []
    try:
        for row in rows:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                claimed.append(execute_player_exchange_cashout_row(cur, row, owner_id))
        conn.commit()
    except Exception:
        rollback_quietly(conn)
        raise
    return {
        "ok": True,
        "dryRun": False,
        "claimed": claimed,
        "candidateCount": len(rows),
        "total": total,
        "credited": sum(int(row["credited"]) for row in claimed),
    }


def format_exchange_cashout(result):
    if result.get("dryRun"):
        count = int(result.get("candidateCount") or 0)
        total = int(result.get("total") or 0)
        solari = int(result.get("expectedSolari") or 0)
        if count <= 0:
            return "exchange cashout preview: no completed sales ready"
        suffix = "; rerun after current batch if more remain" if total > count else ""
        return f"exchange cashout preview: {solari} Solaris from {count}/{total} completed sales; enable DUNE_CHAT_COMMAND_EXCHANGE_CASHOUT_ENABLED=true to execute{suffix}"
    claimed = result.get("claimed") or []
    credited = int(result.get("credited") or 0)
    if not claimed:
        return "exchange cashout: no completed sales ready"
    suffix = "; rerun to claim remaining completed sales" if int(result.get("total") or 0) > len(claimed) else ""
    return f"exchange cashout: credited {credited} Solaris from {len(claimed)} completed sales{suffix}"


def dd1_totems_for_player(conn, player_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            with owned as (
              select f.totem_id
              from dune.base_backup_find_totems_from_player_owner(%s) f
            ),
            roots as (
              select o.totem_id, root.owner_entity_id
              from owned o
              join dune.placeables root on root.id=o.totem_id
            ),
            child_placeables as (
              select root.id as totem_id, count(child.id) as placeable_count
              from owned o
              join dune.placeables root on root.id=o.totem_id
              left join dune.placeables child on child.owner_entity_id=root.owner_entity_id and child.id<>root.id
              group by root.id
            ),
            building_pieces as (
              select root.id as totem_id, count(bi.*) as building_piece_count
              from owned o
              join dune.placeables root on root.id=o.totem_id
              left join dune.building_instances bi on bi.owner_entity_id=root.owner_entity_id
              group by root.id
            ),
            base_points as (
              select
                r.totem_id,
                ((a.transform).location).x::float8 as x,
                ((a.transform).location).y::float8 as y,
                ((a.transform).location).z::float8 as z
              from roots r
              join dune.actors a on a.id=r.totem_id
              union all
              select
                r.totem_id,
                ((a.transform).location).x::float8 as x,
                ((a.transform).location).y::float8 as y,
                ((a.transform).location).z::float8 as z
              from roots r
              join dune.placeables child on child.owner_entity_id=r.owner_entity_id and child.id<>r.totem_id
              join dune.actors a on a.id=child.id
              union all
              select
                r.totem_id,
                bi.transform[array_lower(bi.transform, 1)]::float8 as x,
                bi.transform[array_lower(bi.transform, 1) + 1]::float8 as y,
                bi.transform[array_lower(bi.transform, 1) + 2]::float8 as z
              from roots r
              join dune.building_instances bi on bi.owner_entity_id=r.owner_entity_id
              where bi.transform is not null
                and array_length(bi.transform, 1) >= 3
            ),
            bounds as (
              select
                totem_id,
                min(x) as min_x,
                max(x) as max_x,
                min(y) as min_y,
                max(y) as max_y,
                min(z) as min_z,
                max(z) as max_z
              from base_points
              group by totem_id
            )
            select
              o.totem_id,
              a.map,
              a.partition_id,
              a.dimension_index,
              ((a.transform).location).x::float8 as x,
              ((a.transform).location).y::float8 as y,
              ((a.transform).location).z::float8 as z,
              p.owner_entity_id,
              p.building_type,
              coalesce(cp.placeable_count, 0)::integer as placeable_count,
              coalesce(bp.building_piece_count, 0)::integer as building_piece_count,
              coalesce(b.min_x, ((a.transform).location).x)::float8 as min_x,
              coalesce(b.max_x, ((a.transform).location).x)::float8 as max_x,
              coalesce(b.min_y, ((a.transform).location).y)::float8 as min_y,
              coalesce(b.max_y, ((a.transform).location).y)::float8 as max_y,
              coalesce(b.min_z, ((a.transform).location).z)::float8 as min_z,
              coalesce(b.max_z, ((a.transform).location).z)::float8 as max_z
            from owned o
            join dune.actors a on a.id=o.totem_id
            left join dune.placeables p on p.id=o.totem_id
            left join child_placeables cp on cp.totem_id=o.totem_id
            left join building_pieces bp on bp.totem_id=o.totem_id
            left join bounds b on b.totem_id=o.totem_id
            where a.partition_id=%s
              and a.map = any(%s)
            order by o.totem_id
            """,
            (int(player_id), dd1_partition_id(), dd1_map_names()),
        )
        return cur.fetchall()


def format_dd1_totem(row):
    return (
        f"{row.get('totem_id')} {row.get('map')} p{row.get('partition_id')} "
        f"x={float(row.get('x') or 0):.1f} y={float(row.get('y') or 0):.1f} "
        f"pieces={int(row.get('building_piece_count') or 0)} "
        f"placeables={int(row.get('placeable_count') or 0)}"
    )


def format_dd1_totem_choices(rows, command_name="DD1_backup"):
    if not rows:
        return "no DD1 totems found for your character"
    choices = "; ".join(format_dd1_totem(row) for row in rows[:8])
    return f"multiple DD1 totems found: {choices}; use &{command_name} <totem_id>"


def dd1_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dd1_bounds_for_totem(row):
    x = dd1_float(row.get("x"), 0.0)
    y = dd1_float(row.get("y"), 0.0)
    z = dd1_float(row.get("z"), 0.0)
    return {
        "minX": dd1_float(row.get("min_x"), x),
        "maxX": dd1_float(row.get("max_x"), x),
        "minY": dd1_float(row.get("min_y"), y),
        "maxY": dd1_float(row.get("max_y"), y),
        "minZ": dd1_float(row.get("min_z"), z),
        "maxZ": dd1_float(row.get("max_z"), z),
    }


def point_in_dd1_totem(row, location, padding=None):
    if not location:
        return False
    bounds = dd1_bounds_for_totem(row)
    padding = dd1_base_containment_padding() if padding is None else float(padding)
    x = dd1_float(location.get("x"))
    y = dd1_float(location.get("y"))
    z = dd1_float(location.get("z"))
    if x is None or y is None or z is None:
        return False
    return (
        bounds["minX"] - padding <= x <= bounds["maxX"] + padding
        and bounds["minY"] - padding <= y <= bounds["maxY"] + padding
        and bounds["minZ"] - padding <= z <= bounds["maxZ"] + padding
    )


def dd1_current_location_from_sender(player, sender_origin=None, action="DD1 command"):
    if int(player.get("partition_id") or -1) != dd1_partition_id():
        raise ValueError(f"{action} must be requested while your character is in DD1")
    origin = normalize_origin_location(sender_origin)
    if origin:
        source = "chat-origin"
    elif player.get("x") is not None and player.get("y") is not None and player.get("z") is not None:
        origin = {"x": float(player["x"]), "y": float(player["y"]), "z": float(player["z"])}
        source = "stored-player-transform"
    else:
        raise ValueError(f"could not resolve your current DD1 position for {action}")
    return {
        "source": source,
        "map": player.get("partition_map") or player.get("actor_map") or "DeepDesert_1",
        "partitionId": dd1_partition_id(),
        "dimensionIndex": int(player.get("dimension_index") or 0),
        **origin,
    }


def format_dd1_not_in_base(rows):
    owned = "; ".join(format_dd1_totem(row) for row in rows[:8])
    suffix = f"; owned DD1 totems: {owned}" if owned else ""
    return f"you are not standing inside one of your owned DD1 bases{suffix}"


def select_dd1_totem(conn, player, args, command_name="DD1_backup", current_location=None):
    rows = dd1_totems_for_player(conn, player["player_controller_id"])
    if args:
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
            raise ValueError(f"usage: &{command_name} [totem_id]")
        wanted = int(args[0])
        for row in rows:
            if int(row["totem_id"]) == wanted:
                if current_location and not point_in_dd1_totem(row, current_location):
                    raise ValueError(f"you are not standing inside owned DD1 totem {wanted}")
                return row, rows
        raise ValueError(f"totem {wanted} is not one of your DD1 totems")
    if current_location:
        containing = [row for row in rows if point_in_dd1_totem(row, current_location)]
        if len(containing) == 1:
            return containing[0], rows
        if not containing:
            raise ValueError(format_dd1_not_in_base(rows))
        choices = "; ".join(format_dd1_totem(row) for row in containing[:8])
        raise ValueError(f"multiple owned DD1 bases contain your current position: {choices}; use &{command_name} <totem_id>")
    if len(rows) == 1:
        return rows[0], rows
    raise ValueError(format_dd1_totem_choices(rows, command_name=command_name))


def dd1_backup_payload_summary(conn, backup_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            with d as (
              select * from dune.base_backup_get_data(%s)
            ),
            points as (
              select
                ((s.transform).location).x::float8 as x,
                ((s.transform).location).y::float8 as y,
                ((s.transform).location).z::float8 as z
              from dune.base_backup_get_actors_to_spawn(%s) s
              union all
              select
                bp.transform[array_lower(bp.transform, 1)]::float8 as x,
                bp.transform[array_lower(bp.transform, 1) + 1]::float8 as y,
                bp.transform[array_lower(bp.transform, 1) + 2]::float8 as z
              from d, unnest(d.building_pieces) bp
            ),
            totem_spawn as (
              select
                ((s.transform).location).x::float8 as x,
                ((s.transform).location).y::float8 as y,
                ((s.transform).location).z::float8 as z
              from d, dune.base_backup_get_actors_to_spawn(%s) s
              where s.id = (d.totem).totem_actor_id
              limit 1
            )
            select
              d.name,
              (d.totem).totem_actor_id as source_totem_id,
              (d.totem).totem_building_type as totem_building_type,
              (d.totem).totem_map as source_map,
              (d.totem).landclaim_original_global_location as source_landclaim_location,
              (d.totem).landclaim_original_global_yaw_rotation as source_landclaim_yaw,
              coalesce(array_length((d.totem).landclaim_grid, 1), 0)::integer as landclaim_segment_count,
              coalesce(array_length(d.building_pieces, 1), 0)::integer as building_piece_count,
              coalesce(array_length(d.placeables, 1), 0)::integer as placeable_count,
              (select count(*) from dune.base_backup_get_actors_to_spawn(%s))::integer as actor_count,
              coalesce((select x from totem_spawn), ((d.totem).landclaim_original_global_location)[array_lower((d.totem).landclaim_original_global_location, 1)]::float8) as source_anchor_x,
              coalesce((select y from totem_spawn), ((d.totem).landclaim_original_global_location)[array_lower((d.totem).landclaim_original_global_location, 1) + 1]::float8) as source_anchor_y,
              coalesce((select z from totem_spawn), ((d.totem).landclaim_original_global_location)[array_lower((d.totem).landclaim_original_global_location, 1) + 2]::float8) as source_anchor_z,
              min(points.x) as min_x,
              max(points.x) as max_x,
              min(points.y) as min_y,
              max(points.y) as max_y,
              min(points.z) as min_z,
              max(points.z) as max_z
            from d, points
            group by d.name, d.totem, d.building_pieces, d.placeables
            """,
            (int(backup_id), int(backup_id), int(backup_id), int(backup_id)),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"backup {backup_id} was not found")
    return dict(row)


def dd1_create_backup(conn, player, totem, dry_run=True, current_location=None):
    plan = {
        "player": player.get("character_name"),
        "playerId": int(player["player_controller_id"]),
        "totemId": int(totem["totem_id"]),
        "partitionId": int(totem["partition_id"]),
        "map": totem.get("map"),
        "buildingPieceCount": int(totem.get("building_piece_count") or 0),
        "placeableCount": int(totem.get("placeable_count") or 0),
        "dryRun": dry_run,
    }
    if current_location:
        plan["currentLocation"] = current_location
    if dry_run:
        return {"ok": True, "dryRun": True, "plan": plan}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "select dune.base_backup_save_from_totem(%s, %s) as backup_id",
                (int(player["player_controller_id"]), int(totem["totem_id"])),
            )
            backup_id = int(cur.fetchone()["backup_id"])
            cur.execute("select count(*)::integer as count from dune.base_backup_linked_actors where id=%s", (backup_id,))
            linked_count = int((cur.fetchone() or {}).get("count") or 0)
        summary = dd1_backup_payload_summary(conn, backup_id)
        conn.commit()
        return {"ok": True, "dryRun": False, "backupId": backup_id, "linkedActorCount": linked_count, "plan": plan, "summary": summary}
    except Exception:
        rollback_quietly(conn)
        raise


def format_dd1_backup_result(result):
    plan = result.get("plan") or {}
    if result.get("dryRun"):
        return (
            f"DD1 backup preview: totem {plan.get('totemId')} has "
            f"{plan.get('buildingPieceCount')} pieces and {plan.get('placeableCount')} placeables; "
            "backup execution is disabled because DB backup marks the base BaseBackup but does not live-unload it from DD"
        )
    summary = result.get("summary") or {}
    return (
        f"DD1 backup created: backup {result.get('backupId')} from totem {plan.get('totemId')} "
        f"with {summary.get('building_piece_count', 0)} pieces, {summary.get('placeable_count', 0)} placeables"
    )


def dd1_backups_for_player(conn, player_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select *
            from dune.base_backup_get_available_backups(%s)
            order by id desc
            """,
            (int(player_id),),
        )
        return cur.fetchall()


def format_dd1_backup_choice(row):
    loc = row.get("landclaim_original_global_location")
    loc_text = ""
    if loc and len(loc) >= 2:
        loc_text = f" x={float(loc[0]):.1f} y={float(loc[1]):.1f}"
    return f"{row.get('id')} {row.get('base_backup_name')} {row.get('base_backup_map')}{loc_text}"


def select_dd1_backup(conn, player, args):
    backups = dd1_backups_for_player(conn, player["player_controller_id"])
    if args:
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
            raise ValueError("usage: &DD1_restore [backup_id] or &DD1_restore yes")
        wanted = int(args[0])
        for row in backups:
            if int(row["id"]) == wanted:
                return row, backups
        raise ValueError(f"backup {wanted} is not one of your available BRT backups")
    dd_backups = [row for row in backups if row.get("base_backup_map") in dd1_map_names()]
    candidates = dd_backups or backups
    if len(candidates) == 1:
        return candidates[0], backups
    if not candidates:
        raise ValueError("no BRT backups found for your character; use &DD1_backup first")
    choices = "; ".join(format_dd1_backup_choice(row) for row in candidates[:8])
    raise ValueError(f"multiple BRT backups found: {choices}; use &DD1_restore <backup_id>")


def normalize_origin_location(origin):
    if not isinstance(origin, dict):
        return None
    try:
        x = origin.get("X", origin.get("x"))
        y = origin.get("Y", origin.get("y"))
        z = origin.get("Z", origin.get("z"))
        if x is None or y is None or z is None:
            return None
        x = float(x)
        y = float(y)
        z = float(z)
    except (TypeError, ValueError):
        return None
    if abs(x) < 0.001 and abs(y) < 0.001 and abs(z) < 0.001:
        return None
    return {"x": x, "y": y, "z": z}


def dd1_restore_target_from_sender(player, sender_origin=None):
    origin = dd1_current_location_from_sender(player, sender_origin=sender_origin, action="DD1 restore")
    offset = dd1_restore_offset()
    return {
        "source": origin["source"],
        "map": origin["map"],
        "partitionId": origin["partitionId"],
        "dimensionIndex": origin["dimensionIndex"],
        "x": float(origin["x"]) + float(offset["x"]),
        "y": float(origin["y"]) + float(offset["y"]),
        "z": float(origin["z"]) + float(offset["z"]),
        "playerLocation": origin,
        "restoreOffset": offset,
    }


def dd1_restore_plan(conn, player, backup, target):
    summary = dd1_backup_payload_summary(conn, int(backup["id"]))
    dx = float(target["x"]) - float(summary["source_anchor_x"])
    dy = float(target["y"]) - float(summary["source_anchor_y"])
    dz = float(target["z"]) - float(summary["source_anchor_z"])
    bbox = {
        "minX": float(summary["min_x"]) + dx if summary.get("min_x") is not None else None,
        "maxX": float(summary["max_x"]) + dx if summary.get("max_x") is not None else None,
        "minY": float(summary["min_y"]) + dy if summary.get("min_y") is not None else None,
        "maxY": float(summary["max_y"]) + dy if summary.get("max_y") is not None else None,
        "minZ": float(summary["min_z"]) + dz if summary.get("min_z") is not None else None,
        "maxZ": float(summary["max_z"]) + dz if summary.get("max_z") is not None else None,
    }
    return {
        "player": player.get("character_name"),
        "playerId": int(player["player_controller_id"]),
        "backupId": int(backup["id"]),
        "backupName": backup.get("base_backup_name"),
        "sourceMap": summary.get("source_map"),
        "sourceTotemId": int(summary["source_totem_id"]),
        "target": target,
        "offset": {"x": dx, "y": dy, "z": dz},
        "bbox": bbox,
        "actorCount": int(summary.get("actor_count") or 0),
        "buildingPieceCount": int(summary.get("building_piece_count") or 0),
        "placeableCount": int(summary.get("placeable_count") or 0),
        "landclaimSegmentCount": int(summary.get("landclaim_segment_count") or 0),
    }


def format_dd1_restore_preview(plan, expires_seconds=None):
    target = plan["target"]
    expires_seconds = dd1_restore_confirmation_ttl() if expires_seconds is None else expires_seconds
    gate = "enabled" if chat_dd1_restore_enabled() else "disabled"
    return (
        f"DD1 restore preview: backup {plan['backupId']} has {plan['buildingPieceCount']} pieces, "
        f"{plan['placeableCount']} placeables, {plan['landclaimSegmentCount']} landclaim segments; "
        f"target x={target['x']:.1f} y={target['y']:.1f} z={target['z']:.1f} via {target['source']}; "
        f"restore gate {gate}; reply &DD1_restore yes within {expires_seconds}s"
    )


def dd1_restore_max_confirm_distance():
    return float(env("DUNE_CHAT_COMMAND_DD1_RESTORE_CONFIRM_MAX_DISTANCE", "500"))


def dd1_restore_confirm_position_ok(pending, current_target):
    previous = pending.get("target") or {}
    if previous.get("source") != "chat-origin" or current_target.get("source") != "chat-origin":
        return True, 0.0
    dx = float(previous["x"]) - float(current_target["x"])
    dy = float(previous["y"]) - float(current_target["y"])
    dz = float(previous["z"]) - float(current_target["z"])
    distance = (dx * dx + dy * dy + dz * dz) ** 0.5
    return distance <= dd1_restore_max_confirm_distance(), distance


def dd1_restore_totem_permission(cur, totem_id, player_id, actor_name="##Totem_Placeable", owner_rank=1):
    cur.execute(
        """
        insert into dune.permission_actor(actor_id, actor_name, actor_type, access_level, is_child)
        values (%s, %s, 3, 3, false)
        on conflict (actor_id) do update set
          actor_name=excluded.actor_name,
          actor_type=excluded.actor_type,
          access_level=excluded.access_level,
          is_child=excluded.is_child
        """,
        (int(totem_id), actor_name),
    )
    permission_actor_rows = cur.rowcount
    cur.execute(
        """
        insert into dune.permission_actor_rank(permission_actor_id, player_id, rank)
        values (%s, %s, %s)
        on conflict (permission_actor_id, player_id) do update set rank=excluded.rank
        """,
        (int(totem_id), int(player_id), int(owner_rank)),
    )
    permission_rank_rows = cur.rowcount
    return {
        "permissionActorRows": permission_actor_rows,
        "permissionRankRows": permission_rank_rows,
    }


def read_manifest_file(path):
    values = {}
    try:
        for raw in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except OSError:
        return {}
    return values


def latest_dd_pre_restore_backup():
    root = ROOT / "backups" / "manual"
    try:
        candidates = [p for p in root.glob("dd-pre-restore-*") if p.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def dd1_verify_recent_pre_restore_backup():
    if not dd1_restore_recent_backup_required():
        return {"required": False, "ok": True}
    backup_dir = latest_dd_pre_restore_backup()
    if backup_dir is None:
        raise RuntimeError("DD1 restore execution is blocked: no dd-pre-restore backup exists")
    manifest = read_manifest_file(backup_dir / "manifest.txt")
    dump_name = manifest.get("postgres_dump")
    dump_path = backup_dir / dump_name if dump_name else None
    if not dump_name or not dump_path.exists() or not dump_path.is_file():
        raise RuntimeError(f"DD1 restore execution is blocked: backup {backup_dir} has no postgres dump")
    age_seconds = max(0, int(time.time() - backup_dir.stat().st_mtime))
    max_age = dd1_restore_backup_max_age_seconds()
    if max_age >= 0 and age_seconds > max_age:
        raise RuntimeError(
            f"DD1 restore execution is blocked: latest backup {backup_dir.name} is {age_seconds}s old "
            f"(max {max_age}s)"
        )
    return {
        "required": True,
        "ok": True,
        "backupDir": str(backup_dir),
        "postgresDump": str(dump_path),
        "ageSeconds": age_seconds,
        "manifest": manifest,
    }


def dd1_restore_collision_rows(cur, backup_id, target_map, target_partition, target_dimension, bbox):
    if not dd1_restore_collision_guard_enabled():
        return []
    if any(bbox.get(key) is None for key in ("minX", "maxX", "minY", "maxY")):
        raise RuntimeError("DD1 restore execution is blocked: restore footprint bounds are unavailable")
    padding = dd1_restore_collision_padding()
    cur.execute(
        """
        with linked as (
          select actor_id
          from dune.base_backup_linked_actors
          where id=%s
        )
        select
          t.id as totem_id,
          a.map,
          a.partition_id,
          a.dimension_index,
          ((a.transform).location).x::float8 as x,
          ((a.transform).location).y::float8 as y,
          ((a.transform).location).z::float8 as z
        from dune.totems t
        join dune.actors a on a.id=t.id
        where a.map=%s
          and a.partition_id=%s
          and a.dimension_index=%s
          and t.id not in (select actor_id from linked)
          and ((a.transform).location).x::float8 between %s and %s
          and ((a.transform).location).y::float8 between %s and %s
        order by t.id
        limit 8
        """,
        (
            int(backup_id),
            target_map,
            int(target_partition),
            int(target_dimension),
            float(bbox["minX"]) - padding,
            float(bbox["maxX"]) + padding,
            float(bbox["minY"]) - padding,
            float(bbox["maxY"]) + padding,
        ),
    )
    return [dict(row) for row in cur.fetchall()]


def write_dd1_restore_audit(result, plan, backup_guard):
    try:
        DD1_RESTORE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_id = int(result.get("backupId") or plan.get("backupId") or 0)
        path = DD1_RESTORE_AUDIT_DIR / f"{stamp}-backup-{backup_id}.json"
        payload = {
            "createdUtc": stamp,
            "result": result,
            "plan": plan,
            "backupGuard": backup_guard,
        }
        path.write_text(json.dumps(payload, default=str, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return str(path)
    except Exception as exc:
        return f"audit-write-failed: {exc}"


def dd1_connected_players(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select coalesce(sum(coalesce(fs.connected_players, 0)), 0)::integer as connected_players
            from dune.world_partition wp
            left join dune.farm_state fs on fs.server_id=wp.server_id
            where wp.partition_id=%s
              and wp.map = any(%s)
            """,
            (dd1_partition_id(), dd1_map_names()),
        )
        row = cur.fetchone() or {}
    return int(row.get("connected_players") or 0)


def dd1_restore_backup_to_location(conn, plan, dry_run=True):
    if dry_run:
        return {"ok": True, "dryRun": True, "plan": plan}
    if not chat_dd1_restore_copy_source_enabled():
        raise RuntimeError(
            "DD1 restore execution is blocked: set DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_COPY_SOURCE_ENABLED=true "
            "only after disposable validation of the persistence copy writer"
        )
    backup_guard = dd1_verify_recent_pre_restore_backup()
    connected_players = dd1_connected_players(conn)
    max_connected_players = dd1_restore_max_connected_players()
    if max_connected_players >= 0 and connected_players > max_connected_players:
        raise RuntimeError(
            f"DD1 restore execution is blocked: DD connected_players={connected_players} exceeds "
            f"DUNE_CHAT_COMMAND_DD1_RESTORE_MAX_DD_CONNECTED_PLAYERS={max_connected_players}"
        )

    backup_id = int(plan["backupId"])
    player_id = int(plan["playerId"])
    target = plan["target"]
    target_partition = int(target["partitionId"])
    target_dimension = int(target["dimensionIndex"])
    target_map = target.get("map") or ""
    if target_partition != dd1_partition_id():
        raise ValueError(f"DD1 restore target partition must be {dd1_partition_id()}, got {target_partition}")
    if target_map not in dd1_map_names():
        raise ValueError(f"DD1 restore target map must be one of {dd1_map_names()}, got {target_map}")

    summary = dd1_backup_payload_summary(conn, backup_id)
    source_map = summary.get("source_map") or ""
    allowed_source_maps = dd1_restore_allowed_source_maps()
    if allowed_source_maps is not None and source_map not in allowed_source_maps:
        raise ValueError(f"DD1 restore source map must be one of {allowed_source_maps}, got {source_map}")
    totem_id = int(summary["source_totem_id"])
    dx = float(target["x"]) - float(summary["source_anchor_x"])
    dy = float(target["y"]) - float(summary["source_anchor_y"])
    dz = float(target["z"]) - float(summary["source_anchor_z"])
    bbox = plan.get("bbox") or {}
    if not bbox:
        bbox = {
            "minX": float(summary["min_x"]) + dx if summary.get("min_x") is not None else None,
            "maxX": float(summary["max_x"]) + dx if summary.get("max_x") is not None else None,
            "minY": float(summary["min_y"]) + dy if summary.get("min_y") is not None else None,
            "maxY": float(summary["max_y"]) + dy if summary.get("max_y") is not None else None,
            "minZ": float(summary["min_z"]) + dz if summary.get("min_z") is not None else None,
            "maxZ": float(summary["max_z"]) + dz if summary.get("max_z") is not None else None,
        }

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            with linked as (
              select actor_id
              from dune.base_backup_linked_actors
              where id=%s
            )
            select
              bb.player_id,
              player_actor.map as player_map,
              player_actor.partition_id as player_partition_id,
              player_actor.dimension_index as player_dimension_index,
              (select count(*) from linked)::integer as linked_actor_count,
              (select count(*) from linked l join dune.actors a on a.id=l.actor_id)::integer as linked_actors_present,
              (select count(*) from linked where actor_id=%s)::integer as linked_totem_count
            from dune.base_backups bb
            join dune.actors player_actor on player_actor.id=bb.player_id
            where bb.id=%s
            """,
            (backup_id, totem_id, backup_id),
        )
        health = cur.fetchone()
        if not health:
            raise ValueError(f"backup {backup_id} was not found")
        if int(health["player_id"]) != player_id:
            raise ValueError(f"backup {backup_id} does not belong to player {player_id}")
        player_partition = int(health["player_partition_id"])
        player_dimension = int(health["player_dimension_index"])
        player_map = health.get("player_map") or ""
        if player_partition != target_partition:
            raise ValueError(
                f"DD1 restore player actor must be in target partition {target_partition}, got {player_partition}"
            )
        if player_dimension != target_dimension:
            raise ValueError(
                f"DD1 restore player actor must be in target dimension {target_dimension}, got {player_dimension}"
            )
        if player_map not in dd1_map_names():
            raise ValueError(f"DD1 restore player actor map must be one of {dd1_map_names()}, got {player_map}")
        actor_map = player_map
        linked_actor_count = int(health["linked_actor_count"] or 0)
        linked_actors_present = int(health["linked_actors_present"] or 0)
        linked_totem_count = int(health["linked_totem_count"] or 0)
        if linked_actor_count <= 0:
            raise ValueError(f"backup {backup_id} has no linked actors")
        if linked_actors_present != linked_actor_count:
            raise ValueError(
                f"backup {backup_id} linked actors are incomplete: {linked_actors_present}/{linked_actor_count} present"
            )
        if linked_totem_count != 1:
            raise ValueError(f"backup {backup_id} does not link expected totem {totem_id}")
        collisions = dd1_restore_collision_rows(cur, backup_id, actor_map, target_partition, target_dimension, bbox)
        if collisions:
            found = "; ".join(
                f"{row.get('totem_id')} x={float(row.get('x') or 0):.1f} y={float(row.get('y') or 0):.1f}"
                for row in collisions
            )
            raise RuntimeError(f"DD1 restore execution is blocked: target overlaps nearby totems: {found}")

        cur.execute(
            """
            update dune.actors a
            set
              map=%s,
              partition_id=%s,
              dimension_index=%s,
              transform=row(
                row(
                  ((a.transform).location).x + %s::float8,
                  ((a.transform).location).y + %s::float8,
                  ((a.transform).location).z + %s::float8
                )::dune.vector,
                (a.transform).rotation
              )::dune.transform
            where a.id in (
              select actor_id from dune.base_backup_linked_actors where id=%s
            )
            """,
            (actor_map, target_partition, target_dimension, dx, dy, dz, backup_id),
        )
        actor_rows = cur.rowcount

        cur.execute(
            """
            update dune.building_instances bi
            set
              transform[array_lower(bi.transform, 1)] = bi.transform[array_lower(bi.transform, 1)] + %s::real,
              transform[array_lower(bi.transform, 1) + 1] = bi.transform[array_lower(bi.transform, 1) + 1] + %s::real,
              transform[array_lower(bi.transform, 1) + 2] = bi.transform[array_lower(bi.transform, 1) + 2] + %s::real
            where bi.building_id in (
              select actor_id from dune.base_backup_linked_actors where id=%s
            )
            """,
            (dx, dy, dz, backup_id),
        )
        building_instance_rows = cur.rowcount

        cur.execute(
            """
            update dune.totems t
            set
              landclaim_original_global_location[array_lower(t.landclaim_original_global_location, 1)] =
                landclaim_original_global_location[array_lower(t.landclaim_original_global_location, 1)] + %s::real,
              landclaim_original_global_location[array_lower(t.landclaim_original_global_location, 1) + 1] =
                landclaim_original_global_location[array_lower(t.landclaim_original_global_location, 1) + 1] + %s::real,
              landclaim_original_global_location[array_lower(t.landclaim_original_global_location, 1) + 2] =
                landclaim_original_global_location[array_lower(t.landclaim_original_global_location, 1) + 2] + %s::real
            where t.id=%s
            """,
            (dx, dy, dz, totem_id),
        )
        totem_rows = cur.rowcount

        cur.execute("select dune.base_backup_finish_placing(%s)", (backup_id,))
        permission_counts = dd1_restore_totem_permission(cur, totem_id, player_id)

    result = {
        "ok": True,
        "dryRun": False,
        "backupId": backup_id,
        "playerId": player_id,
        "totemId": totem_id,
        "sourceMap": source_map,
        "actorMap": actor_map,
        "target": target,
        "connectedPlayers": connected_players,
        "backupGuard": backup_guard,
        "offset": {"x": dx, "y": dy, "z": dz},
        "counts": {
            "actorsUpdated": actor_rows,
            "buildingInstancesUpdated": building_instance_rows,
            "totemsUpdated": totem_rows,
            "buildingPieces": int(summary.get("building_piece_count") or 0),
            "placeables": int(summary.get("placeable_count") or 0),
            "linkedActors": linked_actor_count,
            "actorSpawns": int(summary.get("actor_count") or 0),
            "landclaimSegments": int(summary.get("landclaim_segment_count") or 0),
            **permission_counts,
        },
    }
    result["auditPath"] = write_dd1_restore_audit(result, plan, backup_guard)
    return result


def fuzzy_item_suggestion(rows, search_text, count):
    normalized = normalize_item_search(search_text)
    wanted_tokens = [token for token in re.split(r"[^a-z0-9]+", search_text.lower()) if token]
    ignored = {"improved", "basic", "advanced", "mk", "tier"}
    scored = []
    for row in rows:
        if int(row.get("stack_size") or 0) < count:
            continue
        template = row.get("template_id") or ""
        template_norm = normalize_item_search(template)
        if not template_norm:
            continue
        ratio = difflib.SequenceMatcher(None, normalized, template_norm).ratio() if normalized else 0.0
        token_hits = sum(1 for token in wanted_tokens if token not in ignored and token in template_norm)
        prefix_bonus = 0.2 if template_norm.startswith(normalized[: min(len(normalized), 4)]) else 0.0
        contains_bonus = 0.25 if normalized and normalized in template_norm else 0.0
        score = ratio + contains_bonus + prefix_bonus + (token_hits * 0.1)
        scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], int(item[1].get("stack_size") or 0)), reverse=True)
    score, row = scored[0]
    threshold = float(env("DUNE_CHAT_COMMAND_AUCTION_SUGGESTION_MIN_SCORE", "0.55"))
    if score < threshold:
        return None
    return {
        "itemId": row["id"],
        "templateId": row["template_id"],
        "stackSize": row["stack_size"],
        "inventoryId": row["inventory_id"],
        "qualityLevel": row["quality_level"],
        "score": round(score, 3),
    }


def item_search_candidates(conn, player, search_text, source="personal", explicit_inventory_id=None, explicit_item_id=None, count=1):
    inventories = auction_source_inventories(conn, player, source=source, explicit_inventory_id=explicit_inventory_id)
    if not inventories:
        return [], [], None
    inventory_ids = [row["id"] for row in inventories]
    if explicit_item_id is not None:
        rows = inventory_item_rows(conn, inventory_ids, explicit_item_id=explicit_item_id)
        return rows, inventories, None
    normalized = normalize_item_search(search_text)
    if not normalized:
        return [], inventories, None
    rows = inventory_item_rows(conn, inventory_ids)
    exact = []
    contains = []
    token_contains = []
    wanted_tokens = [token for token in re.split(r"[^a-z0-9]+", search_text.lower()) if token]
    for row in rows:
        template_norm = normalize_item_search(row["template_id"])
        if template_norm == normalized:
            exact.append(row)
        elif normalized and normalized in template_norm:
            contains.append(row)
        elif wanted_tokens and all(token in template_norm for token in wanted_tokens if token not in {"improved", "basic", "advanced", "mk", "tier"}):
            token_contains.append(row)
    matches = exact or contains or token_contains
    suggestion = None if matches else fuzzy_item_suggestion(rows, search_text, count)
    return matches, inventories, suggestion


def parse_auction_command_args(args):
    if len(args) < 3:
        raise ValueError(auction_usage_message())
    source = "personal"
    explicit_inventory_id = None
    explicit_item_id = None
    remaining = list(args)
    while remaining:
        if remaining[0] in ("--base", "base", "--storage", "storage"):
            source = "base"
            remaining = remaining[1:]
        elif remaining[0] in ("--inventory", "inventory", "--inv", "inv"):
            if len(remaining) < 2:
                raise ValueError("usage: &auction --inventory <inventory_id> <item> <count> <price>")
            explicit_inventory_id = parse_positive_int(remaining[1], "inventory_id")
            source = "inventory"
            remaining = remaining[2:]
        elif remaining[0] in ("--item-id", "item-id", "--item", "item"):
            if len(remaining) < 2:
                raise ValueError("usage: &auction --item-id <item_id> <count> <price>")
            explicit_item_id = parse_positive_int(remaining[1], "item_id")
            remaining = remaining[2:]
        else:
            break
    if explicit_item_id is None and len(remaining) < 3:
        raise ValueError(auction_usage_message())
    if explicit_item_id is not None and len(remaining) > 2:
        raise ValueError("do not provide an item name when using --item-id")
    if explicit_item_id is not None and len(remaining) < 2:
        raise ValueError("usage: &auction --item-id <item_id> <count> <price>")
    count = parse_positive_int(remaining[-2], "count")
    price = parse_positive_int(remaining[-1], "price")
    if explicit_item_id is None:
        search_text = " ".join(remaining[:-2]).strip()
        if not search_text:
            raise ValueError(auction_usage_message())
    else:
        if remaining[:-2]:
            raise ValueError("do not provide an item name when using --item-id")
        search_text = f"item-id:{explicit_item_id}"
    return source, explicit_inventory_id, explicit_item_id, search_text, count, price


def solari_balance(conn, owner_id):
    with conn.cursor() as cur:
        cur.execute("select dune.dune_exchange_retrieve_solari_balance(%s)", (owner_id,))
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def exchange_order_slots(conn, owner_id):
    with conn.cursor() as cur:
        cur.execute("select dune.get_dune_exchange_used_order_slots(%s)", (owner_id,))
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def auction_item(conn, player, search_text, count, price, dry_run=True, source="personal", explicit_inventory_id=None, explicit_item_id=None):
    owner_id = int(player["player_controller_id"])
    exchange_id = int(env("DUNE_CHAT_COMMAND_AUCTION_EXCHANGE_ID", "2"))
    access_point_id = int(env("DUNE_CHAT_COMMAND_AUCTION_ACCESS_POINT_ID", "1"))
    max_orders = int(env("DUNE_CHAT_COMMAND_AUCTION_MAX_ORDERS_PER_PLAYER", "50"))
    solari_cost = int(env("DUNE_CHAT_COMMAND_AUCTION_LISTING_FEE", "0"))
    duration_seconds = int(env("DUNE_CHAT_COMMAND_AUCTION_DURATION_SECONDS", "2419200"))
    category_mask = int(env("DUNE_CHAT_COMMAND_AUCTION_CATEGORY_MASK", "0"))
    category_depth = int(env("DUNE_CHAT_COMMAND_AUCTION_CATEGORY_DEPTH", "0"))
    expires_at = int(time.time()) + duration_seconds

    try:
        matches, inventories, suggestion = item_search_candidates(conn, player, search_text, source=source, explicit_inventory_id=explicit_inventory_id, explicit_item_id=explicit_item_id, count=count)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not matches:
        response = {
            "ok": False,
            "error": f"no allowed inventory item matched '{search_text}'",
            "source": source,
            "searchedInventories": [row["id"] for row in inventories],
        }
        if suggestion:
            response["suggestion"] = suggestion
        return response
    templates = sorted({row["template_id"] for row in matches})
    if len(templates) > 1:
        return {
            "ok": False,
            "error": "item search is ambiguous: " + ", ".join(templates[:12]),
            "source": source,
            "matches": [
                {"itemId": row["id"], "templateId": row["template_id"], "stackSize": row["stack_size"], "inventoryId": row["inventory_id"]}
                for row in matches[:20]
            ],
        }

    available = sum(int(row["stack_size"]) for row in matches)
    if available < count:
        return {
            "ok": False,
            "error": f"only {available}x {templates[0]} available, requested {count}",
            "source": source,
            "matches": [
                {"itemId": row["id"], "templateId": row["template_id"], "stackSize": row["stack_size"], "inventoryId": row["inventory_id"]}
                for row in matches
            ],
        }
    if exchange_order_slots(conn, owner_id) >= max_orders:
        return {"ok": False, "error": f"exchange order slot limit reached ({max_orders})"}
    balance = solari_balance(conn, owner_id)
    if balance < solari_cost:
        return {"ok": False, "error": f"exchange Solari balance {balance} is below listing fee {solari_cost}"}

    source_item = next((row for row in matches if int(row["stack_size"]) >= count), None)
    if not source_item:
        return {"ok": False, "error": "split-across-stacks listing is not supported yet; merge the stack first"}
    stats = source_item.get("stats") or {}
    durability = ((stats.get("FItemStackAndDurabilityStats") or [None, {}])[1] or {}) if isinstance(stats, dict) else {}
    durability_cur = float(durability.get("CurrentDurability") or 100.0)
    durability_max = float(durability.get("MaxDurability") or 100.0)
    plan = {
        "player": player["character_name"],
        "ownerId": owner_id,
        "exchangeId": exchange_id,
        "accessPointId": access_point_id,
        "itemId": source_item["id"],
        "templateId": source_item["template_id"],
        "count": count,
        "price": price,
        "listingFee": solari_cost,
        "expiresAt": expires_at,
        "sourceInventoryId": source_item["inventory_id"],
        "sourceStackSize": source_item["stack_size"],
        "source": source,
        "explicitItemId": explicit_item_id,
        "dryRun": dry_run,
    }
    if dry_run:
        return {"ok": True, "action": "auction.preview", "plan": plan}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select *
            from dune.dune_exchange_add_sell_order(
              %s::bigint, %s::bigint, %s::bigint, %s::integer, %s::bigint,
              %s::bigint, %s::bigint, %s::integer, %s::smallint, %s::real,
              %s::real, %s::bigint, %s::bigint, %s::bigint, %s::bigint
            )
            """,
            (
                exchange_id,
                access_point_id,
                owner_id,
                max_orders,
                expires_at,
                int(source_item["id"]),
                count,
                category_mask,
                category_depth,
                durability_cur,
                durability_max,
                price,
                price,
                int(source_item["quality_level"] or 0),
                solari_cost,
            ),
        )
        result = cur.fetchone()
        order_id = int(result.get("order_id") or 0) if result else 0
        slots = int(result.get("order_slots_used") or 0) if result else 0
        if order_id <= 0:
            conn.rollback()
            return {"ok": False, "error": "exchange function returned order_id=0", "plan": plan, "orderSlotsUsed": slots}
        cur.execute(
            """
            select o.id, o.owner_id, o.item_id, o.template_id, o.item_price, o.expiration_time,
                   i.stack_size, i.inventory_id
            from dune.dune_exchange_orders o
            join dune.items i on i.id=o.item_id
            where o.id=%s
            """,
            (order_id,),
        )
        order = cur.fetchone()
    conn.commit()
    return {"ok": True, "action": "auction.created", "plan": plan, "order": dict(order), "orderSlotsUsed": slots}


def specialization_track_types(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select enumlabel
            from pg_enum e
            join pg_type t on t.oid = e.enumtypid
            join pg_namespace n on n.oid = t.typnamespace
            where n.nspname = 'dune' and t.typname = 'specializationtracktype'
            order by e.enumsortorder
            """
        )
        return [row[0] for row in cur.fetchall()]


def handle_gm_command(conn, args, resolved_admin, reply=False):
    if not args:
        response = "usage: &gm <help|catalog|test|routes|mark|marks|recall|pos|dry|where|goto|bring|unstuck|item|kit|xp|tp|map|travel|dimension|patrol|sandworm|marker|vehicle|fly|ghost|walk> ...; online &goto/&bring require &armgoto/&armbring first"
        if reply:
            run_announce(response)
        return {"ok": False, "error": response}

    subcommand = args[0].lower()
    admin, _ = character_row(conn, resolved_admin)

    if subcommand in ("help", "?"):
        response = "gm: catalog, test, routes, mark, marks, recall, pos, dry, where, goto, bring, unstuck, item, kit, xp, tp, map, travel, dimension, patrol, sandworm, marker, vehicle, fly, ghost, walk; use &armgoto/&armbring before live online movement"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.help", "message": response}

    if subcommand in ("catalog", "commands"):
        data = gm_command_catalog.catalog()
        counts = {}
        for command in data["commands"]:
            counts[command["status"]] = counts.get(command["status"], 0) + 1
        wired = [command["name"] for command in data["commands"] if command["status"] in ("wired-preview", "gated-preview")]
        response = "gm catalog: " + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        response += "; wired: " + ", ".join(wired[:12])
        if len(wired) > 12:
            response += f", +{len(wired) - 12} more"
        response += "; full list: ./scripts/gm-command-catalog.py --format markdown"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.catalog", "message": response, "catalog": data}

    if subcommand == "test":
        response = "f00"
        announce_result = run_announce(response) if reply else None
        return {"ok": True, "action": "gm.test", "message": response, "reply": announce_result}

    if subcommand == "routes":
        if admin is None:
            response = f"admin player not found: {resolved_admin}"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for(conn, admin)
        response = f"gm route {route}; native execution {'enabled' if gm_execution_allowed() else 'gated'}; you are at {format_location(admin)}"
        if reply:
            run_announce(response)
        return {
            "ok": True,
            "action": "gm.routes",
            "message": response,
            "route": route,
            "executionAllowed": gm_execution_allowed(),
            "admin": compact_character(admin),
        }

    if subcommand == "mark":
        location_name = args[1] if len(args) > 1 else "location0"
        if admin is None:
            response = f"admin player not found: {resolved_admin}"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        try:
            location = save_gm_location(resolved_admin, location_name, admin)
        except ValueError as exc:
            response = str(exc)
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        response = f"marked {format_saved_location(location)}"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.mark", "message": response, "location": location}

    if subcommand in ("marks", "locations"):
        locations = list_gm_locations(resolved_admin)
        if locations:
            response = "marks: " + "; ".join(format_saved_location(location) for location in locations[:8])
        else:
            response = "no saved marks; use &gm mark [name]"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.marks", "message": response, "locations": locations}

    if subcommand in ("unmark", "delete-mark", "rm"):
        location_name = args[1] if len(args) > 1 else "location0"
        location = delete_gm_location(resolved_admin, location_name)
        response = f"deleted {format_saved_location(location)}" if location else f"no saved {location_name}"
        if reply:
            run_announce(response)
        return {"ok": bool(location), "action": "gm.unmark", "message": response, "location": location}

    if subcommand == "recall":
        location_name = args[1] if len(args) > 1 else "location0"
        location = load_gm_location(resolved_admin, location_name)
        if not location:
            response = f"no saved {location_name}; use &gm mark {location_name}"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for_saved_location(conn, location)
        command_text = f"TeleportToExact {location['x']:.3f} {location['y']:.3f} {location['z']:.3f}"
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        if gm_result.get("ok"):
            response = f"sent recall to {format_saved_location(location)} via {route}"
        else:
            response = f"recall ready for {format_saved_location(location)}; native GM execution is still gated"
        if reply:
            run_announce(response)
        return {
            "ok": bool(gm_result.get("ok")),
            "action": "gm.recall",
            "blocked": not bool(gm_result.get("ok")),
            "message": response,
            "location": location,
            "gm": gm_result,
        }

    if subcommand == "pos":
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        gm_result = send_gm_command("PrintPos", resolved_admin, resolved_admin, route)
        response = f"PrintPos {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.pos", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand == "dry":
        command_text = " ".join(args[1:]).strip()
        if not command_text:
            response = "usage: &gm dry <native command...>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        preview = gm_command_preview(command_text, resolved_admin, resolved_admin, route)
        response = f"dry native command {command_text} via {route}"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.dry", "message": response, "preview": preview}

    if subcommand == "where":
        if len(args) != 2:
            response = "usage: &gm where <playername>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        response = f"{target['character_name']} is {target['online_status']} at {format_location(target)}"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.where", "message": response, "target": compact_character(target)}

    if subcommand == "goto":
        if len(args) != 2:
            response = "usage: &gm goto <playername>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        if admin is None:
            response = f"admin player not found: {resolved_admin}"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        safe, safety_reason, admin_route, target_route = online_gm_teleport_safety(conn, "goto", admin, target)
        route = target_route or gm_route_for(conn, target)
        command_text = f"TeleportToPlayer {target['character_name']}"
        if safe:
            gm_result = send_online_gm_teleport("goto", command_text, resolved_admin, resolved_admin, route)
            response = f"goto {'sent' if gm_result.get('ok') else 'preview ready'} for {target['character_name']} via {route}"
        else:
            gm_result = {"ok": False, "blocked": True, "reason": safety_reason, "preview": gm_command_preview(command_text, resolved_admin, resolved_admin, route)}
            response = f"goto blocked for {target['character_name']}: {safety_reason}"
        if reply:
            run_announce(response)
        reason = safety_reason if not safe else "online GM teleport requires DUNE_ADMIN_GM_COMMANDS_ENABLED=true, DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true, and DUNE_CHAT_COMMAND_EXECUTE_ONLINE_GM_TELEPORT=true"
        return {"ok": bool(gm_result.get("ok")), "action": "gm.goto", "blocked": not bool(gm_result.get("ok")), "reason": reason, "message": response, "adminRoute": admin_route, "targetRoute": target_route, "target": compact_character(target), "gm": gm_result}

    if subcommand in ("bring", "summon"):
        if len(args) != 2:
            response = "usage: &gm bring <playername>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        if admin is None or admin.get("x") is None:
            response = f"admin location unavailable for {resolved_admin}"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        safe, safety_reason, admin_route, target_route = online_gm_teleport_safety(conn, "bring", admin, target)
        route = target_route or gm_route_for(conn, target)
        command_text = f"TeleportToExact {admin['x']:.3f} {admin['y']:.3f} {admin['z']:.3f}"
        if safe:
            gm_result = send_online_gm_teleport("bring", command_text, target["character_name"], resolved_admin, route)
            response = f"bring {'sent' if gm_result.get('ok') else 'preview ready'} for {target['character_name']} via {route}"
        else:
            gm_result = {"ok": False, "blocked": True, "reason": safety_reason, "preview": gm_command_preview(command_text, target["character_name"], resolved_admin, route)}
            response = f"bring blocked for {target['character_name']}: {safety_reason}"
        if reply:
            run_announce(response)
        reason = safety_reason if not safe else "online GM teleport requires DUNE_ADMIN_GM_COMMANDS_ENABLED=true, DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true, and DUNE_CHAT_COMMAND_EXECUTE_ONLINE_GM_TELEPORT=true"
        return {"ok": bool(gm_result.get("ok")), "action": "gm.bring", "blocked": not bool(gm_result.get("ok")), "reason": reason, "message": response, "adminRoute": admin_route, "targetRoute": target_route, "admin": compact_character(admin), "target": compact_character(target), "gm": gm_result}

    if subcommand == "unstuck":
        if len(args) not in (2, 3):
            response = "usage: &gm unstuck <playername> [mark]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        mark_name = args[2] if len(args) == 3 else "location0"
        location = load_gm_location(resolved_admin, mark_name)
        if location:
            route = gm_route_for(conn, target)
            command_text = f"TeleportToExact {location['x']:.3f} {location['y']:.3f} {location['z']:.3f}"
            destination = format_saved_location(location)
        elif admin and admin.get("x") is not None:
            route = gm_route_for(conn, target)
            command_text = f"TeleportToExact {admin['x']:.3f} {admin['y']:.3f} {admin['z']:.3f}"
            destination = f"{resolved_admin} at {format_location(admin)}"
        else:
            response = f"no saved {mark_name} and admin location unavailable"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        gm_result = send_gm_command(command_text, target["character_name"], resolved_admin, route)
        response = f"unstuck {'sent' if gm_result.get('ok') else 'preview ready'} for {target['character_name']} to {destination} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.unstuck", "blocked": not bool(gm_result.get("ok")), "message": response, "target": compact_character(target), "location": location, "gm": gm_result}

    if subcommand == "item":
        if len(args) not in (3, 4, 5):
            response = "usage: &gm item <playername> <template> [count] [quality]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        count = args[3] if len(args) >= 4 else "1"
        quality = args[4] if len(args) >= 5 else ""
        command_text = " ".join(part for part in ("AddItemToInventory", target["character_name"], args[2], count, quality) if part)
        route = gm_route_for(conn, target)
        gm_result = send_gm_command(command_text, target["character_name"], resolved_admin, route)
        response = f"item {'sent' if gm_result.get('ok') else 'preview ready'} for {target['character_name']} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.item", "blocked": not bool(gm_result.get("ok")), "message": response, "target": compact_character(target), "gm": gm_result}

    if subcommand == "kit":
        if len(args) not in (2, 3):
            response = "usage: &gm kit <playername> [basic]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        kit_name = args[2].lower() if len(args) == 3 else "basic"
        if kit_name != "basic":
            response = "only basic kit is wired right now"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        route = gm_route_for(conn, target)
        command_text = f"AddBasicInventoryToCharacter {target['character_name']}"
        gm_result = send_gm_command(command_text, target["character_name"], resolved_admin, route)
        response = f"basic kit {'sent' if gm_result.get('ok') else 'preview ready'} for {target['character_name']} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.kit", "blocked": not bool(gm_result.get("ok")), "message": response, "target": compact_character(target), "gm": gm_result}

    if subcommand == "xp":
        if len(args) not in (4, 5, 6):
            response = "usage: &gm xp <playername> <track> <amount> [add|set] [level]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        target, matches = character_row(conn, args[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches]}
        tracks = specialization_track_types(conn)
        track = args[2]
        if track not in tracks:
            response = "unknown track; valid tracks: " + ", ".join(tracks)
            if reply:
                run_announce(response)
            return {"ok": False, "error": response, "tracks": tracks}
        try:
            amount = int(args[3])
            mode = args[4] if len(args) >= 5 else "add"
            level = float(args[5]) if len(args) >= 6 else 0.0
        except ValueError:
            response = "usage: &gm xp <playername> <track> <amount> [add|set] [level]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        if mode not in ("add", "set"):
            response = "xp mode must be add or set"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        body = {
            "player_id": target["player_controller_id"],
            "track_type": track,
            "amount": amount,
            "level": level,
            "mode": mode,
        }
        response = f"xp preview ready for {target['character_name']}: {mode} {amount} {track}; use admin panel mutations to execute/audit"
        if reply:
            run_announce(response)
        return {"ok": True, "action": "gm.xp", "blocked": True, "message": response, "target": compact_character(target), "adminPanelApi": {"method": "POST", "path": "/api/admin/xp", "body": body}}

    if subcommand == "tp":
        if len(args) != 4:
            response = "usage: &gm tp <x> <y> <z>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        try:
            x, y, z = (float(args[1]), float(args[2]), float(args[3]))
        except ValueError:
            response = "usage: &gm tp <x> <y> <z>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        command_text = f"TeleportToExact {x:.3f} {y:.3f} {z:.3f}"
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        response = f"tp {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.tp", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand in ("map", "tomap"):
        if len(args) not in (2, 3):
            response = "usage: &gm map <map> [dimension]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        map_name = args[1]
        dimension = args[2] if len(args) == 3 else ""
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        command_text = " ".join(part for part in ("TeleportToMap", map_name, dimension) if part)
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        response = f"map teleport {'sent' if gm_result.get('ok') else 'preview ready'} to {map_name}{(' dim ' + dimension) if dimension else ''} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.map", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand in ("travel", "travelto"):
        if len(args) not in (2, 3):
            response = "usage: &gm travel <map> [location]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        command_text = " ".join(["TravelTo"] + args[1:])
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        response = f"travel {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.travel", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand in ("dimension", "dim"):
        if len(args) != 3:
            response = "usage: &gm dimension <map> <dimension>"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        command_text = f"TravelToDimension {args[1]} {args[2]}"
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        response = f"dimension travel {'sent' if gm_result.get('ok') else 'preview ready'} to {args[1]} dim {args[2]} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.dimension", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand in ("patrol", "patrolship"):
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        gm_result = send_gm_command("PatrolShipTeleportToNearest", resolved_admin, resolved_admin, route)
        response = f"patrol ship teleport {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.patrol", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand == "sandworm":
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        gm_result = send_gm_command("TeleportToSandworm", resolved_admin, resolved_admin, route)
        response = f"sandworm teleport {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.sandworm", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand in ("marker", "personalmarker"):
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        gm_result = send_gm_command("TeleportToPersonalMarker", resolved_admin, resolved_admin, route)
        response = f"personal marker teleport {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.marker", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand == "vehicle":
        if len(args) < 2:
            response = "usage: &gm vehicle <template> [args...]"
            if reply:
                run_announce(response)
            return {"ok": False, "error": response}
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        command_text = " ".join(["SpawnVehicle"] + args[1:])
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        response = f"vehicle spawn {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": "gm.vehicle", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    if subcommand in ("fly", "ghost", "walk"):
        route = gm_route_for(conn, admin) if admin else env("DUNE_GM_COMMAND_DEFAULT_ROUTE", "Survival_11")
        command_text = {"fly": "Fly", "ghost": "Ghost", "walk": "Walk"}[subcommand]
        gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
        response = f"{subcommand} {'sent' if gm_result.get('ok') else 'preview ready'} via {route}"
        if reply:
            run_announce(response)
        return {"ok": bool(gm_result.get("ok")), "action": f"gm.{subcommand}", "blocked": not bool(gm_result.get("ok")), "message": response, "gm": gm_result}

    response = f"unknown &gm command: {subcommand}"
    if reply:
        run_announce(response)
    return {"ok": False, "error": response}


def kick_candidate_routes(conn, target):
    route = gm_route_for(conn, target)
    return [
        {
            "name": "native-gm-session-command",
            "status": "rejected",
            "route": route,
            "candidateCommands": [
                f"PrintAllowedCommands",
                f"RemoveSessionMember {target['character_name']}",
                f"KickLobbyMember {target['character_name']}",
                f"BattlEyeMegaKick {target['character_name']}",
            ],
            "reason": "Ghidra and DedicatedServerGame.ini show these are not shipped dedicated-server GM kick commands for this build.",
        },
        {
            "name": "map-service-restart",
            "status": "available-but-not-targeted",
            "route": route,
            "reason": "Restarting the owning map can disconnect this player, but it disconnects every player on that map and is not a targeted kick.",
        },
        {
            "name": "database-online-state-write",
            "status": "rejected",
            "reason": "Postgres player_state is not the live network socket; blind writes can desync or be overwritten by the running map server.",
        },
        {
            "name": "network-block-by-client-ip",
            "status": "blocked",
            "reason": "The current DB/log surfaces do not reliably map character names to client IPs or UDP session handles.",
        },
    ]


def run_announce(message, target_name="", target_fls_id=""):
    global LAST_ANNOUNCE_RESULT
    if not target_name or not target_fls_id:
        inferred_name, inferred_fls_id = infer_command_reply_target()
        target_name = target_name or inferred_name
        target_fls_id = target_fls_id or inferred_fls_id
    command = env("DUNE_CHAT_COMMAND_REPLY_COMMAND", env("DUNE_ADMIN_ANNOUNCE_COMMAND", "/workspace/scripts/announce_pika.py"))
    if command.startswith("/workspace/") and not pathlib.Path(command).exists():
        command = str(ROOT / command.removeprefix("/workspace/"))
    wrapped = f"[Paul] {message}"
    child_env = os.environ.copy()
    cwd = ROOT
    target_reply_mode = env("DUNE_CHAT_COMMAND_TARGET_REPLY_MODE", "").strip().lower()
    if not target_reply_mode and env_bool("DUNE_CHAT_COMMAND_PRIVATE_REPLIES_ENABLED", False):
        target_reply_mode = "whisper"
    if target_reply_mode in ("whisper", "private") and (not target_name or not target_fls_id):
        LAST_ANNOUNCE_RESULT = {
            "ok": False,
            "skipped": True,
            "reason": "private command reply target unavailable; refusing public fallback",
            "stdout": "",
            "stderr": "",
        }
        return LAST_ANNOUNCE_RESULT
    if target_name and target_fls_id and target_reply_mode in ("whisper", "private"):
        whisper_route = whisper_route_for_fls_id(target_fls_id)
        child_env["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"] = "true"
        child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"] = env("DUNE_CHAT_COMMAND_PRIVATE_REPLY_EXCHANGE", "chat.whispers")
        child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"] = env("DUNE_CHAT_COMMAND_PRIVATE_REPLY_CHANNEL", "Whispers")
        child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = target_name
        child_env["DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS"] = whisper_route["routingKey"]
        child_env["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"] = whisper_route["queue"]
        child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"] = env("DUNE_CHAT_COMMAND_PRIVATE_REPLY_ROUTING_KEY", whisper_route["routingKey"]) or whisper_route["routingKey"]
        child_env["DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES"] = "false"
        child_env["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"] = "true"
        child_env["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"] = "false"
        cwd = "/tmp"
    elif target_name and target_fls_id and target_reply_mode in ("proximity", "prox"):
        child_env["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"] = "true"
        child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"] = env("DUNE_CHAT_COMMAND_TARGET_REPLY_EXCHANGE", "chat.proximity")
        child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"] = env("DUNE_CHAT_COMMAND_TARGET_REPLY_CHANNEL", "Proximity")
        child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = target_name
        child_env["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"] = f"{target_fls_id}_queue"
        child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"] = env("DUNE_CHAT_COMMAND_TARGET_REPLY_ROUTING_KEY", f"dash.chat-command-reply.{target_fls_id}")
        child_env["DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES"] = "false"
        child_env["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"] = "true"
        child_env["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"] = "false"
        cwd = "/tmp"
    elif target_name and target_fls_id and target_reply_mode == "guild":
        child_env["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"] = "true"
        child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"] = env("DUNE_CHAT_COMMAND_TARGET_REPLY_EXCHANGE", "chat.guild.1")
        child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"] = env("DUNE_CHAT_COMMAND_TARGET_REPLY_CHANNEL", "Guild")
        child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = target_name
        child_env["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"] = f"{target_fls_id}_queue"
        child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"] = env("DUNE_CHAT_COMMAND_TARGET_REPLY_ROUTING_KEY", "<empty>")
        child_env["DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES"] = "false"
        child_env["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"] = "true"
        child_env["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"] = "false"
        cwd = "/tmp"
    elif target_name:
        child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = target_name
    result = subprocess.run(
        [command, wrapped],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(env("DUNE_CHAT_COMMAND_REPLY_TIMEOUT_SECONDS", "10")),
        check=False,
        env=child_env,
    )
    LAST_ANNOUNCE_RESULT = {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    return LAST_ANNOUNCE_RESULT


def maybe_reply(message, reply=False, target_name="", target_fls_id=""):
    return run_announce(message, target_name=target_name, target_fls_id=target_fls_id) if reply else None


def infer_command_reply_target():
    frame = inspect.currentframe()
    if frame is None:
        return "", ""
    frame = frame.f_back
    while frame:
        if frame.f_code.co_name == "handle_command":
            sender_fls_id = frame.f_locals.get("sender_fls_id") or ""
            if not sender_fls_id:
                return "", ""
            sender_name = frame.f_locals.get("sender_name") or ""
            resolved_admin = frame.f_locals.get("resolved_admin") or ""
            return resolved_admin or sender_name, sender_fls_id
        frame = frame.f_back
    return "", ""


def normalize_chat_text(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def spam_protect_enabled():
    return env_bool("DUNE_CHAT_SPAM_PROTECT_ENABLED", True)


def spam_exempt(conn, sender_name, sender_fls_id):
    if env_bool("DUNE_CHAT_SPAM_PROTECT_EXEMPT_ADMINS", True):
        allowed, _ = is_admin(conn, sender_name, sender_fls_id)
        if allowed:
            return True
    exempt_names = {item.lower() for item in split_csv(env("DUNE_CHAT_SPAM_EXEMPT_NAMES", ""))}
    exempt_fls_ids = set(split_csv(env("DUNE_CHAT_SPAM_EXEMPT_FLS_IDS", "")))
    resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
    return bool((resolved_name and resolved_name.lower() in exempt_names) or (sender_fls_id and sender_fls_id in exempt_fls_ids))


def spam_state_key(sender_name, sender_fls_id):
    return sender_fls_id or sender_name or "unknown"


def spam_violation(state, normalized, now):
    consecutive_limit = int(env("DUNE_CHAT_SPAM_SAME_CONSECUTIVE_LIMIT", "3"))
    window_limit = int(env("DUNE_CHAT_SPAM_SAME_WINDOW_LIMIT", "5"))
    window_seconds = float(env("DUNE_CHAT_SPAM_SAME_WINDOW_SECONDS", "30"))

    if state.get("lastText") == normalized:
        state["consecutive"] = int(state.get("consecutive", 0)) + 1
    else:
        state["lastText"] = normalized
        state["consecutive"] = 1

    recent = state.setdefault("recent", collections.defaultdict(collections.deque))
    hits = recent[normalized]
    hits.append(now)
    while hits and now - hits[0] > window_seconds:
        hits.popleft()

    if state["consecutive"] > consecutive_limit:
        return {
            "type": "consecutive-repeat",
            "count": state["consecutive"],
            "limit": consecutive_limit,
            "windowSeconds": window_seconds,
        }
    if len(hits) >= window_limit:
        return {
            "type": "window-repeat",
            "count": len(hits),
            "limit": window_limit,
            "windowSeconds": window_seconds,
        }
    return None


def run_spam_kick_command(player_name, sender_fls_id, reason, text):
    command_template = env("DUNE_CHAT_SPAM_KICK_COMMAND", "")
    if not command_template:
        return {"ok": False, "blocked": True, "reason": "DUNE_CHAT_SPAM_KICK_COMMAND is not configured"}
    mapping = {
        "player": player_name,
        "character_name": player_name,
        "fls_id": sender_fls_id,
        "reason": reason,
        "message": text,
    }
    command = [part.format(**mapping) for part in shlex.split(command_template)]
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(env("DUNE_CHAT_SPAM_KICK_TIMEOUT_SECONDS", "10")),
        check=False,
    )
    return {
        "ok": result.returncode == 0,
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def enforce_spam_protect(conn, text, sender_name="", sender_fls_id=""):
    if not spam_protect_enabled():
        return None
    normalized = normalize_chat_text(text)
    min_len = int(env("DUNE_CHAT_SPAM_MIN_MESSAGE_LENGTH", "1"))
    if len(normalized) < min_len:
        return None
    if spam_exempt(conn, sender_name, sender_fls_id):
        return None

    now = time.time()
    key = spam_state_key(sender_name, sender_fls_id)
    state = SPAM_STATE.setdefault(key, {})
    cooldown = float(env("DUNE_CHAT_SPAM_KICK_COOLDOWN_SECONDS", "300"))
    if now - float(state.get("lastKickAt", 0)) < cooldown:
        return None

    violation = spam_violation(state, normalized, now)
    if not violation:
        return None

    state["lastKickAt"] = now
    player_name = resolve_sender_character(conn, sender_name, sender_fls_id)
    reason = f"chat spam: {violation['type']} {violation['count']}/{violation['limit']}"
    kick_result = run_spam_kick_command(player_name, sender_fls_id, reason, text)
    announce = None
    if env_bool("DUNE_CHAT_SPAM_ANNOUNCE_ACTION", True):
        if kick_result.get("ok"):
            announce = run_announce(f"{player_name} was kicked for repeated chat spam")
        else:
            announce = run_announce(f"{player_name} triggered spam auto-kick, but kick backend is not configured")
    return {
        "ok": bool(kick_result.get("ok")),
        "action": "spam-protect",
        "player": player_name,
        "senderFlsId": sender_fls_id,
        "violation": violation,
        "message": text,
        "kick": kick_result,
        "announce": announce,
    }


def resolve_player_for_self_command(conn, sender_name="", sender_fls_id="", action="command"):
    resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
    player, matches = character_row(conn, resolved_name)
    if player is None:
        if matches:
            raise ValueError("no unique character match: " + ", ".join(row["character_name"] for row in matches))
        raise ValueError(f"could not resolve your character for {action}")
    return resolved_name, player


def handle_dd1_backup_command(conn, args, sender_name="", sender_fls_id="", sender_origin=None, reply=False):
    resolved_name = sender_name
    try:
        resolved_name, player = resolve_player_for_self_command(conn, sender_name, sender_fls_id, action="DD1 backup")
        current_location = dd1_current_location_from_sender(player, sender_origin=sender_origin, action="DD1 backup")
        totem, _ = select_dd1_totem(conn, player, args, command_name="DD1_backup", current_location=current_location)
        dry_run = not chat_dd1_backup_enabled()
        result = dd1_create_backup(conn, player, totem, dry_run=dry_run, current_location=current_location)
        response = format_dd1_backup_result(result)
        ok = bool(result.get("ok"))
    except Exception as exc:
        rollback_quietly(conn)
        result = {"ok": False, "error": str(exc)}
        response = f"DD1 backup failed: {exc}"
        ok = False
    announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
    return result | {"ok": ok, "action": "dd1_backup", "message": response, "reply": announce_result}


def parse_dd1_restore_args(args):
    if not args:
        return "preview", []
    first = args[0].lower()
    if first in ("yes", "y", "confirm"):
        return "confirm", args[1:]
    if first in ("no", "n", "cancel"):
        return "cancel", args[1:]
    if first in ("preview", "dry", "dry-run"):
        return "preview", args[1:]
    return "preview", args


def handle_dd1_restore_command(conn, args, sender_name="", sender_fls_id="", sender_origin=None, reply=False):
    mode, rest = parse_dd1_restore_args(args)
    resolved_name = sender_name
    try:
        resolved_name, player = resolve_player_for_self_command(conn, sender_name, sender_fls_id, action="DD1 restore")
        confirm_key = dd1_restore_confirmation_key(sender_name, sender_fls_id, resolved_name)

        if mode == "cancel":
            DD1_RESTORE_CONFIRMATIONS.pop(confirm_key, None)
            response = "DD1 restore cancelled"
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return {"ok": True, "action": "dd1_restore.cancel", "message": response, "reply": announce_result}

        if mode == "confirm":
            if rest:
                raise ValueError("usage: &DD1_restore yes")
            pending = DD1_RESTORE_CONFIRMATIONS.get(confirm_key)
            now = int(time.time())
            if not pending or now > int(pending.get("expiresAt") or 0):
                DD1_RESTORE_CONFIRMATIONS.pop(confirm_key, None)
                raise ValueError("no pending DD1 restore preview; run &DD1_restore [backup_id] first")
            current_target = dd1_restore_target_from_sender(player, sender_origin=sender_origin)
            position_ok, distance = dd1_restore_confirm_position_ok(pending, current_target)
            if not position_ok:
                raise ValueError(f"you moved {distance:.1f}cm since preview; run &DD1_restore again at the target")
            dry_run = chat_dd1_restore_dry_run() or not chat_dd1_restore_enabled()
            if not dry_run and not chat_dd1_restore_execution_allowed(player, sender_fls_id=sender_fls_id):
                raise RuntimeError(
                    "DD1 restore execution is blocked: sender is not in "
                    "DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ALLOWED_FLS_IDS or "
                    "DUNE_CHAT_COMMAND_DD1_BRT_RESTORE_ALLOWED_PLAYER_IDS"
                )
            result = dd1_restore_backup_to_location(conn, pending["plan"], dry_run=dry_run)
            if not dry_run:
                conn.commit()
            DD1_RESTORE_CONFIRMATIONS.pop(confirm_key, None)
            if result.get("dryRun"):
                response = (
                    f"DD1 restore preview confirmed for backup {pending['plan']['backupId']}; "
                    "execution is still dry-run or disabled"
                )
            else:
                response = f"DD1 restore executed for backup {pending['plan']['backupId']}"
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return result | {"ok": bool(result.get("ok")), "action": "dd1_restore.confirm", "message": response, "reply": announce_result}

        backup, _ = select_dd1_backup(conn, player, rest)
        target = dd1_restore_target_from_sender(player, sender_origin=sender_origin)
        plan = dd1_restore_plan(conn, player, backup, target)
        expires = int(time.time()) + dd1_restore_confirmation_ttl()
        DD1_RESTORE_CONFIRMATIONS[confirm_key] = {
            "expiresAt": expires,
            "backupId": int(backup["id"]),
            "target": target,
            "plan": plan,
        }
        response = format_dd1_restore_preview(plan)
        announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
        return {"ok": True, "action": "dd1_restore.preview", "message": response, "plan": plan, "expiresAt": expires, "reply": announce_result}
    except Exception as exc:
        rollback_quietly(conn)
        response = f"DD1 restore failed: {exc}"
        announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
        return {"ok": False, "action": "dd1_restore", "error": str(exc), "message": response, "reply": announce_result}


def handle_command(conn, command_text, sender_name="", sender_fls_id="", reply=False, sender_origin=None):
    prefix = env("DUNE_CHAT_COMMAND_PREFIX", "&")
    if not command_text.startswith(prefix):
        return {"ok": True, "ignored": True}
    try:
        parts = shlex.split(command_text[len(prefix):])
    except ValueError as exc:
        return {"ok": False, "error": f"bad command syntax: {exc}"}
    if not parts:
        return {"ok": True, "ignored": True}
    if not sender_fls_id and sender_name:
        sender_fls_id = fls_id_for_sender(conn, sender_name)

    command = parts[0].lower()
    if command in ("list", "players", "online"):
        players = online_player_list(conn)
        response = format_online_player_list(players)
        announce_result = maybe_reply(response, reply, target_name=sender_name, target_fls_id=sender_fls_id)
        return {"ok": True, "action": "list", "message": response, "players": [dict(row) for row in players], "reply": announce_result}

    if command in ("inv_list", "inventory", "inv"):
        resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
        player, matches = character_row(conn, resolved_name)
        if player is None:
            response = "could not resolve your character for inventory list"
            if matches:
                response = "no unique character match: " + ", ".join(row["character_name"] for row in matches)
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        rows, response = inventory_list_for_player(conn, player)
        announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
        return {"ok": True, "action": "inv_list", "message": response, "items": [dict(row) for row in rows], "reply": announce_result}

    if command in ("exchange_list", "auction_list", "my_auctions", "sales"):
        try:
            limit = parse_exchange_list_limit(parts[1:])
        except ValueError as exc:
            response = str(exc)
            announce_result = maybe_reply(response, reply, target_name=sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
        player, matches = character_row(conn, resolved_name)
        if player is None:
            response = "could not resolve your character for exchange list"
            if matches:
                response = "no unique character match: " + ", ".join(row["character_name"] for row in matches)
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        rows, total = player_exchange_list_rows(conn, player, limit)
        response = format_player_exchange_list(rows, total)
        announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
        return {"ok": True, "action": "exchange_list", "message": response, "orders": [dict(row) for row in rows], "total": total, "reply": announce_result}

    if command in ("exchange_cashout", "cashout"):
        if len(parts) > 1:
            response = "usage: &exchange_cashout"
            announce_result = maybe_reply(response, reply, target_name=sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
        player, matches = character_row(conn, resolved_name)
        if player is None:
            response = "could not resolve your character for exchange cashout"
            if matches:
                response = "no unique character match: " + ", ".join(row["character_name"] for row in matches)
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        dry_run = not chat_exchange_cashout_enabled()
        try:
            result = player_exchange_cashout(conn, player, dry_run=dry_run)
            response = format_exchange_cashout(result)
            ok = bool(result.get("ok"))
        except Exception as exc:
            conn.rollback()
            result = {"ok": False, "error": str(exc), "dryRun": dry_run}
            response = f"exchange cashout failed: {exc}"
            ok = False
        announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
        return result | {"ok": ok, "action": "exchange_cashout", "message": response, "reply": announce_result}

    if command == "auction":
        if len(parts) == 1:
            resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
            player, matches = character_row(conn, resolved_name)
            if player is None:
                inventory_response = "could not resolve your character for inventory list"
                if matches:
                    inventory_response = "no unique character match: " + ", ".join(row["character_name"] for row in matches)
                response = f"{auction_usage_message()}; {inventory_response}"
                announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
                return {"ok": False, "error": inventory_response, "message": response, "reply": announce_result}
            rows, inventory_response = inventory_list_for_player(conn, player)
            response = f"{auction_usage_message()}; {inventory_response}"
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return {"ok": True, "action": "auction.help", "message": response, "items": [dict(row) for row in rows], "reply": announce_result}

        if len(parts) == 2 and parts[1].lower() in ("yes", "y", "no", "n"):
            resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
            confirm_key = auction_confirmation_key(sender_name, sender_fls_id, resolved_name)
            pending = AUCTION_CONFIRMATIONS.get(confirm_key)
            if not pending or int(time.time()) > int(pending.get("expiresAt", 0)):
                AUCTION_CONFIRMATIONS.pop(confirm_key, None)
                response = "no pending auction suggestion"
                announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
                return {"ok": False, "error": response, "reply": announce_result}
            if parts[1].lower() in ("no", "n"):
                AUCTION_CONFIRMATIONS.pop(confirm_key, None)
                response = "auction suggestion cancelled"
                announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
                return {"ok": True, "action": "auction.cancelled", "message": response, "reply": announce_result}
            player, matches = character_row(conn, resolved_name)
            if player is None:
                response = "could not resolve your character for auction"
                if matches:
                    response = "no unique character match: " + ", ".join(row["character_name"] for row in matches)
                announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
                return {"ok": False, "error": response, "reply": announce_result}
            dry_run = not chat_auction_enabled()
            result = auction_item(
                conn,
                player,
                pending["searchText"],
                int(pending["count"]),
                int(pending["price"]),
                dry_run=dry_run,
                source=pending["source"],
                explicit_inventory_id=pending.get("explicitInventoryId"),
                explicit_item_id=pending["itemId"],
            )
            AUCTION_CONFIRMATIONS.pop(confirm_key, None)
            if result.get("ok"):
                plan = result.get("plan") or {}
                source_inventory = plan.get("sourceInventoryId")
                if dry_run:
                    source_label = f" from inventory {source_inventory}" if plan.get("source") != "personal" and source_inventory else ""
                    response = f"auction preview: {plan.get('count')}x {plan.get('templateId')}{source_label} for {plan.get('price')}; enable DUNE_CHAT_COMMAND_AUCTION_ENABLED to execute"
                else:
                    order = result.get("order") or {}
                    source_label = f" from inventory {source_inventory}" if source_inventory else ""
                    response = f"auction listed {plan.get('count')}x {plan.get('templateId')}{source_label} for {plan.get('price')}; order {order.get('id')}"
            else:
                response = result.get("error", "auction failed")
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return result | {"message": response, "dryRun": dry_run, "reply": announce_result}

        try:
            source, explicit_inventory_id, explicit_item_id, search_text, count, price = parse_auction_command_args(parts[1:])
        except ValueError as exc:
            response = str(exc)
            announce_result = maybe_reply(response, reply, target_name=sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        resolved_name = resolve_sender_character(conn, sender_name, sender_fls_id)
        player, matches = character_row(conn, resolved_name)
        if player is None:
            response = "could not resolve your character for auction"
            if matches:
                response = "no unique character match: " + ", ".join(row["character_name"] for row in matches)
            announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
            return {"ok": False, "error": response, "reply": announce_result}
        dry_run = not chat_auction_enabled()
        result = auction_item(conn, player, search_text, count, price, dry_run=dry_run, source=source, explicit_inventory_id=explicit_inventory_id, explicit_item_id=explicit_item_id)
        if result.get("ok"):
            plan = result.get("plan") or {}
            source_inventory = plan.get("sourceInventoryId")
            if dry_run:
                source_label = f" from inventory {source_inventory}" if plan.get("source") != "personal" and source_inventory else ""
                response = f"auction preview: {count}x {plan.get('templateId')}{source_label} for {price}; enable DUNE_CHAT_COMMAND_AUCTION_ENABLED to execute"
            else:
                order = result.get("order") or {}
                source_label = f" from inventory {source_inventory}" if source_inventory else ""
                response = f"auction listed {count}x {plan.get('templateId')}{source_label} for {price}; order {order.get('id')}"
        else:
            response = result.get("error", "auction failed")
            suggestion = result.get("suggestion")
            if suggestion and explicit_item_id is None:
                confirm_key = auction_confirmation_key(sender_name, sender_fls_id, resolved_name)
                AUCTION_CONFIRMATIONS[confirm_key] = {
                    "itemId": int(suggestion["itemId"]),
                    "templateId": suggestion["templateId"],
                    "inventoryId": int(suggestion["inventoryId"]),
                    "source": source,
                    "explicitInventoryId": explicit_inventory_id,
                    "searchText": search_text,
                    "count": count,
                    "price": price,
                    "expiresAt": int(time.time()) + auction_confirmation_ttl(),
                }
                response = (
                    f"no exact match for '{search_text}'. did you mean {suggestion['templateId']} "
                    f"from inventory {suggestion['inventoryId']}? reply &auction yes or &auction no"
                )
        announce_result = maybe_reply(response, reply, target_name=resolved_name or sender_name, target_fls_id=sender_fls_id)
        return result | {"message": response, "dryRun": dry_run, "reply": announce_result}

    if command in ("dd1_backup", "dd1backup", "dd_backup"):
        return handle_dd1_backup_command(
            conn,
            parts[1:],
            sender_name=sender_name,
            sender_fls_id=sender_fls_id,
            sender_origin=sender_origin,
            reply=reply,
        )

    if command in ("dd1_restore", "dd1restore", "dd_restore"):
        return handle_dd1_restore_command(
            conn,
            parts[1:],
            sender_name=sender_name,
            sender_fls_id=sender_fls_id,
            sender_origin=sender_origin,
            reply=reply,
        )

    allowed, resolved_admin = is_admin(conn, sender_name, sender_fls_id)
    if not allowed:
        response = f"command denied for {resolved_admin or sender_name or sender_fls_id or 'unknown'}"
        announce_result = maybe_reply(response, reply)
        return {"ok": False, "error": response, "reply": announce_result}

    if command == "test":
        response = "f00"
        announce_result = run_announce(response) if reply else None
        return {"ok": True, "action": "test", "message": response, "reply": announce_result}

    if command == "gm":
        global LAST_ANNOUNCE_RESULT
        LAST_ANNOUNCE_RESULT = None
        result = handle_gm_command(conn, parts[1:], resolved_admin, reply=reply)
        if reply and isinstance(result, dict) and "reply" not in result:
            result = result | {"reply": LAST_ANNOUNCE_RESULT}
        return result

    if command in ("where", "loc", "location"):
        if len(parts) != 2:
            response = "usage: &where <playername>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        target, matches = character_row(conn, parts[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}
        response = f"{target['character_name']} is {target['online_status']} at {format_location(target)}"
        announce_result = maybe_reply(response, reply)
        return {"ok": True, "action": "where", "message": response, "target": dict(target), "reply": announce_result}

    if command in ("kick", "disconnect", "sessionkick"):
        if len(parts) != 2:
            response = "usage: &disconnect <playername>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        target, matches = character_row(conn, parts[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}

        online = (target["online_status"] or "").lower() == "online"
        route = gm_route_for(conn, target)
        if online:
            try:
                command_text = player_disconnect_command(target["character_name"])
                gm_result = send_player_disconnect(command_text, target["character_name"], resolved_admin, route)
                sent = bool(gm_result.get("ok"))
                response = f"{target['character_name']} disconnect {'sent' if sent else 'preview ready'} via {route} using {command_text.split()[0]}"
                ok = sent
                reason = "disconnect sent" if sent else "player disconnect execution is gated or payload route is not verified"
            except ValueError as exc:
                command_text = ""
                gm_result = {"ok": False, "blocked": True, "error": str(exc)}
                response = str(exc)
                ok = False
                reason = str(exc)
        else:
            response = f"{target['character_name']} is {target['online_status']}; no live session to kick"
            ok = True
            reason = "target is not online"
            command_text = ""
            gm_result = None
        announce_result = maybe_reply(response, reply)
        return {
            "ok": ok,
            "action": "disconnect",
            "blocked": not ok,
            "reason": reason,
            "message": response,
            "commandText": command_text,
            "gm": gm_result,
            "target": compact_character(target),
            "candidateRoutes": kick_candidate_routes(conn, target),
            "reply": announce_result,
        }

    if command == "teleport":
        if len(parts) < 2:
            response = "usage: &teleport <playername>|<slot>|list|set <slot> [name]|replace <slot> [name]|delete <slot>|<playername> <slot>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}

        subcommand = parts[1].lower()
        if subcommand in ("list", "locations"):
            locations = list_teleport_slots()
            response = "teleport slots: " + "; ".join(format_teleport_slot(location) for location in locations[:12]) if locations else "no teleport slots; use &teleport set 0 <name>"
            announce_result = maybe_reply(response, reply)
            return {"ok": True, "action": "teleport.list", "message": response, "locations": locations, "reply": announce_result}

        if subcommand in ("set", "replace"):
            if len(parts) < 3:
                response = f"usage: &teleport {subcommand} <slot> [name]"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            try:
                slot = parse_teleport_slot(parts[2])
            except ValueError as exc:
                response = str(exc)
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            admin, _ = character_row(conn, resolved_admin)
            if admin is None or admin["partition_id"] is None or admin["x"] is None:
                response = f"admin location unavailable for {resolved_admin}"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            name = " ".join(parts[3:]).strip() or None
            location, existing, next_slot = save_teleport_slot(slot, name, admin, resolved_admin, replace=(subcommand == "replace"))
            if existing:
                response = f"{format_teleport_slot_collision(existing)}; next free slot is {next_slot}; use &teleport set {next_slot} or &teleport replace {slot}"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "action": "teleport.set", "error": response, "location": existing, "nextFreeSlot": next_slot, "reply": announce_result}
            response = f"{'replaced' if subcommand == 'replace' else 'saved'} {format_teleport_slot(location)}"
            announce_result = maybe_reply(response, reply)
            return {"ok": True, "action": f"teleport.{subcommand}", "message": response, "location": location, "reply": announce_result}

        if subcommand in ("delete", "rm"):
            if len(parts) != 3:
                response = f"usage: &teleport {subcommand} <slot>"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            try:
                slot = parse_teleport_slot(parts[2])
            except ValueError as exc:
                response = str(exc)
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            location = delete_teleport_slot(slot)
            response = f"deleted {format_teleport_slot(location)}" if location else f"no saved slot {slot}"
            announce_result = maybe_reply(response, reply)
            return {"ok": bool(location), "action": "teleport.delete", "message": response, "location": location, "reply": announce_result}

        if len(parts) == 2:
            try:
                slot = parse_teleport_slot(parts[1])
            except ValueError:
                slot = None
            if slot is not None:
                location = load_teleport_slot(slot)
                if not location:
                    response = f"no saved slot {slot}; use &teleport set {slot} [name]"
                    announce_result = maybe_reply(response, reply)
                    return {"ok": False, "error": response, "reply": announce_result}
                route = gm_route_for_saved_location(conn, location)
                command_text = f"TeleportToExact {location['x']:.3f} {location['y']:.3f} {location['z']:.3f}"
                gm_result = send_gm_command(command_text, resolved_admin, resolved_admin, route)
                response = f"teleport slot {'sent' if gm_result.get('ok') else 'preview ready'} for {format_teleport_slot(location)} via {route}"
                announce_result = maybe_reply(response, reply)
                return {"ok": bool(gm_result.get("ok")), "action": "teleport.slot", "blocked": not bool(gm_result.get("ok")), "message": response, "location": location, "gm": gm_result, "reply": announce_result}

        if len(parts) == 3:
            try:
                slot = parse_teleport_slot(parts[2])
            except ValueError:
                response = "usage: &teleport <playername> <slot>"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            location = load_teleport_slot(slot)
            if not location:
                response = f"no saved slot {slot}; use &teleport set {slot} [name]"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "reply": announce_result}
            target, matches = character_row(conn, parts[1])
            if target is None:
                response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}
            if target["online_status"].lower() != "offline":
                response = f"{target['character_name']} is {target['online_status']}; safe teleport only supports offline targets right now"
                announce_result = maybe_reply(response, reply)
                return {"ok": False, "error": response, "target": dict(target), "reply": announce_result}
            execute = env_bool("DUNE_CHAT_COMMAND_EXECUTE_TELEPORT", False)
            dry_run = env_bool("DUNE_CHAT_COMMAND_DRY_RUN", True) or not execute
            move_result, teleport_error = chat_guarded_offline_teleport(
                conn, target, location["partitionId"],
                location["x"], location["y"], location["z"], execute=not dry_run,
            )
            if teleport_error:
                announce_result = maybe_reply(teleport_error, reply)
                return {"ok": False, "blocked": True, "action": "teleport.player-slot", "dryRun": dry_run, "error": teleport_error, "moveResult": move_result, "location": location, "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)}, "reply": announce_result}
            response = f"would move {target['character_name']} to {format_teleport_slot(location)} through the guarded Admin contract"
            if not dry_run:
                response = f"moved {target['character_name']} to {format_teleport_slot(location)} via dune.admin_move_offline_player_to_partition"
            announce_result = maybe_reply(response, reply)
            return {"ok": bool(move_result.get("ok")), "action": "teleport.player-slot", "dryRun": dry_run, "message": response, "moveResult": move_result, "location": location, "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)}, "reply": announce_result}

        if len(parts) != 2:
            response = "usage: &teleport <playername>|<slot>|list|set <slot> [name]|replace <slot> [name]|delete <slot>|<playername> <slot>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}

        admin, _ = character_row(conn, resolved_admin)
        if admin is None or admin["partition_id"] is None or admin["x"] is None:
            response = f"admin location unavailable for {resolved_admin}"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        target, matches = character_row(conn, parts[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}
        if target["online_status"].lower() != "offline":
            response = f"{target['character_name']} is {target['online_status']}; safe teleport only supports offline targets right now"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "target": dict(target), "reply": announce_result}

        execute = env_bool("DUNE_CHAT_COMMAND_EXECUTE_TELEPORT", False)
        dry_run = env_bool("DUNE_CHAT_COMMAND_DRY_RUN", True) or not execute
        move_result, teleport_error = chat_guarded_offline_teleport(
            conn, target, admin["partition_id"], admin["x"], admin["y"], admin["z"],
            execute=not dry_run,
        )
        if teleport_error:
            announce_result = maybe_reply(teleport_error, reply)
            return {"ok": False, "blocked": True, "action": "teleport", "dryRun": dry_run, "error": teleport_error, "moveResult": move_result, "admin": {"characterName": resolved_admin, "location": compact_location(admin)}, "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)}, "reply": announce_result}
        response = f"would move {target['character_name']} to {resolved_admin} at {format_location(admin)} through the guarded Admin contract"
        if not dry_run:
            response = f"moved {target['character_name']} to {resolved_admin} at {format_location(admin)} via dune.admin_move_offline_player_to_partition"
        announce_result = maybe_reply(response, reply)
        return {
            "ok": bool(move_result.get("ok")),
            "action": "teleport",
            "dryRun": dry_run,
            "message": response,
            "moveResult": move_result,
            "admin": {"characterName": resolved_admin, "location": compact_location(admin)},
            "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)},
            "reply": announce_result,
        }

    if command in ("armgoto", "armbring"):
        if len(parts) != 2:
            response = f"usage: &{command} <playername>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        action = "goto" if command == "armgoto" else "bring"
        admin, _ = character_row(conn, resolved_admin)
        target, matches = character_row(conn, parts[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}
        safe, safety_reason, admin_route, target_route = online_gm_teleport_safety(conn, action, admin, target)
        route = target_route or gm_route_for(conn, target)
        if action == "goto":
            command_text = f"TeleportToPlayer {target['character_name']}"
            target_player = resolved_admin
        else:
            command_text = f"TeleportToExact {admin['x']:.3f} {admin['y']:.3f} {admin['z']:.3f}" if admin and admin.get("x") is not None else "TeleportToExact <admin-x> <admin-y> <admin-z>"
            target_player = target["character_name"]
        if not safe:
            response = f"arm {action} blocked for {target['character_name']}: {safety_reason}"
            announce_result = maybe_reply(response, reply)
            return {
                "ok": False,
                "action": f"arm.{action}",
                "blocked": True,
                "reason": safety_reason,
                "message": response,
                "adminRoute": admin_route,
                "targetRoute": target_route,
                "preview": gm_command_preview(command_text, target_player, resolved_admin, route),
                "reply": announce_result,
            }
        arm = arm_online_gm_teleport(action, resolved_admin, target_player, route, command_text)
        response = f"armed {action} for {target['character_name']} via {route} for {online_gm_teleport_arm_seconds()}s; run &{action} {target['character_name']} to execute once"
        announce_result = maybe_reply(response, reply)
        return {
            "ok": True,
            "action": f"arm.{action}",
            "message": response,
            "arm": arm,
            "adminRoute": admin_route,
            "targetRoute": target_route,
            "preview": gm_command_preview(command_text, target_player, resolved_admin, route),
            "reply": announce_result,
        }

    if command in ("goto", "teleportto", "tpto"):
        if len(parts) != 2:
            response = "usage: &goto <playername>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        admin, _ = character_row(conn, resolved_admin)
        if admin is None:
            response = f"admin player not found: {resolved_admin}"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        target, matches = character_row(conn, parts[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}
        if target["online_status"].lower() == "online":
            safe, safety_reason, admin_route, target_route = online_gm_teleport_safety(conn, "goto", admin, target)
            route = target_route or gm_route_for(conn, target)
            command_text = f"TeleportToPlayer {target['character_name']}"
            if safe:
                gm_result = send_online_gm_teleport("goto", command_text, resolved_admin, resolved_admin, route)
            else:
                gm_result = {"ok": False, "blocked": True, "reason": safety_reason, "preview": gm_command_preview(command_text, resolved_admin, resolved_admin, route)}
            if gm_result.get("ok"):
                response = f"sent native GM goto for {resolved_admin} to {target['character_name']} via {route}"
            elif not safe:
                response = f"online goto blocked for {target['character_name']}: {safety_reason}"
            else:
                response = f"{target['character_name']} is online at {format_location(target)}; live goto GM envelope is still gated"
            announce_result = maybe_reply(response, reply)
            return {
                "ok": bool(gm_result.get("ok")),
                "action": "goto",
                "blocked": not bool(gm_result.get("ok")),
                "reason": safety_reason if not safe else "online admin teleport requires DUNE_ADMIN_GM_COMMANDS_ENABLED=true, DUNE_GM_COMMAND_PAYLOAD_VERIFIED=true, and DUNE_CHAT_COMMAND_EXECUTE_ONLINE_GM_TELEPORT=true",
                "gm": gm_result,
                "candidateCommands": [
                    f"TeleportToPlayer {target['character_name']}",
                    f"TeleportToExact {target['x']} {target['y']} {target['z']}" if target["x"] is not None else "TeleportToExact <x> <y> <z>",
                    f"TravelTo {target['actor_map'] or target['partition_map']}",
                ],
                "message": response,
                "adminRoute": admin_route,
                "targetRoute": target_route,
                "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)},
                "reply": announce_result,
            }
        response = f"{target['character_name']} is offline at {format_location(target)}; live goto still needs native GM command route verification"
        announce_result = maybe_reply(response, reply)
        return {
            "ok": False,
            "action": "goto",
            "blocked": True,
            "reason": "the sender is online when issuing chat commands, so moving the sender needs a live server command",
            "message": response,
            "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)},
            "reply": announce_result,
        }

    if command in ("bring", "summon", "tphere"):
        if len(parts) != 2:
            response = "usage: &bring <playername>"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        admin, _ = character_row(conn, resolved_admin)
        if admin is None or admin["partition_id"] is None or admin["x"] is None:
            response = f"admin location unavailable for {resolved_admin}"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "reply": announce_result}
        target, matches = character_row(conn, parts[1])
        if target is None:
            response = "no unique player match: " + ", ".join(row["character_name"] for row in matches) if matches else "player not found"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "matches": [row["character_name"] for row in matches], "reply": announce_result}
        if target["online_status"].lower() != "online":
            response = f"{target['character_name']} is {target['online_status']}; use &teleport for offline targets"
            announce_result = maybe_reply(response, reply)
            return {"ok": False, "error": response, "target": dict(target), "reply": announce_result}
        safe, safety_reason, admin_route, target_route = online_gm_teleport_safety(conn, "bring", admin, target)
        route = target_route or gm_route_for(conn, target)
        command_text = f"TeleportToExact {admin['x']:.3f} {admin['y']:.3f} {admin['z']:.3f}"
        if safe:
            gm_result = send_online_gm_teleport("bring", command_text, target["character_name"], resolved_admin, route)
        else:
            gm_result = {"ok": False, "blocked": True, "reason": safety_reason, "preview": gm_command_preview(command_text, target["character_name"], resolved_admin, route)}
        if gm_result.get("ok"):
            response = f"sent native GM bring for {target['character_name']} to {resolved_admin} via {route}"
        elif not safe:
            response = f"online bring blocked for {target['character_name']}: {safety_reason}"
        else:
            response = f"{target['character_name']} is online; bring GM envelope is still gated"
        announce_result = maybe_reply(response, reply)
        return {
            "ok": bool(gm_result.get("ok")),
            "action": "bring",
            "blocked": not bool(gm_result.get("ok")),
            "reason": safety_reason if not safe else "online player teleport requires the native live GM command route gates",
            "gm": gm_result,
            "candidateCommands": [command_text, f"TeleportToPlayer {resolved_admin}"],
            "message": response,
            "adminRoute": admin_route,
            "targetRoute": target_route,
            "admin": {"characterName": resolved_admin, "location": compact_location(admin)},
            "target": {"characterName": target["character_name"], "status": target["online_status"], "location": compact_location(target)},
            "reply": announce_result,
        }

    response = f"unknown command: {command}"
    announce_result = maybe_reply(response, reply)
    return {"ok": False, "error": response, "reply": announce_result}


def parse_chat_message_context(body):
    outer = json.loads(body.decode("utf-8"))
    content = outer.get("content", outer)
    if isinstance(content, str):
        content = json.loads(content)
    message = content.get("m_Message", {}).get("m_UnlocalizedMessage", "")
    sender = content.get("m_FuncomIdFrom", "")
    return {
        "message": message,
        "sender": sender,
        "origin": normalize_origin_location(content.get("m_OriginLocation")),
        "content": content,
    }


def parse_chat_message(body):
    context = parse_chat_message_context(body)
    return context["message"], context["sender"]


def consume_forever():
    host = env("DUNE_CHAT_COMMAND_AMQP_HOST", env("DUNE_ANNOUNCE_HOST_AMQP_HOST", "172.31.240.1"))
    port = int(env("DUNE_CHAT_COMMAND_AMQP_PORT", env("DUNE_ANNOUNCE_HOST_AMQP_PORT", "31982")))
    tls = env_bool("DUNE_CHAT_COMMAND_AMQP_TLS", env_bool("DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS", True))
    user = env_chat_or_announce("DUNE_CHAT_COMMAND_AMQP_USER", "DUNE_ANNOUNCE_CHAT_USER", "")
    password = env_chat_or_announce("DUNE_CHAT_COMMAND_AMQP_PASSWORD", "DUNE_ANNOUNCE_CHAT_PASSWORD", "")
    exchange = env("DUNE_CHAT_COMMAND_EXCHANGE", "chat.intercept")
    exchanges = split_csv(env("DUNE_CHAT_COMMAND_EXCHANGES", exchange))
    if exchange and exchange not in exchanges:
        exchanges.insert(0, exchange)
    queue = env("DUNE_CHAT_COMMAND_QUEUE", "dash_admin_chat_commands")
    routing_key = env("DUNE_CHAT_COMMAND_ROUTING_KEY", "#")
    bind_routing_keys = split_routing_keys(env("DUNE_CHAT_COMMAND_BIND_ROUTING_KEYS", routing_key))

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    retry_seconds = float(env("DUNE_CHAT_COMMAND_AMQP_RETRY_SECONDS", "5"))
    max_attempts = int(env("DUNE_CHAT_COMMAND_AMQP_CONNECT_ATTEMPTS", "0"))
    attempt = 0
    while True:
        attempt += 1
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(
                host=host,
                port=port,
                virtual_host="/",
                credentials=pika.PlainCredentials(user, password),
                ssl_options=pika.SSLOptions(context, host) if tls else None,
                heartbeat=30,
                blocked_connection_timeout=10,
            ))
            break
        except pika.exceptions.AMQPConnectionError as exc:
            if max_attempts and attempt >= max_attempts:
                raise
            print(json.dumps({"ok": False, "amqpConnectAttempt": attempt, "retrySeconds": retry_seconds, "error": str(exc)}, separators=(",", ":")), file=sys.stderr, flush=True)
            time.sleep(retry_seconds)
    channel = connection.channel()
    channel.queue_declare(queue=queue, durable=True, auto_delete=False)
    for exchange_name in exchanges:
        for bind_routing_key in dict.fromkeys(bind_routing_keys):
            channel.queue_bind(queue=queue, exchange=exchange_name, routing_key=bind_routing_key)
    channel.basic_qos(prefetch_count=1)

    conn = connect_db()

    def on_message(ch, method, props, body):
        try:
            context = parse_chat_message_context(body)
            text = context["message"]
            sender_name = context["sender"]
            sender_origin = context.get("origin")
            sender_fls_id = getattr(props, "user_id", "") or ""
            spam_result = enforce_spam_protect(conn, text, sender_name=sender_name, sender_fls_id=sender_fls_id)
            if spam_result:
                print(json.dumps({"ts": int(time.time()), "routingKey": method.routing_key, "sender": sender_name, "senderFlsId": sender_fls_id, "result": spam_result}, default=str, separators=(",", ":")), flush=True)
            if text.startswith(env("DUNE_CHAT_COMMAND_PREFIX", "&")):
                result = handle_command(conn, text, sender_name=sender_name, sender_fls_id=sender_fls_id, sender_origin=sender_origin, reply=True)
                print(json.dumps({"ts": int(time.time()), "routingKey": method.routing_key, "sender": sender_name, "senderFlsId": sender_fls_id, "origin": sender_origin, "result": result}, default=str, separators=(",", ":")), flush=True)
            ch.basic_ack(method.delivery_tag)
        except Exception as exc:
            conn.rollback()
            print(json.dumps({"ts": int(time.time()), "error": str(exc)}, separators=(",", ":")), file=sys.stderr, flush=True)
            ch.basic_ack(method.delivery_tag)

    print(json.dumps({"ok": True, "listening": exchanges, "queue": queue, "routingKeys": bind_routing_keys}, separators=(",", ":")), flush=True)
    channel.basic_consume(queue=queue, on_message_callback=on_message)
    channel.start_consuming()


def healthcheck():
    queue = env("DUNE_CHAT_COMMAND_QUEUE", "dash_admin_chat_commands")
    min_consumers = int(env("DUNE_CHAT_COMMAND_HEALTH_MIN_CONSUMERS", "1"))

    conn = connect_db()
    conn.close()

    host = env("DUNE_CHAT_COMMAND_AMQP_HOST", env("DUNE_ANNOUNCE_HOST_AMQP_HOST", "172.31.240.1"))
    port = int(env("DUNE_CHAT_COMMAND_AMQP_PORT", env("DUNE_ANNOUNCE_HOST_AMQP_PORT", "31982")))
    tls = env_bool("DUNE_CHAT_COMMAND_AMQP_TLS", env_bool("DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS", True))
    user = env_chat_or_announce("DUNE_CHAT_COMMAND_AMQP_USER", "DUNE_ANNOUNCE_CHAT_USER", "")
    password = env_chat_or_announce("DUNE_CHAT_COMMAND_AMQP_PASSWORD", "DUNE_ANNOUNCE_CHAT_PASSWORD", "")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host=host,
        port=port,
        virtual_host="/",
        credentials=pika.PlainCredentials(user, password),
        ssl_options=pika.SSLOptions(context, host) if tls else None,
        heartbeat=30,
        blocked_connection_timeout=10,
    ))
    try:
        channel = connection.channel()
        declared = channel.queue_declare(queue=queue, passive=True)
        consumers = declared.method.consumer_count
        messages = declared.method.message_count
        ok = consumers >= min_consumers
        print(json.dumps({
            "ok": ok,
            "queue": queue,
            "consumers": consumers,
            "messages": messages,
            "minConsumers": min_consumers,
        }, separators=(",", ":")), flush=True)
        return 0 if ok else 1
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Paul in-game chat command listener")
    parser.add_argument("--healthcheck", action="store_true", help="Check DB, RabbitMQ, and the live command queue consumer.")
    parser.add_argument("--dry-run-command", help="Process a command once without consuming RabbitMQ")
    parser.add_argument("--dry-run-spam-message", help="Process one message through spam protection without consuming RabbitMQ")
    parser.add_argument("--dry-run-spam-count", type=int, default=1, help="Number of times to feed --dry-run-spam-message")
    parser.add_argument("--sender-name", default="", help="Sender character name for --dry-run-command")
    parser.add_argument("--sender-fls-id", default="", help="Sender account/user id for --dry-run-command")
    parser.add_argument("--reply", action="store_true", help="Send an in-game reply for --dry-run-command")
    args = parser.parse_args()

    if args.healthcheck:
        return healthcheck()

    if args.dry_run_spam_message:
        with connect_db() as conn:
            results = [
                enforce_spam_protect(conn, args.dry_run_spam_message, args.sender_name, args.sender_fls_id)
                for _ in range(args.dry_run_spam_count)
            ]
        print(json.dumps({"ok": True, "results": results}, default=str, indent=2))
        return

    if args.dry_run_command:
        with connect_db() as conn:
            result = handle_command(conn, args.dry_run_command, args.sender_name, args.sender_fls_id, reply=args.reply)
        print(json.dumps(result, default=str, indent=2))
        return
    consume_forever()


if __name__ == "__main__":
    main()
