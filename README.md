<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-kalshi/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-nba</h1>

<p align="center">
  NBA, WNBA, and NBA G League standings, scores, box scores, and statistics as ordinary DuckDB tables.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-nba/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-nba/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-3776AB.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-read--only-6f42c1.svg" alt="Read-only VGI worker"></a>
</p>

---

`vgi-nba` fetches data on demand from the NBA's public Stats API
(`stats.nba.com`) and its content CDNs (`cdn.nba.com`, `cdn.wnba.com`), and
reformats the responses as typed Arrow data for Haybarn SQL. It needs no
credentials and does not rehost or persist any league dataset.

> **Not affiliated with the NBA.** This project is not affiliated with,
> endorsed by, or sponsored by the National Basketball Association, the WNBA,
> the NBA G League, or NBA Media Ventures, LLC. Read
> [Data source and terms](#data-source-and-terms) before you use it.

## Quick start

Install the released worker with `uv`:

```bash
uv tool install vgi-nba
vgi-nba
```

Launch Haybarn with the catalog pre-attached:

```bash
uvx haybarn-cli \
  -cmd "INSTALL vgi FROM community; LOAD vgi; ATTACH 'nba' (TYPE vgi, LOCATION 'vgi-nba');" \
  :memory:
```

Or attach it in an existing Haybarn session:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'nba' (
  TYPE vgi,
  LOCATION 'vgi-nba'
);
```

For source development, run `uv lock --check --no-sources`, then
`uv sync --no-sources` and `uv run --no-sources vgi-nba`.

## Catalog surface

The `nba` catalog has one schema per league, and every schema has the same
surface: `nba.nba`, `nba.wnba`, and `nba.gleague`.

| Kind | Objects |
| --- | --- |
| Current season | `current_standings`, `current_scoreboard`, `current_schedule`*, `current_teams`, `current_players`, `current_player_stats`, `current_team_stats`, `franchise_history` |
| Standings and games | `standings()`, `scoreboard()`, `league_game_log()`, `box_score()`, `play_by_play()` |
| Teams and players | `team_roster()`, `players()` |
| Season statistics | `player_stats()`, `advanced_player_stats()`, `team_stats()`, `advanced_team_stats()`, `league_leaders()` |
| Player detail | `player_game_log()`, `player_career_stats()`, `player_career_totals()` |

\* The G League publishes no season-schedule feed, so `nba.gleague` has no
`current_schedule`; use `scoreboard()` by date instead.

**Seasons** are passed as their start year. `season := 2025` is the 2025-26
season in the NBA and G League and the 2025 season in the WNBA. A `NULL` season
selects the current season, or the most recent one with data before a new
season's games begin. Each row's `season` column shows the season key it came
from, such as `2025-26` or `2025`.

**Game IDs** are ten-digit text with leading zeros, such as `'0022600201'`. The
first digit is the league (0 NBA, 1 WNBA, 2 G League) and the third digit is the
season type.

## Usage

### Standings

```sql
-- Today's conference races
SELECT conference, playoff_rank, team_name, wins, losses, conference_games_back
FROM nba.nba.current_standings
ORDER BY conference, playoff_rank;

-- Final 2025-26 standings
SELECT conference, playoff_rank, team_city, team_name, wins, losses
FROM nba.nba.standings(season := 2025)
ORDER BY conference, playoff_rank;

-- The 2025 WNBA playoffs seeding
SELECT playoff_rank, team_city, team_name, wins, losses
FROM nba.wnba.standings(season := 2025)
ORDER BY playoff_rank;
```

`win_percentage` and the games-back columns are exact decimals. `season_type`
accepts `Regular Season`, `Playoffs`, `Pre Season`, or `PlayIn`.

### Scores, schedules, and games

```sql
-- Today's scoreboard (live status; refreshed at most every 30 seconds)
SELECT away_team_tricode, away_score, home_team_tricode, home_score, game_status_text
FROM nba.nba.current_scoreboard
ORDER BY start_time_utc;

-- Any date
SELECT game_id, away_team_tricode, away_score, home_team_tricode, home_score
FROM nba.nba.scoreboard(DATE '2026-11-08');

-- A box score and the fourth-quarter threes from the same game
SELECT team_tricode, player_name, minutes, points, rebounds, assists
FROM nba.nba.box_score('0022600201')
ORDER BY points DESC;

SELECT clock, team_tricode, player_name, description, home_score, away_score
FROM nba.nba.play_by_play('0022600201')
WHERE period = 4 AND shot_value = 3 AND shot_result = 'Made'
ORDER BY action_id;
```

Play-by-play rows carry the running score on every event. Order them by
`action_id`: a block or steal shares its `action_number` with the shot or
turnover it ended.

### Player and team statistics

```sql
-- Current per-game scoring leaders
SELECT player_name, team_abbreviation, games_played, points
FROM nba.nba.current_player_stats
WHERE games_played >= 10
ORDER BY points DESC
LIMIT 10;

-- The official 2025-26 assists leaderboard
SELECT rank, player_name, team_abbreviation, assists
FROM nba.nba.league_leaders(season := 2025, stat_category := 'AST')
ORDER BY rank;

-- 2025-26 team ratings
SELECT team_name, offensive_rating, defensive_rating, net_rating, pace
FROM nba.nba.advanced_team_stats(season := 2025)
ORDER BY net_rating DESC;

-- 2025-26 G League season totals
SELECT player_name, team_abbreviation, points, assists
FROM nba.gleague.player_stats(season := 2025, per_mode := 'Totals')
ORDER BY points DESC
LIMIT 10;
```

`per_mode` chooses `PerGame` (the default), `Totals`, `Per36`, or
`Per100Possessions`, and the counting columns follow it. Shooting percentages
are 0-1 fractions.

### Players, careers, and game logs

```sql
-- Find a player ID
SELECT player_id, player_name, team_abbreviation, from_year, to_year
FROM nba.nba.players(only_current := false)
WHERE player_name ILIKE '%LeBron%';

-- Career totals and season-by-season lines
SELECT games_played, points, rebounds, assists
FROM nba.nba.player_career_totals(2544);

SELECT season, team_abbreviation, games_played, points
FROM nba.nba.player_career_stats(2544)
WHERE NOT is_multi_team_total
ORDER BY season;

-- A season's game log
SELECT game_date, matchup, win_loss, points, rebounds, assists
FROM nba.nba.player_game_log(2544, season := 2025)
ORDER BY game_date;
```

A traded player's season has one row per team plus a `TOT` row
(`is_multi_team_total`) that sums them; filter one or the other before adding
seasons together.

## Data source and terms

- **Source.** All data comes from the NBA's undocumented but long-stable public
  endpoints: `stats.nba.com/stats/*` (with `LeagueID` 00, 10, or 20) and the
  `cdn.nba.com` / `cdn.wnba.com` `liveData` and `staticData` feeds. No ESPN or
  other third-party source is used. The CDN's odds feed is deliberately not
  exposed.
- **Ownership.** NBA, WNBA, and NBA G League data, names, and marks belong to
  NBA Media Ventures, LLC and its affiliates. The MIT licence covers this
  worker's code only, not the data it retrieves.
- **Terms of use.** Section 9 of the NBA.com Terms of Use permits personal,
  non-commercial use only, and prohibits gambling and fantasy products, live
  data products, and building "a database of comprehensive, regularly updated
  statistics". `vgi-nba` is a user-run client in the same posture as the
  open-source `nba_api` package: it fetches data on request and stores nothing
  beyond a short in-memory cache. You are responsible for making sure your use
  complies with the league's terms. Do not use it to power a betting, fantasy,
  or commercial data product, or to republish the data.
- **No affiliation.** This project is not affiliated with, endorsed by, or
  sponsored by the NBA, the WNBA, or the NBA G League.

## Production HTTP deployment

Stdio is the recommended transport when Haybarn can launch the worker locally.
Use HTTP when the worker must be shared across hosts. The production HTTP entry
point fails closed unless it has a stable signing key and either authentication
or an explicit anonymous-access decision:

```bash
export VGI_SIGNING_KEY='a-long-random-secret-shared-by-every-replica'
export VGI_BEARER_TOKENS='replace-with-a-random-token=haybarn'

vgi-nba-http \
  --host 0.0.0.0 \
  --port 8000 \
  --cors-origins 'https://sql.example.com' \
  --log-format json
```

`vgi-nba-http` disables the description pages and cross-origin access unless
they are explicitly configured. Terminate TLS at a trusted proxy or load
balancer. All replicas must receive the same `VGI_SIGNING_KEY`; rotate it as a
coordinated deployment because outstanding state tokens are signed with it.

Supported authentication options are:

- `VGI_BEARER_TOKENS`: comma-separated `token=principal` pairs.
- `VGI_JWT_ISSUER` and `VGI_JWT_AUDIENCE`: JWT/JWKS authentication.
- `VGI_PROXY_PROOF_MODE=require` with the corresponding VGI proxy-proof
  settings when a trusted proxy authenticates users.

`VGI_ALLOW_ANONYMOUS=1` turns off the authentication requirement. Given the
league's non-commercial terms, do not run a public, anonymous deployment that
redistributes NBA data to third parties. The health probe is `GET /health`.

The NBA's Akamai edge blocks many cloud and datacenter address ranges. Check
that a deployment host can reach `stats.nba.com` before you rely on it.

### Container

```bash
docker build -t vgi-nba .
docker run --rm -p 8000:8000 \
  -e VGI_SIGNING_KEY='replace-with-a-stable-random-secret' \
  -e VGI_BEARER_TOKENS='replace-with-a-random-token=haybarn' \
  vgi-nba
```

The image runs as an unprivileged user, emits JSON logs, disables interactive
description pages, and includes a health check.

## Reliability and operations

- **Headers.** The Stats API and CDNs sit behind Akamai bot management. Every
  request sends an honest User-Agent (`vgi-nba/<version> (Query-farm/vgi-nba)`),
  `Referer: https://www.nba.com/`, `Accept`, `Accept-Language`,
  `Accept-Encoding`, and a keep-alive connection. Akamai rejects any
  User-Agent that contains a URL or domain (403 on the CDN, a silent stall on
  the Stats API), so the project is identified by its GitHub slug instead of a
  `+https://` contact link.
- **Stalls fail fast.** A rejected or blocked request is not answered with an
  error. Akamai holds the socket open instead. Requests use a 5-second connect
  timeout and a 15-second read timeout, and a timeout is reported at once as a
  clear "did not respond" error rather than retried.
- **Retries.** HTTP 429 and transient 5xx responses and connection resets are
  retried up to three attempts with jittered exponential backoff, honoring
  `Retry-After` (capped at 20 seconds) within a 60-second query deadline.
- **Pacing and concurrency.** Requests to one host start at least 0.6 seconds
  apart (`NBA_REQUEST_INTERVAL` overrides this), with at most two requests in
  flight per process.
- **Caching.** Responses are cached in process for the longer of the origin's
  `max-age` and a floor per dataset: 30 seconds for live scoreboards, box
  scores, and play-by-play; 5 minutes for standings, statistics, and the
  schedule; 30 minutes for the player index; and 6 hours for teams and
  franchise history. The remaining lifetime is forwarded to VGI clients as
  their cache TTL.

`NBA_STATS_BASE_URL`, `NBA_CDN_BASE_URL`, and `WNBA_CDN_BASE_URL` may point to
alternate roots for hermetic tests. Do not set them from untrusted user input.

## Development and release checks

```bash
uv lock --check --no-sources
uv sync --no-sources
uv run --no-sources ruff check .
uv run --no-sources ruff format --check .
uv run --no-sources pytest
uv build
uvx twine check dist/*
uvx --no-sources --from vgi-lint-check==2.2.0 vgi-lint lint \
  'uv run --no-sources nba_worker.py' \
  --no-execute --no-check-links --audit-waivers --fail-on warning
```

The offline suite runs against recorded, trimmed real responses in
`tests/fixtures/`. That includes a Haybarn end-to-end test that serves them from
a local HTTP server and queries every current table in all three schemas. Live
upstream smoke tests are opt-in with `uv run pytest -m live` and also run in a
serialized scheduled workflow alongside the executing `vgi-lint`. Publishing is
release-driven: create a GitHub release named `vX.Y.Z` matching the version in
`pyproject.toml`. CI builds, attests, validates, and publishes the
distributions through PyPI trusted publishing.

---

Copyright 2026 Query Farm LLC - https://query.farm
