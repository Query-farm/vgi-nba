"""Typed Arrow schemas and response flattening for every NBA-family result.

Each schema is declared as a list of :class:`Col` specs: the SQL column name,
its Arrow type, its comment, and how to read the value from one upstream
record. :func:`batch` turns flattened records into a record batch in the
declared column order.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pyarrow as pa

from vgi_nba.meta import field

INT = pa.int32()
BIG = pa.int64()
FLOAT = pa.float64()
TEXT = pa.string()
BOOL = pa.bool_()
DATE = pa.date32()
TIMESTAMP = pa.timestamp("us", tz="UTC")
WIN_PCT = pa.decimal128(4, 3)
GAMES_BACK = pa.decimal128(5, 1)

Getter = Callable[[Mapping[str, Any]], Any]


# --------------------------------------------------------------------------- parsing


def to_int(value: Any) -> int | None:
    """Parse an integer-like API value without treating booleans as integers."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(float(value)) if isinstance(value, str) and "." in value else int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def to_float(value: Any) -> float | None:
    """Parse a finite floating-point API value."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def to_decimal(value: Any) -> Decimal | None:
    """Parse a finite decimal, returning null for display markers."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def to_text(value: Any) -> str | None:
    """Return a stripped string, mapping empty strings to null."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_bool(value: Any) -> bool | None:
    """Parse the API's 0/1, Y/N, and true/false flags."""
    if isinstance(value, bool):
        return value
    if value in (1, "1", "Y", "y", "true", "True"):
        return True
    if value in (0, "0", "N", "n", "false", "False"):
        return False
    return None


def to_id(value: Any) -> int | None:
    """Parse an identifier, mapping the API's 0 placeholder to null."""
    result = to_int(value)
    return result if result else None


_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y")


def to_date(value: Any) -> date | None:
    """Parse the API's several date spellings (ISO, 'Apr 12, 2026', 'MAY 09, 2003')."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    head = text[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head):
        try:
            return date.fromisoformat(head)
        except ValueError:
            return None
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", head):
        text = head
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text.title() if "%b" in pattern else text, pattern).date()
        except ValueError:
            continue
    return None


def to_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def minutes_to_seconds(value: Any) -> int | None:
    """Convert a 'MM:SS' minutes display, as box scores publish it, into seconds."""
    if not isinstance(value, str) or ":" not in value:
        return None
    minutes, _, seconds = value.partition(":")
    try:
        return int(minutes) * 60 + round(float(seconds))
    except ValueError:
        return None


_CLOCK = re.compile(r"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")


def clock_to_seconds(value: Any) -> Decimal | None:
    """Convert an ISO-8601 game clock ('PT11M42.00S') to seconds remaining."""
    if not isinstance(value, str):
        return None
    match = _CLOCK.fullmatch(value.strip())
    if not match or not any(match.groups()):
        return None
    minutes = int(match.group(1) or 0)
    seconds = Decimal(match.group(2) or "0")
    return (minutes * 60 + seconds).quantize(Decimal("0.01"))


def height_to_inches(value: Any) -> int | None:
    """Convert a '6-11' height display into total inches."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+)-(\d+)\s*", value)
    return int(match.group(1)) * 12 + int(match.group(2)) if match else None


def experience_years(value: Any) -> int | None:
    """Parse roster experience, where 'R' marks a rookie with zero prior seasons."""
    if isinstance(value, str) and value.strip().upper() == "R":
        return 0
    return to_int(value)


def season_type_of_game(game_id: Any) -> str | None:
    """Derive the season type from the game ID.

    The first digit is the league (0 NBA, 1 WNBA, 2 G League) and the third
    the season type, so 0022600201 is an NBA regular-season game and
    1042500001 a WNBA playoff game.
    """
    if not isinstance(game_id, str) or len(game_id) < 3:
        return None
    return {
        "1": "Pre Season",
        "2": "Regular Season",
        "3": "All Star",
        "4": "Playoffs",
        "5": "PlayIn",
        "6": "NBA Cup Final",
    }.get(game_id[2])


def _matchup_parts(matchup: Any) -> tuple[str | None, str | None, bool | None]:
    """Split 'LAL vs. GSW' / 'LAL @ GSW' into (team, opponent, is_home)."""
    if not isinstance(matchup, str):
        return None, None, None
    if " vs. " in matchup:
        team, opponent = matchup.split(" vs. ", 1)
        return team.strip(), opponent.strip(), True
    if " @ " in matchup:
        team, opponent = matchup.split(" @ ", 1)
        return team.strip(), opponent.strip(), False
    return None, None, None


# --------------------------------------------------------------------------- column specs


@dataclass(frozen=True, slots=True)
class Col:
    """One output column and how to read it from a flattened upstream record."""

    name: str
    kind: pa.DataType
    comment: str
    get: Getter

    def field(self) -> pa.Field:
        """Return the Arrow field carrying this column's comment."""
        return field(self.name, self.kind, self.comment)


def _reader(kind: pa.DataType) -> Callable[[Any], Any]:
    if kind in (INT, BIG):
        return to_int
    if kind == FLOAT:
        return to_float
    if kind == BOOL:
        return to_bool
    if kind == DATE:
        return to_date
    if kind == TIMESTAMP:
        return to_timestamp
    if pa.types.is_decimal(kind):
        quantum = Decimal(1).scaleb(-kind.scale)
        return lambda value: _quantize(to_decimal(value), quantum)
    return to_text


def _quantize(value: Decimal | None, quantum: Decimal) -> Decimal | None:
    return None if value is None else value.quantize(quantum)


