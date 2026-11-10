"""Every generated function and current table, executed against recorded fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
import pytest

from vgi_nba import api
from vgi_nba import functions as f
from vgi_nba import schemas as s
from vgi_nba.leagues import GLEAGUE, LEAGUES, NBA, WNBA


@dataclass
class _Args:
    season: int | None = 2025
    season_type: str = "Regular Season"
    per_mode: str = "PerGame"
    stat_category: str = "PTS"
    player_or_team: str = "T"
    game_date: date = date(2026, 11, 9)
    team_id: int = 1610612747
    only_current: bool = True
    player_id: int = 2544
    game_id: str = "0022600201"


def _game(league) -> str:
    return {"00": "0022600201", "10": "1022500200", "20": "2022500298"}[league.league_id]


@pytest.mark.parametrize("league", LEAGUES, ids=lambda league: league.schema)
def test_every_spec_returns_typed_rows(league, recorded) -> None:
    for spec in [*f.SPECS, *f.CURRENT_SPECS]:
        if league.schema not in spec.leagues:
            continue
        rows, ttl = spec.fetch(league, _Args(game_id=_game(league)))
        batch = s.batch(f._dedupe(rows, spec), spec.cols, s.schema_of(spec.cols))
        assert batch.num_rows > 0, spec.name
        assert ttl >= 1
        for column in spec.key:
            assert None not in batch.column(column).to_pylist(), (spec.name, column)


def test_requests_carry_league_and_season_keys(recorded) -> None:
    f.fetch_standings(WNBA, 2025, "Pre Season")
    f.fetch_standings(NBA, 2025, "Regular Season")
    wnba, nba = recorded
    assert wnba.url.params["LeagueID"] == "10" and wnba.url.params["Season"] == "2025"
    assert wnba.url.params["SeasonType"] == "Pre Season"
    assert nba.url.params["Season"] == "2025-26"


def test_dashboard_sends_every_filter(recorded) -> None:
    f._dashboard("leaguedashplayerstats", GLEAGUE, 2024, "Playoffs", "Totals", "Base")
    params = recorded[0].url.params
    assert params["LeagueID"] == "20" and params["Season"] == "2024-25"
    assert params["PerMode"] == "Totals" and params["MeasureType"] == "Base"
    assert set(f.DASHBOARD_DEFAULTS) <= set(params)


def test_current_scoreboard_sources(recorded) -> None:
    f.fetch_current_scoreboard(NBA)
    f.fetch_current_scoreboard(WNBA)
    f.fetch_current_scoreboard(GLEAGUE)
    nba, wnba, gleague = (request.url for request in recorded)
    assert nba.host == "cdn.nba.com" and nba.path.endswith("todaysScoreboard_00.json")
    assert wnba.host == "cdn.wnba.com" and wnba.path.endswith("todaysScoreboard_10.json")
    assert gleague.host == "stats.nba.com" and gleague.params["LeagueID"] == "20"


def test_players_filter_on_roster_status(recorded) -> None:
    current, _ttl = f.fetch_players(WNBA, True)
    everyone, _ttl = f.fetch_players(WNBA, False)
    assert len(current) == 3 and len(everyone) == 5
    assert recorded[0].url.params["IsOnlyCurrentSeason"] == "0"


def test_missing_season_falls_back_to_the_latest_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    seasons: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seasons.append(request.url.params["Season"])
        rows = [] if len(seasons) == 1 else [["00", 1610612760, "Thunder"]]
        return httpx.Response(
            200,
            json={
                "resultSets": [{"name": "Standings", "headers": ["L", "TeamID", "TeamName"], "rowSet": rows}]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(api, "_client", lambda: client)
    monkeypatch.setattr(GLEAGUE.__class__, "current_season", lambda self, today=None: 2026)
    rows, _ttl = f.fetch_standings(GLEAGUE, None, "Regular Season")
    assert seasons == ["2026-27", "2025-26"]
    assert rows[0]["__season"] == "2025-26"


def test_explicit_empty_season_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"resultSets": [{"name": "Standings", "headers": [], "rowSet": []}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(api, "_client", lambda: client)
    assert f.fetch_standings(NBA, 1999, "Regular Season")[0] == []
    assert calls == 1


def test_game_ids_must_belong_to_the_league() -> None:
    with pytest.raises(ValueError, match="WNBA game IDs start with 1"):
        f.fetch_box_score(WNBA, "0022600201")
    with pytest.raises(ValueError, match="G League game IDs start with 2"):
        f.fetch_play_by_play(GLEAGUE, "1022500200")


def test_schedule_is_unavailable_for_the_g_league() -> None:
    with pytest.raises(ValueError, match="no season schedule"):
        f.fetch_schedule(GLEAGUE)
    assert "nba.gleague.current_schedule" not in str(f.SURFACES["gleague"].tables)


def test_dedupe_drops_null_and_repeated_keys() -> None:
    spec = next(spec for spec in f.SPECS if spec.name == "box_score")
    rows = [{"personId": 1}, {"personId": 1}, {"personId": None}, {"personId": 2}]
    assert [row["personId"] for row in f._dedupe(rows, spec)] == [1, 2]
    unkeyed = next(spec for spec in f.SPECS if spec.name == "league_game_log")
    assert f._dedupe(rows, unkeyed) == rows


def test_render_substitutes_league_samples() -> None:
    text = "{s}|{league}|{player}|{team}|{game}|{date}|{season}|{player_search}"
    assert f.render(text, WNBA) == "wnba|WNBA|1628932|1611661319|1022500200|2026-07-15|2025|Wilson"


def test_backing_scan_examples_call_the_scan() -> None:
    surface = f.SURFACES["nba"]
    for spec, cls in surface.tables:
        assert all(f"nba.nba.{spec.name}()" in example.sql for example in cls.Meta.examples)


# Columns that are legitimately NULL in every recorded fixture row: in-game or
# postseason fields on a finished regular-season slate, team rows of a team-mode
# game log, and fields the WNBA (no divisions) or the trimmed fixtures leave empty.
_LIVE_ONLY = {"game_clock", "series_text", "game_label"}
_EXPECTED_EMPTY: dict[str, set[str]] = {
    "standings": {"eliminated_from_playoffs", "division", "division_rank"},
    "all_current_standings": {"eliminated_from_playoffs", "division", "division_rank"},
    "scoreboard": _LIVE_ONLY,
    "all_current_scoreboard": _LIVE_ONLY,
    "league_game_log": {"player_id", "player_name"},
}


@pytest.mark.parametrize("league", LEAGUES, ids=lambda league: league.schema)
def test_no_column_is_always_null(league, recorded) -> None:
    empty: dict[str, list[str]] = {}
    for spec in [*f.SPECS, *f.CURRENT_SPECS]:
        if league.schema not in spec.leagues:
            continue
        rows, _ttl = spec.fetch(league, _Args(game_id=_game(league)))
        batch = s.batch(f._dedupe(rows, spec), spec.cols, s.schema_of(spec.cols))
        nulls = [
            name
            for name in batch.schema.names
            if batch.column(name).null_count == batch.num_rows
            and name not in _EXPECTED_EMPTY.get(spec.name, ())
        ]
        if nulls:
            empty[spec.name] = nulls
    assert empty == {}, empty
