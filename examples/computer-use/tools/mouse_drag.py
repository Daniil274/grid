"""
Mouse drag tool for computer use.
Drags from one point to another within the window.
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
from _desktop_helper import require_wid, mouse_drag as _drag


@function_tool
def mouse_drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    button: str = 'left',
    window_id: str = None,
) -> dict:
    """
    Drag from (x1, y1) to (x2, y2) within the window.

    Useful for sliders, resizing, selecting text, or drag-and-drop.

    Args:
        x1, y1:     Start position (pixels from window top-left).
        x2, y2:     End position (pixels from window top-left).
        button:     Mouse button to hold during drag: 'left' (default), 'right'.
        window_id:  Window to drag in. Uses active window if not specified.

    Returns:
        dict: {'success': bool, 'message': str}

    Example:
        >>> mouse_drag(50, 100, 200, 100)          # horizontal drag
        >>> mouse_drag(100, 50, 100, 300)          # vertical drag (scroll bar)
    """
    try:
        wid = require_wid(window_id)
        _drag(wid, x1, y1, x2, y2, button=button)
        return {
            'success': True,
            'message': f'Dragged ({button}) from ({x1},{y1}) to ({x2},{y2}) in window {wid}',
        }
    except Exception as exc:
        return {'success': False, 'message': f'Drag error: {exc}'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('x1', type=int)
    parser.add_argument('y1', type=int)
    parser.add_argument('x2', type=int)
    parser.add_argument('y2', type=int)
    parser.add_argument('--button', default='left')
    parser.add_argument('--window')
    args = parser.parse_args()
    r = mouse_drag(args.x1, args.y1, args.x2, args.y2, args.button, args.window)
    print(r['message'])
    sys.exit(0 if r['success'] else 1)
