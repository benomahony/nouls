# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Command line output: plain lines for pipes, and Rich panels for errors in a terminal."""

import sys

from rich.console import Console, Group
from rich.highlighter import ReprHighlighter
from rich.panel import Panel
from rich.text import Text

USAGE_ERROR = 2
PREFIX = "nouls: "
errors = Console(stderr=True)


def split_fix(message: str) -> tuple[str, str]:
    """Split a message into the problem and how to fix it.

    Messages say what went wrong in their first sentence and how to fix it after, so the
    split falls at the first full stop followed by a capital letter.

    Args:
        message: A message without the ``nouls:`` prefix.

    Returns:
        The problem, and the fix, which is empty when the message has one sentence.

    """
    cut = next(
        (
            i
            for i in range(len(message) - 2)
            if message[i : i + 2] == ". " and message[i + 2].isupper()
        ),
        -1,
    )
    problem, fix = (message, "") if cut < 0 else (message[: cut + 1], message[cut + 2 :])
    assert problem, "Every message has a problem to state"
    assert len(problem) + len(fix) <= len(message), "Splitting must not add text"
    return problem, fix


def say(text: str) -> None:
    """Write one line of plain text to standard output.

    Args:
        text: The line to write, without a trailing newline.

    """
    assert "\n" not in text.rstrip("\n"), (
        f"say writes one line but got {text!r}; call it once per line"
    )
    written = sys.stdout.write(f"{text}\n")
    assert written == len(text) + 1, "stdout must accept the whole line; check it is not closed"


def fail(message: str) -> int:
    """Write an error message to standard error, as a Rich panel in a terminal.

    Args:
        message: A plain language message that starts with ``nouls:``, says what went wrong
            and how to fix it.

    Returns:
        The usage error exit code, for the command to return.

    """
    assert message.startswith(PREFIX), (
        f"Error messages start with 'nouls: ' so users know where they came from; got {message!r}"
    )
    if not errors.is_terminal:
        written = sys.stderr.write(f"{message}\n")
        assert written == len(message) + 1, "stderr must accept the whole message; check it is open"
        return USAGE_ERROR
    problem, fix = split_fix(message.removeprefix(PREFIX))
    body = [Text(problem[0].upper() + problem[1:], style="bold")]
    if fix:
        body.append(ReprHighlighter()(Text(fix)))
    errors.print(
        Panel(
            Group(*body),
            title="nouls",
            title_align="left",
            border_style="red",
            expand=False,
        )
    )
    return USAGE_ERROR
