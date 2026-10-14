"""Masking of credentials before anything reaches the memory store.

Memory is a plain SQLite file shown in the web panel and injected into prompts,
unlike conversations, which are encrypted. A password typed into a chat must
not outlive that chat there, so every stored text passes :func:`redact_secrets`:
the value is replaced by :data:`REDACTED` and the sentence around it is kept
(``пароль: abc123`` -> ``пароль: [secret]``), so what was done stays usable.

Key *file names* (``daniil.key``) are not secrets and are kept; the passphrase
that opens them, inline private keys and well-known token formats are masked.
"""
from __future__ import annotations

import re

REDACTED = "[secret]"

_WORD = (r"(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|client[_ -]?secret|secret|auth[_ -]?token|"
         r"access[_ -]?token|token|passw(?:or)?d|passphrase|pwd|"
         r"парол(?:ь|ем|я|ю)|токен(?:ом|а|у)?|секрет(?:ом|а|у)?|ключ(?:ом)? api)")
_NOT_DONE = rf"(?!{re.escape(REDACTED)})"

# "password: x", "token = x", "пароль — x", "password is x"
_LABELLED = re.compile(
    rf"(?<![\w-])({_WORD}\s*(?:is|—|–|:|=)\s*[\"'`]?){_NOT_DONE}[^\s\"'`,;)\]]+?(?=[.!?]?(?:\s|$|[\"'`,;)\]]))", re.IGNORECASE)
# "с паролем `x`", "password 'x'"
_QUOTED = re.compile(
    rf"(?<![\w-])({_WORD}\s+[\"'`]){_NOT_DONE}[^\s\"'`]+(?=[\"'`])", re.IGNORECASE)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_TOKENS = re.compile(
    r"\bsk-(?:ant-)?[\w-]{12,}|\bghp_\w{12,}|\bgho_\w{12,}|\bgithub_pat_\w{20,}|\bglpat-[\w-]{12,}|"
    r"\bxox[abprs]-[\w-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[\w-]{30,}|"
    r"(?<=Bearer )[\w.~+/=-]{12,}|(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")


def redact_secrets(text: str) -> str:
    """*text* with credential values replaced by :data:`REDACTED`."""
    if not text:
        return text
    text = _PEM.sub(REDACTED, text)
    text = _TOKENS.sub(REDACTED, text)
    text = _LABELLED.sub(lambda m: m[1] + REDACTED, text)
    return _QUOTED.sub(lambda m: m[1] + REDACTED, text)
