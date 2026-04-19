"""
File Tools — чтение, запись, редактирование и добавление файлов.
Все пути изолированы в рабочей директории агента через resolve_agent_path_auto().
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
            raise _PatchApplyError(f"❌ Ожидался заголовок hunk, найдено: {line}")

        match = _HUNK_HEADER_RE.match(line)
        if not match:
            raise _PatchApplyError(f"❌ Некорректный заголовок блока: {line}")

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
                raise _PatchApplyError("❌ Пустая строка в hunk без префикса ' ', '+' или '-'")

            prefix = current[0]
            if prefix not in (" ", "+", "-"):
                raise _PatchApplyError(f"❌ Некорректная строка патча: {current}")
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
        raise _PatchApplyError("❌ Патч не содержит ни одного hunk-блока")

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
            f"❌ Hunk не помещается в файл около строки {expected_start + 1}. "
            f"Проверьте смещение и контекст патча."
        )
    raise _PatchApplyError(
        f"❌ Контекст патча не найден около строки {expected_start + 1}: "
        f"на строке {line_no} ожидалось '{expected}', найдено '{actual if actual is not None else '<EOF>'}'"
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
    return content[:max_chars] + f"\n\n... [обрезано {remaining} символов, используй limit_lines и offset] ..."


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
    Читает содержимое файла.

    Args:
        filepath:    Путь к файлу
        offset:      Начальная строка (0-indexed)
        limit_lines: Количество строк для чтения (None = все)

    Returns:
        Содержимое файла или его части
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)

        if not path.exists():
            return f"❌ Файл не найден: {visible}"
        if not path.is_file():
            return f"❌ Не является файлом: {visible}"

        file_size = path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            return f"⚠️ Файл слишком большой ({file_size} байт). Используй limit_lines и offset."

        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        total = len(lines)

        if offset > 0 or limit_lines is not None:
            start = max(0, offset)
            end = start + limit_lines if limit_lines is not None else total
            end = min(end, total)
            sliced = lines[start:end]
            header = f"📄 {visible} (строки {start + 1}–{end} из {total}):\n"
            content = "\n".join(sliced)
        else:
            header = f"📄 {visible} ({total} строк):\n"

        return header + "\n" + _truncate(content)

    except Exception as exc:
        return f"❌ Ошибка чтения: {exc}"


@function_tool
def file_write(
    filepath: str,
    content: str,
    overwrite: bool = False,
) -> str:
    """
    Записывает содержимое в файл.

    Args:
        filepath:  Путь к файлу
        content:   Содержимое
        overwrite: Разрешить перезапись существующего файла

    Returns:
        Результат операции
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)
        existed_before = path.exists()

        if existed_before and not overwrite:
            return f"❌ Файл уже существует: {visible}. Используй overwrite=true для перезаписи."

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        lines = len(content.split("\n"))
        size = path.stat().st_size
        verb = "перезаписан" if existed_before else "создан"
        emoji = "📝" if existed_before else "✅"
        return f"{emoji} Файл {verb}: {visible} ({lines} строк, {size} байт)"

    except Exception as exc:
        return f"❌ Ошибка записи: {exc}"


@function_tool
def file_append(
    filepath: str,
    content: str,
) -> str:
    """
    Добавляет содержимое в конец файла.
    Автоматически вставляет перевод строки, если файл не заканчивается на него.

    Args:
        filepath: Путь к файлу
        content:  Содержимое для добавления

    Returns:
        Результат операции
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
        return f"✅ Добавлено в {visible}: {lines_added} строк, {added_bytes} байт"

    except Exception as exc:
        return f"❌ Ошибка добавления: {exc}"


@function_tool
def file_edit(
    filepath: str,
    patch_content: str,
) -> str:
    """
    Редактирует файл через патч в формате unified diff.

    Формат патча:
        --- a/filename
        +++ b/filename
        @@ -start,count +start,count @@
         context line
        -removed line
        +added line

    Args:
        filepath:      Путь к файлу
        patch_content: Патч в формате unified diff

    Returns:
        Результат операции
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)

        if not path.exists():
            return f"❌ Файл не найден: {visible}"
        if not path.is_file():
            return f"❌ Не является файлом: {visible}"

        original = path.read_text(encoding="utf-8", errors="replace")
        updated = _apply_unified_patch(original, patch_content)
        path.write_text(updated, encoding="utf-8")

        original_lines, _ = _split_file_content(original)
        result_lines, _ = _split_file_content(updated)
        diff = len(result_lines) - len(original_lines)
        sign = f"{diff:+d}" if diff != 0 else "±0"
        return f"✅ Файл обновлён: {visible} ({len(result_lines)} строк, {sign})"

    except _PatchApplyError as exc:
        return str(exc)
    except Exception as exc:
        return f"❌ Ошибка редактирования: {exc}"
