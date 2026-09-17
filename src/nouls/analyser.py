import asyncio
from dataclasses import dataclass
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Noul

from nouls.config import Config, Rule, Severity
from nouls.store import Observation, Store, digest
from nouls.units import Span, extract_units


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    severity: Severity
    probability: float
    span: Span
    unit_hash: str

    def describe(self, show_probability: bool) -> str:
        assert self.message, "Finding must have a message"
        text = f"{self.message} ({self.probability:.0%})" if show_probability else self.message
        assert text.startswith(self.message), "Description must lead with the message"
        return text


@dataclass
class Asked:
    probabilities: dict[str, float]
    asked: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def unit_hash(language: str, source: str) -> str:
    assert language, "Language must not be empty"
    assert source, "Source must not be empty"
    return digest(language, source)


def question_hash(rule: Rule) -> str:
    assert rule.question.strip(), "Rule question must not be blank"
    value = digest(rule.question)
    assert len(value) == 32, "Question hash must be a digest"
    return value


class Analyser:
    def __init__(self, config: Config, client: AsyncTypeSafeClient, store: Store):
        assert config.concurrency > 0, "Concurrency must be positive"
        assert config.languages, "At least one language must be configured"
        self.config = config
        self.client = client
        self.store = store
        self._semaphore = asyncio.Semaphore(config.concurrency)

    def threshold(self, rule: Rule) -> float:
        assert rule.enabled, "Thresholds apply to enabled rules"
        value = self.config.threshold if rule.threshold is None else rule.threshold
        assert 0.0 <= value <= 1.0, "Threshold must be in [0, 1]"
        return value

    async def ask(self, language: str, source: str, rules: dict[str, Rule]) -> Asked:
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
                state={"language": language, "function": source},
                questions={
                    name: Noul(instructions=rule.question) for name, rule in missing.items()
                },
                model=model,
            )
        fresh = {name: answer.noul for name, answer in response.nouls.items() if name in missing}
        self.store.save_answers(model, uhash, {hashes[name]: p for name, p in fresh.items()})
        result.probabilities |= fresh
        result.asked = len(missing)
        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        assert set(result.probabilities) == set(rules), "Every rule must have an answer"
        return result

    async def analyse(self, text: str, language: str, path: Path) -> list[Finding]:
        assert language in self.config.languages, "Language must be configured"
        rules = self.config.rules_for(language, path)
        if not rules:
            return []
        units = extract_units(text, self.config.languages[language])
        hashes = [unit_hash(language, unit.source) for unit in units]
        self.store.save_units(language, list(zip(hashes, (unit.source for unit in units))))
        results = await asyncio.gather(*(self.ask(language, unit.source, rules) for unit in units))
        labels = self.store.labels(hashes)
        observations: list[Observation] = []
        findings: list[Finding] = []
        for unit, uhash, result in zip(units, hashes, results):
            for name, probability in result.probabilities.items():
                rule = rules[name]
                threshold = self.threshold(rule)
                fired = probability >= threshold and labels.get((name, uhash)) is not False
                observations.append(
                    Observation(
                        name,
                        uhash,
                        unit.name,
                        unit.first_line,
                        question_hash(rule),
                        probability,
                        threshold,
                        fired,
                    )
                )
                if fired:
                    findings.append(
                        Finding(name, rule.message, rule.severity, probability, unit.span, uhash)
                    )
        self.record(path, language, observations, results)
        assert len(observations) == len(units) * len(rules), "Every rule must be observed per unit"
        return findings

    def record(
        self, path: Path, language: str, observations: list[Observation], results: list[Asked]
    ) -> None:
        assert all(o.unit_hash for o in observations), "Observations must name their unit"
        key = str(path.resolve())
        asked = sum(r.asked for r in results)
        total = len(observations)
        assert asked <= total, "Cannot ask more questions than were observed"
        self.store.replace_observations(key, language, self.config.model, observations)
        self.store.record_run(
            key,
            len(results),
            asked,
            total - asked,
            sum(r.input_tokens for r in results),
            sum(r.output_tokens for r in results),
        )
