# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Tests for explaining TypeSafe failures."""

from pathlib import Path

import httpx2
import pytest
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafeInternalServerError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from nouls.output import USAGE_ERROR
from nouls.server import lint
from nouls.typesafe import API_KEY, explain
from tests.conftest import PYTHON, FakeClient, FakeServer, as_server, run

pytestmark = pytest.mark.unit

NO_BODY = httpx2.Headers()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TypeSafeAuthenticationError(401, {"error": "bad"}, NO_BODY), "did not accept the key"),
        (TypeSafePermissionDeniedError(403, None, NO_BODY), "not allowed to make this request"),
        (TypeSafeRateLimitError(429, None, NO_BODY), "lower concurrency"),
        (TypeSafeAPIConnectionError("refused"), "could not reach TypeSafe"),
        (
            TypeSafeInternalServerError(500, None, httpx2.Headers({"x-request-id": "req_9"})),
            "contact TypeSafe support",
        ),
    ],
)
def test_each_failure_says_what_to_do(error: TypeSafeError, expected: str) -> None:
    """Each failure says in plain language what went wrong and what to do."""
    message = explain(error)
    assert message.startswith("nouls: ")
    assert expected in message
    assert not any(code in message for code in ("401", "403", "429", "500"))


def test_a_missing_key_says_how_to_set_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing key says how to set one."""
    monkeypatch.delenv(API_KEY, raising=False)
    message = explain(TypeSafeError("No API key was provided."))
    assert f"{API_KEY} is not set" in message
    assert f"export {API_KEY}=" in message


def test_check_without_a_key_explains_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check without a key explains how to set one instead of printing a traceback."""
    monkeypatch.delenv(API_KEY, raising=False)
    _ = (tmp_path / "app.py").write_text(PYTHON, encoding="utf-8")
    assert run(["check", str(tmp_path)]) == USAGE_ERROR
    err = capsys.readouterr().err
    assert f"{API_KEY} is not set" in err
    assert "Traceback" not in err


def test_check_with_a_rejected_key_explains_it(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check with a rejected key explains it."""
    client.error = TypeSafeAuthenticationError(401, {"error": "bad"}, NO_BODY)
    _ = (tmp_path / "app.py").write_text(PYTHON, encoding="utf-8")
    assert run(["check", str(tmp_path)]) == USAGE_ERROR
    assert "did not accept the key" in capsys.readouterr().err


async def test_the_editor_warns_once_and_publishes_nothing(ls: FakeServer) -> None:
    """The editor shows the warning once, however many lints fail the same way."""
    ls.client.error = TypeSafeAuthenticationError(401, {"error": "bad"}, NO_BODY)
    await lint(as_server(ls), "file:///app.py")
    await lint(as_server(ls), "file:///app.py")
    assert ls.published == []
    (shown,) = ls.shown
    assert "did not accept the key" in shown.message
