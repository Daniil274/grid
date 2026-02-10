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
        # Resolve file path
        # Try to get working directory from config if available
        working_dir = "."
        factory = getattr(ctx.context, 'factory', None)
        if factory and hasattr(factory, 'config'):
            working_dir = factory.config.get_working_directory()
        
        # Determine absolute path
        if os.path.isabs(file_path):
            abs_path = Path(file_path)
        else:
            abs_path = Path(working_dir) / file_path
            
        if not abs_path.exists():
            return [ToolOutputText(text=f"File not found: {file_path}")]
            
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                # Read full content to get total size
                full_content = f.read()
                total_chars = len(full_content)

                # Apply character range
                end_char = min(start_char + max_chars, total_chars)
                content = full_content[start_char:end_char]

                # Add metadata about pagination
                if start_char > 0 or end_char < total_chars:
                    pagination_info = f"\n[Документ: символы {start_char}-{end_char} из {total_chars}]\n\n"
                    content = pagination_info + content

        except Exception as e:
            return [ToolOutputText(text=f"Error reading file: {e}")]

        # Parse Markdown for images
        # Regex for ![alt](url "title") or ![alt](url)
        # We simplify to capture url
        pattern = re.compile(r'!\[([^\]]*)\]\(([^)"]+)(?:\s+"[^"]+")?\)')
        
        last_pos = 0
        final_output = []
        base_dir = abs_path.parent
        
        for match in pattern.finditer(content):
            start, end = match.span()
            
            # Text before image
            if start > last_pos:
                text_segment = content[last_pos:start]
                if text_segment.strip():
                    final_output.append(ToolOutputText(text=text_segment))
            
            # Image handling
            alt_text = match.group(1)
            image_url = match.group(2).strip()
            
            # Resolve image path/URL
            resolved_url = image_url
            is_local = False
            
            # Check if it's a web URL
            if not (image_url.startswith('http://') or image_url.startswith('https://')):
                # Assume local file
                is_local = True
                if os.path.isabs(image_url):
                    img_path = Path(image_url)
                else:
                    img_path = base_dir / image_url
                
                if img_path.exists():
                    # For local files, we might need to convert to absolute path or file URI
                    # The system (multimodal_converter) generally handles paths if they exist
                    # We pass the absolute path
                    resolved_url = str(img_path.absolute())
                else:
                    # Image not found, return as text
                    logger.warning(f"Markdown image not found: {img_path}")
                    final_output.append(ToolOutputText(text=f"[Image not found: {image_url}]"))
                    last_pos = end
                    continue
            
            final_output.append(ToolOutputImage(
                image_url=resolved_url,
                detail="auto"
            ))
            
            last_pos = end
            
        # Remaining text
        if last_pos < len(content):
            text_segment = content[last_pos:]
            if text_segment.strip():
                final_output.append(ToolOutputText(text=text_segment))
                
        # ✅ ИЗОЛЯЦИЯ КОНТЕКСТА: Инжектируем в ЛОКАЛЬНУЮ сессию агента!
        has_images = any(isinstance(item, ToolOutputImage) for item in final_output)

        if has_images and factory and hasattr(factory, 'context_manager'):
            try:
                session = getattr(ctx.context, 'session', None)
                if session:
                    from schemas import ImageContent, ImageUrl, TextContent

                    # ✅ Используем правильный формат Agents SDK (input_text, input_image)
                    content_list = []
                    content_list.append({
                        "type": "input_text",
                        "text": f"[System: Content of {file_path} (Text + Images)]"
                    })

                    for item in final_output:
                        if isinstance(item, ToolOutputText):
                            content_list.append({
                                "type": "input_text",
                                "text": item.text
                            })
                        elif isinstance(item, ToolOutputImage):
                            url = getattr(item, 'image_url', getattr(item, 'url', None))
                            detail = getattr(item, 'detail', 'auto')
                            if url:
                                content_list.append({
                                    "type": "input_image",
                                    "image_url": url,
                                    "detail": detail
                                })

                    if len(content_list) > 1:
                        await session.add_items([{"role": "user", "content": content_list}])
                        logger.info(f"✅ Injected multimodal content into LOCAL session ({len(content_list)} parts)")

                        if hasattr(ctx.context, 'should_restart'):
                            ctx.context.should_restart = True
                            logger.info("✅ Set should_restart flag for LOCAL agent")

                        return [ToolOutputText(text=f"[System: Content of {file_path} loaded with {len(content_list)} parts. Restarting...]")]

            except Exception as e:
                logger.warning(f"⚠️ Failed to inject into local session: {e}")

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























