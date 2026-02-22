"""
Vision Tools for Grid agents - просмотр и анализ изображений.
"""
import io
import logging
from pathlib import Path
from typing import Any, List, Optional, Union
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
    Показывает изображение агенту для анализа. Только для jpg, png, gif, webp, bmp — не для PDF.

    Args:
        image_path: Абсолютный путь к файлу изображения
        question: Что нужно найти или описать на изображении
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


@function_tool
async def crop_image(
    ctx: RunContextWrapper[Any],
    left: int,
    top: int,
    right: int,
    bottom: int,
    image_path: Optional[str] = None,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Вырезает фрагмент изображения по пиксельным координатам для детального zoom-in.

    Координаты в пикселях (берутся из размера скриншота, который возвращает take_screenshot).

    Args:
        left: X левого края (пиксели)
        top: Y верхнего края (пиксели)
        right: X правого края (пиксели)
        bottom: Y нижнего края (пиксели)
        image_path: Путь к изображению (None = последний скриншот)
    """
    try:
        from PIL import Image
    except ImportError:
        return [ToolOutputText(text="❌ Pillow не установлен. Установи: pip install Pillow")]

    # Определяем источник изображения
    if image_path is None:
        wd = Path(ctx.context.factory.config.get_working_directory())
        img_file = wd / "screenshots" / "last_screenshot.png"
        if not img_file.exists():
            return [ToolOutputText(
                text="❌ Нет последнего скриншота. Сначала вызови take_screenshot()."
            )]
    else:
        img_file = Path(image_path)
        if not img_file.is_absolute():
            img_file = Path.cwd() / img_file
        img_file = img_file.resolve()
        if not img_file.exists():
            return [ToolOutputText(text=f"❌ Файл не найден: {image_path}")]

    try:
        with Image.open(img_file) as img:
            w, h = img.size

            if left >= right or top >= bottom:
                return [ToolOutputText(
                    text=f"❌ Некорректные координаты: left={left}, top={top}, right={right}, bottom={bottom}."
                )]

            cropped = img.crop((left, top, right, bottom))
            crop_w, crop_h = cropped.size

            # Конвертируем в JPEG bytes
            output = io.BytesIO()
            rgb = cropped.convert("RGB") if cropped.mode not in ("RGB", "L") else cropped
            rgb.save(output, format="JPEG", quality=90)
            img_bytes = output.getvalue()

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"

        logger.info(f"✂️ Crop {left},{top}→{right},{bottom} ({crop_w}x{crop_h}px) from {img_file.name}")
        return [
            ToolOutputText(text=f"✂️ Crop {left},{top}→{right},{bottom} ({crop_w}x{crop_h}px)"),
            ToolOutputImage(image_url=data_url, detail="high"),
        ]

    except Exception as e:
        logger.error(f"Error in crop_image: {e}", exc_info=True)
        return [ToolOutputText(text=f"❌ Ошибка при кропе: {e}")]


# Экспорт инструментов
VISION_TOOLS = {
    "view_image": view_image,
    "analyze_screenshot": analyze_screenshot,
    "crop_image": crop_image,
}
