"""A fake Docker Engine on a unix socket, recording every request."""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path


class FakeEngine:
    def __init__(self, socket_path: Path, containers=None):
        self.socket_path = str(socket_path)
        self.containers = containers or []
        self.calls = []
        self.status_override = {}
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def address_string(self):
                return "unix"

            def _reply(self, status, body=b""):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                engine.calls.append(("GET", self.path))
                status = engine.status_override.get(("GET", self.path), 200)
                self._reply(status, json.dumps(engine.containers).encode())

            def do_POST(self):
                engine.calls.append(("POST", self.path))
                parts = self.path.split("?")[0].split("/")
                cid, action = parts[2], parts[3]
                status = engine.status_override.get(("POST", self.path))
                if status is None:
                    status = engine._apply(cid, action)
                self._reply(status)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(self.socket_path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _apply(self, cid, action):
        for item in self.containers:
            if item["Id"] == cid:
                if action == "pause":
                    if item["State"] != "running":
                        return 409
                    item["State"] = "paused"
                elif action == "unpause":
                    if item["State"] != "paused":
                        return 409
                    item["State"] = "running"
                elif action == "stop":
                    item["State"] = "exited"
                return 204
        return 404

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def container(cid, name, image="img", state="running", labels=None, created=0):
    return {"Id": cid, "Names": [f"/{name}"], "Image": image, "State": state,
            "Labels": labels or {}, "Created": created}
