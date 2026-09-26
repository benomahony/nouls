# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""SQLite store for cached answers, observations, labels and run costs."""

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, get_args

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
    """Find the default store under the user's cache directory.

    Returns:
        ``$XDG_CACHE_HOME/nouls/nouls.db``, or ``~/.cache/nouls/nouls.db``.

    """
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "nouls" / "nouls.db"
    assert path.suffix == ".db", "Store must be a SQLite file"
    assert path.parent.name == "nouls", "Store must live in a nouls directory"
    return path


DIGEST_LENGTH = 32


def digest(*parts: str) -> str:
    """Hash text parts into a short stable identifier.

    Args:
        *parts: The text to hash, joined with NUL.

    Returns:
        The first 32 hex characters of the SHA-256.

    """
    assert parts, "Digest needs at least one part"
    value = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:DIGEST_LENGTH]
    assert len(value) == DIGEST_LENGTH, "Digest must be 32 hex characters"
    return value


def now() -> str:
    """Timestamp the current moment.

    Returns:
        The current UTC time in ISO 8601, to the second.

    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    assert stamp.endswith("+00:00"), (
        f"Timestamp {stamp} is not in UTC; build it from datetime.now(UTC)"
    )
    assert "T" in stamp, f"Timestamp {stamp} is not ISO 8601; format it with isoformat()"
    return stamp


@dataclass(frozen=True)
class Observation:
    """The latest answer for one rule on one unit in one file."""

    rule: str
    unit_hash: str
    unit_name: str
    line: int
    question_hash: str
    probability: float
    threshold: float
    fired: bool


@dataclass(frozen=True)
class RunStats:
    """How many units were checked and what the questions cost."""

    units: int
    asked: int
    cached: int
    input_tokens: int
    output_tokens: int


class Store:
    """SQLite cache of answers, observations, labels and runs, shared by every process."""

    path: Path
    db: sqlite3.Connection

    def __init__(self, path: Path) -> None:
        """Open or create the store in WAL mode.

        Args:
            path: The SQLite file.

        """
        assert not path.is_dir(), "Store path must be a file"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        _ = self.db.execute("PRAGMA journal_mode=WAL")
        _ = self.db.execute("PRAGMA synchronous=NORMAL")
        _ = self.db.executescript(SCHEMA)
        assert self.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "Store must use WAL"

    def save_units(self, language: str, units: Iterable[tuple[str, str]]) -> None:
        """Remember the source of units so they can be reviewed and re-asked.

        Args:
            language: The units' language, or ``project``.
            units: Each unit's hash and source.

        """
        assert language, "Language must not be empty"
        rows = [(unit_hash, language, source) for unit_hash, source in units]
        assert all(len(row[0]) == DIGEST_LENGTH for row in rows), "Unit hashes must be digests"
        with self.db:
            _ = self.db.executemany("INSERT OR IGNORE INTO units VALUES (?, ?, ?)", rows)

    def answers(
        self, model: str, unit_hash: str, question_hashes: Sequence[str]
    ) -> dict[str, float]:
        """Look up cached answers for one unit.

        Args:
            model: The model that answered.
            unit_hash: The unit.
            question_hashes: The questions to look up.

        Returns:
            The cached probability for each question that has one.

        """
        assert model, (
            "answers needs a model name; set model in your nouls config, such as jev-latest"
        )
        rows = self.rows(
            tuple[str, float],
            "SELECT question_hash, probability FROM answers WHERE model = ? AND unit_hash = ? "
            "AND question_hash IN (SELECT value FROM json_each(?))",
            (model, unit_hash, json.dumps(list(question_hashes))),
        )
        found = dict(rows)
        assert set(found) <= set(question_hashes), (
            "answers returned questions that were not requested; "
            "the query must filter on the question hashes it was given"
        )
        return found

    def save_answers(self, model: str, unit_hash: str, answers: dict[str, float]) -> None:
        """Cache answers for one unit.

        Args:
            model: The model that answered.
            unit_hash: The unit.
            answers: Each question's probability.

        """
        assert model, "Model must not be empty"
        assert all(0.0 <= p <= 1.0 for p in answers.values()), "Probabilities must be in [0, 1]"
        with self.db:
            _ = self.db.executemany(
                "INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?)",
                [
                    (model, question_hash, unit_hash, p, now())
                    for question_hash, p in answers.items()
                ],
            )

    def labels(self, unit_hashes: Sequence[str]) -> dict[tuple[str, str], bool]:
        """Look up the verdicts on some units.

        Args:
            unit_hashes: The units.

        Returns:
            Whether each labelled rule and unit is real.

        """
        assert all(unit_hashes), "Unit hashes must not be empty"
        rows = self.rows(
            tuple[str, str, int],
            "SELECT rule, unit_hash, real FROM labels "
            "WHERE unit_hash IN (SELECT value FROM json_each(?))",
            (json.dumps(list(unit_hashes)),),
        )
        found = {(rule, unit_hash): bool(real) for rule, unit_hash, real in rows}
        assert {key[1] for key in found} <= set(unit_hashes), "Only requested labels may return"
        return found

    def label(self, rule: str, unit_hash: str, *, real: bool) -> None:
        """Record whether a rule's finding on a unit is real.

        Args:
            rule: The rule.
            unit_hash: The unit.
            real: True for a real problem, False for not a problem.

        """
        assert rule, "Rule must not be empty"
        assert len(unit_hash) == DIGEST_LENGTH, "Unit hash must be a digest"
        with self.db:
            _ = self.db.execute(
                "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?)",
                (rule, unit_hash, int(real), now()),
            )

    def record_results(
        self,
        path: str,
        language: str,
        model: str,
        observations: Sequence[Observation],
        run: RunStats,
    ) -> None:
        """Replace a file's observations and record the run's cost.

        Args:
            path: The file.
            language: The observations' language, or ``project``.
            model: The model that answered.
            observations: One observation per rule and unit.
            run: The units checked and the cost of the questions.

        """
        assert path, "Path must not be empty"
        assert all(0.0 <= o.probability <= 1.0 for o in observations), "Probabilities in [0, 1]"
        assert run.asked >= 0, "Asked count must not be negative"
        assert run.cached >= 0, "Cached count must not be negative"
        seen = now()
        with self.db:
            _ = self.db.execute(
                "DELETE FROM observations WHERE path = ? AND language = ?", (path, language)
            )
            _ = self.db.executemany(
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
            _ = self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seen,
                    path,
                    run.units,
                    run.asked,
                    run.cached,
                    run.input_tokens,
                    run.output_tokens,
                ),
            )

    def query[Row: tuple[object, ...]](
        self, row: type[Row], sql: str, params: Sequence[object] = ()
    ) -> list[Row]:
        """Run a read only SQL query.

        Args:
            row: The type of each row, such as ``tuple[str, int]``, one entry per column.
            sql: A SELECT or WITH statement.
            params: The statement's parameters.

        Returns:
            Every row.

        """
        assert sql.strip(), "query needs SQL; pass a SELECT or WITH statement"
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), (
            "query only runs read only statements; write through the Store's own methods instead"
        )
        return self.rows(row, sql, params)

    def rows[Row: tuple[object, ...]](
        self, row: type[Row], sql: str, params: Sequence[object] = ()
    ) -> list[Row]:
        """Run SQL and type its rows.

        Args:
            row: The type of each row, one entry per column.
            sql: The statement.
            params: The statement's parameters.

        Returns:
            Every row, checked to have one value per entry in ``row``.

        """
        width = len(get_args(row))
        assert width, f"row must list its column types, such as tuple[str, int]; got {row}"
        found = cast("list[Row]", self.db.execute(sql, params).fetchall())
        assert all(len(values) == width for values in found), (
            f"The query returned rows that do not have {width} columns; "
            f"make the SELECT list match {row}"
        )
        return found
