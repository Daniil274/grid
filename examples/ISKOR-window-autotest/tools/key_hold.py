"""
Key hold tool for GUI window automation.
Simulates holding a key for a specified duration via xdotool keydown/keyup.
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
from window_helper import KEY_MAP, find_window_id, hold_key


def key_hold_impl(key: str, duration_ms: int = 1000) -> dict:
    """Hold a key in the target application window for duration_ms milliseconds."""
    key_str = str(key).lower()
    xsym = KEY_MAP.get(key_str, key_str)

    try:
        wid = find_window_id()
        hold_key(wid, key_str, duration_ms)
        return {
            'success': True,
            'message': f'Key "{key}" (xsym: {xsym}) held for {duration_ms}ms in window {wid}',
            'key': key,
            'xsym': xsym,
            'duration_ms': duration_ms,
            'window_id': wid,
        }
    except Exception as exc:
        return {
            'success': False,
            'message': f'Error holding key "{key}": {exc}',
            'key': key,
            'xsym': xsym,
            'duration_ms': duration_ms,
            'window_id': None,
        }


@function_tool
def key_hold(key: str, duration_ms: int = 1000) -> dict:
    """
    Hold a key in the target application window for the specified duration.

    Sends xdotool keydown, waits duration_ms milliseconds, then sends keyup.
    The application receives a continuous stream of auto-repeat events from X11.

    Args:
        key: Key name (up/down/left/right/enter/esc/0-9/q/off)
        duration_ms: Hold duration in milliseconds (default: 1000)

    Returns:
        dict: {
            'success': bool,
            'message': str,
            'key': str,
            'xsym': str,
            'duration_ms': int,
            'window_id': str
        }

    Example:
        >>> key_hold("up", duration_ms=2000)
        {'success': True, 'message': 'Key "up" (xsym: Up) held for 2000ms ...', ...}
    """
    return key_hold_impl(key, duration_ms)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Hold key in application window')
    parser.add_argument('key', help='Key name')
    parser.add_argument('--duration', type=int, default=1000,
                        help='Hold duration in milliseconds (default: 1000)')
    args = parser.parse_args()

    result = key_hold_impl(args.key, args.duration)
    print(result['message'])
    sys.exit(0 if result['success'] else 1)
