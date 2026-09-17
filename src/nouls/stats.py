import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter, validators
from rich.console import Console
from rich.table import Table
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, question_hash
from nouls.config import Config, load_config
from nouls.store import Store

stats = App(name="stats", help="Analyse stored answers, findings and labels.")
console = Console()

ConfigOption = Annotated[
    Path | None, Parameter(name=["--config", "-c"], help="Path to a nouls.yaml file.")
]

Positive = Annotated[int, Parameter(validator=validators.Number(gt=0))]
Price = Annotated[float, Parameter(validator=validators.Number(gte=0))]

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
    assert loaded.rules, "Config must define rules"
    store = Store(loaded.store_path())
    assert store.path == loaded.store_path(), "Store must open the configured file"
    return loaded, store


def sparkline(counts: list[int]) -> str:
    assert counts, "Sparkline needs at least one bucket"
    peak = max(counts) or 1
    line = "".join(BARS[round(c / peak * (len(BARS) - 1))] for c in counts)
    assert len(line) == len(counts), "One bar per bucket"
    return line


def display(path: str) -> str:
    assert path, "Path must not be empty"
    candidate = Path(path)
    shown = str(candidate.relative_to(Path.cwd())) if candidate.is_relative_to(Path.cwd()) else path
    assert shown.endswith(candidate.name), "Displayed path must keep the file name"
    return shown


@stats.command
def rules(*, config: ConfigOption = None) -> None:
    """Spread of probabilities, fire rate and ambiguity for every rule."""
    _, store = open_store(config)
    buckets: dict[str, list[int]] = defaultdict(lambda: [0] * 10)
    for rule, bucket, count in store.query(
        """
        SELECT rule, MIN(CAST(probability * 10 AS INTEGER), 9), COUNT(*)
        FROM observations GROUP BY 1, 2
        """
    ):
        buckets[rule][bucket] = count
    assert all(len(counts) == 10 for counts in buckets.values()), "Ten buckets per rule"
    table = Table("rule", "functions", "fires", "ambiguous", "mean", "0 ▸ 1", "labels")
    for rule, n, fires, ambiguous, mean, labels in store.query(
        """
        SELECT o.rule, COUNT(*), AVG(o.probability >= o.threshold),
               AVG(o.probability BETWEEN 0.35 AND 0.65), AVG(o.probability),
               (SELECT COUNT(*) FROM labels l WHERE l.rule = o.rule)
        FROM observations o GROUP BY o.rule ORDER BY 3 DESC
        """
    ):
        table.add_row(
            rule,
            str(n),
            f"{fires:.0%}",
            f"{ambiguous:.0%}",
            f"{mean:.2f}",
            sparkline(buckets[rule]),
            str(labels),
        )
    assert table.row_count == len(buckets), "One row per observed rule"
    console.print(table)


@stats.command
def hotspots(*, limit: Positive = 20, config: ConfigOption = None) -> None:
    """Files and functions with the most findings."""
    assert limit > 0, "Limit must be positive"
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
    assert functions.row_count <= limit, "Function table must respect the limit"
    console.print(functions)


@stats.command
def cost(
    *, days: Positive = 14, price_per_million: Price = 0.042, config: ConfigOption = None
) -> None:
    """Questions asked, cache hit rate and input token spend per day."""
    assert days > 0, "Days must be positive"
    assert price_per_million >= 0, "Price must not be negative"
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


def score(
    pairs: list[tuple[float, bool]], threshold: float
) -> tuple[int, float | None, float | None]:
    assert 0.0 <= threshold <= 1.0, "Threshold must be in [0, 1]"
    tp = sum(1 for p, real in pairs if real and p >= threshold)
    fp = sum(1 for p, real in pairs if not real and p >= threshold)
    fn = sum(1 for p, real in pairs if real and p < threshold)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    assert tp + fp + fn <= len(pairs), "Counts cannot exceed the labelled pairs"
    return tp + fp, precision, recall


def percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    assert 0.0 <= value <= 1.0, "Ratio must be in [0, 1]"
    text = f"{value:.0%}"
    assert text.endswith("%"), "Ratio must render as a percentage"
    return text


async def ask_missing(loaded: Config, store: Store, missing: list[tuple[str, str, str]]) -> None:
    assert missing, "There must be something to ask"
    assert all(item[0] in loaded.rules for item in missing), "Every rule must be configured"
    async with AsyncTypeSafeClient() as client:
        analyser = Analyser(loaded, client, store)
        await asyncio.gather(
            *(
                analyser.ask(language, source, {rule: loaded.rules[rule]})
                for rule, language, source in missing
            )
        )


def labelled(loaded: Config, store: Store, name: str) -> list[tuple[bool, float | None, str, str]]:
    assert name in loaded.rules, "Rule must be configured"
    rows = [
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
    assert all(row[3] for row in rows), "Every labelled function must have source"
    return rows


def threshold_table(
    name: str, rows: list[tuple[bool, float | None, str, str]], active: float
) -> Table:
    assert rows, "A table needs labelled rows"
    pairs = [(p, real) for real, p, _, _ in rows if p is not None]
    unanswered = len(rows) - len(pairs)
    positives = sum(real for _, real in pairs)
    title = f"{name}  {positives} real, {len(pairs) - positives} false"
    if unanswered:
        title += f", {unanswered} unanswered (use --ask)"
    table = Table("threshold", "fires", "precision", "recall", title=title)
    for threshold in sorted({*THRESHOLDS, active}):
        fires, precision, recall = score(pairs, threshold)
        marker = " ◂ current" if threshold == active else ""
        table.add_row(f"{threshold:.2f}{marker}", str(fires), percent(precision), percent(recall))
    assert table.row_count >= len(THRESHOLDS), "Every standard threshold must be shown"
    return table


@stats.command
def thresholds(rule: str | None = None, *, ask: bool = False, config: ConfigOption = None) -> int:
    """Precision and recall at each threshold, from your labels and the current question wording.

    Parameters
    ----------
    rule
        Only show this rule.
    ask
        Ask the current question for labelled functions that have no answer yet.
    """
    loaded, store = open_store(config)
    if rule is not None and rule not in loaded.rules:
        print(f"nouls: unknown rule {rule}, see nouls rules", file=sys.stderr)
        return 2
    names = [rule] if rule else [name for name, r in loaded.rules.items() if r.enabled]
    assert all(name in loaded.rules for name in names), "Every rule must be configured"
    if ask:
        missing = [
            (name, language, source)
            for name in names
            for _, p, language, source in labelled(loaded, store, name)
            if p is None
        ]
        if missing:
            asyncio.run(ask_missing(loaded, store, missing))
    shown = 0
    for name in names:
        rows = labelled(loaded, store, name)
        if rows:
            current = loaded.rules[name]
            active = loaded.threshold if current.threshold is None else current.threshold
            console.print(threshold_table(name, rows, active))
            shown += 1
    assert shown <= len(names), "Cannot show more tables than rules"
    return 0
