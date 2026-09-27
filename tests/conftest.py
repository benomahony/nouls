# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Shared fixtures and fakes for the nouls tests."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Self, cast

import pytest
from lsprotocol import types
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser
from nouls.cli import main
from nouls.config import Config, load_config
from nouls.server import NoulsServer
from nouls.store import Store

PYTHON = """import time


class Session:
    def expired(self, timeout_s: int) -> bool:
        return time.time() * 1000 - self.started > timeout_s


def total(items: list[int]) -> int:
    return sum(items)
"""
PYTHON_FUNCTIONS = 2
PYTHON_CALLS = PYTHON_FUNCTIONS + 1  # One question batch per function, and one about the file.
# The build files' questions, the operations files' questions, and the strict pyproject's only
# setting.
PROJECT_CALLS = 3

APP = Path("src/app.py")

STRICT = '[tool.basedpyright]\ntypeCheckingMode = "strict"\n'


@dataclass
class Answer:
    """One probability, shaped like TypeSafe's answer."""

    noul: float


@dataclass
class Usage:
    """Token counts, shaped like TypeSafe's usage."""

    input_tokens: int
    output_tokens: int


@dataclass
class Response:
    """Answers and usage, shaped like TypeSafe's response."""

    nouls: dict[str, Answer]
    usage: Usage


@dataclass
class Call:
    """One question batch the fake client was asked."""

    state: Mapping[str, object]
    questions: set[str]
    model: str


@dataclass
class FakeClient:
    """TypeSafe stand in that flags ms/s mixing and projects without strict settings."""

    calls: list[Call] = field(default_factory=list)
    error: Exception | None = None

    async def __aenter__(self) -> Self:
        """Open the fake client.

        Returns:
            The client itself.

        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the fake client."""

    async def system_one(
        self, state: Mapping[str, object], questions: Mapping[str, object], model: str
    ) -> Response:
        """Answer every question, recording what was asked.

        Args:
            state: The function or project files being asked about.
            questions: The questions, by rule name.
            model: The model that would answer.

        Returns:
            0.95 for flagged rules and 0.05 for the rest.

        """
        self.calls.append(Call(state, set(questions), model))
        if self.error is not None:
            raise self.error
        files = state.get("files")
        function = state.get("function")
        line = state.get("line")
        setting = state.get("setting")
        flagged: set[str] = set()
        if isinstance(files, dict):
            texts = cast("dict[str, str]", files).values()
            if not any("strict" in text for text in texts):
                flagged.add("unscheduled_static_analysis")
        elif isinstance(line, str) and "ignore" in f"{setting} {line}":
            flagged.add("relaxed_warnings")
        elif isinstance(function, str) and "* 1000" in function:
            flagged.add("unit_mismatch")
        return Response(
            {name: Answer(0.95 if name in flagged else 0.05) for name in questions},
            Usage(100 * len(questions), 0),
        )


def as_client(fake: FakeClient) -> AsyncTypeSafeClient:
    """Pass the fake where the real client is expected.

    Args:
        fake: The fake client.

    Returns:
        The same object, typed as the real client.

    """
    return cast("AsyncTypeSafeClient", cast("object", fake))


@dataclass
class FakeServer:
    """Language server stand in that serves one document and records diagnostics."""

    analyser: Analyser
    document: SimpleNamespace
    client: FakeClient
    pending: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    published: list[types.PublishDiagnosticsParams] = field(default_factory=list)
    warned: set[str] = field(default_factory=set)
    shown: list[types.ShowMessageParams] = field(default_factory=list)

    @property
    def workspace(self) -> SimpleNamespace:
        """Serve the one document for any URI.

        Returns:
            A workspace whose get_text_document returns the document.

        """

        def get_text_document(_uri: str) -> SimpleNamespace:
            return self.document

        return SimpleNamespace(get_text_document=get_text_document)

    def text_document_publish_diagnostics(self, params: types.PublishDiagnosticsParams) -> None:
        """Record published diagnostics.

        Args:
            params: The diagnostics for one document.

        """
        self.published.append(params)

    def window_show_message(self, params: types.ShowMessageParams) -> None:
        """Record messages shown in the editor.

        Args:
            params: The message.

        """
        self.shown.append(params)


def as_server(fake: FakeServer) -> NoulsServer:
    """Pass the fake where the real language server is expected.

    Args:
        fake: The fake server.

    Returns:
        The same object, typed as the real server.

    """
    return cast("NoulsServer", cast("object", fake))


def run(*tokens: str) -> int:
    """Run the nouls command line in process.

    Args:
        *tokens: The command line arguments.

    Returns:
        The exit code.

    """
    with pytest.raises(SystemExit) as exit_info:
        main(list(tokens))
    code = exit_info.value.code
    return code if isinstance(code, int) else 0


def scripted(*replies: str) -> Callable[..., str]:
    """Answer prompts with fixed replies, in order.

    Args:
        *replies: What the user would type at each prompt.

    Returns:
        A stand in for Prompt.ask.

    """
    remaining = iter(replies)

    def reply(*_args: object, **_kwargs: object) -> str:
        return next(remaining)

    return reply


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each test in its own strict project with its own store.

    Applied to every test through ``usefixtures`` in pyproject.toml.

    Returns:
        The project directory, which is also the working directory.

    """
    monkeypatch.setattr("nouls.config.default_path", lambda: tmp_path / "xdg" / "nouls.db")
    monkeypatch.chdir(tmp_path)
    _ = (tmp_path / "pyproject.toml").write_text(STRICT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Load the default configuration for the test project.

    Returns:
        The configuration.

    """
    return load_config(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Open a store private to the test.

    Returns:
        The store.

    """
    return Store(tmp_path / "cache" / "nouls.db")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Replace TypeSafe with the fake everywhere nouls creates a client.

    Returns:
        The fake client, to inspect its calls.

    """
    fake = FakeClient()
    monkeypatch.setattr("nouls.cli.AsyncTypeSafeClient", lambda: fake)
    monkeypatch.setattr("nouls.stats.AsyncTypeSafeClient", lambda: fake)
    monkeypatch.setattr("nouls.server.AsyncTypeSafeClient", lambda: fake)
    return fake


@pytest.fixture
def ls(tmp_path: Path, config: Config, store: Store) -> FakeServer:
    """Serve app.py from a fake language server with no debounce.

    Returns:
        The fake server.

    """
    config.debounce_ms = 0
    path = tmp_path / "app.py"
    _ = path.write_text(PYTHON, encoding="utf-8")
    document = SimpleNamespace(path=str(path), source=PYTHON, version=3)
    fake = FakeClient()
    return FakeServer(Analyser(config, as_client(fake), store), document, fake)
