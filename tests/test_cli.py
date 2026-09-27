# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for the nouls command line."""

import ast
import inspect
import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from nouls import cli
from nouls.config import load_config
from nouls.output import USAGE_ERROR
from nouls.store import Store
from tests.conftest import PROJECT_CALLS, PYTHON, PYTHON_CALLS, FakeClient, run, scripted

pytestmark = pytest.mark.unit

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def stored(tmp_path: Path) -> Store:
    """Open the store the command line writes to in a test.

    Args:
        tmp_path: The test project.

    Returns:
        The store under the test's XDG cache.

    """
    return Store(tmp_path / "xdg" / "nouls.db")


def test_check_prints_findings_and_fails_on_errors(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check prints findings and fails on errors."""
    _ = (tmp_path / "app.py").write_text(PYTHON)
    assert run("check", str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "app.py:5:9: error [unit_mismatch]" in out
    assert len(client.calls) == PYTHON_CALLS + PROJECT_CALLS


def test_check_passes_when_nothing_fires(tmp_path: Path, client: FakeClient) -> None:
    """Check passes when nothing fires."""
    _ = (tmp_path / "clean.py").write_text("def total(items):\n    return sum(items)\n")
    assert run("check", str(tmp_path / "clean.py")) == 0
    assert len(client.calls) == 2 + PROJECT_CALLS, "One function, and the file"


def test_check_hides_probability_when_configured(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check hides probability when configured."""
    _ = (tmp_path / "app.py").write_text(PYTHON)
    _ = (tmp_path / "nouls.yaml").write_text("show_probability: false\n")
    _ = run("check")
    assert capsys.readouterr().out.rstrip().endswith("in the variable names")
    assert client.calls


@pytest.mark.usefixtures("client")
def test_check_prints_a_pretty_grouped_summary_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check prints a pretty grouped summary on a terminal."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buffer, force_terminal=True, no_color=True, width=200)
    )
    _ = (tmp_path / "app.py").write_text(PYTHON)
    assert run("check", str(tmp_path)) == 1
    out = ANSI.sub("", buffer.getvalue())
    assert "app.py" in out
    assert "unit_mismatch" in out
    assert "1 error" in out
    assert "across 1 file" in out


@pytest.mark.usefixtures("client")
def test_check_prints_no_issues_found_on_a_clean_terminal_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check prints no issues found on a clean terminal run."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buffer, force_terminal=True, no_color=True, width=200)
    )
    _ = (tmp_path / "clean.py").write_text("def total(items):\n    return sum(items)\n")
    assert run("check", str(tmp_path / "clean.py")) == 0
    assert "No issues found" in buffer.getvalue()


def test_check_rejects_missing_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Check rejects missing paths."""
    assert run("check", str(tmp_path / "nope")) == USAGE_ERROR
    err = capsys.readouterr().err
    assert "nope because it does not exist" in err
    assert "Fix the path" in err


def test_rules_lists_scope_and_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rules lists scope and threshold."""
    _ = (tmp_path / "nouls.yaml").write_text(
        "rules:\n  unit_mismatch:\n    threshold: 0.95\n    languages: [python]\n"
        "  mixed_abstraction:\n    enabled: false\n"
    )
    _ = run("rules")
    out = capsys.readouterr().out
    assert "unit_mismatch (error, threshold 0.95, python)" in out
    assert "test_slow (warning, threshold 0.8, all languages in 30 file patterns)" in out
    assert "mixed_abstraction" not in out


def test_rules_prints_a_table_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules prints a table on a terminal."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buffer, force_terminal=True, no_color=True, width=200)
    )
    _ = (tmp_path / "nouls.yaml").write_text(
        "rules:\n  unit_mismatch:\n    threshold: 0.95\n    languages: [python]\n"
        "  mixed_abstraction:\n    enabled: false\n"
    )
    _ = run("rules")
    out = ANSI.sub("", buffer.getvalue())
    assert "unit_mismatch" in out
    assert "95%" in out
    assert "python" in out
    assert "mixed_abstraction" not in out


