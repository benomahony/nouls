# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Split source files into function units with tree-sitter."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache

from tree_sitter import Node, Parser, Query, QueryCursor
from tree_sitter_language_pack import get_language, get_tags_query
from tree_sitter_language_pack import get_parser as _get_parser

from nouls.config import TAG_KINDS, Language, has_function_tags
from nouls.metrics import Metric, measure_file, measure_function

type NodeKey = tuple[int, int, str]


@cache
def get_parser(grammar: str) -> Parser:
    """Load the tree-sitter parser for a grammar.

    Args:
        grammar: A tree-sitter-language-pack name.

    Returns:
        A cached parser.

    """
    assert grammar, (
        "A language in your nouls config has an empty grammar; "
        "set grammar to a tree-sitter-language-pack name such as python"
    )
    parser = _get_parser(grammar)
    assert parser is not None, (
        f"tree-sitter-language-pack returned no parser for {grammar!r}; "
        "check the name against its supported languages or upgrade the package"
    )
    return parser


@cache
def function_tags(grammar: str) -> Query:
    """Compile the grammar's tags query, which marks functions and methods.

    Args:
        grammar: A tree-sitter-language-pack name with function tags.

    Returns:
        A cached query.

    """
    assert has_function_tags(grammar), (
        f"{grammar} has no function tags; configure units for it under languages instead"
    )
    query = Query(get_language(grammar), get_tags_query(grammar) or "")
    assert query.pattern_count, f"The tags query for {grammar} compiled to no patterns"
    return query


def node_key(node: Node) -> NodeKey:
    """Identify a node by its position and type.

    Args:
        node: A syntax tree node.

    Returns:
        The node's start byte, end byte and type.

    """
    key = (node.start_byte, node.end_byte, node.type)
    assert key[0] <= key[1], "A node must not end before it starts"
    assert key[2], "A node must have a type"
    return key


def tagged_functions(root: Node, grammar: str) -> set[NodeKey]:
    """Find the functions and methods a grammar's tags query marks.

    Some queries mark a whole class for each method in it. A marked node that contains a marked
    node of another type is dropped, so only the functions themselves remain.

    Args:
        root: The file's syntax tree.
        grammar: A tree-sitter-language-pack name with function tags.

    Returns:
        The marked function and method nodes.

    """
    captures = QueryCursor(function_tags(grammar)).captures(root)
    nodes = [node for kind in TAG_KINDS for node in captures.get(kind, [])]
    kept = {
        node_key(node)
        for node in nodes
        if not any(
            other.type != node.type
            and node.start_byte <= other.start_byte
            and other.end_byte <= node.end_byte
            for other in nodes
        )
    }
    assert len(kept) <= len(nodes), "Filtering must not add nodes"
    assert all(key[1] <= root.end_byte for key in kept), "Tagged nodes must lie in the file"
    return kept


@dataclass(frozen=True)
class Span:
    """Where a diagnostic sits, zero based."""

    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Unit:
    """One function, method, fixture or test call sent as a question."""

    kind: str
    name: str
    source: str
    span: Span
    first_line: int
    last_line: int
    metrics: Mapping[Metric, int] = field(default_factory=dict[Metric, int])

    def contains(self, line: int) -> bool:
        """Tell whether a line falls inside the unit.

        Args:
            line: A zero based line number.

        Returns:
            True when the line is between the unit's first and last lines.

        """
        assert line >= 0, "Lines are zero based"
        assert self.first_line <= self.last_line, "Unit must not end before it starts"
        return self.first_line <= line <= self.last_line


def headline(node: Node, source: str) -> Span:
    """Find where to put a unit's diagnostic.

    Args:
        node: The unit's node.
        source: The unit's source text.

    Returns:
        The node's name, or its first line when it has none.

    """
    assert source, "headline needs the unit's source text; pass the source sliced from the node"
    name = node.child_by_field_name("name")
    if name is not None:
        span = Span(
            name.start_point.row, name.start_point.column, name.end_point.row, name.end_point.column
        )
    else:
        first_line = source.split("\n", 1)[0]
        span = Span(
            node.start_point.row,
            node.start_point.column,
            node.start_point.row,
            node.start_point.column + len(first_line),
        )
    assert span.end_line >= span.line, (
        f"The diagnostic span for {node.type} ends on line {span.end_line} before it starts on "
        f"line {span.line}; tree-sitter returned an inconsistent tree, so upgrade the grammar"
    )
    return span


