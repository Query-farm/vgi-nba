"""VGI table functions for every NBA-family dataset, generated once per league.

Each dataset is described by one :class:`Spec`. :func:`build_functions`
turns a spec into a concrete ``TableFunctionGenerator`` subclass for each
league (``nba``, ``wnba``, ``gleague``), with league-specific names in its
documentation and runnable example SQL.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

import pyarrow as pa
from vgi.arguments import Arg
from vgi.cache_control import CacheControl
from vgi.metadata import FunctionExample
from vgi.table_function import (
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc.rpc import OutputCollector

from vgi_nba import api
from vgi_nba import schemas as s
from vgi_nba.leagues import GLEAGUE, NBA, WNBA, League, eastern_today
from vgi_nba.meta import result_columns_schema

SEASON_TYPES = ("Regular Season", "Playoffs", "Pre Season", "PlayIn")
STANDINGS_SEASON_TYPES = ("Regular Season", "Pre Season", "PlayIn")
CAREER_SEASON_TYPES = ("Regular Season", "Playoffs")
PER_MODES = ("PerGame", "Totals", "Per36", "Per100Possessions")
LEADER_PER_MODES = ("PerGame", "Totals", "Per48")
STAT_CATEGORIES = (
    "PTS",
    "REB",
    "AST",
    "STL",
    "BLK",
    "TOV",
    "MIN",
    "FGM",
    "FG_PCT",
    "FG3M",
    "FG3_PCT",
    "FTM",
    "FT_PCT",
    "OREB",
    "DREB",
    "EFF",
)

# leaguedash* endpoints answer HTTP 500 unless every filter is present, even when blank.
DASHBOARD_DEFAULTS: dict[str, str | int] = {
    "College": "",
    "Conference": "",
    "Country": "",
    "DateFrom": "",
    "DateTo": "",
    "Division": "",
    "DraftPick": "",
    "DraftYear": "",
    "GameScope": "",
    "GameSegment": "",
    "Height": "",
    "LastNGames": 0,
    "Location": "",
    "Month": 0,
    "OpponentTeamID": 0,
    "Outcome": "",
    "PORound": 0,
    "PaceAdjust": "N",
    "Period": 0,
    "PlayerExperience": "",
    "PlayerPosition": "",
    "PlusMinus": "N",
    "Rank": "N",
    "SeasonSegment": "",
    "ShotClockRange": "",
    "StarterBench": "",
    "TeamID": 0,
    "TwoWay": 0,
    "VsConference": "",
    "VsDivision": "",
    "Weight": "",
}

_SEASON_DOC = (
    "Start year of the season: 2025 means 2025-26 in the NBA and G League and the 2025 season in "
    "the WNBA. NULL selects the current or most recent season."
)


def _season_arg() -> Arg:
    return Arg("season", default=None, arrow_type=pa.int32(), ge=1946, le=2100, doc=_SEASON_DOC)


def _choice(name: str, default: str, choices: Sequence[str], doc: str) -> Arg:
    return Arg(name, default=default, arrow_type=pa.string(), choices=tuple(choices), doc=doc)


_SEASON_TYPE = _choice(
    "season_type", "Regular Season", SEASON_TYPES, "Regular Season, Playoffs, Pre Season, or PlayIn."
)
# leaguestandingsv3 answers an empty result set for Playoffs in every league.
_STANDINGS_SEASON_TYPE = _choice(
    "season_type",
    "Regular Season",
    STANDINGS_SEASON_TYPES,
    "Regular Season, Pre Season, or PlayIn; the league publishes no playoff standings.",
)
_CAREER_SEASON_TYPE = _choice(
    "season_type", "Regular Season", CAREER_SEASON_TYPES, "Regular Season or Playoffs totals."
)
_PER_MODE = _choice(
    "per_mode", "PerGame", PER_MODES, "PerGame averages, season Totals, Per36 minutes, or Per100Possessions."
)
_PLAYER_ID = Arg(0, arrow_type=pa.int64(), ge=1, doc="Stable NBA Stats player identifier.")
_GAME_ID = Arg(
    0,
    arrow_type=pa.string(),
    pattern=r"^[0-9]{10}$",
    doc="Ten-digit NBA Stats game identifier as text, such as '0022600201'.",
)


@dataclass(kw_only=True)
class SeasonArgs:
    """Season and season type."""

    season: Annotated[int | None, _season_arg()]
    season_type: Annotated[str, _SEASON_TYPE]


@dataclass(kw_only=True)
class StandingsArgs:
    """Season and a standings season type."""

    season: Annotated[int | None, _season_arg()]
    season_type: Annotated[str, _STANDINGS_SEASON_TYPE]


@dataclass(kw_only=True)
class DashboardArgs:
    """Season, season type, and per-mode aggregation."""

    season: Annotated[int | None, _season_arg()]
    season_type: Annotated[str, _SEASON_TYPE]
    per_mode: Annotated[str, _PER_MODE]


@dataclass(kw_only=True)
class LeaderArgs:
    """Leaderboard selection."""

    season: Annotated[int | None, _season_arg()]
    season_type: Annotated[str, _SEASON_TYPE]
    stat_category: Annotated[
        str,
        _choice("stat_category", "PTS", STAT_CATEGORIES, "Statistic to rank by, such as PTS, REB, or AST."),
    ]
    per_mode: Annotated[str, _choice("per_mode", "PerGame", LEADER_PER_MODES, "PerGame, Totals, or Per48.")]


@dataclass(kw_only=True)
class GameLogArgs:
    """League-wide game-log selection."""

    season: Annotated[int | None, _season_arg()]
    season_type: Annotated[str, _SEASON_TYPE]
    player_or_team: Annotated[
        str,
        _choice(
            "player_or_team", "T", ("T", "P"), "T for one row per team-game, P for one row per player-game."
        ),
    ]


@dataclass(kw_only=True)
class DateArgs:
    """One scoreboard date."""

    game_date: Annotated[
        date,
        Arg(
            0,
            arrow_type=pa.date32(),
            doc="Calendar day of the games, as the league schedules it in US Eastern time.",
        ),
    ]


@dataclass(kw_only=True)
class RosterArgs:
    """One team's roster for a season."""

    team_id: Annotated[int, Arg(0, arrow_type=pa.int64(), ge=1, doc="Stable NBA Stats team identifier.")]
    season: Annotated[int | None, _season_arg()]


