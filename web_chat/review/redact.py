"""Scrubbing secrets from evidence, and bounding its size.

Evidence is copied out of a user's space and read by admins and by review
agents, so no credential may travel with it. Two nets catch them:

- the values of this process's environment variables whose names say they
  are secret (``*_KEY``, ``*_TOKEN``, ``*_SECRET``, passwords): exactly the
  provider keys a tool output or an error message may echo;
- shapes of common credentials: ``sk-...`` API keys, bearer tokens, GitHub
  and Slack tokens, AWS access key ids, PEM private keys.

A long text is cut, marked with how much was left out; the cut comes after
the scrub, so a secret is never split past recognition.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

#: Longest text kept whole; the rest is replaced by a marker.
MAX_TEXT = 20_000

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
#: Shorter values are too likely to occur in ordinary text to be replaced.
_MIN_SECRET_LENGTH = 8

_SHAPES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def environment_secrets(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Secret-looking environment values, by variable name."""
    return {
        name: value
        for name, value in environ.items()
        if _SECRET_NAME.search(name) and len(value) >= _MIN_SECRET_LENGTH
    }


class Redactor:
    """Scrubs and bounds every string of a JSON-like value."""

    def __init__(self, secrets: Mapping[str, str], max_text: int = MAX_TEXT) -> None:
        # Longest first: a secret containing another is replaced whole.
        self._secrets = sorted(secrets.items(), key=lambda item: -len(item[1]))
        self._max_text = max_text

    def text(self, value: str) -> str:
        for name, secret in self._secrets:
            if secret in value:
                value = value.replace(secret, f"[redacted: {name}]")
        for shape in _SHAPES:
            value = shape.sub("[redacted]", value)
        if len(value) > self._max_text:
            left_out = len(value) - self._max_text
            value = f"{value[: self._max_text]}\n…[{left_out} more characters left out]"
        return value

    def value(self, value: Any) -> Any:
        """*value* with every string scrubbed and bounded; keys stay as they are."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


def bounded(items: Iterable[Any], limit: int) -> tuple[list[Any], int]:
    """The last *limit* of *items*, and how many earlier ones were left out."""
    items = list(items)
    return items[-limit:] if limit else [], max(0, len(items) - limit)
