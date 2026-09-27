# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Explain TypeSafe failures in plain language, with what to do about them."""

import os

from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

API_KEY = "TYPESAFE_API_KEY"
GET_A_KEY = (
    f"Get a key from https://docs.typesafe.ai, run export {API_KEY}=<your key>, and try again."
)
CACHED = "Answers received so far are cached, so nothing is asked twice."


def explain(error: TypeSafeError) -> str:
    """Say what went wrong talking to TypeSafe and how to fix it.

    Args:
        error: Any failure raised by the TypeSafe client.

    Returns:
        A plain language message that starts with ``nouls:``.

    """
    if isinstance(error, TypeSafeAuthenticationError):
        message = (
            f"nouls: TypeSafe did not accept the key in {API_KEY}. Check it is copied in full "
            f"and has not been revoked. {GET_A_KEY}"
        )
    elif isinstance(error, TypeSafePermissionDeniedError):
        message = (
            f"nouls: the key in {API_KEY} is not allowed to make this request. Check the model "
            "in your nouls config is one your TypeSafe plan includes, or use another key."
        )
    elif isinstance(error, TypeSafeRateLimitError):
        message = (
            f"nouls: TypeSafe is limiting how fast nouls can ask. {CACHED} Wait a minute and run "
            "again, or lower concurrency in your nouls config."
        )
    elif isinstance(error, TypeSafeAPIConnectionError):
        message = (
            "nouls: nouls could not reach TypeSafe. Check your network connection or proxy and "
            f"run again. {CACHED}"
        )
    elif isinstance(error, TypeSafeAPIError):
        reference = f" and quote request {error.request_id}" if error.request_id else ""
        message = (
            f"nouls: TypeSafe could not answer. {CACHED} Run again, and if it keeps failing, "
            f"contact TypeSafe support{reference}."
        )
    elif not os.environ.get(API_KEY, "").strip():
        message = (
            f"nouls: {API_KEY} is not set, so nouls cannot ask TypeSafe about your code. "
            f"{GET_A_KEY}"
        )
    else:
        message = (
            f"nouls: the TypeSafe client could not start: {error}. Check {API_KEY} and try again."
        )
    assert message.startswith("nouls: "), "Messages start with nouls: so users know their source"
    assert "Traceback" not in message, "explain must summarise the failure, not dump it"
    return message
