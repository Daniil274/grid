"""
Screen and Vision Tools - Capture screenshots and analyze screen content.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Union
import pyautogui
from agents import function_tool, RunContextWrapper
from agents.tool import ToolOutputImage, ToolOutputText

from .vision_tools import _image_path_to_data_url

logger = logging.getLogger(__name__)

@function_tool
async def take_screenshot(
    context: RunContextWrapper,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """Take a screenshot of the entire screen and return it as an image."""
    try:
        wd = Path(context.context.factory.config.get_working_directory())
        screenshots_dir = wd / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        filepath = screenshots_dir / f"screenshot_{int(time.time())}.png"
        last_path = screenshots_dir / "last_screenshot.png"

        await asyncio.sleep(0.05)
        screenshot = pyautogui.screenshot()
        screenshot.save(str(filepath))
        screenshot.save(str(last_path))

        width, height = screenshot.size
        logger.info(f"📸 Screenshot saved to {filepath} ({width}x{height})")

        data_url = _image_path_to_data_url(str(filepath))
        return [
            ToolOutputText(text=f"Screenshot: {filepath}\nSize: {width}x{height}px"),
            ToolOutputImage(image_url=data_url, detail="high"),
        ]
    except Exception as e:
        logger.error(f"❌ Error taking screenshot: {e}")
        return [ToolOutputText(text=f"❌ Error taking screenshot: {e}")]

SCREEN_TOOLS = {
    "take_screenshot": take_screenshot,
}
