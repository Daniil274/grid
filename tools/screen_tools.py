"""
Screen and Vision Tools - Capture screenshots and analyze screen content.
"""

import logging
import time
from pathlib import Path
from typing import Optional
import pyautogui
from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputText

from .vision_tools import _inject_image_for_analysis

logger = logging.getLogger(__name__)

@function_tool
async def take_screenshot(
    context: RunContextWrapper,
    question: str = "Что на экране?",
    filename: Optional[str] = None
) -> str:
    """
    Take a screenshot of the entire screen and analyze it.

    Args:
        question: Question about the screen content for the vision model
        filename: Optional name for the screenshot file

    Returns:
        Analysis result of the screenshot
    """
    try:
        workspace = Path("d:/Work/repo/agents_portable/workspace")
        screenshots_dir = workspace / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            filename = f"screenshot_{int(time.time())}.png"
        if not filename.endswith(".png"):
            filename += ".png"

        filepath = screenshots_dir / filename

        import asyncio
        await asyncio.sleep(0.05)

        screenshot = pyautogui.screenshot()
        screenshot.save(str(filepath))

        logger.info(f"📸 Screenshot saved to {filepath}, analyzing with question: {question}")

        result = await _inject_image_for_analysis(context, str(filepath), question)

        texts = [item.text for item in result if isinstance(item, ToolOutputText) and getattr(item, "text", None)]
        return "\n".join(texts) if texts else "Скриншот сохранён. Изображение передано агенту для анализа."
    except Exception as e:
        logger.error(f"❌ Error taking/analyzing screenshot: {e}")
        return f"❌ Error taking/analyzing screenshot: {e}"

SCREEN_TOOLS = {
    "take_screenshot": take_screenshot,
}
