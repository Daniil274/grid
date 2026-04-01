"""
Key press tool for GUI window automation.
Sends a single key press to the target application window via xdotool.
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
from window_helper import KEY_MAP, find_window_id, send_key


def key_press_impl(key: str) -> dict:
    """Send a single key press to the target application window."""
    key_str = str(key).lower()
    xsym = KEY_MAP.get(key_str, key_str)

    try:
        wid = find_window_id()
        send_key(wid, key_str)
        return {
            'success': True,
            'message': f'Key "{key}" (xsym: {xsym}) sent to window {wid}',
            'key': key,
            'xsym': xsym,
            'window_id': wid,
        }
    except Exception as exc:
        return {
            'success': False,
            'message': f'Error sending key "{key}": {exc}',
            'key': key,
            'xsym': xsym,
            'window_id': None,
        }


@function_tool
def key_press(key: str) -> dict:
    """
    Send a key press to the target application window.

    Args:
        key: Key name — same names as serial-based tools:
             digits: '0'..'9'
             navigation: 'up', 'down', 'left', 'right'
             control: 'enter', 'esc', 'q', 'off'

    Returns:
        dict: {
            'success': bool,
            'message': str,
            'key': str,
            'xsym': str,
            'window_id': str
        }

    Example:
        >>> key_press("enter")
        {'success': True, 'message': 'Key "enter" (xsym: Return) sent to window 12345678', ...}
    """
    return key_press_impl(key)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Send key press to application window')
    parser.add_argument('key', help='Key name (enter/esc/up/down/left/right/0-9/q/off)')
    args = parser.parse_args()

    result = key_press_impl(args.key)
    print(result['message'])
    sys.exit(0 if result['success'] else 1)
