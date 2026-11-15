# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.2.1]

- Docs link to NBA.com Stats, WNBA.com Stats, the NBA G League and the NBA.com
  Terms of Use: the catalog's documentation gains a Sources and references
  section (vgi-lint VGI183).
- Composed examples that join two of a league's functions (standings with net
  rating, a roster with season averages, scoring leaders with efficiency), on
  each driving function and in the schema-level list, plus two agent tasks.
- Fix the `playoff_rank` column comment: WNBA seeding is league-wide, not per
  conference.

## [0.2.0]

- Port to VGI protocol 3.3 (vgi-python 0.51). Requires the current VGI DuckDB
  extension; 3.2 extensions are incompatible. No SQL surface changes.

## [0.1.0]

- Initial release on VGI protocol 3.2 (vgi-python 0.50) with vgi-lint 2.2.0.
- Three league schemas, `nba`, `wnba`, and `gleague`, generated from one set of
  specs over the NBA Stats API (LeagueID 00/10/20) and the NBA/WNBA CDNs.
- Current tables: standings, today's scoreboard, the season schedule (NBA and
  WNBA), teams, players, per-game player and team statistics, and franchise
  history.
- Functions: standings, scoreboards by date, rosters, the player index, season
  and advanced player/team statistics, official leaderboards, league and player
  game logs, career statistics and totals, box scores, and play-by-play.
- Akamai-compatible request headers, fast failure on stalled connections,
  per-host request pacing, bounded retries honoring `Retry-After`, and an
  in-process response cache that never refreshes faster than the origin allows.
