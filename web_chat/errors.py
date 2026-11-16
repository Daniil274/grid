"""The text a failure shows the user.

An exception built without a message - ``asyncio.CancelledError()``,
``TimeoutError()``, a bare ``RuntimeError()`` - has an empty ``str()``;
sending that on is how "errors without the errors themselves" happen.
Every user-facing failure text goes through :func:`error_text`, which
falls back to the exception's type name so the message is never empty.
"""

from __future__ import annotations


def error_text(exc: BaseException) -> str:
    """The failure's message, or its type name when the message is empty."""
    return str(exc) or type(exc).__name__
