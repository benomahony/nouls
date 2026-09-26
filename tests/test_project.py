# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for project scoped rules that read configuration files."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nouls.analyser import Analyser
from nouls.config import Config, Rule, find_root, load_config, project_files
from nouls.server import lint
from nouls.store import Store
from tests.conftest import PROJECT_CALL, PYTHON, FakeClient, FakeServer, as_client, as_server, run

pytestmark = pytest.mark.unit

LAX = '[tool.ruff]\nselect = ["E"]\n'


def test_project_files_keep_pattern_order_and_include_dotfiles(tmp_path: Path) -> None:
    """Project files keep pattern order and include dotfiles."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _ = (workflows / "ci.yml").write_text("on: push\n")
    _ = (tmp_path / "ruff.toml").write_text("")
    found = project_files(
        tmp_path, ["pyproject.toml", ".github/workflows/*.y*ml", "ruff.toml", "*.toml"]
    )
    assert [path.relative_to(tmp_path).as_posix() for path in found] == [
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "ruff.toml",
    ]


def test_find_root_stops_at_the_repository(tmp_path: Path) -> None:
    """Find root stops at the repository."""
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_root(nested) == tmp_path


def test_project_rules_must_name_their_files() -> None:
    """Project rules must name their files."""
    with pytest.raises(ValidationError, match="add files with globs relative to the project root"):
        _ = Rule(question="Is it strict?", message="Make it strict", scope="project")


def test_project_rules_are_not_asked_about_functions(config: Config) -> None:
    """Project rules are not asked about functions."""
    project = {"relaxed_warnings", "unscheduled_static_analysis"}
    assert not project & set(config.rules_for("python", Path("app.py")))
    assert set(config.project_rules()) == project


async def test_a_lax_project_fires_on_its_first_config_file(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """A lax project fires on its first config file."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    client = FakeClient()
    ((anchor, finding),) = await Analyser(config, as_client(client), store).analyse_project(
        tmp_path
    )
    assert anchor == tmp_path / "pyproject.toml"
    assert finding.rule == "relaxed_warnings"
    assert client.calls[0].state == {"files": {"pyproject.toml": LAX}}


async def test_project_answers_are_cached_until_a_config_file_changes(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """Project answers are cached until a config file changes."""
    client = FakeClient()
    analyser = Analyser(config, as_client(client), store)
    assert await analyser.analyse_project(tmp_path) == []
    _ = await analyser.analyse_project(tmp_path)
    assert len(client.calls) == 1
    _ = (tmp_path / "ruff.toml").write_text("")
    _ = await analyser.analyse_project(tmp_path)
    assert len(client.calls) == 2 * PROJECT_CALL


async def test_a_project_without_config_files_fires_on_its_root(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """A project without config files fires on its root."""
    (tmp_path / "pyproject.toml").unlink()
    ((anchor, _),) = await Analyser(config, as_client(FakeClient()), store).analyse_project(
        tmp_path
    )
    assert anchor == tmp_path


async def test_project_observations_keep_function_observations_on_the_same_file(
    tmp_path: Path, store: Store
) -> None:
    """Project observations keep function observations on the same file."""
    _ = (tmp_path / "nouls.yaml").write_text(
        "rules:\n  whole_app:\n    scope: project\n    question: Is the app lax?\n"
        "    message: Tighten the app\n    files: [app.py]\n"
    )
    app = tmp_path / "app.py"
    _ = app.write_text(PYTHON)
    analyser = Analyser(load_config(tmp_path), as_client(FakeClient()), store)
    _ = await analyser.analyse(PYTHON, "python", app)
    _ = await analyser.analyse_project(tmp_path)
    languages = dict(
        store.query(
            tuple[str, int],
            "SELECT language, COUNT(*) FROM observations WHERE path = ? GROUP BY language",
            (str(app.resolve()),),
        )
    )
    assert languages["python"] > 0
    assert languages["project"] == 1


@pytest.mark.usefixtures("client")
def test_check_reports_a_lax_project_on_its_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check reports a lax project on its config file."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    assert run("check", str(tmp_path)) == 0
    assert "pyproject.toml:1:1: warning [relaxed_warnings]" in capsys.readouterr().out


@pytest.mark.usefixtures("client")
def test_labelling_the_project_false_silences_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Labelling the project false silences it."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    assert run("label", "pyproject.toml", "1", "relaxed_warnings", "false") == 0
    assert "Labelled relaxed_warnings on the project" in capsys.readouterr().out
    _ = run("check", str(tmp_path))
    assert "relaxed_warnings" not in capsys.readouterr().out


def test_rules_lists_project_scope(capsys: pytest.CaptureFixture[str]) -> None:
    """Rules lists project scope."""
    _ = run("rules")
    assert "unscheduled_static_analysis (warning, threshold 0.8, project in 40 file patterns)" in (
        capsys.readouterr().out
    )


async def test_opening_a_config_file_publishes_the_project_finding(
    ls: FakeServer,
    tmp_path: Path,
) -> None:
    """Opening a config file publishes the project finding."""
    pyproject = tmp_path / "pyproject.toml"
    _ = pyproject.write_text(LAX)
    ls.document = SimpleNamespace(path=str(pyproject), source=LAX, version=1)
    await lint(as_server(ls), "file:///pyproject.toml")
    (params,) = ls.published
    assert [diagnostic.code for diagnostic in params.diagnostics] == ["relaxed_warnings"]


async def test_function_files_do_not_show_the_project_finding(
    ls: FakeServer,
    tmp_path: Path,
) -> None:
    """Function files do not show the project finding."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    await lint(as_server(ls), "file:///app.py")
    (params,) = ls.published
    assert [diagnostic.code for diagnostic in params.diagnostics] == ["unit_mismatch"]
