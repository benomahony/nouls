# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
r"""MCP server for nouls documentation.

Provides documentation access via Model Context Protocol.
Usage:
    claude mcp add nouls --transport stdio \
        python src/nouls/mcp_server.py
"""

import asyncio
import logging
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource
from pydantic import AnyUrl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("nouls")

DOCS = Path(__file__).parent.parent.parent / "docs"
SCHEME = "doc://nouls/"


class UnknownDocError(ValueError):
    """A client asked for a documentation page that is not one nouls serves."""

    def __init__(self, uri: str) -> None:
        """Say which URI was refused and where valid ones come from.

        Args:
            uri: The URI the client asked for.

        """
        super().__init__(
            f"{uri} is not a nouls documentation page. Use a URI from list_resources, "
            f"which all start with {SCHEME}."
        )
        assert uri in str(self), "The error must name the refused URI"
        assert "list_resources" in str(self), "The error must say where valid URIs come from"


def doc_resources(docs: Path) -> list[Resource]:
    """Describe every Markdown page under the docs directory.

    Args:
        docs: The nouls docs directory.

    Returns:
        One resource per page, addressed as doc://nouls/<relative path>.

    """
    assert docs.is_dir(), (
        f"The nouls docs directory {docs} is missing; run from a source checkout with docs/"
    )
    resources = [
        Resource(
            uri=AnyUrl(f"{SCHEME}{page.relative_to(docs).as_posix()}"),
            name=page.relative_to(docs).as_posix(),
            mimeType="text/markdown",
            description=f"Documentation: {page.relative_to(docs).as_posix()}",
        )
        for page in sorted(docs.rglob("*.md"))
    ]
    assert all(str(r.uri).startswith(SCHEME) for r in resources), (
        f"Every resource URI must start with {SCHEME}; build it from SCHEME"
    )
    return resources


def read_doc(docs: Path, uri: str) -> str:
    """Read one documentation page.

    Args:
        docs: The nouls docs directory.
        uri: A doc://nouls/ URI from list_resources.

    Returns:
        The page's Markdown text.

    Raises:
        UnknownDocError: When the URI is not a doc://nouls/ URI, points outside the docs
            directory, or names no Markdown page. The URI comes from the MCP client, so this is
            a real check, not an assertion that disappears under ``python -O``.

    """
    assert docs.is_absolute() or docs.exists(), "read_doc needs the docs directory"
    page = (docs / uri.removeprefix(SCHEME)).resolve()
    if (
        not uri.startswith(SCHEME)
        or not page.is_relative_to(docs.resolve())
        or page.suffix != ".md"
        or not page.is_file()
    ):
        raise UnknownDocError(uri)
    assert page.is_relative_to(docs.resolve()), "Only pages inside the docs directory are read"
    return page.read_text(encoding="utf-8")


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List available documentation resources.

    Returns:
        One resource per documentation page.

    """
    assert DOCS.is_absolute(), (
        f"DOCS must be absolute so the server works from any directory; got {DOCS}"
    )
    resources = await asyncio.to_thread(doc_resources, DOCS)
    assert all(r.mimeType == "text/markdown" for r in resources), (
        "Every documentation resource is Markdown; doc_resources must set mimeType"
    )
    return resources


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """Read documentation content.

    Args:
        uri: A doc://nouls/ URI from list_resources.

    Returns:
        The page's Markdown text.

    """
    assert DOCS.is_absolute(), f"DOCS must be absolute; got {DOCS}"
    text = await asyncio.to_thread(read_doc, DOCS, str(uri))
    assert isinstance(text, str), "read_doc must return the page's text"
    return text


async def main() -> None:
    """Run MCP server via stdio."""
    assert app is not None, (
        "The MCP app was not created at import; keep app = Server(...) at module level"
    )

    async with stdio_server() as (read_stream, write_stream):
        assert read_stream is not None, (
            "stdio_server gave no read stream; run this server with --transport stdio"
        )
        assert write_stream is not None, (
            "stdio_server gave no write stream; run this server with --transport stdio"
        )
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
