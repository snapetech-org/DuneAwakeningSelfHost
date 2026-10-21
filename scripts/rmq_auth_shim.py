#!/usr/bin/env python3
import os
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from secret_env import read_secret_env


WORLD_UNIQUE_NAME = os.environ["WORLD_UNIQUE_NAME"]
UPSTREAM = os.environ.get("TEXT_ROUTER_AUTH_BASE", "http://text-router:8080")
MANAGEMENT_USER = os.environ.get("DUNE_RMQ_MANAGEMENT_USER", "")
MANAGEMENT_PASSWORD = read_secret_env("DUNE_RMQ_MANAGEMENT_PASSWORD")
SERVICE_USER_RE = re.compile(rf"^(sg|bgd|tr)\.{re.escape(WORLD_UNIQUE_NAME)}\.[^.]+(?:\.(game|admin))?$")
PLAYER_USER_RE = re.compile(r"^[0-9A-Fa-f]{16}$")


def parse_form(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def post_upstream(path: str, body: bytes, content_type: str | None) -> bytes:
    req = urllib.request.Request(f"{UPSTREAM}{path}", data=body, method="POST")
    req.add_header("Content-Type", content_type or "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "dune-rmq-auth-shim"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        form = parse_form(body)
        username = form.get("username", "")

        if self.path in {"/v0/auth/user", "/v0/auth/vhost", "/v0/auth/resource", "/v0/auth/topic"}:
            if MANAGEMENT_USER and username == MANAGEMENT_USER:
                if self.path == "/v0/auth/user" and form.get("password", "") == MANAGEMENT_PASSWORD:
                    self.respond(b"allow administrator")
                    return
                if self.path != "/v0/auth/user":
                    self.respond(b"allow")
                    return
                self.respond(b"deny")
                return
            if SERVICE_USER_RE.match(username):
                if self.path == "/v0/auth/user":
                    self.respond(b"allow administrator")
                    return
                self.respond(b"allow")
                return
            if PLAYER_USER_RE.match(username):
                self.respond(b"allow")
                return

        try:
            self.respond(post_upstream(self.path, body, self.headers.get("Content-Type")))
        except Exception:
            self.respond(b"deny")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AuthServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = int(os.environ.get("RMQ_AUTH_SHIM_BACKLOG", "128"))


if __name__ == "__main__":
    AuthServer(("0.0.0.0", 8080), Handler).serve_forever()