@dataclass(kw_only=True)
class PlayersArgs:
    """Player-index selection."""

    only_current: Annotated[
        bool,
        Arg(
            "only_current",
            default=True,
            arrow_type=pa.bool_(),
            doc="True returns players on a current roster; false returns every player in league history.",
        ),
    ]


@dataclass(kw_only=True)
class PlayerSeasonArgs:
    """One player's season."""

    player_id: Annotated[int, _PLAYER_ID]
    season: Annotated[int | None, _season_arg()]
    season_type: Annotated[str, _SEASON_TYPE]


@dataclass(kw_only=True)
class CareerArgs:
    """One player's career."""

    player_id: Annotated[int, _PLAYER_ID]
    season_type: Annotated[str, _CAREER_SEASON_TYPE]


@dataclass(kw_only=True)
class GameArgs:
    """One game."""

    game_id: Annotated[str, _GAME_ID]


# --------------------------------------------------------------------------- fetchers

Rows = tuple[list[dict[str, Any]], int]


def _stats(endpoint: str, params: Mapping[str, str | int], ttl: int = api.TTL_STATS) -> api.ApiResponse:
    return api.fetch_stats(endpoint, params, ttl_floor=ttl)


def _season_or_latest(league: League, season: int | None, fetch: Callable[[str], Rows]) -> Rows:
    """Fetch ``season``; with no season, fall back to the last one if the new one has no rows yet.

    League-wide results for a season that has only just rolled over (preseason,
    or the G League before its regular season) are empty, so NULL season means
    "the current season, or the most recent one with data". Each row's season
    column says which season was returned.
    """
    start = league.resolve_season(season)
    rows, ttl = fetch(league.season_label(start))
    if not rows and season is None and start - 1 >= league.first_season:
        rows, ttl = fetch(league.season_label(start - 1))
    return rows, ttl


def fetch_standings(league: League, season: int | None, season_type: str) -> Rows:
    """Standings for one season and season type."""

    def one(label: str) -> Rows:
        response = _stats(
            "leaguestandingsv3", {"LeagueID": league.league_id, "Season": label, "SeasonType": season_type}
        )
        rows = api.result_set(response.payload, "Standings")
        return s.with_stamps(rows, season=label, season_type=season_type), response.ttl

    return _season_or_latest(league, season, one)


def _scoreboard_rows(response: api.ApiResponse, fallback: date) -> Rows:
    board_date, games = s.scoreboard_records(response.payload)
    return s.with_stamps(games, game_date=board_date or fallback), response.ttl


def fetch_scoreboard(league: League, game_date: date) -> Rows:
    """Every game on one date."""
    response = _stats(
        "scoreboardv3", {"GameDate": game_date.isoformat(), "LeagueID": league.league_id}, api.TTL_LIVE
    )
    return _scoreboard_rows(response, game_date)


def fetch_current_scoreboard(league: League) -> Rows:
    """Today's games from the live CDN scoreboard (or the Stats API for the G League)."""
    today = eastern_today()
    if league.cdn is None:
        return fetch_scoreboard(league, today)
    response = api.fetch_cdn(
        league.cdn, f"liveData/scoreboard/todaysScoreboard_{league.league_id}.json", ttl_floor=api.TTL_LIVE
    )
    return _scoreboard_rows(response, today)


def fetch_schedule(league: League) -> Rows:
    """The full published current-season schedule."""
    if league.cdn is None or league.schedule_path is None:
        raise ValueError(f"The {league.short} publishes no season schedule feed")
    response = api.fetch_cdn(league.cdn, league.schedule_path, ttl_floor=api.TTL_STATS)
    season, games = s.schedule_records(response.payload)
    return s.with_stamps(games, season=season), response.ttl


def fetch_teams(league: League) -> Rows:
    """Active teams joined with their all-time franchise summaries."""
    years = _stats("commonteamyears", {"LeagueID": league.league_id}, api.TTL_STATIC)
    history = _stats("franchisehistory", {"LeagueID": league.league_id}, api.TTL_STATIC)
    rows = s.team_records(api.result_set(years.payload), api.result_set(history.payload, "FranchiseHistory"))
    return rows, min(years.ttl, history.ttl)


def fetch_franchise_history(league: League) -> Rows:
    """Franchise summaries, name eras, and defunct franchises."""
    response = _stats("franchisehistory", {"LeagueID": league.league_id}, api.TTL_STATIC)
    rows = s.franchise_records(
        api.result_set(response.payload, "FranchiseHistory"), api.result_set(response.payload, "DefunctTeams")
    )
    return rows, response.ttl


def fetch_roster(league: League, team_id: int, season: int | None) -> Rows:
    """One team's roster."""
    label = league.season_label(league.resolve_season(season))
    response = _stats("commonteamroster", {"TeamID": team_id, "Season": label, "LeagueID": league.league_id})
    return s.with_stamps(api.result_set(response.payload, "CommonTeamRoster"), season=label), response.ttl


