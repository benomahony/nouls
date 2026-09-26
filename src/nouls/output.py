# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Plain text output for command line results and errors."""

import sys

USAGE_ERROR = 2


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
    """Write an error message to standard error.

    Args:
        message: A plain language message that starts with ``nouls:``, says what went wrong
            and how to fix it.

    Returns:
        The usage error exit code, for the command to return.

    """
    assert message.startswith("nouls: "), (
        f"Error messages start with 'nouls: ' so users know where they came from; got {message!r}"
    )
    written = sys.stderr.write(f"{message}\n")
    assert written == len(message) + 1, "stderr must accept the whole message; check it is open"
    return USAGE_ERROR
