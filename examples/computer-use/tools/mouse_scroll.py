"""
Mouse scroll tool for computer use.
Scrolls at a specified position within the window.
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
from _desktop_helper import require_wid, mouse_scroll as _scroll


@function_tool
def mouse_scroll(
    x: int,
    y: int,
    direction: str = 'down',
    amount: int = 3,
    window_id: str = None,
) -> dict:
    """
    Scroll at position (x, y) within the window.

    Args:
        x:          Horizontal position in pixels from window's left edge.
        y:          Vertical position in pixels from window's top edge.
        direction:  'up', 'down', 'left', or 'right'. Default: 'down'.
        amount:     Number of scroll steps (default: 3).
        window_id:  Window to scroll in. Uses active window if not specified.

    Returns:
        dict: {'success': bool, 'message': str}

    Example:
        >>> mouse_scroll(240, 200, direction='down', amount=5)
        >>> mouse_scroll(240, 200, direction='up', amount=3)
    """
    try:
        wid = require_wid(window_id)
        _scroll(wid, x, y, direction=direction, amount=amount)
        return {
            'success': True,
            'message': f'Scrolled {direction} x{amount} at ({x}, {y}) in window {wid}',
        }
    except Exception as exc:
        return {'success': False, 'message': f'Scroll error: {exc}'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('x', type=int)
    parser.add_argument('y', type=int)
    parser.add_argument('--direction', default='down')
    parser.add_argument('--amount', type=int, default=3)
    parser.add_argument('--window')
    args = parser.parse_args()
    r = mouse_scroll(args.x, args.y, args.direction, args.amount, args.window)
    print(r['message'])
    sys.exit(0 if r['success'] else 1)
