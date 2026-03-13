"""
PDF/OCR tools для Grid agents.

- `pdf` рендерит страницы PDF в изображения и сохраняет их в рабочую директорию агента.
- `pdf-ocr` / `pdf_to_markdown` выполняют OCR через DeepSeek-OCR-2.
"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Union

# Import from agents SDK
from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from utils.path_utils import display_agent_path_from_ctx, resolve_agent_path_from_ctx
from .vision_tools import _image_path_to_data_url

logger = logging.getLogger("tools.ocr")

DEEPSEEK_OCR_DIR = Path(__file__).parent / "DeepSeek-OCR-2"
WRAPPER_SCRIPT = DEEPSEEK_OCR_DIR / "ocr_wrapper.py"
PDF_RENDER_DPI = 180
MAX_PDF_PAGES_PER_CALL = 5


def get_python_executable() -> str:
    """Находит python executable для DeepSeek-OCR."""
    venv_python = DEEPSEEK_OCR_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)

    venv_python_linux = DEEPSEEK_OCR_DIR / ".venv" / "bin" / "python"
    if venv_python_linux.exists():
        return str(venv_python_linux)

    return sys.executable


def get_user_id_from_context(ctx: RunContextWrapper[Any]) -> str:
    """Извлекает user_id из контекста агента."""
    # Отладка: логируем структуру контекста
    logger.debug(f"Context type: {type(ctx)}")
    logger.debug(f"Has user_id attr: {hasattr(ctx, 'user_id')}")
    logger.debug(f"Has context attr: {hasattr(ctx, 'context')}")

    # Пытаемся получить user_id из различных мест
    if hasattr(ctx, 'user_id') and ctx.user_id:
        logger.info(f"Found user_id directly on ctx: {ctx.user_id}")
        return str(ctx.user_id)

    # Проверяем GridRunContext (новый способ)
    if hasattr(ctx, 'context'):
        logger.debug(f"Context.context type: {type(ctx.context)}")
        logger.debug(f"Context.context attrs: {dir(ctx.context)}")

        if hasattr(ctx.context, 'user_id') and ctx.context.user_id:
            logger.info(f"Found user_id in GridRunContext: {ctx.context.user_id}")
            return str(ctx.context.user_id)

        # Проверяем metadata в GridRunContext
        if hasattr(ctx.context, 'metadata') and ctx.context.metadata:
            metadata = ctx.context.metadata
            logger.debug(f"Metadata: {metadata}")
            if isinstance(metadata, dict) and 'user_id' in metadata:
                logger.info(f"Found user_id in metadata: {metadata['user_id']}")
                return str(metadata['user_id'])

    # Fallback
    logger.warning("user_id not found in context, using 'default'")
    return "default"


def get_workspace_root() -> Path:
    """Возвращает корневую директорию workspace."""
    # Проверяем переменные окружения
    workspace_env = os.environ.get("WORKSPACE_ROOT")
    if workspace_env:
        return Path(workspace_env).resolve()

    # Fallback на корень проекта
    return Path(__file__).parent.parent


def _parse_pages_range(pages: str) -> tuple[int, int]:
    """Парсит диапазон страниц вида start:end."""
    raw_pages = (pages or "").strip()
    if ":" not in raw_pages:
        raise ValueError('pages должен быть в формате "start:end"')

    start_raw, end_raw = raw_pages.split(":", 1)
    start = int(start_raw)
    end = int(end_raw)

    if start < 1 or end < 1:
        raise ValueError("Номера страниц должны быть >= 1")
    if end < start:
        raise ValueError("Конечная страница должна быть не меньше начальной")
    if (end - start + 1) > MAX_PDF_PAGES_PER_CALL:
        raise ValueError(f"За один вызов можно запросить не более {MAX_PDF_PAGES_PER_CALL} страниц")

    return start, end


def _get_pdf_page_count(pdf_path: str) -> int:
    """Возвращает количество страниц PDF через pdfinfo."""
    result = subprocess.run(
        ["pdfinfo", pdf_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Не удалось получить информацию о PDF: {error_msg}")

    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            _, value = line.split(":", 1)
            return int(value.strip())

    raise RuntimeError("pdfinfo не вернул число страниц")


def _get_output_root(ctx: RunContextWrapper[Any], pdf_file: Path) -> Path:
    """Папка для вывода изображений страниц PDF."""
    working_dir = Path(ctx.context.factory.config.get_working_directory()).resolve()
    doc_name = pdf_file.stem or "document"
    return working_dir / "pdf_output" / doc_name


def _render_single_pdf_page(pdf_path: str, page_number: int, output_path_no_ext: Path) -> None:
    """Рендерит одну страницу PDF в PNG с помощью pdftoppm."""
    cmd = [
        "pdftoppm",
        "-f", str(page_number),
        "-l", str(page_number),
        "-r", str(PDF_RENDER_DPI),
        "-png",
        "-singlefile",
        pdf_path,
        str(output_path_no_ext),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Не удалось отрендерить страницу {page_number}: {error_msg}")


async def _run_pdf_ocr(
    ctx: RunContextWrapper[Any],
    pdf_path: str,
    pages: str,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """Общая реализация OCR для PDF."""
    python_exe = get_python_executable()
    visible_pdf_path = display_agent_path_from_ctx(pdf_path, ctx)

    if not WRAPPER_SCRIPT.exists():
        return [ToolOutputText(text="❌ OCR wrapper not found")]

    user_id = get_user_id_from_context(ctx)
    workspace_root = get_workspace_root()

    logger.info(f"OCR tool - user_id: {user_id}, workspace: {workspace_root}")

    try:
        resolved_pdf_path = resolve_agent_path_from_ctx(pdf_path, ctx)
        cmd = [
            python_exe,
            str(WRAPPER_SCRIPT),
            "--pdf", resolved_pdf_path,
            "--pages", pages,
            "--quality", "12gb",
            "--user-id", user_id,
            "--workspace", str(workspace_root),
        ]

        logger.info(f"Running OCR wrapper for user {user_id}: {visible_pdf_path} pages {pages}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"OCR wrapper failed: {error_msg}")
            return [ToolOutputText(text=f"❌ OCR processing failed: {error_msg}")]

        try:
            if not result.stdout or not result.stdout.strip():
                logger.error("OCR wrapper returned empty output")
                stderr_preview = result.stderr[:500] if result.stderr else "No stderr"
                return [ToolOutputText(text=f"❌ OCR wrapper returned empty output.\nStderr: {stderr_preview}")]

            output_data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OCR output: {result.stdout[:200]}")
            return [ToolOutputText(text=f"❌ Invalid OCR output format: {str(e)}")]

        if isinstance(output_data, dict) and "error" in output_data:
            return [ToolOutputText(text=f"❌ OCR Error: {output_data['error']}")]

        final_output = []
        if isinstance(output_data, list):
            for item in output_data:
                if item.get("type") == "text":
                    final_output.append(ToolOutputText(text=item.get("text", "")))
                elif item.get("type") == "image_url":
                    img_data = item.get("image_url", {})
                    url = img_data.get("url", "")
                    detail = img_data.get("detail", "auto")
                    if url:
                        final_output.append(ToolOutputImage(
                            image_url=url,
                            detail=detail if detail in ["low", "high", "auto"] else "auto"
                        ))

        if not final_output:
            return [ToolOutputText(text="⚠️ No content extracted from PDF")]

        return final_output

    except Exception as e:
        logger.error(f"OCR tool error: {e}", exc_info=True)
        return [ToolOutputText(text=f"❌ Error: {str(e)}")]


@function_tool
async def pdf(
    ctx: RunContextWrapper[Any],
    pdf_path: str,
    pages: str = "1:1"
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Рендерит страницы PDF в изображения, сохраняет их в рабочую директорию агента
    и возвращает эти страницы как визуальные входы для последующего анализа.

    Args:
        pdf_path: Путь к PDF файлу
        pages: Диапазон страниц в формате "start:end" (например "1:2")

    Returns:
        Текстовая сводка и изображения страниц
    """
    visible_pdf_path = display_agent_path_from_ctx(pdf_path, ctx)

    try:
        resolved_pdf_path = resolve_agent_path_from_ctx(pdf_path, ctx)
        pdf_file = Path(resolved_pdf_path)
        if not pdf_file.exists():
            return [ToolOutputText(text=f"❌ Файл не найден: {visible_pdf_path}")]

        start_page, end_page = _parse_pages_range(pages)
        total_pages = _get_pdf_page_count(resolved_pdf_path)
        if end_page > total_pages:
            return [ToolOutputText(
                text=f"❌ В PDF только {total_pages} стр., запрошен диапазон {pages}"
            )]

        output_root = _get_output_root(ctx, pdf_file)
        output_root.mkdir(parents=True, exist_ok=True)

        saved_paths: list[Path] = []
        for page_number in range(start_page, end_page + 1):
            page_dir = output_root / f"page_{page_number}"
            page_dir.mkdir(parents=True, exist_ok=True)
            image_path = page_dir / "page.png"
            _render_single_pdf_page(resolved_pdf_path, page_number, page_dir / "page")
            if not image_path.exists():
                raise RuntimeError(f"Файл страницы не создан: {image_path}")
            saved_paths.append(image_path)

        visible_output_root = display_agent_path_from_ctx(str(output_root), ctx)
        visible_saved_paths = [
            display_agent_path_from_ctx(str(path), ctx)
            for path in saved_paths
        ]

        result_blocks: List[Union[ToolOutputText, ToolOutputImage]] = [
            ToolOutputText(
                text=(
                    f"PDF страницы сохранены из `{visible_pdf_path}` в `{visible_output_root}`.\n"
                    f"Диапазон: {start_page}:{end_page} из {total_pages}\n"
                    f"Файлы:\n- " + "\n- ".join(visible_saved_paths)
                )
            )
        ]

        for page_number, image_path in zip(range(start_page, end_page + 1), saved_paths):
            result_blocks.append(ToolOutputText(text=f"Страница {page_number}"))
            result_blocks.append(
                ToolOutputImage(
                    image_url=_image_path_to_data_url(str(image_path)),
                    detail="high",
                )
            )

        return result_blocks

    except Exception as e:
        logger.error(f"PDF render tool error: {e}", exc_info=True)
        return [ToolOutputText(text=f"❌ Error: {str(e)}")]


@function_tool(name_override="pdf-ocr")
async def pdf_ocr(
    ctx: RunContextWrapper[Any],
    pdf_path: str,
    pages: str = "1:1"
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Выполняет OCR для PDF через DeepSeek-OCR-2 и возвращает структурированный текст/изображения.

    Args:
        pdf_path: Путь к PDF файлу
        pages: Диапазон страниц в формате "start:end"
    """
    return await _run_pdf_ocr(ctx, pdf_path, pages)


@function_tool(name_override="pdf_to_markdown")
async def pdf_to_markdown(
    ctx: RunContextWrapper[Any],
    pdf_path: str,
    pages: str = "1:1"
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Обратносуместимое имя для OCR-инструмента PDF.
    """
    return await _run_pdf_ocr(ctx, pdf_path, pages)


# Экспортируемые инструменты
OCR_TOOLS = {
    "pdf": pdf,
    "pdf-ocr": pdf_ocr,
    "pdf_to_markdown": pdf_to_markdown,
    "pdf_ocr": pdf_ocr,
}
