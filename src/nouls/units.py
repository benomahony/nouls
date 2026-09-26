import re
from dataclasses import dataclass
from functools import lru_cache

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser as _get_parser

from nouls.config import Language


@lru_cache(maxsize=None)
def get_parser(grammar: str) -> Parser:
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
