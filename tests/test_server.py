# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for the nouls language server."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from lsprotocol import types

from nouls.server import (
    NoulsServer,
    code_actions,
    did_change,
    did_close,
    did_open,
    did_save,
    label,
    lint,
    unit_hash_of,
)
from nouls.store import DIGEST_LENGTH
from tests.conftest import PYTHON, FakeClient, FakeServer, as_server

pytestmark = pytest.mark.unit


class ParserCrashedError(RuntimeError):
    """A stand in for an unexpected failure inside analysis."""


def identifier(uri: str) -> types.TextDocumentIdentifier:
    """Name a document for the language server.

    Args:
        uri: The document's URI.

    Returns:
        The document identifier.

    """
    return types.TextDocumentIdentifier(uri=uri)


async def settle(ls: FakeServer, uri: str) -> None:
    """Wait for a document's pending lint to finish.

    Args:
        ls: The fake language server.
        uri: The document.

    """
    await ls.pending[uri]


async def test_open_publishes_diagnostics_with_function_hash(ls: FakeServer) -> None:
    """Open publishes diagnostics with function hash."""
    did_open(
        as_server(ls),
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri="file:///app.py", language_id="python", version=3, text=PYTHON
            )
        ),
    )
    await settle(ls, "file:///app.py")
    (params,) = ls.published
    (diagnostic,) = params.diagnostics
    assert (diagnostic.code, diagnostic.source, params.version) == ("unit_mismatch", "nouls", 3)
    assert diagnostic.severity == types.DiagnosticSeverity.Error
    assert diagnostic.range.start == types.Position(line=4, character=8)
    unit_hash = unit_hash_of(diagnostic)
    assert unit_hash is not None
    assert len(unit_hash) == DIGEST_LENGTH


async def test_change_relints_only_when_linting_on_change(ls: FakeServer) -> None:
    """Change relints only when linting on change."""
    change = types.DidChangeTextDocumentParams(
        text_document=types.VersionedTextDocumentIdentifier(uri="file:///app.py", version=4),
        content_changes=[],
    )
    ls.analyser.config.lint_on = "save"
    did_change(as_server(ls), change)
    assert ls.pending == {}
    ls.analyser.config.lint_on = "change"
    did_change(as_server(ls), change)
    await settle(ls, "file:///app.py")
    assert len(ls.published) == 1


async def test_save_cancels_the_previous_lint(ls: FakeServer) -> None:
    """Save cancels the previous lint."""
    ls.analyser.config.debounce_ms = 10_000
    did_save(
        as_server(ls), types.DidSaveTextDocumentParams(text_document=identifier("file:///app.py"))
    )
    first = ls.pending["file:///app.py"]
    did_save(
        as_server(ls), types.DidSaveTextDocumentParams(text_document=identifier("file:///app.py"))
    )
    await asyncio.sleep(0)
    assert first.cancelled()
    did_close(
        as_server(ls), types.DidCloseTextDocumentParams(text_document=identifier("file:///app.py"))
    )
    assert ls.pending == {}
    assert ls.published[-1].diagnostics == []


async def test_unknown_languages_publish_nothing(ls: FakeServer, tmp_path: Path) -> None:
    """Unknown languages publish nothing."""
    ls.document.path = str(tmp_path / "notes.txt")
    await lint(as_server(ls), "file:///notes.txt")
    assert ls.published == []
    assert ls.client.calls == []


async def test_analysis_failures_are_logged_not_raised(
    ls: FakeServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Analysis failures are logged not raised."""

    async def explode(_text: str, _language: str, _path: Path) -> None:
        await asyncio.sleep(0)
        raise ParserCrashedError

    monkeypatch.setattr(ls.analyser, "analyse", explode)
    await lint(as_server(ls), "file:///app.py")
    assert ls.published == []
    assert "nouls could not analyse" in caplog.text
    assert "edit or save the file to retry" in caplog.text


async def test_code_actions_offer_both_labels_and_relabel(ls: FakeServer) -> None:
    """Code actions offer both labels and relabel."""
    await lint(as_server(ls), "file:///app.py")
    ours = ls.published[0].diagnostics[0]
    theirs = types.Diagnostic(range=ours.range, message="other", source="ruff")
    params = types.CodeActionParams(
        text_document=identifier("file:///app.py"),
        range=ours.range,
        context=types.CodeActionContext(diagnostics=[ours, theirs]),
    )
    actions = code_actions(as_server(ls), params)
    assert [action.title for action in actions] == [
        "nouls: not a problem (unit_mismatch)",
        "nouls: confirm finding (unit_mismatch)",
    ]
    command = actions[0].command
    assert command is not None
    uri, rule, unit_hash, verdict = cast("list[str]", command.arguments)
    label(as_server(ls), uri, rule, unit_hash, verdict)
    await settle(ls, "file:///app.py")
    assert ls.published[-1].diagnostics == []


def test_server_builds_its_analyser_from_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: FakeClient
) -> None:
    """Server builds its analyser from the workspace."""
    server = NoulsServer()

    def workspace(_server: NoulsServer) -> SimpleNamespace:
        return SimpleNamespace(root_path=str(tmp_path))

    monkeypatch.setattr(NoulsServer, "workspace", property(workspace))
    analyser = server.analyser
    assert analyser is server.analyser
    assert analyser.client is client
