# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Language server that publishes nouls findings as editor diagnostics."""

import asyncio
import logging
from importlib.metadata import version
from pathlib import Path
from typing import cast

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

from nouls.analyser import Analyser, Finding
from nouls.config import find_root, load_config, project_files
from nouls.store import DIGEST_LENGTH, Store
from nouls.typesafe import explain

logger = logging.getLogger(__name__)

SEVERITIES = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
    "hint": types.DiagnosticSeverity.Hint,
}


class NoulsServer(LanguageServer):
    """Language server that lints documents as they open, change and save."""

    def __init__(self) -> None:
        """Start with no analyser and no pending lints."""
        # pygls declares LanguageServer.__init__(..., *args, **kwargs) without types.
        super().__init__("nouls", version("nouls"))  # pyright: ignore[reportUnknownMemberType]
        self._analyser: Analyser | None = None
        self.pending: dict[str, asyncio.Task[None]] = {}
        self.warned: set[str] = set()
        assert self.name == "nouls", "Server must identify as nouls"
        assert self.version, "Server must report a version"

    @property
    def analyser(self) -> Analyser:
        """Build the analyser from the workspace's configuration on first use.

        Returns:
            The analyser shared by every lint.

        """
        if self._analyser is None:
            root = Path(self.workspace.root_path or Path.cwd())
            config = load_config(root)
            self._analyser = Analyser(config, AsyncTypeSafeClient(), Store(config.store_path()))
        assert self._analyser.config.languages, (
            "The nouls config for this workspace defines no languages, so nothing can be linted; "
            "add at least one entry under languages in nouls.yaml"
        )
        assert self._analyser.config.debounce_ms >= 0, (
            f"debounce_ms is {self._analyser.config.debounce_ms}, but a delay cannot be negative; "
            "set debounce_ms to 0 or more in nouls.yaml"
        )
        return self._analyser


server = NoulsServer()


def to_diagnostic(finding: Finding, *, show_probability: bool) -> types.Diagnostic:
    """Convert a finding into an editor diagnostic.

    Args:
        finding: The finding to show.
        show_probability: Whether to include the probability in the message.

    Returns:
        A diagnostic carrying the unit hash so it can be labelled.

    """
    assert finding.severity in SEVERITIES, "Severity must map to an LSP severity"
    assert finding.span.end_line >= finding.span.line, "Span must not end before it starts"
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(line=finding.span.line, character=finding.span.column),
            end=types.Position(line=finding.span.end_line, character=finding.span.end_column),
        ),
        message=finding.describe(show_probability=show_probability),
        severity=SEVERITIES[finding.severity],
        code=finding.rule,
        source="nouls",
        data={"unit_hash": finding.unit_hash},
    )


def warn(ls: NoulsServer, message: str) -> None:
    """Show a warning in the editor once, however many lints hit the same problem.

    Args:
        ls: The language server.
        message: A plain language message that says how to fix the problem.

    """
    assert message.startswith("nouls: "), "Warnings start with nouls: so users know their source"
    if message in ls.warned:
        return
    ls.warned.add(message)
    logger.warning(message)
    ls.window_show_message(types.ShowMessageParams(type=types.MessageType.Warning, message=message))
    assert message in ls.warned, "A shown warning must be remembered so it is not repeated"


async def lint(ls: NoulsServer, uri: str) -> None:
    """Check a document after the debounce delay and publish its findings.

    Args:
        ls: The language server.
        uri: The document to check.

    """
    assert uri, "Document URI must not be empty"
    try:
        analyser = ls.analyser
    except TypeSafeError as error:
        warn(ls, explain(error))
        return
    await asyncio.sleep(analyser.config.debounce_ms / 1000)
    document = ls.workspace.get_text_document(uri)
    path = Path(document.path)
    language = analyser.config.language_for(path)
    root = find_root(path.parent)
    in_project = any(
        path in project_files(root, patterns) for patterns in analyser.config.file_patterns()
    )
    if language is None and not in_project:
        return
    try:
        findings = await analyser.analyse(document.source, language, path) if language else []
        if in_project:
            project = await analyser.analyse_project(root)
            project += await analyser.analyse_settings(root)
            findings += [finding for anchor, finding in project if anchor == path]
    except TypeSafeError as error:
        warn(ls, explain(error))
        return
    except Exception:
        logger.exception(
            "nouls could not analyse %s, so its diagnostics were not updated; "
            "the traceback below gives the cause. Fix it, then edit or save the file to retry.",
            uri,
        )
        return
    assert all(finding.unit_hash for finding in findings), "Findings must name their function"
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(
            uri=uri,
            version=document.version,
            diagnostics=[
                to_diagnostic(finding, show_probability=analyser.config.show_probability)
                for finding in findings
            ],
        )
    )


