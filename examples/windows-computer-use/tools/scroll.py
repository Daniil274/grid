"""
Mouse scroll and drag tools — low-level actions for when click/type_into aren't enough.
"""

import pyautogui
from agents import function_tool

pyautogui.FAILSAFE = False


@function_tool
def scroll(x: int, y: int, amount: int) -> str:
    """
    Scroll at screen coordinates.

    Args:
        x:      Screen X coordinate to scroll at.
        y:      Screen Y coordinate to scroll at.
        amount: Scroll clicks. Positive = scroll up, negative = scroll down.

    Examples:
        scroll(960, 540, -5)    scroll down 5 clicks at screen centre
        scroll(960, 540, 3)     scroll up 3 clicks
    """
    pyautogui.scroll(amount, x=x, y=y)
    direction = "up" if amount > 0 else "down"
    return f"OK: scrolled {direction} {abs(amount)} clicks at ({x},{y})"


@function_tool
def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """
    Drag from one screen position to another (e.g. move a window, resize, drag-and-drop).

    Args:
        x1: Start X coordinate.
        y1: Start Y coordinate.
        x2: End X coordinate.
        y2: End Y coordinate.
    """
    pyautogui.moveTo(x1, y1)
    pyautogui.dragTo(x2, y2, duration=0.35, button="left")
    return f"OK: dragged ({x1},{y1}) → ({x2},{y2})"
