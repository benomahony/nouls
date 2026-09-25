import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lsprotocol import types

from nouls.analyser import Analyser
from nouls.config import Config
from nouls.server import (
    NoulsServer,
    code_actions,
    did_change,
    did_close,
    did_open,
    did_save,
    label,
    lint,
)
from nouls.store import Store
from tests.conftest import PYTHON, FakeClient, as_client

pytestmark = pytest.mark.unit


@dataclass
class FakeServer:
    analyser: Analyser
    document: SimpleNamespace
    pending: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    published: list[types.PublishDiagnosticsParams] = field(default_factory=list)

    @property
    def workspace(self) -> SimpleNamespace:
        return SimpleNamespace(get_text_document=lambda _: self.document)

    def text_document_publish_diagnostics(self, params: types.PublishDiagnosticsParams) -> None:
        self.published.append(params)


def identifier(uri: str) -> types.TextDocumentIdentifier:
    return types.TextDocumentIdentifier(uri=uri)


@pytest.fixture
def ls(tmp_path: Path, config: Config, store: Store) -> Any:
    config.debounce_ms = 0
    path = tmp_path / "app.py"
    path.write_text(PYTHON)
    document = SimpleNamespace(path=str(path), source=PYTHON, version=3)
    return FakeServer(Analyser(config, as_client(FakeClient()), store), document)


async def settle(ls: Any, uri: str) -> None:
    await ls.pending[uri]


async def test_open_publishes_diagnostics_with_function_hash(ls: Any) -> None:
    did_open(
        ls,
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
    assert len(diagnostic.data["unit_hash"]) == 32


async def test_change_relints_only_when_linting_on_change(ls: Any) -> None:
    change = types.DidChangeTextDocumentParams(
        text_document=types.VersionedTextDocumentIdentifier(uri="file:///app.py", version=4),
        content_changes=[],
    )
    ls.analyser.config.lint_on = "save"
    did_change(ls, change)
    assert ls.pending == {}
    ls.analyser.config.lint_on = "change"
    did_change(ls, change)
    await settle(ls, "file:///app.py")
    assert len(ls.published) == 1


async def test_save_cancels_the_previous_lint(ls: Any) -> None:
    ls.analyser.config.debounce_ms = 10_000
    did_save(ls, types.DidSaveTextDocumentParams(text_document=identifier("file:///app.py")))
    first = ls.pending["file:///app.py"]
    did_save(ls, types.DidSaveTextDocumentParams(text_document=identifier("file:///app.py")))
    await asyncio.sleep(0)
    assert first.cancelled()
    did_close(ls, types.DidCloseTextDocumentParams(text_document=identifier("file:///app.py")))
    assert ls.pending == {}
    assert ls.published[-1].diagnostics == []


async def test_unknown_languages_publish_nothing(ls: Any) -> None:
    ls.document.path = "/tmp/notes.txt"
    await lint(ls, "file:///notes.txt")
    assert ls.published == []
    assert ls.analyser.client.calls == []


async def test_analysis_failures_are_logged_not_raised(
    ls: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode(*_: object) -> None:
        raise RuntimeError("TypeSafe unavailable")

    monkeypatch.setattr(ls.analyser, "analyse", explode)
    await lint(ls, "file:///app.py")
    assert ls.published == []
    assert "nouls analysis failed" in caplog.text


async def test_code_actions_offer_both_labels_and_relabel(ls: Any) -> None:
    await lint(ls, "file:///app.py")
    ours = ls.published[0].diagnostics[0]
    theirs = types.Diagnostic(range=ours.range, message="other", source="ruff")
    params = types.CodeActionParams(
        text_document=identifier("file:///app.py"),
        range=ours.range,
        context=types.CodeActionContext(diagnostics=[ours, theirs]),
    )
    actions = code_actions(ls, params)
    assert [action.title for action in actions] == [
        "nouls: not a problem (unit_mismatch)",
        "nouls: confirm finding (unit_mismatch)",
    ]
    command = actions[0].command
    assert command is not None
    label(ls, *cast(list[Any], command.arguments))
    await settle(ls, "file:///app.py")
    assert ls.published[-1].diagnostics == []


def test_server_builds_its_analyser_from_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: FakeClient
) -> None:
    server = NoulsServer()
    monkeypatch.setattr(
        NoulsServer, "workspace", property(lambda _: SimpleNamespace(root_path=str(tmp_path)))
    )
    analyser = server.analyser
    assert analyser is server.analyser
    assert analyser.client is client
