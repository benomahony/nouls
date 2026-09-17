import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from rich.console import Console
from rich.table import Table
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, question_hash
from nouls.config import Config, load_config
from nouls.store import Store

stats = App(name="stats", help="Analyse stored answers, findings and labels.")
console = Console()

ConfigOption = Annotated[Path | None, Parameter(name=["--config", "-c"], help="Path to a nouls.yaml file.")]

BARS = " ▁▂▃▄▅▆▇█"
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]

SAMPLE = """
WITH candidates AS (
    SELECT o.unit_hash, u.language, u.source, o.unit_name, o.path, o.line, o.probability,
           ROW_NUMBER() OVER (PARTITION BY o.unit_hash ORDER BY o.seen_at DESC) AS copy
    FROM observations o
    JOIN units u USING (unit_hash)
    LEFT JOIN labels l ON l.rule = o.rule AND l.unit_hash = o.unit_hash
    WHERE o.rule = ? AND l.unit_hash IS NULL
),
banded AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY MIN(CAST(probability * 5 AS INTEGER), 4) ORDER BY RANDOM()
    ) AS turn
    FROM candidates WHERE copy = 1
)
SELECT unit_hash, language, source, unit_name, path, line, probability
FROM banded ORDER BY turn, probability DESC LIMIT ?
"""


def open_store(config: Path | None) -> tuple[Config, Store]:
    loaded = load_config(Path.cwd(), config)
    return loaded, Store(loaded.store_path())


def sparkline(counts: list[int]) -> str:
    peak = max(counts) or 1
    return "".join(BARS[round(c / peak * (len(BARS) - 1))] for c in counts)


def display(path: str) -> str:
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path


@stats.command
def rules(*, config: ConfigOption = None) -> None:
    """Spread of probabilities, fire rate and ambiguity for every rule."""
    _, store = open_store(config)
    buckets: dict[str, list[int]] = defaultdict(lambda: [0] * 10)
    for rule, bucket, count in store.query(
        "SELECT rule, MIN(CAST(probability * 10 AS INTEGER), 9), COUNT(*) FROM observations GROUP BY 1, 2"
    ):
        buckets[rule][bucket] = count
    table = Table("rule", "functions", "fires", "ambiguous", "mean", "0 ▸ 1", "labels")
    for rule, n, fires, ambiguous, mean, labels in store.query(
        """
        SELECT o.rule, COUNT(*), AVG(o.probability >= o.threshold), AVG(o.probability BETWEEN 0.35 AND 0.65),
               AVG(o.probability), (SELECT COUNT(*) FROM labels l WHERE l.rule = o.rule)
        FROM observations o GROUP BY o.rule ORDER BY 3 DESC
        """
    ):
        table.add_row(
            rule, str(n), f"{fires:.0%}", f"{ambiguous:.0%}", f"{mean:.2f}", sparkline(buckets[rule]), str(labels)
        )
    console.print(table)


@stats.command
def hotspots(*, limit: int = 20, config: ConfigOption = None) -> None:
    """Files and functions with the most findings."""
    _, store = open_store(config)
    files = Table("file", "findings", "functions")
    for path, fired, functions in store.query(
        """
        SELECT path, SUM(fired), COUNT(DISTINCT unit_hash) FROM observations
        GROUP BY path HAVING SUM(fired) > 0 ORDER BY 2 DESC LIMIT ?
        """,
        (limit,),
    ):
        files.add_row(display(path), str(fired), str(functions))
    console.print(files)
    functions = Table("function", "location", "rules")
    for name, path, line, found in store.query(
        """
        SELECT unit_name, path, line, GROUP_CONCAT(rule, ', ') FROM observations WHERE fired
        GROUP BY path, unit_hash, line ORDER BY COUNT(*) DESC LIMIT ?
        """,
        (limit,),
    ):
        functions.add_row(name, f"{display(path)}:{line + 1}", found)
    console.print(functions)


@stats.command
def cost(*, days: int = 14, price_per_million: float = 0.042, config: ConfigOption = None) -> None:
    """Questions asked, cache hit rate and input token spend per day."""
    _, store = open_store(config)
    table = Table("day", "runs", "asked", "cached", "hit rate", "input tokens", "cost")
    for day, runs, asked, cached, tokens in store.query(
        """
        SELECT SUBSTR(at, 1, 10), COUNT(*), SUM(asked), SUM(cached), SUM(input_tokens) FROM runs
        GROUP BY 1 ORDER BY 1 DESC LIMIT ?
        """,
        (days,),
    ):
        total = asked + cached
        table.add_row(
            day,
            str(runs),
            str(asked),
            str(cached),
            f"{cached / total:.0%}" if total else "n/a",
            f"{tokens:,}",
            f"${tokens / 1_000_000 * price_per_million:.4f}",
        )
    console.print(table)


def score(pairs: list[tuple[float, bool]], threshold: float) -> tuple[int, float | None, float | None]:
    tp = sum(1 for p, real in pairs if real and p >= threshold)
    fp = sum(1 for p, real in pairs if not real and p >= threshold)
    fn = sum(1 for p, real in pairs if real and p < threshold)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return tp + fp, precision, recall


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


async def ask_missing(loaded: Config, store: Store, missing: list[tuple[str, str, str]]) -> None:
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(loaded, client, store)
        await asyncio.gather(
            *(analyser.ask(language, source, {rule: loaded.rules[rule]}) for rule, language, source in missing)
        )


def labelled(loaded: Config, store: Store, name: str) -> list[tuple[bool, float | None, str, str]]:
    return [
        (bool(real), p, language, source)
        for real, p, language, source in store.query(
            """
            SELECT l.real, a.probability, u.language, u.source FROM labels l
            JOIN units u USING (unit_hash)
            LEFT JOIN answers a ON a.unit_hash = l.unit_hash AND a.model = ? AND a.question_hash = ?
            WHERE l.rule = ?
            """,
            (loaded.model, question_hash(loaded.rules[name]), name),
        )
    ]


@stats.command
def thresholds(rule: str | None = None, *, ask: bool = False, config: ConfigOption = None) -> None:
    """Precision and recall at each threshold, from your labels and the current question wording.

    Parameters
    ----------
    rule
        Only show this rule.
    ask
        Ask the current question for labelled functions that have no answer yet.
    """
    loaded, store = open_store(config)
    names = [rule] if rule else [name for name, r in loaded.rules.items() if r.enabled]
    if ask:
        missing = [
            (name, language, source)
            for name in names
            for _, p, language, source in labelled(loaded, store, name)
            if p is None
        ]
        if missing:
            asyncio.run(ask_missing(loaded, store, missing))
    for name in names:
        rows = labelled(loaded, store, name)
        if not rows:
            continue
        pairs = [(p, real) for real, p, _, _ in rows if p is not None]
        unanswered = len(rows) - len(pairs)
        positives = sum(real for _, real in pairs)
        table = Table(
            "threshold",
            "fires",
            "precision",
            "recall",
            title=f"{name}  {positives} real, {len(pairs) - positives} false"
            + (f", {unanswered} unanswered (use --ask)" if unanswered else ""),
        )
        current = loaded.rules[name]
        active = loaded.threshold if current.threshold is None else current.threshold
        for threshold in sorted({*THRESHOLDS, active}):
            fires, precision, recall = score(pairs, threshold)
            marker = " ◂ current" if threshold == active else ""
            table.add_row(f"{threshold:.2f}{marker}", str(fires), percent(precision), percent(recall))
        console.print(table)
