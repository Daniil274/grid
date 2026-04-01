"""
Screenshot tool for computer use.
Captures the active window or the full screen.
"""

import base64
import os
import sys
import time
from typing import List, Union, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from agents import function_tool
    from agents.tool import ToolOutputImage, ToolOutputText
except ImportError:
    def function_tool(f): return f
    class ToolOutputText:
        def __init__(self, text): self.text = text
    class ToolOutputImage:
        def __init__(self, image_url, detail=None): self.image_url = image_url

try:
    from utils.path_utils import resolve_agent_path_auto as _resolve
    from utils.path_utils import display_agent_path_auto as _display
except ImportError:
    def _resolve(p): return p
    def _display(p): return p

_tools_dir = os.path.dirname(__file__)
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)
from _desktop_helper import require_wid, capture_window, capture_screen, get_active_wid


def _to_data_url(path: str) -> str:
    with open(path, 'rb') as f:
        return 'data:image/png;base64,' + base64.b64encode(f.read()).decode()


@function_tool
def get_screen(
    window_id: str = None,
    save_path: str = None,
    full_screen: bool = False,
) -> List[Union[ToolOutputText, ToolOutputImage]]:
    """
    Capture a screenshot of the active window (or full screen) for AI analysis.

    Args:
        window_id:   Window ID to capture. If None, uses the active window set
                     by find_window(). Pass "screen" to capture the full desktop.
        save_path:   Where to save the PNG. Relative paths use working_directory.
                     Default: screenshots/screen_{timestamp}.png
        full_screen: If True, capture the entire screen regardless of window_id.

    Returns:
        List with ToolOutputText (metadata) and ToolOutputImage (the screenshot).

    Example:
        >>> get_screen()                          # active window
        >>> get_screen(window_id="67108870")      # specific window
        >>> get_screen(full_screen=True)          # full desktop
    """
    if save_path is None:
        ts = time.strftime('%Y%m%d_%H%M%S')
        save_path = f'screenshots/screen_{ts}.png'

    abs_path = _resolve(save_path)
    disp_path = _display(abs_path)

    try:
        t0 = time.monotonic()

        if full_screen or window_id == 'screen':
            geom = capture_screen(abs_path)
            source = 'full screen'
        else:
            wid = require_wid(window_id)
            geom = capture_window(wid, abs_path)
            source = f'window {wid}'

        elapsed = time.monotonic() - t0
        data_url = _to_data_url(abs_path)

        info = (
            f'Screenshot: {disp_path}\n'
            f'Source: {source} | Size: {geom["width"]}x{geom["height"]}px | '
            f'Time: {elapsed:.2f}s'
        )
        return [ToolOutputText(text=info),
                ToolOutputImage(image_url=data_url, detail='high')]

    except Exception as exc:
        return [ToolOutputText(text=f'Error capturing screenshot: {exc}')]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('output', nargs='?', default='screenshot.png')
    parser.add_argument('--window', help='Window ID')
    parser.add_argument('--screen', action='store_true')
    args = parser.parse_args()
    result = get_screen(args.window, args.output, args.screen)
    for item in result:
        if hasattr(item, 'text'):
            print(item.text)