def fetch_players(league: League, only_current: bool) -> Rows:
    """The league's player index.

    ``IsOnlyCurrentSeason=1`` is unreliable outside the NBA (the WNBA answers
    HTTP 500 or a partial list), so the full index is fetched and filtered on
    roster status locally.
    """
    label = league.season_label(league.current_season())
    response = _stats(
        "commonallplayers",
        {"LeagueID": league.league_id, "Season": label, "IsOnlyCurrentSeason": 0},
        1800,
    )
    rows = api.result_set(response.payload, "CommonAllPlayers")
    if only_current:
        rows = [row for row in rows if s.to_bool(row.get("ROSTERSTATUS"))]
    return rows, response.ttl


def _dashboard(
    endpoint: str, league: League, season: int | None, season_type: str, per_mode: str, measure: str
) -> Rows:
    def one(label: str) -> Rows:
        response = _stats(
            endpoint,
            {
                **DASHBOARD_DEFAULTS,
                "LeagueID": league.league_id,
                "Season": label,
                "SeasonType": season_type,
                "PerMode": per_mode,
                "MeasureType": measure,
            },
        )
        rows = api.result_set(response.payload)
        return s.with_stamps(rows, season=label, season_type=season_type, per_mode=per_mode), response.ttl

    return _season_or_latest(league, season, one)


def fetch_leaders(
    league: League, season: int | None, season_type: str, stat_category: str, per_mode: str
) -> Rows:
    """An official leaderboard."""

    def one(label: str) -> Rows:
        response = _stats(
            "leagueleaders",
            {
                "LeagueID": league.league_id,
                "PerMode": per_mode,
                "Scope": "S",
                "Season": label,
                "SeasonType": season_type,
                "StatCategory": stat_category,
            },
        )
        rows = api.result_set(response.payload, "LeagueLeaders")
        stamped = s.with_stamps(
            rows, season=label, season_type=season_type, stat_category=stat_category, per_mode=per_mode
        )
        return stamped, response.ttl

    return _season_or_latest(league, season, one)


def fetch_league_game_log(league: League, season: int | None, season_type: str, player_or_team: str) -> Rows:
    """Every team-game or player-game line of a season."""

    def one(label: str) -> Rows:
        response = _stats(
            "leaguegamelog",
            {
                "Counter": 0,
                "Direction": "ASC",
                "LeagueID": league.league_id,
                "PlayerOrTeam": player_or_team,
                "Season": label,
                "SeasonType": season_type,
                "Sorter": "DATE",
            },
        )
        return s.with_stamps(api.result_set(response.payload, "LeagueGameLog"), season=label), response.ttl

    return _season_or_latest(league, season, one)


def fetch_player_game_log(league: League, player_id: int, season: int | None, season_type: str) -> Rows:
    """One player's game-by-game lines, in game order."""
    label = league.season_label(league.resolve_season(season))
    response = _stats(
        "playergamelog",
        {"PlayerID": player_id, "Season": label, "SeasonType": season_type, "LeagueID": league.league_id},
    )
    rows = sorted(
        api.result_set(response.payload, "PlayerGameLog"), key=lambda row: str(row.get("Game_ID") or "")
    )
    return s.with_stamps(rows, season=label), response.ttl


_CAREER_SETS = {"Regular Season": "RegularSeason", "Playoffs": "PostSeason"}


def _career(league: League, player_id: int, season_type: str, kind: str) -> Rows:
    response = _stats(
        "playercareerstats", {"PlayerID": player_id, "PerMode": "Totals", "LeagueID": league.league_id}
    )
    rows = api.result_set(response.payload, f"{kind}{_CAREER_SETS[season_type]}")
    return s.with_stamps(rows, season_type=season_type), response.ttl


def _check_game(league: League, game_id: str) -> None:
    prefix = {"00": "0", "10": "1", "20": "2"}[league.league_id]
    if not game_id.startswith(prefix):
        raise ValueError(
            f"{league.short} game IDs start with {prefix}; {game_id!r} belongs to another league"
        )


def fetch_box_score(league: League, game_id: str) -> Rows:
    """Traditional box score, one row per player."""
    _check_game(league, game_id)
    response = _stats(
        "boxscoretraditionalv3",
        {"GameID": game_id, "StartPeriod": 0, "EndPeriod": 0, "StartRange": 0, "EndRange": 0, "RangeType": 0},
        api.TTL_LIVE,
    )
    return s.box_score_records(response.payload), response.ttl


def fetch_play_by_play(league: League, game_id: str) -> Rows:
    """Every play-by-play event of a game."""
    _check_game(league, game_id)
    response = _stats("playbyplayv3", {"GameID": game_id, "StartPeriod": 0, "EndPeriod": 0}, api.TTL_LIVE)
    return s.with_stamps(s.play_by_play_records(response.payload), game_id=game_id), response.ttl


# --------------------------------------------------------------------------- specs


@dataclass(frozen=True)
class Example:
    """One example query; ``{s}`` is replaced by the schema and ids by league values."""

    description: str
    sql: str


@dataclass(frozen=True)
class Spec:
    """Everything needed to generate one function for every league."""

    name: str
    title: str
    description: str
    category: str
    cols: list[s.Col]
    args: type | None
    fetch: Callable[[League, Any], Rows]
    doc_llm: str
    doc_md: str
    examples: list[Example]
    estimate: dict[str, int]
    max_rows: int | None = None
    key: tuple[str, ...] = ()
    leagues: tuple[str, ...] = ("nba", "wnba", "gleague")
    keywords: list[str] = field(default_factory=list)


