#!/usr/bin/env python3
"""First-party DASH Discord slash-command bot with no third-party packages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import secrets
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from http_url import require_http_url


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "backups" / "discord-bot"
STATE_FILE = STATE_DIR / "state.json"
API_BASE = "https://discord.com/api/v10"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
COMMAND_GROUPS = {
    "core": {
        "about": (None, None, "Bot and security model"),
        "ping": (None, None, "Bot liveness"),
        "help": (None, None, "List available commands"),
    },
    "server": {
        "health": ("POST", "/api/integrations/discord/status", "Server health"),
        "status": ("POST", "/api/integrations/discord/status", "Server status"),
        "summary": ("POST", "/api/integrations/discord/status", "Server summary"),
        "readiness": ("POST", "/api/integrations/discord/readiness", "Travel and service readiness"),
        "services": ("POST", "/api/integrations/discord/services", "Compose service states"),
    },
    "data": {
        "population": ("POST", "/api/integrations/discord/population", "Aggregate player population"),
        "backups": ("GET", "/api/integrations/discord/backups/list", "Recent backup metadata"),
        "maps": ("POST", "/api/integrations/discord/servers", "World map assignments"),
    },
    "ops": {
        "activity": ("POST", "/api/integrations/discord/ops", "Recent player activity"),
        "combat": ("POST", "/api/integrations/discord/ops", "Recent combat/death summary"),
        "resources": ("POST", "/api/integrations/discord/ops", "Resource-field summary"),
        "economy": ("POST", "/api/integrations/discord/ops", "Aggregate economy summary"),
        "inventory": ("POST", "/api/integrations/discord/ops", "Inventory integrity summary"),
        "location": ("POST", "/api/integrations/discord/ops", "Online map distribution"),
        "soc": ("POST", "/api/integrations/discord/ops", "Server orchestration connectivity"),
        "prometheus": ("POST", "/api/integrations/discord/ops", "Private metrics status"),
        "dashboard": ("POST", "/api/integrations/discord/ops", "Private dashboard status"),
    },
    "admin": {
        "doctor": ("POST", "/api/integrations/discord/ops", "Read-only diagnostic summary"),
        "cooldowns": ("POST", "/api/integrations/discord/ops", "Scheduled job/cooldown summary"),
        "latency": (None, None, "Bot-to-adapter latency guidance"),
        "events": ("POST", "/api/integrations/discord/events", "Scheduled event metadata"),
        "broadcast": (None, None, "Broadcast write status"),
    },
    "infra": {
        "version": ("GET", "/api/integrations/discord/version", "DASH build version"),
        "servers": ("POST", "/api/integrations/discord/servers", "World partition assignments"),
        "ports": ("POST", "/api/integrations/discord/ports", "Bound service ports"),
        "database": ("POST", "/api/integrations/discord/db", "Database connectivity summary"),
    },
    "shop": {
        "howtolink": ("POST", "/api/integrations/discord/community", "How to link your Dune account"),
        "link": ("POST", "/api/integrations/discord/community", "Redeem a one-time account link code"),
        "balance": ("POST", "/api/integrations/discord/community", "Your community-credit balance"),
        "catalog": ("POST", "/api/integrations/discord/community", "Available shop items and kits"),
        "buy": ("POST", "/api/integrations/discord/community", "Buy a catalog item"),
        "kits": ("POST", "/api/integrations/discord/community", "Available multi-item kits"),
        "track": ("POST", "/api/integrations/discord/community", "Your current reward-track progress"),
        "claim": ("POST", "/api/integrations/discord/community", "Claim an unlocked reward-track level"),
    },
}
COMMANDS = {name: definition for commands in COMMAND_GROUPS.values() for name, definition in commands.items()}
COMMAND_OPTIONS = {
    "link": [{"type": 3, "name": "code", "description": "One-time code created in DASH", "required": True, "min_length": 6, "max_length": 32}],
    "buy": [
        {"type": 3, "name": "offer", "description": "Catalog offer ID", "required": True, "min_length": 1, "max_length": 64},
        {"type": 4, "name": "quantity", "description": "Quantity", "required": False, "min_value": 1, "max_value": 100},
    ],
    "claim": [
        {"type": 3, "name": "track", "description": "Reward track ID", "required": True, "min_length": 1, "max_length": 64},
        {"type": 4, "name": "level", "description": "Unlocked level", "required": True, "min_value": 1, "max_value": 10000},
    ],
}


def read_env_file(path):
    values = {}
    try:
        for raw in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def read_secret(values, name):
    file_name = values.get(f"{name}_FILE", "").strip()
    if file_name:
        try:
            return pathlib.Path(file_name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return values.get(name, "").strip()


def load_config(env_file=None):
    env_file = pathlib.Path(env_file or os.environ.get("DUNE_DISCORD_BOT_ENV_FILE", ROOT / ".env"))
    values = read_env_file(env_file)
    values.update({key: value for key, value in os.environ.items() if key.startswith(("DUNE_DISCORD_", "DUNE_BOT_", "DISCORD_"))})
    return {
        "envFile": str(env_file),
        "token": read_secret(values, "DUNE_DISCORD_BOT_TOKEN"),
        "applicationId": values.get("DUNE_DISCORD_APPLICATION_ID", "").strip(),
        "guildId": values.get("DUNE_DISCORD_GUILD_ID", "").strip(),
        "channelIds": {item.strip() for item in values.get("DUNE_DISCORD_CHANNEL_IDS", "").split(",") if item.strip()},
        "adapterToken": read_secret(values, "DUNE_BOT_API_TOKEN"),
        "adapterUrl": values.get("DUNE_DISCORD_ADAPTER_URL", "http://127.0.0.1:18080").rstrip("/"),
        "adapterHost": values.get("DUNE_DISCORD_ADAPTER_HOST", "admin-panel:8080").strip(),
        "registerCommands": values.get("DUNE_DISCORD_REGISTER_COMMANDS", "true").lower() in ("1", "true", "yes", "on"),
        "requestTimeout": max(1.0, min(float(values.get("DUNE_DISCORD_REQUEST_TIMEOUT_SECONDS", "2.5")), 10.0)),
    }


def public_config(config):
    return {
        "configured": bool(config["token"] and config["applicationId"] and config["guildId"] and config["adapterToken"]),
        "tokenConfigured": bool(config["token"]),
        "applicationIdConfigured": bool(config["applicationId"]),
        "guildIdConfigured": bool(config["guildId"]),
        "adapterTokenConfigured": bool(config["adapterToken"]),
        "adapterUrl": config["adapterUrl"],
        "adapterHost": config["adapterHost"],
        "channelRestrictionCount": len(config["channelIds"]),
        "commandCount": len(COMMANDS),
        "messageContentIntent": False,
    }


def write_state(**updates):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        state.update(updates)
        state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(STATE_FILE)
        STATE_FILE.chmod(0o600)
    except OSError:
        pass


def encode_client_frame(payload, opcode=1):
    payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    first = 0x80 | opcode
    length = len(payload)
    mask = secrets.token_bytes(4)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + masked


def decode_server_frame(data):
    if len(data) < 2:
        return None
    first, second = data[0], data[1]
    length = second & 0x7F
    offset = 2
    if second & 0x80:
        raise ValueError("server WebSocket frames must not be masked")
    if length == 126:
        if len(data) < 4:
            return None
        length, offset = struct.unpack("!H", data[2:4])[0], 4
    elif length == 127:
        if len(data) < 10:
            return None
        length, offset = struct.unpack("!Q", data[2:10])[0], 10
    if len(data) < offset + length:
        return None
    return {"fin": bool(first & 0x80), "opcode": first & 0x0F, "payload": data[offset:offset + length], "consumed": offset + length}


class WebSocket:
    def __init__(self, url, timeout=30):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "wss" or not parsed.hostname:
            raise ValueError("Discord Gateway URL must use wss")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        self.buffer = b""
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            "User-Agent: DASH-Discord-Bot/1\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = self._until(b"\r\n\r\n", 65536)
        headers, self.buffer = response.split(b"\r\n\r\n", 1)
        lines = headers.decode("iso-8859-1").split("\r\n")
        response_headers = {key.lower(): value.strip() for key, value in (line.split(":", 1) for line in lines[1:] if ":" in line)}
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii"), usedforsecurity=False).digest()).decode("ascii")
        if " 101 " not in f" {lines[0]} " or response_headers.get("sec-websocket-accept") != expected:
            self.close()
            raise ConnectionError("Discord Gateway WebSocket upgrade failed")

    def _until(self, marker, maximum):
        data = self.buffer
        while marker not in data:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket closed during handshake")
            data += chunk
            if len(data) > maximum:
                raise ValueError("WebSocket handshake exceeds limit")
        return data

    def send_json(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(body) > 4096:
            raise ValueError("Discord Gateway payload exceeds 4096 bytes")
        self.sock.sendall(encode_client_frame(body))

    def recv_json(self, timeout=None):
        self.sock.settimeout(timeout)
        fragments = []
        while True:
            frame = decode_server_frame(self.buffer)
            while frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("Discord Gateway closed")
                self.buffer += chunk
                if len(self.buffer) > 8 * 1024 * 1024:
                    raise ValueError("Discord Gateway buffer exceeds limit")
                frame = decode_server_frame(self.buffer)
            self.buffer = self.buffer[frame["consumed"]:]
            opcode = frame["opcode"]
            if opcode == 8:
                code = struct.unpack("!H", frame["payload"][:2])[0] if len(frame["payload"]) >= 2 else 1000
                raise ConnectionError(f"Discord Gateway closed with code {code}")
            if opcode == 9:
                self.sock.sendall(encode_client_frame(frame["payload"], opcode=10))
                continue
            if opcode == 10:
                continue
            if opcode in (1, 0):
                fragments.append(frame["payload"])
                if frame["fin"]:
                    return json.loads(b"".join(fragments).decode("utf-8"))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def http_json(url, method="GET", token=None, data=None, timeout=5, headers=None):
    body = None if data is None else json.dumps(data, separators=(",", ":")).encode("utf-8")
    request_headers = {"User-Agent": "DASH-Discord-Bot/1", "Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bot {token}"
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        require_http_url(request.full_url)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read(8192)
        detail = raw.decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None


def command_definition():
    return {
        "name": "dune",
        "type": 1,
        "description": "Dune: Awakening server status and operations",
        "dm_permission": False,
        "options": [{
            "type": 2,
            "name": group,
            "description": f"{group.title()} commands",
            "options": [
                dict({"type": 1, "name": name, "description": description[:100]}, **({"options": COMMAND_OPTIONS[name]} if name in COMMAND_OPTIONS else {}))
                for name, (_, _, description) in commands.items()
            ],
        } for group, commands in COMMAND_GROUPS.items()],
    }


def register_commands(config):
    scope = f"/guilds/{config['guildId']}" if config["guildId"] else ""
    url = f"{API_BASE}/applications/{config['applicationId']}{scope}/commands"
    return http_json(url, method="PUT", token=config["token"], data=[command_definition()], timeout=10)


def actor_from_interaction(interaction):
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return {
        "guildId": str(interaction.get("guild_id") or ""),
        "channelId": str(interaction.get("channel_id") or ""),
        "userId": str(user.get("id") or ""),
        "username": str(user.get("global_name") or user.get("username") or "unknown")[:256],
        "roleIds": [str(item) for item in (member.get("roles") or [])[:100]],
    }


def interaction_command(interaction):
    data = interaction.get("data") or {}
    if data.get("name") != "dune":
        return None
    options = data.get("options") or []
    if not options:
        return "help"
    selected = options[0]
    nested = selected.get("options") or []
    return str(nested[0].get("name")) if nested else str(selected.get("name"))


def interaction_arguments(interaction):
    data = interaction.get("data") or {}
    options = data.get("options") or []
    if not options:
        return {}
    nested = options[0].get("options") or []
    if not nested:
        return {}
    arguments = nested[0].get("options") or []
    return {str(item.get("name")): item.get("value") for item in arguments if item.get("name")}


def adapter_request(config, command, actor, arguments=None):
    method, path, _ = COMMANDS[command]
    if method is None:
        return {"local": command, "commands": {group: list(commands) for group, commands in COMMAND_GROUPS.items()}}
    headers = {
        "Authorization": f"Bearer {config['adapterToken']}",
        "Host": config["adapterHost"],
    }
    data = {"actor": actor, "arguments": arguments or {}} if method == "POST" else None
    if path.endswith("/ops"):
        data["domain"] = command
    if path.endswith("/community"):
        data["action"] = command
    return http_json(config["adapterUrl"] + path, method=method, data=data, timeout=config["requestTimeout"], headers=headers)


def compact_result(command, result):
    payload = result.get("result", result)
    if command == "population":
        return f"Online players: **{payload.get('onlinePlayers', '?')}** · known players: {payload.get('totalPlayers', '?')}"
    if command == "readiness":
        return f"Ready: **{payload.get('ready', False)}** · state: **{payload.get('overall', 'unknown')}**\n```json\n{json.dumps(payload.get('summary', {}), sort_keys=True)[:1400]}\n```"
    if command == "version":
        return f"DASH version: `{payload.get('version', 'unknown')}`"
    if command == "help":
        return "\n".join(f"`/dune {group} {name}` — {description}" for group, commands in COMMAND_GROUPS.items() for name, (_, _, description) in commands.items())[:2000]
    if command == "about":
        return "DASH first-party read-only bot. Guild/channel restrictions and adapter role mapping apply; it has no database, Docker, shell, or admin-owner credential."
    if command == "ping":
        return "DASH bot is connected and receiving Gateway interactions."
    if command == "latency":
        return "The bot uses a bounded local adapter request (default 2.5 seconds); inspect service state for the most recent command timestamp."
    if command == "broadcast":
        return "Discord-to-game broadcast is disabled. Use the independently authenticated DASH announcement controls."
    if command in ("howtolink", "link", "balance", "catalog", "buy", "kits", "track", "claim"):
        if payload.get("message"):
            return str(payload["message"])[:2000]
        text = json.dumps(payload, sort_keys=True, indent=2, default=str)
        return f"**DASH community {command}**\n```json\n{text[:1750]}\n```"
    text = json.dumps(payload, sort_keys=True, indent=2, default=str)
    if len(text) > 1750:
        text = text[:1750] + "\n...[truncated]"
    return f"**DASH {command}**\n```json\n{text}\n```"


def interaction_response(interaction, content, ephemeral=True):
    data = {"content": str(content)[:2000], "allowed_mentions": {"parse": []}}
    if ephemeral:
        data["flags"] = 64
    url = f"{API_BASE}/interactions/{interaction['id']}/{interaction['token']}/callback"
    return http_json(url, method="POST", data={"type": 4, "data": data}, timeout=3)


class Bot:
    def __init__(self, config):
        self.config = config
        self.sequence = None
        self.session_id = None
        self.resume_url = None

    def handle_interaction(self, interaction):
        actor = actor_from_interaction(interaction)
        actor["requestId"] = str(interaction.get("id") or "")[:128]
        command = interaction_command(interaction)
        if not command or command not in COMMANDS:
            return interaction_response(interaction, "Unknown DASH command.")
        if str(interaction.get("guild_id") or "") != self.config["guildId"]:
            return interaction_response(interaction, "This DASH bot is not configured for this server.")
        if self.config["channelIds"] and actor["channelId"] not in self.config["channelIds"]:
            return interaction_response(interaction, "DASH commands are not enabled in this channel.")
        try:
            result = adapter_request(self.config, command, actor, interaction_arguments(interaction))
            content = compact_result(command, result)
            write_state(lastCommand=command, lastCommandAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), lastError=None)
        except Exception as exc:
            content = f"DASH request failed: {str(exc)[:500]}"
            write_state(lastError=str(exc)[:500])
        return interaction_response(interaction, content)

    def connect_once(self):
        gateway = http_json(f"{API_BASE}/gateway/bot", token=self.config["token"], timeout=10)
        url = (self.resume_url or gateway["url"]) + "?v=10&encoding=json"
        ws = WebSocket(url)
        try:
            hello = ws.recv_json(timeout=15)
            if hello.get("op") != 10:
                raise ConnectionError("Discord Gateway did not send HELLO")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            if self.session_id and self.sequence is not None:
                ws.send_json({"op": 6, "d": {"token": self.config["token"], "session_id": self.session_id, "seq": self.sequence}})
            else:
                ws.send_json({"op": 2, "d": {"token": self.config["token"], "intents": 1, "properties": {"os": "linux", "browser": "dash", "device": "dash"}}})
            next_heartbeat = time.monotonic() + interval
            heartbeat_acked = True
            while True:
                timeout = max(0.1, next_heartbeat - time.monotonic())
                try:
                    event = ws.recv_json(timeout=timeout)
                except socket.timeout:
                    if not heartbeat_acked:
                        raise ConnectionError("Discord Gateway heartbeat was not acknowledged")
                    ws.send_json({"op": 1, "d": self.sequence})
                    heartbeat_acked = False
                    next_heartbeat = time.monotonic() + interval
                    continue
                if event.get("s") is not None:
                    self.sequence = event["s"]
                if event.get("op") == 1:
                    ws.send_json({"op": 1, "d": self.sequence})
                    heartbeat_acked = False
                    next_heartbeat = time.monotonic() + interval
                elif event.get("op") == 11:
                    heartbeat_acked = True
                elif event.get("op") == 7:
                    return
                elif event.get("op") == 9:
                    self.session_id = None
                    self.sequence = None
                    self.resume_url = None
                    time.sleep(1)
                    return
                elif event.get("op") == 0 and event.get("t") == "READY":
                    self.session_id = event["d"].get("session_id")
                    self.resume_url = event["d"].get("resume_gateway_url")
                    write_state(status="connected", sessionConfigured=True, lastError=None)
                elif event.get("op") == 0 and event.get("t") == "INTERACTION_CREATE":
                    self.handle_interaction(event["d"])
        finally:
            ws.close()

    def run(self):
        delay = 1
        while True:
            try:
                self.connect_once()
                delay = 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                write_state(status="reconnecting", lastError=str(exc)[:500])
                time.sleep(delay)
                delay = min(delay * 2, 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--wait-for-config", action="store_true")
    args = parser.parse_args()
    while True:
        config = load_config(args.env_file)
        public = public_config(config)
        if args.check:
            print(json.dumps(public, sort_keys=True))
            return 0 if public["configured"] else 3
        if public["configured"]:
            break
        write_state(status="waiting-for-credentials", config=public)
        if not args.wait_for_config:
            print(json.dumps(public, sort_keys=True))
            return 3
        time.sleep(30)
    if args.register or config["registerCommands"]:
        commands = register_commands(config)
        write_state(commandsRegistered=len(commands), status="registered")
        if args.register:
            print(json.dumps({"registered": len(commands)}, sort_keys=True))
            return 0
    Bot(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
