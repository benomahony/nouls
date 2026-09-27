# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for rules measured from the syntax tree, and rules about whole files."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from nouls.analyser import Analyser, file_kind, unit_hash
from nouls.config import Config, Rule, load_config
from nouls.metrics import Metric
from nouls.output import USAGE_ERROR
from nouls.store import Store
from nouls.units import extract_units, file_metrics
from tests.conftest import APP, PYTHON, FakeClient, as_client, run

pytestmark = pytest.mark.unit

PYTHON_BRANCHY = """def f(self, a, *args, b=1, **kw):
    if a and b or c:
        for x in y:
            while z:
                pass
    elif d:
        return 1 if e else 2
    try:
        g()
    except E:
        pass
"""


@pytest.mark.parametrize(
    ("language", "source", "expected"),
    [
        (
            "python",
            PYTHON_BRANCHY,
            {"parameters": 4, "complexity": 9, "nesting": 3, "variadic_parameters": 2},
        ),
        (
            "javascript",
            (
                "function f(a, ...rest) { if (a && b || c) { for (const x of y) { while (z) {} } }"
                " else if (d) { return e ? 1 : 2; } try { g(); } catch (e) {} }"
            ),
            {"parameters": 2, "complexity": 9, "nesting": 3, "empty_blocks": 2},
        ),
        (
            "go",
            (
                "package p\nfunc f(a int, rest ...int) int { if a > 0 && b || c { for i := range y "
                "{ _ = i } } else if d { return 1 }; goto L\nL: return 0 }"
            ),
            {"parameters": 2, "complexity": 6, "nesting": 2, "variadic_parameters": 1, "gotos": 1},
        ),
        (
            "rust",
            (
                "fn f(&self, a: i32) -> i32 { if a > 0 && b || c { for x in y { while z {} } }"
                " else if d { return 1; } match q { 1 => 2, _ => 3 }; 0 }"
            ),
            {"parameters": 1, "complexity": 9, "nesting": 3, "empty_blocks": 1},
        ),
        (
            "c",
            "int f(void) { if (a) {} else if (b) { if (c) { goto L; } } L: return 0; }",
            {"parameters": 0, "complexity": 4, "nesting": 2, "empty_blocks": 1, "gotos": 1},
        ),
    ],
)
def test_functions_are_measured_the_same_way_in_every_language(
    config: Config, language: str, source: str, expected: dict[Metric, int]
) -> None:
    """Receivers such as self are not parameters, and else if chains do not nest."""
    (unit,) = extract_units(source, config.language(language))
    assert {name: unit.metrics[name] for name in expected} == expected


def test_lines_of_code_skip_blank_and_comment_lines(config: Config) -> None:
    """Lines of code skip blank and comment lines."""
    text = "# header\n\nimport os\n\n\ndef f():\n    # note\n    return os.sep\n"
    assert file_metrics(text, config.language("python")) == {"lines": 3}


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"question": "Is it?", "metric": "parameters", "limit": 1}, "both a question and"),
        ({}, "or neither"),
        ({"metric": "parameters"}, "needs a limit of 0 or more"),
        ({"question": "Is it?", "limit": 3}, "has a limit but no metric"),
        ({"metric": "lines", "limit": 10}, "needs scope: file"),
        ({"metric": "nesting", "limit": 1, "scope": "file"}, "needs scope: function"),
    ],
)
def test_a_rule_decides_exactly_one_way(fields: dict[str, object], problem: str) -> None:
    """A rule decides exactly one way."""
    with pytest.raises(ValidationError, match=problem):
        _ = Rule.model_validate({"message": "Fix it", **fields})


@pytest.fixture
def measured(tmp_path: Path) -> Config:
    """Load a config with one parameter limit and one file length limit.

    Returns:
        The configuration.

    """
    _ = (tmp_path / "nouls.yaml").write_text(
        "rules:\n"
        "  two_params:\n    metric: parameters\n    limit: 0\n"
        "    message: Too many parameters\n    severity: error\n"
        "  short_files:\n    metric: lines\n    limit: 5\n    scope: file\n"
        "    message: Too long\n"
    )
    return load_config(tmp_path)


async def test_metric_rules_fire_with_their_measurement_and_are_never_asked(
    measured: Config, store: Store
) -> None:
    """Metric rules fire with their measurement and are never asked."""
    client = FakeClient()
    findings = await Analyser(measured, as_client(client), store).analyse(PYTHON, "python", APP)
    found = {(f.rule, f.span.line, f.describe(show_probability=True)) for f in findings}
    assert ("two_params", 4, "Too many parameters (parameters: 1, limit 0)") in found
    assert ("two_params", 8, "Too many parameters (parameters: 1, limit 0)") in found
    assert ("short_files", 0, "Too long (lines of code: 6, limit 5)") in found
    assert not any({"two_params", "short_files"} & call.questions for call in client.calls)


async def test_file_rules_are_asked_once_about_the_whole_file(config: Config, store: Store) -> None:
    """File rules are asked once about the whole file."""
    client = FakeClient()
    _ = await Analyser(config, as_client(client), store).analyse(PYTHON, "python", APP)
    (whole,) = [call for call in client.calls if "file" in call.state]
    assert whole.state == {"language": "python", "file": PYTHON}
    file_questions = {
        name for name, rule in config.rules_for("python", APP, "file").items() if rule.question
    }
    assert whole.questions == file_questions
    assert "cwe_1115" in whole.questions
    assert not any("cwe_1115" in call.questions for call in client.calls if call is not whole)


def test_label_records_a_verdict_on_a_whole_file(tmp_path: Path) -> None:
    """Label records a verdict on a whole file."""
    _ = (tmp_path / "app.py").write_text(PYTHON)
    assert run(["label", "app.py", "1", "cwe_1115", "false"]) == 0
    store = Store(tmp_path / "xdg" / "nouls.db")
    assert store.labels([unit_hash(file_kind("python"), PYTHON)]) == {
        ("cwe_1115", unit_hash(file_kind("python"), PYTHON)): False
    }


def test_rules_lists_metric_rules_with_their_limit(capsys: pytest.CaptureFixture[str]) -> None:
    """Rules lists metric rules with their limit."""
    assert run(["rules"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "cwe_1064 (info, all languages): Is the number of parameters above 7?" in lines
    assert "cwe_1121 (warning, all languages): Is the cyclomatic complexity above 10?" in lines
    assert any(
        line.startswith("cwe_1115 (info, threshold 0.8, all languages, each file):")
        for line in lines
    )


def test_thresholds_explains_metric_rules_have_none(capsys: pytest.CaptureFixture[str]) -> None:
    """Thresholds explains metric rules have none."""
    assert run(["stats", "thresholds", "cwe_1064"]) == USAGE_ERROR
    assert "Change its limit" in capsys.readouterr().err
