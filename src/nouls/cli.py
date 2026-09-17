import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter
from rich.console import Console
from rich.prompt import Prompt
from rich.syntax import Syntax
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, Finding, unit_hash
from nouls.config import Config, load_config
from nouls.stats import SAMPLE, display, stats
from nouls.store import Store
from nouls.units import extract_units

app = App(name="nouls", help="Semantic linter that asks TypeSafe yes/no questions about every function.")

app.command(stats)
console = Console()

ConfigOption = Annotated[Path | None, Parameter(name=["--config", "-c"], help="Path to a nouls.yaml file.")]


def discover(paths: list[Path], config: Config) -> Iterator[tuple[Path, str]]:
    for path in paths:
        candidates = [path] if path.is_file() else (p for p in sorted(path.rglob("*")) if p.is_file())
        for candidate in candidates:
            relative = candidate.relative_to(path) if path.is_dir() else candidate
            if path.is_dir() and config.excluded(relative):
                continue
            if language := config.language_for(candidate):
                yield candidate, language


def render(path: Path, finding: Finding, show_probability: bool) -> str:
    return (
        f"{path}:{finding.span.line + 1}:{finding.span.column + 1}: "
        f"{finding.severity} [{finding.rule}] {finding.describe(show_probability)}"
    )


async def run_check(paths: list[Path], config: Config) -> int:
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(config, client, Store(config.store_path()))
        files = list(discover(paths, config))
        results = await asyncio.gather(
            *(analyser.analyse(path.read_text(), language, path) for path, language in files)
        )
    findings = [(path, finding) for (path, _), found in zip(files, results) for finding in found]
    for path, finding in findings:
        print(render(path, finding, config.show_probability))
    return 1 if any(finding.severity == "error" for _, finding in findings) else 0


@app.command
def check(*paths: Path, config: ConfigOption = None) -> int:
    """Lint files or directories and exit non zero when any error level rule fires."""
    targets = list(paths) or [Path.cwd()]
    return asyncio.run(
        run_check(targets, load_config(targets[0] if targets[0].is_dir() else targets[0].parent, config))
    )


@app.command
def rules(config: ConfigOption = None) -> None:
    """List the rules that are enabled for this project."""
    loaded = load_config(Path.cwd(), config)
    for name, rule in loaded.rules.items():
        if rule.enabled:
            scope = ", ".join(rule.languages) if rule.languages else "all languages"
            if rule.files:
                scope += f" in {len(rule.files)} file patterns"
            threshold = loaded.threshold if rule.threshold is None else rule.threshold
            print(f"{name} ({rule.severity}, threshold {threshold}, {scope}): {rule.question}")


@app.command
def label(path: Path, line: int, rule: str, verdict: Literal["real", "false"], *, config: ConfigOption = None) -> int:
    """Record whether a rule's finding on the function at PATH:LINE is a real problem."""
    loaded = load_config(path.parent, config)
    language = loaded.language_for(path)
    if language is None or rule not in loaded.rules:
        print(f"nouls: no language for {path} or unknown rule {rule}", file=sys.stderr)
        return 2
    units = [u for u in extract_units(path.read_text(), loaded.languages[language]) if u.contains(line - 1)]
    if not units:
        print(f"nouls: no function contains {path}:{line}", file=sys.stderr)
        return 2
    unit = min(units, key=lambda u: u.last_line - u.first_line)
    Store(loaded.store_path()).label(rule, unit_hash(language, unit.source), verdict == "real")
    print(f"Labelled {rule} on {unit.name} as {verdict}")
    return 0


@app.command
def review(rule: str, *, limit: int = 20, config: ConfigOption = None) -> None:
    """Label unlabelled functions for RULE, sampled evenly across probability bands."""
    loaded = load_config(Path.cwd(), config)
    store = Store(loaded.store_path())
    console.print(f"[bold]{rule}[/bold]: {loaded.rules[rule].question}")
    for uhash, language, source, name, path, line, probability in store.query(SAMPLE, (rule, limit)):
        console.rule(f"{name}  {display(path)}:{line + 1}  p={probability:.2f}")
        console.print(Syntax(source, language, line_numbers=True, start_line=line + 1))
        answer = Prompt.ask("Real problem?", choices=["y", "n", "s", "q"], default="s")
        if answer == "q":
            return
        if answer in {"y", "n"}:
            store.label(rule, uhash, answer == "y")


@app.command
def serve() -> None:
    """Start the language server on stdio."""
    from nouls.server import server

    server.start_io()


def main() -> None:
    app()
