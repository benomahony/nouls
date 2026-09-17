from dataclasses import dataclass

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from nouls.config import Language


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
        return self.first_line <= line <= self.last_line


def headline(node: Node, source: str) -> Span:
    name = node.child_by_field_name("name")
    if name is not None:
        return Span(name.start_point.row, name.start_point.column, name.end_point.row, name.end_point.column)
    first_line = source.split("\n", 1)[0]
    return Span(
        node.start_point.row,
        node.start_point.column,
        node.start_point.row,
        node.start_point.column + len(first_line),
    )


def extract_units(text: str, language: Language) -> list[Unit]:
    root = get_parser(language.grammar).parse(text.encode()).root_node
    kinds = set(language.units)
    units: list[Unit] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in kinds and node.text:
            source = node.text.decode()
            name = node.child_by_field_name("name")
            label = name.text.decode() if name is not None and name.text else source.split("\n", 1)[0].strip()
            units.append(
                Unit(node.type, label, source, headline(node, source), node.start_point.row, node.end_point.row)
            )
        stack.extend(node.children)
    return sorted(units, key=lambda unit: (unit.span.line, unit.span.column))
