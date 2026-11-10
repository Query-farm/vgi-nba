"""Hermetic end-to-end coverage through Haybarn and the VGI transport.

A local HTTP server answers every Stats API and CDN request from the recorded
fixtures, and Haybarn attaches the worker over stdio and queries every current
table in all three league schemas.
"""

from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from conftest import FIXTURES, fixture_for

TABLES = [
    ("nba", "current_standings"),
    ("nba", "current_scoreboard"),
    ("nba", "current_schedule"),
    ("nba", "current_teams"),
    ("nba", "current_players"),
    ("nba", "current_player_stats"),
    ("nba", "current_team_stats"),
    ("nba", "franchise_history"),
    ("wnba", "current_standings"),
    ("wnba", "current_scoreboard"),
    ("wnba", "current_schedule"),
    ("wnba", "current_teams"),
    ("wnba", "current_players"),
    ("wnba", "current_player_stats"),
    ("wnba", "current_team_stats"),
    ("wnba", "franchise_history"),
    ("gleague", "current_standings"),
    ("gleague", "current_scoreboard"),
    ("gleague", "current_teams"),
    ("gleague", "current_players"),
    ("gleague", "current_player_stats"),
    ("gleague", "current_team_stats"),
    ("gleague", "franchise_history"),
]


class _FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        name = fixture_for(parsed.path, query)
        if name is None:
            self.send_error(404)
            return
        body = (FIXTURES / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_haybarn_attaches_and_queries_every_current_table() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = Path(__file__).resolve().parent.parent
    location = f"uv run --project {project} vgi-nba"
    counts = ", ".join(
        f"(SELECT count(*) FROM nba.{schema}.{table}) AS {schema}_{table}" for schema, table in TABLES
    )
    sql = (
        f"INSTALL vgi FROM community; LOAD vgi; ATTACH 'nba' (TYPE vgi, LOCATION '{location}'); "
        f"SELECT {counts}; "
        "SELECT sum(points) AS box_points FROM nba.nba.box_score('0022600201'); "
        "SELECT count(*) AS plays FROM nba.nba.play_by_play('0022600201');"
    )
    base = f"http://127.0.0.1:{server.server_port}"
    env = {
        **os.environ,
        "NBA_STATS_BASE_URL": f"{base}/stats",
        "NBA_CDN_BASE_URL": f"{base}/nba-cdn",
        "WNBA_CDN_BASE_URL": f"{base}/wnba-cdn",
        "NBA_REQUEST_INTERVAL": "0",
    }
    try:
        result = subprocess.run(
            ["uvx", "haybarn-cli", ":memory:", "-csv", "-no-agent", "-c", sql],
            cwd=project,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    lines = result.stdout.splitlines()
    header = lines.index(",".join(f"{schema}_{table}" for schema, table in TABLES))
    counts_by_table = dict(zip(lines[header].split(","), map(int, lines[header + 1].split(",")), strict=True))
    assert all(count > 0 for count in counts_by_table.values()), counts_by_table
    assert counts_by_table["nba_current_teams"] == 3
    assert counts_by_table["wnba_current_players"] == 3
    box_points = int(lines[lines.index("box_points") + 1])
    plays = int(lines[lines.index("plays") + 1])
    # The trimmed fixtures keep 73 player points and 7 play-by-play actions.
    assert box_points == 73
    assert plays == 7
