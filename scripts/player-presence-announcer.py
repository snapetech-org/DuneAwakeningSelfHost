#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import ssl
import sys
import time

from dune_whisper_route import whisper_route_for_fls_id
import base_recovery_reminders
import player_login_notices
import vehicle_recovery_reminders


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "backups" / "admin-bot"
STATE_FILE = STATE_DIR / "player-presence.json"
DELIVERY_FILE = STATE_DIR / "player-presence-delivery.json"
STARTER_BASE_TOOL_TEMPLATE = "BaseBackupTool"
STARTER_BASE_TOOL_MESSAGE = "A Base Reconstruction Tool has been added to your inventory. You may need to log out and back in before it appears."
STARTER_EMOTE_TEMPLATES = (
    "Emote_AtreSalute_01",
    "Emote_ChusukMusic_01",
    "Emote_HarkCurse_01",
    "Emote_KaitanBow_01",
)
ADMIN_ANOMALY_DIGEST_TEMPLATE = "Admin digest: stale online activity={stuck_count} ({stuck_names}); over base cap={over_base_cap}."
BASE_RECOVERY_REMINDER_MESSAGE = base_recovery_reminders.DEFAULT_MESSAGE
RESTART_STATE_FILE = ROOT / "backups" / "admin-panel" / "restart-jobs.json"
ANNOUNCEMENT_STATE_FILE = ROOT / "backups" / "admin-panel" / "announcements.json"
AUDIT_FILE = ROOT / "backups" / "admin-panel" / "audit.jsonl"


def read_env_file(path):
    values = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


FILE_ENV = read_env_file(ROOT / os.environ.get("DUNE_ADMIN_BOT_ENV_FILE", ".env"))
DB = os.environ.get("DUNE_DB_NAME") or FILE_ENV.get("DUNE_DB_NAME") or "dune_sb_1_4_0_0"


def env(name, default=""):
    return os.environ.get(name) or FILE_ENV.get(name) or default


def env_bool(name, default=False):
    return str(env(name, "true" if default else "false")).lower() in ("1", "true", "yes", "on")


def env_is_set(name):
    return name in os.environ or name in FILE_ENV


def configured_base_cap():
    config_path = ROOT / env("DUNE_PLAYER_PRESENCE_BASE_CAP_CONFIG", "config/UserGame.ini")
    target_map = env("DUNE_PLAYER_PRESENCE_BASE_CAP_MAP", "HaggaBasin")
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return int(env("DUNE_PLAYER_PRESENCE_BASE_CAP", env("DUNE_ADMIN_BOT_MAX_BASES_WARN", "6")))

    match = re.search(r"^m_MaxLandclaimSegmentsPerMap\s*=\s*(.+)$", text, flags=re.MULTILINE)
    if not match:
        return int(env("DUNE_PLAYER_PRESENCE_BASE_CAP", env("DUNE_ADMIN_BOT_MAX_BASES_WARN", "6")))

    entries = re.findall(r'Name="([^"]+)"\)\s*,\s*(\d+)', match.group(1))
    for map_name, cap in entries:
        if map_name == target_map:
            return int(cap)
    if entries:
        return int(entries[0][1])
    return int(env("DUNE_PLAYER_PRESENCE_BASE_CAP", env("DUNE_ADMIN_BOT_MAX_BASES_WARN", "6")))


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ts():
    return int(time.time())


