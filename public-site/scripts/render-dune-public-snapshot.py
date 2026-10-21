#!/usr/bin/env python3
import base64
import datetime
import html
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys


DUNE_ROOT = pathlib.Path(os.environ.get("DUNE_ROOT", "/opt/DuneAwakeningSelfHost"))
STATIC_DIR = pathlib.Path(os.environ.get("STATIC_DIR", "/srv/dash-public-site"))
for module_root in (DUNE_ROOT / "admin", pathlib.Path(__file__).resolve().parents[2] / "admin"):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))
import public_directory

WIDTH = 1600
HEIGHT = 1600
PUBLIC_VIEWBOX_WIDTH = 2133.333
PUBLIC_X_SCALE = PUBLIC_VIEWBOX_WIDTH / WIDTH
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "dune_server")
_POSTGRES_CONTAINER = ""


def public_env_value(key, default=""):
    return os.environ.get(key) or default


DATABASE = os.environ.get("DUNE_PUBLIC_STATUS_DB_NAME", "dune_sb_1_5_15_0")
PEAKS_FILE = pathlib.Path(public_env_value(
    "DUNE_PLAYER_PEAKS_FILE",
    str(DUNE_ROOT / "backups" / "admin-panel" / "player-peaks.json"),
))



def int_list_env(key, default):
    values = []
    for raw in public_env_value(key, default).split(","):
        raw = raw.strip()
        if raw.isdigit() and int(raw) > 0:
            values.append(int(raw))
    return values or [1, 2]


CORE_PARTITION_IDS = int_list_env("DUNE_PUBLIC_STATUS_CORE_PARTITION_IDS", "1,2")


def bool_env(key, default):
    value = public_env_value(key, default)
    return str(value).lower() not in ("0", "false", "no", "off")


def float_env(key, default):
    try:
        return float(public_env_value(key, default))
    except ValueError:
        return float(default)


CAL = {
    "min_x": float_env("DUNE_HAGGA_MAP_MIN_X", "-457200"),
    "max_x": float_env("DUNE_HAGGA_MAP_MAX_X", "355600"),
    "min_y": float_env("DUNE_HAGGA_MAP_MIN_Y", "-457200"),
    "max_y": float_env("DUNE_HAGGA_MAP_MAX_Y", "355600"),
    "invert_x": bool_env("DUNE_HAGGA_MAP_INVERT_X", "false"),
    "invert_y": bool_env("DUNE_HAGGA_MAP_INVERT_Y", "false"),
    "image_min_u": float_env("DUNE_HAGGA_MAP_IMAGE_MIN_U", "0"),
    "image_max_u": float_env("DUNE_HAGGA_MAP_IMAGE_MAX_U", "1"),
    "image_min_v": float_env("DUNE_HAGGA_MAP_IMAGE_MIN_V", "0"),
    "image_max_v": float_env("DUNE_HAGGA_MAP_IMAGE_MAX_V", "1"),
}


def postgres_container():
    global _POSTGRES_CONTAINER
    if _POSTGRES_CONTAINER:
        return _POSTGRES_CONTAINER
    output = subprocess.check_output(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT_NAME}",
            "--filter",
            "label=com.docker.compose.service=postgres",
            "--format",
            "{{.ID}}",
        ],
        cwd=DUNE_ROOT,
        text=True,
        timeout=10,
    )
    _POSTGRES_CONTAINER = output.strip().splitlines()[0] if output.strip() else ""
    if not _POSTGRES_CONTAINER:
        raise RuntimeError("no running Postgres container found for the public status project")
    return _POSTGRES_CONTAINER


def docker_psql(sql):
    cmd = [
        "docker",
        "exec",
        postgres_container(),
        "psql",
        "-U",
        "dune",
        "-d",
        DATABASE,
        "-X",
        "-q",
        "-t",
        "-A",
        "-c",
        "copy (" + sql + ") to stdout",
    ]
    return subprocess.check_output(cmd, cwd=DUNE_ROOT, text=True, timeout=20)


