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
        return f"{self.message} ({self.probability:.0%})" if show_probability else self.message


@dataclass
class Asked:
    probabilities: dict[str, float]
    asked: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def unit_hash(language: str, source: str) -> str:
    return digest(language, source)


def question_hash(rule: Rule) -> str:
    return digest(rule.question)


class Analyser:
    def __init__(self, config: Config, client: AsyncTypeSafeClient, store: Store):
        self.config = config
        self.client = client
        self.store = store
        self._semaphore = asyncio.Semaphore(config.concurrency)

    def threshold(self, rule: Rule) -> float:
        return self.config.threshold if rule.threshold is None else rule.threshold

    async def ask(self, language: str, source: str, rules: dict[str, Rule]) -> Asked:
        model = self.config.model
        hashes = {name: question_hash(rule) for name, rule in rules.items()}
        uhash = unit_hash(language, source)
        cached = self.store.answers(model, uhash, list(set(hashes.values())))
        missing = {name: rules[name] for name, qhash in hashes.items() if qhash not in cached}
        result = Asked({name: cached[qhash] for name, qhash in hashes.items() if qhash in cached})
        if not missing:
            return result
        async with self._semaphore:
            response = await self.client.system_one(
                state={"language": language, "function": source},
                questions={name: Noul(instructions=rule.question) for name, rule in missing.items()},
                model=model,
            )
        fresh = {name: answer.noul for name, answer in response.nouls.items() if name in missing}
        self.store.save_answers(model, uhash, {hashes[name]: p for name, p in fresh.items()})
        result.probabilities |= fresh
        result.asked = len(missing)
        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        return result

    async def analyse(self, text: str, language: str, path: Path) -> list[Finding]:
        rules = self.config.rules_for(language, path)
        if not rules:
            return []
        units = extract_units(text, self.config.languages[language])
        self.store.save_units(language, [(unit_hash(language, unit.source), unit.source) for unit in units])
        results = await asyncio.gather(*(self.ask(language, unit.source, rules) for unit in units))
        labels = self.store.labels([unit_hash(language, unit.source) for unit in units])
        observations: list[Observation] = []
        findings: list[Finding] = []
        for unit, result in zip(units, results):
            uhash = unit_hash(language, unit.source)
            for name, probability in result.probabilities.items():
                rule = rules[name]
                fired = probability >= self.threshold(rule) and labels.get((name, uhash)) is not False
                observations.append(
                    Observation(
                        name,
                        uhash,
                        unit.name,
                        unit.first_line,
                        question_hash(rule),
                        probability,
                        self.threshold(rule),
                        fired,
                    )
                )
                if fired:
                    findings.append(Finding(name, rule.message, rule.severity, probability, unit.span, uhash))
        key = str(path.resolve())
        self.store.replace_observations(key, language, self.config.model, observations)
        asked = sum(r.asked for r in results)
        self.store.record_run(
            key,
            len(units),
            asked,
            len(units) * len(rules) - asked,
            sum(r.input_tokens for r in results),
            sum(r.output_tokens for r in results),
        )
        return findings
