# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for the nouls stats commands."""

from pathlib import Path

import pytest

from nouls.output import USAGE_ERROR
from nouls.stats import display, percent, sparkline
from nouls.store import Store, default_path
from tests.conftest import PROJECT_CALL, PYTHON, PYTHON_FUNCTIONS, FakeClient, run

pytestmark = pytest.mark.unit


@pytest.fixture
def checked(tmp_path: Path, client: FakeClient) -> Path:
    """Check app.py once and label its two functions, one real and one false.

    Args:
        tmp_path: The test project.
        client: The fake TypeSafe client.

    Returns:
        The checked project.

    """
    _ = (tmp_path / "app.py").write_text(PYTHON)
    _ = run("check")
    _ = run("label", "app.py", "5", "unit_mismatch", "real")
    _ = run("label", "app.py", "9", "unit_mismatch", "false")
    assert client.calls
    return tmp_path


@pytest.mark.usefixtures("checked")
def test_rules_reports_fire_rate_and_histogram(capsys: pytest.CaptureFixture[str]) -> None:
    """Rules reports fire rate and histogram."""
    _ = capsys.readouterr()
    _ = run("stats", "rules")
    row = next(line for line in capsys.readouterr().out.splitlines() if "unit_mismatch" in line)
    assert "50%" in row
    assert "█" in row


@pytest.mark.usefixtures("checked")
def test_hotspots_lists_the_offending_function(capsys: pytest.CaptureFixture[str]) -> None:
    """Hotspots lists the offending function."""
    _ = capsys.readouterr()
    _ = run("stats", "hotspots", "--limit", "5")
    out = capsys.readouterr().out
    assert "app.py:5" in out
    assert "expired" in out


@pytest.mark.usefixtures("checked")
def test_cost_counts_cache_hits(client: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Cost counts cache hits."""
    _ = run("check")
    _ = capsys.readouterr()
    _ = run("stats", "cost", "--price-per-million", "1")
    row = next(line for line in capsys.readouterr().out.splitlines() if "│ 20" in line)
    assert "50%" in row
    assert "$0.0030" in row
    assert len(client.calls) == PYTHON_FUNCTIONS + PROJECT_CALL


@pytest.mark.usefixtures("checked")
def test_thresholds_scores_labels(capsys: pytest.CaptureFixture[str]) -> None:
    """Thresholds scores labels."""
    _ = capsys.readouterr()
    assert run("stats", "thresholds", "unit_mismatch") == 0
    out = capsys.readouterr().out
    assert "unit_mismatch  1 real, 1 false" in out
    assert "0.80 ◂ current" in out


def test_thresholds_asks_reworded_questions(
    checked: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Thresholds asks reworded questions."""
    _ = (checked / "nouls.yaml").write_text(
        "rules:\n  unit_mismatch:\n    question: Are seconds added to milliseconds?\n"
    )
    _ = capsys.readouterr()
    _ = run("stats", "thresholds")
    assert "2 unanswered" in capsys.readouterr().out
    _ = run("stats", "thresholds", "--ask")
    assert "unanswered" not in capsys.readouterr().out
    assert [call.questions for call in client.calls[-2:]] == [{"unit_mismatch"}] * 2


def test_thresholds_rejects_unknown_rules(capsys: pytest.CaptureFixture[str]) -> None:
    """Thresholds rejects unknown rules."""
    assert run("stats", "thresholds", "made_up") == USAGE_ERROR
    assert "there is no rule called made_up" in capsys.readouterr().err


def test_helpers_format_values(tmp_path: Path) -> None:
    """Helpers format values."""
    assert sparkline([0, 5, 10]) == " ▄█"
    assert sparkline([0, 0]) == "  "
    assert percent(None) == "n/a"
    assert percent(0.5) == "50%"
    assert display(str(tmp_path / "src" / "a.py")) == "src/a.py"
    assert display("/elsewhere/a.py") == "/elsewhere/a.py"


def test_default_store_follows_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default store follows xdg."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert default_path() == tmp_path / "cache" / "nouls" / "nouls.db"
    assert Store(default_path()).path.exists()
