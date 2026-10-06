"""
File Tools — reading, writing, editing, and appending files.
All paths are isolated in the agent's working directory via resolve_agent_path_auto().
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils import confined_fs
from utils.path_utils import resolve_agent_path_auto, display_agent_path_auto


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_OUTPUT_CHARS = 10_000
_HUNK_HEADER_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]]
    # A hunk without line numbers ("@@" or "@@ <anchor line>", the
    # *** Begin Patch format) is placed by its context alone.
    numbered: bool = True
    anchor: Optional[str] = None

    @property
    def old_span(self) -> int:
        return sum(1 for kind, _ in self.lines if kind in (" ", "-"))


class _PatchApplyError(Exception):
    """Raised when a unified diff cannot be applied safely."""


_PATCH_FORMAT_HINT = (
    "Expected format: optional '--- a/file' / '+++ b/file', then hunks that start "
    "with '@@ -start,count +start,count @@' (or a bare '@@' located by its context), "
    "each line prefixed with ' ' (context), '-' (removed) or '+' (added)."
)

# Envelope lines of the "*** Begin Patch" format; the hunks inside are diffs.
_ENVELOPE_PREFIXES = ("*** Begin Patch", "*** End Patch", "*** Update File:", "*** End of File")


def _split_file_content(text: str) -> tuple[list[str], bool]:
    """Return file lines without terminators and whether the file ends with LF."""
    return text.splitlines(), text.endswith("\n")


def _parse_unified_diff(patch_content: str) -> list[_Hunk]:
    """Parse a unified diff, or the "*** Begin Patch" format, into hunks."""
    patch_lines = patch_content.splitlines()
    hunks: list[_Hunk] = []
    i = 0

    while i < len(patch_lines):
        line = patch_lines[i]
        if line.startswith(("*** Add File:", "*** Delete File:", "*** Move to:")):
            raise _PatchApplyError(
                f"❌ file_edit changes one existing file: use file_write to create "
                f"a file instead of '{line.strip()}'"
            )
        if line.startswith(_ENVELOPE_PREFIXES):
            i += 1
            continue
        if line.startswith("---") or line.startswith("+++"):
            i += 1
            continue

        if not line.startswith("@@"):
            if not line.strip():
                i += 1
                continue
            raise _PatchApplyError(f"❌ Expected hunk header, found: {line}\n{_PATCH_FORMAT_HINT}")

        match = _HUNK_HEADER_RE.match(line)
        if match:
            old_start = int(match.group(1)) - 1
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3)) - 1
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            numbered, anchor = True, None
        elif line.lstrip("@").strip().startswith("-") and re.match(r"@@\s*-\d", line):
            raise _PatchApplyError(f"❌ Invalid hunk header: {line}\n{_PATCH_FORMAT_HINT}")
        else:
            old_start = old_count = new_start = new_count = 0
            numbered = False
            anchor = line[2:].strip().removesuffix("@@").strip() or None
        i += 1

        hunk_lines: list[tuple[str, str]] = []
        while i < len(patch_lines):
            current = patch_lines[i]
            if current.startswith("@@") or current.startswith("---") or current.startswith("+++"):
                break
            if current.startswith(_ENVELOPE_PREFIXES):
                break
            if current == r"\ No newline at end of file":
                i += 1
                continue
            if not current:
                # An empty context line whose leading space was trimmed.
                hunk_lines.append((" ", ""))
                i += 1
                continue

            prefix = current[0]
            if prefix not in (" ", "+", "-"):
                raise _PatchApplyError(f"❌ Invalid patch line: {current}\n{_PATCH_FORMAT_HINT}")
            hunk_lines.append((prefix, current[1:]))
            i += 1

        # Trailing blank lines are separators between hunks, not context.
        while hunk_lines and hunk_lines[-1] == (" ", "") and not numbered:
            hunk_lines.pop()

        hunks.append(
            _Hunk(
                old_start=max(0, old_start),
                old_count=old_count,
                new_start=max(0, new_start),
                new_count=new_count,
                lines=hunk_lines,
                numbered=numbered,
                anchor=anchor,
            )
        )

    if not hunks:
        raise _PatchApplyError(f"❌ Patch contains no hunk blocks\n{_PATCH_FORMAT_HINT}")

    return hunks


def _match_hunk_at(
    lines: list[str],
    start: int,
    hunk: _Hunk,
) -> tuple[bool, Optional[int], Optional[str], Optional[str]]:
    """Return whether the old side of hunk matches lines at start."""
    old_side = [(kind, content) for kind, content in hunk.lines if kind in (" ", "-")]

    if start < 0:
        return False, 0, old_side[0][1] if old_side else None, None
    if start + len(old_side) > len(lines):
        idx = max(0, len(lines) - start)
        expected = old_side[idx][1] if idx < len(old_side) else None
        return False, idx, expected, None

    for offset, (_, expected) in enumerate(old_side):
        actual = lines[start + offset]
        if actual != expected:
            return False, offset, expected, actual

    return True, None, None, None


def _find_hunk_start(lines: list[str], hunk: _Hunk, expected_start: int) -> int:
    """Find the best hunk application point, tolerating small line drift."""
    old_side = [(kind, content) for kind, content in hunk.lines if kind in (" ", "-")]

    if not old_side:
        return max(0, min(expected_start, len(lines)))

    max_start = max(0, len(lines) - len(old_side))
    expected_start = max(0, min(expected_start, max_start))
    radius = max(8, min(64, len(old_side) * 3))

    candidates: list[int] = []
    for delta in range(radius + 1):
        for start in (expected_start - delta, expected_start + delta):
            if 0 <= start <= max_start and start not in candidates:
                candidates.append(start)

    first_expected = old_side[0][1]
    for start in range(0, max_start + 1):
        if start in candidates:
            continue
        if lines[start] == first_expected:
            candidates.append(start)

    best_error: Optional[tuple[int, Optional[int], Optional[str], Optional[str]]] = None
    for start in candidates:
        matched, mismatch_idx, expected, actual = _match_hunk_at(lines, start, hunk)
        if matched:
            return start
        if best_error is None or abs(start - expected_start) < abs(best_error[0] - expected_start):
            best_error = (start, mismatch_idx, expected, actual)

    mismatch_start, mismatch_idx, expected, actual = best_error or (expected_start, 0, None, None)
    line_no = mismatch_start + (mismatch_idx or 0) + 1
    if expected is None:
        raise _PatchApplyError(
            f"❌ Hunk does not fit in file around line {expected_start + 1}. "
            f"Check the offset and context of the patch."
        )
    raise _PatchApplyError(
        f"❌ Patch context not found near line {expected_start + 1}: "
        f"at line {line_no} expected '{expected}', found '{actual if actual is not None else '<EOF>'}'"
    )


def _find_unnumbered_hunk_start(lines: list[str], hunk: _Hunk, search_from: int) -> int:
    """Place a hunk without line numbers: its old side must match exactly once.

    The search starts after the previous hunk, and after the anchor line when
    the header names one ("@@ def name"). Two matches are refused rather than
    guessed: editing the wrong copy of a repeated block is worse than failing.
    """
    old_side = [content for kind, content in hunk.lines if kind in (" ", "-")]
    if not old_side:
        raise _PatchApplyError(
            "❌ A hunk without line numbers needs context or removed lines to locate it"
        )
    begin = search_from
    if hunk.anchor:
        anchored = [
            index for index in range(search_from, len(lines))
            if lines[index].strip() == hunk.anchor.strip()
        ]
        if anchored:
            begin = anchored[0]
    span = len(old_side)
    matches = [
        start for start in range(begin, len(lines) - span + 1)
        if lines[start:start + span] == old_side
    ]
    if not matches and begin != 0:
        matches = [
            start for start in range(0, len(lines) - span + 1)
            if lines[start:start + span] == old_side
        ]
    if not matches:
        # Whitespace drift is the usual cause; report the first line that differs.
        raise _PatchApplyError(
            f"❌ Patch context not found: no place in the file matches the hunk's "
            f"context/removed lines starting with '{old_side[0]}'. Re-read the file "
            f"and copy the lines exactly."
        )
    if len(matches) > 1:
        raise _PatchApplyError(
            f"❌ Patch context is ambiguous: it matches {len(matches)} places "
            f"(lines {', '.join(str(m + 1) for m in matches[:5])}). Add more context "
            f"lines or use '@@ -start,count +start,count @@' with line numbers."
        )
    return matches[0]


def _apply_unified_patch(original: str, patch_content: str) -> str:
    """Apply unified diff content to the original text."""
    hunks = _parse_unified_diff(patch_content)
    result_lines, had_trailing_newline = _split_file_content(original)
    current_offset = 0
    search_from = 0

    for hunk in hunks:
        if hunk.numbered:
            expected_start = hunk.old_start + current_offset
            start = _find_hunk_start(result_lines, hunk, expected_start)
        else:
            start = _find_unnumbered_hunk_start(result_lines, hunk, search_from)
        replacement = [content for kind, content in hunk.lines if kind in (" ", "+")]
        old_span = hunk.old_span

        result_lines = result_lines[:start] + replacement + result_lines[start + old_span:]
        current_offset += len(replacement) - old_span
        search_from = start + len(replacement)

    updated = "\n".join(result_lines)
    if had_trailing_newline and result_lines:
        updated += "\n"
    return updated


def _truncate(content: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    remaining = len(content) - max_chars
    return content[:max_chars] + f"\n\n... [truncated {remaining} characters, use limit_lines and offset] ..."


def _resolve(raw: str) -> tuple:
    """
    Returns (visible_path, resolved_path_str).
    Raises ValueError if path escapes workspace.
    """
    visible = display_agent_path_auto(raw)
    resolved = resolve_agent_path_auto(raw)  # raises ValueError on sandbox escape
    return visible, resolved


@function_tool
def file_read(
    filepath: str,
    offset: int = 0,
    limit_lines: Optional[int] = None,
) -> str:
    """
    Reads file content.

    Args:
        filepath:    Path to the file
        offset:      Starting line (0-indexed)
        limit_lines: Number of lines to read (None = all)

    Returns:
        File content or a portion of it
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)

        if not path.exists():
            return f"❌ File not found: {visible}"
        if not path.is_file():
            return f"❌ Not a file: {visible}"

        file_size = path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            return f"⚠️ File too large ({file_size} bytes). Use limit_lines and offset."

        content = confined_fs.read_text(path, errors="replace")
        lines = content.split("\n")
        total = len(lines)

        if offset > 0 or limit_lines is not None:
            start = max(0, offset)
            end = start + limit_lines if limit_lines is not None else total
            end = min(end, total)
            sliced = lines[start:end]
            header = f"📄 {visible} (lines {start + 1}–{end} of {total}):\n"
            content = "\n".join(sliced)
        else:
            header = f"📄 {visible} ({total} lines):\n"

        return header + "\n" + _truncate(content)

    except Exception as exc:
        return f"❌ Read error: {exc}"


