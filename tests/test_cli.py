from pathlib import Path

import pytest

from nouls.cli import app
from nouls.store import Store
from tests.conftest import PYTHON, FakeClient

pytestmark = pytest.mark.unit


def run(*tokens: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        app(list(tokens))
    code = exit_info.value.code
    return code if isinstance(code, int) else 0


def stored(tmp_path: Path) -> Store:
    return Store(tmp_path / "xdg" / "nouls.db")


def test_check_prints_findings_and_fails_on_errors(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text(PYTHON)
    assert run("check", str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "app.py:5:9: error [unit_mismatch]" in out
    assert len(client.calls) == 2


def test_check_passes_when_nothing_fires(tmp_path: Path, client: FakeClient) -> None:
    (tmp_path / "clean.py").write_text("def total(items):\n    return sum(items)\n")
    assert run("check", str(tmp_path / "clean.py")) == 0
    assert len(client.calls) == 1


def test_check_hides_probability_when_configured(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text(PYTHON)
    (tmp_path / "nouls.yaml").write_text("show_probability: false\n")
    run("check")
    assert capsys.readouterr().out.rstrip().endswith("without conversion")
    assert client.calls


def test_check_rejects_missing_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("check", str(tmp_path / "nope")) == 2
    assert "no such file or directory" in capsys.readouterr().err


def test_rules_lists_scope_and_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "nouls.yaml").write_text(
        "rules:\n  unit_mismatch:\n    threshold: 0.95\n    languages: [python]\n"
        "  mixed_abstraction:\n    enabled: false\n"
    )
    run("rules")
    out = capsys.readouterr().out
    assert "unit_mismatch (error, threshold 0.95, python)" in out
    assert "test_slow (warning, threshold 0.8, all languages in 16 file patterns)" in out
    assert "mixed_abstraction" not in out


def test_label_rejects_unknown_rules_and_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "app.py"
    source.write_text(PYTHON)
    assert run("label", str(source), "5", "made_up", "real") == 2
    assert run("label", str(source), "1", "unit_mismatch", "real") == 2
    assert run("label", str(tmp_path / "notes.txt"), "1", "unit_mismatch", "real") == 2
    err = capsys.readouterr().err
    assert "unknown rule made_up" in err
    assert "no function contains" in err


def test_label_rejects_non_positive_lines(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(PYTHON)
    assert run("label", str(source), "0", "unit_mismatch", "real") != 0
    assert not stored(tmp_path).query("SELECT * FROM labels")


def test_review_labels_each_sampled_function(
    tmp_path: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text(PYTHON)
    run("check")
    answers = iter(["y", "n"])
    monkeypatch.setattr("nouls.cli.Prompt.ask", lambda *_, **__: next(answers))
    assert run("review", "unit_mismatch") == 0
    labels = stored(tmp_path).query("SELECT real FROM labels ORDER BY real")
    assert labels == [(0,), (1,)]
    assert len(client.calls) == 2


def test_review_skips_and_quits(
    tmp_path: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text(PYTHON)
    run("check")
    answers = iter(["s", "q"])
    monkeypatch.setattr("nouls.cli.Prompt.ask", lambda *_, **__: next(answers))
    assert run("review", "unit_mismatch", "--limit", "5") == 0
    assert stored(tmp_path).query("SELECT * FROM labels") == []
    assert client.calls


def test_review_rejects_unknown_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("review", "made_up") == 2
    assert "unknown rule made_up" in capsys.readouterr().err


def test_serve_starts_the_language_server(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr("nouls.server.server.start_io", lambda: started.append(True))
    assert run("serve") == 0
    assert started == [True]


def test_help_lists_examples(capsys: pytest.CaptureFixture[str]) -> None:
    run("--help")
    out = capsys.readouterr().out
    assert "Usage examples:" in out
    assert "nouls check src/" in out
