import asyncio
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter, validators
from rich.console import Console
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, Finding, unit_hash
from nouls.config import Config, Rule, Severity, load_config
from nouls.stats import SAMPLE, display, stats
from nouls.store import Store
from nouls.units import extract_units

SEVERITY_STYLE: dict[Severity, str] = {
    "error": "bold red",
    "warning": "yellow",
    "info": "cyan",
    "hint": "dim",
}

app = App(
    name="nouls",
    help_format="plaintext",
    help="""Semantic linter that asks TypeSafe yes/no questions about every function.

Usage examples:
  nouls check src/                               Lint a directory
  nouls rules                                    List enabled rules
  nouls label src/app.py 42 unit_mismatch false  Record a verdict
  nouls review unit_mismatch --limit 10          Label a sample interactively
  nouls stats thresholds --ask                   Precision and recall per threshold
  nouls serve                                    Start the language server
""",
)

app.command(stats)
console = Console()

Positive = Annotated[int, Parameter(validator=validators.Number(gt=0))]
ConfigOption = Annotated[
    Path | None, Parameter(name=["--config", "-c"], help="Path to a nouls.yaml file.")
]


def discover(paths: list[Path], config: Config) -> Iterator[tuple[Path, str]]:
    assert paths, "At least one path must be given"
    for path in paths:
        candidates = (
            [path] if path.is_file() else (p for p in sorted(path.rglob("*")) if p.is_file())
        )
        for candidate in candidates:
            relative = candidate.relative_to(path) if path.is_dir() else candidate
            if path.is_dir() and config.excluded(relative):
                continue
            if language := config.language_for(candidate):
                assert language in config.languages, "Discovered language must be configured"
                yield candidate, language


def render(path: Path, finding: Finding, show_probability: bool) -> str:
    assert finding.span.line >= 0, "Finding lines are zero based"
    text = (
        f"{path}:{finding.span.line + 1}:{finding.span.column + 1}: "
        f"{finding.severity} [{finding.rule}] {finding.describe(show_probability)}"
    )
    assert f"[{finding.rule}]" in text, "Rendered finding must name its rule"
    return text


def render_pretty(findings: list[tuple[Path, Finding]], show_probability: bool) -> None:
    by_path: dict[Path, list[Finding]] = {}
    for path, finding in findings:
        by_path.setdefault(path, []).append(finding)
    assert sum(len(found) for found in by_path.values()) == len(findings), (
        "Grouping by path must not drop or duplicate findings"
    )
    for path, found in by_path.items():
        console.print(f"\n[bold underline]{display(str(path))}[/bold underline]")
        for finding in sorted(found, key=lambda f: (f.span.line, f.span.column)):
            style = SEVERITY_STYLE[finding.severity]
            location = f"{finding.span.line + 1}:{finding.span.column + 1}"
            console.print(
                f"  [dim]{location:<8}[/dim][{style}]{finding.severity:<8}[/{style}]"
                f"[bold]{finding.rule}[/bold]  {finding.describe(show_probability)}",
                soft_wrap=True,
            )
    console.print()
    if not findings:
        console.print("[bold green]No issues found[/bold green]")
        return
    counts: Counter[Severity] = Counter(finding.severity for _, finding in findings)
    assert counts.total() == len(findings), "Every finding must be counted exactly once"
    summary = "  ".join(
        f"[{SEVERITY_STYLE[sev]}]{count} {sev}{'s' if count != 1 else ''}[/{SEVERITY_STYLE[sev]}]"
        for sev, count in counts.items()
    )
    files = "file" if len(by_path) == 1 else "files"
    console.print(f"{summary}  across {len(by_path)} {files}")


async def run_check(paths: list[Path], config: Config) -> int:
    assert paths, "At least one path must be given"
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(config, client, Store(config.store_path()))
        files = list(discover(paths, config))
        # Every file is parsed here, up front, before any network call starts:
        # tree-sitter's parse trees must all be built and discarded before
        # concurrent async I/O begins, not interleaved with it.
        parsed = [
            unit
            for unit in (
                analyser.parse(path.read_text(), language, path) for path, language in files
            )
            if unit is not None
        ]
        results = await asyncio.gather(*(analyser.score(p) for p in parsed))
    assert len(results) == len(parsed), "Every parsed file must have results"
    findings = [(p.path, finding) for p, found in zip(parsed, results) for finding in found]
    if console.is_terminal:
        render_pretty(findings, config.show_probability)
    else:
        for path, finding in findings:
            print(render(path, finding, config.show_probability))
    return 1 if any(finding.severity == "error" for _, finding in findings) else 0


