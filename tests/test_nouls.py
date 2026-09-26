# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for unit extraction, analysis, caching and labelling."""

from pathlib import Path

import pytest

from nouls.analyser import Analyser, unit_hash
from nouls.cli import app, discover, render
from nouls.config import Config, load_config
from nouls.stats import SAMPLE, SampleRow, score
from nouls.store import Store
from nouls.units import extract_units
from tests.conftest import APP, PYTHON, PYTHON_FUNCTIONS, FakeClient, as_client

pytestmark = pytest.mark.unit


async def test_flags_only_the_offending_function(config: Config, store: Store) -> None:
    """Flags only the offending function."""
    client = FakeClient()
    findings = await Analyser(config, as_client(client), store).analyse(PYTHON, "python", APP)
    assert [(f.rule, f.severity, f.span.line, f.span.column) for f in findings] == [
        ("unit_mismatch", "error", 4, 8)
    ]
    assert len(client.calls) == PYTHON_FUNCTIONS


async def test_unchanged_functions_are_cached_across_processes(
    config: Config, store: Store
) -> None:
    """Unchanged functions are cached across processes."""
    client = FakeClient()
    _ = await Analyser(config, as_client(client), store).analyse(PYTHON, "python", APP)
    reopened = Store(store.path)
    _ = await Analyser(config, as_client(client), reopened).analyse(
        PYTHON.replace("sum(items)", "sum(items) + 0"),
        "python",
        APP,
    )
    assert len(client.calls) == PYTHON_FUNCTIONS + 1
    ((asked, cached),) = reopened.query(tuple[int, int], "SELECT SUM(asked), SUM(cached) FROM runs")
    assert (asked, cached) == (42, 14)


async def test_rewording_one_rule_only_reasks_that_rule(config: Config, store: Store) -> None:
    """Rewording one rule only reasks that rule."""
    client = FakeClient()
    analyser = Analyser(config, as_client(client), store)
    _ = await analyser.analyse(PYTHON, "python", APP)
    config.rules["unit_mismatch"].question = "Are seconds mixed with milliseconds?"
    _ = await analyser.analyse(PYTHON, "python", APP)
    assert [call.questions for call in client.calls[2:]] == [
        {"unit_mismatch"},
        {"unit_mismatch"},
    ]


async def test_findings_labelled_false_are_suppressed(config: Config, store: Store) -> None:
    """Findings labelled false are suppressed."""
    analyser = Analyser(config, as_client(FakeClient()), store)
    findings = await analyser.analyse(PYTHON, "python", APP)
    store.label("unit_mismatch", findings[0].unit_hash, real=False)
    assert await analyser.analyse(PYTHON, "python", APP) == []
    ((fired,),) = store.query(tuple[int], "SELECT SUM(fired) FROM observations")
    assert fired == 0


async def test_project_yaml_disables_and_scopes_rules(tmp_path: Path) -> None:
    """Project yaml disables and scopes rules."""
    _ = (tmp_path / "nouls.yaml").write_text(
        "threshold: 0.99\nrules:\n  unit_mismatch:\n    threshold: 0.9\n"
        "  docstring_drift:\n    enabled: false\n"
        "  go_only:\n    question: Is this Go?\n    message: Go\n    languages: [go]\n"
    )
    config = load_config(tmp_path / "nested")
    python_rules = config.rules_for("python", APP)
    assert "docstring_drift" not in python_rules
    assert "go_only" not in python_rules
    assert "go_only" in config.rules_for("go", Path("main.go"))
    analyser = Analyser(config, as_client(FakeClient()), Store(tmp_path / "nouls.db"))
    findings = await analyser.analyse(PYTHON, "python", APP)
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
def test_units_are_found_from_yaml_node_types(
    config: Config, language: str, source: str, expected: int
) -> None:
    """Units are found from yaml node types."""
    units = extract_units(source, config.languages[language])
    assert len(units) == expected
    assert all(unit.span.end_column > unit.span.column for unit in units)


@pytest.mark.parametrize(
    ("language", "path", "source", "expected"),
    [
        (
            "python",
            "tests/conftest.py",
            "@pytest.fixture\ndef db():\n    yield 1\n",
            ["@pytest.fixture\ndef db():\n    yield 1"],
        ),
        ("rust", "crate/tests/api.rs", "#[test]\nfn f() {}\n", ["#[test]\nfn f() {}"]),
        (
            "typescript",
            "web/cart.spec.ts",
            "describe('cart', () => {\n  beforeEach(() => {});\n  it('adds', () => {});\n});\n",
            ["beforeEach(() => {})", "it('adds', () => {})"],
        ),
        (
            "ruby",
            "spec/cart_spec.rb",
            "describe Cart do\n  let!(:cart) { Cart.new }\n  it 'adds' do\n  end\nend\n",
            ["let!(:cart) { Cart.new }", "it 'adds' do\n  end"],
        ),
        (
            "lua",
            "spec/cart_spec.lua",
            "describe('cart', function()\n  before_each(function() end)\nend)\n",
            ["before_each(function() end)"],
        ),
    ],
)
def test_fixtures_and_hooks_are_units_with_their_decorators(
    config: Config, language: str, path: str, source: str, expected: list[str]
) -> None:
    """Fixtures and hooks are units with their decorators."""
    assert config.is_test(Path(path))
    units = extract_units(source, config.languages[language], tests=config.is_test(Path(path)))
    assert [unit.source for unit in units] == expected


