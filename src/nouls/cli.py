# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Command line interface: check, rules, label, review, serve and stats."""

import asyncio
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter, validators
from rich.console import Console
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

from nouls.analyser import (
    PROJECT,
    SETTING,
    Analyser,
    Finding,
    file_kind,
    project_source,
    unit_hash,
)
from nouls.config import Config, ConfigError, Rule, Severity, find_root, load_config
from nouls.output import fail, say
from nouls.server import server
from nouls.settings import settings_in
from nouls.stats import SAMPLE, SampleRow, display, stats
from nouls.store import Store
from nouls.typesafe import explain
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

_ = app.command(stats)
console = Console()

Positive = Annotated[int, Parameter(validator=validators.Number(gt=0))]
ConfigOption = Annotated[
    Path | None, Parameter(name=["--config", "-c"], help="Path to a nouls.yaml file.")
]


def discover(paths: list[Path], config: Config) -> Iterator[tuple[Path, str]]:
    """Find every file to check under the given paths.

    Args:
        paths: Files or directories to check.
        config: The loaded configuration, for exclusions and languages.

    Yields:
        Each file with a configured language, and that language.

    """
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
                assert config.language(language), "A discovered language must be parseable"
                yield candidate, language


def render(path: Path, finding: Finding, *, show_probability: bool) -> str:
    """Format a finding as one line of plain text.

    Args:
        path: The file the finding belongs to.
        finding: The finding to format.
        show_probability: Whether to include the probability.

    Returns:
        ``path:line:column: severity [rule] message``.

    """
    assert finding.span.line >= 0, "Finding lines are zero based"
    text = (
        f"{path}:{finding.span.line + 1}:{finding.span.column + 1}: "
        f"{finding.severity} [{finding.rule}] {finding.describe(show_probability=show_probability)}"
    )
    assert f"[{finding.rule}]" in text, "Rendered finding must name its rule"
    return text


