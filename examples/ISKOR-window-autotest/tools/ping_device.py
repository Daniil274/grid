"""
Window ping tool for GUI window automation.
Checks that the target application window is open and responding.
Replaces serial ping_device — no physical device required.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from agents import function_tool
except ImportError:
    def function_tool(func):
        return func

_tools_dir = os.path.dirname(__file__)
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)
from window_helper import find_window_id, get_window_geometry, _get_title_pattern


def ping_device_impl() -> dict:
    """Check that the target application window is open."""
    pattern = _get_title_pattern()
    try:
        wid = find_window_id(pattern)
        geom = get_window_geometry(wid)
        return {
            'success': True,
            'message': (
                f'Window found: "{pattern}" (id={wid}) — '
                f'{geom["width"]}x{geom["height"]} at ({geom["x"]},{geom["y"]})'
            ),
            'alive': True,
            'window_id': wid,
            'geometry': geom,
        }
    except Exception as exc:
        return {
            'success': False,
            'message': f'Window "{pattern}" not found: {exc}',
            'alive': False,
            'window_id': None,
            'geometry': None,
        }


@function_tool
def ping_device() -> dict:
    """
    Check that the target application window is open and visible.

    Analogous to the serial ping_device — use this at the start of every test
    run to confirm the application is running before sending key events.

    Returns:
        dict: {
            'success': bool,
            'message': str,
            'alive': bool,
            'window_id': str,
            'geometry': dict  # {'x', 'y', 'width', 'height'}
        }

    Example:
        >>> ping_device()
        {'success': True, 'message': 'Window found: "ISKOR" (id=12345678) — 800x600 at (0,0)', ...}
    """
    return ping_device_impl()


if __name__ == '__main__':
    result = ping_device_impl()
    print(result['message'])
    sys.exit(0 if result['success'] else 1)