# League-specific identifiers used in runnable examples.
SAMPLES: dict[str, dict[str, str]] = {
    "nba": {
        "player": "2544",
        "player_name": "LeBron James",
        "player_search": "LeBron",
        "team": "1610612747",
        "team_name": "Los Angeles Lakers",
        "game": "0022600201",
        "date": "2026-11-09",
        "season": "2025",
    },
    "wnba": {
        "player": "1628932",
        "player_name": "A'ja Wilson",
        "player_search": "Wilson",
        "team": "1611661319",
        "team_name": "Las Vegas Aces",
        "game": "1022500200",
        "date": "2026-07-15",
        "season": "2025",
    },
    "gleague": {
        "player": "1630644",
        "player_name": "Mac McClung",
        "player_search": "McClung",
        "team": "1612709889",
        "team_name": "Oklahoma City Blue",
        "game": "2022500298",
        "date": "2026-02-10",
        "season": "2025",
    },
}


def render(text: str, league: League) -> str:
    """Substitute league placeholders in documentation and example SQL."""
    sample = SAMPLES[league.schema]
    return (
        text.replace("{s}", league.schema)
        .replace("{league}", league.short)
        .replace("{player}", sample["player"])
        .replace("{player_name}", sample["player_name"].replace("'", "’"))
        .replace("{player_search}", sample["player_search"])
        .replace("{team}", sample["team"])
        .replace("{team_name}", sample["team_name"])
        .replace("{game}", sample["game"])
        .replace("{date}", sample["date"])
        .replace("{season}", sample["season"])
    )


TEAMS = {"nba": 30, "wnba": 15, "gleague": 31}


def _per_league(nba: int, wnba: int, gleague: int) -> dict[str, int]:
    return {"nba": nba, "wnba": wnba, "gleague": gleague}


