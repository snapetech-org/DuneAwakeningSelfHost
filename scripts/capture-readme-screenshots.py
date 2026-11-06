#!/usr/bin/env python3
"""Capture README admin screenshots from the current UI with safe fixture data."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADMIN_SOURCE = ROOT / "admin" / "admin_panel.py"
CAPTURE_SCRIPT = pathlib.Path(__file__).resolve()
ASSET_DIR = ROOT / "docs" / "assets"
MANIFEST = ASSET_DIR / "readme-screenshots.json"
README = ROOT / "README.md"
WIDTH = 1440
HEIGHT = 1000
BUILD = "readme-screenshot-fixture-v1"
CAPTURES = {
    "admin-overview.png": {"route": "overview", "url": "?capture=overview", "required": ["Operator Briefing", "DASH Demo Farm", "Online Maps"]},
    "admin-ops.png": {"route": "ops", "url": "?capture=ops", "required": ["On-Call Alert Inbox", "Conflict-Aware Operations Calendar", "Connected Players"]},
}


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def index_html() -> str:
    tree = ast.parse(ADMIN_SOURCE.read_text(encoding="utf-8"), filename=str(ADMIN_SOURCE))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "INDEX" for target in node.targets):
            value = ast.literal_eval(node.value)
            if not isinstance(value, str):
                break
            return value.replace("__NONCE__", "readme-capture").replace("__ADMIN_PANEL_BUILD__", BUILD).replace(
                "__CHANGE_CONTRACT_PATHS__", "[]"
            )
    raise RuntimeError("could not extract INDEX HTML from admin/admin_panel.py")


def map_rows() -> list[dict]:
    names = [
        ("Survival_1", "Hagga Basin"), ("Caves_1", "Eastern Vermillius Gap"),
        ("Caves_2", "Western Vermillius Gap"), ("Testing_1", "Imperial Testing Station"),
        ("Tradepost_1", "Harko Village"), ("Dungeon_1", "The O'odham"),
        ("Dungeon_2", "Mysa Tarill"), ("Dungeon_3", "The Deep Fissure"),
        ("DD_1", "Deep Desert"), ("Social_1", "Arrakeen"),
        ("Special_1", "Sietch Tarl"), ("Special_2", "Sietch Abbir"),
    ]
    players = [3, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0]
    return [
        {
            "map": name, "label": label, "partition_id": index + 1, "ready": True,
            "alive": True, "active": True, "online": True, "blocked": False,
            "players": players[index], "game": f"777{index % 10}/udp", "igw": f"788{index % 10}/udp",
            "runtime": {
                "service": name.lower(), "state": "running", "uptime": f"{9 + index}h {12 + index}m",
                "uptimeSeconds": 33000 + index * 3700, "startedAt": "2026-07-17T08:00:00Z", "restartCount": 0,
            },
        }
        for index, (name, label) in enumerate(names)
    ]


MAPS = map_rows()
HEALTH = {
    "ok": True,
    "generatedAt": "2026-07-17T18:42:00Z",
    "summary": {
        "readyAlive": 12, "expectedPartitions": 12, "onlineMaps": 12, "totalMaps": 12,
        "flsPublication": "healthy", "peakPlayersToday": 7,
    },
    "playerCounts": {
        "online_controller_ids": 4, "active_farm_connected_players": 4,
        "raw_farm_connected_players": 4, "online_or_recently_disconnected": 5,
        "grace_period_entries": 1,
    },
    "playerPeak": {"peak": 7},
    "flsPublication": {"state": "healthy", "cached": False, "refreshing": False},
    "verdicts": [
        {"name": "Gateway readiness", "ok": True, "value": "ready"},
        {"name": "Director partitions", "ok": True, "value": "12 active"},
        {"name": "Game RabbitMQ", "ok": True, "value": "reachable"},
        {"name": "PostgreSQL", "ok": True, "value": "healthy"},
        {"name": "Map identity", "ok": True, "value": "no duplicates"},
        {"name": "FLS publication", "ok": True, "value": "healthy"},
    ],
    "mapStatus": MAPS,
    "farmState": [{"map": row["map"], "connected_players": row["players"], "ready": row["ready"]} for row in MAPS],
    "partitions": [{"partition_id": row["partition_id"], "map": row["map"], "status": "Online"} for row in MAPS],
}

ROSTER = {
    "counts": {"online": 4, "offline": 4, "total": 8},
    "online": [
        {"account_id": 1001, "character_name": "Demo Ornithopter", "online_status": "Online", "life_state": "Alive", "map": "Hagga Basin", "last_login_time": "2026-07-17 18:36 UTC"},
        {"account_id": 1002, "character_name": "Demo Maker", "online_status": "Online", "life_state": "Alive", "map": "Hagga Basin", "last_login_time": "2026-07-17 18:38 UTC"},
        {"account_id": 1003, "character_name": "Demo Mentat", "online_status": "Online", "life_state": "Alive", "map": "Hagga Basin", "last_login_time": "2026-07-17 18:40 UTC"},
        {"account_id": 1004, "character_name": "Demo Scout", "online_status": "Online", "life_state": "Alive", "map": "Deep Desert", "last_login_time": "2026-07-17 18:41 UTC"},
    ],
    "offline": [
        {"account_id": 1005, "character_name": "Demo Crafter", "online_status": "Offline", "life_state": "Alive", "map": "Hagga Basin", "last_login_time": "2026-07-16 22:10 UTC"},
        {"account_id": 1006, "character_name": "Demo Trader", "online_status": "Offline", "life_state": "Alive", "map": "Arrakeen", "last_login_time": "2026-07-16 20:05 UTC"},
        {"account_id": 1007, "character_name": "Demo Builder", "online_status": "Offline", "life_state": "Alive", "map": "Hagga Basin", "last_login_time": "2026-07-15 17:44 UTC"},
        {"account_id": 1008, "character_name": "Demo Surveyor", "online_status": "Offline", "life_state": "Alive", "map": "Deep Desert", "last_login_time": "2026-07-14 09:12 UTC"},
    ],
}

RESOURCES = {
    "generatedAt": "2026-07-17T18:42:00Z",
    "host": {
        "cpuCount": 24,
        "load": {"one": 3.18, "five": 3.44, "fifteen": 3.71},
        "memory": {"usedBytes": 47244640256, "totalBytes": 137438953472, "usedPercent": 34.4},
        "disk": {"usedBytes": 824633720832, "totalBytes": 2199023255552, "usedPercent": 37.5},
    },
    "docker": {
        "liveStats": True,
        "containers": [
            {"service": "survival", "name": "dash-survival", "status": "running", "cpuPercent": 28.4, "memory": "7.8 GiB", "memoryPercent": 5.8, "netRxBytes": 18958254, "netTxBytes": 14577111, "netIO": "18 MiB / 14 MiB", "blockIO": "1.2 GiB / 84 MiB", "pids": 98},
            {"service": "director", "name": "dash-director", "status": "running", "cpuPercent": 8.2, "memory": "2.1 GiB", "memoryPercent": 1.6, "netRxBytes": 9582544, "netTxBytes": 5577111, "netIO": "9 MiB / 5 MiB", "blockIO": "320 MiB / 22 MiB", "pids": 41},
            {"service": "gateway", "name": "dash-gateway", "status": "running", "cpuPercent": 5.7, "memory": "1.4 GiB", "memoryPercent": 1.1, "netRxBytes": 6582544, "netTxBytes": 4577111, "netIO": "6 MiB / 4 MiB", "blockIO": "180 MiB / 14 MiB", "pids": 35},
            {"service": "postgres", "name": "dash-postgres", "status": "running", "cpuPercent": 3.9, "memory": "1.8 GiB", "memoryPercent": 1.3, "netRxBytes": 4582544, "netTxBytes": 3577111, "netIO": "4 MiB / 3 MiB", "blockIO": "2.8 GiB / 680 MiB", "pids": 29},
            {"service": "admin-panel", "name": "dash-admin-panel", "status": "running", "cpuPercent": 1.2, "memory": "312 MiB", "memoryPercent": 0.2, "netRxBytes": 1582544, "netTxBytes": 2577111, "netIO": "1 MiB / 2 MiB", "blockIO": "42 MiB / 8 MiB", "pids": 17},
        ],
    },
}


def fixtures() -> dict[str, dict]:
    now = "2026-07-17T18:42:00Z"
    return {
        "/api/status": {
            "ok": True, "build": BUILD, "server": {"name": "DASH Demo Farm", "description": "sanitized screenshot fixture"},
            "adminTokenRequired": False, "adminTokenConfigured": False, "itemGrantsEnabled": True,
            "mutationsEnabled": False, "database": "demo_fixture", "changeContracts": {"required": True},
        },
        "/api/auth/federated/status": {"configured": False, "sessionActive": False},
        "/api/ops/health": HEALTH,
        "/api/characters/roster": ROSTER,
        "/api/ops/operations-briefing": {
            "ok": True, "enabled": True, "currentReady": True, "eventDebounceSeconds": 2,
            "changeMinimumIntervalSeconds": 15, "summary": {"retained": 24},
            "runtime": {"eventGenerations": 18},
            "latest": {
                "id": "demo-briefing", "state": "ready", "score": 96,
                "generatedAt": now, "receiptSha256": "demo-fixture-receipt",
                "summary": {"healthy": 13, "sources": 14, "critical": 0, "attention": 1, "informational": 0},
                "verification": {"ok": True, "ageSeconds": 41},
                "actions": [{"priority": 1, "title": "Review maintenance window", "source": "calendar", "detail": "A low-impact window is available tomorrow at 06:00.", "surface": "ops:calendar"}],
                "sources": [{"id": "calendar", "state": "attention"}], "changes": [],
            },
            "executionContract": {"automaticExecution": False, "recommendationsOnly": True},
        },
        "/api/players/hagga-basin": {
            "ok": True, "map": "HaggaBasin", "generatedAt": now,
            "calibration": {"minX": -457200, "maxX": 355600, "minY": -457200, "maxY": 355600, "invertY": True, "imageMinU": 0, "imageMaxU": 1, "imageMinV": 0, "imageMaxV": 1},
            "players": [
                {"account_id": 1001, "character_name": "Demo Ornithopter", "x": -112000, "y": 142000, "z": 9200, "label": "Hagga Basin", "last_login_time": now},
                {"account_id": 1002, "character_name": "Demo Maker", "x": 56000, "y": 32000, "z": 9000, "label": "Hagga Basin", "last_login_time": now},
                {"account_id": 1003, "character_name": "Demo Mentat", "x": 172000, "y": -84000, "z": 8700, "label": "Hagga Basin", "last_login_time": now},
            ],
            "pois": {"groups": {}, "markers": []}, "diagnostics": [],
        },
        "/api/ops/resources": RESOURCES,
        "/api/ops/optimization": {"Autoscaling": [{"name": "Profile", "value": "balanced-adaptive", "why": "Retain active and recently used partitions."}]},
        "/api/ops/announcement": {"jobs": [], "lastDelivery": None},
        "/api/ops/restart": {"jobs": [], "lastExecution": None, "targets": {"all": {"label": "Full world"}, "deep-desert": {"label": "Deep Desert"}, "survival": {"label": "Hagga Basin"}}},
        "/api/ops/maintenance-history": {"ok": True, "summary": {"retained": 12, "passed": 12, "failed": 0}, "receipts": []},
        "/api/ops/maintenance-planner": {
            "source": "measured-presence", "confidence": "high", "recommendation": {"localLabel": "Tomorrow 06:00", "expectedConcurrentPlayers": 0, "p95PeakPlayers": 1},
            "comparison": {"expectedImpactReduction": 0.86}, "evidence": {"observationBuckets": 8064},
            "recommendations": [{"startAt": "2026-07-18T12:00:00Z", "localLabel": "Tomorrow 06:00", "expectedConcurrentPlayers": 0, "p95PeakPlayers": 1}],
        },
        "/api/ops/calendar": {
            "ok": True, "horizonDays": 14, "fingerprint": "9a31c086d5c6demo", "conflicts": [], "coverageFindings": [], "errors": [],
            "summary": {"windows": 2, "current": 0, "criticalConflicts": 0, "warningConflicts": 0, "uncoveredDisruptive": 0},
            "next": {"title": "Verified daily backup", "startsAtIso": "Jul 18, 05:30 local"},
            "windows": [
                {"id": "backup", "startsAt": 1784374200, "endsAt": 1784376000, "impact": "none", "title": "Verified daily backup", "target": "all stores", "source": "backup", "recurring": True},
                {"id": "maintenance", "startsAt": 1784376000, "endsAt": 1784377800, "impact": "disruptive", "title": "Certified maintenance window", "target": "full world", "source": "maintenance", "recurring": False},
            ],
            "operationLock": {"path": "backups/admin-panel/operation.lock"}, "restartOperationRetrySeconds": 30,
            "executionContract": {"conflictsBlockExecution": True},
        },
        "/api/ops/alerts": {
            "ok": True, "enabled": True, "mutationEnabled": False, "alerts": [], "history": [],
            "summary": {"active": 0, "unacknowledged": 0, "critical": 0, "warning": 0, "pending": 0},
            "collector": {"ageSeconds": 12, "transitionsTotal": 4}, "delivery": {"signedWebhooksEnabled": True},
            "executionContract": {"acknowledgementDoesNotSilencePrometheus": True}, "runtime": {}, "retentionDays": 90,
        },
        "/api/ops/network": {
            "probes": [
                {"name": "Gateway", "ok": True, "target": "gateway:8080", "latencyMs": 2, "httpStatus": 200},
                {"name": "FLS", "ok": True, "target": "publication endpoint", "latencyMs": 34, "httpStatus": 200},
            ],
            "verdicts": [{"name": "Local services", "ok": True, "value": "reachable"}, {"name": "FLS", "ok": True, "value": "reachable"}],
        },
    }


class FixtureHandler(BaseHTTPRequestHandler):
    html = index_html()
    payloads = fixtures()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/overview", "/ops"}:
            body = self.html
            capture_tab = (urllib.parse.parse_qs(parsed.query).get("capture") or [""])[0]
            if capture_tab in {"overview", "ops"}:
                body = body.replace(
                    "const ADMIN_PANEL_BUILD =",
                    f"sessionStorage.setItem('duneAdminTab', '{capture_tab}');\n"
                    "sessionStorage.setItem('duneAdminAutoRefresh', 'off');\n"
                    "localStorage.setItem('duneAdminHaggaMapAutoRefresh', 'off');\n"
                    "const ADMIN_PANEL_BUILD =",
                    1,
                )
            self.send_bytes(body.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/static/hagga-basin.webp":
            self.send_bytes((ROOT / "admin" / "static" / "hagga-basin.webp").read_bytes(), "image/webp")
            return
        payload = self.payloads.get(parsed.path)
        if payload is not None:
            self.send_bytes(json.dumps(payload, separators=(",", ":")).encode(), "application/json; charset=utf-8")
            return
        self.send_error(404)

    def send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args) -> None:
        return


def chromium_binary(explicit: str | None) -> str:
    candidate = explicit or shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not candidate:
        raise RuntimeError("Chromium is required; set CHROMIUM_BIN or install chromium")
    return candidate


class DevTools:
    def __init__(self, connection):
        self.connection = connection
        self.request_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.request_id += 1
        request_id = self.request_id
        self.connection.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            response = json.loads(self.connection.recv())
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"Chromium {method} failed: {response['error']}")
            return response.get("result") or {}

    def evaluate(self, expression: str, *, await_promise: bool = False):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        ).get("result") or {}
        if result.get("subtype") == "error":
            raise RuntimeError(f"Chromium evaluation failed: {result.get('description')}")
        return result.get("value")


def unused_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def devtools_page(port: int, process: subprocess.Popen) -> dict:
    endpoint = f"http://127.0.0.1:{port}/json/list"
    deadline = time.monotonic() + 15
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chromium exited before DevTools became available: {process.returncode}")
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                pages = json.load(response)
            page = next((row for row in pages if row.get("type") == "page"), None)
            if page and page.get("webSocketDebuggerUrl"):
                return page
        except Exception as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"Chromium DevTools did not become available: {last_error}")


def wait_for_page(devtools: DevTools, required: list[str]) -> None:
    required_json = json.dumps(required)
    expression = """
