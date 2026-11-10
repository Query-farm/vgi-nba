"""Release metadata and standalone launchers must stay in sync."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("nba_worker.py", "serve.py")
_BLOCK = re.compile(r"^# /// script$(.+?)^# ///$", re.MULTILINE | re.DOTALL)


def _name(requirement: str) -> str:
    return re.split(r"[\[><=!~;\s]", requirement, maxsplit=1)[0].strip().lower()


def _script_dependencies(path: Path) -> set[str]:
    match = _BLOCK.search(path.read_text())
    assert match is not None, f"{path.name} has no PEP-723 script header"
    body = "".join(
        line.removeprefix("# ").removeprefix("#") for line in match.group(1).splitlines(keepends=True)
    )
    return {_name(spec) for spec in tomllib.loads(body)["dependencies"]}


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


@pytest.fixture(scope="module")
def project_dependencies(project: dict) -> set[str]:
    return {_name(spec) for spec in project["dependencies"]}


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_header_matches_runtime_dependencies(script: str, project_dependencies: set[str]) -> None:
    assert _script_dependencies(ROOT / script) == project_dependencies


def test_license_uses_pep_639_without_deprecated_classifier(project: dict) -> None:
    assert project["license"] == "MIT"
    assert not any(value.startswith("License ::") for value in project.get("classifiers", []))
