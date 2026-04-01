"""
Keyboard key press tool for computer use.
Sends key combinations to the active window.
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
from _desktop_helper import require_wid, key_send

# Quick reference for the LLM
KEY_EXAMPLES = """
Common key names (xdotool keysym format):
  Letters/digits: a-z, A-Z, 0-9
  Navigation:     Up, Down, Left, Right, Home, End, Page_Up, Page_Down
  Control:        Return, Escape, Tab, BackSpace, Delete, Insert, space
  Function keys:  F1-F12
  Modifiers:      ctrl, alt, shift, super (Windows key)
  Combinations:   ctrl+c, ctrl+v, ctrl+z, ctrl+s, alt+F4, ctrl+alt+t
                  ctrl+shift+t, super+d, alt+Tab, shift+Tab
"""


@function_tool
def key_press(keys: str, window_id: str = None) -> dict:
    """
    Send a key or key combination to the window.

    Args:
        keys:       Key name or combination in xdotool format.
                    Examples:
                      'Return'      — Enter key
                      'Escape'      — Escape key
                      'Tab'         — Tab key
                      'ctrl+c'      — Copy
                      'ctrl+v'      — Paste
                      'ctrl+z'      — Undo
                      'alt+F4'      — Close window
                      'ctrl+alt+t'  — Open terminal (desktop shortcut)
                      'super+d'     — Show desktop
                      'F5'          — Refresh
                      'ctrl+a'      — Select all
                      'BackSpace'   — Delete backwards
                      'Delete'      — Delete forwards
        window_id:  Window to send key to. Uses active window if not specified.

    Returns:
        dict: {'success': bool, 'message': str, 'keys': str}

    Example:
        >>> key_press('ctrl+c')
        >>> key_press('Return')
        >>> key_press('alt+F4', window_id='67108870')
    """
    try:
        wid = require_wid(window_id)
        key_send(wid, keys)
        return {
            'success': True,
            'message': f'Key "{keys}" sent to window {wid}',
            'keys': keys,
        }
    except Exception as exc:
        return {
            'success': False,
            'message': f'Key press error: {exc}',
            'keys': keys,
        }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('keys', help='Key or combination, e.g. ctrl+c')
    parser.add_argument('--window')
    args = parser.parse_args()
    r = key_press(args.keys, args.window)
    print(r['message'])
    sys.exit(0 if r['success'] else 1)
