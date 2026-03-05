"""
Tools for reading Markdown files with images for Grid agents.
"""
import logging
import re
import os
from pathlib import Path
from typing import List, Union, Any, Optional

from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from utils.image_utils import ImageUtils
from utils.path_utils import resolve_agent_path_from_ctx

logger = logging.getLogger("tools.markdown")

@function_tool
async def read_markdown(
    ctx: RunContextWrapper[Any],
    file_path: str,
    start_char: int = 0,
    max_chars: int = 50000
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Read a Markdown file and return structured content with images.
    Images in the markdown (e.g. ![alt](path)) are extracted and provided as visual inputs to the agent.

    ВАЖНО ДЛЯ БОЛЬШИХ ДОКУМЕНТОВ:
    - Для документов > 50000 символов используй start_char и max_chars для постепенного чтения
    - Сначала прочитай начало (0-50000) чтобы найти содержание/оглавление
    - Затем читай нужные разделы по диапазонам символов
    - Не пытайся прочитать весь большой документ за раз!

    Args:
        file_path: Path to the markdown file.
        start_char: Starting character position (default: 0).
        max_chars: Maximum number of characters to read (default: 50000, ~10-15 pages).

    Example:
        read_markdown(ctx, "large_doc.md", start_char=0, max_chars=50000)  # Первая часть
        read_markdown(ctx, "large_doc.md", start_char=50000, max_chars=50000)  # Вторая часть
    """
    try:
        abs_path = Path(resolve_agent_path_from_ctx(file_path, ctx))

        if not abs_path.exists():
            return [ToolOutputText(text=f"File not found: {file_path}")]

        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                full_content = f.read()
                total_chars = len(full_content)

                end_char = min(start_char + max_chars, total_chars)
                content = full_content[start_char:end_char]

                if start_char > 0 or end_char < total_chars:
                    pagination_info = f"\n[Документ: символы {start_char}-{end_char} из {total_chars}]\n\n"
                    content = pagination_info + content

        except Exception as e:
            return [ToolOutputText(text=f"Error reading file: {e}")]

        # Parse Markdown for images
        pattern = re.compile(r'!\[([^\]]*)\]\(([^)"]+)(?:\s+"[^"]+")?\)')

        last_pos = 0
        final_output = []
        base_dir = abs_path.parent

        for match in pattern.finditer(content):
            start, end = match.span()

            if start > last_pos:
                text_segment = content[last_pos:start]
                if text_segment.strip():
                    final_output.append(ToolOutputText(text=text_segment))

            alt_text = match.group(1)
            image_url = match.group(2).strip()

            resolved_url = image_url
            is_local = False

            if not (image_url.startswith('http://') or image_url.startswith('https://')):
                is_local = True
                if os.path.isabs(image_url):
                    img_path = Path(image_url)
                else:
                    img_path = base_dir / image_url

                if img_path.exists():
                    data_url = ImageUtils.file_to_base64(str(img_path), resize=True)
                    if data_url:
                        resolved_url = data_url
                    else:
                        logger.warning(f"Failed to encode image to base64: {img_path}")
                        final_output.append(ToolOutputText(text=f"[Image encode failed: {image_url}]"))
                        last_pos = end
                        continue
                else:
                    logger.warning(f"Markdown image not found: {img_path}")
                    final_output.append(ToolOutputText(text=f"[Image not found: {image_url}]"))
                    last_pos = end
                    continue

            final_output.append(ToolOutputImage(
                image_url=resolved_url,
                detail="auto"
            ))

            last_pos = end

        if last_pos < len(content):
            text_segment = content[last_pos:]
            if text_segment.strip():
                final_output.append(ToolOutputText(text=text_segment))

        if not final_output:
            return [ToolOutputText(text="File is empty.")]

        return final_output

    except Exception as e:
        logger.error(f"Error executing read_markdown tool: {e}", exc_info=True)
        return [ToolOutputText(text=f"Error reading markdown: {str(e)}")]

MARKDOWN_TOOLS = {
    "read_markdown": read_markdown,
    "read_md": read_markdown # alias
}
