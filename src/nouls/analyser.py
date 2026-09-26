# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Ask each rule's question about functions, projects and settings, and report what fires."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from typesafe_sdk import AsyncTypeSafeClient, Noul

from nouls.config import Config, Rule, Severity, project_files
from nouls.settings import settings_in
from nouls.store import DIGEST_LENGTH, Observation, RunStats, Store, digest
from nouls.units import Span, extract_units

PROJECT = "project"
SETTING = "setting"


@dataclass(frozen=True)
class Finding:
    """A rule that fired on a function or project."""

    rule: str
    message: str
    severity: Severity
    probability: float
    span: Span
    unit_hash: str

    def describe(self, *, show_probability: bool) -> str:
        """Say what the finding means, optionally with its probability.

        Args:
            show_probability: Whether to append the model's probability.

        Returns:
            The rule's message, followed by the probability when asked for.

        """
        assert self.message, "Finding must have a message"
        text = (
            f"{self.message} (Probability: {self.probability:.0%})"
            if show_probability
            else self.message
        )
        assert text.startswith(self.message), "Description must lead with the message"
        return text


@dataclass(frozen=True)
class Target:
    """What a batch of answers is about: a function, a project or a setting."""

    name: str
    line: int
    span: Span
    unit_hash: str


@dataclass
class Asked:
    """The answers for one unit, and what asking for them cost."""

    probabilities: dict[str, float]
    asked: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def unit_hash(language: str, source: str) -> str:
    """Identify a unit by its language and source.

    Args:
        language: The unit's language, or ``project`` for project rules.
        source: The unit's source text.

    Returns:
        A digest that changes whenever the language or source does.

    """
    assert language, "Language must not be empty"
    assert source, "Source must not be empty"
    return digest(language, source)


def project_source(root: Path, patterns: list[str]) -> tuple[list[Path], str]:
    """Collect the files a project rule reads into one source text.

    Args:
        root: The project root that the globs are relative to.
        patterns: The rule's file globs.

    Returns:
        The matched paths in pattern order, and their contents as JSON keyed by relative path.

    """
    assert patterns, (
        "project_source needs the rule's file globs; pass rule.files, not an empty list"
    )
    paths = project_files(root, patterns)
    contents = {
        path.relative_to(root).as_posix(): path.read_text(errors="replace") for path in paths
    }
    source = json.dumps(contents, indent=2)
    assert len(contents) == len(paths), (
        "Two matched files share a relative path; project_files must return each file once"
    )
    return paths, source


def state_for(language: str, source: str) -> dict[str, str | dict[str, str]]:
    """Build the state that TypeSafe answers questions against.

    Args:
        language: The unit's language, or ``project`` for project rules.
        source: The unit's source, or the project's files as JSON.

    Returns:
        ``{"files": ...}`` for project rules, otherwise ``{"language": ..., "function": ...}``.

    """
    assert source, "state_for needs source text; pass the unit or project source that was hashed"
    state: dict[str, str | dict[str, str]]
    if language == PROJECT:
        state = {"files": cast("dict[str, str]", json.loads(source))}
    elif language == SETTING:
        state = dict(cast("dict[str, str]", json.loads(source)))
    else:
        state = {"language": language, "function": source}
    assert state, "state_for built an empty state; both branches must set at least one key"
    return state


def question_hash(rule: Rule) -> str:
    """Identify a rule's current wording.

    Args:
        rule: The rule whose question is hashed.

    Returns:
        A digest that changes whenever the question is reworded.

    """
    assert rule.question.strip(), "Rule question must not be blank"
    value = digest(rule.question)
    assert len(value) == DIGEST_LENGTH, "Question hash must be a digest"
    return value


