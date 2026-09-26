import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

NOULS = str(Path(sys.executable).parent / "nouls")


def frame(message: dict[str, Any]) -> bytes:
    body = json.dumps(message).encode()
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def responses(stdout: bytes) -> list[dict[str, Any]]:
    messages = []
    while stdout:
        header, _, rest = stdout.partition(b"\r\n\r\n")
        fields = dict(line.split(b": ", 1) for line in header.split(b"\r\n"))
        length = int(fields[b"Content-Length"])
        messages.append(json.loads(rest[:length]))
        stdout = rest[length:]
    return messages


def test_help_and_rules_run_as_a_real_process(tmp_path: Path) -> None:
    help_text = subprocess.run(
        [NOULS, "--help"], capture_output=True, text=True, check=True, cwd=tmp_path
    ).stdout
    rules = subprocess.run(
        [NOULS, "rules"], capture_output=True, text=True, check=True, cwd=tmp_path
    ).stdout
    assert "Usage examples:" in help_text
    assert len(rules.splitlines()) == 28


def test_language_server_handshake_over_stdio(tmp_path: Path) -> None:
    session = b"".join(
        [
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"rootUri": tmp_path.as_uri(), "capabilities": {}},
                }
            ),
            frame({"jsonrpc": "2.0", "method": "initialized", "params": {}}),
            frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}),
            frame({"jsonrpc": "2.0", "method": "exit"}),
        ]
    )
    result = subprocess.run(
        [NOULS, "serve"], input=session, capture_output=True, timeout=30, check=False, cwd=tmp_path
    )
    replies = {message["id"]: message for message in responses(result.stdout) if "id" in message}
    capabilities = replies[1]["result"]["capabilities"]
    assert capabilities["codeActionProvider"] == {"codeActionKinds": ["quickfix"]}
    assert capabilities["executeCommandProvider"] == {"commands": ["nouls.label"]}
    assert replies[2]["result"] is None
    assert result.returncode == 0


def test_extracting_units_repeatedly_does_not_corrupt_the_heap() -> None:
    # tree-sitter 0.26.0 over-decrefs Point.row/column (py-tree-sitter#472), corrupting the heap.
    script = (
        "import gc, glob, rich\n"
        "from pathlib import Path\n"
        "from nouls.config import load_config\n"
        "from nouls.units import extract_units\n"
        "gc.set_threshold(10)\n"
        "language = load_config(Path.cwd()).languages['python']\n"
        "sources = [Path(f).read_text() for f in glob.glob(rich.__path__[0] + '/*.py')]\n"
        "for _ in range(20):\n"
        "    for source in sources:\n"
        "        extract_units(source, language)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=300)
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr.decode()}"
