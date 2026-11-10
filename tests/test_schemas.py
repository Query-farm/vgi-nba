"""Parsing and Arrow-schema tests over recorded upstream responses."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from conftest import load

from vgi_nba import schemas as s
from vgi_nba.api import result_set
from vgi_nba.meta import comment_of, result_columns_schema, sql_type


def _batch(records, cols) -> pa.RecordBatch:
    return s.batch(records, cols, s.schema_of(cols))


def test_every_column_has_a_comment() -> None:
    for name in dir(s):
        if name.endswith("_SCHEMA"):
            schema = getattr(s, name)
            assert all(comment_of(value) for value in schema), name
            assert len({value.name for value in schema}) == len(schema), name
            assert len(result_columns_schema(schema)) < 4_000, name


def test_standings_flatten() -> None:
    rows = s.with_stamps(
        result_set(load("leaguestandingsv3.json"), "Standings"),
        season="2025-26",
        season_type="Regular Season",
    )
    batch = _batch(rows, s.STANDINGS_COLS)
    first = batch.to_pylist()[0]
    assert batch.num_rows == 4
    assert first["season"] == "2025-26"
    assert first["team_name"] == "Thunder"
    assert first["conference"] == "West"
    assert isinstance(first["win_percentage"], Decimal)
    assert first["conference_games_back"] == Decimal("0.0")
    assert first["wins"] + first["losses"] == 82
    assert first["current_streak"] == int(
        first["current_streak_display"].replace("W ", "").replace("L ", "-")
    )
    assert isinstance(first["clinched_playoff_berth"], bool)


def test_wnba_standings_have_no_divisions_and_clean_clinch_markers() -> None:
    rows = result_set(load("leaguestandingsv3_wnba.json"), "Standings")
    batch = _batch(s.with_stamps(rows, season="2026", season_type="Regular Season"), s.STANDINGS_COLS)
    first = batch.to_pylist()[0]
    assert first["division"] is None
    assert first["division_rank"] is None
    assert first["clinch_indicator"] == "x"


def test_scoreboard_and_cdn_scoreboard_share_a_shape() -> None:
    for name in ("scoreboardv3.json", "todaysScoreboard_00.json"):
        board_date, games = s.scoreboard_records(load(name))
        assert isinstance(board_date, date)
        batch = _batch(s.with_stamps(games, game_date=board_date), s.SCOREBOARD_COLS)
        row = batch.to_pylist()[0]
        assert len(row["game_id"]) == 10 and row["game_id"].startswith("002")
        assert row["season_type"] == "Regular Season"
        assert row["home_team_tricode"] and row["away_team_tricode"]
        assert row["start_time_utc"].tzinfo is not None
    with pytest.raises(ValueError, match="no scoreboard"):
        s.scoreboard_records({})
    with pytest.raises(ValueError, match="no games"):
        s.scoreboard_records({"scoreboard": {}})


def test_schedule_flatten() -> None:
    season, games = s.schedule_records(load("scheduleLeagueV2.json"))
    batch = _batch(s.with_stamps(games, season=season), s.SCHEDULE_COLS)
    rows = batch.to_pylist()
    assert season == "2026-27"
    assert rows[0]["season_type"] == "Pre Season"
    assert rows[0]["game_date"] == date(2026, 10, 3)
    assert rows[0]["start_time_utc"] == datetime(2026, 10, 3, 23, 0, tzinfo=UTC)
    assert rows[0]["is_postponed"] is False
    assert {row["season_type"] for row in rows[1:]} == {"Regular Season"}
    with pytest.raises(ValueError, match="gameDates"):
        s.schedule_records({"leagueSchedule": {}})


def test_teams_join_active_years_with_franchise_summaries() -> None:
    years = result_set(load("commonteamyears.json"))
    franchises = result_set(load("franchisehistory.json"), "FranchiseHistory")
    rows = _batch(s.team_records(years, franchises), s.TEAM_COLS).to_pylist()
    assert [row["team_abbreviation"] for row in rows] == ["CHA", "DET", "WAS"]  # defunct ids drop out
    detroit = rows[1]
    assert detroit["team_city"] == "Detroit"
    assert detroit["seasons"] == 79  # the all-time summary row, not an era row
    assert detroit["first_season"] == 1948
    assert rows[2]["team_city"] is None  # no franchise row recorded for this id


def test_franchise_history_labels_rows() -> None:
    payload = load("franchisehistory.json")
    rows = s.franchise_records(result_set(payload, "FranchiseHistory"), result_set(payload, "DefunctTeams"))
    batch = _batch(rows, s.FRANCHISE_COLS).to_pylist()
    kinds = [row["row_kind"] for row in batch]
    assert kinds.count("franchise") == 3
    assert "era" in kinds and kinds[-1] == "defunct"


def test_roster_flatten() -> None:
    rows = result_set(load("commonteamroster.json"), "CommonTeamRoster")
    batch = _batch(s.with_stamps(rows, season="2026-27"), s.ROSTER_COLS).to_pylist()
    first = batch[0]
    assert first["height_inches"] == 84
    assert first["birth_date"] == date(2003, 5, 9)
    assert first["weight_pounds"] == 235
    assert first["jersey_number"] is None


def test_players_flatten() -> None:
    rows = _batch(result_set(load("commonallplayers.json")), s.PLAYER_COLS).to_pylist()
    assert [row["is_active"] for row in rows] == [True, True, True, False, False]
    assert rows[0]["from_year"] <= rows[0]["to_year"]
    assert all(row["team_id"] is None for row in rows if not row["is_active"])


@pytest.mark.parametrize(
    ("name", "cols", "key"),
    [
        ("leaguedashplayerstats.json", s.PLAYER_STATS_COLS, "player_id"),
        ("leaguedashplayerstats_advanced.json", s.ADVANCED_PLAYER_COLS, "player_id"),
        ("leaguedashteamstats.json", s.TEAM_STATS_COLS, "team_id"),
        ("leaguedashteamstats_advanced.json", s.ADVANCED_TEAM_COLS, "team_id"),
        ("leagueleaders.json", s.LEADER_COLS, "player_id"),
    ],
)
def test_dashboards_flatten(name: str, cols, key: str) -> None:
    rows = s.with_stamps(
        result_set(load(name)),
        season="2025-26",
        season_type="Regular Season",
        per_mode="PerGame",
        stat_category="PTS",
    )
    batch = _batch(rows, cols)
    assert batch.num_rows == 3
    assert None not in batch.column(key).to_pylist()
    assert batch.column("season").to_pylist() == ["2025-26"] * 3


def test_leaders_are_ranked() -> None:
    rows = _batch(result_set(load("leagueleaders.json"), "LeagueLeaders"), s.LEADER_COLS).to_pylist()
    assert [row["rank"] for row in rows] == [1, 2, 3]
    assert rows[0]["player_name"] == "Luka Dončić"
    assert rows[0]["points"] == pytest.approx(33.5)


def test_game_logs_parse_matchups_and_dates() -> None:
    team_rows = _batch(result_set(load("leaguegamelog.json")), s.GAME_LOG_COLS).to_pylist()
    first = team_rows[0]
    assert first["game_date"] == date(2025, 10, 21)
    assert first["team_abbreviation"] == "LAL" and first["opponent_abbreviation"] == "GSW"
    assert first["is_home"] is True
    assert first["player_id"] is None
    player_rows = _batch(result_set(load("leaguegamelog_player.json")), s.GAME_LOG_COLS).to_pylist()
    assert player_rows[0]["player_id"] and player_rows[0]["player_name"]
    single = _batch(result_set(load("playergamelog.json")), s.GAME_LOG_COLS).to_pylist()
    assert single[0]["game_date"] == date(2026, 4, 12)
    assert single[0]["player_id"] == 2544
    assert single[0]["team_abbreviation"] == "LAL"


def test_career_marks_multi_team_totals() -> None:
    payload = load("playercareerstats.json")
    seasons = _batch(result_set(payload, "SeasonTotalsRegularSeason"), s.CAREER_COLS).to_pylist()
    totals = _batch(result_set(payload, "CareerTotalsRegularSeason"), s.CAREER_TOTAL_COLS).to_pylist()
    assert seasons[0]["season"] == "2003-04" and seasons[0]["team_abbreviation"] == "CLE"
    assert not any(row["is_multi_team_total"] for row in seasons)
    assert totals[0]["points"] == sum(row["points"] for row in seasons)
    tot = s.CAREER_COLS
    traded = _batch([{"TEAM_ID": 0, "TEAM_ABBREVIATION": "TOT", "PLAYER_ID": 1}], tot).to_pylist()[0]
    assert traded["is_multi_team_total"] is True and traded["team_id"] is None


def test_box_score_flatten() -> None:
    rows = _batch(s.box_score_records(load("boxscoretraditionalv3.json")), s.BOX_SCORE_COLS).to_pylist()
    assert {row["team_tricode"] for row in rows} == {"LAC", "NOP"}
    starter = rows[0]
    assert starter["is_starter"] is True and starter["is_home"] is True
    assert starter["seconds_played"] == 28 * 60 + 42
    assert starter["plus_minus"] == 4
    dnp = next(row for row in rows if row["status_comment"])
    assert dnp["is_starter"] is False and dnp["seconds_played"] is None
    with pytest.raises(ValueError, match="boxScoreTraditional"):
        s.box_score_records({})


def test_play_by_play_flatten() -> None:
    actions = s.play_by_play_records(load("playbyplayv3.json"))
    rows = _batch(s.with_stamps(actions, game_id="0022600201"), s.PLAY_BY_PLAY_COLS).to_pylist()
    assert rows[0]["seconds_remaining"] == Decimal("720.00")
    assert rows[0]["team_id"] is None and rows[0]["player_id"] is None
    ids = [row["action_id"] for row in rows]
    assert len(ids) == len(set(ids))
    shared = [row for row in rows if row["action_number"] == 22]
    assert len(shared) == 2  # a block shares the number of the miss it caused
    three = next(row for row in rows if row["shot_value"] == 3 and row["shot_result"] == "Made")
    assert three["is_field_goal"] is True and three["period"] == 4
    with pytest.raises(ValueError, match="actions"):
        s.play_by_play_records({"game": {}})


def test_projection_keeps_requested_column_order() -> None:
    rows = result_set(load("leaguedashteamstats.json"))
    projected = pa.schema([s.schema_of(s.TEAM_STATS_COLS).field("points"), pa.field("team_id", pa.int64())])
    batch = s.batch(rows, s.TEAM_STATS_COLS, projected)
    assert batch.schema.names == ["points", "team_id"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-09", date(2026, 11, 9)),
        ("2026-11-09T00:00:00Z", date(2026, 11, 9)),
        ("Apr 12, 2026", date(2026, 4, 12)),
        ("MAY 09, 2003", date(2003, 5, 9)),
        ("10/03/2026 00:00:00", date(2026, 10, 3)),
        ("2026-13-40", None),
        ("someday", None),
        ("", None),
        (None, None),
    ],
)
def test_to_date(value, expected) -> None:
    assert s.to_date(value) == expected


def test_scalar_parsers() -> None:
    assert s.to_int("12") == 12 and s.to_int("3.0") == 3 and s.to_int(True) is None and s.to_int("x") is None
    assert s.to_float("1.5") == 1.5 and s.to_float("nan") is None and s.to_float("") is None
    assert s.to_float("abc") is None
    assert s.to_decimal("0.500") == Decimal("0.500") and s.to_decimal("x") is None
    assert s.to_decimal(True) is None and s.to_decimal("inf") is None
    assert s.to_text("  hi ") == "hi" and s.to_text("  ") is None and s.to_text(None) is None
    assert s.to_bool("Y") is True and s.to_bool(0) is False and s.to_bool("maybe") is None
    assert s.to_id(0) is None and s.to_id("1610612747") == 1610612747
    assert s.to_timestamp("bad") is None and s.to_timestamp(None) is None
    assert s.to_timestamp("2026-11-10T00:00:00").tzinfo is UTC
    assert s.minutes_to_seconds("12:30") == 750 and s.minutes_to_seconds("") is None
    assert s.minutes_to_seconds("x:y") is None
    assert s.clock_to_seconds("PT00M05.40S") == Decimal("5.40") and s.clock_to_seconds("") is None
    assert s.clock_to_seconds(None) is None
    assert s.height_to_inches("6-11") == 83 and s.height_to_inches("tall") is None
    assert s.height_to_inches(None) is None
    assert s.experience_years("R") == 0 and s.experience_years("7") == 7
    assert s.season_type_of_game("1042500001") == "Playoffs"
    assert s.season_type_of_game("2022500298") == "Regular Season"
    assert s.season_type_of_game(None) is None and s.season_type_of_game("00") is None
    assert s._matchup_parts("LAL @ BOS") == ("LAL", "BOS", False)
    assert s._matchup_parts("LAL v BOS") == (None, None, None)
    assert s._matchup_parts(None) == (None, None, None)


def test_sql_type_names() -> None:
    assert sql_type(pa.decimal128(4, 3)) == "DECIMAL(4,3)"
    assert sql_type(pa.timestamp("us", tz="UTC")) == "TIMESTAMP WITH TIME ZONE"
    assert sql_type(pa.timestamp("us")) == "TIMESTAMP"
    assert sql_type(pa.date32()) == "DATE"
    assert sql_type(pa.bool_()) == "BOOLEAN"
    with pytest.raises(ValueError, match="Unsupported"):
        sql_type(pa.list_(pa.int8()))
