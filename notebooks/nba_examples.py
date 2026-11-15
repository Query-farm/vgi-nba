"""Serve explicit recorded or fictional responses to the real released NBA worker."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ENVIRONMENTS = ("NBA_STATS_BASE_URL", "NBA_CDN_BASE_URL", "WNBA_CDN_BASE_URL", "NBA_REQUEST_INTERVAL")


class NBAExample:
    """Exact-route local API data, with bounded explicit live mode for the real story."""

    def __init__(self, mode, scenario):
        if scenario not in {"game-log", "games", "rates", "career"}:
            raise ValueError("Unknown example scenario")
        if mode not in ({"replay", "live"} if scenario == "game-log" else {"synthetic"}):
            raise ValueError("DATA_MODE must be replay or live for game-log; expansions use synthetic only")
        self.previous = {key: os.environ.pop(key, None) for key in ENVIRONMENTS}
        self.mode = mode
        self.scenario = scenario
        self.requests = []
        self.unexpected = []
        self.server = self.thread = None
        if mode == "live":
            return
        folder = Path(__file__).resolve().parent / "fixtures"
        if mode == "replay":
            manifest = json.loads((folder / "provenance.json").read_text())
            item = manifest["files"][0]
            body = (folder / item["file"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ValueError("Recorded response digest mismatch")
            self.routes = [
                {
                    "id": "recorded-team-game-log",
                    "path": "/stats/leaguegamelog",
                    "parameters": {
                        "Counter": "0",
                        "Direction": "ASC",
                        "LeagueID": "00",
                        "PlayerOrTeam": "T",
                        "Season": "2025-26",
                        "SeasonType": "Regular Season",
                        "Sorter": "DATE",
                    },
                    "bytes": body,
                }
            ]
        else:
            bundle = json.loads((folder / "synthetic-responses.json").read_text())
            self.routes = [r for r in bundle["routes"] if r["scenario"] == scenario]
            now = datetime.now(ZoneInfo("America/New_York"))
            for route in self.routes:
                if route["parameters"].get("Season") == "CURRENT":
                    league = route["parameters"]["LeagueID"]
                    start = 5 if league == "10" else 10
                    year = now.year - int(now.month < start)
                    route["parameters"]["Season"] = str(year) if league == "10" else f"{year}-{(year + 1) % 100:02}"
                route["bytes"] = json.dumps(route["body"], ensure_ascii=False, separators=(",", ":")).encode()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                raw = parse_qs(parsed.query, keep_blank_values=True)
                params = {k: v[0] for k, v in raw.items()}
                found = next(
                    (
                        r
                        for r in owner.routes
                        if r["path"] == parsed.path
                        and r["parameters"] == params
                        and all(len(v) == 1 for v in raw.values())
                    ),
                    None,
                )
                if found:
                    owner.requests.append(found["id"])
                    body = found["bytes"]
                else:
                    owner.unexpected.append({"path": parsed.path, "parameters": params})
                    body = b'{"error":"outside explicit fixture scope"}'
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "max-age=600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        root = f"http://127.0.0.1:{self.server.server_port}"
        os.environ.update(
            NBA_STATS_BASE_URL=root + "/stats",
            NBA_CDN_BASE_URL=root + "/unsupported",
            WNBA_CDN_BASE_URL=root + "/unsupported",
            NBA_REQUEST_INTERVAL="0",
        )

    def audit(self):
        if self.mode == "live":
            return "Explicit live public request; no replay fallback."
        assert not self.unexpected, self.unexpected
        expected = sorted(r["id"] for r in self.routes)
        assert sorted(self.requests) == expected, {"expected": expected, "actual": sorted(self.requests)}
        return f"Exactly {len(expected)} approved API requests; each once, all parameters matched."

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
