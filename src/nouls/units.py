import gc
from dataclasses import dataclass
from functools import lru_cache

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser as _get_parser

from nouls.config import Language

_gc_disabled = False


def _disable_cyclic_gc() -> None:
    # tree-sitter's Tree/Node objects are unsafe under CPython's cyclic
    # collector: collecting one mid-traversal, or even later once it's
    # garbage, reliably segfaults. Refcounting alone still frees them
    # promptly, so disabling the cyclic collector once is enough.
    global _gc_disabled
    if not _gc_disabled:
        gc.disable()
        _gc_disabled = True
    assert _gc_disabled, "The disable-once flag must be set after this call"
    assert not gc.isenabled(), "Cyclic GC must stay disabled once tree-sitter has parsed anything"


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


def extract_units(text: str, language: Language) -> list[Unit]:
    assert language.units, "Language must declare unit node types"
    _disable_cyclic_gc()
    kinds = set(language.units)
    units: list[Unit] = []
    tree = get_parser(language.grammar).parse(text.encode())
    root = tree.root_node
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in kinds and node.text:
            source = node.text.decode()
            name = node.child_by_field_name("name")
            label = (
                name.text.decode()
                if name is not None and name.text
                else source.split("\n", 1)[0].strip()
            )
            units.append(
                Unit(
                    node.type,
                    label,
                    source,
                    headline(node, source),
                    node.start_point.row,
                    node.end_point.row,
                )
            )
        stack.extend(node.children)
    ordered = sorted(units, key=lambda unit: (unit.span.line, unit.span.column))
    assert all(unit.source for unit in ordered), "Every unit must have source"
    return ordered
