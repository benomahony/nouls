# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for reporting broken configuration, and for keeping the store's queries read only."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from nouls.config import ConfigError, load_config
from nouls.output import USAGE_ERROR
from nouls.server import NoulsServer, lint
from nouls.store import Store
from tests.conftest import run

if TYPE_CHECKING:
    from lsprotocol import types

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("yaml", "problem"),
    [
        ("- rules\n", "must be a map of settings, but its top level is a list"),
        ("rules:\n  x: [1\n", "is not valid YAML on line 3"),
        ("1: true\n", "must be a map of settings"),
        ("rules:\n  mine:\n    question: Is it?\n", "rules.mine.message: Field required"),
        ("threshold: 2\nconcurrency: 0\n", "has 2 invalid settings: threshold"),
        ("exclude: ['']\n", "exclude.0: String should have at least 1 character"),
        ("languages:\n  sql:\n    grammar: sql\n", "cannot find functions in sql"),
        (
            (
                "languages:\n  c:\n    grammar: c\n    units: [function_definition]\n"
                "    attached: [function_definition]\n"
            ),
            "function_definition is listed in both units and attached",
        ),
        ("rules:\n  cwe_1064:\n    limit: -1\n", "needs a limit of 0 or more"),
    ],
)
def test_broken_configs_are_reported_with_how_to_fix_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], yaml: str, problem: str
) -> None:
    """Broken configs are reported with how to fix them, not as a traceback."""
    _ = (tmp_path / "nouls.yaml").write_text(yaml)
    assert run("rules") == USAGE_ERROR
    err = capsys.readouterr().err
    assert err.startswith(f"nouls: {tmp_path / 'nouls.yaml'} ")
    assert problem in err
    assert "Traceback" not in err


def test_a_missing_config_file_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    """A missing config file is reported."""
    assert run("rules", "--config", "missing.yaml") == USAGE_ERROR
    assert "missing.yaml cannot be read" in capsys.readouterr().err


def test_a_store_that_is_a_directory_is_reported(tmp_path: Path) -> None:
    """A store that is a directory is reported."""
    _ = (tmp_path / "nouls.yaml").write_text(f"store: {tmp_path}\n")
    with pytest.raises(ConfigError, match="which is a directory"):
        _ = load_config(tmp_path).store_path()


@pytest.mark.usefixtures("client")
async def test_the_language_server_warns_about_a_broken_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The language server warns about a broken config, then recovers once it is fixed."""
    _ = (tmp_path / "nouls.yaml").write_text("threshold: 2\n")
    server = NoulsServer()
    shown: list[types.ShowMessageParams] = []

    def workspace(_server: NoulsServer) -> SimpleNamespace:
        return SimpleNamespace(root_path=str(tmp_path))

    monkeypatch.setattr(NoulsServer, "workspace", property(workspace))
    monkeypatch.setattr(server, "window_show_message", shown.append)
    await lint(server, (tmp_path / "app.py").as_uri())
    ((message,),) = [[m.message for m in shown]]
    assert "threshold: Input should be less than or equal to 1" in message
    _ = (tmp_path / "nouls.yaml").write_text("threshold: 0.9\n")
    assert server.analyser.config.threshold == pytest.approx(0.9)


def test_queries_cannot_write_to_the_store(store: Store) -> None:
    """Queries cannot write to the store, even when they start with WITH."""
    store.label("unit_mismatch", "a" * 32, real=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        _ = store.query(tuple[int], "WITH gone AS (SELECT 1) DELETE FROM labels RETURNING 1")
    assert store.labels(["a" * 32]) == {("unit_mismatch", "a" * 32): True}
    store.label("unit_mismatch", "a" * 32, real=False)
    assert store.labels(["a" * 32]) == {("unit_mismatch", "a" * 32): False}
