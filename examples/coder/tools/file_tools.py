"""
File Tools — reading, writing, editing, and appending files.
All paths are isolated in the agent's working directory via resolve_agent_path_auto().
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
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

    @property
    def old_span(self) -> int:
        return sum(1 for kind, _ in self.lines if kind in (" ", "-"))


class _PatchApplyError(Exception):
    """Raised when a unified diff cannot be applied safely."""


def _split_file_content(text: str) -> tuple[list[str], bool]:
    """Return file lines without terminators and whether the file ends with LF."""
    return text.splitlines(), text.endswith("\n")


def _parse_unified_diff(patch_content: str) -> list[_Hunk]:
    """Parse unified diff content into hunk objects."""
    patch_lines = patch_content.splitlines()
    hunks: list[_Hunk] = []
    i = 0

    while i < len(patch_lines):
        line = patch_lines[i]
        if line.startswith("---") or line.startswith("+++"):
            i += 1
            continue

        if not line.startswith("@@"):
            if not line.strip():
                i += 1
                continue
            raise _PatchApplyError(f"❌ Expected hunk header, found: {line}")

        match = _HUNK_HEADER_RE.match(line)
        if not match:
            raise _PatchApplyError(f"❌ Invalid hunk header: {line}")

        old_start = int(match.group(1)) - 1
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3)) - 1
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        i += 1

        hunk_lines: list[tuple[str, str]] = []
        while i < len(patch_lines):
            current = patch_lines[i]
            if current.startswith("@@") or current.startswith("---") or current.startswith("+++"):
                break
            if current == r"\ No newline at end of file":
                i += 1
                continue
            if not current:
                raise _PatchApplyError("❌ Empty line in hunk without ' ', '+' or '-' prefix")

            prefix = current[0]
            if prefix not in (" ", "+", "-"):
                raise _PatchApplyError(f"❌ Invalid patch line: {current}")
            hunk_lines.append((prefix, current[1:]))
            i += 1

        hunks.append(
            _Hunk(
                old_start=max(0, old_start),
                old_count=old_count,
                new_start=max(0, new_start),
                new_count=new_count,
                lines=hunk_lines,
            )
        )

    if not hunks:
        raise _PatchApplyError("❌ Patch contains no hunk blocks")

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


def _apply_unified_patch(original: str, patch_content: str) -> str:
    """Apply unified diff content to the original text."""
    hunks = _parse_unified_diff(patch_content)
    result_lines, had_trailing_newline = _split_file_content(original)
    current_offset = 0

    for hunk in hunks:
        expected_start = hunk.old_start + current_offset
        start = _find_hunk_start(result_lines, hunk, expected_start)
        replacement = [content for kind, content in hunk.lines if kind in (" ", "+")]
        old_span = hunk.old_span

        result_lines = result_lines[:start] + replacement + result_lines[start + old_span:]
        current_offset += len(replacement) - old_span

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

        content = path.read_text(encoding="utf-8", errors="replace")
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

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

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
        path.parent.mkdir(parents=True, exist_ok=True)

        old_size = path.stat().st_size if path.exists() else 0

        with path.open("a", encoding="utf-8") as f:
            # Ensure new content starts on a fresh line
            if old_size > 0:
                with path.open("rb") as rb:
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

        original = path.read_text(encoding="utf-8", errors="replace")
        updated = _apply_unified_patch(original, patch_content)
        path.write_text(updated, encoding="utf-8")

        original_lines, _ = _split_file_content(original)
        result_lines, _ = _split_file_content(updated)
        diff = len(result_lines) - len(original_lines)
        sign = f"{diff:+d}" if diff != 0 else "±0"
        return f"✅ File updated: {visible} ({len(result_lines)} lines, {sign})"

    except _PatchApplyError as exc:
        return str(exc)
    except Exception as exc:
        return f"❌ Edit error: {exc}"
