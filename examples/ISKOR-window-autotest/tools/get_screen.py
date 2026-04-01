"""
Screenshot tool for GUI window automation.
Captures the target application window and returns it for AI analysis.
"""

import base64
import os
import sys
import time
from typing import List, Union, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from utils.path_utils import resolve_agent_path_auto as _resolve_path
    from utils.path_utils import display_agent_path_auto as _display_path
except ImportError:
    def _resolve_path(p: str) -> str:
        return p
    def _display_path(p: str) -> str:
        return p

try:
    from agents import function_tool
    from agents.tool import ToolOutputImage, ToolOutputText
except ImportError:
    def function_tool(func):
        return func
    class ToolOutputText:
        def __init__(self, text): self.text = text
    class ToolOutputImage:
        def __init__(self, image_url, detail=None): self.image_url = image_url

_tools_dir = os.path.dirname(__file__)
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)
from window_helper import find_window_id, capture_window


def _image_to_data_url(image_path: str) -> str:
    with open(image_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    return f'data:image/png;base64,{b64}'


@function_tool
def get_screen(
    save_path: str = None,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Capture a screenshot of the target application window and return it for AI analysis.

    Args:
        save_path: Path to save PNG file. Relative paths are resolved against the
            agent's working_directory. Default: screenshots/screen_{timestamp}.png

    Returns:
        List with ToolOutputText (path and metadata) and ToolOutputImage (the screenshot).

    Example:
        >>> get_screen("screenshots/step_01.png")
        [ToolOutputText(...), ToolOutputImage(...)]
    """
    if save_path is None:
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        save_path = f'screenshots/screen_{timestamp}.png'

    abs_path = _resolve_path(save_path)
    display_path = _display_path(abs_path)
    save_path = abs_path

    try:
        t0 = time.monotonic()
        wid = find_window_id()
        geom = capture_window(wid, save_path)
        duration = time.monotonic() - t0

        data_url = _image_to_data_url(save_path)

        info_text = (
            f"📸 Screenshot captured: {display_path}\n"
            f"Window ID: {wid}\n"
            f"Size: {geom['width']}x{geom['height']}px  "
            f"Position: ({geom['x']}, {geom['y']})\n"
            f"Time: {duration:.2f}s"
        )
        return [
            ToolOutputText(text=info_text),
            ToolOutputImage(image_url=data_url, detail="high"),
        ]

    except Exception as exc:
        return [ToolOutputText(text=f"❌ Error capturing window screenshot: {exc}")]


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Capture screenshot of target window')
    parser.add_argument('output', nargs='?', default='screenshot.png')
    args = parser.parse_args()

    result = get_screen(args.output)
    success = True
    for item in result:
        if isinstance(item, ToolOutputText):
            print(item.text)
            if '❌' in item.text:
                success = False
        elif hasattr(item, 'text'):
            print(item.text)
            if '❌' in item.text:
                success = False
    sys.exit(0 if success else 1)
