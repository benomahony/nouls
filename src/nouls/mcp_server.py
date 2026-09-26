"""MCP server for nouls documentation.

Provides documentation access via Model Context Protocol.
Usage:
    claude mcp add nouls --transport stdio \\
        python src/nouls/mcp_server.py
"""

import logging
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("nouls")


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List available documentation resources."""
    assert app is not None, "Server must be initialized"

    docs_dir = Path(__file__).parent.parent.parent / "docs"
    assert docs_dir.exists(), "Docs directory must exist"

    resources = []
    for doc_file in docs_dir.rglob("*.md"):
        relative_path = doc_file.relative_to(docs_dir)
        uri_str = f"doc://nouls/{relative_path}"
        resources.append(
            Resource(
                uri=uri_str,  # type: ignore[arg-type]
                name=str(relative_path),
                mimeType="text/markdown",
                description=f"Documentation: {relative_path}",
            )
        )

    return resources


@app.read_resource()
async def read_resource(uri) -> str:  # type: ignore[no-untyped-def]
    """Read documentation content."""
    uri_str = str(uri)
    assert uri_str is not None, "read_resource needs a URI; use one from list_resources"
    assert uri_str.startswith("doc://nouls/"), (
        f"{uri_str} is not a nouls documentation URI; use one from list_resources, "
        "which all start with doc://nouls/"
    )

    path = uri_str.replace("doc://nouls/", "")
    docs_dir = Path(__file__).parent.parent.parent / "docs"
    doc_file = docs_dir / path

    assert doc_file.exists(), (
        f"There is no nouls documentation page called {path}; use a URI from list_resources"
    )
    assert doc_file.is_relative_to(docs_dir), (
        f"{path} points outside the nouls docs directory; use a URI from list_resources"
    )

    return doc_file.read_text()


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
    import asyncio

    asyncio.run(main())
