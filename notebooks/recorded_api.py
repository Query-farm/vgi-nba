"""Serve one unchanged, recorded NBA Stats response through its supported URL override."""

from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

ENVIRONMENTS = ("NBA_STATS_BASE_URL", "NBA_CDN_BASE_URL", "WNBA_CDN_BASE_URL", "NBA_REQUEST_INTERVAL")


class RecordedAPI:
    """Replay only the exact recorded team-season request on loopback."""

    def __init__(self):
        """Verify fixture bytes and start the exact-request loopback server."""
        manifest = json.loads(Path("fixtures/provenance.json").read_text())
        item = manifest["files"][0]
        body = (Path("fixtures") / item["file"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == item["sha256"], "Fixture hash mismatch"
        expected = {
            "Counter": ["0"],
            "Direction": ["ASC"],
            "LeagueID": ["00"],
            "PlayerOrTeam": ["T"],
            "Season": ["2025-26"],
            "SeasonType": ["Regular Season"],
            "Sorter": ["DATE"],
        }
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                route = urlparse(self.path)
                if route.path != "/stats/leaguegamelog" or parse_qs(route.query) != expected:
                    self.send_error(404, "Outside recorded fixture coverage")
                    return
                owner.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=60")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        root = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["NBA_STATS_BASE_URL"] = root + "/stats"
        os.environ["NBA_CDN_BASE_URL"] = root + "/unsupported"
        os.environ["WNBA_CDN_BASE_URL"] = root + "/unsupported"
        os.environ["NBA_REQUEST_INTERVAL"] = "0"

    def close(self):
        """Stop the server and remove its endpoint overrides."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for key in ENVIRONMENTS:
            os.environ.pop(key, None)


def configure(mode):
    """Clear inherited endpoint overrides; never substitute replay for a live failure."""
    if mode not in {"replay", "live"}:
        raise ValueError("DATA_MODE must be replay or live")
    for key in ENVIRONMENTS:
        os.environ.pop(key, None)
    return RecordedAPI() if mode == "replay" else None
