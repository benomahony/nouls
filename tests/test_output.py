# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for command line output."""

import io

import pytest
from rich.console import Console

from nouls import output
from nouls.output import USAGE_ERROR, fail, split_fix

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "there is no rule called made_up. Run nouls rules to list every rule.",
            ("there is no rule called made_up.", "Run nouls rules to list every rule."),
        ),
        (
            "cannot check src/a.py because it does not exist. Fix the path.",
            ("cannot check src/a.py because it does not exist.", "Fix the path."),
        ),
        ("everything is fine.", ("everything is fine.", "")),
    ],
)
def test_messages_split_into_problem_and_fix(message: str, expected: tuple[str, str]) -> None:
    """Messages split into the problem and the fix at the first sentence break."""
    assert split_fix(message) == expected


def test_errors_are_a_panel_in_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors are a titled panel with the problem and fix on their own lines in a terminal."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        output, "errors", Console(file=buffer, force_terminal=True, no_color=True, width=100)
    )
    assert fail("nouls: there is no rule called made_up. Run nouls rules.") == USAGE_ERROR
    lines = buffer.getvalue().splitlines()
    assert lines[0].startswith("╭─ nouls ")
    assert "There is no rule called made_up." in lines[1]
    assert "Run nouls rules." in lines[2]
    assert "nouls: there" not in buffer.getvalue()


def test_errors_are_one_plain_line_when_piped(capsys: pytest.CaptureFixture[str]) -> None:
    """Errors are one plain line when piped, so scripts and editors can read them."""
    assert fail("nouls: there is no rule called made_up. Run nouls rules.") == USAGE_ERROR
    assert capsys.readouterr().err == "nouls: there is no rule called made_up. Run nouls rules.\n"