def load_rows():
    sql = r"""
        select coalesce(json_agg(row_to_json(t)), '[]'::json)
        from (
            select coalesce(ps.character_name, 'Unnamed') as character_name,
                   ps.online_status::text as online_status,
                   ps.life_state::text as life_state,
                   coalesce(wp.label, fs.map, ps.server_id, 'Unknown') as public_location,
                   fs.map as farm_map,
                   a.map as actor_map,
                   ((a.transform).location).x::float8 as x,
                   ((a.transform).location).y::float8 as y
            from dune.player_state ps
            left join dune.farm_state fs on fs.server_id = ps.server_id
            left join dune.world_partition wp on wp.server_id = ps.server_id
            left join dune.actors a on a.id = ps.player_pawn_id
            where ps.online_status::text = 'Online'
            order by ps.character_name nulls last, ps.account_id
        ) t
    """
    text = docker_psql(sql).strip() or "[]"
    return json.loads(text)


def load_deep_desert_markers():
    sql = r"""
        select coalesce(json_agg(row_to_json(t)), '[]'::json)
        from (
            select m.marker_hash_id,
                   m.dimension_index,
                   m.area_id,
                   m.marker_type,
                   (m.position).x::float8 as x,
                   (m.position).y::float8 as y,
                   (m.position).z::float8 as z,
                   m.area_radius::float8 as area_radius
            from dune.markers m
            join dune.map_names mn on mn.map_name_id = m.map_name_id
            where mn.map_name = 'DeepDesert'
            order by m.dimension_index, m.marker_type, m.marker_hash_id
        ) t
    """
    text = docker_psql(sql).strip() or "[]"
    return json.loads(text)


def load_deep_desert_layout_state():
    sql = r"""
        select json_build_object(
            'seeds', json_build_object(
                'farm', (select world_reset_seed from dune.world_farm_reset_seed where onerow_id = true),
                'map', (select world_reset_seed from dune.world_map_reset_seed where map = 'DeepDesert'),
                'partitions', coalesce((
                    select json_agg(row_to_json(t) order by t.dimension_index, t.partition_id)
                    from (
                        select wp.partition_id, wp.dimension_index, wprs.world_reset_seed
                        from dune.world_partition wp
                        left join dune.world_partition_reset_seed wprs on wprs.partition_id = wp.partition_id
                        where wp.map = 'DeepDesert_1'
                    ) t
                ), '[]'::json)
            ),
            'shiftingSands', coalesce((
                select json_agg(row_to_json(s) order by s.id)
                from (
                    select id, alpha::float8 as alpha, x::float8 as x, y::float8 as y,
                           extract(epoch from last_modified_time)::bigint as last_modified_time
                    from dune.shiftingsands_data
                ) s
            ), '[]'::json),
            'resourceFields', coalesce((
                select json_agg(row_to_json(r) order by r.dimension_index, r.field_id, r.spawn_time)
                from (
                    select map, dimension_index, field_id,
                           spawn_time::float8 as spawn_time,
                           value_remaining::bigint as value_remaining
                    from dune.resourcefield_state
                    where map = 'DeepDesert'
                ) r
            ), '[]'::json),
            'spiceFields', '[]'::json,
            'spiceFieldsAvailable', false
        )
    """
    text = docker_psql(sql).strip() or "{}"
    return json.loads(text)


def load_deep_desert_observations():
    sql = r"""
        select json_build_object(
            'source', 'live DB passive observation',
            'spiceAvailability', '[]'::json,
            'spiceAvailabilityAvailable', false,
            'shipwreckSpawners', coalesce((
                select json_agg(row_to_json(s) order by s.dimension_index, s.name, s.id)
                from (
                    select id, map, dimension_index, name,
                           substring(name from 'CB_WL_[0-9]+') as world_layout_ref
                    from dune.actor_spawners
                    where map = 'DeepDesert'
                      and (
                        name ilike '%WreckedShip%'
                        or name ilike '%Shipwreck%'
                        or name ilike '%CrashSite%'
                        or name ilike '%PatrolShip%'
                      )
                ) s
            ), '[]'::json)
        )
    """
    text = docker_psql(sql).strip() or "{}"
    return json.loads(text)


