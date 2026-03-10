"""
OCR Tools для Grid agents - чистые технические инструменты без бизнес-логики.
Использует DeepSeek-OCR-2 через subprocess wrapper с кэшированием по пользователям.
"""
import sys
import json
import logging
import subprocess
import os
from pathlib import Path
from typing import List, Union, Dict, Any, Optional

# Import from agents SDK
from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from utils.path_utils import display_agent_path_from_ctx, resolve_agent_path_from_ctx

logger = logging.getLogger("tools.ocr")

DEEPSEEK_OCR_DIR = Path(__file__).parent / "DeepSeek-OCR-2"
WRAPPER_SCRIPT = DEEPSEEK_OCR_DIR / "ocr_wrapper.py"


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


@function_tool
async def pdf_to_markdown(
    ctx: RunContextWrapper[Any],
    pdf_path: str,
    pages: str = "1:5"
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Конвертирует PDF в структурированный Markdown с изображениями через OCR.
    Результаты кэшируются по пользователям для повторного использования.

    Args:
        pdf_path: Абсолютный путь к PDF файлу
        pages: Диапазон страниц в формате "start:end" (например "1:5" или "10:20")

    Returns:
        Список блоков контента (текст и изображения) в порядке следования
    """
    python_exe = get_python_executable()
    visible_pdf_path = display_agent_path_from_ctx(pdf_path, ctx)

    if not WRAPPER_SCRIPT.exists():
        return [ToolOutputText(text="❌ OCR wrapper not found")]

    # Получаем user_id и workspace
    user_id = get_user_id_from_context(ctx)
    workspace_root = get_workspace_root()

    # Отладка: логируем что получили
    logger.info(f"OCR tool - user_id: {user_id}, workspace: {workspace_root}")

    try:
        resolved_pdf_path = resolve_agent_path_from_ctx(pdf_path, ctx)
        # Команда для запуска wrapper
        cmd = [
            python_exe,
            str(WRAPPER_SCRIPT),
            "--pdf", resolved_pdf_path,
            "--pages", pages,
            "--quality", "12gb",
            "--user-id", user_id,
            "--workspace", str(workspace_root)
        ]

        logger.info(f"Running OCR wrapper for user {user_id}: {visible_pdf_path} pages {pages}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',  # Заменяем непонятные символы вместо падения
            check=False
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"OCR wrapper failed: {error_msg}")
            return [ToolOutputText(text=f"❌ OCR processing failed: {error_msg}")]

        # Парсим JSON output
        try:
            if not result.stdout or not result.stdout.strip():
                logger.error("OCR wrapper returned empty output")
                stderr_preview = result.stderr[:500] if result.stderr else "No stderr"
                return [ToolOutputText(text=f"❌ OCR wrapper returned empty output.\nStderr: {stderr_preview}")]

            output_data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OCR output: {result.stdout[:200]}")
            return [ToolOutputText(text=f"❌ Invalid OCR output format: {str(e)}")]

        # Проверка на ошибку
        if isinstance(output_data, dict) and "error" in output_data:
            return [ToolOutputText(text=f"❌ OCR Error: {output_data['error']}")]

        # Конвертируем в ToolOutput объекты
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


# Экспортируемые инструменты
OCR_TOOLS = {
    "pdf_to_markdown": pdf_to_markdown,
    "pdf": pdf_to_markdown  # alias
}