@app.command
def check(*paths: Path, config: ConfigOption = None) -> int:
    """Lint files or directories and exit non zero when any error level rule fires."""
    targets = list(paths) or [Path.cwd()]
    missing = [target for target in targets if not target.exists()]
    if missing:
        print(f"nouls: no such file or directory: {missing[0]}", file=sys.stderr)
        return 2
    assert targets, "At least one target must be checked"
    root = targets[0] if targets[0].is_dir() else targets[0].parent
    assert root.is_dir(), "Config search root must be a directory"
    return asyncio.run(run_check(targets, load_config(root, config)))


def rule_fields(loaded: Config, rule: Rule) -> tuple[Severity, float, str]:
    assert rule.enabled, "Only enabled rules are describable"
    scope = ", ".join(rule.languages) if rule.languages else "all languages"
    if rule.files:
        scope += f" in {len(rule.files)} file patterns"
    threshold = loaded.threshold if rule.threshold is None else rule.threshold
    assert 0.0 <= threshold <= 1.0, "Threshold must be in [0, 1]"
    return rule.severity, threshold, scope


def describe_rule(loaded: Config, name: str, rule: Rule) -> str:
    assert name in loaded.rules, "Rule must be configured"
    assert rule.question.strip(), "Rule question must not be blank"
    severity, threshold, scope = rule_fields(loaded, rule)
    return f"{name} ({severity}, threshold {threshold}, {scope}): {rule.question}"


@app.command
def rules(config: ConfigOption = None) -> None:
    """List the rules that are enabled for this project."""
    loaded = load_config(Path.cwd(), config)
    assert loaded.rules, "Config must define rules"
    enabled = {name: rule for name, rule in loaded.rules.items() if rule.enabled}
    assert enabled, "At least one rule must be enabled to list"
    if not console.is_terminal:
        for name, rule in enabled.items():
            console.print(describe_rule(loaded, name, rule), soft_wrap=True)
        return
    table = Table(show_lines=True, expand=True)
    table.add_column("Rule", style="bold", no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Question", ratio=1)
    for name, rule in enabled.items():
        severity, threshold, scope = rule_fields(loaded, rule)
        style = SEVERITY_STYLE[severity]
        question = (
            rule.question if scope == "all languages" else f"[dim]({scope})[/dim] {rule.question}"
        )
        table.add_row(name, f"[{style}]{severity}[/{style}] {threshold:.0%}", question)
    console.print(table)


@app.command
def label(
    path: Path,
    line: Positive,
    rule: str,
    verdict: Literal["real", "false"],
    *,
    config: ConfigOption = None,
) -> int:
    """Record whether a rule's finding on the function at PATH:LINE is a real problem."""
    loaded = load_config(path.parent, config)
    language = loaded.language_for(path)
    if language is None or rule not in loaded.rules:
        print(f"nouls: no language for {path} or unknown rule {rule}", file=sys.stderr)
        return 2
    units = [
        u
        for u in extract_units(path.read_text(), loaded.languages[language], loaded.is_test(path))
        if u.contains(line - 1)
    ]
    if not units:
        print(f"nouls: no function contains {path}:{line}", file=sys.stderr)
        return 2
    unit = min(units, key=lambda u: u.last_line - u.first_line)
    assert unit.contains(line - 1), "Chosen function must contain the line"
    assert rule in loaded.rules, "Rule must be configured"
    Store(loaded.store_path()).label(rule, unit_hash(language, unit.source), verdict == "real")
    print(f"Labelled {rule} on {unit.name} as {verdict}")
    return 0


@app.command
def review(rule: str, *, limit: Positive = 20, config: ConfigOption = None) -> int:
    """Label unlabelled functions for RULE, sampled evenly across probability bands."""
    loaded = load_config(Path.cwd(), config)
    if rule not in loaded.rules:
        print(f"nouls: unknown rule {rule}, see nouls rules", file=sys.stderr)
        return 2
    assert limit > 0, "Sample size must be positive"
    store = Store(loaded.store_path())
    console.print(f"[bold]{rule}[/bold]: {loaded.rules[rule].question}")
    for uhash, language, source, name, path, line, probability in store.query(
        SAMPLE, (rule, limit)
    ):
        if not 0.0 <= probability <= 1.0:
            print(
                f"nouls: skipping {name} at {display(path)}:{line + 1}: "
                f"corrupt stored probability {probability}",
                file=sys.stderr,
            )
            continue
        console.rule(f"{name}  {display(path)}:{line + 1}  p={probability:.2f}")
        console.print(Syntax(source, language, line_numbers=True, start_line=line + 1))
        answer = Prompt.ask("Real problem?", choices=["y", "n", "s", "q"], default="s")
        assert answer in {"y", "n", "s", "q"}, "Prompt must return one of its offered choices"
        if answer == "q":
            return 0
        if answer in {"y", "n"}:
            store.label(rule, uhash, answer == "y")
    return 0


@app.command
def serve() -> None:
    """Start the language server on stdio."""
    from nouls.server import server

    assert server.name == "nouls", "Language server must identify as nouls"
    assert not server.pending, "Language server must start idle"
    server.start_io()