@function_tool
def file_write(
    filepath: str,
    content: str,
    overwrite: bool = False,
) -> str:
    """
    Writes content to a file.

    Args:
        filepath:  Path to the file
        content:   Content to write
        overwrite: Allow overwriting an existing file

    Returns:
        Operation result
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)
        existed_before = path.exists()

        if existed_before and not overwrite:
            return f"❌ File already exists: {visible}. Use overwrite=true to overwrite."

        confined_fs.make_dirs(path.parent)
        confined_fs.write_text(path, content)

        lines = len(content.split("\n"))
        size = path.stat().st_size
        verb = "overwritten" if existed_before else "created"
        emoji = "📝" if existed_before else "✅"
        return f"{emoji} File {verb}: {visible} ({lines} lines, {size} bytes)"

    except Exception as exc:
        return f"❌ Write error: {exc}"


@function_tool
def file_append(
    filepath: str,
    content: str,
) -> str:
    """
    Appends content to the end of a file.
    Automatically inserts a newline if the file does not end with one.

    Args:
        filepath: Path to the file
        content:  Content to append

    Returns:
        Operation result
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)
        confined_fs.make_dirs(path.parent)

        old_size = path.stat().st_size if path.exists() else 0

        with confined_fs.open_file(path, "a", encoding="utf-8") as f:
            # Ensure new content starts on a fresh line
            if old_size > 0:
                with confined_fs.open_file(path, "rb") as rb:
                    rb.seek(-1, 2)
                    last_byte = rb.read(1)
                if last_byte not in (b"\n", b"\r"):
                    f.write("\n")
            f.write(content)

        new_size = path.stat().st_size
        added_bytes = new_size - old_size
        lines_added = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"✅ Appended to {visible}: {lines_added} lines, {added_bytes} bytes"

    except Exception as exc:
        return f"❌ Append error: {exc}"


