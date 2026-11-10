"""League configuration and season-key arithmetic for the NBA family of leagues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def eastern_today(now: datetime | None = None) -> date:
    """Return today's date in US Eastern time, the calendar the leagues schedule by."""
    moment = now or datetime.now(UTC)
    try:
        return moment.astimezone(ZoneInfo("America/New_York")).date()
    except ZoneInfoNotFoundError:  # pragma: no cover - only on hosts without tz data
        return (moment.astimezone(UTC) - timedelta(hours=5)).date()


@dataclass(frozen=True, slots=True)
class League:
    """One league served by the NBA Stats API.

    Attributes:
        schema: SQL schema name inside the ``nba`` catalog.
        league_id: The Stats API ``LeagueID`` (00, 10, or 20).
        name: Full league name.
        short: Short display name used in titles and comments.
        split_season: True when seasons straddle two calendar years (2025-26).
        season_start_month: First month of a new season; earlier months belong
            to the previous season.
        first_season: Earliest season the Stats API serves for this league.
        cdn: Which CDN serves the live scoreboard and schedule, if any.
        schedule_path: CDN path of the full current-season schedule, if any.
    """

    schema: str
    league_id: str
    name: str
    short: str
    split_season: bool
    season_start_month: int
    first_season: int
    cdn: str | None
    schedule_path: str | None

    def season_label(self, start_year: int) -> str:
        """Return the Stats API season key: '2025-26' for split seasons, '2025' otherwise."""
        if self.split_season:
            return f"{start_year}-{(start_year + 1) % 100:02d}"
        return str(start_year)

    def current_season(self, today: date | None = None) -> int:
        """Return the start year of the league's current (or most recent) season."""
        day = today or eastern_today()
        return day.year if day.month >= self.season_start_month else day.year - 1

    def resolve_season(self, season: int | None) -> int:
        """Return ``season`` or the current season, validating the league's range."""
        resolved = self.current_season() if season is None else season
        if resolved < self.first_season:
            raise ValueError(f"{self.short} statistics start with the {self.first_season} season")
        if resolved > self.current_season() + 1:
            raise ValueError(f"season {resolved} is in the future for the {self.short}")
        return resolved


NBA = League(
    schema="nba",
    league_id="00",
    name="National Basketball Association",
    short="NBA",
    split_season=True,
    season_start_month=10,
    first_season=1946,
    cdn="nba",
    schedule_path="staticData/scheduleLeagueV2.json",
)

WNBA = League(
    schema="wnba",
    league_id="10",
    name="Women's National Basketball Association",
    short="WNBA",
    split_season=False,
    season_start_month=5,
    first_season=1997,
    cdn="wnba",
    schedule_path="staticData/scheduleLeagueV2.json",
)

GLEAGUE = League(
    schema="gleague",
    league_id="20",
    name="NBA G League",
    short="G League",
    split_season=True,
    season_start_month=10,
    first_season=2001,
    cdn=None,
    schedule_path=None,
)

LEAGUES = (NBA, WNBA, GLEAGUE)
