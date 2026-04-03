"""
Notebook Tool — чтение, редактирование и создание Jupyter notebooks.
Все пути ограничены рабочей директорией агента.
"""

import json
import logging
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils.path_utils import resolve_agent_path_auto, display_agent_path_auto

logger = logging.getLogger(__name__)

try:
    import nbformat
    HAS_NBFORMAT = True
except ImportError:
    HAS_NBFORMAT = False


def _resolve(filepath: str) -> tuple:
    """Returns (visible, resolved_str). Raises ValueError on sandbox escape."""
    visible = display_agent_path_auto(filepath)
    resolved = resolve_agent_path_auto(filepath)
    return visible, resolved


def _check_ipynb(path: Path, visible: str) -> Optional[str]:
    """Returns error string if path is not a valid .ipynb file."""
    if not path.exists():
        return f"❌ Файл не найден: {visible}"
    if path.suffix != ".ipynb":
        return f"❌ Ожидается файл .ipynb: {visible}"
    if not path.is_file():
        return f"❌ Не является файлом: {visible}"
    return None


# ---------------------------------------------------------------------------
# notebook_read
# ---------------------------------------------------------------------------

@function_tool
def notebook_read(
    filepath: str,
    include_output: bool = False,
) -> str:
    """
    Читает Jupyter notebook (.ipynb).

    Args:
        filepath:       Путь к .ipynb файлу
        include_output: Включать вывод ячеек

    Returns:
        Структура notebook с ячейками
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    path = Path(resolved)
    err = _check_ipynb(path, visible)
    if err:
        return err

    try:
        # Try nbformat first
        if HAS_NBFORMAT:
            try:
                nb = nbformat.read(path, as_version=4)
                return _format_nb_nbformat(nb, visible, include_output)
            except Exception as exc:
                logger.warning(f"nbformat.read failed for {visible}: {exc}. Falling back to JSON.")

        # JSON fallback
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
        return _format_nb_json(nb, visible, include_output)

    except Exception as exc:
        return f"❌ Ошибка чтения notebook: {exc}"


def _format_nb_nbformat(nb, visible: str, include_output: bool) -> str:
    lines = [
        f"📓 {visible}",
        f"Формат: {nb.nbformat}.{nb.nbformat_minor}",
        "",
    ]
    kernel = nb.metadata.get("kernelspec", {}).get("display_name", "")
    if kernel:
        lines += [f"Kernel: {kernel}", ""]
    lines.append(f"Всего ячеек: {len(nb.cells)}\n")

    for i, cell in enumerate(nb.cells, 1):
        lines.append(f"## Cell {i} [{cell.cell_type}]")
        source = cell.source
        if len(source) > 500:
            source = source[:500] + "..."
        if source:
            lang = "python" if cell.cell_type == "code" else ""
            lines += [f"```{lang}", source, "```"]

        if include_output and cell.cell_type == "code" and cell.outputs:
            lines.append("\nOutput:")
            for out in cell.outputs[:3]:
                otype = out.get("output_type", "")
                if otype == "stream":
                    text = out.get("text", "")
                    if len(text) > 300:
                        text = text[:300] + "..."
                    lines.append(f"[{out.get('name', 'stdout')}]\n{text}")
                elif otype in ("execute_result", "display_data"):
                    data = out.get("data", {})
                    plain = data.get("text/plain", "")
                    if isinstance(plain, list):
                        plain = "".join(plain)
                    if len(plain) > 300:
                        plain = plain[:300] + "..."
                    lines.append(plain)
        lines.append("")

    return "\n".join(lines)


def _format_nb_json(nb: dict, visible: str, include_output: bool) -> str:
    cells = nb.get("cells", [])
    metadata = nb.get("metadata", {})
    lines = [
        f"📓 {visible}",
        f"Формат: {nb.get('nbformat', 4)}.{nb.get('nbformat_minor', 2)}",
        "",
    ]
    kernel = metadata.get("kernelspec", {}).get("display_name", "")
    if kernel:
        lines += [f"Kernel: {kernel}", ""]
    lines.append(f"Всего ячеек: {len(cells)}\n")

    for i, cell in enumerate(cells, 1):
        cell_type = cell.get("cell_type", "unknown")
        source = cell.get("source", [])
        if isinstance(source, list):
            source = "".join(source)
        if len(source) > 500:
            source = source[:500] + "..."

        lines.append(f"## Cell {i} [{cell_type}]")
        if source:
            lang = "python" if cell_type == "code" else ""
            lines += [f"```{lang}", source, "```"]

        if include_output and cell_type == "code":
            outputs = cell.get("outputs", [])
            if outputs:
                lines.append(f"\nOutput ({len(outputs)} items)")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# notebook_edit
# ---------------------------------------------------------------------------

@function_tool
def notebook_edit(
    filepath: str,
    cell_index: int,
    new_source: str,
    cell_type: Optional[str] = None,
) -> str:
    """
    Редактирует ячейку Jupyter notebook.

    Args:
        filepath:   Путь к .ipynb файлу
        cell_index: Индекс ячейки (1-based)
        new_source: Новое содержимое ячейки
        cell_type:  Тип ячейки ('code' или 'markdown', опционально)

    Returns:
        Результат операции
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    path = Path(resolved)
    err = _check_ipynb(path, visible)
    if err:
        return err

    if cell_type and cell_type not in ("code", "markdown", "raw"):
        return f"❌ Некорректный тип ячейки: {cell_type}. Используйте: code, markdown, raw"

    # Try nbformat
    if HAS_NBFORMAT:
        try:
            nb = nbformat.read(path, as_version=4)

            if cell_index < 1 or cell_index > len(nb.cells):
                return f"❌ Индекс {cell_index} вне диапазона (ячеек: {len(nb.cells)})"

            cell = nb.cells[cell_index - 1]
            cell.source = new_source
            if cell_type:
                cell.cell_type = cell_type
                if cell_type == "code" and not hasattr(cell, "outputs"):
                    cell.outputs = []
                    cell.execution_count = None

            nbformat.write(nb, path)
            return f"✅ Ячейка {cell_index} обновлена ({cell.cell_type})"

        except Exception as exc:
            logger.warning(f"nbformat edit failed for {visible}: {exc}. Falling back to JSON.")

    # JSON fallback
    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)

        cells = nb.get("cells", [])
        if cell_index < 1 or cell_index > len(cells):
            return f"❌ Индекс {cell_index} вне диапазона (ячеек: {len(cells)})"

        cell = cells[cell_index - 1]
        # nbformat spec: source must always be a list of strings
        cell["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            cell["cell_type"] = cell_type

        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=2, ensure_ascii=False)

        actual_type = cell.get("cell_type", "unknown")
        return f"✅ Ячейка {cell_index} обновлена ({actual_type})"

    except Exception as exc:
        return f"❌ Ошибка редактирования: {exc}"


