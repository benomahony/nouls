import asyncio
import logging
from importlib.metadata import version
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, Finding
from nouls.config import load_config

logger = logging.getLogger(__name__)

SEVERITIES = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
    "hint": types.DiagnosticSeverity.Hint,
}


class NoulsServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__("nouls", version("nouls"))
        self._analyser: Analyser | None = None
        self.pending: dict[str, asyncio.Task[None]] = {}

    @property
    def analyser(self) -> Analyser:
        if self._analyser is None:
            root = Path(self.workspace.root_path or Path.cwd())
            self._analyser = Analyser(load_config(root), AsyncTypeSafeClient())
        return self._analyser


server = NoulsServer()


def to_diagnostic(finding: Finding, show_probability: bool) -> types.Diagnostic:
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(line=finding.span.line, character=finding.span.column),
            end=types.Position(line=finding.span.end_line, character=finding.span.end_column),
        ),
        message=finding.describe(show_probability),
        severity=SEVERITIES[finding.severity],
        code=finding.rule,
        source="nouls",
    )


async def lint(ls: NoulsServer, uri: str) -> None:
    analyser = ls.analyser
    await asyncio.sleep(analyser.config.debounce_ms / 1000)
    document = ls.workspace.get_text_document(uri)
    language = analyser.config.language_for(Path(document.path))
    if language is None:
        return
    try:
        findings = await analyser.analyse(document.source, language)
    except Exception:
        logger.exception("nouls analysis failed for %s", uri)
        return
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(
            uri=uri,
            version=document.version,
            diagnostics=[to_diagnostic(finding, analyser.config.show_probability) for finding in findings],
        )
    )


def schedule(ls: NoulsServer, uri: str) -> None:
    if task := ls.pending.pop(uri, None):
        task.cancel()
    ls.pending[uri] = asyncio.ensure_future(lint(ls, uri))


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: NoulsServer, params: types.DidOpenTextDocumentParams) -> None:
    schedule(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: NoulsServer, params: types.DidChangeTextDocumentParams) -> None:
    schedule(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: NoulsServer, params: types.DidSaveTextDocumentParams) -> None:
    schedule(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: NoulsServer, params: types.DidCloseTextDocumentParams) -> None:
    if task := ls.pending.pop(params.text_document.uri, None):
        task.cancel()
    ls.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[]))
