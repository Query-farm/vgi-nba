"""VGI worker exposing NBA, WNBA, and NBA G League data as a read-only SQL catalog."""

from __future__ import annotations

import json
import os
import sys

from vgi import Worker
from vgi.catalog import Catalog, ConstraintInfo, Schema, Table, constraint_record

from vgi_nba import schemas as s
from vgi_nba.functions import SURFACES, Spec, render
from vgi_nba.leagues import GLEAGUE, NBA, WNBA, League
from vgi_nba.meta import column_comments

SOURCE_URL = "https://github.com/Query-farm/vgi-nba"

_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "nba_teams_are_populated",
            "description": "The NBA has thirty active teams with stable identifiers.",
            "sql": "SELECT count(*) = 30 AS thirty_teams FROM nba.nba.current_teams",
            "expected_result": [{"thirty_teams": True}],
        },
        {
            "name": "wnba_franchises_include_summaries",
            "description": "Every active WNBA team has an all-time franchise summary row.",
            "sql": (
                "SELECT count(*) >= 13 AS populated FROM nba.wnba.franchise_history "
                "WHERE row_kind = 'franchise'"
            ),
            "expected_result": [{"populated": True}],
        },
        {
            "name": "historical_standings_balance",
            "description": "Final 2025-26 NBA standings: 30 teams whose wins equal their losses in total.",
            "sql": (
                "SELECT count(*) = 30 AND sum(wins) = sum(losses) AS balanced "
                "FROM nba.nba.standings(season := 2025)"
            ),
            "expected_result": [{"balanced": True}],
        },
        {
            "name": "box_score_points_match_scoreboard",
            "description": "A completed game's player points sum to the published final score.",
            "sql": (
                "SELECT (SELECT sum(points) FROM nba.nba.box_score('0022600201')) = "
                "(SELECT home_score + away_score FROM nba.nba.scoreboard(DATE '2026-11-08') "
                "WHERE game_id = '0022600201') AS consistent"
            ),
            "expected_result": [{"consistent": True}],
        },
    ]
)

_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "conference_leaders",
            "prompt": "Who leads each NBA conference right now, and what is each leader's scoring average?",
        },
        {
            "name": "western_standings_2025",
            "prompt": (
                "List the final 2025-26 NBA Western Conference standings in seeding order, with each "
                "franchise's all-time championship count."
            ),
        },
        {
            "name": "todays_slate",
            "prompt": "Which NBA games are on today's scoreboard, and in which arena is each one played?",
        },
        {
            "name": "tallest_laker",
            "prompt": "Who is the tallest player on the current Los Angeles Lakers roster?",
        },
        {
            "name": "lebron_career",
            "prompt": (
                "How many career regular-season points has LeBron James scored, and in which season did "
                "he score the most?"
            ),
        },
        {
            "name": "game_top_scorer",
            "prompt": (
                "In NBA game 0022600201, who won, who was the top scorer, and how many of his points came "
                "in the fourth quarter?"
            ),
        },
        {
            "name": "assist_leaders_efficiency",
            "prompt": (
                "For the top five 2025-26 NBA assists-per-game leaders, show their true shooting "
                "percentage and turnovers per game."
            ),
        },
        {
            "name": "best_net_rating_team",
            "prompt": (
                "Which NBA team had the best net rating in 2025-26, what was its three-point percentage, "
                "and what was its highest-scoring game?"
            ),
        },
        {
            "name": "lebron_this_season",
            "prompt": (
                "Show LeBron James's game-by-game scoring this NBA season (player ID 2544) and compare his "
                "average with the current league scoring leader."
            ),
        },
        {
            "name": "wnba_scoring_title",
            "prompt": "Who led the WNBA in points per game in the 2025 season?",
        },
    ]
)

