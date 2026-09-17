from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nouls.analyser import Analyser
from nouls.cli import discover, render
from nouls.config import load_config
from nouls.units import extract_units

PYTHON = """import time


class Session:
    def expired(self, timeout_s: int) -> bool:
        return time.time() * 1000 - self.started > timeout_s


def total(items: list[int]) -> int:
    return sum(items)
"""


@dataclass
class Answer:
    noul: float


@dataclass
class Response:
    nouls: dict[str, Answer]


@dataclass
class FakeClient:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def system_one(self, state: dict[str, str], questions: dict[str, Any], model: str) -> Response:
        self.calls.append(state)
        flagged = "* 1000" in state["function"]
        return Response({name: Answer(0.95 if flagged and name == "unit_mismatch" else 0.05) for name in questions})


@pytest.fixture
def config(tmp_path: Path):
    return load_config(tmp_path)


async def test_flags_only_the_offending_function(config) -> None:
    client = FakeClient()
    findings = await Analyser(config, client).analyse(PYTHON, "python")
    assert [(f.rule, f.severity, f.span.line, f.span.column) for f in findings] == [("unit_mismatch", "error", 4, 8)]
    assert len(client.calls) == 2


async def test_unchanged_functions_are_cached(config) -> None:
    client = FakeClient()
    analyser = Analyser(config, client)
    await analyser.analyse(PYTHON, "python")
    await analyser.analyse(PYTHON.replace("sum(items)", "sum(items) + 0"), "python")
    assert len(client.calls) == 3


async def test_project_yaml_disables_and_scopes_rules(tmp_path: Path) -> None:
    (tmp_path / "nouls.yaml").write_text(
        "threshold: 0.99\nrules:\n  unit_mismatch:\n    threshold: 0.9\n  docstring_drift:\n    enabled: false\n"
        "  go_only:\n    question: Is this Go?\n    message: Go\n    languages: [go]\n"
    )
    config = load_config(tmp_path / "nested")
    python_rules = config.rules_for("python")
    assert "docstring_drift" not in python_rules
    assert "go_only" not in python_rules
    assert "go_only" in config.rules_for("go")
    findings = await Analyser(config, FakeClient()).analyse(PYTHON, "python")
    assert [f.rule for f in findings] == ["unit_mismatch"]


@pytest.mark.parametrize(
    ("language", "source", "expected"),
    [
        ("go", "package x\nfunc (s S) Run() {}\nfunc Stop() {}\n", 2),
        ("typescript", "function f() {}\nclass A { m() {} }\n", 2),
        ("rust", "fn f() {}\nimpl A { fn g(&self) {} }\n", 2),
        ("lua", "local function f() end\nfunction M.g() end\n", 2),
        ("cpp", "int f(int x) {\n  return x;\n}\n", 1),
        ("ruby", "class A\n  def a; end\n  def self.b; end\nend\n", 2),
    ],
)
def test_units_are_found_from_yaml_node_types(config, language: str, source: str, expected: int) -> None:
    units = extract_units(source, config.languages[language])
    assert len(units) == expected
    assert all(unit.span.end_column > unit.span.column for unit in units)


def test_discover_skips_excluded_and_unknown_files(config, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("")
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("")
    assert [(p.name, lang) for p, lang in discover([tmp_path], config)] == [("app.py", "python")]


async def test_render_is_one_based(config) -> None:
    findings = await Analyser(config, FakeClient()).analyse(PYTHON, "python")
    assert render(Path("a.py"), findings[0]).startswith("a.py:5:9: error [unit_mismatch]")