def schedule(ls: NoulsServer, uri: str) -> None:
    """Replace any pending lint of a document with a fresh one.

    Args:
        ls: The language server.
        uri: The document to check.

    """
    assert uri, "Document URI must not be empty"
    if task := ls.pending.pop(uri, None):
        _ = task.cancel()
    ls.pending[uri] = asyncio.ensure_future(lint(ls, uri))
    assert not ls.pending[uri].done(), "A freshly scheduled lint must be pending"


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: NoulsServer, params: types.DidOpenTextDocumentParams) -> None:
    """Lint a document when it opens.

    Args:
        ls: The language server.
        params: The opened document.

    """
    uri = params.text_document.uri
    assert uri, "Opened document must have a URI"
    schedule(ls, uri)
    assert uri in ls.pending, "Opening a document must schedule a lint"


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: NoulsServer, params: types.DidChangeTextDocumentParams) -> None:
    """Lint a document as it changes, when linting on change.

    Args:
        ls: The language server.
        params: The changed document.

    """
    uri = params.text_document.uri
    assert uri, "Changed document must have a URI"
    if ls.analyser.config.lint_on == "change":
        schedule(ls, uri)
        assert uri in ls.pending, "A change must schedule a lint when linting on change"


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: NoulsServer, params: types.DidSaveTextDocumentParams) -> None:
    """Lint a document when it is saved.

    Args:
        ls: The language server.
        params: The saved document.

    """
    uri = params.text_document.uri
    assert uri, "Saved document must have a URI"
    schedule(ls, uri)
    assert uri in ls.pending, "Saving a document must schedule a lint"


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: NoulsServer, params: types.DidCloseTextDocumentParams) -> None:
    """Cancel a closed document's lint and clear its diagnostics.

    Args:
        ls: The language server.
        params: The closed document.

    """
    uri = params.text_document.uri
    assert uri, "Closed document must have a URI"
    if task := ls.pending.pop(uri, None):
        _ = task.cancel()
    assert uri not in ls.pending, "Closing a document must cancel its lint"
    ls.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=uri, diagnostics=[]))


def unit_hash_of(diagnostic: types.Diagnostic) -> str | None:
    """Find the function or project a nouls diagnostic is about.

    Args:
        diagnostic: Any diagnostic under the cursor, from nouls or another tool.

    Returns:
        The unit hash nouls stored in the diagnostic, or None for other tools' diagnostics.

    """
    data = diagnostic.data
    found = None
    if diagnostic.source == "nouls" and isinstance(data, dict):
        value = cast("dict[str, object]", data).get("unit_hash")
        found = value if isinstance(value, str) else None
    assert found is None or diagnostic.source == "nouls", "Only nouls diagnostics carry hashes"
    assert found is None or len(found) == DIGEST_LENGTH, (
        f"A nouls diagnostic carries unit hash {found!r}, which is not a digest; "
        "build diagnostics with to_diagnostic"
    )
    return found


@server.feature(
    types.TEXT_DOCUMENT_CODE_ACTION,
    types.CodeActionOptions(code_action_kinds=[types.CodeActionKind.QuickFix]),
)
def code_actions(_ls: NoulsServer, params: types.CodeActionParams) -> list[types.CodeAction]:
    """Offer to label each nouls diagnostic as real or not a problem.

    Args:
        _ls: The language server, unused.
        params: The diagnostics under the cursor.

    Returns:
        Two label actions per nouls diagnostic.

    """
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
                arguments=[uri, diagnostic.code, uhash, verdict],
            ),
        )
        for diagnostic in params.context.diagnostics
        if (uhash := unit_hash_of(diagnostic))
        for title, verdict in (("not a problem", "false"), ("confirm finding", "real"))
    ]
    assert len(actions) % 2 == 0, "Every finding gets both label actions"
    return actions


@server.command("nouls.label")
def label(ls: NoulsServer, uri: str, rule: str, unit_hash: str, verdict: str) -> None:
    """Record a verdict from a code action and lint the document again.

    Args:
        ls: The language server.
        uri: The document the finding is in.
        rule: The finding's rule.
        unit_hash: The function or project the finding is on.
        verdict: ``real`` or ``false``.

    """
    assert rule in ls.analyser.config.rules, "Labelled rule must be configured"
    assert verdict in {"real", "false"}, (
        f"nouls.label needs a verdict of real or false but got {verdict!r}; "
        "send the arguments from nouls's own code actions"
    )
    ls.analyser.store.label(rule, unit_hash, real=verdict == "real")
    schedule(ls, uri)
    assert uri in ls.pending, "Labelling must re-lint the document"
