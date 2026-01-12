"""
OCR Tools for Grid agents using DeepSeek-OCR via subprocess wrapper.
"""
import sys
import json
import logging
import subprocess
import os
from pathlib import Path
from typing import List, Union, Dict, Any, Optional
import inspect

# Import from agents SDK
from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText
from utils.multimodal_converter import MultimodalConverter

logger = logging.getLogger("tools.ocr")

DEEPSEEK_OCR_DIR = Path(__file__).parent / "DeepSeek-OCR"
WRAPPER_SCRIPT = DEEPSEEK_OCR_DIR / "ocr_wrapper.py"

def get_python_executable() -> str:
    """
    Find the python executable to use for DeepSeek-OCR.
    Prioritize the venv inside DeepSeek-OCR directory.
    """
    # Check for .venv in DeepSeek-OCR directory
    venv_python = DEEPSEEK_OCR_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
        
    venv_python_linux = DEEPSEEK_OCR_DIR / ".venv" / "bin" / "python"
    if venv_python_linux.exists():
        return str(venv_python_linux)

    # Fallback to system python (sys.executable)
    return sys.executable

@function_tool
def pdf_to_markdown_structured(ctx: RunContextWrapper[Any], pdf_path: str, pages: str = "1:1") -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Convert PDF to structured Markdown with interleaved images.
    Returns a list of content blocks (text and images) exactly in the order they appear.
    
    Args:
        pdf_path: Absolute path to the PDF file.
        pages: Page range string (e.g., "1:5", "1", "1:end").
    """
    python_exe = get_python_executable()
    
    if not WRAPPER_SCRIPT.exists():
        return [ToolOutputText(text=f"OCR wrapper script not found at {WRAPPER_SCRIPT}")]
        
    cmd = [
        python_exe,
        str(WRAPPER_SCRIPT),
        "--pdf", pdf_path,
        "--pages", pages,
        "--full-page"
    ]
    
    try:
        logger.info(f"Running OCR with command: {' '.join(cmd)}")
        # Run subprocess
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=False 
        )
        
        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"OCR process failed: {error_msg}")
            return [ToolOutputText(text=f"OCR process failed: {error_msg}")]
            
        # Parse JSON output
        try:
            output_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse OCR output: {result.stdout}")
            return [ToolOutputText(text=f"Failed to parse OCR output. Raw output: {result.stdout[:200]}...")]
            
        # Convert dicts to ToolOutput objects
        final_output = []
        if isinstance(output_data, dict) and "error" in output_data:
             return [ToolOutputText(text=f"OCR Error: {output_data['error']}")]

        if isinstance(output_data, list):
            for item in output_data:
                if item.get("type") == "text":
                    final_output.append(ToolOutputText(text=item.get("text", "")))
                elif item.get("type") == "image_url":
                    img_data = item.get("image_url", {})
                    url = img_data.get("url", "")
                    detail = img_data.get("detail", "auto")
                    
                    if url:
                        # Log image info for debugging
                        logger.info(f"Processing image for output (length: {len(url)})")
                        
                        try:
                            # Attempt to create ToolOutputImage
                            img_out = ToolOutputImage(
                                image_url=url,
                                detail=detail if detail in ["low", "high", "auto"] else "auto"
                            )
                            final_output.append(img_out)
                        except TypeError as te:
                             # Fallback if argument name is different
                            logger.warning(f"ToolOutputImage init failed (TypeError): {te}. Trying 'url' arg.")
                            try:
                                img_out = ToolOutputImage(
                                    url=url,
                                    detail=detail
                                )
                                final_output.append(img_out)
                            except Exception as e2:
                                logger.error(f"ToolOutputImage fallback failed: {e2}")
                                final_output.append(ToolOutputText(text=f"[Error: Could not create image output]"))
                        except Exception as e:
                            logger.error(f"Failed to create ToolOutputImage: {e}")
                            final_output.append(ToolOutputText(text=f"[Error: Could not create image output]"))

        # --- Injection Strategy: Persist FULL content as USER message ---
        # If output contains images, we inject EVERYTHING (text + images) as a User message
        # and raise MultimodalContentInjected to trigger a fresh agent turn.
        has_images = any(isinstance(item, ToolOutputImage) for item in final_output)
        
        if has_images:
            try:
                # Get factory and context manager
                # ctx.context is GridRunContext
                factory = getattr(ctx.context, 'factory', None)
                if factory and hasattr(factory, 'context_manager'):
                    
                    # Convert to schemas.ImageContent
                    # Import locally to avoid circular deps
                    from schemas import ImageContent, ImageUrl, TextContent
                    
                    content_parts = []
                    # Add a text prefix to explain context
                    content_parts.append(TextContent(type="text", text="[System: Result of PDF Processing Tool (Text + Images)]"))
                    
                    for item in final_output:
                        if isinstance(item, ToolOutputText):
                            content_parts.append(TextContent(type="text", text=item.text))
                        elif isinstance(item, ToolOutputImage):
                            # Extract URL from ToolOutputImage
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
                                "tool": "pdf_to_markdown",
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
                        return [ToolOutputText(text="[System: Multimodal content injected. Agent is restarting to process images...]")]
                        
            except Exception as e:
                logger.warning(f"Failed to inject tool content into context: {e}")
                # Fallback to returning original output if injection fails
        # ----------------------------------------------------------

        if not final_output:
            return [ToolOutputText(text="OCR returned no content.")]

        return final_output

    except Exception as e:
        # If this is our special injection exception, let it bubble up
        if type(e).__name__ == 'MultimodalContentInjected':
            raise e
            
        logger.error(f"Error executing OCR tool: {e}", exc_info=True)
        return [ToolOutputText(text=f"Error executing OCR tool: {str(e)}")]

OCR_TOOLS = {
    "pdf_to_markdown": pdf_to_markdown_structured,
    "pdf": pdf_to_markdown_structured # alias
}