def attached_start(node: Node, attached: set[str]) -> Node:
    """Find the first decorator or attribute that belongs to a unit.

    Args:
        node: The unit's node.
        attached: Node types that belong to the unit they wrap or precede.

    Returns:
        The earliest attached node, or the unit's node when there is none.

    """
    assert node.type not in attached, (
        f"{node.type} is listed in both units and attached for this language; "
        "remove it from one of them in the nouls config"
    )
    first = node
    while first.parent is not None and first.parent.type in attached:
        first = first.parent
    while first.prev_named_sibling is not None and first.prev_named_sibling.type in attached:
        first = first.prev_named_sibling
    assert first.start_byte <= node.start_byte, (
        f"An attached node starts after the {node.type} it belongs to; "
        "only walk to parents and previous siblings when collecting attached nodes"
    )
    return first


def callee(node: Node, language: Language) -> str | None:
    """Find the name of the function a test call calls.

    Args:
        node: A call node.
        language: The language, which must declare ``calls``.

    Returns:
        The callee's name, such as ``it``, or None when it has none.

    """
    assert language.calls is not None, (
        f"callee was called for {language.grammar}, which has no calls section; "
        "only call it when language.calls is set"
    )
    assert node.type == language.calls.node, (
        f"callee expects a {language.calls.node} node but got {node.type}; "
        "check node.type against calls.node before calling it"
    )
    target = node.child_by_field_name(language.calls.callee)
    if target is None or not target.text:
        return None
    match = re.match(r"[\w!?]+", target.text.decode())
    return match.group() if match else None


def unit_at(node: Node, data: bytes, attached: set[str]) -> Unit:
    """Build the unit for a function node, with the decorators that belong to it.

    Args:
        node: A function, method or test call node.
        data: The file's bytes.
        attached: Node types that belong to the unit they wrap or precede.

    Returns:
        The unit, named by the node's name or its first line.

    """
    text = (node.text or b"").decode()
    assert text, f"unit_at needs a node with text, but the {node.type} node is empty"
    first = attached_start(node, attached)
    name = node.child_by_field_name("name")
    label = name.text.decode() if name is not None and name.text else text.split("\n", 1)[0].strip()
    unit = Unit(
        node.type,
        label,
        data[first.start_byte : node.end_byte].decode(),
        headline(node, text),
        first.start_point.row,
        node.end_point.row,
        measure_function(node),
    )
    assert unit.first_line <= unit.last_line, "A unit must not end before it starts"
    return unit


def is_test_call(node: Node, language: Language, *, tests: bool) -> bool:
    """Tell whether a node is a test call, such as ``it(...)`` or ``beforeEach(...)``.

    Args:
        node: Any node.
        language: The file's language.
        tests: Whether the file is a test file; test calls only count in test files.

    Returns:
        True when the file is a test file and the node calls one of the language's test calls.

    """
    calls = language.calls
    found = (
        tests
        and calls is not None
        and node.type == calls.node
        and callee(node, language) in calls.names
    )
    assert not found or tests, "Test calls only count in test files"
    assert not found or calls is not None, "Only a language with calls has test calls"
    return found


def extract_units(text: str, language: Language, *, tests: bool = False) -> list[Unit]:
    """Split a file into units.

    Args:
        text: The file's text.
        language: The file's language.
        tests: Whether the file is a test file, so test calls become units.

    Returns:
        Every unit, in order of where its diagnostic sits.

    """
    assert language.units or has_function_tags(language.grammar), (
        f"nouls cannot find functions in {language.grammar}; configure units for it"
    )
    kinds = set(language.units)
    attached = set(language.attached)
    data = text.encode()
    units: list[Unit] = []
    tree = get_parser(language.grammar).parse(data)
    root = tree.root_node
    tagged: set[NodeKey] = set() if kinds else tagged_functions(root, language.grammar)
    stack = [root]
    while stack:
        node = stack.pop()
        is_call = is_test_call(node, language, tests=tests)
        is_unit = node.type in kinds or node_key(node) in tagged
        if (is_unit or is_call) and node.text:
            units.append(unit_at(node, data, attached))
            if is_call:
                continue
        stack.extend(node.children)
    ordered = sorted(units, key=lambda unit: (unit.span.line, unit.span.column))
    assert all(unit.source for unit in ordered), "Every unit must have source"
    return ordered


def file_metrics(text: str, language: Language) -> Mapping[Metric, int]:
    """Measure a whole file.

    Args:
        text: The file's text.
        language: The file's language.

    Returns:
        Every file metric, by name.

    """
    assert language.grammar, "file_metrics needs a language with a grammar"
    measured = measure_file(get_parser(language.grammar).parse(text.encode()).root_node)
    assert all(value >= 0 for value in measured.values()), "Metrics are never negative"
    return measured
