# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for project scoped rules that read configuration files."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nouls.analyser import Analyser
from nouls.config import Config, Rule, find_root, load_config, project_files
from nouls.output import USAGE_ERROR
from nouls.server import lint
from nouls.settings import settings_in
from nouls.store import Store
from tests.conftest import PYTHON, FakeClient, FakeServer, as_client, as_server, run

pytestmark = pytest.mark.unit

LAX = '[tool.ruff.lint]\nselect = ["ALL"]\nignore = [\n    "E501",\n    "D203",\n]\n'


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


def test_project_and_setting_rules_are_not_asked_about_functions(config: Config) -> None:
    """Project and setting rules are not asked about functions."""
    assert set(config.scoped_rules("project")) == {
        "unscheduled_static_analysis",
        "cwe_1127",
        "ext_build_004",
        "ext_delivery_003",
        "ext_verify_004",
        "ext_human_004",
    }
    assert set(config.scoped_rules("setting")) == {"relaxed_warnings"}
    scoped = {"unscheduled_static_analysis", "relaxed_warnings"}
    assert not scoped & set(config.rules_for("python", Path("app.py")))


async def test_a_project_without_strict_analysis_fires_on_its_first_config_file(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """A project without strict analysis fires on its first config file."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    client = FakeClient()
    ((anchor, finding),) = await Analyser(config, as_client(client), store).analyse_project(
        tmp_path
    )
    assert anchor == tmp_path / "pyproject.toml"
    assert finding.rule == "unscheduled_static_analysis"
    assert client.calls[0].state == {"files": {"pyproject.toml": LAX}}


async def test_project_answers_are_cached_until_a_config_file_changes(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """Project answers are cached until a config file changes."""
    client = FakeClient()
    analyser = Analyser(config, as_client(client), store)
    assert await analyser.analyse_project(tmp_path) == []
    asked = len(client.calls)
    _ = await analyser.analyse_project(tmp_path)
    assert len(client.calls) == asked
    _ = (tmp_path / "ruff.toml").write_text("")
    _ = await analyser.analyse_project(tmp_path)
    assert len(client.calls) == asked + 1


async def test_a_project_without_config_files_fires_on_its_root(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """A project without config files fires on its root."""
    (tmp_path / "pyproject.toml").unlink()
    ((anchor, _),) = await Analyser(config, as_client(FakeClient()), store).analyse_project(
        tmp_path
    )
    assert anchor == tmp_path


def test_settings_carry_the_keys_they_sit_under(tmp_path: Path) -> None:
    """Settings carry the keys they sit under, one per line that sets something."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _ = (workflows / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: ruff check .\n        continue-on-error: true\n"
    )
    _ = (tmp_path / "tsconfig.json").write_text(
        '{\n  "compilerOptions": {\n    "strict": false\n  }\n}\n'
    )
    _ = (tmp_path / "Makefile").write_text("# lint\nlint:\n\truff check .\n")
    found = {
        name: [(s.span.line, s.keys, s.text) for s in settings_in(tmp_path, tmp_path / name)]
        for name in ["pyproject.toml", ".github/workflows/ci.yml", "tsconfig.json", "Makefile"]
    }
    assert found["pyproject.toml"] == [
        (1, "tool.ruff.lint.select", 'select = ["ALL"]'),
        (3, "tool.ruff.lint.ignore", '"E501",'),
        (4, "tool.ruff.lint.ignore", '"D203",'),
    ]
    assert found[".github/workflows/ci.yml"] == [
        (3, "jobs.test.steps.run", "- run: ruff check ."),
        (4, "jobs.test.steps.continue-on-error", "continue-on-error: true"),
    ]
    assert found["tsconfig.json"] == [(2, "compilerOptions.strict", '"strict": false')]
    assert found["Makefile"] == [(2, "", "ruff check .")]


async def test_each_relaxed_setting_is_its_own_finding(
    tmp_path: Path, config: Config, store: Store
) -> None:
    """Each relaxed setting is its own finding, on its own line."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    findings = await Analyser(config, as_client(FakeClient()), store).analyse_settings(tmp_path)
    assert [(path.name, f.rule, f.span.line, f.span.column) for path, f in findings] == [
        ("pyproject.toml", "relaxed_warnings", 3, 4),
        ("pyproject.toml", "relaxed_warnings", 4, 4),
    ]
    assert len({f.unit_hash for _, f in findings}) == len(findings)


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
def test_check_reports_every_relaxed_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check reports every relaxed setting at its line, and the project finding on line 1."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    assert run("check", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "pyproject.toml:1:1: warning [unscheduled_static_analysis]" in out
    assert "pyproject.toml:4:5: warning [relaxed_warnings]" in out
    assert "pyproject.toml:5:5: warning [relaxed_warnings]" in out


@pytest.mark.usefixtures("client")
def test_labelling_one_setting_false_silences_only_that_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Labelling one setting false silences only that line."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    assert run("label", "pyproject.toml", "4", "relaxed_warnings", "false") == 0
    assert "Labelled relaxed_warnings on pyproject.toml:4 as false" in capsys.readouterr().out
    _ = run("check", str(tmp_path))
    out = capsys.readouterr().out
    assert "pyproject.toml:4:5" not in out
    assert "pyproject.toml:5:5: warning [relaxed_warnings]" in out


def test_labelling_a_line_that_sets_nothing_explains_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Labelling a line that sets nothing explains why."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    assert run("label", "pyproject.toml", "1", "relaxed_warnings", "false") == USAGE_ERROR
    assert "line 1 of pyproject.toml does not set anything" in capsys.readouterr().err


@pytest.mark.usefixtures("client")
def test_labelling_the_project_false_silences_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Labelling the project false silences it."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    assert run("label", "pyproject.toml", "1", "unscheduled_static_analysis", "false") == 0
    assert "Labelled unscheduled_static_analysis on the project" in capsys.readouterr().out
    _ = run("check", str(tmp_path))
    assert "unscheduled_static_analysis" not in capsys.readouterr().out


def test_rules_lists_project_and_setting_scopes(capsys: pytest.CaptureFixture[str]) -> None:
    """Rules lists project and setting scopes."""
    _ = run("rules")
    out = capsys.readouterr().out
    assert (
        "unscheduled_static_analysis (warning, threshold 0.8, project in 40 file patterns)" in out
    )
    assert "relaxed_warnings (warning, threshold 0.6, each setting in 40 file patterns)" in out


async def test_opening_a_config_file_publishes_its_findings(
    ls: FakeServer,
    tmp_path: Path,
) -> None:
    """Opening a config file publishes its project and setting findings."""
    pyproject = tmp_path / "pyproject.toml"
    _ = pyproject.write_text(LAX)
    ls.document = SimpleNamespace(path=str(pyproject), source=LAX, version=1)
    await lint(as_server(ls), "file:///pyproject.toml")
    (params,) = ls.published
    assert [(d.code, d.range.start.line) for d in params.diagnostics] == [
        ("unscheduled_static_analysis", 0),
        ("relaxed_warnings", 3),
        ("relaxed_warnings", 4),
    ]


async def test_function_files_do_not_show_project_or_setting_findings(
    ls: FakeServer,
    tmp_path: Path,
) -> None:
    """Function files do not show project or setting findings."""
    _ = (tmp_path / "pyproject.toml").write_text(LAX)
    await lint(as_server(ls), "file:///app.py")
    (params,) = ls.published
    assert [diagnostic.code for diagnostic in params.diagnostics] == ["unit_mismatch"]
