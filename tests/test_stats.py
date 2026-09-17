from pathlib import Path

import pytest

from nouls.cli import app
from nouls.stats import display, percent, sparkline
from nouls.store import Store, default_path
from tests.conftest import PYTHON, FakeClient

pytestmark = pytest.mark.unit


def run(*tokens: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        app(list(tokens))
    code = exit_info.value.code
    return code if isinstance(code, int) else 0


@pytest.fixture
def checked(tmp_path: Path, client: FakeClient) -> Path:
    (tmp_path / "app.py").write_text(PYTHON)
    run("check")
    run("label", "app.py", "5", "unit_mismatch", "real")
    run("label", "app.py", "9", "unit_mismatch", "false")
    assert client.calls
    return tmp_path


def test_rules_reports_fire_rate_and_histogram(
    checked: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    run("stats", "rules")
    row = next(line for line in capsys.readouterr().out.splitlines() if "unit_mismatch" in line)
    assert "50%" in row
    assert "█" in row


def test_hotspots_lists_the_offending_function(
    checked: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    run("stats", "hotspots", "--limit", "5")
    out = capsys.readouterr().out
    assert "app.py:5" in out
    assert "expired" in out


def test_cost_counts_cache_hits(
    checked: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run("check")
    capsys.readouterr()
    run("stats", "cost", "--price-per-million", "1")
    row = next(line for line in capsys.readouterr().out.splitlines() if "│ 20" in line)
    assert "50%" in row
    assert "$0.0022" in row
    assert len(client.calls) == 2


def test_thresholds_scores_labels(checked: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert run("stats", "thresholds", "unit_mismatch") == 0
    out = capsys.readouterr().out
    assert "unit_mismatch  1 real, 1 false" in out
    assert "0.80 ◂ current" in out


def test_thresholds_asks_reworded_questions(
    checked: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    (checked / "nouls.yaml").write_text(
        "rules:\n  unit_mismatch:\n    question: Are seconds added to milliseconds?\n"
    )
    capsys.readouterr()
    run("stats", "thresholds")
    assert "2 unanswered" in capsys.readouterr().out
    run("stats", "thresholds", "--ask")
    assert "unanswered" not in capsys.readouterr().out
    assert [call["questions"] for call in client.calls[-2:]] == [{"unit_mismatch"}] * 2


def test_thresholds_rejects_unknown_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("stats", "thresholds", "made_up") == 2
    assert "unknown rule made_up" in capsys.readouterr().err


def test_helpers_format_values(tmp_path: Path) -> None:
    assert sparkline([0, 5, 10]) == " ▄█"
    assert sparkline([0, 0]) == "  "
    assert percent(None) == "n/a"
    assert percent(0.5) == "50%"
    assert display(str(tmp_path / "src" / "a.py")) == "src/a.py"
    assert display("/elsewhere/a.py") == "/elsewhere/a.py"


def test_default_store_follows_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert default_path() == tmp_path / "cache" / "nouls" / "nouls.db"
    assert Store(default_path()).path.exists()
