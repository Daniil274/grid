"""
File Tools — чтение, запись, редактирование и добавление файлов.
Все пути изолированы в рабочей директории агента через resolve_agent_path_auto().
"""

import os
import re
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils.path_utils import resolve_agent_path_auto, display_agent_path_auto


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_OUTPUT_CHARS = 10_000


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
        original_lines = original.split("\n")
        result_lines = original_lines.copy()

        patch_lines = patch_content.split("\n")
        i = 0
        current_offset = 0

        while i < len(patch_lines):
            line = patch_lines[i]

            if line.startswith("---") or line.startswith("+++"):
                i += 1
                continue

            if line.startswith("@@"):
                m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
                if not m:
                    return f"❌ Некорректный заголовок блока: {line}"

                old_start = int(m.group(1)) - 1
                old_count = int(m.group(2)) if m.group(2) else 1
                i += 1

                adjusted = old_start + current_offset
                old_idx = adjusted
                new_block = []

                while i < len(patch_lines):
                    pl = patch_lines[i]
                    if pl.startswith("@@") or pl.startswith("---") or pl.startswith("+++"):
                        break
                    if pl.startswith(" "):
                        new_block.append(pl[1:])
                        old_idx += 1
                    elif pl.startswith("-"):
                        if old_idx < len(result_lines):
                            expected = result_lines[old_idx]
                            actual = pl[1:]
                            if expected != actual:
                                return (
                                    f"❌ Несоответствие контекста на строке "
                                    f"{old_start + (old_idx - adjusted) + 1}: "
                                    f"ожидалось '{expected}', найдено '{actual}'"
                                )
                        old_idx += 1
                    elif pl.startswith("+"):
                        new_block.append(pl[1:])
                    elif pl == "\\ No newline at end of file":
                        pass
                    i += 1

                end_del = adjusted + old_count
                result_lines = result_lines[:adjusted] + new_block + result_lines[end_del:]
                current_offset += len(new_block) - old_count
            else:
                i += 1

        path.write_text("\n".join(result_lines), encoding="utf-8")

        diff = len(result_lines) - len(original_lines)
        sign = f"{diff:+d}" if diff != 0 else "±0"
        return f"✅ Файл обновлён: {visible} ({len(result_lines)} строк, {sign})"

    except Exception as exc:
        return f"❌ Ошибка редактирования: {exc}"
