import hashlib
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    unit_hash TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
    model TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    unit_hash TEXT NOT NULL,
    probability REAL NOT NULL,
    asked_at TEXT NOT NULL,
    PRIMARY KEY (model, question_hash, unit_hash)
);
CREATE TABLE IF NOT EXISTS observations (
    path TEXT NOT NULL,
    rule TEXT NOT NULL,
    unit_hash TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    line INTEGER NOT NULL,
    language TEXT NOT NULL,
    model TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    probability REAL NOT NULL,
    threshold REAL NOT NULL,
    fired INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (path, rule, unit_hash, line)
);
CREATE TABLE IF NOT EXISTS labels (
    rule TEXT NOT NULL,
    unit_hash TEXT NOT NULL,
    real INTEGER NOT NULL,
    labelled_at TEXT NOT NULL,
    PRIMARY KEY (rule, unit_hash)
);
CREATE TABLE IF NOT EXISTS runs (
    at TEXT NOT NULL,
    path TEXT NOT NULL,
    units INTEGER NOT NULL,
    asked INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);
"""


def default_path() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "nouls" / "nouls.db"


def digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Observation:
    rule: str
    unit_hash: str
    unit_name: str
    line: int
    question_hash: str
    probability: float
    threshold: float
    fired: bool


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def save_units(self, language: str, units: Iterable[tuple[str, str]]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO units VALUES (?, ?, ?)",
                [(unit_hash, language, source) for unit_hash, source in units],
            )

    def answers(self, model: str, unit_hash: str, question_hashes: Sequence[str]) -> dict[str, float]:
        marks = ",".join("?" * len(question_hashes))
        rows = self.db.execute(
            f"SELECT question_hash, probability FROM answers "
            f"WHERE model = ? AND unit_hash = ? AND question_hash IN ({marks})",
            [model, unit_hash, *question_hashes],
        )
        return dict(rows.fetchall())

    def save_answers(self, model: str, unit_hash: str, answers: dict[str, float]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?)",
                [(model, question_hash, unit_hash, p, now()) for question_hash, p in answers.items()],
            )

    def labels(self, unit_hashes: Sequence[str]) -> dict[tuple[str, str], bool]:
        marks = ",".join("?" * len(unit_hashes))
        rows = self.db.execute(
            f"SELECT rule, unit_hash, real FROM labels WHERE unit_hash IN ({marks})", list(unit_hashes)
        )
        return {(rule, unit_hash): bool(real) for rule, unit_hash, real in rows}

    def label(self, rule: str, unit_hash: str, real: bool) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?)", (rule, unit_hash, int(real), now()))

    def replace_observations(self, path: str, language: str, model: str, observations: Sequence[Observation]) -> None:
        seen = now()
        with self.db:
            self.db.execute("DELETE FROM observations WHERE path = ?", (path,))
            self.db.executemany(
                "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        o.rule,
                        o.unit_hash,
                        o.unit_name,
                        o.line,
                        language,
                        model,
                        o.question_hash,
                        o.probability,
                        o.threshold,
                        int(o.fired),
                        seen,
                    )
                    for o in observations
                ],
            )

    def record_run(self, path: str, units: int, asked: int, cached: int, input_tokens: int, output_tokens: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now(), path, units, asked, cached, input_tokens, output_tokens),
            )

    def query(self, sql: str, params: Sequence[object] = ()) -> list[tuple]:
        return self.db.execute(sql, params).fetchall()