def col(name: str, kind: pa.DataType, comment: str, source: str | Getter | None = None) -> Col:
    """Declare a column read from ``source`` (an upstream key or a getter)."""
    if callable(source):
        return Col(name, kind, comment, source)
    key = source or name
    parse = _reader(kind)
    return Col(name, kind, comment, lambda record: parse(record.get(key)))


def stamp(name: str, kind: pa.DataType, comment: str) -> Col:
    """Declare a column the worker fills from the request rather than the payload."""
    return Col(name, kind, comment, lambda record: record.get(f"__{name}"))


def schema_of(cols: Sequence[Col]) -> pa.Schema:
    """Build the Arrow schema for a column-spec list."""
    return pa.schema([value.field() for value in cols])


def batch(records: Iterable[Mapping[str, Any]], cols: Sequence[Col], schema: pa.Schema) -> pa.RecordBatch:
    """Flatten ``records`` through ``cols`` and build a batch in ``schema``'s column order.

    ``schema`` may be a projection of the full column list.
    """
    by_name = {value.name: value for value in cols}
    rows = list(records)
    arrays = [
        pa.array([by_name[target.name].get(row) for row in rows], type=target.type) for target in schema
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


def with_stamps(records: Iterable[Mapping[str, Any]], **values: Any) -> list[dict[str, Any]]:
    """Copy records, adding request-derived values for :func:`stamp` columns."""
    extra = {f"__{key}": value for key, value in values.items()}
    return [{**record, **extra} for record in records]


def _shooting(kind: pa.DataType, scope: str = " (count)") -> list[Col]:
    """The shared box-score stat block, as counts or per-mode values."""
    return [
        col("field_goals_made", kind, f"Field goals made{scope}.", "FGM"),
        col("field_goals_attempted", kind, f"Field goals attempted{scope}.", "FGA"),
        col("field_goal_percentage", FLOAT, "Field-goal percentage (0-1 ratio).", "FG_PCT"),
        col("three_pointers_made", kind, f"Three-point field goals made{scope}.", "FG3M"),
        col("three_pointers_attempted", kind, f"Three-point field goals attempted{scope}.", "FG3A"),
        col("three_point_percentage", FLOAT, "Three-point percentage (0-1 ratio).", "FG3_PCT"),
        col("free_throws_made", kind, f"Free throws made{scope}.", "FTM"),
        col("free_throws_attempted", kind, f"Free throws attempted{scope}.", "FTA"),
        col("free_throw_percentage", FLOAT, "Free-throw percentage (0-1 ratio).", "FT_PCT"),
        col("offensive_rebounds", kind, f"Offensive rebounds{scope}.", "OREB"),
        col("defensive_rebounds", kind, f"Defensive rebounds{scope}.", "DREB"),
        col("rebounds", kind, f"Total rebounds{scope}.", "REB"),
        col("assists", kind, f"Assists{scope}.", "AST"),
        col("steals", kind, f"Steals{scope}.", "STL"),
        col("blocks", kind, f"Blocked shots{scope}.", "BLK"),
        col("turnovers", kind, f"Turnovers{scope}.", "TOV"),
        col("personal_fouls", kind, f"Personal fouls{scope}.", "PF"),
        col("points", kind, f"Points{scope}.", "PTS"),
    ]


# --------------------------------------------------------------------------- standings


def _streak(record: Mapping[str, Any]) -> int | None:
    return to_int(record.get("CurrentStreak"))


def _clinch(record: Mapping[str, Any]) -> str | None:
    text = to_text(record.get("ClinchIndicator"))
    return text.lstrip("- ").strip() or None if text else None


STANDINGS_COLS = [
    stamp("season", TEXT, "Season key: 2025-26 (NBA, G League) or 2025 (WNBA)."),
    stamp("season_type", TEXT, "Season type requested, such as Regular Season."),
    col("team_id", BIG, "Stable NBA Stats team identifier (numeric ID).", "TeamID"),
    col("team_city", TEXT, "Team city or region.", "TeamCity"),
    col("team_name", TEXT, "Team nickname, such as Lakers.", "TeamName"),
    col("conference", TEXT, "Conference, East or West.", "Conference"),
    col("division", TEXT, "Division name; NULL for leagues without divisions.", "Division"),
    col("playoff_rank", INT, "Rank within the conference (the playoff seeding order).", "PlayoffRank"),
    col(
        "division_rank",
        INT,
        "Rank within the division (1 is first); NULL without divisions.",
        lambda r: to_int(r.get("DivisionRank")) or None,
    ),
    col("league_rank", INT, "Rank across the whole league (1 is first), when published.", "LeagueRank"),
    col("wins", INT, "Wins (count).", "WINS"),
    col("losses", INT, "Losses (count).", "LOSSES"),
    col("win_percentage", WIN_PCT, "Winning percentage as an exact decimal (0-1 ratio).", "WinPCT"),
    col("record", TEXT, "Win-loss record display, such as 10-2.", "Record"),
    col(
        "conference_games_back",
        GAMES_BACK,
        "Games behind the conference leader (in games).",
        "ConferenceGamesBack",
    ),
    col(
        "division_games_back",
        GAMES_BACK,
        "Games behind the division leader (in games).",
        "DivisionGamesBack",
    ),
    col(
        "league_games_back",
        GAMES_BACK,
        "Games behind the league's best record (in games).",
        "LeagueGamesBack",
    ),
    col("home_record", TEXT, "Home win-loss record.", "HOME"),
    col("road_record", TEXT, "Road win-loss record.", "ROAD"),
    col("last_ten_record", TEXT, "Win-loss record over the last ten games.", "L10"),
    col("conference_record", TEXT, "Record against conference opponents.", "ConferenceRecord"),
    col("division_record", TEXT, "Record against division opponents.", "DivisionRecord"),
    col("vs_east_record", TEXT, "Record against Eastern Conference teams.", "vsEast"),
    col("vs_west_record", TEXT, "Record against Western Conference teams.", "vsWest"),
    col(
        "current_streak",
        INT,
        "Current streak length in games (positive for wins, negative for losses).",
        _streak,
    ),
    col("current_streak_display", TEXT, "Current streak display, such as W 6 or L 2.", "strCurrentStreak"),
    col("longest_win_streak", INT, "Longest winning streak this season (count of games).", "LongWinStreak"),
    col("longest_loss_streak", INT, "Longest losing streak this season (count of games).", "LongLossStreak"),
    col("points_per_game", FLOAT, "Points scored per game.", "PointsPG"),
    col("opponent_points_per_game", FLOAT, "Points allowed per game.", "OppPointsPG"),
    col("point_differential_per_game", FLOAT, "Average scoring margin per game.", "DiffPointsPG"),
    col("total_points", INT, "Total points scored in the season (count).", "TotalPoints"),
    col("opponent_total_points", INT, "Total points allowed in the season (count).", "OppTotalPoints"),
    col("clinch_indicator", TEXT, "Clinch marker as published, such as x, y, z, pi, or o.", _clinch),
    col(
        "clinched_playoff_berth",
        BOOL,
        "Whether the team has clinched a playoff berth.",
        "ClinchedPlayoffBirth",
    ),
    col(
        "clinched_play_in", BOOL, "Whether the team has clinched at least a play-in place.", "ClinchedPlayIn"
    ),
    col("clinched_division", BOOL, "Whether the team has clinched its division.", "ClinchedDivisionTitle"),
    col(
        "clinched_conference",
        BOOL,
        "Whether the team has clinched its conference.",
        "ClinchedConferenceTitle",
    ),
    col(
        "eliminated_from_playoffs",
        BOOL,
        "Whether the team is eliminated from its conference race.",
        "EliminatedConference",
    ),
]
STANDINGS_SCHEMA = schema_of(STANDINGS_COLS)


# --------------------------------------------------------------------------- scoreboard


def _side(record: Mapping[str, Any], side: str, key: str) -> Any:
    team = record.get(side)
    return team.get(key) if isinstance(team, Mapping) else None


def _team_col(side: str, prefix: str, label: str) -> list[Col]:
    def read(key: str, parse: Callable[[Any], Any]) -> Getter:
        return lambda record: parse(_side(record, side, key))

    return [
        col(f"{prefix}_team_id", BIG, f"{label} team identifier (numeric ID).", read("teamId", to_id)),
        col(f"{prefix}_team_tricode", TEXT, f"{label} team three-letter code.", read("teamTricode", to_text)),
        col(f"{prefix}_team_city", TEXT, f"{label} team city.", read("teamCity", to_text)),
        col(f"{prefix}_team_name", TEXT, f"{label} team nickname.", read("teamName", to_text)),
        col(
            f"{prefix}_wins",
            INT,
            f"{label} team's season wins (count) as of this game.",
            read("wins", to_int),
        ),
        col(
            f"{prefix}_losses",
            INT,
            f"{label} team's season losses (count) as of this game.",
            read("losses", to_int),
        ),
        col(f"{prefix}_score", INT, f"{label} team's points; 0 before tip-off.", read("score", to_int)),
    ]


SCOREBOARD_COLS = [
    stamp("game_date", DATE, "Scoreboard date in US Eastern time."),
    col("game_id", TEXT, "Ten-character NBA Stats game identifier; keep it as text.", "gameId"),
    col("game_code", TEXT, "Game code, date plus away and home tricodes.", "gameCode"),
    col(
        "season_type",
        TEXT,
        "Season type derived from the game ID prefix.",
        lambda r: season_type_of_game(r.get("gameId")),
    ),
    col("game_status", INT, "Game status: 1 scheduled, 2 in progress, 3 final.", "gameStatus"),
    col(
        "game_status_text",
        TEXT,
        "Status display, such as Final, Halftime, or a tip-off time.",
        "gameStatusText",
    ),
    col("period", INT, "Current or final period; 0 before tip-off.", "period"),
    col("game_clock", TEXT, "ISO-8601 time remaining in the current period, when live.", "gameClock"),
    col("start_time_utc", TIMESTAMP, "Scheduled tip-off time in UTC.", "gameTimeUTC"),
    col("series_text", TEXT, "Playoff series status text, when applicable.", "seriesText"),
    col("game_label", TEXT, "Game label, such as an NBA Cup or playoff round name.", "gameLabel"),
    col("is_neutral_site", BOOL, "Whether the game is played at a neutral site.", "isNeutral"),
    *_team_col("homeTeam", "home", "Home"),
    *_team_col("awayTeam", "away", "Away"),
]
SCOREBOARD_SCHEMA = schema_of(SCOREBOARD_COLS)


def scoreboard_records(payload: Mapping[str, Any]) -> tuple[date | None, list[dict[str, Any]]]:
    """Return the scoreboard date and its game objects."""
    board = payload.get("scoreboard")
    if not isinstance(board, Mapping):
        raise ValueError("scoreboard response has no scoreboard object")
    games = board.get("games")
    if not isinstance(games, list):
        raise ValueError("scoreboard response has no games array")
    return to_date(board.get("gameDate")), [game for game in games if isinstance(game, Mapping)]


# --------------------------------------------------------------------------- schedule

SCHEDULE_COLS = [
    stamp("season", TEXT, "Season key of the published schedule."),
    col("game_id", TEXT, "Ten-character NBA Stats game identifier; keep it as text.", "gameId"),
    col("game_code", TEXT, "Game code, date plus away and home tricodes.", "gameCode"),
    col(
        "season_type",
        TEXT,
        "Season type derived from the game ID prefix.",
        lambda r: season_type_of_game(r.get("gameId")),
    ),
    col("game_date", DATE, "Game date in US Eastern time.", "gameDateEst"),
    col("start_time_utc", TIMESTAMP, "Scheduled tip-off time in UTC.", "gameDateTimeUTC"),
    col("game_status", INT, "Game status: 1 scheduled, 2 in progress, 3 final.", "gameStatus"),
    col("game_status_text", TEXT, "Status display, such as Final or a tip-off time.", "gameStatusText"),
    col("week_number", INT, "Schedule week number (0 for preseason).", "weekNumber"),
    col("game_label", TEXT, "Game label, such as Preseason or an NBA Cup round.", "gameLabel"),
    col("game_sub_label", TEXT, "Secondary game label, such as a special-event name.", "gameSubLabel"),
    col("arena_name", TEXT, "Arena name.", "arenaName"),
    col("arena_city", TEXT, "Arena city.", "arenaCity"),
    col("arena_state", TEXT, "Arena state or province code.", "arenaState"),
    col("is_neutral_site", BOOL, "Whether the game is played at a neutral site.", "isNeutral"),
    col(
        "is_postponed",
        BOOL,
        "Whether the league marks the game postponed.",
        lambda r: to_bool(r.get("postponedStatus")) if r.get("postponedStatus") in ("Y", "N") else None,
    ),
    *_team_col("homeTeam", "home", "Home"),
    *_team_col("awayTeam", "away", "Away"),
]
SCHEDULE_SCHEMA = schema_of(SCHEDULE_COLS)


def schedule_records(payload: Mapping[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Return the schedule's season key and every game across its game dates."""
    schedule = payload.get("leagueSchedule")
    if not isinstance(schedule, Mapping) or not isinstance(schedule.get("gameDates"), list):
        raise ValueError("schedule response has no leagueSchedule.gameDates array")
    games: list[dict[str, Any]] = []
    for day in schedule["gameDates"]:
        if isinstance(day, Mapping) and isinstance(day.get("games"), list):
            games.extend(game for game in day["games"] if isinstance(game, Mapping))
    return to_text(schedule.get("seasonYear")), games


# --------------------------------------------------------------------------- teams

TEAM_COLS = [
    col("team_id", BIG, "Stable NBA Stats team identifier (numeric ID).", "TEAM_ID"),
    col("team_abbreviation", TEXT, "Team three-letter abbreviation.", "ABBREVIATION"),
    col("team_city", TEXT, "Team city or region.", "TEAM_CITY"),
    col("team_name", TEXT, "Team nickname.", "TEAM_NAME"),
    col("first_season", INT, "Start year of the franchise's first season.", "MIN_YEAR"),
    col("last_season", INT, "Start year of the franchise's most recent season.", "MAX_YEAR"),
    col("seasons", INT, "Number of seasons played by the franchise.", "YEARS"),
    col("games", INT, "All-time regular-season games played (count).", "GAMES"),
    col("wins", INT, "All-time regular-season wins (count).", "WINS"),
    col("losses", INT, "All-time regular-season losses (count).", "LOSSES"),
    col("win_percentage", WIN_PCT, "All-time regular-season winning percentage (0-1 ratio).", "WIN_PCT"),
    col("playoff_appearances", INT, "Playoff appearances (count).", "PO_APPEARANCES"),
    col("division_titles", INT, "Division titles (count).", "DIV_TITLES"),
    col("conference_titles", INT, "Conference titles (count).", "CONF_TITLES"),
    col("league_titles", INT, "League championships (count).", "LEAGUE_TITLES"),
]
TEAM_SCHEMA = schema_of(TEAM_COLS)


def team_records(
    team_years: Sequence[Mapping[str, Any]], franchises: Sequence[Mapping[str, Any]]
) -> list[dict]:
    """Join active team-years rows with each franchise's all-time summary row.

    A team is active when its last season equals the newest last season in the
    league. The first FranchiseHistory row for a team is its all-time summary.
    """
    summaries: dict[Any, Mapping[str, Any]] = {}
    for row in franchises:
        summaries.setdefault(row.get("TEAM_ID"), row)
    latest = max((to_int(row.get("MAX_YEAR")) or 0 for row in team_years), default=0)
    teams = []
    for row in team_years:
        if to_int(row.get("MAX_YEAR")) != latest or not to_id(row.get("TEAM_ID")):
            continue
        summary = summaries.get(row.get("TEAM_ID"), {})
        teams.append({**summary, **row})
    return sorted(teams, key=lambda value: (str(value.get("ABBREVIATION")), value.get("TEAM_ID")))


FRANCHISE_COLS = [
    col("team_id", BIG, "Stable NBA Stats team identifier shared by every era of the franchise.", "TEAM_ID"),
    col(
        "row_kind",
        TEXT,
        "franchise for the all-time summary, era for one city/name era, defunct for a folded franchise.",
        "__row_kind",
    ),
    col("team_city", TEXT, "City or region in this row's era.", "TEAM_CITY"),
    col("team_name", TEXT, "Nickname in this row's era.", "TEAM_NAME"),
    col("start_year", INT, "Start year of the first season covered by this row.", "START_YEAR"),
    col("end_year", INT, "Start year of the last season covered by this row.", "END_YEAR"),
    col("seasons", INT, "Number of seasons covered by this row.", "YEARS"),
    col("games", INT, "Regular-season games (count).", "GAMES"),
    col("wins", INT, "Regular-season wins (count).", "WINS"),
    col("losses", INT, "Regular-season losses (count).", "LOSSES"),
    col("win_percentage", WIN_PCT, "Regular-season winning percentage (0-1 ratio).", "WIN_PCT"),
    col("playoff_appearances", INT, "Playoff appearances (count).", "PO_APPEARANCES"),
    col("division_titles", INT, "Division titles (count).", "DIV_TITLES"),
    col("conference_titles", INT, "Conference titles (count).", "CONF_TITLES"),
    col("league_titles", INT, "League championships (count).", "LEAGUE_TITLES"),
]
FRANCHISE_SCHEMA = schema_of(FRANCHISE_COLS)


def franchise_records(
    history: Sequence[Mapping[str, Any]], defunct: Sequence[Mapping[str, Any]]
) -> list[dict]:
    """Label franchise-summary, era, and defunct rows."""
    seen: set[Any] = set()
    rows: list[dict] = []
    for row in history:
        kind = "era" if row.get("TEAM_ID") in seen else "franchise"
        seen.add(row.get("TEAM_ID"))
        rows.append({**row, "__row_kind": kind})
    rows.extend({**row, "__row_kind": "defunct"} for row in defunct)
    return rows


# --------------------------------------------------------------------------- players and rosters

PLAYER_COLS = [
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PERSON_ID"),
    col("player_name", TEXT, "Player name, first name first.", "DISPLAY_FIRST_LAST"),
    col("last_comma_first", TEXT, "Player name, last name first.", "DISPLAY_LAST_COMMA_FIRST"),
    col("is_active", BOOL, "Whether the player is currently on a roster.", "ROSTERSTATUS"),
    col("from_year", INT, "Start year of the player's first season in the league.", "FROM_YEAR"),
    col("to_year", INT, "Start year of the player's most recent season in the league.", "TO_YEAR"),
    col(
        "team_id",
        BIG,
        "Current or last team identifier; NULL when unattached.",
        lambda r: to_id(r.get("TEAM_ID")),
    ),
    col("team_abbreviation", TEXT, "Current or last team abbreviation.", "TEAM_ABBREVIATION"),
    col("team_city", TEXT, "Current or last team city.", "TEAM_CITY"),
    col("team_name", TEXT, "Current or last team nickname.", "TEAM_NAME"),
    col("player_slug", TEXT, "URL slug of the player.", "PLAYER_SLUG"),
    col("has_played", BOOL, "Whether the player has appeared in a game.", "GAMES_PLAYED_FLAG"),
]
PLAYER_SCHEMA = schema_of(PLAYER_COLS)

ROSTER_COLS = [
    stamp("season", TEXT, "Season key of the roster."),
    col("team_id", BIG, "Team identifier (numeric ID).", "TeamID"),
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PLAYER_ID"),
    col("player_name", TEXT, "Player name.", "PLAYER"),
    col("jersey_number", TEXT, "Jersey number as text; NULL until assigned.", "NUM"),
    col("position", TEXT, "Listed position, such as G, F-C, or C.", "POSITION"),
    col("height", TEXT, "Listed height display in feet-inches, such as 6-11.", "HEIGHT"),
    col("height_inches", INT, "Listed height in inches.", lambda r: height_to_inches(r.get("HEIGHT"))),
    col("weight_pounds", INT, "Listed weight in pounds.", "WEIGHT"),
    col("birth_date", DATE, "Date of birth.", "BIRTH_DATE"),
    col("age", FLOAT, "Age in years as published by the roster.", "AGE"),
    col(
        "experience_years",
        INT,
        "Prior seasons of league experience in years; 0 for rookies.",
        lambda r: experience_years(r.get("EXP")),
    ),
    col("school", TEXT, "College or last pre-league affiliation.", "SCHOOL"),
    col("how_acquired", TEXT, "How the team acquired the player, when published.", "HOW_ACQUIRED"),
]
ROSTER_SCHEMA = schema_of(ROSTER_COLS)


# --------------------------------------------------------------------------- season dashboards

_SEASON_STAMPS = [
    stamp("season", TEXT, "Season key: 2025-26 (NBA, G League) or 2025 (WNBA)."),
    stamp("season_type", TEXT, "Season type requested, such as Regular Season or Playoffs."),
]

PLAYER_STATS_COLS = [
    *_SEASON_STAMPS,
    stamp("per_mode", TEXT, "Aggregation requested: Totals, PerGame, Per36, or Per100Possessions."),
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PLAYER_ID"),
    col("player_name", TEXT, "Player name.", "PLAYER_NAME"),
    col("team_id", BIG, "Current or last team identifier (numeric ID).", "TEAM_ID"),
    col("team_abbreviation", TEXT, "Current or last team abbreviation.", "TEAM_ABBREVIATION"),
    col(
        "team_count",
        INT,
        "Number of teams the player appeared for; above 1 means totals span teams.",
        "TEAM_COUNT",
    ),
    col("age", FLOAT, "Player age in years during the season.", "AGE"),
    col("games_played", INT, "Games played (count).", "GP"),
    col("wins", INT, "Team wins in games the player played.", "W"),
    col("losses", INT, "Team losses in games the player played.", "L"),
    col("minutes", FLOAT, "Minutes played under the requested per_mode.", "MIN"),
    *_shooting(FLOAT, " (count, scaled per per_mode)"),
    col(
        "blocks_against",
        FLOAT,
        "Shots of this player blocked by opponents (count, scaled per per_mode).",
        "BLKA",
    ),
    col("fouls_drawn", FLOAT, "Personal fouls drawn (count, scaled per per_mode).", "PFD"),
    col("plus_minus", FLOAT, "Point differential while on the floor (points).", "PLUS_MINUS"),
    col("double_doubles", INT, "Games with double figures in two categories.", "DD2"),
    col("triple_doubles", INT, "Games with double figures in three categories.", "TD3"),
]
PLAYER_STATS_SCHEMA = schema_of(PLAYER_STATS_COLS)

TEAM_STATS_COLS = [
    *_SEASON_STAMPS,
    stamp("per_mode", TEXT, "Aggregation requested: Totals, PerGame, Per36, or Per100Possessions."),
    col("team_id", BIG, "Stable NBA Stats team identifier (numeric ID).", "TEAM_ID"),
    col("team_name", TEXT, "Full team name.", "TEAM_NAME"),
    col("games_played", INT, "Games played (count).", "GP"),
    col("wins", INT, "Wins (count).", "W"),
    col("losses", INT, "Losses (count).", "L"),
    col("win_percentage", WIN_PCT, "Winning percentage (0-1 ratio).", "W_PCT"),
    col("minutes", FLOAT, "Team minutes under the requested per_mode.", "MIN"),
    *_shooting(FLOAT, " (count, scaled per per_mode)"),
    col("blocks_against", FLOAT, "Team shots blocked by opponents.", "BLKA"),
    col("fouls_drawn", FLOAT, "Personal fouls drawn (count, scaled per per_mode).", "PFD"),
    col("plus_minus", FLOAT, "Point differential.", "PLUS_MINUS"),
]
TEAM_STATS_SCHEMA = schema_of(TEAM_STATS_COLS)

_ADVANCED = [
    col("offensive_rating", FLOAT, "Points produced per 100 possessions.", "OFF_RATING"),
    col("defensive_rating", FLOAT, "Points allowed per 100 possessions.", "DEF_RATING"),
    col("net_rating", FLOAT, "Offensive minus defensive rating (points per 100 possessions).", "NET_RATING"),
    col("assist_percentage", FLOAT, "Share of teammate field goals assisted (0-1 ratio).", "AST_PCT"),
    col("assist_to_turnover", FLOAT, "Assists per turnover.", "AST_TO"),
    col("assist_ratio", FLOAT, "Assists per 100 possessions used.", "AST_RATIO"),
    col(
        "offensive_rebound_percentage",
        FLOAT,
        "Share of available offensive rebounds (0-1 ratio).",
        "OREB_PCT",
    ),
    col(
        "defensive_rebound_percentage",
        FLOAT,
        "Share of available defensive rebounds (0-1 ratio).",
        "DREB_PCT",
    ),
    col("rebound_percentage", FLOAT, "Share of available rebounds (0-1 ratio).", "REB_PCT"),
    col("effective_field_goal_percentage", FLOAT, "Effective field-goal percentage (0-1 ratio).", "EFG_PCT"),
    col("true_shooting_percentage", FLOAT, "True shooting percentage (0-1 ratio).", "TS_PCT"),
    col("pace", FLOAT, "Possessions per 48 minutes.", "PACE"),
    col(
        "player_impact_estimate",
        FLOAT,
        "NBA Player Impact Estimate, share of game events (0-1 ratio).",
        "PIE",
    ),
    col("possessions", INT, "Possessions played (count).", "POSS"),
]

ADVANCED_PLAYER_COLS = [
    *_SEASON_STAMPS,
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PLAYER_ID"),
    col("player_name", TEXT, "Player name.", "PLAYER_NAME"),
    col("team_id", BIG, "Current or last team identifier (numeric ID).", "TEAM_ID"),
    col("team_abbreviation", TEXT, "Current or last team abbreviation.", "TEAM_ABBREVIATION"),
    col("age", FLOAT, "Player age in years during the season.", "AGE"),
    col("games_played", INT, "Games played (count).", "GP"),
    col("minutes_per_game", FLOAT, "Minutes per game.", "MIN"),
    *_ADVANCED,
    col(
        "usage_percentage", FLOAT, "Share of team possessions used while on the floor (0-1 ratio).", "USG_PCT"
    ),
    col("turnover_ratio", FLOAT, "Turnovers per 100 plays used, on a 0-100 scale.", "TM_TOV_PCT"),
]
ADVANCED_PLAYER_SCHEMA = schema_of(ADVANCED_PLAYER_COLS)

ADVANCED_TEAM_COLS = [
    *_SEASON_STAMPS,
    col("team_id", BIG, "Stable NBA Stats team identifier (numeric ID).", "TEAM_ID"),
    col("team_name", TEXT, "Full team name.", "TEAM_NAME"),
    col("games_played", INT, "Games played (count).", "GP"),
    col("wins", INT, "Wins (count).", "W"),
    col("losses", INT, "Losses (count).", "L"),
    *_ADVANCED,
    col("turnover_percentage", FLOAT, "Turnovers per possession (0-1 ratio).", "TM_TOV_PCT"),
]
ADVANCED_TEAM_SCHEMA = schema_of(ADVANCED_TEAM_COLS)

LEADER_COLS = [
    *_SEASON_STAMPS,
    stamp("stat_category", TEXT, "Statistic the leaderboard is ranked by, such as PTS."),
    stamp("per_mode", TEXT, "Aggregation requested: Totals, PerGame, or Per48."),
    col("rank", INT, "Leaderboard rank (1 is the leader); ties share a rank.", "RANK"),
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PLAYER_ID"),
    col("player_name", TEXT, "Player name.", "PLAYER"),
    col("team_id", BIG, "Current or last team identifier (numeric ID).", "TEAM_ID"),
    col("team_abbreviation", TEXT, "Current or last team abbreviation.", "TEAM"),
    col("games_played", INT, "Games played (count).", "GP"),
    col("minutes", FLOAT, "Minutes under the requested per_mode.", "MIN"),
    # leagueleaders publishes no personal-foul column.
    *(c for c in _shooting(FLOAT, " (count, scaled per per_mode)") if c.name != "personal_fouls"),
    col("efficiency", FLOAT, "NBA efficiency rating (EFF index).", "EFF"),
]
LEADER_SCHEMA = schema_of(LEADER_COLS)


# --------------------------------------------------------------------------- game logs


def _matchup(index: int) -> Getter:
    return lambda record: _matchup_parts(record.get("MATCHUP"))[index]


GAME_LOG_COLS = [
    stamp("season", TEXT, "Season key: 2025-26 (NBA, G League) or 2025 (WNBA)."),
    col(
        "game_id",
        TEXT,
        "Ten-character NBA Stats game identifier; keep it as text.",
        lambda r: to_text(r.get("GAME_ID", r.get("Game_ID"))),
    ),
    col("game_date", DATE, "Game date.", "GAME_DATE"),
    col(
        "season_type",
        TEXT,
        "Season type derived from the game ID prefix.",
        lambda r: season_type_of_game(r.get("GAME_ID", r.get("Game_ID"))),
    ),
    col(
        "player_id",
        BIG,
        "Player identifier; NULL on team rows.",
        lambda r: to_id(r.get("PLAYER_ID", r.get("Player_ID"))),
    ),
    col("player_name", TEXT, "Player name; NULL on team rows.", "PLAYER_NAME"),
    col("team_id", BIG, "Team identifier (numeric ID).", "TEAM_ID"),
    col(
        "team_abbreviation",
        TEXT,
        "Team abbreviation.",
        lambda r: to_text(r.get("TEAM_ABBREVIATION")) or _matchup(0)(r),
    ),
    col("opponent_abbreviation", TEXT, "Opponent abbreviation parsed from the matchup.", _matchup(1)),
    col("is_home", BOOL, "Whether the team played at home ('vs.' rather than '@').", _matchup(2)),
    col("matchup", TEXT, "Matchup display, such as LAL vs. GSW or LAL @ BOS.", "MATCHUP"),
    col("win_loss", TEXT, "W or L; NULL for games not yet final.", "WL"),
    col("minutes", INT, "Minutes played (count of whole minutes).", "MIN"),
    *_shooting(INT),
    col("plus_minus", INT, "Point differential while on the floor (points).", "PLUS_MINUS"),
]
GAME_LOG_SCHEMA = schema_of(GAME_LOG_COLS)

# playergamelog carries no player name or team ID: the caller already knows the
# player, and the team is the first abbreviation of the matchup.
PLAYER_GAME_LOG_COLS = [c for c in GAME_LOG_COLS if c.name not in ("player_name", "team_id")]
PLAYER_GAME_LOG_SCHEMA = schema_of(PLAYER_GAME_LOG_COLS)


# --------------------------------------------------------------------------- career

CAREER_COLS = [
    stamp("season_type", TEXT, "Season type requested: Regular Season or Playoffs."),
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PLAYER_ID"),
    col("season", TEXT, "Season key, such as 2003-04.", "SEASON_ID"),
    col(
        "team_id", BIG, "Team identifier; NULL on a multi-team total row.", lambda r: to_id(r.get("TEAM_ID"))
    ),
    col("team_abbreviation", TEXT, "Team abbreviation; TOT on a multi-team total row.", "TEAM_ABBREVIATION"),
    col(
        "is_multi_team_total",
        BOOL,
        "True for the TOT row that sums a traded player's teams.",
        lambda r: r.get("TEAM_ABBREVIATION") == "TOT",
    ),
    col("player_age", FLOAT, "Player age in years during the season.", "PLAYER_AGE"),
    col("games_played", INT, "Games played (count).", "GP"),
    col("games_started", INT, "Games started (count).", "GS"),
    col("minutes", INT, "Minutes played (count of whole minutes).", "MIN"),
    *_shooting(INT),
]
CAREER_SCHEMA = schema_of(CAREER_COLS)

CAREER_TOTAL_COLS = [
    stamp("season_type", TEXT, "Season type requested: Regular Season or Playoffs."),
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "PLAYER_ID"),
    col("games_played", INT, "Career games played.", "GP"),
    col("games_started", INT, "Career games started.", "GS"),
    col("minutes", INT, "Career minutes played.", "MIN"),
    *_shooting(INT, " over the career (count)"),
]
CAREER_TOTAL_SCHEMA = schema_of(CAREER_TOTAL_COLS)


# --------------------------------------------------------------------------- box score

_BOX_KEYS = {
    "FGM": "fieldGoalsMade",
    "FGA": "fieldGoalsAttempted",
    "FG_PCT": "fieldGoalsPercentage",
    "FG3M": "threePointersMade",
    "FG3A": "threePointersAttempted",
    "FG3_PCT": "threePointersPercentage",
    "FTM": "freeThrowsMade",
    "FTA": "freeThrowsAttempted",
    "FT_PCT": "freeThrowsPercentage",
    "OREB": "reboundsOffensive",
    "DREB": "reboundsDefensive",
    "REB": "reboundsTotal",
    "AST": "assists",
    "STL": "steals",
    "BLK": "blocks",
    "TOV": "turnovers",
    "PF": "foulsPersonal",
    "PTS": "points",
    "PLUS_MINUS": "plusMinusPoints",
}

BOX_SCORE_COLS = [
    col("game_id", TEXT, "Ten-character NBA Stats game identifier (numeric ID).", "__game_id"),
    col("team_id", BIG, "Team identifier (numeric ID).", "__team_id"),
    col("team_tricode", TEXT, "Team three-letter code.", "__team_tricode"),
    col("team_city", TEXT, "Team city.", "__team_city"),
    col("team_name", TEXT, "Team nickname.", "__team_name"),
    col("is_home", BOOL, "Whether the player's team was the home team.", "__is_home"),
    col("player_id", BIG, "Stable NBA Stats player identifier (numeric ID).", "personId"),
    col(
        "player_name",
        TEXT,
        "Player name.",
        lambda r: to_text(f"{r.get('firstName') or ''} {r.get('familyName') or ''}"),
    ),
    col("position", TEXT, "Starting position (G, F, C); NULL for reserves.", "position"),
    col(
        "is_starter", BOOL, "Whether the player started the game.", lambda r: bool(to_text(r.get("position")))
    ),
    col("status_comment", TEXT, "Inactive or did-not-play reason, when given.", "comment"),
    col("minutes", TEXT, "Minutes played display, MM:SS.", "minutes"),
    col(
        "seconds_played",
        INT,
        "Exact playing time in seconds.",
        lambda r: minutes_to_seconds(r.get("minutes")),
    ),
    *_shooting(INT),
    col("plus_minus", INT, "Point differential while on the floor (points).", "PLUS_MINUS"),
]
BOX_SCORE_SCHEMA = schema_of(BOX_SCORE_COLS)


def box_score_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten boxscoretraditionalv3 into one record per player."""
    box = payload.get("boxScoreTraditional")
    if not isinstance(box, Mapping):
        raise ValueError("box score response has no boxScoreTraditional object")
    rows: list[dict[str, Any]] = []
    for side, is_home in (("homeTeam", True), ("awayTeam", False)):
        team = box.get(side)
        if not isinstance(team, Mapping):
            continue
        for player in team.get("players") or []:
            if not isinstance(player, Mapping):
                continue
            stats = player.get("statistics") if isinstance(player.get("statistics"), Mapping) else {}
            row = {**player, **{short: stats.get(long) for short, long in _BOX_KEYS.items()}}
            row["minutes"] = stats.get("minutes")
            row.update(
                __game_id=box.get("gameId"),
                __team_id=team.get("teamId"),
                __team_tricode=team.get("teamTricode"),
                __team_city=team.get("teamCity"),
                __team_name=team.get("teamName"),
                __is_home=is_home,
            )
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- play-by-play

PLAY_BY_PLAY_COLS = [
    stamp("game_id", TEXT, "Ten-character NBA Stats game identifier (numeric ID)."),
    col(
        "action_number",
        INT,
        "Play sequence number (index); a block or steal repeats the number of the play it ended.",
        "actionNumber",
    ),
    col(
        "action_id",
        INT,
        "Event ordinal in the published feed (index); unique within the game and increasing.",
        "actionId",
    ),
    col("period", INT, "Period number (1-4 regulation quarters, 5 and above overtime).", "period"),
    col("clock", TEXT, "ISO-8601 time remaining in the period, such as PT11M42.00S.", "clock"),
    col(
        "seconds_remaining",
        pa.decimal128(6, 2),
        "Seconds remaining in the period.",
        lambda r: clock_to_seconds(r.get("clock")),
    ),
    col(
        "team_id",
        BIG,
        "Team credited with the event; NULL for neutral events.",
        lambda r: to_id(r.get("teamId")),
    ),
    col("team_tricode", TEXT, "Team three-letter code.", "teamTricode"),
    col(
        "player_id",
        BIG,
        "Player credited with the event; NULL when none.",
        lambda r: to_id(r.get("personId")),
    ),
    col("player_name", TEXT, "Player family name as published.", "playerName"),
    col("action_type", TEXT, "Event type, such as Made Shot, Rebound, Foul, or period.", "actionType"),
    col("sub_type", TEXT, "Event detail, such as Jump Shot or start.", "subType"),
    col("description", TEXT, "Play description.", "description"),
    col(
        "home_score",
        INT,
        "Home points after the event (running score, carried forward on non-scoring plays).",
        "scoreHome",
    ),
    col(
        "away_score",
        INT,
        "Away points after the event (running score, carried forward on non-scoring plays).",
        "scoreAway",
    ),
    col("location", TEXT, "h for the home team or v for the visitors.", "location"),
    col("is_field_goal", BOOL, "Whether the event is a field-goal attempt.", "isFieldGoal"),
    col("shot_result", TEXT, "Made or Missed for shots.", "shotResult"),
    col("shot_value", INT, "Point value of the attempted shot (1, 2, or 3; 0 when not a shot).", "shotValue"),
    col("shot_distance", INT, "Shot distance in feet.", "shotDistance"),
    col("x_legacy", INT, "Legacy shot-chart x coordinate (tenths of feet from the basket).", "xLegacy"),
    col("y_legacy", INT, "Legacy shot-chart y coordinate (tenths of feet from the basket).", "yLegacy"),
]
PLAY_BY_PLAY_SCHEMA = schema_of(PLAY_BY_PLAY_COLS)


def play_by_play_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the actions of a playbyplayv3 response with a running score on every event.

    The feed fills ``scoreHome``/``scoreAway`` only on scoring events; the last
    known score is carried forward so every row states the score after it.
    """
    game = payload.get("game")
    if not isinstance(game, Mapping) or not isinstance(game.get("actions"), list):
        raise ValueError("play-by-play response has no game.actions array")
    home = away = 0
    actions = []
    for action in game["actions"]:
        if not isinstance(action, Mapping):
            continue
        home = to_int(action.get("scoreHome")) if to_int(action.get("scoreHome")) is not None else home
        away = to_int(action.get("scoreAway")) if to_int(action.get("scoreAway")) is not None else away
        actions.append({**action, "scoreHome": home, "scoreAway": away})
    return actions
