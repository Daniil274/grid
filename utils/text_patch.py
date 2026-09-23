"""Applying model-written edits to text files: unified diffs and exact replacements.

Models write patches in several dialects: a plain unified diff, a ``diff --git``
header, hunk headers without line numbers (``@@``) and the ``*** Begin Patch``
envelope. All of them reduce to hunks of context, removed and added lines,
which are located by their content; line numbers are only a hint.

Both edits keep the file's own line endings, so a one-line change to a CRLF
file stays a one-line change instead of rewriting every line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
_SKIPPED = ("diff --git ", "index ", "--- ", "+++ ", "new file mode", "deleted file mode",
            "similarity index", "rename from", "rename to", "*** ")


class PatchError(ValueError):
    """An edit that cannot be applied unambiguously; the message is meant for the model."""


@dataclass
class Hunk:
    hint: int | None                  # 0-based start line from the header, if any
    lines: list[tuple[str, str]]      # (" " | "-" | "+", text)

    @property
    def old(self) -> list[str]:
        return [text for kind, text in self.lines if kind != "+"]

    @property
    def new(self) -> list[str]:
        return [text for kind, text in self.lines if kind != "-"]


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def parse_hunks(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    current: Hunk | None = None
    for raw in patch.replace("\r\n", "\n").split("\n"):
        if raw.startswith("@@"):
            match = _HUNK_HEADER.match(raw)
            current = Hunk(int(match.group(1)) - 1 if match else None, [])
            hunks.append(current)
            continue
        if raw.startswith(("*** ", "diff --git ")) or raw == r"\ No newline at end of file":
            continue
        if current is None or (raw.startswith(_SKIPPED) and not current.lines):
            # File headers before the first changed line of a hunk.
            if current is None and raw.strip() and not raw.startswith(_SKIPPED):
                raise PatchError(
                    "The patch has no hunk: start each changed block with a line '@@' "
                    "and prefix lines with ' ' (context), '-' (remove) or '+' (add)."
                )
            continue
        if raw == "":
            # A blank context line whose leading space was stripped by the model.
            current.lines.append((" ", ""))
            continue
        kind = raw[0]
        if kind not in " +-":
            raise PatchError(
                f"Invalid patch line {raw[:80]!r}: every hunk line starts with ' ', '-' or '+'."
            )
        current.lines.append((kind, raw[1:]))
    for hunk in hunks:
        while hunk.lines and hunk.lines[-1] == (" ", ""):
            hunk.lines.pop()
    hunks = [hunk for hunk in hunks if hunk.lines]
    if not hunks:
        raise PatchError("The patch contains no changes.")
    if any(all(kind == " " for kind, _ in hunk.lines) for hunk in hunks):
        raise PatchError("A hunk has only context lines and changes nothing.")
    return hunks


def _positions(lines: list[str], block: list[str], strip: bool) -> list[int]:
    if not block:
        return []
    norm = (lambda s: s.rstrip()) if strip else (lambda s: s)
    target = [norm(line) for line in block]
    return [
        start for start in range(len(lines) - len(block) + 1)
        if [norm(line) for line in lines[start:start + len(block)]] == target
    ]


def _locate(lines: list[str], hunk: Hunk, floor: int) -> int:
    old = hunk.old
    if not old:
        if hunk.hint is None:
            raise PatchError("A hunk that only adds lines needs context lines or '@@ -N +N @@'.")
        return min(max(hunk.hint, floor), len(lines))
    for strip in (False, True):
        found = [start for start in _positions(lines, old, strip) if start >= floor]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            if hunk.hint is not None:
                return min(found, key=lambda start: abs(start - hunk.hint))
            raise PatchError(
                f"The context of a hunk occurs {len(found)} times ({old[0][:60]!r}...): "
                "add more context lines or line numbers to the '@@' header."
            )
    first = old[0].strip()
    near = [i + 1 for i, line in enumerate(lines) if first and first in line][:3]
    where = f" A similar line is at {near}." if near else ""
    raise PatchError(
        f"The lines to replace were not found: {old[0][:80]!r}.{where} "
        "Read the file again and copy the lines exactly."
    )


def apply_patch(original: str, patch: str) -> str:
    """Apply a unified diff to *original*; raises PatchError with a fixable reason."""
    newline = newline_of(original)
    trailing = original.endswith(("\n", "\r\n"))
    lines = original.replace("\r\n", "\n").split("\n")
    if trailing:
        lines.pop()
    floor = 0
    for hunk in parse_hunks(patch):
        start = _locate(lines, hunk, floor)
        lines[start:start + len(hunk.old)] = hunk.new
        floor = start + len(hunk.new)
    return newline.join(lines) + (newline if trailing else "")


def replace_once(original: str, old: str, new: str) -> str:
    """Replace the single occurrence of *old*; refuses a missing or ambiguous text."""
    if not old:
        raise PatchError("old_text is empty: copy the exact text to replace from the file.")
    if old == new:
        raise PatchError("old_text and new_text are identical: nothing would change.")
    newline = newline_of(original)
    text = original.replace("\r\n", "\n")
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    count = text.count(old)
    if count == 0:
        stripped = old.strip()
        hint = " It occurs with different surrounding whitespace." if stripped and stripped in text else ""
        raise PatchError(
            f"old_text was not found.{hint} Read the file and copy the text exactly, "
            "including indentation."
        )
    if count > 1:
        raise PatchError(
            f"old_text occurs {count} times: include more surrounding lines so it is unique."
        )
    result = text.replace(old, new)
    return result.replace("\n", newline) if newline != "\n" else result


def keep_newlines(existing: str | None, content: str) -> str:
    """Write *content* with the line endings the file already uses."""
    if existing is None or newline_of(existing) == "\n":
        return content
    return content.replace("\r\n", "\n").replace("\n", "\r\n")
