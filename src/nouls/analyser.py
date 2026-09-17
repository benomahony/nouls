import asyncio
import hashlib
from dataclasses import dataclass

from typesafe_sdk import AsyncTypeSafeClient, Noul

from nouls.config import Config, Rule, Severity
from nouls.units import Span, Unit, extract_units


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    severity: Severity
    probability: float
    span: Span

    def describe(self, show_probability: bool) -> str:
        return f"{self.message} ({self.probability:.0%})" if show_probability else self.message


class Analyser:
    def __init__(self, config: Config, client: AsyncTypeSafeClient, cache_size: int = 2048):
        self.config = config
        self.client = client
        self.cache_size = cache_size
        self._cache: dict[str, dict[str, float]] = {}
        self._semaphore = asyncio.Semaphore(config.concurrency)

    def threshold(self, rule: Rule) -> float:
        return self.config.threshold if rule.threshold is None else rule.threshold

    async def _ask(self, language: str, unit: Unit, rules: dict[str, Rule]) -> dict[str, float]:
        key = hashlib.sha256(
            "\0".join(
                [self.config.model, language, unit.source, *(f"{n}={r.question}" for n, r in sorted(rules.items()))]
            ).encode()
        ).hexdigest()
        if key in self._cache:
            return self._cache[key]
        async with self._semaphore:
            response = await self.client.system_one(
                state={"language": language, "function": unit.source},
                questions={name: Noul(instructions=rule.question) for name, rule in rules.items()},
                model=self.config.model,
            )
        answers = {name: answer.noul for name, answer in response.nouls.items()}
        self._cache[key] = answers
        while len(self._cache) > self.cache_size:
            del self._cache[next(iter(self._cache))]
        return answers

    async def analyse(self, text: str, language: str) -> list[Finding]:
        rules = self.config.rules_for(language)
        if not rules:
            return []
        units = extract_units(text, self.config.languages[language])
        results = await asyncio.gather(*(self._ask(language, unit, rules) for unit in units))
        return [
            Finding(name, rules[name].message, rules[name].severity, probability, unit.span)
            for unit, answers in zip(units, results)
            for name, probability in answers.items()
            if name in rules and probability >= self.threshold(rules[name])
        ]
