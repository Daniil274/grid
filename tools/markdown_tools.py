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
def read_markdown(ctx: RunContextWrapper[Any], file_path: str) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Read a Markdown file and return structured content with images.
    Images in the markdown (e.g. ![alt](path)) are extracted and provided as visual inputs to the agent.
    
    Args:
        file_path: Path to the markdown file.
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
                content = f.read()
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
                
        # --- Injection Strategy: Persist FULL content as USER message ---
        # If output contains images, we inject EVERYTHING (text + images) as a User message
        # and force a restart to make the agent "see" the images immediately.
        has_images = any(isinstance(item, ToolOutputImage) for item in final_output)
        
        if has_images and factory and hasattr(factory, 'context_manager'):
            try:
                # Import schemas locally to avoid circular deps
                from schemas import ImageContent, ImageUrl, TextContent
                
                content_parts = []
                # Add a text prefix to explain context
                content_parts.append(TextContent(type="text", text=f"[System: Content of {file_path} (Text + Images)]"))
                
                for item in final_output:
                    if isinstance(item, ToolOutputText):
                        content_parts.append(TextContent(type="text", text=item.text))
                    elif isinstance(item, ToolOutputImage):
                        url = getattr(item, 'image_url', getattr(item, 'url', None))
                        detail = getattr(item, 'detail', 'auto')
                        if url:
                            content_parts.append(ImageContent(
                                type="image_url",
                                image_url=ImageUrl(url=url, detail=detail)
                            ))
                
                # Inject as USER message
                if len(content_parts) > 1:
                    factory.context_manager.add_message(
                        "user",
                        content_parts,
                        metadata={
                            "source": "tool_injection",
                            "tool": "read_markdown",
                            "file": file_path,
                            "generated_by_tool": True,
                            "note": "Full content injection for Vision compatibility"
                        }
                    )
                    logger.info(f"Injected full multimodal content ({len(content_parts)} parts) as USER message")
                    
                    # Set restart flag on GridRunContext
                    if hasattr(ctx.context, 'should_restart'):
                        ctx.context.should_restart = True
                        logger.info("Set should_restart flag on context")
                    
                    # Return dummy text to complete tool call gracefully
                    return [ToolOutputText(text=f"[System: Content of {file_path} loaded with {len(content_parts)} parts (images detected). Agent is restarting to process visuals...]")]
                    
            except Exception as e:
                logger.warning(f"Failed to inject tool content into context: {e}")
                # Fallback to returning original output if injection fails
        
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























