import asyncio
import logging
from importlib.metadata import version
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from typesafe_sdk import AsyncTypeSafeClient

from nouls.analyser import Analyser, Finding
from nouls.config import load_config
from nouls.store import Store

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
        assert self.name == "nouls", "Server must identify as nouls"
        assert self.version, "Server must report a version"

    @property
    def analyser(self) -> Analyser:
        if self._analyser is None:
            root = Path(self.workspace.root_path or Path.cwd())
            config = load_config(root)
            self._analyser = Analyser(config, AsyncTypeSafeClient(), Store(config.store_path()))
        assert self._analyser.config.languages, "Analyser must know at least one language"
        assert self._analyser.config.debounce_ms >= 0, "Debounce must not be negative"
        return self._analyser


server = NoulsServer()


def to_diagnostic(finding: Finding, show_probability: bool) -> types.Diagnostic:
    assert finding.severity in SEVERITIES, "Severity must map to an LSP severity"
    assert finding.span.end_line >= finding.span.line, "Span must not end before it starts"
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(line=finding.span.line, character=finding.span.column),
            end=types.Position(line=finding.span.end_line, character=finding.span.end_column),
        ),
        message=finding.describe(show_probability),
        severity=SEVERITIES[finding.severity],
        code=finding.rule,
        source="nouls",
        data={"unit_hash": finding.unit_hash},
    )


async def lint(ls: NoulsServer, uri: str) -> None:
    assert uri, "Document URI must not be empty"
    analyser = ls.analyser
    await asyncio.sleep(analyser.config.debounce_ms / 1000)
    document = ls.workspace.get_text_document(uri)
    path = Path(document.path)
    language = analyser.config.language_for(path)
    if language is None:
        return
    try:
        findings = await analyser.analyse(document.source, language, path)
    except Exception:
        logger.exception("nouls analysis failed for %s", uri)
        return
    assert all(finding.unit_hash for finding in findings), "Findings must name their function"
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(
            uri=uri,
            version=document.version,
            diagnostics=[
                to_diagnostic(finding, analyser.config.show_probability) for finding in findings
            ],
        )
    )


def schedule(ls: NoulsServer, uri: str) -> None:
    assert uri, "Document URI must not be empty"
    if task := ls.pending.pop(uri, None):
        task.cancel()
    ls.pending[uri] = asyncio.ensure_future(lint(ls, uri))
    assert not ls.pending[uri].done(), "A freshly scheduled lint must be pending"


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: NoulsServer, params: types.DidOpenTextDocumentParams) -> None:
    uri = params.text_document.uri
    assert uri, "Opened document must have a URI"
    schedule(ls, uri)
    assert uri in ls.pending, "Opening a document must schedule a lint"


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: NoulsServer, params: types.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
    assert uri, "Changed document must have a URI"
    if ls.analyser.config.lint_on == "change":
        schedule(ls, uri)
        assert uri in ls.pending, "A change must schedule a lint when linting on change"


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: NoulsServer, params: types.DidSaveTextDocumentParams) -> None:
    uri = params.text_document.uri
    assert uri, "Saved document must have a URI"
    schedule(ls, uri)
    assert uri in ls.pending, "Saving a document must schedule a lint"


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: NoulsServer, params: types.DidCloseTextDocumentParams) -> None:
    uri = params.text_document.uri
    assert uri, "Closed document must have a URI"
    if task := ls.pending.pop(uri, None):
        task.cancel()
    assert uri not in ls.pending, "Closing a document must cancel its lint"
    ls.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=uri, diagnostics=[]))


@server.feature(
    types.TEXT_DOCUMENT_CODE_ACTION,
    types.CodeActionOptions(code_action_kinds=[types.CodeActionKind.QuickFix]),
)
def code_actions(ls: NoulsServer, params: types.CodeActionParams) -> list[types.CodeAction]:
    uri = params.text_document.uri
    assert uri, "Code action request must have a URI"
    actions = [
        types.CodeAction(
            title=f"nouls: {title} ({diagnostic.code})",
            kind=types.CodeActionKind.QuickFix,
            diagnostics=[diagnostic],
            command=types.Command(
                title=title,
                command="nouls.label",
                arguments=[uri, diagnostic.code, diagnostic.data["unit_hash"], real],
            ),
        )
        for diagnostic in params.context.diagnostics
        if diagnostic.source == "nouls" and isinstance(diagnostic.data, dict)
        for title, real in (("not a problem", False), ("confirm finding", True))
    ]
    assert len(actions) % 2 == 0, "Every finding gets both label actions"
    return actions


@server.command("nouls.label")
def label(ls: NoulsServer, uri: str, rule: str, unit_hash: str, real: bool) -> None:
    assert rule in ls.analyser.config.rules, "Labelled rule must be configured"
    ls.analyser.store.label(rule, unit_hash, real)
    schedule(ls, uri)
    assert uri in ls.pending, "Labelling must re-lint the document"