def test_rules_explains_how_to_fix_every_rule_being_disabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rules explains how to fix every rule being disabled."""
    names = load_config(tmp_path).rules
    _ = (tmp_path / "nouls.yaml").write_text(
        "rules:\n" + "".join(f"  {name}:\n    enabled: false\n" for name in names)
    )
    assert run("rules") == USAGE_ERROR
    err = capsys.readouterr().err
    assert "every rule is disabled" in err
    assert "Set enabled: true" in err


def test_rules_uses_console_not_bare_print() -> None:
    """Rules uses console not bare print."""
    tree = ast.parse(inspect.getsource(cli.rules))
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "print" not in calls, "rules() must render through the Rich console, not bare print()"


def test_label_rejects_unknown_rules_and_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Label rejects unknown rules and lines."""
    source = tmp_path / "app.py"
    _ = source.write_text(PYTHON)
    assert run("label", str(source), "5", "made_up", "real") == USAGE_ERROR
    assert run("label", str(source), "1", "unit_mismatch", "real") == USAGE_ERROR
    assert run("label", str(tmp_path / "notes.txt"), "1", "unit_mismatch", "real") == USAGE_ERROR
    assert run("label", str(tmp_path / "gone.py"), "1", "unit_mismatch", "real") == USAGE_ERROR
    err = capsys.readouterr().err
    assert "there is no rule called made_up" in err
    assert "line 1 of" in err
    assert "outside every function" in err
    assert "nouls cannot find functions in" in err
    assert "notes.txt" in err
    assert "gone.py because it does not exist" in err


def test_label_rejects_non_positive_lines(tmp_path: Path) -> None:
    """Label rejects non positive lines."""
    source = tmp_path / "app.py"
    _ = source.write_text(PYTHON)
    assert run("label", str(source), "0", "unit_mismatch", "real") != 0
    assert not stored(tmp_path).query(tuple[str, str, int, str], "SELECT * FROM labels")


def test_review_labels_each_sampled_function(
    tmp_path: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review labels each sampled function."""
    _ = (tmp_path / "app.py").write_text(PYTHON)
    _ = run("check")
    monkeypatch.setattr("nouls.cli.Prompt.ask", scripted("y", "n"))
    assert run("review", "unit_mismatch") == 0
    labels = stored(tmp_path).query(tuple[int], "SELECT real FROM labels ORDER BY real")
    assert labels == [(0,), (1,)]
    assert len(client.calls) == PYTHON_CALLS + PROJECT_CALLS


def test_review_skips_and_quits(
    tmp_path: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review skips and quits."""
    _ = (tmp_path / "app.py").write_text(PYTHON)
    _ = run("check")
    monkeypatch.setattr("nouls.cli.Prompt.ask", scripted("s", "q"))
    assert run("review", "unit_mismatch", "--limit", "5") == 0
    assert stored(tmp_path).query(tuple[str, str, int, str], "SELECT * FROM labels") == []
    assert client.calls


def test_review_skips_rows_with_corrupt_probability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review skips rows with corrupt probability."""
    store = stored(tmp_path)
    _ = store.db.execute(
        "INSERT INTO units VALUES (?, ?, ?)", ("a" * 32, "python", "def f(): pass")
    )
    _ = store.db.execute(
        "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "app.py",
            "unit_mismatch",
            "a" * 32,
            "f",
            0,
            "python",
            "jev-latest",
            "b" * 32,
            1.5,
            0.8,
            1,
            "2024-01-01T00:00:00+00:00",
        ),
    )
    assert run("review", "unit_mismatch") == 0
    err = capsys.readouterr().err
    assert "outside 0 to 1" in err
    assert "Run nouls check" in err
    assert not store.query(tuple[str, str, int, str], "SELECT * FROM labels")


def test_review_rejects_unknown_rules(capsys: pytest.CaptureFixture[str]) -> None:
    """Review rejects unknown rules."""
    assert run("review", "made_up") == USAGE_ERROR
    assert "Run nouls rules" in capsys.readouterr().err
    assert run("review", "unit_mismatchh") == USAGE_ERROR
    assert "Did you mean unit_mismatch?" in capsys.readouterr().err


def test_serve_starts_the_language_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve starts the language server."""
    started: list[bool] = []
    monkeypatch.setattr("nouls.server.server.start_io", lambda: started.append(True))
    assert run("serve") == 0
    assert started == [True]


def test_help_lists_examples(capsys: pytest.CaptureFixture[str]) -> None:
    """Help lists examples."""
    _ = run("--help")
    out = capsys.readouterr().out
    assert "Usage examples:" in out
    assert "nouls check src/" in out