# ---------------------------------------------------------------------------
# notebook_create
# ---------------------------------------------------------------------------

@function_tool
def notebook_create(
    filepath: str,
    kernel: str = "python3",
) -> str:
    """
    Создаёт новый Jupyter notebook.

    Args:
        filepath: Путь для сохранения (.ipynb)
        kernel:   Имя kernel (по умолчанию python3)

    Returns:
        Результат операции
    """
    try:
        visible, resolved = _resolve(filepath)
    except ValueError as exc:
        return str(exc)

    try:
        path = Path(resolved)
        if path.suffix != ".ipynb":
            path = path.with_suffix(".ipynb")
            visible = visible if visible.endswith(".ipynb") else visible + ".ipynb"

        if path.exists():
            return f"❌ Файл уже существует: {visible}"

        path.parent.mkdir(parents=True, exist_ok=True)

        if HAS_NBFORMAT:
            nb = nbformat.v4.new_notebook()
            nb.metadata["kernelspec"] = {
                "display_name": kernel,
                "language": "python",
                "name": kernel,
            }
            nb.cells.append(nbformat.v4.new_markdown_cell("# Новый Notebook"))
            nbformat.write(nb, path)
        else:
            nb = {
                "cells": [
                    {
                        "cell_type": "markdown",
                        "metadata": {},
                        "source": ["# Новый Notebook"],
                    }
                ],
                "metadata": {
                    "kernelspec": {
                        "display_name": kernel,
                        "language": "python",
                        "name": kernel,
                    },
                    "language_info": {
                        "name": "python",
                    },
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(nb, f, indent=2, ensure_ascii=False)

        return f"✅ Создан notebook: {visible}"

    except Exception as exc:
        return f"❌ Ошибка создания: {exc}"
