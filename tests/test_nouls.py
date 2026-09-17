from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nouls.analyser import Analyser, unit_hash
from nouls.cli import app, discover, render
from nouls.config import load_config
from nouls.stats import SAMPLE, score
from nouls.store import Store
from nouls.units import extract_units

PYTHON = """import time


class Session:
    def expired(self, timeout_s: int) -> bool:
        return time.time() * 1000 - self.started > timeout_s


def total(items: list[int]) -> int:
    return sum(items)
"""

APP = Path("src/app.py")


@dataclass
class Answer:
    noul: float


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class Response:
    nouls: dict[str, Answer]
    usage: Usage


@dataclass
class FakeClient:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def system_one(self, state: dict[str, str], questions: dict[str, Any], model: str) -> Response:
        self.calls.append({"state": state, "questions": set(questions)})
        flagged = "* 1000" in state["function"]
        return Response(
            {name: Answer(0.95 if flagged and name == "unit_mismatch" else 0.05) for name in questions},
            Usage(100 * len(questions), 0),
        )


@pytest.fixture
def config(tmp_path: Path):
    return load_config(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "cache" / "nouls.db")


async def test_flags_only_the_offending_function(config, store) -> None:
    client = FakeClient()
    findings = await Analyser(config, client, store).analyse(PYTHON, "python", APP)
    assert [(f.rule, f.severity, f.span.line, f.span.column) for f in findings] == [("unit_mismatch", "error", 4, 8)]
    assert len(client.calls) == 2


async def test_unchanged_functions_are_cached_across_processes(config, store) -> None:
    client = FakeClient()
    await Analyser(config, client, store).analyse(PYTHON, "python", APP)
    reopened = Store(store.path)
    await Analyser(config, client, reopened).analyse(PYTHON.replace("sum(items)", "sum(items) + 0"), "python", APP)
    assert len(client.calls) == 3
    ((asked, cached),) = reopened.query("SELECT SUM(asked), SUM(cached) FROM runs")
    assert (asked, cached) == (33, 11)


async def test_rewording_one_rule_only_reasks_that_rule(config, store) -> None:
    client = FakeClient()
    await Analyser(config, client, store).analyse(PYTHON, "python", APP)
    config.rules["unit_mismatch"].question = "Are seconds mixed with milliseconds?"
    await Analyser(config, client, store).analyse(PYTHON, "python", APP)
    assert [call["questions"] for call in client.calls[2:]] == [{"unit_mismatch"}, {"unit_mismatch"}]


async def test_findings_labelled_false_are_suppressed(config, store) -> None:
    analyser = Analyser(config, FakeClient(), store)
    findings = await analyser.analyse(PYTHON, "python", APP)
    store.label("unit_mismatch", findings[0].unit_hash, False)
    assert await analyser.analyse(PYTHON, "python", APP) == []
    ((fired,),) = store.query("SELECT SUM(fired) FROM observations")
    assert fired == 0


async def test_project_yaml_disables_and_scopes_rules(tmp_path: Path) -> None:
    (tmp_path / "nouls.yaml").write_text(
        "threshold: 0.99\nrules:\n  unit_mismatch:\n    threshold: 0.9\n  docstring_drift:\n    enabled: false\n"
        "  go_only:\n    question: Is this Go?\n    message: Go\n    languages: [go]\n"
    )
    config = load_config(tmp_path / "nested")
    python_rules = config.rules_for("python", APP)
    assert "docstring_drift" not in python_rules
    assert "go_only" not in python_rules
    assert "go_only" in config.rules_for("go", Path("main.go"))
    findings = await Analyser(config, FakeClient(), Store(tmp_path / "nouls.db")).analyse(PYTHON, "python", APP)
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


async def test_render_is_one_based(config, store) -> None:
    findings = await Analyser(config, FakeClient(), store).analyse(PYTHON, "python", APP)
    assert render(Path("a.py"), findings[0], True).startswith("a.py:5:9: error [unit_mismatch]")
    assert render(Path("a.py"), findings[0], True).endswith("(95%)")
    assert render(Path("a.py"), findings[0], False).endswith("without conversion")


def test_desiderata_rules_only_apply_to_test_files(config) -> None:
    desiderata = {name for name in config.rules if name.startswith("test_")}
    assert len(desiderata) == 12
    for path in [
        "tests/test_orders.py",
        "orders_test.go",
        "web/cart.spec.tsx",
        "src/OrderTest.java",
        "crate/tests/api.rs",
    ]:
        language = config.language_for(Path(path))
        assert language is not None
        assert desiderata <= set(config.rules_for(language, Path(path))), path
    for path in ["src/orders.py", "orders.go", "web/cart.tsx", "src/lib.rs"]:
        language = config.language_for(Path(path))
        assert language is not None
        assert not desiderata & set(config.rules_for(language, Path(path))), path


def test_score_counts_precision_and_recall() -> None:
    pairs = [(0.95, True), (0.9, False), (0.7, True), (0.2, False)]
    assert score(pairs, 0.8) == (2, 0.5, 0.5)
    assert score(pairs, 0.5) == (3, 2 / 3, 1.0)
    assert score(pairs, 0.99) == (0, None, 0.0)


async def test_review_sample_skips_labelled_and_spreads_bands(config, store) -> None:
    findings = await Analyser(config, FakeClient(), store).analyse(PYTHON, "python", APP)
    rows = store.query(SAMPLE, ("unit_mismatch", 10))
    assert sorted(round(row[6], 2) for row in rows) == [0.05, 0.95]
    store.label("unit_mismatch", findings[0].unit_hash, True)
    assert [row[3] for row in store.query(SAMPLE, ("unit_mismatch", 10))] == ["total"]


def test_label_command_targets_the_innermost_function(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    source = tmp_path / "app.py"
    source.write_text(PYTHON)
    with pytest.raises(SystemExit) as exit_info:
        app(["label", str(source), "6", "unit_mismatch", "false"])
    assert exit_info.value.code == 0
    store = Store(tmp_path / "xdg" / "nouls" / "nouls.db")
    expected = unit_hash(
        "python",
        "def expired(self, timeout_s: int) -> bool:\n        return time.time() * 1000 - self.started > timeout_s",
    )
    assert store.query("SELECT rule, unit_hash, real FROM labels") == [("unit_mismatch", expected, 0)]