def load_map_health():
    core_partition_ids = ",".join(str(value) for value in CORE_PARTITION_IDS)
    sql = rf"""
        select coalesce(json_agg(row_to_json(t)), '[]'::json)
        from (
            select coalesce(wp.label, wp.map, 'Unknown') as name,
                   wp.map,
                   case
                     when wp.blocked then 'offline'
                     when wp.server_id is null and wp.partition_id in ({core_partition_ids}) then 'offline'
                     when wp.server_id is null then 'on-demand'
                     when coalesce(fs.ready, false) and coalesce(fs.alive, false) and exists (
                       select 1 from dune.active_server_ids asi where asi.server_id = wp.server_id
                     ) then 'online'
                     when coalesce(fs.alive, false) and exists (
                       select 1 from dune.active_server_ids asi where asi.server_id = wp.server_id
                     ) then 'warming'
                     when wp.partition_id in ({core_partition_ids}) then 'offline'
                     else 'on-demand'
                   end as status,
                   coalesce(fs.ready, false) as ready,
                   coalesce(fs.alive, false) as alive,
                   exists (
                     select 1 from dune.active_server_ids asi where asi.server_id = wp.server_id
                   ) as active,
                   wp.blocked,
                   coalesce(fs.connected_players, 0) as players
            from dune.world_partition wp
            left join dune.farm_state fs on fs.server_id = wp.server_id
            order by wp.partition_id
        ) t
    """
    text = docker_psql(sql).strip() or "[]"
    return json.loads(text)


def summarize_map_health(rows):
    return {
        "online": sum(1 for row in rows if row.get("status") == "online"),
        "warming": sum(1 for row in rows if row.get("status") == "warming"),
        "onDemand": sum(1 for row in rows if row.get("status") == "on-demand"),
        "offline": sum(1 for row in rows if row.get("status") == "offline"),
        "total": len(rows),
    }


def clamp(value, low, high):
    return max(low, min(high, value))


def project(x, y):
    u = (float(x) - CAL["min_x"]) / (CAL["max_x"] - CAL["min_x"])
    v = (float(y) - CAL["min_y"]) / (CAL["max_y"] - CAL["min_y"])
    if CAL["invert_x"]:
        u = 1 - u
    if CAL["invert_y"]:
        v = 1 - v
    u = CAL["image_min_u"] + u * (CAL["image_max_u"] - CAL["image_min_u"])
    v = CAL["image_min_v"] + v * (CAL["image_max_v"] - CAL["image_min_v"])
    return clamp(u * WIDTH, 0, WIDTH), clamp(v * HEIGHT, 0, HEIGHT)


def public_x(x):
    return float(x) * PUBLIC_X_SCALE


def map_image_href(source_map):
    if source_map.exists():
        encoded = base64.b64encode(source_map.read_bytes()).decode("ascii")
        return "data:image/webp;base64," + encoded
    return "/hagga-basin.webp"


def render_svg(players, generated_at, image_href, map_key="HaggaBasin", label="Hagga Basin", image=True):
    plotted = [
        p for p in players
        if p.get("actor_map") in (map_key, f"{map_key}_1") and p.get("x") is not None and p.get("y") is not None
    ]
    grid = []
    for i in range(1, 4):
        pos = WIDTH * i / 4
        grid.append(f'<line x1="{pos:.1f}" y1="0" x2="{pos:.1f}" y2="{HEIGHT}" class="grid"/>')
        grid.append(f'<line x1="0" y1="{pos:.1f}" x2="{WIDTH}" y2="{pos:.1f}" class="grid"/>')
    markers = []
    for player in plotted:
        x, y = project(player["x"], player["y"])
        name = html.escape(str(player.get("character_name") or "Player"))
        sx = public_x(x)
        label_x = clamp(sx + 16, 10, PUBLIC_VIEWBOX_WIDTH - 180)
        label_y = clamp(y - 14, 24, HEIGHT - 20)
        markers.append(
            f'<g><circle cx="{sx:.1f}" cy="{y:.1f}" r="10" class="dot"/>'
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" class="label">{name}</text></g>'
        )
    empty = ""
    if not plotted:
        empty = f'<text x="{WIDTH / 2}" y="{HEIGHT / 2}" class="empty">No online {html.escape(label)} positions.</text>'
    background = f'<image href="{image_href}" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" preserveAspectRatio="none"/>' if image else '<rect x="0" y="0" width="1600" height="1600" fill="#171512"/><path d="M0 1120 C300 1010 540 1240 820 1110 C1120 970 1320 1080 1600 960 L1600 1600 L0 1600 Z" fill="#2a241b" opacity=".75"/><path d="M0 420 C260 320 520 500 770 390 C1100 250 1320 390 1600 260" fill="none" stroke="#8f6b34" stroke-width="10" opacity=".42"/><path d="M0 760 C220 690 520 870 760 740 C1040 590 1300 720 1600 600" fill="none" stroke="#d9a63c" stroke-width="7" opacity=".28"/>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PUBLIC_VIEWBOX_WIDTH:.3f} {HEIGHT}" preserveAspectRatio="none" role="img" aria-label="{html.escape(label)} live player map">
