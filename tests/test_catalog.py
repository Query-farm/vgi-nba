"""Tests for the SQL-facing catalog shape and its discovery metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vgi_nba.functions import SURFACES
from vgi_nba.worker import NBA_CATALOG

ROOT = Path(__file__).resolve().parent.parent

FUNCTIONS = {
    "standings",
    "scoreboard",
    "team_roster",
    "players",
    "player_stats",
    "advanced_player_stats",
    "team_stats",
    "advanced_team_stats",
    "league_leaders",
    "league_game_log",
    "player_game_log",
    "player_career_stats",
    "player_career_totals",
    "box_score",
    "play_by_play",
}
TABLES = {
    "current_standings",
    "current_scoreboard",
    "current_schedule",
    "current_teams",
    "current_players",
    "current_player_stats",
    "current_team_stats",
    "franchise_history",
}


def _schemas():
    return {schema.path[0]: schema for schema in NBA_CATALOG.schemas}


def test_catalog_surface() -> None:
    schemas = _schemas()
    assert NBA_CATALOG.name == "nba"
    assert set(schemas) == {"nba", "wnba", "gleague"}
    for name, schema in schemas.items():
        expected_tables = TABLES - ({"current_schedule"} if name == "gleague" else set())
        assert {table.name for table in schema.tables} == expected_tables
        assert {fn.Meta.name for fn in schema.functions} == FUNCTIONS | {f"all_{t}" for t in expected_tables}
        for table in schema.tables:
            assert table.function.Meta.name == f"all_{table.name}"


def _key_of(table, kind: str) -> list[tuple[str, ...]]:
    return [
        tuple(name for path in record.definition.column_paths for name in path)
        for record in table.constraints
        if record.definition.kind == kind
    ]


def test_table_keys() -> None:
    expected = {
        "current_standings": ("team_id",),
        "current_scoreboard": ("game_id",),
        "current_schedule": ("game_id",),
        "current_teams": ("team_id",),
        "current_players": ("player_id",),
        "current_player_stats": ("player_id",),
        "current_team_stats": ("team_id",),
    }
    for schema in NBA_CATALOG.schemas:
        for table in schema.tables:
            if table.name == "franchise_history":
                assert table.constraints == ()
                continue
            key = expected[table.name]
            assert _key_of(table, "PRIMARY_KEY") == [key]
            assert _key_of(table, "NOT_NULL") == [(column,) for column in key]


def test_every_object_is_documented() -> None:
    required = {"vgi.title", "vgi.doc_llm", "vgi.doc_md", "vgi.example_queries"}
    for schema in NBA_CATALOG.schemas:
        assert required <= set(schema.tags)
        for table in schema.tables:
            assert required | {"vgi.keywords"} <= set(table.tags), table.name
            assert table.column_comments
        for fn in schema.functions:
            tags = fn.Meta.tags
            assert required | {"vgi.result_columns_schema"} <= set(tags), fn.Meta.name
            assert fn.Meta.examples
            assert len(tags["vgi.result_columns_schema"]) < 4_000


def test_examples_target_their_own_schema() -> None:
    for schema in NBA_CATALOG.schemas:
        name = schema.path[0]
        for fn in schema.functions:
            for example in fn.Meta.examples:
                assert f"nba.{name}.{fn.Meta.name}" in example.sql, (name, fn.Meta.name)
                assert not re.search(r"\{[a-z_]+\}", example.sql)


def test_arguments_are_documented_and_named_safely() -> None:
    reserved = {"from", "to", "end", "interval", "order", "group", "limit"}
    for surface in SURFACES.values():
        for fn in surface.functions:
            for parameter in fn.get_metadata().parameters:
                assert parameter.name not in reserved
                assert parameter.description


def test_catalog_discovery_contract_is_machine_readable() -> None:
    examples = json.loads(NBA_CATALOG.tags["vgi.executable_examples"])
    tasks = json.loads(NBA_CATALOG.tags["vgi.agent_test_tasks"])
    assert all({"name", "description", "sql", "expected_result"} <= example.keys() for example in examples)
    assert 6 <= len(tasks) <= 10
    assert all(set(task) == {"name", "prompt"} for task in tasks)
    assert len({task["name"] for task in tasks}) == len(tasks)


def test_agent_test_file_matches_catalog_tasks() -> None:
    text = (ROOT / "vgi-agent-tests.yaml").read_text()
    names = re.findall(r"^  - name: (\S+)$", text, re.MULTILINE)
    tasks = {task["name"] for task in json.loads(NBA_CATALOG.tags["vgi.agent_test_tasks"])}
    assert set(names) == tasks
    assert text.count("    check_sql:") == len(names)
    assert "reference_sql" not in text


@pytest.mark.parametrize("schema", ["nba", "wnba", "gleague"])
def test_cardinality_estimates(schema: str) -> None:
    for fn in SURFACES[schema].functions:
        estimate = fn.cardinality(None)
        assert estimate.estimate >= 1
        assert estimate.max is None or estimate.max >= estimate.estimate
