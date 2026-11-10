"""Shared fixtures: recorded upstream responses and a router that serves them."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from vgi_nba import api

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    """Load one recorded upstream response."""
    return json.loads((FIXTURES / name).read_text())


def fixture_for(path: str, query: dict[str, str]) -> str | None:
    """Map a Stats API or CDN request onto the recorded response that answers it."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if name.startswith("todaysScoreboard_"):
        return "todaysScoreboard_00.json"
    if name == "scheduleLeagueV2.json":
        return name
    if name.startswith("leaguedash") and query.get("MeasureType") == "Advanced":
        return f"{name}_advanced.json"
    if name == "leaguegamelog" and query.get("PlayerOrTeam") == "P":
        return "leaguegamelog_player.json"
    if name == "leaguestandingsv3" and query.get("LeagueID") == "10":
        return "leaguestandingsv3_wnba.json"
    candidate = f"{name}.json"
    return candidate if (FIXTURES / candidate).exists() else None


def fixture_response(request: httpx.Request) -> httpx.Response:
    """Answer a mocked request from the recorded fixtures (404 when none matches)."""
    query = {key: values[0] for key, values in parse_qs(request.url.query.decode()).items()}
    name = fixture_for(request.url.path, query)
    if name is None:
        return httpx.Response(404, text="no fixture")
    return httpx.Response(
        200,
        content=(FIXTURES / name).read_bytes(),
        headers={"Content-Type": "application/json", "Cache-Control": "public, max-age=60"},
    )


@pytest.fixture(autouse=True)
def _isolated_client(monkeypatch: pytest.MonkeyPatch):
    """Start every test with an empty cache and no request pacing."""
    monkeypatch.setenv("NBA_REQUEST_INTERVAL", "0")
    api.clear_cache()
    yield
    api.clear_cache()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route the shared client to the fixtures and record every request it makes."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fixture_response(request)

    client = httpx.Client(transport=httpx.MockTransport(handler), headers=api.HEADERS)
    monkeypatch.setattr(api, "_client", lambda: client)
    return seen
