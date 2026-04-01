"""
Text typing tool for computer use.
Types a string into the active window as if the user typed it.
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
from _desktop_helper import require_wid, type_string


@function_tool
def type_text(
    text: str,
    delay_ms: int = 12,
    window_id: str = None,
) -> dict:
    """
    Type a text string into the window, simulating keyboard input character by character.

    Click on the target text field first, then call type_text.

    Args:
        text:       The text to type. Supports Unicode.
        delay_ms:   Delay between characters in milliseconds (default 12 ≈ 80 wpm).
                    Increase for slow applications that miss keystrokes.
        window_id:  Window to type into. Uses active window if not specified.

    Returns:
        dict: {'success': bool, 'message': str, 'text': str}

    Example:
        >>> type_text("Hello, World!")
        >>> type_text("192.168.1.1", delay_ms=20)
        >>> type_text("password123")
    """
    try:
        wid = require_wid(window_id)
        type_string(wid, text, delay_ms=delay_ms)
        preview = text[:40] + '...' if len(text) > 40 else text
        return {
            'success': True,
            'message': f'Typed {len(text)} chars into window {wid}: "{preview}"',
            'text': text,
            'chars': len(text),
        }
    except Exception as exc:
        return {
            'success': False,
            'message': f'Type error: {exc}',
            'text': text,
            'chars': 0,
        }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('text')
    parser.add_argument('--delay', type=int, default=12)
    parser.add_argument('--window')
    args = parser.parse_args()
    r = type_text(args.text, args.delay, args.window)
    print(r['message'])
    sys.exit(0 if r['success'] else 1)