@function_tool
def file_edit(
    filepath: str,
    patch_content: str,
) -> str:
    """
    Edits a file via a unified diff patch.

    Patch format:
        --- a/filename
        +++ b/filename
        @@ -start,count +start,count @@
         context line
        -removed line
        +added line

    A hunk header may also be a bare "@@" (or "@@ <anchor line>"): the hunk is
    then located by its context lines, which must match exactly one place.
    The "*** Begin Patch / *** Update File / *** End Patch" envelope is accepted.

    Args:
        filepath:      Path to the file
        patch_content: Patch in unified diff format

    Returns:
        Operation result
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)

        if not path.exists():
            return f"❌ File not found: {visible}"
        if not path.is_file():
            return f"❌ Not a file: {visible}"

        original = confined_fs.read_text(path, errors="replace")
        updated = _apply_unified_patch(original, patch_content)
        confined_fs.write_text(path, updated)

        original_lines, _ = _split_file_content(original)
        result_lines, _ = _split_file_content(updated)
        diff = len(result_lines) - len(original_lines)
        sign = f"{diff:+d}" if diff != 0 else "±0"
        return f"✅ File updated: {visible} ({len(result_lines)} lines, {sign})"

    except _PatchApplyError as exc:
        return str(exc)
    except Exception as exc:
        return f"❌ Edit error: {exc}"


# Where these tools act (utils.tool_isolation): in the server, inside the run's workspace
from utils.tool_isolation import WORKSPACE as _WORKSPACE  # noqa: E402

TOOL_ISOLATION = {
    "file_read": _WORKSPACE,
    "file_write": _WORKSPACE,
    "file_append": _WORKSPACE,
    "file_edit": _WORKSPACE,
}

# What these tools do (utils.tool_effects), for the action policy
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {
    "file_read": _effects.read("filepath"),
    "file_write": _effects.write("filepath"),
    "file_append": _effects.write("filepath"),
    "file_edit": _effects.write("filepath"),
}