SPECS: list[Spec] = [
    Spec(
        name="standings",
        title="{league} Standings",
        description="{league} standings by season and season type",
        category="standings",
        cols=s.STANDINGS_COLS,
        args=StandingsArgs,
        fetch=lambda league, a: fetch_standings(league, a.season, a.season_type),
        doc_llm=(
            "One row per {league} team with conference and division rank, wins, losses, exact "
            "winning percentage, games back, home/road/last-ten records, streaks, scoring margin, "
            "and clinch flags. Use season => 2025 for the 2025 season (2025-26 where seasons span "
            "two years); NULL selects the current season."
        ),
        doc_md=(
            "{league} standings flattened to one row per team. `win_percentage` and the games-back "
            "columns are exact decimals; record columns keep the published W-L display text."
        ),
        examples=[
            Example(
                "Final {season} conference standings in seeding order.",
                "SELECT conference, playoff_rank, team_city, team_name, wins, losses "
                "FROM nba.{s}.standings(season := {season}) ORDER BY conference, playoff_rank",
            )
        ],
        estimate=TEAMS,
        max_rows=64,
        key=("team_id",),
    ),
    Spec(
        name="scoreboard",
        title="{league} Scoreboard",
        description="{league} games and scores on one date",
        category="games",
        cols=s.SCOREBOARD_COLS,
        args=DateArgs,
        fetch=lambda league, a: fetch_scoreboard(league, a.game_date),
        doc_llm=(
            "Every {league} game on one US Eastern date with status, period, tip-off time, and each "
            "team's score and record. Pass the date positionally. game_status is 1 scheduled, 2 live, "
            "3 final."
        ),
        doc_md="Scores and status for every {league} game on a date. Game IDs are text with leading zeros.",
        examples=[
            Example(
                "Final scores on {date}.",
                "SELECT game_id, away_team_tricode, away_score, home_team_tricode, home_score, "
                "game_status_text FROM nba.{s}.scoreboard(DATE '{date}') ORDER BY game_id",
            )
        ],
        estimate=_per_league(8, 4, 8),
        max_rows=40,
        key=("game_id",),
    ),
    Spec(
        name="team_roster",
        title="{league} Team Roster",
        description="One {league} team's roster for a season",
        category="teams",
        cols=s.ROSTER_COLS,
        args=RosterArgs,
        fetch=lambda league, a: fetch_roster(league, a.team_id, a.season),
        doc_llm=(
            "Roster for one {league} team ID and season: jersey number, position, height in inches, "
            "weight, birth date, age, experience, and school. Find team IDs in current_teams."
        ),
        doc_md="One row per rostered player for a team and season.",
        examples=[
            Example(
                "The current {team_name} roster.",
                "SELECT jersey_number, player_name, position, height, weight_pounds "
                "FROM nba.{s}.team_roster({team}) ORDER BY player_name",
            )
        ],
        estimate=_per_league(18, 13, 14),
        max_rows=40,
        key=("player_id",),
    ),
    Spec(
        name="players",
        title="{league} Player Index",
        description="The {league} player index, current or all-time",
        category="players",
        cols=s.PLAYER_COLS,
        args=PlayersArgs,
        fetch=lambda league, a: fetch_players(league, a.only_current),
        doc_llm=(
            "Player IDs and names for the {league}. By default only players on a current roster; "
            "only_current => false returns everyone in league history with first and last season. "
            "Use it to look up a player_id for the per-player functions."
        ),
        doc_md="The {league} player index with career span and current or last team.",
        examples=[
            Example(
                "Find a player's identifier.",
                "SELECT player_id, player_name, team_abbreviation, from_year "
                "FROM nba.{s}.players(only_current := false) WHERE player_name ILIKE '%{player_search}%'",
            )
        ],
        estimate=_per_league(520, 160, 380),
        key=("player_id",),
    ),
    Spec(
        name="player_stats",
        title="{league} Player Season Statistics",
        description="{league} player season statistics by season, season type, and per mode",
        category="player-stats",
        cols=s.PLAYER_STATS_COLS,
        args=DashboardArgs,
        fetch=lambda league, a: _dashboard(
            "leaguedashplayerstats", league, a.season, a.season_type, a.per_mode, "Base"
        ),
        doc_llm=(
            "One row per {league} player with games, minutes, shooting, rebounds, assists, steals, "
            "blocks, turnovers, points, plus-minus, and double/triple-doubles. per_mode chooses "
            "PerGame (default), Totals, Per36, or Per100Possessions; counting columns follow it. "
            "Filter games_played or minutes yourself for qualified leaders."
        ),
        doc_md="League-wide player box-score statistics for a season. Percentages are 0-1 fractions.",
        examples=[
            Example(
                "Top {season} scorers per game with at least 20 games.",
                "SELECT player_name, team_abbreviation, games_played, points, rebounds, assists "
                "FROM nba.{s}.player_stats(season := {season}) WHERE games_played >= 20 "
                "ORDER BY points DESC LIMIT 10",
            )
        ],
        estimate=_per_league(560, 180, 520),
        key=("player_id",),
    ),
    Spec(
        name="advanced_player_stats",
        title="{league} Advanced Player Statistics",
        description="{league} advanced player ratings by season and season type",
        category="player-stats",
        cols=s.ADVANCED_PLAYER_COLS,
        args=SeasonArgs,
        fetch=lambda league, a: _dashboard(
            "leaguedashplayerstats", league, a.season, a.season_type, "PerGame", "Advanced"
        ),
        doc_llm=(
            "One row per {league} player with offensive, defensive, and net rating, usage, true "
            "shooting, effective field-goal percentage, assist and rebound percentages, pace, PIE, "
            "and possessions."
        ),
        doc_md="Possession-based player ratings. Percentages are 0-1 fractions except turnover_ratio.",
        examples=[
            Example(
                "Highest {season} true-shooting percentage among high-usage players.",
                "SELECT player_name, team_abbreviation, usage_percentage, true_shooting_percentage "
                "FROM nba.{s}.advanced_player_stats(season := {season}) "
                "WHERE usage_percentage >= 0.25 AND games_played >= 20 "
                "ORDER BY true_shooting_percentage DESC LIMIT 10",
            )
        ],
        estimate=_per_league(560, 180, 520),
        key=("player_id",),
    ),
    Spec(
        name="team_stats",
        title="{league} Team Season Statistics",
        description="{league} team season statistics by season, season type, and per mode",
        category="team-stats",
        cols=s.TEAM_STATS_COLS,
        args=DashboardArgs,
        fetch=lambda league, a: _dashboard(
            "leaguedashteamstats", league, a.season, a.season_type, a.per_mode, "Base"
        ),
        doc_llm=(
            "One row per {league} team with record, shooting, rebounding, assists, turnovers, "
            "points, and plus-minus under the chosen per_mode (PerGame by default)."
        ),
        doc_md="League-wide team box-score statistics for a season.",
        examples=[
            Example(
                "Best {season} three-point shooting teams.",
                "SELECT team_name, three_pointers_made, three_point_percentage "
                "FROM nba.{s}.team_stats(season := {season}) ORDER BY three_point_percentage DESC",
            )
        ],
        estimate=TEAMS,
        max_rows=64,
        key=("team_id",),
    ),
    Spec(
        name="advanced_team_stats",
        title="{league} Advanced Team Statistics",
        description="{league} advanced team ratings by season and season type",
        category="team-stats",
        cols=s.ADVANCED_TEAM_COLS,
        args=SeasonArgs,
        fetch=lambda league, a: _dashboard(
            "leaguedashteamstats", league, a.season, a.season_type, "PerGame", "Advanced"
        ),
        doc_llm=(
            "One row per {league} team with offensive, defensive, and net rating, pace, true "
            "shooting, effective field-goal percentage, rebound and turnover rates, and PIE."
        ),
        doc_md="Possession-based team ratings for a season. Percentages are 0-1 fractions.",
        examples=[
            Example(
                "{season} teams ranked by net rating.",
                "SELECT team_name, offensive_rating, defensive_rating, net_rating "
                "FROM nba.{s}.advanced_team_stats(season := {season}) ORDER BY net_rating DESC",
            )
        ],
        estimate=TEAMS,
        max_rows=64,
        key=("team_id",),
    ),
    Spec(
        name="league_leaders",
        title="{league} League Leaders",
        description="Official {league} leaderboard for one statistic",
        category="player-stats",
        cols=s.LEADER_COLS,
        args=LeaderArgs,
        fetch=lambda league, a: fetch_leaders(league, a.season, a.season_type, a.stat_category, a.per_mode),
        doc_llm=(
            "The league's official leaderboard, already ranked and limited to qualified players, "
            "for stat_category such as PTS, REB, AST, STL, BLK, FG_PCT, FG3M, or EFF. Use it for "
            "'who led the league' questions; rank 1 is the leader."
        ),
        doc_md="Official qualified leaderboard. Ties share a rank.",
        examples=[
            Example(
                "{season} assists-per-game leaders.",
                "SELECT rank, player_name, team_abbreviation, assists "
                "FROM nba.{s}.league_leaders(season := {season}, stat_category := 'AST') "
                "ORDER BY rank LIMIT 10",
            )
        ],
        estimate=_per_league(230, 120, 290),
        key=("player_id",),
    ),
    Spec(
        name="league_game_log",
        title="{league} League Game Log",
        description="Every {league} team-game or player-game line of a season",
        category="games",
        cols=s.GAME_LOG_COLS,
        args=GameLogArgs,
        fetch=lambda league, a: fetch_league_game_log(league, a.season, a.season_type, a.player_or_team),
        doc_llm=(
            "Box-score lines for every game of a {league} season: one row per team-game by default, "
            "or per player-game with player_or_team => 'P'. Includes game date, opponent, home/away, "
            "result, and counting stats; good for streaks, head-to-head, and high-scoring games."
        ),
        doc_md="Season-wide game log; each game appears once per team (or once per player).",
        examples=[
            Example(
                "The highest-scoring team games of {season}.",
                "SELECT game_date, team_abbreviation, opponent_abbreviation, points "
                "FROM nba.{s}.league_game_log(season := {season}) ORDER BY points DESC LIMIT 10",
            )
        ],
        estimate=_per_league(2460, 528, 1200),
    ),
    Spec(
        name="player_game_log",
        title="{league} Player Game Log",
        description="One {league} player's game-by-game lines for a season",
        category="player-detail",
        cols=s.PLAYER_GAME_LOG_COLS,
        args=PlayerSeasonArgs,
        fetch=lambda league, a: fetch_player_game_log(league, a.player_id, a.season, a.season_type),
        doc_llm=(
            "One row per game for a {league} player ID and season: date, opponent, home/away, "
            "result, minutes, shooting, rebounds, assists, and points. Find the ID with players()."
        ),
        doc_md="Per-game lines for one player, ordered by game.",
        examples=[
            Example(
                "{player_name}'s {season} games with at least 25 points.",
                "SELECT game_date, matchup, points, rebounds, assists "
                "FROM nba.{s}.player_game_log({player}, season := {season}) WHERE points >= 25 "
                "ORDER BY game_date",
            )
        ],
        estimate=_per_league(70, 40, 40),
        max_rows=120,
        key=("game_id",),
    ),
    Spec(
        name="player_career_stats",
        title="{league} Player Career By Season",
        description="One {league} player's season-by-season career totals",
        category="player-detail",
        cols=s.CAREER_COLS,
        args=CareerArgs,
        fetch=lambda league, a: _career(league, a.player_id, a.season_type, "SeasonTotals"),
        doc_llm=(
            "Season-by-season totals for one {league} player ID. A traded player's season has one "
            "row per team plus a TOT row (is_multi_team_total) summing them; exclude one or the "
            "other before adding seasons. For whole-career sums use player_career_totals()."
        ),
        doc_md="Season totals for one player; multi-team seasons include a TOT row.",
        examples=[
            Example(
                "{player_name}'s season-by-season points.",
                "SELECT season, team_abbreviation, games_played, points "
                "FROM nba.{s}.player_career_stats({player}) WHERE NOT is_multi_team_total "
                "ORDER BY season",
            )
        ],
        estimate=_per_league(10, 8, 4),
        max_rows=60,
    ),
    Spec(
        name="player_career_totals",
        title="{league} Player Career Totals",
        description="One {league} player's whole-career totals",
        category="player-detail",
        cols=s.CAREER_TOTAL_COLS,
        args=CareerArgs,
        fetch=lambda league, a: _career(league, a.player_id, a.season_type, "CareerTotals"),
        doc_llm="One row of whole-career {league} totals for a player ID, regular season or playoffs.",
        doc_md="Career totals for one player.",
        examples=[
            Example(
                "{player_name}'s career regular-season totals.",
                "SELECT games_played, points, rebounds, assists FROM nba.{s}.player_career_totals({player})",
            )
        ],
        estimate=_per_league(1, 1, 1),
        max_rows=1,
    ),
    Spec(
        name="box_score",
        title="{league} Box Score",
        description="Traditional box score of one {league} game, one row per player",
        category="games",
        cols=s.BOX_SCORE_COLS,
        args=GameArgs,
        fetch=lambda league, a: fetch_box_score(league, a.game_id),
        doc_llm=(
            "Player lines for one {league} game ID (text with leading zeros, from scoreboard or "
            "the schedule): starter flag, minutes and exact seconds, shooting, rebounds, assists, "
            "steals, blocks, turnovers, fouls, points, and plus-minus. Sum by team for team totals."
        ),
        doc_md="One row per player who appeared or was listed for the game.",
        examples=[
            Example(
                "Top scorers in game {game}.",
                "SELECT team_tricode, player_name, minutes, points, rebounds, assists "
                "FROM nba.{s}.box_score('{game}') ORDER BY points DESC LIMIT 10",
            )
        ],
        estimate=_per_league(28, 24, 26),
        max_rows=60,
        key=("player_id",),
    ),
    Spec(
        name="play_by_play",
        title="{league} Play-by-Play",
        description="Every play-by-play event of one {league} game",
        category="games",
        cols=s.PLAY_BY_PLAY_COLS,
        args=GameArgs,
        fetch=lambda league, a: fetch_play_by_play(league, a.game_id),
        doc_llm=(
            "Ordered events for one {league} game ID: period, clock and seconds remaining, team, "
            "player, action type, description, running score, and shot result, value, distance, "
            "and coordinates. Order by action_id; a block or steal shares its action_number with the shot or "
            "turnover it ended."
        ),
        doc_md="Every event of a game in sequence, including running score.",
        examples=[
            Example(
                "Made three-pointers in the fourth quarter of game {game}.",
                "SELECT clock, team_tricode, player_name, description, home_score, away_score "
                "FROM nba.{s}.play_by_play('{game}') "
                "WHERE period = 4 AND shot_value = 3 AND shot_result = 'Made' ORDER BY action_id",
            )
        ],
        estimate=_per_league(500, 450, 480),
        max_rows=2000,
        key=("action_id",),
    ),
]


