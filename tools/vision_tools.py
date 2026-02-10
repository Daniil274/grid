"""
Vision Tools for Grid agents - просмотр и анализ изображений.
"""
import logging
from pathlib import Path
from typing import Any, List, Union
import base64

from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from schemas import ImageContent, ImageUrl, TextContent

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
        logger.info(f"Processing image: {image_path}")

        # Проверка расширения файла - отклоняем PDF
        file_ext = Path(image_path).suffix.lower()
        if file_ext == '.pdf':
            return [ToolOutputText(
                text=f"❌ ОШИБКА: view_image не поддерживает PDF файлы!\n\n"
                     f"Для анализа PDF используй инструмент pdf:\n"
                     f'pdf(ctx, "{image_path}", pages="1:1")'
            )]

        # Конвертировать изображение в data URL
        try:
            data_url = _image_path_to_data_url(image_path)
        except Exception as e:
            return [ToolOutputText(text=f"❌ Ошибка при загрузке изображения: {str(e)}")]

        # Создать ToolOutputImage для отображения в результате инструмента
        img_output = ToolOutputImage(image_url=data_url, detail="high")

        # ✅ ИЗОЛЯЦИЯ КОНТЕКСТА: Инжектируем изображение в ЛОКАЛЬНУЮ сессию агента!
        # Это позволяет vision-модели видеть изображение в следующем turn'е.
        # Изображение НЕ утечет в родительского агента, т.к. каждый агент имеет свою сессию.
        try:
            session = getattr(ctx.context, 'session', None)
            if session:
                # ✅ Используем правильный формат Agents SDK для multimodal контента
                # SDK ожидает: type="input_text" и type="input_image"
                # НЕ старый OpenAI Vision API формат (type="text", type="image_url")
                user_message = {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[System: Результат инструмента view_image]\n\nВопрос: {question}"
                        },
                        {
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": "high"
                        }
                    ]
                }
                logger.info(f"📝 Adding user message to session (SDK format): {user_message}")
                await session.add_items([user_message])

                # Проверяем что добавилось
                session_items = await session.get_items()
                logger.info(f"✅ Session now has {len(session_items)} items")
                logger.info(f"   Last item keys: {list(session_items[-1].keys()) if session_items else 'EMPTY'}")
                logger.info(f"✅ Injected image into LOCAL agent session (isolated from parent)")

                # Установить флаг перезапуска для ЛОКАЛЬНОГО агента
                if hasattr(ctx.context, 'should_restart'):
                    ctx.context.should_restart = True
                    logger.info("✅ Set should_restart flag for LOCAL agent")

                # Вернуть заглушку - реальный ответ будет после перезапуска ЛОКАЛЬНОГО агента
                return [ToolOutputText(
                    text="[System: Изображение загружено в локальную сессию агента для анализа. Перезапуск...]"
                )]
            else:
                logger.warning("⚠️ No session available in context - returning image as tool output")

        except Exception as e:
            logger.warning(f"⚠️ Failed to inject image into local session: {e}")

        # Fallback: если сессия недоступна, вернуть изображение как результат инструмента
        return [
            ToolOutputText(text=f"📷 Изображение: {image_path}\n\nВопрос: {question}"),
            img_output
        ]

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
    return await view_image(
        ctx,
        screenshot_path,
        "Проанализируй этот скриншот: определи тип интерфейса, найди все элементы UI (кнопки, поля, меню), опиши что отображается на экране."
    )


# Экспорт инструментов
VISION_TOOLS = {
    "view_image": view_image,
    "analyze_screenshot": analyze_screenshot,
}
