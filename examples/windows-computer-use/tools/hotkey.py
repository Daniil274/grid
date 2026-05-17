"""
Hotkey and single-key press tool. Pure keyboard shortcuts, no text typing.
For text input use type_into().
"""

import pyautogui
from agents import function_tool

pyautogui.FAILSAFE = False


@function_tool
def hotkey(keys: str) -> str:
    """
    Press keyboard shortcuts or key sequences.
    For typing text use type_into() instead.

    Syntax:
      '+' joins keys pressed simultaneously: "ctrl+c", "alt+f4", "ctrl+shift+t"
      ',' separates keys pressed one after another: "down,down,down,enter"
      Mix: "ctrl+a,delete" — select all, then delete

    Args:
        keys: Key expression using pyautogui key names (lowercase).
    """
    steps = [s.strip() for s in keys.split(",") if s.strip()]
    if not steps:
        return "Error: empty key string."
    try:
        for step in steps:
            parts = [p.strip().lower() for p in step.split("+") if p.strip()]
            if len(parts) == 1:
                pyautogui.press(parts[0])
            else:
                pyautogui.hotkey(*parts)
        return f"OK: pressed {keys}"
    except Exception as e:
        return f"Error: {e}"