# --------------------------------------------------------------------------- current tables


def _current(
    *,
    table: str,
    title: str,
    category: str,
    cols: list[s.Col],
    fetch: Callable[[League], Rows],
    doc_llm: str,
    doc_md: str,
    example: Example,
    estimate: dict[str, int],
    key: tuple[str, ...],
    keywords: list[str],
    leagues: tuple[str, ...] = ("nba", "wnba", "gleague"),
    max_rows: int | None = None,
) -> Spec:
    return Spec(
        name=f"all_{table}",
        title=title,
        description=f"{title} (scan behind {table})",
        category=category,
        cols=cols,
        args=None,
        fetch=lambda league, _args: fetch(league),
        doc_llm=doc_llm,
        doc_md=doc_md,
        examples=[example],
        estimate=estimate,
        max_rows=max_rows,
        key=key,
        leagues=leagues,
        keywords=keywords,
    )


CURRENT_SPECS: list[Spec] = [
    _current(
        table="current_standings",
        title="Current {league} Standings",
        category="standings",
        cols=s.STANDINGS_COLS,
        fetch=lambda league: fetch_standings(league, None, "Regular Season"),
        doc_llm=(
            "Current {league} regular-season standings, one row per team. Start here for today's "
            "conference races, records, games back, streaks, and clinch flags. Use standings() for "
            "another season."
        ),
        doc_md="The current {league} regular-season standings as an ordinary read-only table.",
        example=Example(
            "Current conference standings in seeding order.",
            "SELECT conference, playoff_rank, team_name, wins, losses, conference_games_back "
            "FROM nba.{s}.current_standings ORDER BY conference, playoff_rank",
        ),
        estimate=TEAMS,
        max_rows=64,
        key=("team_id",),
        keywords=["standings", "records", "games back", "playoff race", "seeding"],
    ),
    _current(
        table="current_scoreboard",
        title="Today's {league} Scoreboard",
        category="games",
        cols=s.SCOREBOARD_COLS,
        fetch=fetch_current_scoreboard,
        doc_llm=(
            "Today's {league} games (US Eastern date) with live status, period, clock, and scores. "
            "Use `scoreboard()` with another day for past or future games."
        ),
        doc_md="Live scores for today's {league} games; refreshed at most every 30 seconds.",
        example=Example(
            "Today's games and scores.",
            "SELECT away_team_tricode, away_score, home_team_tricode, home_score, game_status_text "
            "FROM nba.{s}.current_scoreboard ORDER BY start_time_utc",
        ),
        estimate=_per_league(8, 4, 8),
        max_rows=40,
        key=("game_id",),
        keywords=["scores", "today", "live", "games", "results"],
    ),
    _current(
        table="current_schedule",
        title="Current {league} Schedule",
        category="games",
        cols=s.SCHEDULE_COLS,
        fetch=fetch_schedule,
        doc_llm=(
            "The full published {league} schedule for the current season, one row per game, with "
            "date, tip-off time, arena, status, and final scores for completed games."
        ),
        doc_md="The current-season {league} schedule, preseason through playoffs as published.",
        example=Example(
            "The {team_name} regular-season schedule with results so far.",
            "SELECT game_date, away_team_tricode, away_score, home_team_tricode, home_score, "
            "game_status_text FROM nba.{s}.current_schedule WHERE season_type = 'Regular Season' "
            "AND {team} IN (home_team_id, away_team_id) ORDER BY start_time_utc",
        ),
        estimate=_per_league(1274, 373, 1),
        key=("game_id",),
        keywords=["schedule", "fixtures", "calendar", "upcoming games"],
        leagues=("nba", "wnba"),
    ),
    _current(
        table="current_teams",
        title="Current {league} Teams",
        category="teams",
        cols=s.TEAM_COLS,
        fetch=fetch_teams,
        doc_llm=(
            "Active {league} teams with team_id, abbreviation, city, nickname, first season, and "
            "all-time record and titles. Use it to find a team_id."
        ),
        doc_md="Active {league} teams and their franchise summaries.",
        example=Example(
            "Teams by league championships.",
            "SELECT team_abbreviation, team_city, team_name, league_titles "
            "FROM nba.{s}.current_teams ORDER BY league_titles DESC, team_abbreviation",
        ),
        estimate=TEAMS,
        max_rows=64,
        key=("team_id",),
        keywords=["teams", "franchises", "team id", "abbreviations"],
    ),
    _current(
        table="current_players",
        title="Current {league} Players",
        category="players",
        cols=s.PLAYER_COLS,
        fetch=lambda league: fetch_players(league, True),
        doc_llm=(
            "Players on a current {league} roster with player_id and team. Use it to find a "
            "player_id; players(only_current => false) includes retired players."
        ),
        doc_md="The current {league} player index.",
        example=Example(
            "Rostered players per team.",
            "SELECT team_abbreviation, count(*) AS players FROM nba.{s}.current_players "
            "GROUP BY team_abbreviation ORDER BY team_abbreviation",
        ),
        estimate=_per_league(520, 160, 380),
        key=("player_id",),
        keywords=["players", "rosters", "player id"],
    ),
    _current(
        table="current_player_stats",
        title="{league} Player Averages",
        category="player-stats",
        cols=s.PLAYER_STATS_COLS,
        fetch=lambda league: _dashboard(
            "leaguedashplayerstats", league, None, "Regular Season", "PerGame", "Base"
        ),
        doc_llm=(
            "Per-game regular-season averages for every {league} player this season: points, "
            "rebounds, assists, shooting, steals, blocks, and plus-minus. Use player_stats() for "
            "totals, another season, or the playoffs."
        ),
        doc_md="Current-season per-game player statistics, one row per player.",
        example=Example(
            "Current scoring leaders with at least ten games.",
            "SELECT player_name, team_abbreviation, games_played, points "
            "FROM nba.{s}.current_player_stats WHERE games_played >= 10 ORDER BY points DESC LIMIT 10",
        ),
        estimate=_per_league(560, 180, 520),
        key=("player_id",),
        keywords=["player stats", "points per game", "rebounds", "assists", "scoring leaders"],
    ),
    _current(
        table="current_team_stats",
        title="{league} Team Averages",
        category="team-stats",
        cols=s.TEAM_STATS_COLS,
        fetch=lambda league: _dashboard(
            "leaguedashteamstats", league, None, "Regular Season", "PerGame", "Base"
        ),
        doc_llm=(
            "Per-game regular-season team statistics for the current {league} season. Use "
            "team_stats() for another season or totals and advanced_team_stats() for ratings."
        ),
        doc_md="Current-season per-game team statistics, one row per team.",
        example=Example(
            "Current teams by points per game.",
            "SELECT team_name, wins, losses, points, plus_minus "
            "FROM nba.{s}.current_team_stats ORDER BY points DESC",
        ),
        estimate=TEAMS,
        max_rows=64,
        key=("team_id",),
        keywords=["team stats", "offense", "defense", "points per game"],
    ),
    _current(
        table="franchise_history",
        title="{league} Franchise History",
        category="teams",
        cols=s.FRANCHISE_COLS,
        fetch=fetch_franchise_history,
        doc_llm=(
            "All-time {league} franchise records. row_kind 'franchise' is one all-time summary per "
            "franchise, 'era' rows split it by city and nickname, and 'defunct' rows are folded "
            "franchises. Filter row_kind before aggregating."
        ),
        doc_md="Franchise summaries, name eras, and defunct franchises with all-time records and titles.",
        example=Example(
            "Franchises with the most league titles.",
            "SELECT team_city, team_name, start_year, league_titles FROM nba.{s}.franchise_history "
            "WHERE row_kind = 'franchise' ORDER BY league_titles DESC LIMIT 10",
        ),
        estimate=_per_league(89, 29, 79),
        key=(),
        keywords=["franchise history", "relocations", "championships", "all-time records"],
    ),
]


