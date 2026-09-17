import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, Finding
from nouls.config import Config, load_config

app = App(name="nouls", help="Semantic linter that asks TypeSafe yes/no questions about every function.")

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


def render(path: Path, finding: Finding) -> str:
    return (
        f"{path}:{finding.span.line + 1}:{finding.span.column + 1}: "
        f"{finding.severity} [{finding.rule}] {finding.message} ({finding.probability:.0%})"
    )


async def run_check(paths: list[Path], config: Config) -> int:
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(config, client)
        files = list(discover(paths, config))
        results = await asyncio.gather(*(analyser.analyse(path.read_text(), language) for path, language in files))
    findings = [(path, finding) for (path, _), found in zip(files, results) for finding in found]
    for path, finding in findings:
        print(render(path, finding))
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
            print(f"{name} ({rule.severity}, {scope}): {rule.question}")


@app.command
def serve() -> None:
    """Start the language server on stdio."""
    from nouls.server import server

    server.start_io()


def main() -> None:
    app()