class Analyser:
    """Ask rules about functions and projects, caching every answer."""

    config: Config
    client: AsyncTypeSafeClient
    store: Store
    _semaphore: asyncio.Semaphore

    def __init__(self, config: Config, client: AsyncTypeSafeClient, store: Store) -> None:
        """Prepare to ask questions with a bounded number of calls in flight.

        Args:
            config: The loaded nouls configuration.
            client: The TypeSafe client that answers questions.
            store: The store that caches answers and records results.

        """
        assert config.concurrency > 0, "Concurrency must be positive"
        assert config.languages, "At least one language must be configured"
        self.config = config
        self.client = client
        self.store = store
        self._semaphore = asyncio.Semaphore(config.concurrency)

    def threshold(self, rule: Rule) -> float:
        """Find the probability at which a rule fires.

        Args:
            rule: An enabled rule.

        Returns:
            The rule's own threshold, or the global one when it has none.

        """
        assert rule.enabled, "Thresholds apply to enabled rules"
        value = self.config.threshold if rule.threshold is None else rule.threshold
        assert 0.0 <= value <= 1.0, "Threshold must be in [0, 1]"
        return value

    async def ask(self, language: str, source: str, rules: dict[str, Rule]) -> Asked:
        """Answer every rule for one unit, asking only what is not cached.

        Args:
            language: The unit's language, or ``project`` for project rules.
            source: The unit's source, or the project's files as JSON.
            rules: The rules to answer, by name.

        Returns:
            A probability for every rule and the cost of the questions that were asked.

        """
        assert rules, "At least one rule must be asked"
        model = self.config.model
        hashes = {name: question_hash(rule) for name, rule in rules.items()}
        uhash = unit_hash(language, source)
        cached = self.store.answers(model, uhash, list(set(hashes.values())))
        missing = {name: rules[name] for name, qhash in hashes.items() if qhash not in cached}
        result = Asked({name: cached[qhash] for name, qhash in hashes.items() if qhash in cached})
        if not missing:
            assert set(result.probabilities) == set(rules), "Cached answers must cover every rule"
            return result
        async with self._semaphore:
            response = await self.client.system_one(
                state=state_for(language, source),
                questions={
                    name: Noul(instructions=rule.question) for name, rule in missing.items()
                },
                model=model,
            )
        fresh = {name: answer.noul for name, answer in response.nouls.items() if name in missing}
        self.store.save_answers(model, uhash, {hashes[name]: p for name, p in fresh.items()})
        result.probabilities |= fresh
        result.asked = len(missing)
        result.input_tokens = response.usage.input_tokens or 0
        result.output_tokens = response.usage.output_tokens or 0
        assert set(result.probabilities) == set(rules), "Every rule must have an answer"
        return result

    async def analyse(self, text: str, language: str, path: Path) -> list[Finding]:
        """Check every function in a file.

        Args:
            text: The file's current text.
            language: The file's configured language.
            path: The file's path, used to select rules and record results.

        Returns:
            The findings for rules that fired and were not labelled false.

        """
        assert language, "analyse needs a language name; get one from Config.language_for(path)"
        rules = self.config.rules_for(language, path)
        if not rules:
            return []
        units = extract_units(text, self.config.language(language), tests=self.config.is_test(path))
        hashes = [unit_hash(language, unit.source) for unit in units]
        assert len(hashes) == len(units), "Every unit must have a hash"
        self.store.save_units(
            language, list(zip(hashes, (unit.source for unit in units), strict=True))
        )
        results = await asyncio.gather(*(self.ask(language, unit.source, rules) for unit in units))
        assert len(results) == len(units), "Every unit must have an ask result"
        labels = self.store.labels(hashes)
        observations: list[Observation] = []
        findings: list[Finding] = []
        for unit, uhash, result in zip(units, hashes, results, strict=True):
            target = Target(unit.name, unit.first_line, unit.span, uhash)
            observed, fired = self.judge(target, result, rules, labels)
            observations += observed
            findings += fired
        self.record(path, language, observations, results)
        assert len(observations) == len(units) * len(rules), "Every rule must be observed per unit"
        return findings

    async def analyse_project(self, root: Path) -> list[tuple[Path, Finding]]:
        """Check the project rules against the project's files.

        Args:
            root: The project root that project rule globs are relative to.

        Returns:
            Each finding with the file it belongs on: the first matched file, or the root.

        """
        groups: dict[tuple[str, ...], dict[str, Rule]] = {}
        for name, rule in self.config.scoped_rules("project").items():
            assert rule.files, f"Project rule {name} has no files; the Rule validator must catch it"
            groups.setdefault(tuple(rule.files), {})[name] = rule
        findings: list[tuple[Path, Finding]] = []
        recorded: dict[Path, tuple[list[Observation], list[Asked]]] = {}
        for patterns, rules in groups.items():
            paths, source = await asyncio.to_thread(project_source, root, list(patterns))
            anchor = paths[0] if paths else root
            uhash = unit_hash(PROJECT, source)
            self.store.save_units(PROJECT, [(uhash, source)])
            result = await self.ask(PROJECT, source, rules)
            labels = self.store.labels([uhash])
            observations, results = recorded.setdefault(anchor, ([], []))
            results.append(result)
            target = Target(PROJECT, 0, Span(0, 0, 0, 0), uhash)
            observed, fired = self.judge(target, result, rules, labels)
            observations += observed
            findings += [(anchor, finding) for finding in fired]
        for anchor, (observations, results) in recorded.items():
            self.record(anchor, PROJECT, observations, results)
        assert all(finding.unit_hash for _, finding in findings), (
            "A project finding has no unit hash, so it cannot be labelled; set it from the source"
        )
        return findings

    async def analyse_settings(self, root: Path) -> list[tuple[Path, Finding]]:
        """Check every line of the files that setting rules read.

        Args:
            root: The project root that setting rule globs are relative to.

        Returns:
            Each finding with the file it belongs on, at the setting's line.

        """
        rules = self.config.scoped_rules(SETTING)
        paths = {
            path: None
            for rule in rules.values()
            for path in await asyncio.to_thread(project_files, root, rule.files or [])
        }
        findings: list[tuple[Path, Finding]] = []
        for path in paths:
            applying = {
                name: rule
                for name, rule in rules.items()
                if path in project_files(root, rule.files or [])
            }
            settings = await asyncio.to_thread(settings_in, root, path)
            sources = [setting.source() for setting in settings]
            hashes = [unit_hash(SETTING, source) for source in sources]
            self.store.save_units(SETTING, list(zip(hashes, sources, strict=True)))
            results = await asyncio.gather(*(self.ask(SETTING, s, applying) for s in sources))
            labels = self.store.labels(hashes)
            observations: list[Observation] = []
            for setting, uhash, result in zip(settings, hashes, results, strict=True):
                target = Target(
                    setting.keys or setting.text, setting.span.line, setting.span, uhash
                )
                observed, fired = self.judge(target, result, applying, labels)
                observations += observed
                findings += [(path, finding) for finding in fired]
            self.record(path, SETTING, observations, list(results))
        assert all(path in paths for path, _ in findings), "Findings belong to checked files"
        assert all(f.span.line >= 0 for _, f in findings), "Setting findings sit on a line"
        return findings

    def judge(
        self,
        target: Target,
        result: Asked,
        rules: dict[str, Rule],
        labels: dict[tuple[str, str], bool],
    ) -> tuple[list[Observation], list[Finding]]:
        """Decide which rules fire on one target.

        A rule fires when its probability reaches its threshold, unless the target is labelled
        false for that rule.

        Args:
            target: The function, project or setting the answers are about.
            result: The answers, one probability per rule.
            rules: The rules that were asked, by name.
            labels: Verdicts by rule and unit hash.

        Returns:
            One observation per rule, and a finding for each rule that fired.

        """
        assert set(result.probabilities) == set(rules), "judge needs an answer for every rule"
        observations: list[Observation] = []
        findings: list[Finding] = []
        for name, probability in result.probabilities.items():
            rule = rules[name]
            threshold = self.threshold(rule)
            fired = probability >= threshold and labels.get((name, target.unit_hash)) is not False
            observations.append(
                Observation(
                    name,
                    target.unit_hash,
                    target.name,
                    target.line,
                    question_hash(rule),
                    probability,
                    threshold,
                    fired,
                )
            )
            if fired:
                findings.append(
                    Finding(
                        name,
                        rule.message,
                        rule.severity,
                        probability,
                        target.span,
                        target.unit_hash,
                    )
                )
        assert len(findings) <= len(observations), "A rule fires at most once per target"
        return observations, findings

    def record(
        self, path: Path, language: str, observations: list[Observation], results: list[Asked]
    ) -> None:
        """Save the latest observations and the run's cost for one path.

        Args:
            path: The file the observations belong to.
            language: The observations' language, or ``project``.
            observations: One observation per rule and unit.
            results: The answers that produced the observations.

        """
        assert all(o.unit_hash for o in observations), "Observations must name their unit"
        key = str(path.resolve())
        asked = sum(r.asked for r in results)
        total = len(observations)
        assert asked <= total, "Cannot ask more questions than were observed"
        self.store.record_results(
            key,
            language,
            self.config.model,
            observations,
            RunStats(
                units=len(results),
                asked=asked,
                cached=total - asked,
                input_tokens=sum(r.input_tokens for r in results),
                output_tokens=sum(r.output_tokens for r in results),
            ),
        )