_CATEGORIES = json.dumps(
    [
        {
            "name": "standings",
            "title": "Standings",
            "description": "Team records, conference and division ranks, games back, and clinch flags.",
            "keywords": ["standings", "records", "seeding", "games back"],
        },
        {
            "name": "games",
            "title": "Games",
            "description": "Scoreboards, the season schedule, game logs, box scores, and play-by-play.",
            "keywords": ["scores", "schedule", "box score", "play-by-play", "game log"],
        },
        {
            "name": "teams",
            "title": "Teams",
            "description": "Active teams, rosters, and franchise history.",
            "keywords": ["teams", "rosters", "franchises"],
        },
        {
            "name": "players",
            "title": "Players",
            "description": "The player index with identifiers and career spans.",
            "keywords": ["players", "player id"],
        },
        {
            "name": "player-stats",
            "title": "Player Statistics",
            "description": "Season box-score and advanced player statistics and official leaderboards.",
            "keywords": ["player stats", "advanced", "leaders"],
        },
        {
            "name": "team-stats",
            "title": "Team Statistics",
            "description": "Season box-score and advanced team statistics.",
            "keywords": ["team stats", "ratings", "pace"],
        },
        {
            "name": "player-detail",
            "title": "Player Detail",
            "description": "Game logs and career statistics for one player.",
            "keywords": ["career", "game log"],
        },
    ]
)


