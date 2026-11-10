"""Opt-in smoke tests against the public NBA Stats API and NBA/WNBA CDNs.

They pin historical seasons and games so the assertions hold as data moves.
"""

from __future__ import annotations

from datetime import date

import pytest

from vgi_nba import functions as f
from vgi_nba import schemas as s
from vgi_nba.leagues import GLEAGUE, NBA, WNBA

pytestmark = pytest.mark.live


def _rows(spec_name: str, league, args) -> list[dict]:
    spec = next(spec for spec in [*f.SPECS, *f.CURRENT_SPECS] if spec.name == spec_name)
    rows, _ttl = spec.fetch(league, args)
    return s.batch(f._dedupe(rows, spec), spec.cols, s.schema_of(spec.cols)).to_pylist()


class _A:
    def __init__(self, **values) -> None:
        defaults = {
            "season": None,
            "season_type": "Regular Season",
            "per_mode": "PerGame",
            "stat_category": "PTS",
            "player_or_team": "T",
            "only_current": True,
        }
        self.__dict__.update({**defaults, **values})


def test_final_2025_26_standings_balance() -> None:
    rows = _rows("standings", NBA, _A(season=2025))
    assert len(rows) == 30
    assert sum(row["wins"] for row in rows) == sum(row["losses"] for row in rows) == 1230
    assert {row["conference"] for row in rows} == {"East", "West"}


def test_wnba_and_g_league_standings() -> None:
    wnba = _rows("standings", WNBA, _A(season=2025))
    assert len(wnba) == 13 and wnba[0]["season"] == "2025"
    gleague = _rows("standings", GLEAGUE, _A(season=2025))
    assert len(gleague) >= 28 and sum(r["wins"] for r in gleague) == sum(r["losses"] for r in gleague)


def test_current_tables_are_populated() -> None:
    assert len(_rows("all_current_teams", NBA, _A())) == 30
    assert len(_rows("all_current_standings", NBA, _A())) == 30
    assert len(_rows("all_current_players", WNBA, _A())) > 100
    assert len(_rows("all_current_schedule", NBA, _A())) > 1000
    assert isinstance(_rows("all_current_scoreboard", NBA, _A()), list)


def test_known_career_and_leaders() -> None:
    totals = _rows("player_career_totals", NBA, _A(player_id=2544))
    assert totals[0]["points"] >= 43_000
    best = max(_rows("player_career_stats", NBA, _A(player_id=2544)), key=lambda row: row["points"])
    assert (best["season"], best["points"]) == ("2005-06", 2478)
    leader = _rows("league_leaders", WNBA, _A(season=2025))[0]
    assert leader["player_name"] == "A'ja Wilson" and leader["points"] == pytest.approx(23.4)


def test_completed_game_is_internally_consistent() -> None:
    box = _rows("box_score", NBA, _A(game_id="0022600201"))
    board = _rows("scoreboard", NBA, _A(game_date=date(2026, 11, 8)))
    game = next(row for row in board if row["game_id"] == "0022600201")
    assert sum(row["points"] or 0 for row in box) == game["home_score"] + game["away_score"] == 262
    plays = _rows("play_by_play", NBA, _A(game_id="0022600201"))
    assert max(row["home_score"] for row in plays) == game["home_score"]


def test_dashboards_for_every_league() -> None:
    for league in (NBA, WNBA, GLEAGUE):
        players = _rows("player_stats", league, _A(season=2025))
        teams = _rows("advanced_team_stats", league, _A(season=2025))
        assert len(players) > 100 and len(teams) >= 13, league.schema
