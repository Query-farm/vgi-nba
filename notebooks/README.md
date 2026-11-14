# NBA SQL notebooks

[Two team rows, one game](01_two_team_rows_one_game.ipynb) pairs team game-log rows, reconciles scores from made shots, and compares FG% with effective FG% before narrowing a close-game threshold. The saved default uses an unchanged, recorded and trimmed NBA Stats response: four team rows for two games on 2025-10-21. The original capture timestamp is unavailable. [Fixture provenance](fixtures/provenance.json) records the source revision, request, scope and SHA-256.

## Run from this directory

Install [uv](https://docs.astral.sh/uv/), Git, and Python 3.13 or 3.14. Network access is needed for the notebook dependencies, published Haybarn/VGI extension and pinned worker Git revision on first use. Copying this entire `notebooks/` directory is sufficient; the worker source checkout is not required.

```bash
uv sync --frozen
uv run --frozen python execute.py
```

The notebook visibly attaches `uvx --python 3.13 --with vgi-python==0.51.0 --with vgi-rpc==0.53.1 --from git+https://github.com/Query-farm/vgi-nba@5d7bb8011fb035530deda5146446ce29313278bd vgi-nba`. `uvx` fetches that public revision into its own environment. The notebook environment runs Haybarn 2.0.0rc11 and is separately locked. Updating the Git or SDK pins should be followed by default, changed-parameter and live validation.

For interactive use, register the local interpreter and select it in Jupyter:

```bash
uv run --frozen python -m ipykernel install --user --name vgi-nba-notebooks --display-name 'Python (vgi-nba notebooks)'
```

Restart and run all after editing the visible parameters. Python helpers and plotting source are folded; SQL, tables, charts and explanations remain visible. The execution runner uses a temporary kernel, executes with this directory as cwd, and saves only after success. It requires a separate output directory for overrides, rejects unknown parameters and refuses to overwrite the source through `--output-dir`.

## Focused experiment and public API

```bash
uv run --frozen python execute.py --parameters '{"MAX_MARGIN":3}' --output-dir validation/margin-three
uv run --frozen python execute.py --parameters '{"DATA_MODE":"live"}' --output-dir validation/live --timeout 180
```

The experiment changes only the inclusive final-margin threshold from 10 to 3 points. Replay supports the fixed NBA 2025–26 regular-season team game-log request; the loopback helper rejects other requests. It returns the unchanged fixture through the real worker's supported `NBA_STATS_BASE_URL`. It is not a mock VGI result or an invented live dataset. No credentials are required and no writes are exposed.

Live mode clears inherited NBA/WNBA URL overrides and the request-interval override, requests the public season endpoint, and filters the same historical date. Public API failures remain errors; there is no automatic replay fallback. The worker documents that the NBA edge may block automated/cloud clients. The run clock is UTC; game dates follow the NBA schedule, and neither is a recorded capture timestamp.

## Validation and limits

The default, detached margin-three experiment and live run passed. The live request returned 2,460 season team rows and the same four date-filtered rows on 2026-11-14 UTC. Source tests passed (95 tests, 98.75% coverage); packaging, entry points, runtime dependency audit and configured metadata gate passed. Metadata reported 91/100, L1 structural, zero errors/warnings and 103 informational findings; its declared waivers remain unchanged. Docker container validation was unavailable because Docker Desktop could not start.

Default and the separately executed margin-three experiment are checked against the real pinned worker. Eleven SQL invariants cover unique team-game keys, complete final-game pairs, opponent labels, point differentials, shot bounds, score identities and exact margin selection. Chart source footers identify the mode and historical date. Replay is a tiny recorded subset, so it does not support league-wide claims or causal conclusions about shooting strategy. Effective FG% excludes free throws and is not a probability.

Generated validation outputs and extracted chart images are ignored. The saved notebook embeds its default outputs. `execute.py` copies assets, fixture provenance and runtime companions beside experiment copies. [STANDARD.md](STANDARD.md) documents presentation conventions.
