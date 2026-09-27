# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for the MCP server that serves nouls's documentation."""

from pathlib import Path

import pytest

from nouls.mcp_server import DOCS, SCHEME, UnknownDocError, doc_resources, read_doc

pytestmark = pytest.mark.unit


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """Build a small docs directory beside a secret file.

    Returns:
        The docs directory.

    """
    root = tmp_path / "docs"
    (root / "guide").mkdir(parents=True)
    _ = (root / "index.md").write_text("# nouls\n")
    _ = (root / "guide" / "rules.md").write_text("# Rules\n")
    _ = (root / "notes.txt").write_text("not a page\n")
    _ = (tmp_path / "secret.md").write_text("do not serve\n")
    return root


def test_every_markdown_page_is_a_resource(docs: Path) -> None:
    """Every Markdown page is a resource."""
    assert [str(r.uri) for r in doc_resources(docs)] == [
        f"{SCHEME}guide/rules.md",
        f"{SCHEME}index.md",
    ]


def test_a_page_is_read_by_its_uri(docs: Path) -> None:
    """A page is read by its URI."""
    assert read_doc(docs, f"{SCHEME}guide/rules.md") == "# Rules\n"


@pytest.mark.parametrize(
    "uri",
    [
        f"{SCHEME}../secret.md",
        f"{SCHEME}{'/'.join(['..'] * 20)}/etc/hosts",
        f"{SCHEME}notes.txt",
        f"{SCHEME}missing.md",
        "file:///etc/hosts",
    ],
)
def test_uris_outside_the_docs_are_refused(docs: Path, uri: str) -> None:
    """URIs outside the docs, and anything but a Markdown page, are refused."""
    with pytest.raises(UnknownDocError, match="list_resources"):
        _ = read_doc(docs, uri)


def test_the_server_lists_and_reads_the_real_docs() -> None:
    """The server lists and reads the real docs."""
    assert f"{SCHEME}index.md" in {str(r.uri) for r in doc_resources(DOCS)}
    text = read_doc(DOCS, f"{SCHEME}index.md")
    assert text == (DOCS / "index.md").read_text(encoding="utf-8")