def compose_cmd(*args):
    """Run a bounded command in the existing PostgreSQL container.

    The announcer only used Compose to execute psql. Resolving the full Dune
    Compose project on every poll needlessly read the game .env and stalled
    under host pressure. Select the unique running database container by its
    Compose labels, then exec psql with that container's configured user.
    """
    if not args or args[0] != "exec":
        raise ValueError("player-presence database access only supports docker exec")
    command = list(args[1:])
    if command and command[0] == "-T":
        command.pop(0)
    if not command or command.pop(0) != "postgres":
        raise ValueError("player-presence database access only supports the postgres service")

    runtime = env("CONTAINER_RUNTIME", "docker")
    project = env("DUNE_PLAYER_PRESENCE_COMPOSE_PROJECT", "dune_server")
    filters = [
        "--filter", "status=running",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.service=postgres",
    ]
    listed = subprocess.run(
        [runtime, "ps", "-q", *filters],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"could not locate running PostgreSQL container (docker ps exit {listed.returncode})")
    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if len(container_ids) != 1:
        raise RuntimeError(f"expected one running PostgreSQL container for project {project}; found {len(container_ids)}")

    inspected = subprocess.run(
        [runtime, "inspect", "--format", "{{.Config.User}}", container_ids[0]],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    if inspected.returncode != 0:
        raise RuntimeError(f"could not inspect running PostgreSQL container (docker inspect exit {inspected.returncode})")
    container_user = inspected.stdout.strip()
    cmd = [runtime, "exec"]
    if container_user:
        cmd.extend(["--user", container_user])
    cmd.extend([container_ids[0], *command])
    return cmd


def run(cmd, timeout=30):
    return subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_json_file(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def file_hash(paths):
    digest = hashlib.sha256()
    found = False
    for rel in paths:
        path = ROOT / rel
        if path.exists():
            found = True
            digest.update(str(rel).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest() if found else ""


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def save_delivery_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DELIVERY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(DELIVERY_FILE)


def private_delivery_key(player, message, job_id):
    target = player_fls_id(player) or player_name(player)
    payload = json.dumps(
        {"jobId": str(job_id or ""), "target": str(target or ""), "message": str(message or "")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mark_private_delivery_attempt(player, message, job_id):
    if not env_bool("DUNE_PLAYER_PRESENCE_PRIVATE_DEDUPE_ENABLED", True):
        return None
    ttl = int(env("DUNE_PLAYER_PRESENCE_PRIVATE_DEDUPE_SECONDS", "3600"))
    if ttl <= 0:
        return None
    now = now_ts()
    state = load_json_file(DELIVERY_FILE, {"deliveries": {}})
    deliveries = state.setdefault("deliveries", {})
    for key, entry in list(deliveries.items()):
        if now - int(entry.get("ts", 0) or 0) > ttl:
            deliveries.pop(key, None)
    key = private_delivery_key(player, message, job_id)
    previous = deliveries.get(key)
    if previous and now - int(previous.get("ts", 0) or 0) <= ttl:
        save_delivery_state(state)
        return {"duplicate": True, "key": key, "previous": previous}
    deliveries[key] = {
        "ts": now,
        "jobId": str(job_id or ""),
        "target": player_fls_id(player) or player_name(player),
        "message": str(message or ""),
    }
    save_delivery_state(state)
    return {"duplicate": False, "key": key}


def release_private_delivery_attempt(delivery):
    """Remove a failed delivery marker so the next poll can retry it."""
    if not delivery or delivery.get("duplicate") or not delivery.get("key"):
        return
    state = load_json_file(DELIVERY_FILE, {"deliveries": {}})
    deliveries = state.setdefault("deliveries", {})
    if delivery["key"] in deliveries:
        deliveries.pop(delivery["key"], None)
        save_delivery_state(state)


def online_players():
    sql = """
    select
      ps.account_id::text,
      coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name,
      acc.user as fls_id,
      coalesce(ps.last_login_time::text, '') as last_login_time,
      coalesce(act.map, wp.map, '') as map_name,
      coalesce(wp.label, '') as partition_label,
      coalesce(act.partition_id::text, wp.partition_id::text, '') as partition_id,
      ((act.transform).location).x::float8 as x,
      ((act.transform).location).y::float8 as y,
      ((act.transform).location).z::float8 as z
    from dune.player_state ps
    left join dune.accounts acc on acc.id = ps.account_id
    left join dune.actors act on act.id = ps.player_pawn_id
    left join dune.world_partition wp on wp.partition_id = act.partition_id
    where ps.online_status::text = 'Online'
    order by ps.character_name nulls last, ps.account_id;
    """
    result = run(compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-F", "\t", "-c", sql), timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "online player query failed")
    players = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        account_id = parts[0]
        name = parts[1] if len(parts) > 1 else account_id
        fls_id = parts[2] if len(parts) > 2 else ""
        last_login_time = parts[3] if len(parts) > 3 else ""
        map_name = parts[4] if len(parts) > 4 else ""
        partition_label = parts[5] if len(parts) > 5 else ""
        partition_id = parts[6] if len(parts) > 6 else ""
        players[account_id] = {
            "name": name or account_id,
            "flsId": fls_id,
            "lastLoginTime": last_login_time,
            "map": map_name,
            "partitionLabel": partition_label,
            "partitionId": partition_id,
            "x": parts[7] if len(parts) > 7 else "",
            "y": parts[8] if len(parts) > 8 else "",
            "z": parts[9] if len(parts) > 9 else "",
        }
    return players


def base_claim_counts():
    sql = """
    select
      a.owner_account_id::text,
      count(distinct t.id)::int as base_count,
      count(ls.*)::int as segment_count
    from dune.totems t
    join dune.actors a on a.id = t.id
    left join dune.landclaim_segments ls on ls.totem_id = t.id
    where a.owner_account_id is not null
    group by a.owner_account_id;
    """
    result = run(compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-F", "\t", "-c", sql), timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "base claim query failed")
    counts = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        account_id, base_count, segment_count = (line.split("\t") + ["0", "0"])[:3]
        counts[account_id] = {"baseCount": int(base_count or 0), "segmentCount": int(segment_count or 0)}
    return counts


def configured_subfief_limit_bonus():
    override = env("DUNE_PLAYER_PRESENCE_SUBFIEF_MIN_BONUS", "").strip()
    if override:
        return int(override)
    explicit_bonus = env("DUNE_SUBFIEF_LIMIT_BONUS", "").strip()
    if explicit_bonus:
        return int(explicit_bonus)
    limit = env("DUNE_SUBFIEF_LIMIT", "").strip()
    if not limit:
        return 0
    base = int(env("DUNE_SUBFIEF_BASE_LIMIT", "3"))
    return max(0, int(limit) - base)


def ensure_subfief_limit_bonus(account_ids):
    if not account_ids or not env_bool("DUNE_PLAYER_PRESENCE_SUBFIEF_BONUS_ENFORCER_ENABLED", True):
        return []
    bonus = configured_subfief_limit_bonus()
    if bonus <= 0:
        return []
    values = ", ".join(f"({int(account_id)})" for account_id in sorted(set(account_ids), key=int))
    sql = f"""
    with requested(account_id) as (
      values {values}
    ),
    target as (
      select
        ps.account_id,
        coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name,
        ps.player_pawn_id,
        case
          when a.gas_attributes #>> '{{DunePlayerCharacterAttributeSet,SubfiefLimitBonus,BaseValue}}' ~ '^-?[0-9]+(\\.[0-9]+)?$'
            then (a.gas_attributes #>> '{{DunePlayerCharacterAttributeSet,SubfiefLimitBonus,BaseValue}}')::numeric
          else -1
        end as current_base_bonus,
        case
          when a.gas_attributes #>> '{{DunePlayerCharacterAttributeSet,SubfiefLimitBonus,CurrentValue}}' ~ '^-?[0-9]+(\\.[0-9]+)?$'
            then (a.gas_attributes #>> '{{DunePlayerCharacterAttributeSet,SubfiefLimitBonus,CurrentValue}}')::numeric
          else -1
        end as current_value_bonus
      from requested
      join dune.player_state ps on ps.account_id = requested.account_id
      join dune.actors a on a.id = ps.player_pawn_id
    ),
    stale as (
      select *
      from target
      where current_base_bonus < {bonus}
         or current_value_bonus < {bonus}
    ),
    updated as (
      update dune.actors a
      set gas_attributes = jsonb_set(
        jsonb_set(
          coalesce(a.gas_attributes, '{{}}'::jsonb),
          '{{DunePlayerCharacterAttributeSet}}',
          coalesce(a.gas_attributes #> '{{DunePlayerCharacterAttributeSet}}', '{{}}'::jsonb),
          true
        ),
        '{{DunePlayerCharacterAttributeSet,SubfiefLimitBonus}}',
        jsonb_build_object('BaseValue', {bonus}::float, 'CurrentValue', {bonus}::float),
        true
      )
      from stale
      where a.id = stale.player_pawn_id
      returning
        stale.account_id::text,
        stale.character_name,
        stale.player_pawn_id,
        stale.current_base_bonus,
        stale.current_value_bonus,
        {bonus} as applied_bonus
    )
    select coalesce(jsonb_agg(to_jsonb(updated) order by account_id), '[]'::jsonb)::text
    from updated;
    """
    result = run(
        compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql),
        timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "subfief bonus repair failed")
    text = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "[]"
    return json.loads(text)


def map_health_summary():
    sql = """
    with active as (
      select server_id from dune.active_server_ids
    ),
    status as (
      select
        wp.partition_id,
        coalesce(nullif(wp.label, ''), wp.map || ' #' || wp.partition_id::text) as label,
        wp.map,
        wp.server_id,
        coalesce(fs.ready, false) as ready,
        coalesce(fs.alive, false) as alive,
        active.server_id is not null as active,
        coalesce(wp.blocked, false) as blocked,
        coalesce(fs.connected_players, 0)::int as connected_players
      from dune.world_partition wp
      left join dune.farm_state fs on fs.server_id = wp.server_id
      left join active on active.server_id = wp.server_id
    )
    select jsonb_build_object(
      'expected', count(*),
      'online', count(*) filter (where server_id is not null and alive and active and not blocked),
      'readyAlive', count(*) filter (where ready and alive and active and not blocked),
      'activeServers', (select count(*) from active),
      'connectedPlayers', coalesce(sum(connected_players), 0),
      'offlineLabels', coalesce(jsonb_agg(label order by label) filter (where not (server_id is not null and alive and active and not blocked)), '[]'::jsonb),
      'playersByMap', coalesce(jsonb_object_agg(label, connected_players order by label), '{}'::jsonb)
    )::text
    from status;
    """
    result = run(compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql), timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "map health query failed")
    text = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
    return json.loads(text)


def player_roster_by_map():
    sql = """
    select
      coalesce(nullif(wp.label, ''), nullif(act.map, ''), nullif(wp.map, ''), 'Unknown') as map_label,
      coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name
    from dune.player_state ps
    left join dune.actors act on act.id = ps.player_pawn_id
    left join dune.world_partition wp on wp.partition_id = act.partition_id
    where ps.online_status::text = 'Online'
    order by map_label, character_name;
    """
    result = run(compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-F", "\t", "-c", sql), timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "player roster by map query failed")
    roster = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        map_label, name = (line.split("\t") + [""])[:2]
        roster.setdefault(map_label or "Unknown", []).append(name)
    return roster


def compact_roster_by_map(roster, map_limit=8, player_limit=8):
    if not roster:
        return "no online players"
    parts = []
    for map_label in sorted(roster)[:map_limit]:
        players = roster[map_label]
        shown = ", ".join(players[:player_limit])
        if len(players) > player_limit:
            shown += f", +{len(players) - player_limit} more"
        parts.append(f"{map_label}: {shown}")
    if len(roster) > map_limit:
        parts.append(f"+{len(roster) - map_limit} more maps")
    return "; ".join(parts)


def service_health_checks():
    checks = []
    for unit in [item.strip() for item in env("DUNE_PLAYER_PRESENCE_SERVICE_HEALTH_UNITS", "dune-player-presence-announcer.service").split(",") if item.strip()]:
        result = run(["systemctl", "is-active", unit], timeout=5)
        checks.append({"name": unit, "ok": result.returncode == 0, "status": result.stdout.strip() or result.stderr.strip()})
    max_age = int(env("DUNE_PLAYER_PRESENCE_FRESHNESS_MAX_AGE_SECONDS", "300"))
    missing_grace = int(env("DUNE_PLAYER_PRESENCE_FRESHNESS_MISSING_GRACE_SECONDS", "900"))
    default_optional_missing = "" if env_is_set("DUNE_PLAYER_PRESENCE_FRESHNESS_FILES") else "public-site/static/hagga-map.svg"
    optional_missing = {
        item.strip()
        for item in env("DUNE_PLAYER_PRESENCE_FRESHNESS_OPTIONAL_MISSING_FILES", default_optional_missing).split(",")
        if item.strip()
    }
    anchor_paths = [
        ROOT / item.strip()
        for item in env("DUNE_PLAYER_PRESENCE_FRESHNESS_ANCHOR_FILES", "public-site/static/players.json").split(",")
        if item.strip()
    ]
    newest_anchor_mtime = max((path.stat().st_mtime for path in anchor_paths if path.exists()), default=0)
    freshness_files = [
        item.strip()
        for item in env("DUNE_PLAYER_PRESENCE_FRESHNESS_FILES", "public-site/static/players.json").split(",")
        if item.strip() and item.strip().lower() not in ("none", "off", "false", "disabled")
    ]
    for rel in freshness_files:
        path = ROOT / rel
        if not path.exists():
            anchor_age = int(time.time() - newest_anchor_mtime) if newest_anchor_mtime else None
            if rel in optional_missing and newest_anchor_mtime and anchor_age is not None and anchor_age <= missing_grace:
                checks.append({"name": rel, "ok": True, "status": f"missing but optional; anchor age={anchor_age}s"})
            else:
                checks.append({"name": rel, "ok": False, "status": "missing"})
            continue
        age = int(time.time() - path.stat().st_mtime)
        checks.append({"name": rel, "ok": age <= max_age, "status": f"age={age}s"})
    if env_bool("DUNE_PLAYER_PRESENCE_FLS_PUBLICATION_HEALTH_ENABLED", True):
        result = run([
            str(ROOT / "scripts" / "fls-publication-health.py"),
            env("DUNE_PLAYER_PRESENCE_ENV_FILE", ".env"),
            "--compose-files",
            env("COMPOSE_FILES", "compose.yaml"),
            "--json",
        ], timeout=int(env("DUNE_PLAYER_PRESENCE_FLS_PUBLICATION_HEALTH_TIMEOUT_SECONDS", "30")))
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except Exception:
            payload = {}
        failed = [item for item in payload.get("checks", []) if not item.get("ok")]
        if result.returncode == 0 and payload.get("ok"):
            checks.append({"name": "FLS publication", "ok": True, "status": payload.get("state", "healthy")})
        else:
            summary = "; ".join(f"{item.get('name')}: {item.get('value', '')}" for item in failed[:3])
            if not summary:
                summary = (result.stderr or result.stdout or "unknown failure").strip()[:240]
            checks.append({"name": "FLS publication", "ok": False, "status": summary})
    return checks


def latest_backup_age_hours():
    candidates = []
    for path in (ROOT / "backups").glob("*"):
        if path.is_dir() and ((path / "manifest.txt").exists() or (path / "manifest.json").exists()):
            candidates.append(path)
    for path in (ROOT / "backups" / "admin-panel" / "maintenance").glob("*"):
        if path.is_dir() and (path / "manifest.json").exists():
            candidates.append(path)
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    return {"path": str(latest.relative_to(ROOT)), "ageHours": round((time.time() - latest.stat().st_mtime) / 3600, 2)}


def failed_restart_jobs_since(since_ts):
    jobs = []
    for job in load_json_file(RESTART_STATE_FILE, {"jobs": []}).get("jobs", []):
        ts = float(job.get("executedAt") or job.get("createdAt") or 0)
        if job.get("status") == "failed" and ts > since_ts:
            jobs.append({"id": job.get("id"), "target": job.get("targetLabel") or job.get("target") or "unknown", "error": (job.get("lastError") or "")[:180]})
    return jobs


def cancelled_jobs_since(since_ts):
    jobs = []
    for path in (RESTART_STATE_FILE, ANNOUNCEMENT_STATE_FILE):
        for job in load_json_file(path, {"jobs": []}).get("jobs", []):
            ts = float(job.get("cancelledAt") or 0)
            if job.get("status") == "cancelled" and ts > since_ts:
                jobs.append({"id": job.get("id"), "source": path.name, "message": job.get("message", "")})
    return jobs


def audit_counts_since(offset):
    counts = {}
    mutations = 0
    if not AUDIT_FILE.exists():
        return {"offset": 0, "counts": counts, "mutations": mutations}
    size = AUDIT_FILE.stat().st_size
    if offset > size:
        offset = 0
    with AUDIT_FILE.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("event") or event.get("action") or "unknown"
            counts[name] = counts.get(name, 0) + 1
            if any(token in name.lower() for token in ("write", "grant", "mutation", "restart", "transfer", "config")):
                mutations += 1
        offset = handle.tell()
    return {"offset": offset, "counts": counts, "mutations": mutations}


def stuck_transition_players(minutes):
    sql = f"""
    select ps.account_id::text,
           coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name,
           ps.online_status::text,
           coalesce(fs.map, '') as map,
           floor(extract(epoch from (now() - ps.last_avatar_activity)) / 60)::int as stale_minutes
    from dune.player_state ps
    left join dune.farm_state fs on fs.server_id = ps.server_id
    where ps.online_status::text = 'Online'
      and ps.last_avatar_activity < now() - interval '{int(minutes)} minutes'
    order by ps.last_avatar_activity asc
    limit 25;
    """
    result = run(compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-F", "\t", "-c", sql), timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "stuck transition query failed")
    rows = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        account_id, name, status, map_name, stale_minutes = (line.split("\t") + ["", "", "", ""])[:5]
        rows.append({"accountId": account_id, "name": name, "status": status, "map": map_name, "staleMinutes": stale_minutes})
    return rows


def player_name(player):
    if isinstance(player, dict):
        return player.get("name") or player.get("characterName") or player.get("accountId") or "unknown"
    return str(player)


def player_fls_id(player):
    if isinstance(player, dict):
        return player.get("flsId") or ""
    return ""


def player_map(player):
    if isinstance(player, dict):
        return player.get("map") or ""
    return ""


def player_location_label(player):
    if not isinstance(player, dict):
        return ""
    return player.get("partitionLabel") or player.get("map") or ""


def player_partition_id(player):
    if isinstance(player, dict):
        return str(player.get("partitionId") or "")
    return ""


def env_set(name, default=""):
    return {item.strip() for item in env(name, default).split(",") if item.strip()}


def deep_desert_text(player):
    if not isinstance(player, dict):
        return ""
    return f"{player.get('map') or ''} {player.get('partitionLabel') or ''}".lower()


def is_deep_desert_player(player):
    text = re.sub(r"[^a-z0-9]+", "", deep_desert_text(player))
    return "deepdesert" in text


def deep_desert_instance(player):
    if not is_deep_desert_player(player):
        return ""
    text = deep_desert_text(player)
    partition_id = player_partition_id(player)
    hardcore_partitions = env_set("DUNE_PLAYER_PRESENCE_DEEP_DESERT_HARDCORE_PARTITIONS", "")
    if not hardcore_partitions:
        hardcore_partitions = env_set("DUNE_PLAYER_PRESENCE_DEEP_DESERT_PVP_PARTITIONS", "31")
    casual_partitions = env_set("DUNE_PLAYER_PRESENCE_DEEP_DESERT_CASUAL_PARTITIONS", "")
    if not casual_partitions:
        casual_partitions = env_set("DUNE_PLAYER_PRESENCE_DEEP_DESERT_PVE_PARTITIONS", "8")
    if "hardcore" in text or "pvp" in text or partition_id in hardcore_partitions:
        return "hardcore"
    if "casual" in text or "pve" in text or partition_id in casual_partitions:
        return "casual"
    return env("DUNE_PLAYER_PRESENCE_DEEP_DESERT_DEFAULT_INSTANCE", "casual").strip().lower() or "casual"


def player_location_changed(previous_player, current_player):
    if not isinstance(previous_player, dict):
        return True
    previous_location = (player_map(previous_player), player_location_label(previous_player))
    current_location = (player_map(current_player), player_location_label(current_player))
    if previous_location != current_location:
        return True
    previous_partition_id = player_partition_id(previous_player)
    current_partition_id = player_partition_id(current_player)
    return bool(previous_partition_id and current_partition_id and previous_partition_id != current_partition_id)


def player_presence_session(player):
    if not isinstance(player, dict):
        return ""
    return str(player.get("lastLoginTime") or "")


def sorted_account_ids(account_ids, players):
    return sorted(account_ids, key=lambda account_id: player_name(players.get(account_id, account_id)).lower())


def transfer_grace_seconds():
    return max(0, int(env(
        "DUNE_PLAYER_PRESENCE_TRANSFER_GRACE_SECONDS",
        env("DUNE_PLAYER_PRESENCE_MAP_TRANSFER_GRACE_SECONDS", "90"),
    )))


def session_change_counts_as_rejoin(previous_player, current_player):
    previous_session = player_presence_session(previous_player)
    current_session = player_presence_session(current_player)
    if not previous_session or not current_session or previous_session == current_session:
        return False
    if env_bool("DUNE_PLAYER_PRESENCE_SESSION_REJOIN_ON_LOCATION_CHANGE", False):
        return True
    return not player_location_changed(previous_player, current_player)


def admin_players(current):
    names = {item.strip().lower() for item in env("DUNE_PLAYER_PRESENCE_ADMIN_NAMES", env("DUNE_CHAT_COMMAND_ADMINS", "")).split(",") if item.strip()}
    fls_ids = {item.strip() for item in env("DUNE_PLAYER_PRESENCE_ADMIN_FLS_IDS", env("DUNE_CHAT_COMMAND_ADMIN_FLS_IDS", "")).split(",") if item.strip()}
    admins = {}
    for account_id, player in current.items():
        if (names and player_name(player).lower() in names) or (fls_ids and player_fls_id(player) in fls_ids):
            admins[account_id] = player
    return admins


def completed_journey_players(story_node_id):
    sql = f"""
    select ps.account_id::text, coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name
    from dune.journey_story_node jsn
    join dune.player_state ps on ps.id = jsn.character_id
    where jsn.story_node_id = {sql_literal(story_node_id)}
      and jsn.complete_condition_state = 'true'::jsonb
    order by ps.character_name nulls last, ps.account_id;
    """
    result = run(
        compose_cmd(
            "exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB,
            "-At", "-F", "\t", "-c", sql,
        ),
        timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "journey completion query failed")
    players = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        account_id, _, name = line.partition("\t")
        players[account_id] = name or account_id
    return players


def grant_starter_base_tool(account_id):
    template_id = env("DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_TEMPLATE", STARTER_BASE_TOOL_TEMPLATE)
    preferred_inventory_type = int(env("DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_INVENTORY_TYPE", "0"))
    sql = f"""
    with player as (
      select account_id, character_name, player_pawn_id, player_controller_id, online_status::text as online_status
      from dune.player_state
      where account_id = {int(account_id)}
      limit 1
    ),
    owned_inventories as (
      select inv.id as inventory_id, inv.inventory_type, inv.max_item_count
      from player p
      join dune.inventories inv on inv.actor_id in (p.player_pawn_id, p.player_controller_id)
    ),
    existing as (
      select i.id as item_id, i.inventory_id
      from owned_inventories inv
      join dune.items i on i.inventory_id = inv.inventory_id
      where i.template_id = {sql_literal(template_id)}
      limit 1
    ),
    candidate_inventories as (
      select inv.inventory_id, inv.inventory_type, inv.max_item_count
      from owned_inventories inv
      join player p on true
      where p.online_status = 'Offline'
        and not exists (select 1 from existing)
      order by
        case when inv.inventory_type = {preferred_inventory_type} then 0 else 1 end,
        case when inv.max_item_count is null or inv.max_item_count < 0 then 1 else 0 end,
        inv.inventory_type nulls last,
        inv.inventory_id
    ),
    finite_slots as (
      select ci.inventory_id, ci.inventory_type, slot.position_index
      from candidate_inventories ci
      cross join lateral generate_series(0, ci.max_item_count - 1) as slot(position_index)
      where ci.max_item_count is not null and ci.max_item_count > 0
        and not exists (
          select 1 from dune.items i
          where i.inventory_id = ci.inventory_id
            and i.position_index = slot.position_index
        )
    ),
    unbounded_slots as (
      select ci.inventory_id, ci.inventory_type, coalesce(max(i.position_index), -1) + 1 as position_index
      from candidate_inventories ci
      left join dune.items i on i.inventory_id = ci.inventory_id
      where ci.max_item_count is null or ci.max_item_count < 0
      group by ci.inventory_id, ci.inventory_type
    ),
    target as (
      select inventory_id, inventory_type, position_index
      from (
        select inventory_id, inventory_type, position_index from finite_slots
        union all
        select inventory_id, inventory_type, position_index from unbounded_slots
      ) slots
      order by
        case when inventory_type = {preferred_inventory_type} then 0 else 1 end,
        inventory_type nulls last,
        inventory_id,
        position_index
      limit 1
    ),
    next_item as (
      select dune.advance_items_id_sequencer(1) as item_id
      where exists (select 1 from target)
    ),
    saved as (
      select dune.save_item((
        next_item.item_id,
        target.inventory_id,
        1,
        target.position_index,
        {sql_literal(template_id)},
        true,
        (extract(epoch from now()) * 1000)::bigint,
        '{{}}'::jsonb,
        0,
        null
      )::dune.inventoryitem) as ok,
      next_item.item_id,
      target.inventory_id,
      target.position_index,
      target.inventory_type
      from next_item, target
    )
    select jsonb_build_object(
      'accountId', (select account_id from player),
      'characterName', (select character_name from player),
      'templateId', {sql_literal(template_id)},
      'alreadyHadItem', exists(select 1 from existing),
      'granted', exists(select 1 from saved),
      'itemId', (select item_id from saved),
      'inventoryId', coalesce((select inventory_id from saved), (select inventory_id from existing)),
      'inventoryType', (select inventory_type from saved),
      'positionIndex', (select position_index from saved),
      'deferred', coalesce((select online_status from player) <> 'Offline', false),
      'error', case
        when not exists(select 1 from player) then 'player not found'
        when (select online_status from player) <> 'Offline' and not exists(select 1 from existing) then 'player is online; retry after logoff'
        when exists(select 1 from existing) then null
        when not exists(select 1 from target) then 'no empty owned inventory slot'
        else null
      end
    )::text;
    """
    result = run(
        compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql),
        timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
    )
    if result.returncode != 0:
        return {"ok": False, "accountId": account_id, "error": result.stderr.strip() or "starter base tool grant failed"}
    text = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
    payload = json.loads(text)
    payload["ok"] = bool(payload.get("alreadyHadItem") or payload.get("granted"))
    return payload


def starter_emote_templates():
    raw = env("DUNE_PLAYER_PRESENCE_STARTER_EMOTE_TEMPLATES", ",".join(STARTER_EMOTE_TEMPLATES))
    return [item.strip() for item in raw.split(",") if item.strip()]


def grant_starter_emotes(account_id):
    templates = starter_emote_templates()
    if not templates:
        return {"ok": True, "accountId": account_id, "templateIds": [], "alreadyHadAll": True, "granted": 0}
    preferred_inventory_type = int(env("DUNE_PLAYER_PRESENCE_STARTER_EMOTE_INVENTORY_TYPE", "14"))
    template_values = ", ".join(
        f"({index + 1}, {sql_literal(template_id)})"
        for index, template_id in enumerate(templates)
    )
    sql = f"""
    with target_templates(ord, template_id) as (
      values {template_values}
    ),
    player as (
      select account_id, character_name, player_pawn_id, player_controller_id
      from dune.player_state
      where account_id = {int(account_id)}
      limit 1
    ),
    owned_inventories as (
      select inv.id as inventory_id, inv.inventory_type, inv.max_item_count
      from player p
      join dune.inventories inv on inv.actor_id in (p.player_pawn_id, p.player_controller_id)
    ),
    target_inventory as (
      select inventory_id, inventory_type, max_item_count
      from owned_inventories
      where inventory_type = {preferred_inventory_type}
      order by inventory_id
      limit 1
    ),
    missing as (
      select tt.ord, tt.template_id
      from target_templates tt
      where not exists (
        select 1
        from owned_inventories inv
        join dune.items i on i.inventory_id = inv.inventory_id
        where i.template_id = tt.template_id
      )
    ),
    numbered_missing as (
      select ord,
             template_id,
             row_number() over (order by ord) as rn
      from missing
    ),
    free_slots as (
      select slot.position_index,
             row_number() over (order by slot.position_index) as rn
      from target_inventory inv
      cross join lateral generate_series(0, inv.max_item_count - 1) as slot(position_index)
      where inv.max_item_count is not null
        and inv.max_item_count > 0
        and not exists (
          select 1 from dune.items i
          where i.inventory_id = inv.inventory_id
            and i.position_index = slot.position_index
        )
    ),
    planned as (
      select nm.ord,
             nm.template_id,
             ti.inventory_id,
             ti.inventory_type,
             fs.position_index,
             row_number() over (order by nm.ord) as grant_ordinal
      from numbered_missing nm
      join target_inventory ti on true
      join free_slots fs on fs.rn = nm.rn
      where (select count(*) from free_slots) >= (select count(*) from missing)
    ),
    seq as (
      select dune.advance_items_id_sequencer((select count(*) from planned)) as first_item_id
      where exists (select 1 from planned)
    ),
    saved as (
      select dune.save_item((
        (seq.first_item_id + planned.grant_ordinal - 1)::bigint,
        planned.inventory_id,
        1,
        planned.position_index,
        planned.template_id,
        true,
        (extract(epoch from now()))::bigint,
        '{{}}'::jsonb,
        0,
        null
      )::dune.inventoryitem) as ok,
      (seq.first_item_id + planned.grant_ordinal - 1)::bigint as item_id,
      planned.inventory_id,
      planned.inventory_type,
      planned.position_index,
      planned.template_id
      from planned cross join seq
    )
    select jsonb_build_object(
      'accountId', (select account_id from player),
      'characterName', (select character_name from player),
      'templateIds', (select jsonb_agg(template_id order by ord) from target_templates),
      'alreadyHadAll', not exists(select 1 from missing),
      'missing', (select count(*) from missing),
      'granted', (select count(*) from saved),
      'items', coalesce((select jsonb_agg(jsonb_build_object(
        'itemId', item_id,
        'inventoryId', inventory_id,
        'inventoryType', inventory_type,
        'positionIndex', position_index,
        'templateId', template_id
      ) order by position_index) from saved), '[]'::jsonb),
      'error', case
        when not exists(select 1 from player) then 'player not found'
        when not exists(select 1 from target_inventory) and exists(select 1 from missing) then 'no owned emote inventory'
        when (select count(*) from planned) < (select count(*) from missing) then 'not enough empty emote inventory slots'
        else null
      end
    )::text;
    """
    result = run(
        compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql),
        timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
    )
    if result.returncode != 0:
        return {"ok": False, "accountId": account_id, "templateIds": templates, "error": result.stderr.strip() or "starter emote grant failed"}
    text = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
    payload = json.loads(text)
    payload["ok"] = bool(payload.get("alreadyHadAll") or int(payload.get("granted") or 0) == len(templates))
    return payload


def parse_faction_channel_map(raw):
    mapping = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        values = [part.strip() for part in value.replace("|", ";").split(";") if part.strip()]
        if key.strip() and values:
            mapping[key.strip()] = values
    return mapping


def faction_chat_channel_map():
    return parse_faction_channel_map(env("DUNE_PLAYER_PRESENCE_FACTION_CHAT_COMMUNINET_CHANNELS", ""))


def faction_chat_candidates(online_only=True):
    neutral_faction_id = int(env("DUNE_PLAYER_PRESENCE_FACTION_CHAT_NEUTRAL_FACTION_ID", "3"))
    allowed_ids = [
        int(item.strip())
        for item in env("DUNE_PLAYER_PRESENCE_FACTION_CHAT_ALLOWED_FACTION_IDS", "1,2").split(",")
        if item.strip()
    ]
    allowed_values = ", ".join(str(item) for item in allowed_ids) or "1,2"
    online_filter = "and ps.online_status::text = 'Online'" if online_only else ""
    reputation_fallback = env_bool("DUNE_PLAYER_PRESENCE_FACTION_CHAT_INFER_FROM_REPUTATION", False)
    reputation_cases = ""
    if reputation_fallback:
        reputation_cases = """
               when coalesce(c.atreides_rep, -1) > coalesce(c.harkonnen_rep, -1) and coalesce(c.atreides_rep, 0) > 0 then 1
               when coalesce(c.harkonnen_rep, -1) > coalesce(c.atreides_rep, -1) and coalesce(c.harkonnen_rep, 0) > 0 then 2
"""
    sql = f"""
    with reps as (
      select actor_id,
             max(reputation_amount) filter (where faction_id = 1) as atreides_rep,
             max(reputation_amount) filter (where faction_id = 2) as harkonnen_rep
      from dune.player_faction_reputation
      group by actor_id
    ),
    candidates as (
      select
        ps.account_id::text,
        coalesce(nullif(ps.character_name, ''), ps.account_id::text) as character_name,
        ps.online_status::text as online_status,
        coalesce(acc.user, '') as fls_id,
        ps.player_controller_id::text,
        ps.player_pawn_id::text,
        dune.get_player_faction(ps.player_controller_id, {neutral_faction_id}::smallint)::int as controller_faction_id,
        dune.get_player_faction(ps.player_pawn_id, {neutral_faction_id}::smallint)::int as pawn_faction_id,
        reps.atreides_rep,
        reps.harkonnen_rep
      from dune.player_state ps
      left join dune.accounts acc on acc.id = ps.account_id
      left join reps on reps.actor_id = ps.player_controller_id
      where ps.player_controller_id is not null
        and ps.player_pawn_id is not null
        {online_filter}
    ),
    inferred as (
      select c.*,
             case
               when c.controller_faction_id in ({allowed_values}) then c.controller_faction_id
               {reputation_cases}
               else null
             end as inferred_faction_id
      from candidates c
    )
    select i.account_id,
           i.character_name,
           i.online_status,
           i.fls_id,
           i.player_controller_id,
           i.player_pawn_id,
           i.controller_faction_id::text,
           i.pawn_faction_id::text,
           i.inferred_faction_id::text,
           coalesce(f.name, ''),
           coalesce(i.atreides_rep::text, ''),
           coalesce(i.harkonnen_rep::text, '')
    from inferred i
    left join dune.factions f on f.id = i.inferred_faction_id
    where i.inferred_faction_id is not null
      and i.pawn_faction_id <> i.inferred_faction_id
    order by i.online_status desc, i.character_name nulls last, i.account_id;
    """
    result = run(
        compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-F", "\t", "-c", sql),
        timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "faction chat candidate query failed")
    rows = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = (line.split("\t") + [""] * 12)[:12]
        rows.append({
            "accountId": parts[0],
            "characterName": parts[1],
            "onlineStatus": parts[2],
            "flsId": parts[3],
            "playerControllerId": parts[4],
            "playerPawnId": parts[5],
            "controllerFactionId": int(parts[6] or neutral_faction_id),
            "pawnFactionId": int(parts[7] or neutral_faction_id),
            "inferredFactionId": int(parts[8]),
            "factionName": parts[9],
            "atreidesRep": int(parts[10]) if parts[10] else None,
            "harkonnenRep": int(parts[11]) if parts[11] else None,
        })
    return rows


def seed_player_faction(candidate):
    neutral_faction_id = int(env("DUNE_PLAYER_PRESENCE_FACTION_CHAT_NEUTRAL_FACTION_ID", "3"))
    sql = f"""
    select dune.change_player_faction(
      {int(candidate["playerPawnId"])}::bigint,
      {int(candidate["inferredFactionId"])}::smallint,
      {neutral_faction_id}::smallint,
      timezone('utc', now())::timestamp
    );
    select dune.get_player_faction({int(candidate["playerPawnId"])}::bigint, {neutral_faction_id}::smallint);
    """
    result = run(
        compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql),
        timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "change_player_faction failed"}
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    after = int(lines[-1]) if lines and lines[-1].isdigit() else None
    return {"ok": after == int(candidate["inferredFactionId"]), "afterFactionId": after, "stdout": result.stdout.strip()}


def tune_faction_communinet_channels(candidate):
    channels = faction_chat_channel_map().get(candidate.get("factionName") or "", [])
    if not channels:
        return {"ok": True, "skipped": True, "reason": "no configured Communinet channel names"}
    results = []
    for channel_name in channels:
        sql = (
            "select dune.update_communinet_player_channel("
            f"{int(candidate['accountId'])}::bigint,"
            f"{sql_literal(channel_name)}::text,"
            "true"
            ");"
        )
        result = run(
            compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql),
            timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
        )
        results.append({
            "channelName": channel_name,
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        })
    return {"ok": all(item["ok"] for item in results), "channels": results}


def faction_chat_connection_params():
    sys.path.insert(0, str(ROOT / "scripts" / "vendor"))
    import pika

    host = env("DUNE_ANNOUNCE_HOST_AMQP_HOST", env("DUNE_ANNOUNCE_GAME_RMQ_AMQP_HOST", "127.0.0.1"))
    port = int(env("DUNE_ANNOUNCE_HOST_AMQP_PORT", env("DUNE_ANNOUNCE_GAME_RMQ_AMQP_PORT", env("GAME_RMQ_PUBLIC_PORT", "31982"))))
    tls = env_bool("DUNE_ANNOUNCE_GAME_RMQ_AMQP_TLS", True)
    user = env("DUNE_ANNOUNCE_CHAT_USER", "A000000000000001")
    password = env("DUNE_ANNOUNCE_CHAT_PASSWORD", "")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return pika.ConnectionParameters(
        host=host,
        port=port,
        virtual_host="/",
        credentials=pika.PlainCredentials(user, password),
        ssl_options=pika.SSLOptions(context, host) if tls else None,
        heartbeat=0,
        blocked_connection_timeout=10,
    )


def bind_faction_chat_queue(candidate):
    fls_id = candidate.get("flsId") or ""
    if not fls_id:
        return {"ok": False, "skipped": True, "reason": "missing FLS id"}
    sys.path.insert(0, str(ROOT / "scripts" / "vendor"))
    import pika

    exchange = f"chat.faction.{int(candidate['inferredFactionId'])}"
    queue = f"{fls_id}_queue"
    try:
        connection = pika.BlockingConnection(faction_chat_connection_params())
        try:
            channel = connection.channel()
            channel.queue_bind(queue=queue, exchange=exchange, routing_key="")
        finally:
            connection.close()
        return {"ok": True, "exchange": exchange, "queue": queue, "routingKey": ""}
    except Exception as exc:
        return {"ok": False, "exchange": exchange, "queue": queue, "routingKey": "", "error": str(exc)}


def seed_faction_chat(online_only=True, execute=False):
    candidates = faction_chat_candidates(online_only=online_only)
    results = []
    bind_enabled = env_bool("DUNE_PLAYER_PRESENCE_FACTION_CHAT_BIND_QUEUES", True)
    communinet_enabled = env_bool("DUNE_PLAYER_PRESENCE_FACTION_CHAT_TUNE_COMMUNINET", False)
    for candidate in candidates:
        item = {"candidate": candidate, "execute": execute}
        if execute:
            item["playerFaction"] = seed_player_faction(candidate)
            if communinet_enabled:
                item["communinet"] = tune_faction_communinet_channels(candidate)
            if bind_enabled and candidate.get("onlineStatus") == "Online":
                item["binding"] = bind_faction_chat_queue(candidate)
        results.append(item)
    return {"ok": True, "onlineOnly": online_only, "execute": execute, "count": len(results), "results": results}


def private_broadcast(current, message, job_id):
    if not current:
        return {"ok": False, "mode": "whisper", "error": "no current online players"}
    sends = []
    for account_id, player in sorted(current.items(), key=lambda item: player_name(item[1]).lower()):
        sends.append({
            "accountId": account_id,
            "player": player_name(player),
            "send": private_message(player, message, job_id),
        })
    return {
        "ok": bool(sends) and all(item.get("send", {}).get("ok") for item in sends),
        "mode": "whisper",
        "count": len(sends),
        "sends": sends,
    }


def public_announce(message, current, job_id="player-presence"):
    delivery_mode = env("DUNE_PLAYER_PRESENCE_PUBLIC_DELIVERY_MODE", "announce").strip().lower()
    if delivery_mode in ("whisper", "private", "private-whisper"):
        return private_broadcast(current, message, job_id)
    return announce(message)


def announce(message):
    command = env("DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND", env("DUNE_ADMIN_ANNOUNCE_COMMAND", str(ROOT / "scripts" / "announce.sh")))
    if command.startswith("/workspace/"):
        command = str(ROOT / command.removeprefix("/workspace/"))
    timeout = int(env("DUNE_PLAYER_PRESENCE_ANNOUNCE_TIMEOUT_SECONDS", env("DUNE_ADMIN_ANNOUNCEMENT_COMMAND_TIMEOUT_SECONDS", "45")))
    child_env = os.environ.copy()
    child_env.update(FILE_ENV)
    child_env["DUNE_ANNOUNCE_MESSAGE"] = message
    child_env["DUNE_ANNOUNCE_JOB_ID"] = "player-presence"
    child_env["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"] = "true"
    child_env["DUNE_ANNOUNCE_ENV_FILE"] = "/dev/null"
    child_env["DUNE_ANNOUNCE_CHAT_ENSURE_ACCOUNT"] = "false"
    child_env["DUNE_ANNOUNCE_ALLOW_MANAGEMENT_PUBLISH"] = "false"
    child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"] = env("DUNE_PLAYER_PRESENCE_ANNOUNCE_EXCHANGE", env("DUNE_ANNOUNCE_CHAT_EXCHANGE", "chat.map"))
    child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"] = env("DUNE_PLAYER_PRESENCE_ANNOUNCE_CHANNEL", env("DUNE_ANNOUNCE_CHAT_CHANNEL", "Map"))
    child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"] = env("DUNE_PLAYER_PRESENCE_ANNOUNCE_ROUTING_KEYS", "<empty>") or "<empty>"
    child_env["DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES"] = env("DUNE_PLAYER_PRESENCE_ANNOUNCE_BIND_ONLINE_QUEUES", env("DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES", "true"))
    child_env["DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS"] = ""
    child_env["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"] = ""
    child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = ""
    result = subprocess.run([command, message], cwd=ROOT, env=child_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    return {
        "ok": result.returncode == 0,
        "mode": "announce",
        "returncode": result.returncode,
        "routingKeys": child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"],
        "exchange": child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"],
        "channel": child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"],
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }


def private_message(player, message, job_id="player-presence-private-message"):
    delivery = mark_private_delivery_attempt(player, message, job_id)
    if delivery and delivery.get("duplicate"):
        return {
            "ok": True,
            "deduped": True,
            "jobId": job_id,
            "target": player_name(player),
            "dedupeKey": delivery["key"],
        }
    fls_id = player_fls_id(player)
    name = player_name(player)
    route = whisper_route_for_fls_id(fls_id)
    if not route["ok"]:
        release_private_delivery_attempt(delivery)
        return {"ok": False, "error": route["error"], "player": name}
    command = env("DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_COMMAND", env("DUNE_PLAYER_PRESENCE_ANNOUNCE_COMMAND", env("DUNE_ADMIN_ANNOUNCE_COMMAND", str(ROOT / "scripts" / "announce.sh"))))
    if command.startswith("/workspace/"):
        command = str(ROOT / command.removeprefix("/workspace/"))
    timeout = int(env("DUNE_PLAYER_PRESENCE_ANNOUNCE_TIMEOUT_SECONDS", env("DUNE_ADMIN_ANNOUNCEMENT_COMMAND_TIMEOUT_SECONDS", "45")))
    child_env = os.environ.copy()
    child_env.update(FILE_ENV)
    child_env["DUNE_ANNOUNCE_MESSAGE"] = message
    child_env["DUNE_ANNOUNCE_JOB_ID"] = job_id
    child_env["DUNE_ANNOUNCE_ENV_OVERRIDES_FILE"] = "true"
    child_env["DUNE_ANNOUNCE_ENV_FILE"] = "/dev/null"
    child_env["DUNE_ANNOUNCE_CHAT_ENSURE_ACCOUNT"] = "false"
    child_env["DUNE_ANNOUNCE_ALLOW_MANAGEMENT_PUBLISH"] = "false"
    child_env["DUNE_ANNOUNCE_WRAP_DASHBOARD_MESSAGES"] = "false"
    child_env["DUNE_ANNOUNCE_CHAT_EXCHANGE"] = env("DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_EXCHANGE", "chat.whispers")
    child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"] = env("DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_CHANNEL", "Whispers")
    child_env["DUNE_ANNOUNCE_CHAT_USER_NAME_TO"] = name
    child_env["DUNE_ANNOUNCE_CHAT_TARGET_FLS_IDS"] = route["routingKey"]
    child_env["DUNE_ANNOUNCE_CHAT_TARGET_QUEUES"] = route["queue"]
    child_env["DUNE_ANNOUNCE_CHAT_ROUTING_KEYS"] = env("DUNE_PLAYER_PRESENCE_PRIVATE_MESSAGE_ROUTING_KEY", route["routingKey"]) or route["routingKey"]
    child_env["DUNE_ANNOUNCE_CHAT_BIND_ONLINE_QUEUES"] = "false"
    child_env["DUNE_ANNOUNCE_CHAT_CLEANUP_TARGET_BINDINGS"] = "true"
    try:
        result = subprocess.run([command, message], cwd=ROOT, env=child_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except Exception:
        release_private_delivery_attempt(delivery)
        raise
    if result.returncode != 0:
        release_private_delivery_attempt(delivery)
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "targetFlsId": route["routingKey"],
        "targetQueue": route["queue"],
        "channel": child_env["DUNE_ANNOUNCE_CHAT_CHANNEL"],
        "userNameTo": name,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }


def legacy_base_recovery_reminder_config():
    if not env_bool("DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_ENABLED", False):
        return None

    values = {}
    for name in ("ACCOUNT_ID", "BACKUP_ID", "TOTEM_ID"):
        key = f"DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_{name}"
        raw = env(key, "").strip()
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a positive integer") from exc
        if value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        values[{"ACCOUNT_ID": "accountId", "BACKUP_ID": "backupId", "TOTEM_ID": "totemId"}[name]] = value
    values["message"] = env("DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDER_MESSAGE", BASE_RECOVERY_REMINDER_MESSAGE)
    values["enabled"] = True
    return values


def base_recovery_reminder_status(config):
    """Return conservative live status for the staged native BRT backup.

    A missing backup alone is not enough to declare success: the original
    totem must exist, Mara must have an active permission rank on it, and the
    totem's linked actors must no longer be marked BaseBackup. Query failures
    deliberately return restored=False so the reminder remains active.
    """
    sql = base_recovery_reminders.status_sql(config)
    try:
        result = run(
            compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-F", "\t", "-c", sql),
            timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
        )
    except Exception as exc:
        return {"ok": False, "restored": False, "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "restored": False, "error": result.stderr.strip() or "base recovery status query failed"}
    return base_recovery_reminders.parse_status_output(result.stdout)


def configured_base_recovery_reminders():
    configured_path = env("DUNE_PLAYER_PRESENCE_BASE_RECOVERY_REMINDERS_FILE", "").strip()
    path = base_recovery_reminders.registry_path(configured_path or None)
    if path.exists():
        return base_recovery_reminders.list_reminders(path)
    legacy = legacy_base_recovery_reminder_config()
    return [legacy] if legacy else []


def base_recovery_reminder(current, state):
    """Whisper one recovery reminder per login session until native restore."""
    results = []
    try:
        reminders = configured_base_recovery_reminders()
    except Exception as exc:
        return [{"event": "base-recovery-reminder", "ok": False, "error": str(exc)}]
    runtime = state.setdefault("baseRecoveryReminders", {})
    for config in reminders:
        account_id = str(config["accountId"])
        entry = runtime.setdefault(account_id, {})
        fingerprint = base_recovery_reminders.config_fingerprint(config)
        if entry.get("configFingerprint") != fingerprint:
            entry.clear()
            entry["configFingerprint"] = fingerprint
        status = base_recovery_reminder_status(config)
        entry["lastStatus"] = status
        entry["lastStatusCheckedAt"] = now_iso()
        if status.get("restored") or entry.get("restoredAt"):
            if status.get("restored") and not entry.get("restoredAt"):
                entry["restoredAt"] = now_iso()
            entry["active"] = False
            continue

        entry["active"] = True
        player = current.get(account_id)
        if not player:
            continue
        session = player_presence_session(player)
        if not session:
            # lastLoginTime is normally populated by the game. If a partial
            # query omits it, keep a stable marker for this online interval.
            session = f"online:{player_fls_id(player) or account_id}"
        if entry.get("lastSentSession") == session:
            continue

        session_key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        job_id = f"player-presence-base-recovery-{account_id}-{session_key}"
        send = private_message(player, config["message"], job_id)
        result = {
            "event": "base-recovery-reminder",
            "accountId": account_id,
            "player": player_name(player),
            "session": session,
            "message": config["message"],
            "status": status,
            "send": send,
        }
        results.append(result)
        entry["lastAttemptAt"] = now_iso()
        if send.get("ok"):
            entry["lastSentSession"] = session
            entry["lastSentAt"] = now_iso()
    return results


def vehicle_recovery_reminder_status(config):
    """Return conservative status for every native parked-vehicle backup."""
    sql = vehicle_recovery_reminders.status_sql(config)
    try:
        result = run(
            compose_cmd("exec", "-T", "postgres", "psql", "-U", "dune", "-d", DB, "-At", "-c", sql),
            timeout=int(env("DUNE_PLAYER_PRESENCE_SQL_TIMEOUT_SECONDS", "10")),
        )
    except Exception as exc:
        return {"ok": False, "restored": False, "vehicles": [], "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "restored": False, "vehicles": [], "error": result.stderr.strip() or "vehicle recovery status query failed"}
    return vehicle_recovery_reminders.parse_status_output(result.stdout)


def configured_vehicle_recovery_reminders():
    configured_path = env("DUNE_PLAYER_PRESENCE_VEHICLE_RECOVERY_REMINDERS_FILE", "").strip()
    path = vehicle_recovery_reminders.registry_path(configured_path or None)
    if not path.exists():
        return []
    return vehicle_recovery_reminders.list_reminders(path)


def vehicle_recovery_reminder(current, state):
    """Whisper one parked-vehicle recovery reminder per login session."""
    results = []
    try:
        reminders = configured_vehicle_recovery_reminders()
    except Exception as exc:
        return [{"event": "vehicle-recovery-reminder", "ok": False, "error": str(exc)}]
    runtime = state.setdefault("vehicleRecoveryReminders", {})
    for config in reminders:
        account_id = str(config["accountId"])
        entry = runtime.setdefault(account_id, {})
        fingerprint = vehicle_recovery_reminders.config_fingerprint(config)
        if entry.get("configFingerprint") != fingerprint:
            entry.clear()
            entry["configFingerprint"] = fingerprint
        status = vehicle_recovery_reminder_status(config)
        entry["lastStatus"] = status
        entry["lastStatusCheckedAt"] = now_iso()
        if status.get("restored") or entry.get("restoredAt"):
            if status.get("restored") and not entry.get("restoredAt"):
                entry["restoredAt"] = now_iso()
            entry["active"] = False
            continue
        entry["active"] = True
        player = current.get(account_id)
        if not player:
            continue
        session = player_presence_session(player) or f"online:{player_fls_id(player) or account_id}"
        if entry.get("lastSentSession") == session:
            continue
        session_key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        job_id = f"player-presence-vehicle-recovery-{account_id}-{session_key}"
        send = private_message(player, config["message"], job_id)
        result = {
            "event": "vehicle-recovery-reminder",
            "accountId": account_id,
            "player": player_name(player),
            "vehicleIds": config["vehicleIds"],
            "session": session,
            "message": config["message"],
            "status": status,
            "send": send,
        }
        results.append(result)
        entry["lastAttemptAt"] = now_iso()
        if send.get("ok"):
            entry["lastSentSession"] = session
            entry["lastSentAt"] = now_iso()
    return results


def configured_player_login_notices():
    configured_path = env("DUNE_PLAYER_PRESENCE_LOGIN_NOTICES_FILE", "").strip()
    path = player_login_notices.registry_path(configured_path or None)
    return player_login_notices.list_notices(path)


def player_login_notice(current, state):
    """Whisper one queued notice on the first successful login delivery."""
    results = []
    try:
        notices = configured_player_login_notices()
    except Exception as exc:
        return [{"event": "player-login-notice", "ok": False, "error": str(exc)}]
    runtime = state.setdefault("playerLoginNotices", {})
    now = now_ts()
    for config in notices:
        account_id = str(config["accountId"])
        entry = runtime.setdefault(account_id, {})
        fingerprint = player_login_notices.config_fingerprint(config)
        if entry.get("configFingerprint") != fingerprint:
            entry.clear()
            entry["configFingerprint"] = fingerprint
        if not config.get("enabled", True) or entry.get("deliveredAt"):
            entry["active"] = False
            continue

        entry["active"] = True
        player = current.get(account_id)
        if not player:
            continue
        session = player_presence_session(player) or f"online:{player_fls_id(player) or account_id}"
        if (
            entry.get("lastAttemptSession") == session
            and now - int(entry.get("lastAttemptEpoch") or 0) < 60
        ):
            continue

        session_key = hashlib.sha256(f"{session}|{fingerprint}".encode("utf-8")).hexdigest()[:16]
        job_id = f"player-presence-login-notice-{account_id}-{session_key}"
        send = private_message(player, config["message"], job_id)
        entry["lastAttemptAt"] = now_iso()
        entry["lastAttemptEpoch"] = now
        entry["lastAttemptSession"] = session
        result = {
            "event": "player-login-notice",
            "accountId": account_id,
            "player": player_name(player),
            "session": session,
            "message": config["message"],
            "send": send,
        }
        results.append(result)
        if send.get("ok"):
            entry["active"] = False
            entry["deliveredAt"] = now_iso()
            entry["deliveredSession"] = session
    return results


def private_join_message(player, message):
    return private_message(player, message, "player-presence-private-welcome")


def render_template(template, player_name, count, **extra):
    context = {
        "playername": player_name,
        "player_name": player_name,
        "count": count,
        "player_count": count,
        "server_name": env("DUNE_PLAYER_PRESENCE_SERVER_NAME", env("DUNE_SERVER_DISPLAY_NAME", env("WORLD_NAME", "this server"))),
        "server_url": env("DUNE_PLAYER_PRESENCE_SERVER_URL", env("DUNE_PUBLIC_SITE_URL", "")),
        "rules_url": env("DUNE_PLAYER_PRESENCE_RULES_URL", env("DUNE_PLAYER_PRESENCE_SERVER_URL", env("DUNE_PUBLIC_SITE_URL", ""))),
    }
    context.update(extra)
    return template.format(**context)


def render_journey_template(template, player_name, story_node_id):
    return template.format(playername=player_name, player_name=player_name, story_node_id=story_node_id)


def active_restart_targets():
    now = time.time()
    targets = []
    for path in (RESTART_STATE_FILE, ANNOUNCEMENT_STATE_FILE):
        for job in load_json_file(path, {"jobs": []}).get("jobs", []):
            status = job.get("status")
            run_at = float(job.get("runAt") or job.get("restartAt") or 0)
            if status in ("scheduled", "delivering") and run_at > now:
                targets.append({"id": job.get("id"), "runAt": run_at, "message": job.get("message", ""), "source": path.name})
    return targets


def recently_executed_restarts(window_seconds):
    now = time.time()
    jobs = []
    for job in load_json_file(RESTART_STATE_FILE, {"jobs": []}).get("jobs", []):
        executed_at = float(job.get("executedAt") or 0)
        if job.get("status") == "executed" and executed_at and now - executed_at <= window_seconds:
            jobs.append({"id": job.get("id"), "executedAt": executed_at, "target": job.get("targetLabel") or job.get("target") or "server"})
    return jobs


def send_private(player, template, count, event, account_id, extra=None):
    extra = extra or {}
    message = render_template(template, player_name(player), count, **extra)
    return {"event": event, "accountId": account_id, "message": message, "send": private_message(player, message, f"player-presence-{event}")}


def send_admin_private(current, template, count, event, extra=None):
    results = []
    for account_id, player in admin_players(current).items():
        results.append(send_private(player, template, count, event, account_id, extra))
    return results


def admin_anomaly_digest_template(stuck_count, stuck_names, over_base_cap):
    parts = []
    if stuck_count:
        parts.append(f"stuck/recent anomalies={stuck_count} ({stuck_names})")
    if over_base_cap:
        parts.append(f"over base cap={over_base_cap}")
    if not parts:
        return ""
    return "Admin digest: " + "; ".join(parts) + "."


def admin_anomaly_digest_signature(stuck, over_base_cap):
    stuck_ids = sorted(str(item.get("accountId") or item.get("name") or "") for item in stuck if item.get("accountId") or item.get("name"))
    return json.dumps({"stuck": stuck_ids, "overBaseCap": int(over_base_cap or 0)}, sort_keys=True)


def compact_map_counts(players_by_map, limit=6):
    items = [(name, int(count or 0)) for name, count in (players_by_map or {}).items() if int(count or 0) > 0]
    items.sort(key=lambda item: (-item[1], item[0].lower()))
    if not items:
        return "no reported map population"
    shown = [f"{name}: {count}" for name, count in items[:limit]]
    if len(items) > limit:
        shown.append(f"+{len(items) - limit} more")
    return ", ".join(shown)


def record_digest(state, event, audience, message, payload=None):
    entry = {
        "ts": now_iso(),
        "event": event,
        "audience": audience,
        "message": message,
        "payload": payload or {},
    }
    log = state.setdefault("adminDigestLog", [])
    log.append(entry)
    del log[:-int(env("DUNE_PLAYER_PRESENCE_DIGEST_LOG_LIMIT", "250"))]
    return entry


def send_admin_digest(current, state, template, count, event, extra=None):
    extra = extra or {}
    message = render_template(template, "admin", count, **extra)
    record_digest(state, event, "admin", message, extra)
    results = []
    for account_id, player in admin_players(current).items():
        results.append({"event": event, "accountId": account_id, "message": message, "send": private_message(player, message, f"player-presence-{event}")})
    return results


def check_once():
    state = load_state()
    current = online_players()
    player_login_notice_results = player_login_notice(current, state)
    base_recovery_reminder_results = base_recovery_reminder(current, state)
    vehicle_recovery_reminder_results = vehicle_recovery_reminder(current, state)
    previous = state.get("onlinePlayers")
    current_ids = set(current)
    previous_ids = set(previous or {})
    first_run = previous is None
    current_time = now_ts()
    transfer_grace = transfer_grace_seconds()
    pending_leaves = state.setdefault("pendingLeaves", {})
    suppressed_transfer_ids = set()
    joined = sorted_account_ids(current_ids - previous_ids, current)
    session_rejoined = []
    if previous and not first_run:
        for account_id in sorted_account_ids(current_ids & previous_ids, current):
            if session_change_counts_as_rejoin(previous.get(account_id), current.get(account_id)):
                session_rejoined.append(account_id)
    raw_left = sorted_account_ids(previous_ids - current_ids, previous or {}) if previous else []
    left = raw_left
    left_players = {account_id: (previous or {}).get(account_id, account_id) for account_id in raw_left}
    if transfer_grace > 0 and not first_run:
        for account_id in raw_left:
            key = str(account_id)
            if key not in pending_leaves:
                pending_leaves[key] = {
                    "leftAt": current_time,
                    "player": (previous or {}).get(account_id, account_id),
                }

        left = []
        left_players = {}
        for key, entry in list(pending_leaves.items()):
            if key in current_ids:
                previous_player = entry.get("player")
                if current_time - int(entry.get("leftAt") or current_time) <= transfer_grace and player_location_changed(previous_player, current.get(key)):
                    suppressed_transfer_ids.add(key)
                pending_leaves.pop(key, None)
                continue
            if current_time - int(entry.get("leftAt") or current_time) >= transfer_grace:
                left.append(key)
                left_players[key] = entry.get("player") or key
                pending_leaves.pop(key, None)
        left = sorted_account_ids(left, left_players)

    joined = sorted_account_ids((set(joined) | set(session_rejoined)) - suppressed_transfer_ids, current)
    session_rejoined = sorted_account_ids(set(session_rejoined) - suppressed_transfer_ids, current)
    final_count = len(current)
    seen_accounts = set(str(item) for item in state.get("seenAccounts", []))
    join_delay = max(0, int(env("DUNE_PLAYER_PRESENCE_JOIN_MESSAGE_DELAY_SECONDS", "0")))
    pending_join_messages = state.setdefault("pendingJoinMessages", {})
    for account_id in left:
        pending_join_messages.pop(str(account_id), None)
    if join_delay > 0 and not first_run:
        detected_joined = set(joined)
        detected_session_rejoined = set(session_rejoined)
        for account_id in detected_joined:
            key = str(account_id)
            session = player_presence_session(current.get(account_id)) or ""
            entry = pending_join_messages.get(key)
            if not entry or entry.get("session") != session:
                entry = {"firstSeen": current_time, "session": session, "sessionRejoined": account_id in detected_session_rejoined}
            else:
                entry["sessionRejoined"] = bool(entry.get("sessionRejoined")) or account_id in detected_session_rejoined
            pending_join_messages[key] = entry
        ready_joined = []
        ready_session_rejoined = []
        for key, entry in list(pending_join_messages.items()):
            if key not in current_ids:
                pending_join_messages.pop(key, None)
                continue
            first_seen = int(entry.get("firstSeen") or current_time)
            if current_time - first_seen >= join_delay:
                ready_joined.append(key)
                if entry.get("sessionRejoined"):
                    ready_session_rejoined.append(key)
                pending_join_messages.pop(key, None)
        joined = sorted(ready_joined, key=lambda account_id: player_name(current.get(account_id, account_id)).lower())
        session_rejoined = sorted(ready_session_rejoined, key=lambda account_id: player_name(current.get(account_id, account_id)).lower())

    subfief_bonus_results = []
    if joined and not first_run:
        try:
            subfief_bonus_results = ensure_subfief_limit_bonus(joined)
        except Exception as exc:
            subfief_bonus_results = [{"ok": False, "error": str(exc), "accountIds": joined}]

    results = []
    if env_bool("DUNE_PLAYER_PRESENCE_ANNOUNCE_ENABLED", False) and not first_run:
        join_template = env("DUNE_PLAYER_PRESENCE_JOIN_TEMPLATE", "Welcome {playername}! Current player count is now {count}.")
        return_join_template = env(
            "DUNE_PLAYER_PRESENCE_RETURN_JOIN_TEMPLATE",
            env("DUNE_PLAYER_PRESENCE_WELCOME_BACK_TEMPLATE", "Welcome back {playername}! Current player count is now {count}."),
        )
        leave_template = env("DUNE_PLAYER_PRESENCE_LEAVE_TEMPLATE", "{playername} has left, current count is {count}.")
        for account_id in joined:
            event = "join-returning" if str(account_id) in seen_accounts else "join-first-time"
            template = return_join_template if str(account_id) in seen_accounts else join_template
            message = render_template(template, player_name(current.get(account_id, account_id)), final_count)
            results.append({"event": event, "accountId": account_id, "message": message, "announce": public_announce(message, current, f"player-presence-{event}")})
        for account_id in left:
            message = render_template(leave_template, player_name(left_players.get(account_id, account_id)), final_count)
            results.append({"event": "leave", "accountId": account_id, "message": message, "announce": public_announce(message, current, "player-presence-leave")})

    private_welcome_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_ENABLED", False) and not first_run:
        template = env("DUNE_PLAYER_PRESENCE_PRIVATE_WELCOME_TEMPLATE", "Welcome! Please Check https://snape.tech for Server Rules.")
        for account_id in joined:
            player = current.get(account_id, account_id)
            message = render_template(template, player_name(player), final_count)
            private_welcome_results.append({"event": "private-welcome", "accountId": account_id, "message": message, "send": private_join_message(player, message)})

    automated_private_results = []
    if not first_run:
        hagga_arrivals = set(str(item) for item in state.get("haggaArrivalMessaged", []))
        recent_leaves = state.setdefault("recentLeaves", {})

        if env_bool("DUNE_PLAYER_PRESENCE_FIRST_SEEN_ENABLED", False):
            template = env("DUNE_PLAYER_PRESENCE_FIRST_SEEN_TEMPLATE", "Welcome to {server_name}. This is a friendly PvE server. Please keep shared paths, spawns, and resources clear. Rules: {rules_url}")
            for account_id in joined:
                if str(account_id) not in seen_accounts:
                    automated_private_results.append(send_private(current[account_id], template, final_count, "first-seen", account_id))
                    seen_accounts.add(str(account_id))
        seen_accounts.update(str(account_id) for account_id in joined)

        join_counts = state.setdefault("joinCounts", {})
        repo_star_messaged = set(str(item) for item in state.get("repoStarMessaged", []))
        for account_id in joined:
            key = str(account_id)
            join_counts[key] = int(join_counts.get(key, 0) or 0) + 1
        if env_bool("DUNE_PLAYER_PRESENCE_REPO_STAR_THIRD_JOIN_ENABLED", True):
            threshold = int(env("DUNE_PLAYER_PRESENCE_REPO_STAR_JOIN_COUNT", "3"))
            template = env("DUNE_PLAYER_PRESENCE_REPO_STAR_TEMPLATE", "Glad to have you back on {server_name}. If this server and self-host stack have been useful, please consider starring the project: https://github.com/snapetech/DuneAwakeningSelfHost")
            for account_id in joined:
                key = str(account_id)
                if key not in repo_star_messaged and int(join_counts.get(key, 0) or 0) >= threshold:
                    automated_private_results.append(send_private(current[account_id], template, final_count, "repo-star-third-join", account_id, {"join_count": join_counts[key]}))
                    repo_star_messaged.add(key)
        state["repoStarMessaged"] = sorted(repo_star_messaged, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))

        if env_bool("DUNE_PLAYER_PRESENCE_HAGGA_ARRIVAL_ENABLED", False):
            template = env("DUNE_PLAYER_PRESENCE_HAGGA_ARRIVAL_TEMPLATE", "You made it to Hagga Basin. Build with room around roads, spawns, resource areas, and points of interest. Rules: {rules_url}")
            for account_id, player in current.items():
                if str(account_id) not in hagga_arrivals and player_map(player) == "HaggaBasin":
                    automated_private_results.append(send_private(player, template, final_count, "hagga-arrival", account_id))
                    hagga_arrivals.add(str(account_id))

        if env_bool("DUNE_PLAYER_PRESENCE_DEEP_DESERT_FIRST_ENABLED", False):
            deep_desert_seen = set(str(item) for item in state.get("deepDesertMessaged", []))
            template = env("DUNE_PLAYER_PRESENCE_DEEP_DESERT_FIRST_TEMPLATE", "Deep Desert is high risk. Expect sandstorms, sandworms, and harsher recovery. Support: {server_url}")
            for account_id, player in current.items():
                if str(account_id) not in deep_desert_seen and "DeepDesert" in player_map(player):
                    automated_private_results.append(send_private(player, template, final_count, "deep-desert-first", account_id))
                    deep_desert_seen.add(str(account_id))
            state["deepDesertMessaged"] = sorted(deep_desert_seen, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))

        if env_bool("DUNE_PLAYER_PRESENCE_DEEP_DESERT_JOIN_MESSAGES_ENABLED", False):
            templates = {
                "casual": env(
                    "DUNE_PLAYER_PRESENCE_DEEP_DESERT_CASUAL_JOIN_TEMPLATE",
                    env("DUNE_PLAYER_PRESENCE_DEEP_DESERT_PVE_JOIN_TEMPLATE", "PVE Casual ({partition_label}): persistent PvE Deep Desert, standard harvest, no weekly cleanup, no Shifting Sands reset."),
                ),
                "hardcore": env(
                    "DUNE_PLAYER_PRESENCE_DEEP_DESERT_HARDCORE_JOIN_TEMPLATE",
                    env("DUNE_PLAYER_PRESENCE_DEEP_DESERT_PVP_JOIN_TEMPLATE", "PVE Hardcore ({partition_label}): PvE combat, 3x harvest, high sandstorm/Coriolis damage, Shifting Sands, 15% higher vehicle wear, and weekly Hardcore DD cleanup during maintenance."),
                ),
            }
            for account_id, player in current.items():
                instance = deep_desert_instance(player)
                if not instance:
                    continue
                previous_player = (previous or {}).get(account_id)
                if account_id not in joined and not player_location_changed(previous_player, player):
                    continue
                template = templates.get(instance, templates["casual"])
                automated_private_results.append(send_private(
                    player,
                    template,
                    final_count,
                    f"deep-desert-{instance}-join",
                    account_id,
                    {"partition_label": player_location_label(player), "partition_id": player_partition_id(player)},
                ))

        if env_bool("DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_ENABLED", False):
            window = int(env("DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_WINDOW_SECONDS", "600"))
            template = env("DUNE_PLAYER_PRESENCE_RECONNECT_RECOVERY_TEMPLATE", "Welcome back. If you were disconnected during travel, wait a moment before retrying the same transition.")
            for account_id in joined:
                left_at = int(recent_leaves.get(str(account_id), 0) or 0)
                if (left_at and current_time - left_at <= window) or account_id in session_rejoined:
                    automated_private_results.append(send_private(current[account_id], template, final_count, "reconnect-recovery", account_id))

        if env_bool("DUNE_PLAYER_PRESENCE_BASE_REMINDERS_ENABLED", False):
            base_counts = base_claim_counts()
            cap = configured_base_cap()
            near_cap = int(env("DUNE_PLAYER_PRESENCE_BASE_NEAR_CAP", str(max(1, cap - 1))))
            notices = state.setdefault("baseClaimNotices", {})
            near_template = env("DUNE_PLAYER_PRESENCE_BASE_NEAR_CAP_TEMPLATE", "Base reminder: this server has a {base_cap} landclaim cap per Hagga Basin map. Please clean up unused claims. Rules: {rules_url}")
            over_template = env("DUNE_PLAYER_PRESENCE_BASE_OVER_CAP_TEMPLATE", "Heads up: you appear to be over the {base_cap} landclaim cap. Please clean up unused claims. Rules: {rules_url}")
            first_template = env("DUNE_PLAYER_PRESENCE_FIRST_BASE_TEMPLATE", "Base reminder: please avoid blocking shared paths, NPCs, resources, caves, wrecks, and POIs. Rules: {rules_url}")
            for account_id in joined:
                counts = base_counts.get(str(account_id), {"baseCount": 0, "segmentCount": 0})
                base_count = counts["baseCount"]
                last_notice = notices.get(str(account_id), {})
                if base_count <= 0:
                    continue
                if not last_notice.get("firstBase"):
                    automated_private_results.append(send_private(current[account_id], first_template, final_count, "first-base", account_id, {"base_cap": cap}))
                    last_notice["firstBase"] = True
                if base_count > cap and last_notice.get("overBaseCount") != base_count:
                    automated_private_results.append(send_private(current[account_id], over_template, final_count, "base-over-cap", account_id, {"base_cap": cap}))
                    last_notice["overBaseCount"] = base_count
                elif base_count >= near_cap and last_notice.get("nearBaseCount") != base_count:
                    automated_private_results.append(send_private(current[account_id], near_template, final_count, "base-near-cap", account_id, {"base_cap": cap}))
                    last_notice["nearBaseCount"] = base_count
                notices[str(account_id)] = last_notice

        if env_bool("DUNE_PLAYER_PRESENCE_STUCK_POSITION_ENABLED", False):
            template = env("DUNE_PLAYER_PRESENCE_STUCK_POSITION_TEMPLATE", "Your position looks unusual. If you are stuck, message an admin before relogging repeatedly.")
            stuck_state = state.setdefault("stuckPositionNotices", {})
            for account_id, player in current.items():
                location_known = bool(player_map(player) and player.get("x") and player.get("y")) if isinstance(player, dict) else False
                if not location_known and stuck_state.get(str(account_id)) != now_iso()[:10]:
                    automated_private_results.append(send_private(player, template, final_count, "stuck-position", account_id))
                    stuck_state[str(account_id)] = now_iso()[:10]

        state["seenAccounts"] = sorted(seen_accounts, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        state["haggaArrivalMessaged"] = sorted(hagga_arrivals, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        for account_id in raw_left:
            recent_leaves[str(account_id)] = current_time
        for account_id in suppressed_transfer_ids:
            recent_leaves.pop(str(account_id), None)
        for account_id, left_at in list(recent_leaves.items()):
            if current_time - int(left_at or 0) > 86400:
                recent_leaves.pop(account_id, None)

    starter_grant_results = []
    starter_message_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_ENABLED", True) and not first_run:
        granted = state.setdefault("starterBaseToolGranted", [])
        granted_ids = set(str(account_id) for account_id in granted)
        pending = state.setdefault("starterBaseToolPending", {})
        if isinstance(pending, list):
            pending = {str(account_id): {"queuedAt": current_time} for account_id in pending}
        pending = {str(account_id): dict(value) if isinstance(value, dict) else {"queuedAt": current_time} for account_id, value in pending.items()}
        retry_seconds = max(15, int(env("DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_RETRY_SECONDS", "60")))
        message_enabled = env_bool("DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_MESSAGE_ENABLED", True)
        message_template = env("DUNE_PLAYER_PRESENCE_STARTER_BASE_TOOL_MESSAGE_TEMPLATE", STARTER_BASE_TOOL_MESSAGE)

        # Queue new players while they are online. Direct item writes during an
        # active session can be overwritten by the game's later persistence save.
        for account_id in joined:
            key = str(account_id)
            if key not in granted_ids and key not in pending:
                pending[key] = {"queuedAt": current_time}

        # Retry queued grants only after the player is offline. This also repairs
        # a failed first attempt without re-granting accounts already marked as
        # complete.
        for key, entry in sorted(pending.items()):
            if key in current_ids:
                continue
            last_attempt = int(entry.get("lastAttemptAt") or 0)
            if last_attempt and current_time - last_attempt < retry_seconds:
                continue
            entry["lastAttemptAt"] = current_time
            result = grant_starter_base_tool(key)
            starter_grant_results.append({"event": "starter-base-tool", "accountId": key, "grant": result})
            if result.get("ok"):
                granted_ids.add(key)
                pending.pop(key, None)
                # The player is offline by design here; the item will be visible
                # on the next login, so do not emit a misleading live whisper.
                if message_enabled:
                    starter_message_results.append({
                        "event": "starter-base-tool-message",
                        "accountId": key,
                        "message": render_template(message_template, key, final_count),
                        "send": {"ok": True, "skipped": True, "reason": "player offline; item available on next login"},
                    })
        state["starterBaseToolGranted"] = sorted(granted_ids, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        state["starterBaseToolPending"] = pending

    starter_emote_grant_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_STARTER_EMOTES_ENABLED", True) and not first_run:
        granted = state.setdefault("starterEmotesGranted", [])
        granted_ids = set(str(account_id) for account_id in granted)
        for account_id in joined:
            if str(account_id) in granted_ids:
                continue
            result = grant_starter_emotes(account_id)
            starter_emote_grant_results.append({"event": "starter-emotes", "accountId": account_id, "grant": result})
            if result.get("ok"):
                granted_ids.add(str(account_id))
        state["starterEmotesGranted"] = sorted(granted_ids, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))

    faction_chat_seed_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_FACTION_CHAT_SEED_ENABLED", False):
        try:
            seed_result = seed_faction_chat(
                online_only=True,
                execute=env_bool("DUNE_PLAYER_PRESENCE_FACTION_CHAT_SEED_EXECUTE", False),
            )
            faction_chat_seed_results = seed_result.get("results", [])
        except Exception as exc:
            faction_chat_seed_results = [{"ok": False, "error": str(exc)}]

    journey_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_VERMILIUS_GAP_ENABLED", False):
        story_node_id = env("DUNE_PLAYER_PRESENCE_VERMILIUS_GAP_NODE", "DA_SQ_VermiliusGap.Relocate.RelocateOutsideHBS.Drive north to the Vermilius Gap")
        try:
            completed = completed_journey_players(story_node_id)
            journey_state = state.setdefault("announcedJourneyNodes", {})
            previous_completed = set(journey_state.get(story_node_id, []))
            current_completed = set(completed)
            journey_first_run = story_node_id not in journey_state
            newly_completed = sorted(current_completed - previous_completed, key=lambda account_id: completed.get(account_id, account_id).lower())
            if not journey_first_run:
                template = env("DUNE_PLAYER_PRESENCE_VERMILIUS_GAP_TEMPLATE", "Congrats! {playername} has outrun Shai-Hulud!")
                for account_id in newly_completed:
                    message = render_journey_template(template, completed.get(account_id, account_id), story_node_id)
                    journey_results.append({"event": "vermilius-gap", "accountId": account_id, "message": message, "announce": public_announce(message, current, "player-presence-vermilius-gap")})
            journey_state[story_node_id] = sorted(current_completed)
        except Exception as exc:
            journey_results.append({"event": "vermilius-gap", "ok": False, "error": str(exc)})

    restart_private_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_RESTART_PRIVATE_WARNINGS_ENABLED", False):
        marks = [int(item.strip()) for item in env("DUNE_PLAYER_PRESENCE_RESTART_PRIVATE_WARNING_MARKS_SECONDS", "1800,600,300,60").split(",") if item.strip()]
        sent = state.setdefault("restartPrivateWarnings", {})
        template = env("DUNE_PLAYER_PRESENCE_RESTART_PRIVATE_WARNING_TEMPLATE", "Server maintenance in about {remaining}. Please get to a safe place.")
        for job in active_restart_targets():
            remaining = max(0, int(job["runAt"] - time.time()))
            due_marks = [mark for mark in marks if remaining <= mark]
            if not due_marks:
                continue
            mark = min(due_marks)
            sent_key = f"{job['id']}:{mark}"
            sent_accounts = set(sent.get(sent_key, []))
            remaining_text = f"{max(1, round(remaining / 60))} minutes" if remaining >= 90 else f"{remaining} seconds"
            for account_id, player in current.items():
                if str(account_id) in sent_accounts:
                    continue
                message = render_template(template, player_name(player), final_count, remaining=remaining_text, remaining_seconds=remaining)
                restart_private_results.append({"event": "restart-private-warning", "accountId": account_id, "jobId": job["id"], "mark": mark, "message": message, "send": private_message(player, message, "player-presence-restart-warning")})
                sent_accounts.add(str(account_id))
            sent[sent_key] = sorted(sent_accounts)

    post_restart_results = []
    if env_bool("DUNE_PLAYER_PRESENCE_POST_RESTART_RETURN_ENABLED", False) and joined:
        window = int(env("DUNE_PLAYER_PRESENCE_POST_RESTART_WINDOW_SECONDS", "3600"))
        sent = state.setdefault("postRestartReturnMessages", {})
        template = env("DUNE_PLAYER_PRESENCE_POST_RESTART_TEMPLATE", "Maintenance is complete. If anything looks wrong, report it through {server_url}.")
        for job in recently_executed_restarts(window):
            sent_accounts = set(sent.get(job["id"], []))
            for account_id in joined:
                if str(account_id) in sent_accounts:
                    continue
                player = current[account_id]
                post_restart_results.append(send_private(player, template, final_count, "post-restart-return", account_id))
                sent_accounts.add(str(account_id))
            sent[job["id"]] = sorted(sent_accounts)

    public_status_results = []
    admin_alert_results = []
    health = None
    if (
        env_bool("DUNE_PLAYER_PRESENCE_MAP_HEALTH_PUBLIC_ENABLED", False)
        or env_bool("DUNE_PLAYER_PRESENCE_MAP_HEALTH_ADMIN_ENABLED", False)
        or env_bool("DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_ENABLED", False)
        or env_bool("DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_ENABLED", False)
    ):
        health = map_health_summary()
        expected = int(health.get("expected") or 0)
        online = int(health.get("online") or 0)
        health_state = "healthy" if expected > 0 and online == expected else "degraded"
        previous_health_state = state.get("mapHealthState")
        state["mapHealthState"] = health_state

        if env_bool("DUNE_PLAYER_PRESENCE_MAP_HEALTH_PUBLIC_ENABLED", False) and previous_health_state and previous_health_state != health_state:
            if health_state == "degraded":
                template = env("DUNE_PLAYER_PRESENCE_MAP_HEALTH_DEGRADED_TEMPLATE", "Server notice: some travel destinations are recovering ({online}/{expected} online). If travel fails, wait 1-2 minutes and retry.")
            else:
                template = env("DUNE_PLAYER_PRESENCE_MAP_HEALTH_RECOVERED_TEMPLATE", "Server notice: all travel destinations are online again ({online}/{expected}).")
            message = render_template(template, "server", final_count, online=online, expected=expected)
            public_status_results.append({"event": f"map-health-{health_state}", "message": message, "announce": public_announce(message, current, f"player-presence-map-health-{health_state}")})

        if env_bool("DUNE_PLAYER_PRESENCE_MAP_HEALTH_ADMIN_ENABLED", False) and health_state == "degraded":
            interval = int(env("DUNE_PLAYER_PRESENCE_ADMIN_ALERT_INTERVAL_SECONDS", "900"))
            last = int(state.get("lastAdminMapHealthAlertAt", 0) or 0)
            if current_time - last >= interval:
                offline = ", ".join((health.get("offlineLabels") or [])[:8]) or "unknown"
                template = env("DUNE_PLAYER_PRESENCE_MAP_HEALTH_ADMIN_TEMPLATE", "Admin alert: map health degraded, {online}/{expected} online. Offline: {offline_maps}")
                admin_alert_results.extend(send_admin_private(current, template, final_count, "admin-map-health", {"online": online, "expected": expected, "offline_maps": offline}))
                state["lastAdminMapHealthAlertAt"] = current_time

        if env_bool("DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_ENABLED", False):
            thresholds = sorted([int(item.strip()) for item in env("DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_THRESHOLDS", "30,35,40").split(",") if item.strip()])
            band = 0
            for threshold in thresholds:
                if final_count >= threshold:
                    band = threshold
            previous_band = int(state.get("populationPublicBand", 0) or 0)
            if band and band != previous_band:
                template = env("DUNE_PLAYER_PRESENCE_POPULATION_PUBLIC_TEMPLATE", "Server is getting busy: {count} online. Travel and loading may take longer.")
                message = render_template(template, "server", final_count)
                public_status_results.append({"event": "population-threshold", "threshold": band, "message": message, "announce": public_announce(message, current, "player-presence-population-threshold")})
            state["populationPublicBand"] = band

        if env_bool("DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_ENABLED", False):
            interval = int(env("DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_INTERVAL_SECONDS", "1800"))
            last = int(state.get("lastAdminPopulationDigestAt", 0) or 0)
            if current_time - last >= interval:
                map_counts = compact_map_counts(health.get("playersByMap") or {})
                template = env("DUNE_PLAYER_PRESENCE_POPULATION_ADMIN_DIGEST_TEMPLATE", "Admin population: {count} online. Maps: {map_counts}")
                admin_alert_results.extend(send_admin_private(current, template, final_count, "admin-population-digest", {"map_counts": map_counts}))
                state["lastAdminPopulationDigestAt"] = current_time

    if env_bool("DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_ENABLED", False):
        window = int(env("DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_WINDOW_SECONDS", "600"))
        threshold = int(env("DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_THRESHOLD", "3"))
        reconnects = state.setdefault("reconnectSupportEvents", {})
        sent = state.setdefault("reconnectSupportSent", {})
        template = env("DUNE_PLAYER_PRESENCE_RECONNECT_SUPPORT_TEMPLATE", "Looks like you have reconnected several times. If travel is stuck, report it through {server_url}.")
        for account_id in joined:
            events = [int(ts) for ts in reconnects.get(str(account_id), []) if current_time - int(ts) <= window]
            events.append(current_time)
            reconnects[str(account_id)] = events
            if len(events) >= threshold and current_time - int(sent.get(str(account_id), 0) or 0) >= window:
                player = current[account_id]
                automated_private_results.append(send_private(player, template, final_count, "reconnect-support", account_id))
                sent[str(account_id)] = current_time
        for account_id, events in list(reconnects.items()):
            remaining = [int(ts) for ts in events if current_time - int(ts) <= window]
            if remaining:
                reconnects[account_id] = remaining
            else:
                reconnects.pop(account_id, None)

    if env_bool("DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_ENABLED", False):
        interval = int(env("DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_INTERVAL_SECONDS", "1800"))
        last = int(state.get("lastAdminAnomalyDigestAt", 0) or 0)
        if current_time - last >= interval:
            try:
                stuck = stuck_transition_players(int(env("DUNE_ADMIN_BOT_STUCK_TRANSITION_MINUTES", "10")))
                base_counts = base_claim_counts()
                cap = configured_base_cap()
                over_cap = sum(1 for item in base_counts.values() if item.get("baseCount", 0) > cap)
                stuck_names = ", ".join(
                    f"{item['name']} {item.get('staleMinutes', '?')}m {item.get('map') or 'unknown-map'}"
                    for item in stuck[:5]
                ) or "none"
                template = env("DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_TEMPLATE", ADMIN_ANOMALY_DIGEST_TEMPLATE)
                if template == ADMIN_ANOMALY_DIGEST_TEMPLATE:
                    template = admin_anomaly_digest_template(len(stuck), stuck_names, over_cap)
                if template:
                    signature = admin_anomaly_digest_signature(stuck, over_cap)
                    repeat_unchanged = env_bool("DUNE_PLAYER_PRESENCE_ADMIN_ANOMALY_DIGEST_REPEAT_UNCHANGED", False)
                    if repeat_unchanged or signature != state.get("lastAdminAnomalyDigestSignature"):
                        admin_alert_results.extend(send_admin_private(current, template, final_count, "admin-anomaly-digest", {"stuck_count": len(stuck), "stuck_names": stuck_names, "over_base_cap": over_cap}))
                        state["lastAdminAnomalyDigestSignature"] = signature
                else:
                    state.pop("lastAdminAnomalyDigestSignature", None)
                state["lastAdminAnomalyDigestAt"] = current_time
            except Exception as exc:
                admin_alert_results.append({"event": "admin-anomaly-digest", "ok": False, "error": str(exc)})

    if env_bool("DUNE_PLAYER_PRESENCE_ADMIN_MAP_ROSTER_DIGEST_ENABLED", False):
        interval = int(env("DUNE_PLAYER_PRESENCE_ADMIN_MAP_ROSTER_DIGEST_INTERVAL_SECONDS", "1800"))
        last = int(state.get("lastAdminMapRosterDigestAt", 0) or 0)
        if current_time - last >= interval:
            roster = player_roster_by_map()
            roster_text = compact_roster_by_map(roster)
            template = env("DUNE_PLAYER_PRESENCE_ADMIN_MAP_ROSTER_DIGEST_TEMPLATE", "Admin roster: {count} online. {map_roster}")
            admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-map-roster-digest", {"map_roster": roster_text, "roster": roster}))
            state["lastAdminMapRosterDigestAt"] = current_time

    if env_bool("DUNE_PLAYER_PRESENCE_ADMIN_UNIQUE_DAILY_DIGEST_ENABLED", False):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        daily = state.setdefault("dailyUniquePlayers", {})
        seen_today = set(daily.get(today, []))
        seen_today.update(current_ids)
        daily[today] = sorted(seen_today, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        for old_day in list(daily):
            if old_day != today:
                daily.pop(old_day, None)
        interval = int(env("DUNE_PLAYER_PRESENCE_ADMIN_UNIQUE_DAILY_DIGEST_INTERVAL_SECONDS", "86400"))
        last = int(state.get("lastAdminUniqueDailyDigestAt", 0) or 0)
        if current_time - last >= interval:
            template = env("DUNE_PLAYER_PRESENCE_ADMIN_UNIQUE_DAILY_DIGEST_TEMPLATE", "Admin daily players: {unique_count} unique accounts seen today, {count} currently online.")
            admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-unique-daily-digest", {"unique_count": len(seen_today)}))
            state["lastAdminUniqueDailyDigestAt"] = current_time

    if env_bool("DUNE_PLAYER_PRESENCE_MAINTENANCE_ONLINE_ADMIN_ENABLED", False):
        targets = active_restart_targets()
        if targets:
            window = int(env("DUNE_PLAYER_PRESENCE_MAINTENANCE_ONLINE_WINDOW_SECONDS", "1800"))
            for job in targets:
                remaining = int(job["runAt"] - time.time())
                key = f"{job['id']}:online"
                if 0 <= remaining <= window and key not in state.setdefault("maintenanceOnlineDigests", []):
                    roster = compact_roster_by_map(player_roster_by_map())
                    template = env("DUNE_PLAYER_PRESENCE_MAINTENANCE_ONLINE_ADMIN_TEMPLATE", "Admin maintenance check: {count} players still online before maintenance. {map_roster}")
                    admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-maintenance-online", {"map_roster": roster, "remaining_seconds": remaining}))
                    state["maintenanceOnlineDigests"].append(key)

    if env_bool("DUNE_PLAYER_PRESENCE_MAP_WITH_PLAYERS_UNHEALTHY_ADMIN_ENABLED", False):
        if health is None:
            health = map_health_summary()
        offline = set(health.get("offlineLabels") or [])
        players_by_map = health.get("playersByMap") or {}
        impacted = [f"{name}: {players_by_map.get(name)}" for name in offline if int(players_by_map.get(name) or 0) > 0]
        if impacted:
            digest_key = hashlib.sha256("|".join(sorted(impacted)).encode("utf-8")).hexdigest()
            if state.get("lastImpactedMapDigestKey") != digest_key:
                template = env("DUNE_PLAYER_PRESENCE_MAP_WITH_PLAYERS_UNHEALTHY_ADMIN_TEMPLATE", "Admin alert: unhealthy maps still report players: {impacted_maps}")
                admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-map-players-unhealthy", {"impacted_maps": "; ".join(impacted)}))
                state["lastImpactedMapDigestKey"] = digest_key

    if env_bool("DUNE_PLAYER_PRESENCE_PUBLIC_MAINTENANCE_CANCELLED_ENABLED", False):
        since = float(state.get("lastMaintenanceCancelScanAt", current_time - 3600) or 0)
        cancelled = cancelled_jobs_since(since)
        state["lastMaintenanceCancelScanAt"] = current_time
        if cancelled:
            template = env("DUNE_PLAYER_PRESENCE_PUBLIC_MAINTENANCE_CANCELLED_TEMPLATE", "Maintenance has been cancelled or delayed. Normal play can continue.")
            message = render_template(template, "server", final_count)
            record_digest(state, "maintenance-cancelled-public", "public", message, {"cancelled": cancelled[:5]})
            public_status_results.append({"event": "maintenance-cancelled", "message": message, "announce": public_announce(message, current, "player-presence-maintenance-cancelled")})

    if env_bool("DUNE_PLAYER_PRESENCE_INCIDENT_MODE_PUBLIC_ENABLED", False):
        incident_on = env_bool("DUNE_PLAYER_PRESENCE_INCIDENT_MODE_ACTIVE", False)
        previous = state.get("incidentModeActive")
        state["incidentModeActive"] = incident_on
        if previous is not None and previous != incident_on:
            if incident_on:
                template = env("DUNE_PLAYER_PRESENCE_INCIDENT_MODE_ON_TEMPLATE", "Server notice: admins are investigating travel instability. Updates at {server_url}.")
                event = "incident-mode-on"
            else:
                template = env("DUNE_PLAYER_PRESENCE_INCIDENT_MODE_OFF_TEMPLATE", "Server notice: incident mode is cleared. Normal play can continue.")
                event = "incident-mode-off"
            message = render_template(template, "server", final_count)
            record_digest(state, event, "public", message)
            public_status_results.append({"event": event, "message": message, "announce": public_announce(message, current, f"player-presence-{event}")})

    if env_bool("DUNE_PLAYER_PRESENCE_INFRA_ADMIN_ALERTS_ENABLED", False):
        interval = int(env("DUNE_PLAYER_PRESENCE_ADMIN_ALERT_INTERVAL_SECONDS", "900"))
        last = int(state.get("lastAdminInfraAlertAt", 0) or 0)
        if current_time - last >= interval:
            checks = service_health_checks()
            failed = [item for item in checks if not item.get("ok")]
            backup = latest_backup_age_hours()
            max_backup_age = float(env("DUNE_PLAYER_PRESENCE_BACKUP_MAX_AGE_HOURS", "24"))
            if backup is None:
                failed.append({"name": "backup", "status": "no backup found"})
            elif backup["ageHours"] > max_backup_age:
                failed.append({"name": "backup", "status": f"{backup['ageHours']}h old"})
            if failed:
                summary = "; ".join(f"{item['name']} {item.get('status', '')}".strip() for item in failed[:6])
                template = env("DUNE_PLAYER_PRESENCE_INFRA_ADMIN_ALERT_TEMPLATE", "Admin alert: infrastructure check failed: {infra_failures}")
                admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-infra-alert", {"infra_failures": summary}))
                state["lastAdminInfraAlertAt"] = current_time

    if env_bool("DUNE_PLAYER_PRESENCE_RESTART_FAILURE_ADMIN_ENABLED", False):
        since = float(state.get("lastRestartFailureScanAt", current_time - 3600) or 0)
        failed_jobs = failed_restart_jobs_since(since)
        state["lastRestartFailureScanAt"] = current_time
        if failed_jobs:
            summary = "; ".join(f"{job['target']} {job['id']}: {job['error']}" for job in failed_jobs[:3])
            template = env("DUNE_PLAYER_PRESENCE_RESTART_FAILURE_ADMIN_TEMPLATE", "Admin alert: restart job failed: {restart_failures}")
            admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-restart-failure", {"restart_failures": summary, "jobs": failed_jobs[:3]}))

    if env_bool("DUNE_PLAYER_PRESENCE_ADMIN_MUTATION_DIGEST_ENABLED", False):
        interval = int(env("DUNE_PLAYER_PRESENCE_ADMIN_MUTATION_DIGEST_INTERVAL_SECONDS", "1800"))
        last = int(state.get("lastAdminMutationDigestAt", 0) or 0)
        if current_time - last >= interval:
            audit = audit_counts_since(int(state.get("adminMutationAuditOffset", 0) or 0))
            state["adminMutationAuditOffset"] = audit["offset"]
            if audit["mutations"]:
                top = sorted(audit["counts"].items(), key=lambda item: (-item[1], item[0]))[:6]
                summary = ", ".join(f"{name}={count}" for name, count in top)
                template = env("DUNE_PLAYER_PRESENCE_ADMIN_MUTATION_DIGEST_TEMPLATE", "Admin write digest: {mutation_count} write-ish events. {mutation_summary}")
                admin_alert_results.extend(send_admin_digest(current, state, template, final_count, "admin-mutation-digest", {"mutation_count": audit["mutations"], "mutation_summary": summary}))
            state["lastAdminMutationDigestAt"] = current_time

    if env_bool("DUNE_PLAYER_PRESENCE_TRANSFER_POLICY_PUBLIC_ENABLED", False):
        transfer_hash = file_hash(["config/director.ini"])
        previous_hash = state.get("transferPolicyHash")
        state["transferPolicyHash"] = transfer_hash
        if previous_hash and transfer_hash and transfer_hash != previous_hash:
            template = env("DUNE_PLAYER_PRESENCE_TRANSFER_POLICY_PUBLIC_TEMPLATE", "Server notice: character transfer policy changed. Changes may apply after the next Director restart. Details: {server_url}")
            message = render_template(template, "server", final_count)
            record_digest(state, "transfer-policy-changed", "public", message)
            public_status_results.append({"event": "transfer-policy-changed", "message": message, "announce": public_announce(message, current, "player-presence-transfer-policy-changed")})

    if env_bool("DUNE_PLAYER_PRESENCE_RULES_CHANGE_PUBLIC_ENABLED", False):
        rule_paths = [item.strip() for item in env("DUNE_PLAYER_PRESENCE_RULES_HASH_FILES", "public-site/static/index.html").split(",") if item.strip()]
        rules_hash = file_hash(rule_paths)
        previous_hash = state.get("rulesHash")
        state["rulesHash"] = rules_hash
        if previous_hash and rules_hash and rules_hash != previous_hash:
            template = env("DUNE_PLAYER_PRESENCE_RULES_CHANGE_PUBLIC_TEMPLATE", "Server rules were updated. Please review {rules_url}.")
            message = render_template(template, "server", final_count)
            record_digest(state, "rules-changed", "public", message, {"files": rule_paths})
            public_status_results.append({"event": "rules-changed", "message": message, "announce": public_announce(message, current, "player-presence-rules-changed")})

    if env_bool("DUNE_PLAYER_PRESENCE_PEAK_PUBLIC_ENABLED", False):
        thresholds = sorted([int(item.strip()) for item in env("DUNE_PLAYER_PRESENCE_PEAK_PUBLIC_THRESHOLDS", "10,20,30,40").split(",") if item.strip()])
        today = dt.datetime.now().strftime("%Y-%m-%d")
        peak_state = state.setdefault("dailyPeak", {"date": today, "peak": 0, "announced": []})
        if peak_state.get("date") != today:
            peak_state = {"date": today, "peak": 0, "announced": []}
        if final_count > int(peak_state.get("peak", 0) or 0):
            peak_state["peak"] = final_count
        announced = set(int(item) for item in peak_state.get("announced", []))
        for threshold in thresholds:
            if final_count >= threshold and threshold not in announced:
                template = env("DUNE_PLAYER_PRESENCE_PEAK_PUBLIC_TEMPLATE", "New daily player peak: {count} online.")
                message = render_template(template, "server", final_count)
                record_digest(state, "daily-peak-public", "public", message, {"threshold": threshold})
                public_status_results.append({"event": "daily-peak", "threshold": threshold, "message": message, "announce": public_announce(message, current, "player-presence-daily-peak")})
                announced.add(threshold)
        peak_state["announced"] = sorted(announced)
        state["dailyPeak"] = peak_state

    if env_bool("DUNE_PLAYER_PRESENCE_DAILY_STATUS_PUBLIC_ENABLED", False):
        interval = int(env("DUNE_PLAYER_PRESENCE_DAILY_STATUS_INTERVAL_SECONDS", "86400"))
        last = int(state.get("lastDailyStatusAt", 0) or 0)
        if current_time - last >= interval:
            if health is None:
                health = map_health_summary()
            online = int(health.get("online") or 0)
            expected = int(health.get("expected") or 0)
            template = env("DUNE_PLAYER_PRESENCE_DAILY_STATUS_PUBLIC_TEMPLATE", "Daily status: {online}/{expected} maps online, {count} players online. Next maintenance is the normal configured window.")
            message = render_template(template, "server", final_count, online=online, expected=expected)
            record_digest(state, "daily-status-public", "public", message, {"online": online, "expected": expected})
            public_status_results.append({"event": "daily-status", "message": message, "announce": public_announce(message, current, "player-presence-daily-status")})
            state["lastDailyStatusAt"] = current_time

    if env_bool("DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DAILY_ENABLED", True) and joined:
        today = dt.datetime.now().strftime("%Y-%m-%d")
        sent = state.setdefault("adminDailyLoginDigestSent", {})
        digest_log = state.get("adminDigestLog", [])
        latest_messages = []
        for entry in reversed(digest_log):
            if entry.get("audience") == "admin" and entry.get("message") not in latest_messages:
                latest_messages.append(entry.get("message"))
            if len(latest_messages) >= int(env("DUNE_PLAYER_PRESENCE_ADMIN_FIRST_LOGIN_DIGEST_LIMIT", "8")):
                break
        if not latest_messages:
            latest_messages = ["No admin digests have been recorded yet today."]
        for account_id in joined:
            player = current.get(account_id)
            if account_id in admin_players(current) and sent.get(str(account_id)) != today:
                message = "Admin daily digest:\n" + "\n".join(f"- {item}" for item in reversed(latest_messages))
                admin_alert_results.append({"event": "admin-first-login-daily", "accountId": account_id, "message": message, "send": private_message(player, message, "player-presence-admin-daily-login")})
                record_digest(state, "admin-first-login-daily", "admin", message, {"accountId": account_id})
                sent[str(account_id)] = today

    state["onlinePlayers"] = current
    state["updatedAt"] = now_iso()
    save_state(state)
    return {
        "ok": True,
        "firstRun": first_run,
        "onlineCount": final_count,
        "joined": [player_name(current[account_id]) for account_id in joined],
        "left": [player_name(left_players.get(account_id, account_id)) for account_id in left],
        "sessionRejoined": [player_name(current[account_id]) for account_id in session_rejoined],
        "suppressedTransfers": [player_name(current[account_id]) for account_id in sorted_account_ids(suppressed_transfer_ids & current_ids, current)],
        "announcements": results,
        "subfiefBonusRepairs": subfief_bonus_results,
        "privateWelcomeMessages": private_welcome_results,
        "automatedPrivateMessages": automated_private_results,
        "baseRecoveryReminderMessages": base_recovery_reminder_results,
        "vehicleRecoveryReminderMessages": vehicle_recovery_reminder_results,
        "playerLoginNoticeMessages": player_login_notice_results,
        "starterBaseToolGrants": starter_grant_results,
        "starterBaseToolMessages": starter_message_results,
        "starterEmoteGrants": starter_emote_grant_results,
        "factionChatSeeds": faction_chat_seed_results,
        "journeyAnnouncements": journey_results,
        "restartPrivateWarnings": restart_private_results,
        "postRestartReturnMessages": post_restart_results,
        "publicStatusAnnouncements": public_status_results,
        "adminAlertMessages": admin_alert_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Announce Dune player join/leave events through the configured Paul/DASH Admin chat path.")
    parser.add_argument("--once", action="store_true", help="Run one poll and exit.")
    parser.add_argument("--loop", action="store_true", help="Poll forever.")
    parser.add_argument("--seed-faction-chat-backfill", action="store_true", help="Plan or apply faction chat seeding for existing players.")
    parser.add_argument("--all-players", action="store_true", help="With --seed-faction-chat-backfill, include offline players.")
    parser.add_argument("--execute", action="store_true", help="Execute --seed-faction-chat-backfill instead of dry-run planning.")
    args = parser.parse_args()
    if args.seed_faction_chat_backfill:
        print(json.dumps(seed_faction_chat(online_only=not args.all_players, execute=args.execute), indent=2), flush=True)
        return 0
    if not args.once and not args.loop:
        args.once = True
    while True:
        try:
            print(json.dumps(check_once(), indent=2), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc), "ts": now_iso()}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(int(env("DUNE_PLAYER_PRESENCE_POLL_SECONDS", "15")))


if __name__ == "__main__":
    sys.exit(main())
