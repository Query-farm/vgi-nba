"""Structural guard for the connector's read-only upstream boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "vgi_nba"
WRITE_VERBS = {"post", "put", "patch", "delete", "request", "stream", "send"}


def test_no_http_write_calls() -> None:
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_VERBS
            ):
                offenders.append(f"{path.name}: .{node.func.attr}()")
    assert offenders == [], f"write-shaped HTTP calls found: {offenders}"


def test_api_module_is_the_only_http_chokepoint() -> None:
    users = sorted(path.name for path in PACKAGE.rglob("*.py") if "httpx" in path.read_text())
    assert users == ["api.py"]