<style>
.shade{{fill:rgba(4,5,4,.28)}}.grid{{stroke:#f1d08a;stroke-width:1;opacity:.24}}.dot{{fill:#78cf7a;stroke:#071007;stroke-width:4}}.label{{fill:#fff;font:700 24px system-ui,sans-serif;paint-order:stroke;stroke:#0b0d0a;stroke-width:7}}.meta{{fill:#c7bba9;font:20px system-ui,sans-serif}}.empty{{fill:#f3eadb;font:26px system-ui,sans-serif;text-anchor:middle;paint-order:stroke;stroke:#0b0d0a;stroke-width:6}}
</style>
<g transform="scale({PUBLIC_X_SCALE:.6f} 1)">
{background}
<rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" class="shade"/>
{''.join(grid)}
</g>
<text x="22" y="38" class="meta">NW</text>
<text x="{PUBLIC_VIEWBOX_WIDTH - 56:.1f}" y="{HEIGHT - 24}" class="meta">SE</text>
<text x="22" y="{HEIGHT - 24}" class="meta">Updated {html.escape(generated_at)}</text>
{''.join(markers)}
{empty}
</svg>
'''


def marker_group(marker_type):
    value = str(marker_type or "Marker")
    if value in ("TaxiService",):
        return "Transit"
    if value in ("Cave", "Ecolab", "HomeBase"):
        return "Locations"
    if "Wreckage" in value or "Part" in value:
        return "Wreckage"
    if "Ore" in value or "Pickup" in value or value in ("BrittleBush",):
        return "Resources"
    return "Markers"


DD_GROUP_COLORS = {
    "Transit": "#6fb6ff",
    "Locations": "#d9a63c",
    "Wreckage": "#b7c0c7",
    "Resources": "#78cf7a",
    "Markers": "#f3eadb",
}
DD_LAYER_LABELS = {
    "resource_nodes": "Resource nodes",
    "resource_fields": "Resource fields",
    "spice_fields": "Spice fields",
    "transit": "Transit",
    "wreckage": "Wreckage",
    "locations": "Locations",
    "saved_db_markers": "Saved DB markers",
}
DD_LAYER_COLORS = {
    "resource_nodes": "#78cf7a",
    "resource_fields": "#e7d35b",
    "spice_fields": "#f16f9a",
    "transit": "#6fb6ff",
    "wreckage": "#b7c0c7",
    "locations": "#d9a63c",
    "saved_db_markers": "#8d96a0",
}


def dd_bounds(markers, players):
    return {"min_x": -1250000, "max_x": 1150000, "min_y": -1250000, "max_y": 1050000}


def dd_area_cell(area_id):
    try:
        index = int(area_id) - 2
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= 81:
        return None
    return index % 9, index // 9


def dd_marker_area_bounds(markers):
    bounds = {}
    for marker in markers:
        cell = dd_area_cell(marker.get("area_id"))
        if not cell:
            continue
        try:
            x = float(marker["x"])
            y = float(marker["y"])
        except (KeyError, TypeError, ValueError):
            continue
        area = int(marker["area_id"])
        row = bounds.setdefault(area, {"min_x": x, "max_x": x, "min_y": y, "max_y": y})
        row["min_x"] = min(row["min_x"], x)
        row["max_x"] = max(row["max_x"], x)
        row["min_y"] = min(row["min_y"], y)
        row["max_y"] = max(row["max_y"], y)
    return bounds


def render_deep_desert_svg(players, markers, generated_at, layout_state=None, observations=None):
    layout_state = layout_state or {}
    observations = observations or {}
    background_href = layout_state.get("backgroundHref") or ""
    bounds = dd_bounds(markers, players)
    span_x = max(bounds["max_x"] - bounds["min_x"], 1)
    span_y = max(bounds["max_y"] - bounds["min_y"], 1)

    def px(x):
        return clamp((float(x) - bounds["min_x"]) / span_x * PUBLIC_VIEWBOX_WIDTH, 0, PUBLIC_VIEWBOX_WIDTH)

    def py(y):
        return clamp((float(y) - bounds["min_y"]) / span_y * HEIGHT, 0, HEIGHT)

    def pp(x, y):
        return px(x), py(y)

    def marker_point(marker):
        return pp(float(marker["x"]), float(marker["y"]))

    legend_items = []

    grid = []
    row_labels = "ABCDEFGHI"
    for i in range(0, 10):
        tx = i / 9
        x = tx * PUBLIC_VIEWBOX_WIDTH
        y = tx * HEIGHT
        world_x = bounds["min_x"] + tx * span_x
        world_y = bounds["min_y"] + tx * span_y
        grid.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{HEIGHT}" class="grid"/>')
        grid.append(f'<line x1="0" y1="{y:.1f}" x2="{PUBLIC_VIEWBOX_WIDTH:.1f}" y2="{y:.1f}" class="grid"/>')
        if i < 9:
            grid.append(f'<text x="{((i + 0.5) / 9 * PUBLIC_VIEWBOX_WIDTH):.1f}" y="118" class="cellLabel">{i + 1}</text>')
            grid.append(f'<text x="34" y="{((i + 0.5) / 9 * HEIGHT):.1f}" class="cellLabel">{row_labels[8 - i]}</text>')
        if i % 2 == 0:
            grid.append(f'<text x="{clamp(x + 8, 10, PUBLIC_VIEWBOX_WIDTH - 180):.1f}" y="24" class="coord">X {round(world_x)}</text>')
            grid.append(f'<text x="10" y="{clamp(y - 8, 44, HEIGHT - 16):.1f}" class="coord">Y {round(world_y)}</text>')

    shifting_nodes = []
    shifting_sands = layout_state.get("shiftingSands") or []
    for row in shifting_sands:
        if row.get("x") is None or row.get("y") is None:
            continue
        x, y = pp(float(row["x"]), float(row["y"]))
        alpha = clamp(float(row.get("alpha") or 0.4), 0.08, 0.82)
        shifting_nodes.append(f'<circle class="shiftingSand" cx="{x:.1f}" cy="{y:.1f}" r="{18 + alpha * 28:.1f}" opacity="{0.24 + alpha * 0.28:.3f}"><title>Static shifting sand {html.escape(str(row.get("id") or ""))}</title></circle>')

    seeds = layout_state.get("seeds") or {}
    spice_fields = layout_state.get("spiceFields") or []
    resource_fields = layout_state.get("resourceFields") or []
    seed_text = f'farm seed {seeds.get("farm", "unknown")}, map seed {seeds.get("map", "unknown")}, shifting sand rows {len(shifting_sands)}'
    spice_text = ", ".join(
        f'{str(row.get("field_type"))} {row.get("current_globally_active")}/{row.get("max_globally_active")}'
        for row in spice_fields
        if int(row.get("dimension_index") or 0) == 0
    ) or "spice state unavailable"

    marker_nodes = []

    player_nodes = []
    for player in players:
        if player.get("actor_map") not in ("DeepDesert", "DeepDesert_1") or player.get("x") is None or player.get("y") is None:
            continue
        x = px(player["x"])
        y = py(player["y"])
        name = html.escape(str(player.get("character_name") or "Player"))
        player_nodes.append(
            f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="11" class="dot"/>'
            f'<text x="{clamp(x + 16, 10, PUBLIC_VIEWBOX_WIDTH - 180):.1f}" y="{clamp(y - 12, 24, HEIGHT - 20):.1f}" class="label">{name}</text></g>'
        )

    empty = "" if player_nodes else '<text x="1066.7" y="830" class="empty">No online Deep Desert positions.</text>'
    spice_availability = observations.get("spiceAvailability") or []
    shipwreck_spawners = observations.get("shipwreckSpawners") or []
    large_inactive = sum(
        int(row.get("inactive_fields_of_type") or 0)
        for row in spice_availability
        if str(row.get("field_type") or "").lower() == "large" and int(row.get("dimension_index") or 0) == 0
    )
    spice_availability_text = (
        str(large_inactive)
        if observations.get("spiceAvailabilityAvailable")
        else "unavailable"
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{PUBLIC_VIEWBOX_WIDTH:.0f}" height="{HEIGHT}" viewBox="0 0 {PUBLIC_VIEWBOX_WIDTH:.3f} {HEIGHT}" preserveAspectRatio="none" role="img" aria-label="Deep Desert operational map derived from server markers">
<style>
.bg{{fill:#171512}}.dune{{fill:#2a241b;opacity:.82}}.ridge{{fill:none;stroke:#d9a63c;stroke-width:7;opacity:.26}}.grid{{stroke:#f1d08a;stroke-width:1;opacity:.18}}.coord,.meta,.legend{{fill:#c7bba9;font:18px system-ui,sans-serif}}.cellLabel{{fill:#e7c875;font:700 22px system-ui,sans-serif;text-anchor:middle;opacity:.68;paint-order:stroke;stroke:#0b0d0a;stroke-width:5}}.dot{{fill:#78cf7a;stroke:#071007;stroke-width:4}}.label{{fill:#fff;font:700 24px system-ui,sans-serif;paint-order:stroke;stroke:#0b0d0a;stroke-width:7}}.pointLabel{{fill:#fff;font:700 18px system-ui,sans-serif;paint-order:stroke;stroke:#0b0d0a;stroke-width:5}}.empty{{fill:#f3eadb;font:26px system-ui,sans-serif;text-anchor:middle;paint-order:stroke;stroke:#0b0d0a;stroke-width:6}}.marker{{opacity:.92;cursor:help}}.shiftingSand{{fill:#e7d59a;stroke:#fff0bc;stroke-width:2}}
</style>
<rect class="bg" x="0" y="0" width="{PUBLIC_VIEWBOX_WIDTH:.3f}" height="{HEIGHT}"/>
<path class="dune" d="M0 1120 C360 990 630 1230 990 1080 C1340 930 1680 1050 2133 880 L2133 1600 L0 1600 Z"/>
<path class="ridge" d="M0 410 C340 290 650 520 980 365 C1370 190 1700 390 2133 245"/>
<path class="ridge" d="M0 760 C300 660 650 890 1010 710 C1390 520 1700 750 2133 565"/>
{''.join(shifting_nodes)}
{''.join(grid)}
{''.join(marker_nodes)}
{''.join(player_nodes)}
{empty}
<rect x="0" y="0" width="{PUBLIC_VIEWBOX_WIDTH:.3f}" height="122" fill="#171512" opacity=".82"/>
<text x="22" y="38" class="meta">Deep Desert Map</text>
<text x="22" y="66" class="meta">{html.escape(seed_text)} | resource fields {len(resource_fields)} | spice {html.escape(spice_text)}</text>
<text x="22" y="94" class="meta">Passive tracker: no resource heatmap points; shipwreck candidates {len(shipwreck_spawners)}; inactive Large spice candidates {spice_availability_text}</text>
<text x="22" y="1576" class="meta">Updated {html.escape(generated_at)} | verified player points only | saved DB markers and unlocated observations are not authoritative map points</text>
{''.join(legend_items)}
</svg>
'''


def update_daily_peak(count, now):
    today = now.strftime("%Y-%m-%d")
    now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"days": {}}
    try:
        if PEAKS_FILE.exists():
            data = json.loads(PEAKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {"days": {}}
    days = data.setdefault("days", {})
    day = days.setdefault(today, {})
    previous_peak = int(day.get("peak") or 0)
    if count >= previous_peak:
        day["peak"] = int(count)
        day["peakAt"] = now_text
    else:
        day["peak"] = previous_peak
    day["last"] = int(count)
    day["lastAt"] = now_text
    data["today"] = today
    data["peakToday"] = int(day.get("peak") or count)
    data["peakTodayAt"] = day.get("peakAt") or now_text
    data["lastCount"] = int(count)
    data["lastCountAt"] = now_text
    try:
        PEAKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PEAKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(PEAKS_FILE)
    except Exception:
        pass
    return {
        "date": today,
        "peak": data["peakToday"],
        "peakAt": data["peakTodayAt"],
        "last": int(count),
        "lastAt": now_text,
    }


def write_snapshot_error(errors, generated):
    error_file = STATIC_DIR / "players-error.json"
    payload = {
        "ok": False,
        "generatedAt": generated,
        "errors": errors,
        "preserved": (STATIC_DIR / "players.json").exists(),
    }
    error_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def request_directory_signature(snapshot, config):
    signer_config = {
        "DUNE_PUBLIC_DIRECTORY_ENABLED": "true",
        "DUNE_PUBLIC_DIRECTORY_ENTRY_URL": config["entryUrl"],
        "DUNE_PUBLIC_SITE_URL": config["siteUrl"],
        "DUNE_PUBLIC_DIRECTORY_REGION": config["region"],
        "DUNE_PUBLIC_DIRECTORY_CAPACITY": str(config["capacity"]),
        "DUNE_PUBLIC_DIRECTORY_TTL_SECONDS": str(config["ttlSeconds"]),
        "DUNE_PUBLIC_DIRECTORY_DISCORD_INVITE": config["discordInvite"],
        "DUNE_PUBLIC_DIRECTORY_NAME": config["name"],
        "DUNE_PUBLIC_DIRECTORY_DESCRIPTION": config["description"],
        "DUNE_IMAGE_TAG": config["build"],
    }
    signer_snapshot = {
        "ok": bool(snapshot.get("ok")),
        "onlineCount": int(snapshot.get("onlineCount") or 0),
        "mapHealth": snapshot.get("mapHealth") or {},
        "sietches": sum(
            1 for row in (snapshot.get("mapStatus") or [])
            if str(row.get("map") or "").lower() in ("survival_1", "survival")
        ),
    }
    request = json.dumps(
        {"config": signer_config, "snapshot": signer_snapshot},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(request) > 262_144:
        raise ValueError("public directory signing request exceeds its size limit")

    socket_path = os.environ.get(
        "DUNE_PUBLIC_DIRECTORY_SIGNER_SOCKET",
        "/run/dune-public-directory-signer.sock",
    )
    response = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(socket_path)
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        while len(response) <= 262_144:
            chunk = client.recv(min(16_384, 262_145 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if b"\n" in chunk:
                break
    if len(response) > 262_144:
        raise ValueError("public directory signer response exceeds its size limit")
    result = json.loads(bytes(response).split(b"\n", 1)[0])
    if not isinstance(result, dict) or result.get("ok") is not True or result.get("enabled") is not True:
        raise RuntimeError("public directory signer rejected the request")
    entry = result.get("entry")
    if not isinstance(entry, dict):
        raise ValueError("public directory signer returned no entry")
    public_directory.verify_entry(entry, expected_url=config["entryUrl"])
    return entry


def main():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    generated_dt = datetime.datetime.now(datetime.timezone.utc)
    generated = generated_dt.strftime("%Y-%m-%d %H:%M UTC")
    errors = {}
    try:
        players = load_rows()
    except Exception as exc:
        players = []
        errors["players"] = str(exc)
    try:
        map_health = load_map_health()
    except Exception as exc:
        map_health = []
        errors["mapHealth"] = str(exc)
    try:
        deep_desert_markers = load_deep_desert_markers()
    except Exception as exc:
        deep_desert_markers = []
        errors["deepDesertMarkers"] = str(exc)
    try:
        deep_desert_layout = load_deep_desert_layout_state()
    except Exception as exc:
        deep_desert_layout = {}
        errors["deepDesertLayout"] = str(exc)
    try:
        deep_desert_observations = load_deep_desert_observations()
    except Exception as exc:
        deep_desert_observations = {"source": "unavailable", "spiceAvailability": [], "shipwreckSpawners": []}
        errors["deepDesertObservations"] = str(exc)

    if ("players" in errors or "mapHealth" in errors) and (STATIC_DIR / "players.json").exists():
        write_snapshot_error(errors, generated)
        print(f"preserved existing players.json after snapshot failure: {errors}", file=sys.stderr)
        return 1

    ok = not errors
    error = "; ".join(f"{key}: {value}" for key, value in errors.items()) or None

    public_players = [
        {
            "name": p.get("character_name") or "Unnamed",
            "location": p.get("public_location") or "Unknown",
            "lifeState": p.get("life_state") or "",
            "onHaggaMap": p.get("actor_map") == "HaggaBasin" and p.get("x") is not None and p.get("y") is not None,
        }
        for p in players
    ]
    daily_peak = update_daily_peak(len(public_players), generated_dt)
    map_health_summary = summarize_map_health(map_health)
    snapshot = {
        "ok": ok,
        "generatedAt": generated,
        "onlineCount": len(public_players),
        "peakToday": daily_peak["peak"],
        "peakTodayAt": daily_peak["peakAt"],
        "peakDate": daily_peak["date"],
        "haggaPlotted": sum(1 for p in public_players if p["onHaggaMap"]),
        "deepDesertPlotted": sum(1 for p in players if p.get("actor_map") in ("DeepDesert", "DeepDesert_1") and p.get("x") is not None and p.get("y") is not None),
        "mapHealth": map_health_summary,
        "mapStatus": map_health,
        "deepDesertMarkers": len(deep_desert_markers),
        "deepDesertLayout": {
            "farmSeed": (deep_desert_layout.get("seeds") or {}).get("farm"),
            "mapSeed": (deep_desert_layout.get("seeds") or {}).get("map"),
            "shiftingSandsRows": len(deep_desert_layout.get("shiftingSands") or []),
            "resourceFields": len(deep_desert_layout.get("resourceFields") or []),
            "spiceFieldsAvailable": bool(deep_desert_layout.get("spiceFieldsAvailable")),
            "spiceFields": [
                {key: value for key, value in row.items() if key not in ("is_spawning_active", "global_spawn_weight")}
                for row in (deep_desert_layout.get("spiceFields") or [])
            ],
        },
        "deepDesertTracker": {
            "shipwreckCandidates": len(deep_desert_observations.get("shipwreckSpawners") or []),
            "largeSpiceInactiveCandidates": sum(
                int(row.get("inactive_fields_of_type") or 0)
                for row in (deep_desert_observations.get("spiceAvailability") or [])
                if str(row.get("field_type") or "").lower() == "large" and int(row.get("dimension_index") or 0) == 0
            ),
            "spiceAvailabilityAvailable": bool(deep_desert_observations.get("spiceAvailabilityAvailable")),
            "source": deep_desert_observations.get("source") or "live DB passive observation",
        },
        "players": public_players,
        "error": error,
    }
    source_map = DUNE_ROOT / "admin" / "static" / "hagga-basin.webp"
    if source_map.exists():
        shutil.copyfile(source_map, STATIC_DIR / "hagga-basin.webp")
    (STATIC_DIR / "deep-desert-observations.json").write_text(json.dumps(deep_desert_observations, indent=2), encoding="utf-8")
    deep_desert_layout["backgroundHref"] = ""
    (STATIC_DIR / "players.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (STATIC_DIR / "hagga-map.svg").write_text(render_svg(players, generated, map_image_href(source_map)), encoding="utf-8")
    (STATIC_DIR / "deep-desert-map.svg").write_text(render_deep_desert_svg(players, deep_desert_markers, generated, deep_desert_layout, deep_desert_observations), encoding="utf-8")
    try:
        directory_config = public_directory.public_config(os.environ, root=DUNE_ROOT)
        directory_output = STATIC_DIR / "directory-entry.json"
        if not directory_config.get("enabled"):
            pathlib.Path(directory_config["stateFile"]).unlink(missing_ok=True)
            directory_output.unlink(missing_ok=True)
        else:
            entry = request_directory_signature(snapshot, directory_config)
            public_directory.atomic_json(directory_config["stateFile"], entry)
            public_directory.atomic_json(directory_output, entry)
    except Exception as exc:
        errors["publicDirectory"] = type(exc).__name__
        print(f"public directory publication failed: {type(exc).__name__}", file=sys.stderr)
    return 0 if ok and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
