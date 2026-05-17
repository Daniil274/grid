"""
Window management: list, focus, inspect active window.
"""

import win32gui
import win32con
from agents import function_tool


def _visible():
    wins = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd).strip()
            if t:
                wins.append((hwnd, t))
    win32gui.EnumWindows(cb, None)
    return wins


@function_tool
def list_windows() -> str:
    """List all visible windows on the desktop with their titles."""
    wins = _visible()
    if not wins:
        return "No visible windows."
    lines = [f"Visible windows ({len(wins)}):"]
    for hwnd, title in wins[:50]:
        lines.append(f"  {hwnd:10d}  \"{title}\"")
    if len(wins) > 50:
        lines.append(f"  ... and {len(wins)-50} more")
    return "\n".join(lines)


@function_tool
def focus_window(title: str) -> str:
    """
    Bring a window to the foreground by title substring match.

    Args:
        title: Substring of the window title (case-insensitive).
    """
    matches = [(h, t) for h, t in _visible() if title.lower() in t.lower()]
    if not matches:
        return f"No window matching '{title}'. Call list_windows() to see available windows."
    hwnd, full = matches[0]
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return f"Focused: \"{full}\" (hwnd={hwnd})"
    except Exception as e:
        return f"Failed to focus '{full}': {e}"


@function_tool
def active_window() -> str:
    """Get title, handle, position, and size of the currently focused window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return "No active window."
    title = win32gui.GetWindowText(hwnd).strip() or "(untitled)"
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return f"Active: \"{title}\" hwnd={hwnd} pos=({l},{t}) size={r-l}×{b-t}px"
