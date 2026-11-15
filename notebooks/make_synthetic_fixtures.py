"""Generate entirely fictional, deliberately small NBA-shaped API responses.

No values are copied from real athletes or games. Output envelopes follow the
published v0.2.0 client's parsers; visible notebook SQL performs the analysis.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
routes = []
DASH = dict.fromkeys(
    [
        "College",
        "Conference",
        "Country",
        "DateFrom",
        "DateTo",
        "Division",
        "DraftPick",
        "DraftYear",
        "GameScope",
        "GameSegment",
        "Height",
        "Location",
        "Outcome",
        "PlayerExperience",
        "PlayerPosition",
        "SeasonSegment",
        "ShotClockRange",
        "StarterBench",
        "VsConference",
        "VsDivision",
        "Weight",
    ],
    "",
)
DASH.update(
    LastNGames=0,
    Month=0,
    OpponentTeamID=0,
    PORound=0,
    PaceAdjust="N",
    Period=0,
    PlusMinus="N",
    Rank="N",
    TeamID=0,
    TwoWay=0,
)


def result(name, rows):
    headers = list(dict.fromkeys(k for row in rows for k in row))
    return {"name": name, "headers": headers, "rowSet": [[r.get(k) for k in headers] for r in rows]}


def stats(name, rows):
    return {"resultSets": [result(name, rows)]}


def route(scenario, schema, endpoint, params, body, suffix=""):
    routes.append(
        {
            "id": f"{schema}-{endpoint}{suffix}",
            "scenario": scenario,
            "path": "/stats/" + endpoint,
            "parameters": {k: str(v) for k, v in params.items()},
            "body": body,
        }
    )


for i, (schema, league) in enumerate([("nba", "00"), ("wnba", "10"), ("gleague", "20")]):
    season = "2025" if schema == "wnba" else "2025-26"
    prior = "2024" if schema == "wnba" else "2024-25"
    gid = f"{i}022500001"
    # IDs deliberately repeat across schemas: joins must retain the league key.
    teams = [(7001, "Alpha", "AAA"), (7002, "Birch", "BBB"), (7003, "Cedar", "CCC")]
    players = [(9001, "Ada Vale"), (9002, "Bea North"), (9003, "Cam Drift")]
    values = [[2, 3, 3, 3, 2, 2], [3, 2, 2, 3, 3, 2], [2, 3, 2, 2, 2, 3]][i]
    home = away = 0
    actions = []
    contributions = {9001: 0, 9002: 0, 9011: 0, 9012: 0}
    for j, (period, side, player) in enumerate(
        [(1, "h", 9001), (1, "v", 9011), (2, "h", 9002), (3, "v", 9012), (4, "h", 9001), (4, "h", 9002)]
    ):
        points = values[j]
        if side == "h":
            home += points
        else:
            away += points
        contributions[player] += points
        actions.append(
            {
                "actionId": j + 1,
                "actionNumber": (j + 1) * 2,
                "period": period,
                "clock": "PT01M00.00S",
                "teamId": 7001 if side == "h" else 7002,
                "teamTricode": "AAA" if side == "h" else "BBB",
                "personId": player,
                "playerName": f"Fictional {player}",
                "actionType": "Made Shot",
                "description": f"Synthetic {points}-point basket",
                "isFieldGoal": 1,
                "shotResult": "Made",
                "shotValue": points,
                "scoreHome": str(home),
                "scoreAway": str(away),
                "location": side,
            }
        )
    actions.append(
        {
            "actionId": 7,
            "actionNumber": 12,
            "period": 4,
            "clock": "PT00M30.00S",
            "teamId": 7002,
            "teamTricode": "BBB",
            "personId": 9011,
            "playerName": "Fictional 9011",
            "actionType": "Missed Shot",
            "description": "Synthetic missed shot; same action number, distinct event ID",
            "isFieldGoal": 1,
            "shotResult": "Missed",
            "shotValue": 3,
            "scoreHome": "",
            "scoreAway": "",
            "location": "v",
        }
    )
    team_bodies = {}
    for side, teamid, tricode, score, ids in [
        ("homeTeam", 7001, "AAA", home, [9001, 9002]),
        ("awayTeam", 7002, "BBB", away, [9011, 9012]),
    ]:
        team_bodies[side] = {
            "teamId": teamid,
            "teamName": "Fictional " + tricode,
            "teamCity": "Example",
            "teamTricode": tricode,
            "score": score,
            "players": [
                {
                    "personId": pid,
                    "firstName": "Fictional",
                    "familyName": str(pid),
                    "position": "G",
                    "statistics": {"points": contributions[pid], "minutes": "18:30"},
                }
                for pid in ids
            ],
        }
    board = {k: {x: y for x, y in v.items() if x != "players"} for k, v in team_bodies.items()}
    board.update(
        gameId=gid,
        gameStatus=3,
        gameStatusText="Final (synthetic miniature)",
        period=4,
        gameTimeUTC="2025-10-21T23:00:00Z",
    )
    route(
        "games",
        schema,
        "scoreboardv3",
        {"LeagueID": league, "GameDate": "2025-10-21"},
        {"scoreboard": {"gameDate": "2025-10-21", "games": [board]}},
    )
    route(
        "games",
        schema,
        "boxscoretraditionalv3",
        dict(GameID=gid, StartPeriod=0, EndPeriod=0, StartRange=0, EndRange=0, RangeType=0),
        {"boxScoreTraditional": {"gameId": gid, **team_bodies}},
    )
    route(
        "games",
        schema,
        "playbyplayv3",
        dict(GameID=gid, StartPeriod=0, EndPeriod=0),
        {"game": {"gameId": gid, "actions": actions}},
    )
    standings = []
    base_teams = []
    advanced_teams = []
    base_players = []
    advanced_players = []
    leaders = []
    for j, (tid, name, abbr) in enumerate(teams):
        wins = [8, 6, 4][j]
        pts = [100, 104, 98][j] + i
        net = [2, 5, -1][(j + i) % 3]
        standings.append(
            dict(
                TeamID=tid,
                TeamCity="Example",
                TeamName=name,
                Conference="Example",
                PlayoffRank=j + 1,
                WINS=wins,
                LOSSES=10 - wins,
                WinPCT=wins / 10,
                PointsPG=pts,
            )
        )
        base_teams.append(
            dict(
                TEAM_ID=tid,
                TEAM_NAME=name,
                GP=10,
                W=wins,
                L=10 - wins,
                PTS=pts,
                FGM=38,
                FGA=80,
                FG_PCT=0.475,
                FG3M=10,
                FG3A=28,
                FG3_PCT=10 / 28,
                FTM=pts - 86,
                FTA=pts - 82,
            )
        )
        advanced_teams.append(
            dict(
                TEAM_ID=tid,
                TEAM_NAME=name,
                GP=10,
                W=wins,
                L=10 - wins,
                OFF_RATING=110 + net,
                DEF_RATING=110,
                NET_RATING=net,
                PACE=95 + i * 2 + j,
                TS_PCT=0.55 + j * 0.02,
            )
        )
        pid, pname = players[j]
        gp = [4, 10, 10][j]
        points = [30, 24, 20][j] + i
        base_players.append(
            dict(
                PLAYER_ID=pid,
                PLAYER_NAME=pname,
                TEAM_ID=tid,
                TEAM_ABBREVIATION=abbr,
                GP=gp,
                PTS=points,
                MIN=30,
                TEAM_COUNT=1,
            )
        )
        advanced_players.append(
            dict(
                PLAYER_ID=pid,
                PLAYER_NAME=pname,
                TEAM_ID=tid,
                TEAM_ABBREVIATION=abbr,
                GP=gp,
                TS_PCT=[0.65, 0.55, 0.60][j],
                USG_PCT=[0.35, 0.25, 0.20][j],
            )
        )
        leaders.append(dict(PLAYER_ID=pid, PLAYER=pname, TEAM_ID=tid, TEAM=abbr, GP=gp, PTS=points, RANK=j + 1))
    route(
        "rates",
        schema,
        "leaguestandingsv3",
        dict(LeagueID=league, Season=season, SeasonType="Regular Season"),
        stats("Standings", standings),
    )
    for endpoint, measure, rows in [
        ("leaguedashteamstats", "Base", base_teams),
        ("leaguedashteamstats", "Advanced", advanced_teams),
        ("leaguedashplayerstats", "Base", base_players),
        ("leaguedashplayerstats", "Advanced", advanced_players),
    ]:
        route(
            "rates",
            schema,
            endpoint,
            {
                **DASH,
                "LeagueID": league,
                "Season": season,
                "SeasonType": "Regular Season",
                "PerMode": "PerGame",
                "MeasureType": measure,
            },
            stats("LeagueDashPlayerStats" if "player" in endpoint else "LeagueDashTeamStats", rows),
            "-" + measure,
        )
    route(
        "rates",
        schema,
        "leagueleaders",
        dict(
            LeagueID=league,
            Season=season,
            SeasonType="Regular Season",
            PerMode="PerGame",
            Scope="S",
            StatCategory="PTS",
        ),
        stats("LeagueLeaders", leaders),
    )
    index = [
        dict(
            PERSON_ID=pid,
            DISPLAY_FIRST_LAST=name,
            ROSTERSTATUS=int(j < 2),
            FROM_YEAR=2024,
            TO_YEAR=2025,
            TEAM_ID=7001 if j < 2 else 7003,
            TEAM_ABBREVIATION="AAA" if j < 2 else "CCC",
        )
        for j, (pid, name) in enumerate(players)
    ]
    route(
        "career",
        schema,
        "commonallplayers",
        dict(LeagueID=league, Season="CURRENT", IsOnlyCurrentSeason=0),
        stats("CommonAllPlayers", index),
    )
    roster = [
        dict(TeamID=7001, PLAYER_ID=pid, PLAYER=name, POSITION="G", HEIGHT="6-0", EXP="1") for pid, name in players[:2]
    ]
    route(
        "career",
        schema,
        "commonteamroster",
        dict(TeamID=7001, LeagueID=league, Season=season),
        stats("CommonTeamRoster", roster),
    )
    rows = [
        dict(PLAYER_ID=9001, SEASON_ID=yr, TEAM_ID=tid, TEAM_ABBREVIATION=abbr, GP=gp, PTS=pts)
        for yr, tid, abbr, gp, pts in [
            (prior, 7001, "AAA", 3, 60),
            (season, 7001, "AAA", 2, 40),
            (season, 7002, "BBB", 3, 60),
            (season, 0, "TOT", 5, 100),
        ]
    ]
    body = {
        "resultSets": [
            result("SeasonTotalsRegularSeason", rows),
            result("CareerTotalsRegularSeason", [dict(PLAYER_ID=9001, GP=8, PTS=160)]),
        ]
    }
    route("career", schema, "playercareerstats", dict(PlayerID=9001, PerMode="Totals", LeagueID=league), body)
    games = [
        dict(
            Player_ID=9001,
            Game_ID=f"{i}02250000{j + 1}",
            GAME_DATE=f"2025-10-{21 + j}",
            MATCHUP=("AAA" if j < 2 else "BBB") + " vs. CCC",
            WL="W",
            PTS=pts,
        )
        for j, pts in enumerate([10, 30, 20, 25, 15])
    ]
    route(
        "career",
        schema,
        "playergamelog",
        dict(PlayerID=9001, Season=season, SeasonType="Regular Season", LeagueID=league),
        stats("PlayerGameLog", games),
    )

payload = {
    "schema_version": 1,
    "provenance": "Entirely synthetic miniature scenarios. Fictional people, teams, IDs and observations; not league records or live responses. League namespaces exercise the released parser only.",
    "generator": "make_synthetic_fixtures.py",
    "routes": routes,
}
(ROOT / "fixtures/synthetic-responses.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
print("Generated", len(routes), "synthetic response routes.")
