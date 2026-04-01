"""
Find and select a window to work with.
Lists visible application windows and optionally sets one as active.
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
from _desktop_helper import list_windows, save_session


@function_tool
def find_window(query: str = None, set_active: str = None) -> dict:
    """
    List visible application windows, optionally filter by title, optionally set one as active.

    Call this first to discover what windows are available, then pick one
    to work with. Pass its id to other tools, or use set_active to make it
    the default for all subsequent tool calls.

    Args:
        query: Optional substring to filter windows by title (case-insensitive).
               Pass None to list all visible windows.
        set_active: Window ID to set as the active window for subsequent tool calls.
                    Pass None to just list windows without changing the active window.

    Returns:
        dict: {
            'success': bool,
            'windows': [{'id', 'title', 'x', 'y', 'width', 'height'}, ...],
            'count': int,
            'active_window_id': str | None,
            'message': str
        }

    Example:
        >>> find_window()                         # list all
        >>> find_window(query="ISKOR")            # filter by title
        >>> find_window(set_active="67108870")    # set active window
        >>> find_window(query="Terminal", set_active="48234502")  # find + set
    """
    try:
        windows = list_windows(query)

        if set_active:
            save_session(active_window_id=str(set_active))

        active_wid = None
        from _desktop_helper import get_session
        active_wid = get_session().get('active_window_id')

        if not windows:
            msg = f'No windows found' + (f' matching "{query}"' if query else '')
        else:
            lines = [f'Found {len(windows)} window(s):']
            for w in windows:
                active_mark = ' ← active' if w['id'] == active_wid else ''
                lines.append(
                    f'  id={w["id"]}  {w["width"]}x{w["height"]}  "{w["title"]}"{active_mark}'
                )
            if set_active:
                lines.append(f'Active window set to: {set_active}')
            msg = '\n'.join(lines)

        return {
            'success': True,
            'windows': windows,
            'count': len(windows),
            'active_window_id': active_wid,
            'message': msg,
        }

    except Exception as exc:
        return {'success': False, 'windows': [], 'count': 0,
                'active_window_id': None, 'message': f'Error: {exc}'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('query', nargs='?', help='Title filter')
    parser.add_argument('--set-active', help='Set active window by id')
    args = parser.parse_args()
    result = find_window(args.query, args.set_active)
    print(result['message'])
    sys.exit(0 if result['success'] else 1)