# --------------------------------------------------------------------------- class generation


def example_sql(spec: Spec, example: Example, league: League) -> str:
    """Render example SQL; a backing scan's examples call the scan instead of its table."""
    sql = render(example.sql, league)
    if spec.args is None:
        table = spec.name.removeprefix("all_")
        sql = sql.replace(f"nba.{league.schema}.{table}", f"nba.{league.schema}.{spec.name}()")
    return sql


def _tags(spec: Spec, league: League) -> dict[str, str]:
    tags = {
        "provider": "nba",
        "vgi.category": spec.category,
        "vgi.title": render(spec.title, league),
        "vgi.result_columns_schema": result_columns_schema(s.schema_of(spec.cols)),
        "vgi.doc_llm": render(spec.doc_llm, league),
        "vgi.doc_md": render(spec.doc_md, league),
        "vgi.example_queries": json.dumps(
            [
                {"description": render(e.description, league), "sql": example_sql(spec, e, league)}
                for e in spec.examples
            ]
        ),
    }
    if spec.keywords:
        tags["vgi.keywords"] = json.dumps(spec.keywords)
    return tags


def _dedupe(rows: list[dict[str, Any]], spec: Spec) -> list[dict[str, Any]]:
    """Drop rows whose declared key is null or repeated, so key attestations hold."""
    if not spec.key:
        return rows
    by_name = {value.name: value for value in spec.cols}
    seen: set[tuple[Any, ...]] = set()
    kept = []
    for row in rows:
        key = tuple(by_name[name].get(row) for name in spec.key)
        if any(value is None for value in key) or key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def build_function(spec: Spec, league: League) -> type:
    """Create the concrete table-function class for one spec and league."""
    schema = s.schema_of(spec.cols)
    estimate = spec.estimate[league.schema]
    meta = type(
        "Meta",
        (),
        {
            "__doc__": "Function discovery metadata.",
            "name": spec.name,
            "description": render(spec.description, league),
            "categories": ["basketball", league.schema, spec.category],
            "projection_pushdown": True,
            "tags": _tags(spec, league),
            "examples": [
                FunctionExample(
                    sql=example_sql(spec, e, league), description=render(e.description, league).rstrip(".")
                )
                for e in spec.examples
            ],
        },
    )

    def cardinality(cls: type, params: object) -> TableCardinality:
        return TableCardinality(estimate=estimate, max=spec.max_rows)

    def process(cls: type, params: ProcessParams[Any], state: None, out: OutputCollector) -> None:
        rows, ttl = spec.fetch(league, params.args)
        out.emit(
            s.batch(_dedupe(rows, spec), spec.cols, params.output_schema),
            cache_control=CacheControl(ttl=max(1, ttl)),
        )
        out.finish()

    namespace: dict[str, Any] = {
        "__doc__": render(spec.description, league) + ".",
        "__module__": __name__,
        "FIXED_SCHEMA": schema,
        "SPEC": spec,
        "LEAGUE": league,
        "Meta": meta,
        "cardinality": classmethod(cardinality),
        "process": classmethod(process),
    }
    base = TableFunctionGenerator[spec.args if spec.args is not None else None]
    class_name = "".join(part.title() for part in f"{league.schema}_{spec.name}".split("_")) + "Function"
    cls = types.new_class(class_name, (base,), {}, lambda ns: ns.update(namespace))
    return init_single_worker(bind_fixed_schema(cls))


@dataclass(frozen=True)
class LeagueSurface:
    """The generated functions and current tables of one league."""

    league: League
    functions: list[type]
    tables: list[tuple[Spec, type]]


def build_league(league: League) -> LeagueSurface:
    """Generate every function and current-table scan for a league."""
    functions = [build_function(spec, league) for spec in SPECS if league.schema in spec.leagues]
    tables = [(spec, build_function(spec, league)) for spec in CURRENT_SPECS if league.schema in spec.leagues]
    return LeagueSurface(league, [*functions, *(cls for _spec, cls in tables)], tables)


SURFACES: dict[str, LeagueSurface] = {league.schema: build_league(league) for league in (NBA, WNBA, GLEAGUE)}