def render_pretty(findings: list[tuple[Path, Finding]], *, show_probability: bool) -> None:
    """Print findings grouped by file, with a summary, for a terminal.

    Args:
        findings: Each finding with its file.
        show_probability: Whether to include probabilities.

    """
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
            message = finding.describe(show_probability=show_probability)
            console.print(
                f"  [dim]{location:<8}[/dim][{style}]{finding.severity:<8}[/{style}]"
                f"[bold]{finding.rule}[/bold]  {message}",
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


async def run_check(paths: list[Path], config: Config, root: Path) -> int:
    """Check files and the project, then print the findings.

    Args:
        paths: Files or directories to check.
        config: The loaded configuration.
        root: The project root for project rules.

    Returns:
        1 when an error level rule fired, otherwise 0.

    """
    assert paths, "At least one path must be given"
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(config, client, Store(config.store_path()))
        files = list(discover(paths, config))
        results = await asyncio.gather(
            *(
                analyser.analyse(path.read_text(encoding="utf-8"), language, path)
                for path, language in files
            )
        )
        project = await analyser.analyse_project(root)
        project += await analyser.analyse_settings(root)
    assert len(results) == len(files), "Every file must have results"
    findings = [
        (path, finding)
        for (path, _), found in zip(files, results, strict=True)
        for finding in found
    ]
    findings += [(Path(display(str(path))), finding) for path, finding in project]
    if console.is_terminal:
        render_pretty(findings, show_probability=config.show_probability)
    else:
        for path, finding in findings:
            say(render(path, finding, show_probability=config.show_probability))
    return 1 if any(finding.severity == "error" for _, finding in findings) else 0


@app.command
def check(paths: list[Path] | None = None, /, *, config: ConfigOption = None) -> int:
    """Lint files or directories and exit non zero when any error level rule fires.

    Returns:
        1 when an error level rule fired, 2 for a usage error, otherwise 0.

    """
    targets = paths or [Path.cwd()]
    missing = [target for target in targets if not target.exists()]
    if missing:
        return fail(
            f"nouls: cannot check {', '.join(map(str, missing))} because it does not exist. "
            "Fix the path, or run nouls check with no paths to lint the current directory."
        )
    assert targets, "At least one target must be checked"
    root = targets[0] if targets[0].is_dir() else targets[0].parent
    assert root.is_dir(), "Config search root must be a directory"
    try:
        return asyncio.run(run_check(targets, load_config(root, config), find_root(root.resolve())))
    except TypeSafeError as error:
        return fail(explain(error))


def rule_fields(loaded: Config, rule: Rule) -> tuple[Severity, float, str]:
    """Summarise where and how strongly a rule applies.

    Args:
        loaded: The loaded configuration, for the global threshold.
        rule: An enabled rule.

    Returns:
        The rule's severity, threshold and a description of its scope.

    """
    assert rule.enabled, "Only enabled rules are describable"
    scope = ", ".join(rule.languages) if rule.languages else "all languages"
    if rule.scope != "function":
        where = {"file": "each file", "project": "project", "setting": "each setting"}[rule.scope]
        scope = f"{scope}, {where}" if rule.scope == "file" else where
    if rule.files:
        scope += f" in {len(rule.files)} file patterns"
    threshold = loaded.threshold if rule.threshold is None else rule.threshold
    assert 0.0 <= threshold <= 1.0, "Threshold must be in [0, 1]"
    return rule.severity, threshold, scope


def describe_rule(loaded: Config, name: str, rule: Rule) -> str:
    """Describe a rule on one line.

    Args:
        loaded: The loaded configuration.
        name: The rule's name.
        rule: The rule.

    Returns:
        ``name (severity, threshold t, scope): question``, or the metric and its limit.

    """
    assert name in loaded.rules, "Rule must be configured"
    severity, threshold, scope = rule_fields(loaded, rule)
    check = rule.describe_check()
    assert check, "Every rule says how it decides"
    if rule.metric is not None:
        return f"{name} ({severity}, {scope}): {check}"
    return f"{name} ({severity}, threshold {threshold}, {scope}): {check}"


@app.command
def rules(config: ConfigOption = None) -> int:
    """List the rules that are enabled for this project.

    Returns:
        0, or 2 when every rule is disabled.

    """
    loaded = load_config(Path.cwd(), config)
    assert loaded.rules, (
        "The loaded config has no rules, so defaults.yaml was not merged in; "
        "load it with load_config, which always starts from the defaults"
    )
    enabled = {name: rule for name, rule in loaded.rules.items() if rule.enabled}
    if not enabled:
        return fail(
            "nouls: every rule is disabled in your nouls config, so there is nothing to list. "
            "Set enabled: true on the rules you want, or remove enabled: false from them."
        )
    assert all(rule.enabled for rule in enabled.values()), (
        "rules is about to list a disabled rule; filter enabled on rule.enabled"
    )
    if not console.is_terminal:
        for name, rule in enabled.items():
            console.print(describe_rule(loaded, name, rule), soft_wrap=True)
        return 0
    table = Table(show_lines=True, expand=True)
    table.add_column("Rule", style="bold", no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Question", ratio=1)
    for name, rule in enabled.items():
        severity, threshold, scope = rule_fields(loaded, rule)
        style = SEVERITY_STYLE[severity]
        check = rule.describe_check()
        question = check if scope == "all languages" else f"[dim]({scope})[/dim] {check}"
        strength = "" if rule.metric is not None else f" {threshold:.0%}"
        table.add_row(name, f"[{style}]{severity}[/{style}]{strength}", question)
    console.print(table)
    return 0


@app.command
def label(
    path: Path,
    line: Positive,
    rule: str,
    verdict: Literal["real", "false"],
    *,
    config: ConfigOption = None,
) -> int:
    """Record whether a rule's finding at PATH:LINE is a real problem.

    Returns:
        0 when the label was saved, or 2 for a usage error.

    """
    assert path.name, f"label needs a file path but got {path!r}"
    loaded = load_config(path.parent, config)
    if rule not in loaded.rules:
        return fail(loaded.unknown_rule(rule))
    scope = loaded.rules[rule].scope
    assert scope in {"function", "file", "project", "setting"}, f"Unknown rule scope {scope!r}"
    if scope == "file":
        return label_file(loaded, path, rule, verdict)
    if scope == "project":
        return label_project(loaded, path, rule, verdict)
    if scope == "setting":
        return label_setting(loaded, path, line, rule, verdict)
    return label_function(loaded, path, line, rule, verdict)


def label_function(loaded: Config, path: Path, line: int, rule: str, verdict: str) -> int:
    """Record a verdict on a function rule's finding.

    Args:
        loaded: The loaded configuration.
        path: The source file.
        line: A one based line inside the function.
        rule: A function rule.
        verdict: ``real`` or ``false``.

    Returns:
        0 when the label was saved, or the usage error code.

    """
    language = loaded.language_for(path)
    if language is None:
        return fail(loaded.unsupported(path))
    if not path.is_file():
        return fail(
            f"nouls: cannot label {path} because it does not exist. "
            "Use the path exactly as nouls check printed it."
        )
    units = [
        u
        for u in extract_units(
            path.read_text(encoding="utf-8"),
            loaded.language(language),
            tests=loaded.is_test(path),
        )
        if u.contains(line - 1)
    ]
    if not units:
        return fail(
            f"nouls: line {line} of {path} is outside every function, "
            "so there is no finding there to label. "
            "Use the line number nouls check printed for the finding."
        )
    unit = min(units, key=lambda u: u.last_line - u.first_line)
    assert unit.contains(line - 1), "Chosen function must contain the line"
    assert rule in loaded.rules, "Rule must be configured"
    Store(loaded.store_path()).label(rule, unit_hash(language, unit.source), real=verdict == "real")
    say(f"Labelled {rule} on {unit.name} as {verdict}")
    return 0


def label_file(loaded: Config, path: Path, rule: str, verdict: str) -> int:
    """Record a verdict on a file rule's finding.

    Args:
        loaded: The loaded configuration.
        path: The source file.
        rule: A rule with scope: file.
        verdict: ``real`` or ``false``.

    Returns:
        0 when the label was saved, or the usage error code.

    """
    assert loaded.rules[rule].scope == "file", f"{rule} must be a file rule"
    language = loaded.language_for(path)
    if language is None:
        return fail(loaded.unsupported(path))
    if not path.is_file():
        return fail(
            f"nouls: cannot label {path} because it does not exist. "
            "Use the path exactly as nouls check printed it."
        )
    text = path.read_text(encoding="utf-8")
    Store(loaded.store_path()).label(
        rule, unit_hash(file_kind(language), text), real=verdict == "real"
    )
    assert verdict in {"real", "false"}, f"verdict must be real or false but got {verdict}"
    say(f"Labelled {rule} on {path} as {verdict}")
    return 0


def label_project(loaded: Config, path: Path, rule: str, verdict: str) -> int:
    """Record a verdict on a project rule's finding.

    Args:
        loaded: The loaded configuration.
        path: A file or directory in the project.
        rule: A rule with scope: project.
        verdict: ``real`` or ``false``.

    Returns:
        0 when the label was saved, or the usage error code.

    """
    patterns = loaded.rules[rule].files
    assert patterns, f"Project rule {rule} has no files; the Rule validator must reject it"
    if not path.exists():
        return fail(
            f"nouls: cannot label {path} because it does not exist. "
            "Use the path exactly as nouls check printed it."
        )
    root = find_root(path.resolve() if path.is_dir() else path.resolve().parent)
    _, source = project_source(root, patterns)
    Store(loaded.store_path()).label(rule, unit_hash(PROJECT, source), real=verdict == "real")
    assert verdict in {"real", "false"}, f"verdict must be real or false but got {verdict}"
    say(f"Labelled {rule} on the project at {display(str(root))} as {verdict}")
    return 0


def label_setting(loaded: Config, path: Path, line: int, rule: str, verdict: str) -> int:
    """Record a verdict on a setting rule's finding.

    Args:
        loaded: The loaded configuration.
        path: The configuration file.
        line: The one based line of the setting.
        rule: A rule with scope: setting.
        verdict: ``real`` or ``false``.

    Returns:
        0 when the label was saved, or the usage error code.

    """
    assert loaded.rules[rule].scope == "setting", f"{rule} must be a setting rule"
    assert verdict in {"real", "false"}, f"verdict must be real or false but got {verdict}"
    if not path.is_file():
        return fail(
            f"nouls: cannot label {path} because it does not exist. "
            "Use the path exactly as nouls check printed it."
        )
    resolved = path.resolve()
    root = find_root(resolved.parent)
    found = [s for s in settings_in(root, resolved) if s.span.line == line - 1]
    if not found:
        return fail(
            f"nouls: line {line} of {path} does not set anything, so there is no finding there "
            "to label. Use the line number nouls check printed for the finding."
        )
    Store(loaded.store_path()).label(
        rule, unit_hash(SETTING, found[0].source()), real=verdict == "real"
    )
    say(f"Labelled {rule} on {path}:{line} as {verdict}")
    return 0


@app.command
def review(rule: str, *, limit: Positive = 20, config: ConfigOption = None) -> int:
    """Label unlabelled functions for RULE, sampled evenly across probability bands.

    Returns:
        0, or 2 when the rule does not exist.

    """
    loaded = load_config(Path.cwd(), config)
    if rule not in loaded.rules:
        return fail(loaded.unknown_rule(rule))
    assert limit > 0, "Sample size must be positive"
    store = Store(loaded.store_path())
    console.print(f"[bold]{rule}[/bold]: {loaded.rules[rule].question}")
    for uhash, language, source, name, path, line, probability in store.query(
        SampleRow, SAMPLE, (rule, limit)
    ):
        if not 0.0 <= probability <= 1.0:
            _ = fail(
                f"nouls: skipping {name} at {display(path)}:{line + 1} because its stored "
                f"probability {probability} is outside 0 to 1, so the cache is corrupt. "
                f"Run nouls check {display(path)} to record it again. If it stays corrupt, "
                f"delete {loaded.store_path()}, which also deletes your labels."
            )
            continue
        console.rule(f"{name}  {display(path)}:{line + 1}  p={probability:.2f}")
        console.print(Syntax(source, language, line_numbers=True, start_line=line + 1))
        answer = Prompt.ask("Real problem?", choices=["y", "n", "s", "q"], default="s")
        assert answer in {"y", "n", "s", "q"}, "Prompt must return one of its offered choices"
        if answer == "q":
            return 0
        if answer in {"y", "n"}:
            store.label(rule, uhash, real=answer == "y")
    return 0


@app.command
def serve() -> None:
    """Start the language server on stdio."""
    assert server.name == "nouls", "Language server must identify as nouls"
    assert not server.pending, "Language server must start idle"
    server.start_io()


def main(tokens: Sequence[str] | None = None) -> None:
    """Run the command line, reporting a broken nouls config as a message instead of a traceback.

    Args:
        tokens: The arguments, or None to read them from the process.

    """
    assert tokens is None or all(isinstance(token, str) for token in tokens), "Tokens are text"
    try:
        app(tokens)
    except ConfigError as error:
        code = fail(str(error))
        assert code, "A config error exits non zero"
        sys.exit(code)
