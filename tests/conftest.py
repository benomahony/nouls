from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from typesafe_sdk import AsyncTypeSafeClient

from nouls.config import Config, load_config
from nouls.store import Store

PYTHON = """import time


class Session:
    def expired(self, timeout_s: int) -> bool:
        return time.time() * 1000 - self.started > timeout_s


def total(items: list[int]) -> int:
    return sum(items)
"""

APP = Path("src/app.py")


@dataclass
class Answer:
    noul: float


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class Response:
    nouls: dict[str, Answer]
    usage: Usage


@dataclass
class FakeClient:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def system_one(
        self, state: dict[str, str], questions: dict[str, Any], model: str
    ) -> Response:
        self.calls.append({"state": state, "questions": set(questions)})
        flagged = "* 1000" in state["function"]
        return Response(
            {
                name: Answer(0.95 if flagged and name == "unit_mismatch" else 0.05)
                for name in questions
            },
            Usage(100 * len(questions), 0),
        )


def as_client(fake: FakeClient) -> AsyncTypeSafeClient:
    return cast(AsyncTypeSafeClient, fake)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("nouls.config.default_path", lambda: tmp_path / "xdg" / "nouls.db")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return load_config(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "cache" / "nouls.db")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr("nouls.cli.AsyncTypeSafeClient", lambda: fake)
    monkeypatch.setattr("nouls.stats.AsyncTypeSafeClient", lambda: fake)
    monkeypatch.setattr("nouls.server.AsyncTypeSafeClient", lambda: fake)
    return fake
