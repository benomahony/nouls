import hashlib
import json
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
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "nouls" / "nouls.db"
    assert path.suffix == ".db", "Store must be a SQLite file"
    assert path.parent.name == "nouls", "Store must live in a nouls directory"
    return path


def digest(*parts: str) -> str:
    assert parts, "Digest needs at least one part"
    value = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]
    assert len(value) == 32, "Digest must be 32 hex characters"
    return value


def now() -> str:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    assert stamp.endswith("+00:00"), "Timestamps must be UTC"
    assert "T" in stamp, "Timestamps must be ISO 8601"
    return stamp


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
        assert not path.is_dir(), "Store path must be a file"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        assert self.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "Store must use WAL"

    def save_units(self, language: str, units: Iterable[tuple[str, str]]) -> None:
        assert language, "Language must not be empty"
        rows = [(unit_hash, language, source) for unit_hash, source in units]
        assert all(len(row[0]) == 32 for row in rows), "Unit hashes must be digests"
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO units VALUES (?, ?, ?)", rows)

    def answers(
        self, model: str, unit_hash: str, question_hashes: Sequence[str]
    ) -> dict[str, float]:
        assert model, "Model must not be empty"
        rows = self.db.execute(
            "SELECT question_hash, probability FROM answers WHERE model = ? AND unit_hash = ? "
            "AND question_hash IN (SELECT value FROM json_each(?))",
            (model, unit_hash, json.dumps(list(question_hashes))),
        )
        found = dict(rows.fetchall())
        assert set(found) <= set(question_hashes), "Only requested answers may be returned"
        return found

    def save_answers(self, model: str, unit_hash: str, answers: dict[str, float]) -> None:
        assert model, "Model must not be empty"
        assert all(0.0 <= p <= 1.0 for p in answers.values()), "Probabilities must be in [0, 1]"
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?)",
                [
                    (model, question_hash, unit_hash, p, now())
                    for question_hash, p in answers.items()
                ],
            )

    def labels(self, unit_hashes: Sequence[str]) -> dict[tuple[str, str], bool]:
        assert all(unit_hashes), "Unit hashes must not be empty"
        rows = self.db.execute(
            "SELECT rule, unit_hash, real FROM labels "
            "WHERE unit_hash IN (SELECT value FROM json_each(?))",
            (json.dumps(list(unit_hashes)),),
        )
        found = {(rule, unit_hash): bool(real) for rule, unit_hash, real in rows}
        assert {key[1] for key in found} <= set(unit_hashes), "Only requested labels may return"
        return found

    def label(self, rule: str, unit_hash: str, real: bool) -> None:
        assert rule, "Rule must not be empty"
        assert len(unit_hash) == 32, "Unit hash must be a digest"
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?)",
                (rule, unit_hash, int(real), now()),
            )

    def replace_observations(
        self, path: str, language: str, model: str, observations: Sequence[Observation]
    ) -> None:
        assert path, "Path must not be empty"
        assert all(0.0 <= o.probability <= 1.0 for o in observations), "Probabilities in [0, 1]"
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

    def record_run(
        self, path: str, units: int, asked: int, cached: int, input_tokens: int, output_tokens: int
    ) -> None:
        assert asked >= 0, "Asked count must not be negative"
        assert cached >= 0, "Cached count must not be negative"
        with self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now(), path, units, asked, cached, input_tokens, output_tokens),
            )

    def query(self, sql: str, params: Sequence[object] = ()) -> list[tuple]:
        assert sql.strip(), "Query must not be empty"
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), "Only read queries allowed"
        return self.db.execute(sql, params).fetchall()
