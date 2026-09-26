# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Split configuration files into settings, one per line, with the keys each line sits under."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tree_sitter import Node
from tree_sitter_language_pack import detect_language_from_path

from nouls.units import Span, get_parser

GRAMMARS = frozenset({"toml", "yaml", "json"})
KEYED = frozenset({"pair", "block_mapping_pair", "flow_pair"})
HEADED = frozenset({"table", "table_array_element"})
PUNCTUATION = frozenset("[]{}(),")
OPENERS = ("[", "{", "(", ":", "|", ">")


@dataclass(frozen=True)
class Setting:
    """One line of a configuration file that sets something."""

    file: str
    keys: str
    text: str
    span: Span

    def state(self) -> dict[str, str]:
        """Describe the setting for a question.

        Returns:
            The file, the keys the line sits under, and the line itself.

        """
        state = {"file": self.file, "setting": self.keys, "line": self.text}
        assert state["line"], "A setting must have text; settings_in skips blank lines"
        assert state["file"], "A setting must name its file"
        return state

    def source(self) -> str:
        """Serialise the setting, so identical settings share cached answers.

        Returns:
            The state as JSON with sorted keys.

        """
        source = json.dumps(self.state(), sort_keys=True)
        assert cast("dict[str, str]", json.loads(source))["line"] == self.text, (
            "The source must carry the line"
        )
        assert source.startswith("{"), "The source must be a JSON object"
        return source


def is_table_header(code: str) -> bool:
    """Tell whether a stripped line is a TOML table header, such as ``[tool.ruff.lint]``.

    Args:
        code: A line with surrounding whitespace removed.

    Returns:
        True for ``[table]`` and ``[[array.of.tables]]`` headers, with or without a comment.

    """
    head = code.split("#", 1)[0].strip()
    found = (
        head.startswith("[") and head.endswith("]") and not any(mark in head for mark in "=,\"'")
    )
    assert not found or head, "A header cannot be blank"
    assert len(head) <= len(code), "Removing a comment must not add text"
    return found


def sets_something(code: str) -> bool:
    """Tell whether a stripped line sets something, rather than only opening or closing a block.

    Args:
        code: A line with surrounding whitespace removed.

    Returns:
        False for blank lines, comments, table headers, lone brackets and lines that only open
        a nested block, such as ``ignore = [`` or ``hooks:``.

    """
    structural = (
        not code
        or code.startswith(("#", "//"))
        or is_table_header(code)
        or set(code) <= PUNCTUATION
        or code.endswith(OPENERS)
    )
    assert structural or code.strip() == code, "sets_something expects a stripped line"
    assert structural or code, "A line that sets something cannot be blank"
    return not structural


def key_of(node: Node) -> str:
    """Find the key a pair or table node names.

    Args:
        node: A TOML pair or table, a YAML mapping pair or a JSON pair.

    Returns:
        The key's text without quotes, or an empty string when the node has none.

    """
    key = node.child_by_field_name("key") or node.named_child(0)
    text = (key.text or b"").decode() if key is not None else ""
    found = text.strip("\"'")
    assert "\n" not in found or node.type not in HEADED, "A table header is on one line"
    assert len(found) <= len(text), "Stripping quotes must not add text"
    return found


def keys_at(root: Node, row: int, column: int) -> str:
    """Find the keys a position sits under, outermost first.

    Pass the position of a line's last character, so the lookup lands inside the line's value
    rather than on a list item marker such as YAML's ``-``.

    Args:
        root: The file's syntax tree.
        row: A zero based line.
        column: A zero based column on that line.

    Returns:
        The keys joined with dots, such as ``tool.ruff.lint.ignore``.

    """
    assert row >= 0, "Rows are zero based"
    assert column >= 0, "Columns are zero based"
    node = root.named_descendant_for_point_range((row, column), (row, column))
    keys: list[str] = []
    while node is not None:
        if node.type in KEYED | HEADED and (key := key_of(node)):
            keys.append(key)
        node = node.parent
    return ".".join(reversed(keys))


def settings_in(root: Path, path: Path) -> list[Setting]:
    """Split a configuration file into its settings.

    TOML, YAML and JSON lines carry the keys they sit under. Other files, such as Makefiles,
    give one setting per non comment line.

    Args:
        root: The project root, which file names are relative to.
        path: A configuration file under the root.

    Returns:
        One setting per line that sets something, in file order.

    """
    assert path.is_relative_to(root), f"{path} is outside the project root {root}"
    text = path.read_text(encoding="utf-8", errors="replace")
    grammar = detect_language_from_path(str(path))
    tree = get_parser(grammar).parse(text.encode()) if grammar in GRAMMARS else None
    settings: list[Setting] = []
    for row, line in enumerate(text.split("\n")):
        code = line.strip()
        if not sets_something(code):
            continue
        column = len(line) - len(line.lstrip())
        end = len(line.rstrip()) - 1
        keys = keys_at(tree.root_node, row, end) if tree is not None else ""
        span = Span(row, column, row, column + len(code))
        settings.append(Setting(path.relative_to(root).as_posix(), keys, code, span))
    assert all(s.span.line < len(text.split("\n")) for s in settings), "Settings lie in the file"
    return settings
