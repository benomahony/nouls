# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Measure functions and files from their syntax trees, for rules that need no question.

Node types differ between grammars, so each measure lists the names the supported grammars use
for the same construct, such as ``if_statement`` in Python and C and ``if_expression`` in Rust.
Only named nodes count, because keywords such as ``if`` are also anonymous nodes.
"""

from typing import Literal

from tree_sitter import Node

Metric = Literal[
    "lines", "parameters", "complexity", "nesting", "variadic_parameters", "empty_blocks", "gotos"
]
METRICS: tuple[Metric, ...] = (
    "lines",
    "parameters",
    "complexity",
    "nesting",
    "variadic_parameters",
    "empty_blocks",
    "gotos",
)
FILE_METRICS: frozenset[Metric] = frozenset({"lines"})
NOUNS: dict[Metric, str] = {
    "lines": "lines of code",
    "parameters": "parameters",
    "complexity": "cyclomatic complexity",
    "nesting": "nesting depth",
    "variadic_parameters": "variadic parameters",
    "empty_blocks": "empty blocks",
    "gotos": "gotos",
}
MEASURES = frozenset({"complexity", "nesting"})  # Read as "the complexity", not "the number of"

LOOPS = frozenset(
    {
        "for_statement",
        "for_in_statement",
        "for_expression",
        "for_range_loop",
        "enhanced_for_statement",
        "foreach_statement",
        "for",
        "while_statement",
        "while_expression",
        "while",
        "until",
        "while_modifier",
        "until_modifier",
        "do_statement",
    }
)
CONDITIONS = frozenset(
    {
        "if_statement",
        "if_expression",
        "if",
        "unless",
        "if_modifier",
        "unless_modifier",
    }
)
# Branches that add a path but sit inside the construct that owns them, so they add no nesting.
ALTERNATIVES = frozenset(
    {
        "elif_clause",
        "elsif",
        "case_clause",
        "switch_case",
        "expression_case",
        "type_case",
        "communication_case",
        "case_statement",
        "switch_label",
        "match_arm",
        "when",
        "catch_clause",
        "except_clause",
        "rescue",
        "conditional_expression",
        "ternary_expression",
        "conditional",
    }
)
BRANCHES = LOOPS | CONDITIONS | ALTERNATIVES
NESTS = (
    LOOPS
    | CONDITIONS
    | frozenset(
        {
            "loop_expression",
            "switch_statement",
            "expression_switch_statement",
            "type_switch_statement",
            "select_statement",
            "switch_expression",
            "match_statement",
            "match_expression",
            "case",
            "try_statement",
            "with_statement",
            "begin",
        }
    )
)
BOOLEAN_OPERATORS = frozenset({"&&", "||", "and", "or", "??"})
BOOLEAN_EXPRESSIONS = frozenset({"binary_expression", "boolean_operator", "binary"})
VARIADIC = frozenset(
    {
        "list_splat_pattern",
        "dictionary_splat_pattern",
        "rest_pattern",
        "variadic_parameter",
        "variadic_parameter_declaration",
        "spread_parameter",
        "splat_parameter",
        "hash_splat_parameter",
    }
)
NOT_PARAMETERS = frozenset({"self_parameter", "keyword_separator", "positional_separator"})
RECEIVERS = frozenset({"self", "cls", "this", "void"})
BLOCKS = frozenset({"block", "statement_block", "compound_statement"})
PLACEHOLDERS = frozenset({"pass_statement", "empty_statement"})


def descendants(node: Node) -> list[Node]:
    """List a node and every node under it.

    Args:
        node: The root of the walk.

    Returns:
        Every node, the root first.

    """
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        found.append(current)
        stack.extend(reversed(current.children))
    assert found[0] == node, "The walk must start at its root"
    assert len(found) >= 1 + node.child_count, "The walk must include every child"
    return found


def is_comment(node: Node) -> bool:
    """Tell whether a node is a comment in any grammar.

    Args:
        node: Any node.

    Returns:
        True for ``comment``, ``line_comment``, ``block_comment`` and similar nodes.

    """
    assert node.type, "A node must have a type"
    found = "comment" in node.type
    assert not found or node.type.endswith("comment"), f"Unexpected comment type {node.type}"
    return found


def parameters_of(node: Node) -> list[Node]:
    """Find a function's parameters.

    C and C++ keep the parameters inside nested declarators, so the declarators are followed down
    until one has them.

    Args:
        node: A function, method or test call node.

    Returns:
        The named parameter nodes, without comments, receivers such as ``self`` or separators.

    """
    holder: Node | None = node
    while holder is not None and holder.child_by_field_name("parameters") is None:
        holder = holder.child_by_field_name("declarator")
    found = None if holder is None else holder.child_by_field_name("parameters")
    children = [] if found is None else found.named_children
    params = [
        child
        for child in children
        if not is_comment(child)
        and child.type not in NOT_PARAMETERS
        and (child.text or b"").decode().strip() not in RECEIVERS
    ]
    assert len(params) <= len(children), "Filtering parameters must not add any"
    assert all(child.is_named for child in params), "Parameters are named nodes"
    return params


def is_variadic(parameter: Node) -> bool:
    """Tell whether a parameter takes any number of arguments.

    Args:
        parameter: A parameter node.

    Returns:
        True for ``*args``, ``**kwargs``, ``...rest``, ``params`` arrays and C varargs.

    """
    first = parameter.named_children[0] if parameter.named_children else None
    found = (
        parameter.type in VARIADIC
        or (first is not None and first.type in VARIADIC)
        or (parameter.text or b"").decode().startswith("params ")
    )
    assert parameter.is_named, "Only named parameter nodes are checked"
    assert isinstance(found, bool), "is_variadic must answer yes or no"
    return found


def is_else_if(node: Node) -> bool:
    """Tell whether a condition is the ``else if`` of an enclosing condition.

    Args:
        node: A condition node.

    Returns:
        True when the condition continues a chain instead of nesting inside it.

    """
    assert node.type in CONDITIONS, f"is_else_if needs a condition node, not {node.type}"
    parent = node.parent
    found = parent is not None and (parent.type == "else_clause" or parent.type in CONDITIONS)
    assert not found or parent is not None, "Only a node with a parent continues a chain"
    return found


def nesting(node: Node) -> int:
    """Measure how deeply control structures nest inside a node.

    Args:
        node: A function node.

    Returns:
        The largest number of enclosing loops, conditions, switches and try blocks.

    """
    deepest = 0
    stack = [(child, 0) for child in node.children]
    while stack:
        current, depth = stack.pop()
        nests = (
            current.is_named
            and current.type in NESTS
            and not (current.type in CONDITIONS and is_else_if(current))
        )
        inner = depth + 1 if nests else depth
        deepest = max(deepest, inner)
        stack.extend((child, inner) for child in current.children)
    assert deepest >= 0, "Nesting cannot be negative"
    assert deepest < len(descendants(node)), "Each level of nesting is a separate node"
    return deepest


def is_empty_block(node: Node) -> bool:
    """Tell whether a block holds no statements.

    A block holding a comment explains why it is empty, so it does not count.

    Args:
        node: Any node.

    Returns:
        True for a block with nothing in it, or only ``pass`` or ``;``.

    """
    assert node.type, "A node must have a type"
    found = node.type in BLOCKS and all(child.type in PLACEHOLDERS for child in node.named_children)
    assert not found or not any(is_comment(c) for c in node.named_children), (
        "A block with a comment is explained, so it must not count as empty"
    )
    return found


def measure_function(node: Node) -> dict[Metric, int]:
    """Measure a function.

    Args:
        node: A function, method or test call node.

    Returns:
        Every function metric, by name.

    """
    nodes = descendants(node)
    params = parameters_of(node)
    branches = sum(n.is_named and n.type in BRANCHES for n in nodes)
    operators = sum(
        not n.is_named
        and n.type in BOOLEAN_OPERATORS
        and n.parent is not None
        and n.parent.type in BOOLEAN_EXPRESSIONS
        for n in nodes
    )
    measured: dict[Metric, int] = {
        "parameters": len(params),
        "complexity": 1 + branches + operators,
        "nesting": nesting(node),
        "variadic_parameters": sum(map(is_variadic, params)),
        "empty_blocks": sum(map(is_empty_block, nodes)),
        "gotos": sum(n.type == "goto_statement" for n in nodes),
    }
    assert set(measured) == set(METRICS) - FILE_METRICS, "Every function metric is measured"
    assert measured["complexity"] >= 1, "Every function has at least one path"
    return measured


def measure_file(root: Node) -> dict[Metric, int]:
    """Measure a whole file.

    Args:
        root: The file's syntax tree.

    Returns:
        Every file metric, by name. Lines of code are lines with anything but comments on them.

    """
    rows = {
        row
        for n in descendants(root)
        if n.child_count == 0 and not is_comment(n) and n.end_byte > n.start_byte
        for row in range(n.start_point.row, n.end_point.row + 1)
    }
    measured: dict[Metric, int] = {"lines": len(rows)}
    assert set(measured) == FILE_METRICS, "Every file metric is measured"
    assert measured["lines"] <= root.end_point.row + 1, "A file has no more code lines than lines"
    return measured


def describe(metric: Metric, value: int, limit: int) -> str:
    """Say what was measured against what was allowed.

    Args:
        metric: The metric.
        value: What the function or file measured.
        limit: The largest value the rule allows.

    Returns:
        Text such as ``parameters: 9, limit 7``.

    """
    assert value >= 0, "Metrics are never negative"
    assert limit >= 0, "Limits are never negative"
    return f"{NOUNS[metric]}: {value}, limit {limit}"


def check_for(metric: Metric, limit: int) -> str:
    """Phrase a metric rule as the question it answers without asking.

    Args:
        metric: The metric.
        limit: The largest value the rule allows.

    Returns:
        Text such as ``Is the number of parameters above 7?``.

    """
    assert limit >= 0, "Limits are never negative"
    subject = f"the {NOUNS[metric]}" if metric in MEASURES else f"the number of {NOUNS[metric]}"
    text = f"Is {subject} above {limit}?"
    assert text.endswith("?"), "A check reads as a question"
    return text
