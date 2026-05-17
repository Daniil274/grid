"""
Smart text input: finds a field by name in UIA, focuses it, then types text.
Handles Unicode/Cyrillic via clipboard. Replaces the old keyboard("text") pattern.
"""

import time
import pyautogui
import win32clipboard
from agents import function_tool

pyautogui.FAILSAFE = False


def _field_score(el: dict, query: str) -> tuple:
    name = (el.get("n") or "").casefold()
    q = query.casefold()
    exact = name == q
    starts = name.startswith(q)
    type_rank = 0 if el.get("t") == "Edit" else 1
    return (type_rank, not exact, not starts, len(name))


def _paste_unicode(text: str) -> None:
    """Type arbitrary Unicode via clipboard — reliable for all scripts including Cyrillic."""
    prev = None
    try:
        win32clipboard.OpenClipboard()
        try:
            prev = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:
            pass
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
    except Exception:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        raise

    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.12)

    if prev is not None:
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(prev, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        except Exception:
            pass


@function_tool
def type_into(text: str, target: str = None, clear: bool = False) -> str:
    """
    Type text into a UI field. Optionally focus the field first by name.
    Handles all Unicode including Cyrillic, Chinese, emoji.

    Args:
        text:   Text to type.
        target: Optional field name to click and focus before typing
                (uses UIA accessibility tree, case-insensitive match).
                Omit if the correct field is already focused.
        clear:  If True, select all existing content before typing (Ctrl+A).
    """
    from _ps_uia import get_uia_elements

    if target:
        elements = get_uia_elements()
        matches = [el for el in elements if target.lower() in (el.get("n") or "").lower()]
        if matches:
            el = sorted(matches, key=lambda item: _field_score(item, target))[0]
            pyautogui.click(el["x"], el["y"])
            time.sleep(0.1)
        else:
            return (
                f"Field '{target}' not found in UIA tree. "
                "Call perceive() to see available fields. "
                "Text was NOT typed."
            )

    if clear:
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)

    is_ascii = all(ord(c) < 128 for c in text)
    if is_ascii:
        pyautogui.write(text, interval=0.018)
    else:
        _paste_unicode(text)

    focus_note = f" into '{target}'" if target else ""
    clear_note = " (cleared first)" if clear else ""
    return f"OK: typed {len(text)} chars{focus_note}{clear_note}"
