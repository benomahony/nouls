import re
from dataclasses import dataclass
from functools import lru_cache

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser as _get_parser

from nouls.config import Language


@lru_cache(maxsize=None)
def get_parser(grammar: str) -> Parser:
    assert grammar, "Grammar name must not be empty"
    parser = _get_parser(grammar)
    assert parser is not None, "tree-sitter-language-pack must return a parser"
    return parser


@dataclass(frozen=True)
class Span:
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Unit:
    kind: str
    name: str
    source: str
    span: Span
    first_line: int
    last_line: int

    def contains(self, line: int) -> bool:
        assert line >= 0, "Lines are zero based"
        assert self.first_line <= self.last_line, "Unit must not end before it starts"
        return self.first_line <= line <= self.last_line


def headline(node: Node, source: str) -> Span:
    assert source, "Unit source must not be empty"
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
    assert span.end_line >= span.line, "Span must not end before it starts"
    return span


def attached_start(node: Node, attached: set[str]) -> Node:
    assert node.type not in attached, "A unit must not itself be an attached node"
    first = node
    while first.parent is not None and first.parent.type in attached:
        first = first.parent
    while first.prev_named_sibling is not None and first.prev_named_sibling.type in attached:
        first = first.prev_named_sibling
    assert first.start_byte <= node.start_byte, "Attached nodes must precede the unit"
    return first


def callee(node: Node, language: Language) -> str | None:
    assert language.calls is not None, "Language must declare test calls"
    assert node.type == language.calls.node, "Only test call nodes have a callee"
    target = node.child_by_field_name(language.calls.callee)
    if target is None or not target.text:
        return None
    match = re.match(r"[\w!?]+", target.text.decode())
    return match.group() if match else None


def extract_units(text: str, language: Language, tests: bool = False) -> list[Unit]:
    assert language.units, "Language must declare unit node types"
    kinds = set(language.units)
    attached = set(language.attached)
    calls = language.calls if tests else None
    data = text.encode()
    units: list[Unit] = []
    tree = get_parser(language.grammar).parse(data)
    root = tree.root_node
    stack = [root]
    while stack:
        node = stack.pop()
        is_call = (
            calls is not None and node.type == calls.node and callee(node, language) in calls.names
        )
        if (node.type in kinds or is_call) and node.text:
            first = attached_start(node, attached)
            source = data[first.start_byte : node.end_byte].decode()
            name = node.child_by_field_name("name")
            label = (
                name.text.decode()
                if name is not None and name.text
                else node.text.decode().split("\n", 1)[0].strip()
            )
            units.append(
                Unit(
                    node.type,
                    label,
                    source,
                    headline(node, node.text.decode()),
                    first.start_point.row,
                    node.end_point.row,
                )
            )
            if is_call:
                continue
        stack.extend(node.children)
    ordered = sorted(units, key=lambda unit: (unit.span.line, unit.span.column))
    assert all(unit.source for unit in ordered), "Every unit must have source"
    return ordered
