"""
Vision Tools for Grid agents - просмотр и анализ изображений.
"""
import logging
from pathlib import Path
from typing import Any, List, Union
import base64

from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText

logger = logging.getLogger("tools.vision")


def _image_path_to_data_url(image_path: str) -> str:
    """
    Конвертировать путь к изображению в data URL (base64).

    Args:
        image_path: Путь к файлу изображения (абсолютный или относительный от cwd)

    Returns:
        data:image/jpeg;base64,... URL строка
    """
    try:
        img_file = Path(image_path)

        # Если путь относительный, попытаться разрешить от текущей рабочей директории
        if not img_file.is_absolute():
            img_file = Path.cwd() / img_file

        # Разрешить символические ссылки и относительные компоненты (..)
        img_file = img_file.resolve()

        if not img_file.exists():
            raise FileNotFoundError(f"Image file not found: {image_path} (resolved to: {img_file})")

        # Определить MIME тип по расширению
        ext = img_file.suffix.lower()
        mime_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        mime_type = mime_types.get(ext, 'image/jpeg')

        # Прочитать и закодировать в base64
        with open(img_file, 'rb') as f:
            img_data = f.read()

        b64_data = base64.b64encode(img_data).decode('utf-8')
        return f"data:{mime_type};base64,{b64_data}"

    except Exception as e:
        logger.error(f"Failed to convert image to data URL: {e}")
        raise


async def _inject_image_for_analysis(
    ctx: RunContextWrapper[Any],
    image_path: str,
    question: str,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Загрузка изображения и возврат как ToolOutputImage для анализа агентом.
    VisionChatCompletionsModel доставляет его в API как image_url в tool result.
    """
    logger.info(f"Processing image: {image_path}")

    file_ext = Path(image_path).suffix.lower()
    if file_ext == '.pdf':
        return [ToolOutputText(
            text=f"❌ ОШИБКА: view_image не поддерживает PDF файлы!\n\n"
                 f"Для анализа PDF используй инструмент pdf:\n"
                 f'pdf(ctx, "{image_path}", pages="1:1")'
        )]

    try:
        data_url = _image_path_to_data_url(image_path)
    except Exception as e:
        return [ToolOutputText(text=f"❌ Ошибка при загрузке изображения: {str(e)}")]

    logger.info(f"✅ Returning ToolOutputImage for {image_path}")
    return [
        ToolOutputText(text=f"📷 Изображение: {image_path}\n\nВопрос: {question}"),
        ToolOutputImage(image_url=data_url, detail="high"),
    ]


@function_tool
async def view_image(
    ctx: RunContextWrapper[Any],
    image_path: str,
    question: str = "Опиши подробно что изображено на этой картинке"
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Просмотр и анализ изображения. Изображение будет показано агенту для анализа.

    ВАЖНО: Этот инструмент ТОЛЬКО для файлов изображений (jpg, png, gif, webp, bmp).
    Для PDF используй инструмент pdf!

    Args:
        image_path: Абсолютный путь к файлу изображения (ТОЛЬКО jpg, png, gif, webp, bmp, НЕ PDF!)
        question: Вопрос или инструкция для анализа изображения (по умолчанию: "Опиши подробно что изображено на этой картинке")

    Returns:
        Список с результатом обработки изображения

    Example:
        view_image(ctx, "/path/to/image.jpg", "Что изображено на картинке?")

    НЕ ИСПОЛЬЗУЙ для PDF! Для PDF используй pdf(ctx, "file.pdf", pages="1:1")
        view_image(ctx, "/path/to/screenshot.png", "Найди все элементы UI на скриншоте")
    """
    try:
        return await _inject_image_for_analysis(ctx, image_path, question)
    except Exception as e:
        logger.error(f"Error in view_image tool: {e}", exc_info=True)
        return [ToolOutputText(text=f"❌ Ошибка при обработке изображения: {str(e)}")]


@function_tool
async def analyze_screenshot(
    ctx: RunContextWrapper[Any],
    screenshot_path: str
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Анализ скриншота UI/интерфейса. Специализированная версия view_image для скриншотов.

    Args:
        screenshot_path: Абсолютный путь к скриншоту

    Returns:
        Список с результатом анализа скриншота

    Example:
        analyze_screenshot(ctx, "/path/to/screenshot.png")
    """
    return await _inject_image_for_analysis(
        ctx,
        screenshot_path,
        "Проанализируй этот скриншот: определи тип интерфейса, найди все элементы UI (кнопки, поля, меню), опиши что отображается на экране."
    )


# Экспорт инструментов
VISION_TOOLS = {
    "view_image": view_image,
    "analyze_screenshot": analyze_screenshot,
}
