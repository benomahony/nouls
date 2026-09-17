import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter, validators
from rich.console import Console
from rich.prompt import Prompt
from rich.syntax import Syntax
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, Finding, unit_hash
from nouls.config import Config, load_config
from nouls.stats import SAMPLE, display, stats
from nouls.store import Store
from nouls.units import extract_units

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


async def run_check(paths: list[Path], config: Config) -> int:
    assert paths, "At least one path must be given"
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(config, client, Store(config.store_path()))
        files = list(discover(paths, config))
        results = await asyncio.gather(
            *(analyser.analyse(path.read_text(), language, path) for path, language in files)
        )
    assert len(results) == len(files), "Every file must have results"
    findings = [(path, finding) for (path, _), found in zip(files, results) for finding in found]
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


@app.command
def rules(config: ConfigOption = None) -> None:
    """List the rules that are enabled for this project."""
    loaded = load_config(Path.cwd(), config)
    assert loaded.rules, "Config must define rules"
    for name, rule in loaded.rules.items():
        if rule.enabled:
            scope = ", ".join(rule.languages) if rule.languages else "all languages"
            if rule.files:
                scope += f" in {len(rule.files)} file patterns"
            threshold = loaded.threshold if rule.threshold is None else rule.threshold
            assert 0.0 <= threshold <= 1.0, "Threshold must be in [0, 1]"
            print(f"{name} ({rule.severity}, threshold {threshold}, {scope}): {rule.question}")


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
        for u in extract_units(path.read_text(), loaded.languages[language])
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
        console.rule(f"{name}  {display(path)}:{line + 1}  p={probability:.2f}")
        console.print(Syntax(source, language, line_numbers=True, start_line=line + 1))
        assert 0.0 <= probability <= 1.0, "Stored probability must be in [0, 1]"
        answer = Prompt.ask("Real problem?", choices=["y", "n", "s", "q"], default="s")
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