(() => {
  const required = %s;
  const view = document.getElementById('view');
  const brand = document.querySelector('.brand');
  const text = document.body ? document.body.innerText : '';
  const folded = text.toLocaleLowerCase();
  const missing = required.filter(item => !folded.includes(item.toLocaleLowerCase()));
  const ready = document.readyState === 'complete' && view && view.getAttribute('aria-busy') === 'false' &&
    brand && brand.innerText.includes('SNAPE.TECH') && missing.length === 0;
  return {ready: Boolean(ready), readyState: document.readyState, ariaBusy: view?.getAttribute('aria-busy'),
    brand: brand?.innerText || '', missing};
})()
""" % required_json
    deadline = time.monotonic() + 20
    state = None
    while time.monotonic() < deadline:
        state = devtools.evaluate(expression)
        if isinstance(state, dict) and state.get("ready") is True:
            devtools.evaluate("document.fonts.ready.then(() => true)", await_promise=True)
            # Some views replace enough content during their initial render for
            # Chromium to restore a non-zero document position. README captures
            # must always include the shared product header and start at the
            # true top of the dashboard.
            devtools.evaluate(
                "window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0; true"
            )
            devtools.evaluate(
                "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
                await_promise=True,
            )
            return
        time.sleep(0.05)
    raise RuntimeError(f"page did not reach its rendered ready state: {state}")


def capture(binary: str, base_url: str) -> None:
    try:
        import websocket
    except ModuleNotFoundError as exc:
        raise RuntimeError("headless capture requires the Python websocket-client package") from exc
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name, config in CAPTURES.items():
        # Use a fresh renderer for each page. Reusing one headless surface can
        # preserve an unchanged sticky-header layer without repainting it into
        # the next Page.captureScreenshot result.
        with tempfile.TemporaryDirectory(prefix="dash-readme-chromium-") as temp:
            profile = pathlib.Path(temp) / "profile"
            port = unused_port()
            process = subprocess.Popen(
                [
                    binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                    "--hide-scrollbars", "--force-device-scale-factor=1", "--remote-allow-origins=*",
                    f"--remote-debugging-port={port}", f"--window-size={WIDTH},{HEIGHT}",
                    f"--user-data-dir={profile}", "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            connection = None
            try:
                page = devtools_page(port, process)
                connection = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
                devtools = DevTools(connection)
                devtools.call("Page.enable")
                devtools.call("Runtime.enable")
                devtools.call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False},
                )
                url = f"{base_url}/{config.get('url', config['route'])}"
                devtools.call("Page.navigate", {"url": url})
                wait_for_page(devtools, config["required"])
                screenshot = devtools.call(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                )
                data = base64.b64decode(screenshot["data"], validate=True)
                output = ASSET_DIR / name
                output.write_bytes(data)
                if png_dimensions(output) != (WIDTH, HEIGHT):
                    raise RuntimeError(f"unexpected screenshot dimensions: {output}")
            finally:
                if connection is not None:
                    connection.close()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def png_dimensions(path: pathlib.Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if len(data) != 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RuntimeError(f"not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


def manifest_document() -> dict:
    return {
        "schemaVersion": 1,
        "generatedFrom": str(ADMIN_SOURCE.relative_to(ROOT)),
        "sourceSha256": sha256(ADMIN_SOURCE),
        "captureScriptSha256": sha256(CAPTURE_SCRIPT),
        "viewport": {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1},
        "dataPolicy": "sanitized deterministic fixtures; no live server, player, credential, log, or host data",
        "assets": {
            name: {"route": config["route"], "sha256": sha256(ASSET_DIR / name)}
            for name, config in CAPTURES.items()
        },
    }


def write_manifest() -> None:
    MANIFEST.write_text(json.dumps(manifest_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = manifest_document()
    if document != expected:
        raise RuntimeError("README screenshots are stale; run make readme-screenshots")
    readme = README.read_text(encoding="utf-8")
    for name in CAPTURES:
        path = ASSET_DIR / name
        if png_dimensions(path) != (WIDTH, HEIGHT):
            raise RuntimeError(f"{name} must be {WIDTH}x{HEIGHT}")
        if f"docs/assets/{name}" not in readme:
            raise RuntimeError(f"README does not reference docs/assets/{name}")
    print(f"README screenshots are current: {len(CAPTURES)} assets at {WIDTH}x{HEIGHT}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify screenshot hashes and admin UI source binding")
    parser.add_argument("--chromium", help="Chromium-compatible executable")
    args = parser.parse_args()
    if args.check:
        check()
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        capture(chromium_binary(args.chromium), f"http://127.0.0.1:{server.server_port}")
        write_manifest()
        check()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