def test_test_calls_are_not_units_outside_test_files(config: Config) -> None:
    """Test calls are not units outside test files."""
    source = "beforeEach(() => {});\nit('adds', () => {});\n"
    assert not config.is_test(Path("web/cart.ts"))
    assert extract_units(source, config.languages["typescript"]) == []


def test_a_decorated_function_is_labelled_by_line_from_its_decorator(config: Config) -> None:
    """A decorated function is labelled by line from its decorator."""
    source = "@pytest.fixture\ndef db():\n    yield 1\n"
    [unit] = extract_units(source, config.languages["python"])
    assert (unit.first_line, unit.span.line) == (0, 1)
    assert unit.contains(0)


def test_discover_skips_excluded_and_unknown_files(config: Config, tmp_path: Path) -> None:
    """Discover skips excluded and unknown files."""
    _ = (tmp_path / "app.py").write_text("")
    _ = (tmp_path / "notes.txt").write_text("")
    (tmp_path / ".venv").mkdir()
    _ = (tmp_path / ".venv" / "lib.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    _ = (tmp_path / "node_modules" / "x.js").write_text("")
    assert [(p.name, lang) for p, lang in discover([tmp_path], config)] == [("app.py", "python")]


async def test_render_is_one_based(config: Config, store: Store) -> None:
    """Render is one based."""
    findings = await Analyser(config, as_client(FakeClient()), store).analyse(PYTHON, "python", APP)
    shown = render(Path("a.py"), findings[0], show_probability=True)
    assert shown.startswith("a.py:5:9: error [unit_mismatch]")
    assert shown.endswith("(Probability: 95%)")
    hidden = render(Path("a.py"), findings[0], show_probability=False)
    assert hidden.endswith("in the variable names")


def test_desiderata_rules_only_apply_to_test_files(config: Config) -> None:
    """Desiderata rules only apply to test files."""
    desiderata = {name for name in config.rules if name.startswith(("test_", "fixture_"))}
    assert desiderata == {
        "test_not_isolated",
        "test_not_composable",
        "test_nondeterministic",
        "test_slow",
        "test_hard_to_write",
        "test_unreadable",
        "test_not_behavioural",
        "test_structure_sensitive",
        "test_not_automated",
        "test_not_specific",
        "test_not_predictive",
        "test_not_inspiring",
        "fixture_leaks_state",
        "fixture_hides_behaviour",
    }
    for path in [
        "tests/test_orders.py",
        "orders_test.go",
        "web/cart.spec.tsx",
        "src/OrderTest.java",
        "crate/tests/api.rs",
        "tests/conftest.py",
        "spec/support/helpers.rb",
    ]:
        language = config.language_for(Path(path))
        assert language is not None
        assert desiderata <= set(config.rules_for(language, Path(path))), path
    for path in ["src/orders.py", "orders.go", "web/cart.tsx", "src/lib.rs"]:
        language = config.language_for(Path(path))
        assert language is not None
        assert not desiderata & set(config.rules_for(language, Path(path))), path


def test_score_counts_precision_and_recall() -> None:
    """Score counts precision and recall."""
    pairs = [(0.95, True), (0.9, False), (0.7, True), (0.2, False)]
    assert score(pairs, 0.8) == (2, 0.5, 0.5)
    assert score(pairs, 0.5) == (3, 2 / 3, 1.0)
    assert score(pairs, 0.99) == (0, None, 0.0)


async def test_review_sample_skips_labelled_and_spreads_bands(config: Config, store: Store) -> None:
    """Review sample skips labelled and spreads bands."""
    findings = await Analyser(config, as_client(FakeClient()), store).analyse(PYTHON, "python", APP)
    rows = store.query(SampleRow, SAMPLE, ("unit_mismatch", 10))
    assert sorted(round(row[6], 2) for row in rows) == [0.05, 0.95]
    store.label("unit_mismatch", findings[0].unit_hash, real=True)
    assert [row[3] for row in store.query(SampleRow, SAMPLE, ("unit_mismatch", 10))] == ["total"]


def test_label_command_targets_the_innermost_function(tmp_path: Path) -> None:
    """Label command targets the innermost function."""
    source = tmp_path / "app.py"
    _ = source.write_text(PYTHON)
    with pytest.raises(SystemExit) as exit_info:
        app(["label", str(source), "6", "unit_mismatch", "false"])
    assert exit_info.value.code == 0
    store = Store(tmp_path / "xdg" / "nouls.db")
    expected = unit_hash(
        "python",
        "def expired(self, timeout_s: int) -> bool:\n"
        "        return time.time() * 1000 - self.started > timeout_s",
    )
    assert store.query(tuple[str, str, int], "SELECT rule, unit_hash, real FROM labels") == [
        ("unit_mismatch", expected, 0)
    ]
