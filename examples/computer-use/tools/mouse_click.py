"""
Mouse click tool for computer use.
Clicks at specified coordinates relative to the window's top-left corner.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from agents import function_tool
except ImportError:
    def function_tool(f): return f

_tools_dir = os.path.dirname(__file__)
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)
from _desktop_helper import require_wid, mouse_click as _click


@function_tool
def mouse_click(
    x: int,
    y: int,
    button: str = 'left',
    clicks: int = 1,
    window_id: str = None,
) -> dict:
    """
    Click at position (x, y) relative to the top-left corner of the window.

    Always take a screenshot after clicking to verify the result.

    Args:
        x:          Horizontal position in pixels from the window's left edge.
        y:          Vertical position in pixels from the window's top edge.
        button:     Mouse button: 'left' (default), 'right', 'middle'.
        clicks:     Number of clicks (1=single, 2=double-click).
        window_id:  Window to click in. Uses active window if not specified.

    Returns:
        dict: {'success': bool, 'message': str, 'x': int, 'y': int, 'button': str}

    Example:
        >>> mouse_click(240, 139)                  # left click center
        >>> mouse_click(100, 50, button='right')   # right-click context menu
        >>> mouse_click(200, 100, clicks=2)        # double-click
    """
    try:
        wid = require_wid(window_id)
        _click(wid, x, y, button=button, clicks=clicks)
        action = 'Double-click' if clicks == 2 else 'Click'
        return {
            'success': True,
            'message': f'{action} ({button}) at ({x}, {y}) in window {wid}',
            'x': x, 'y': y, 'button': button, 'clicks': clicks,
        }
    except Exception as exc:
        return {
            'success': False,
            'message': f'Error clicking at ({x}, {y}): {exc}',
            'x': x, 'y': y, 'button': button, 'clicks': clicks,
        }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('x', type=int)
    parser.add_argument('y', type=int)
    parser.add_argument('--button', default='left')
    parser.add_argument('--clicks', type=int, default=1)
    parser.add_argument('--window', help='Window ID')
    args = parser.parse_args()
    r = mouse_click(args.x, args.y, args.button, args.clicks, args.window)
    print(r['message'])
    sys.exit(0 if r['success'] else 1)