def _key_constraints(schema: str, table: str, key: tuple[str, ...]) -> tuple[ConstraintInfo, ...]:
    """Primary-key and NOT NULL records for a read-only table keyed by ``key``."""
    if not key:
        return ()
    records = [
        constraint_record(
            [schema],
            table,
            "pk",
            "PRIMARY_KEY",
            [[column] for column in key],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
    ]
    records += [
        constraint_record(
            [schema],
            table,
            f"{column}_not_null",
            "NOT_NULL",
            [[column]],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
        for column in key
    ]
    return tuple(records)


def _table_tags(spec: Spec, league: League) -> dict[str, str]:
    examples = [
        {"description": render(e.description, league), "sql": render(e.sql, league)} for e in spec.examples
    ]
    return {
        "provider": "nba",
        "vgi.category": spec.category,
        "vgi.title": render(spec.title, league),
        "vgi.keywords": json.dumps(spec.keywords),
        "vgi.doc_llm": render(spec.doc_llm, league),
        "vgi.doc_md": render(spec.doc_md, league),
        "vgi.example_queries": json.dumps(examples),
    }


_SCHEMA_TEXT = {
    "nba": (
        "NBA",
        "Use current_standings, current_scoreboard, current_schedule, current_teams, current_players, "
        "current_player_stats, and current_team_stats for the live season. Parameterized functions "
        "cover historical seasons, playoffs, advanced ratings, leaderboards, game logs, careers, box "
        "scores, and play-by-play. Seasons are passed as their start year: season => 2025 is 2025-26.",
    ),
    "wnba": (
        "WNBA",
        "Same tables and functions as the nba schema for the WNBA (no divisions). Seasons are single "
        "calendar years: season => 2025 is the 2025 season. Game IDs start with 10.",
    ),
    "gleague": (
        "NBA G League",
        "Same tables and functions as the nba schema for the NBA G League, except there is no "
        "published season-schedule feed (current_schedule is absent; use scoreboard() by date). "
        "season => 2025 is 2025-26. Game IDs start with 20.",
    ),
}


def _schema(league: League) -> Schema:
    surface = SURFACES[league.schema]
    title, llm = _SCHEMA_TEXT[league.schema]
    tables = []
    for spec, function in surface.tables:
        table = spec.name.removeprefix("all_")
        schema = s.schema_of(spec.cols)
        tables.append(
            Table(
                name=table,
                function=function,
                comment=render(spec.doc_llm, league).split(". ")[0].rstrip("."),
                tags=_table_tags(spec, league),
                column_comments=column_comments(schema),
                constraints=_key_constraints(league.schema, table, spec.key),
                cardinality_estimate=spec.estimate[league.schema],
            )
        )
    first = surface.tables[0][0]
    return Schema(
        path=[league.schema],
        comment=f"{league.name} standings, scores, schedules, and statistics",
        tags={
            "provider": "nba",
            "domain": "sports",
            "vgi.title": f"{title} Data",
            "vgi.keywords": json.dumps([title, "basketball", "standings", "box scores", "player stats"]),
            "vgi.categories": _CATEGORIES,
            "vgi.doc_llm": llm,
            "vgi.doc_md": (
                f"## {league.name}\n\n{llm}\n\n"
                "Every object is read-only and fetched on demand from the NBA Stats API or the "
                "league CDN. Game IDs are ten-digit text; keep the leading zeros when filtering."
            ),
            "vgi.example_queries": _table_tags(first, league)["vgi.example_queries"],
        },
        functions=surface.functions,
        tables=tables,
    )


NBA_CATALOG = Catalog(
    name="nba",
    default_schema="nba",
    source_url=SOURCE_URL,
    comment="Read-only NBA, WNBA, and NBA G League standings, scores, and statistics from the NBA Stats API",
    tags={
        "provider": "nba",
        "domain": "sports",
        "vgi.title": "NBA Basketball Data",
        "vgi.subtitle": "Standings, scores, schedules, box scores, play-by-play, and player and team stats.",
        "vgi.source_url": SOURCE_URL,
        "vgi.author": "Query Farm LLC <hello@query.farm>",
        "vgi.copyright": (
            "Worker (c) 2026 Query Farm LLC. NBA, WNBA, and NBA G League data (c) NBA Media Ventures, "
            "LLC; use is subject to the NBA.com Terms of Use (personal, non-commercial use)."
        ),
        "vgi.license": "MIT",
        "vgi.support_contact": "https://github.com/Query-farm/vgi-nba/issues",
        "vgi.support_policy_url": "https://github.com/Query-farm/vgi-nba/blob/main/README.md",
        "vgi.keywords": json.dumps(["NBA", "WNBA", "G League", "basketball", "standings", "box score"]),
        "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
        "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
        "vgi.doc_llm": (
            "Read-only NBA, WNBA, and NBA G League data from the official NBA Stats API and NBA/WNBA "
            "CDNs, in three schemas: nba, wnba, and gleague, each with the same tables and functions. "
            "current_* tables give live standings, today's scoreboard, the season schedule, teams, "
            "players, and per-game player and team statistics. Functions take a season as its start "
            "year (2025 = 2025-26 for NBA and G League, 2025 for WNBA) and cover standings, "
            "scoreboards by date, rosters, season and advanced statistics, leaderboards, league and "
            "player game logs, careers, box scores, and play-by-play. Game IDs are 10-digit text."
        ),
        "vgi.doc_md": (
            "# NBA, WNBA, and G League basketball data\n\n"
            "Query current and historical basketball data from SQL. Each league is its own schema "
            "(`nba`, `wnba`, `gleague`) with the same surface: standings, scores, schedules, teams, "
            "rosters, player and team statistics, leaderboards, game logs, careers, box scores, and "
            "play-by-play.\n\n"
            "## Data source\n\n"
            "Data is fetched on demand from the NBA's public Stats API and content CDN; nothing is "
            "stored or rehosted. NBA data is for personal, non-commercial use under the NBA.com Terms "
            "of Use. This project is not affiliated with or endorsed by the NBA, WNBA, or NBA G League."
        ),
    },
    schemas=[_schema(league) for league in (NBA, WNBA, GLEAGUE)],
)


class NbaWorker(Worker):
    """Worker process hosting the read-only NBA-family catalog."""

    catalog = NBA_CATALOG


def main() -> None:
    """Run over stdio by default, or HTTP when VGI's ``--http`` flag is supplied."""
    NbaWorker.main()


def main_http() -> None:
    """Run HTTP with production-safe configuration requirements."""
    if not os.environ.get("VGI_SIGNING_KEY"):
        raise SystemExit(
            "VGI_SIGNING_KEY is required for HTTP mode so state remains valid across restarts and replicas"
        )
    auth_configured = bool(os.environ.get("VGI_BEARER_TOKENS") or os.environ.get("VGI_JWT_ISSUER"))
    auth_configured = auth_configured or os.environ.get("VGI_PROXY_PROOF_MODE", "").lower() == "require"
    if not auth_configured and os.environ.get("VGI_ALLOW_ANONYMOUS") != "1":
        raise SystemExit(
            "HTTP authentication is required; configure bearer/JWT/proxy proof, or explicitly set "
            "VGI_ALLOW_ANONYMOUS=1 for an intentionally public deployment"
        )
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    if "--describe" not in argv and "--no-describe" not in argv:
        argv = ["--no-describe", *argv]
    if not any(value == "--cors-origins" or value.startswith("--cors-origins=") for value in argv):
        argv = ["--cors-origins", "", *argv]
    sys.argv = [sys.argv[0], *argv]
    NbaWorker.main()
