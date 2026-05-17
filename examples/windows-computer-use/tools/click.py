"""
Smart click tool: finds element by name in the UIA tree and clicks it.
Falls back to screen coordinates when name-based lookup fails.
Implements the Unified GUI-API Action Layer concept from UFO2.
"""

import pyautogui
from agents import function_tool

pyautogui.FAILSAFE = False


def _match_score(el: dict, query: str, x: int = 0, y: int = 0) -> tuple:
    name = (el.get("n") or "").casefold()
    q = query.casefold()
    exact = name == q
    starts = name.startswith(q)
    word = q in name.split()
    type_rank = {
        "Edit": 0,
        "Button": 1,
        "MenuItem": 2,
        "ListItem": 3,
        "TabItem": 4,
    }.get(el.get("t"), 5)
    if x or y:
        distance = abs(int(el["x"]) - x) + abs(int(el["y"]) - y)
    else:
        distance = 0
    return (not exact, not starts, not word, type_rank, distance, len(name))


@function_tool
def click(name: str = None, x: int = 0, y: int = 0, double: bool = False, right: bool = False) -> str:
    """
    Click a UI element by name (preferred) or by screen coordinates (fallback).

    Args:
        name:   Element name substring to find in the UIA accessibility tree (case-insensitive).
        x:      Screen X coordinate — used when name not provided or element not found.
        y:      Screen Y coordinate — used when name not provided or element not found.
        double: Double-click instead of single click.
        right:  Right-click (context menu).
    """
    from _ps_uia import get_uia_elements

    if name:
        elements = get_uia_elements()
        matches = [el for el in elements if name.lower() in (el.get("n") or "").lower()]
        if matches:
            el = sorted(matches, key=lambda item: _match_score(item, name, x, y))[0]
            cx, cy = el["x"], el["y"]
            btn = "right" if right else "left"
            if double:
                pyautogui.doubleClick(cx, cy)
            else:
                pyautogui.click(cx, cy, button=btn)
            action = "double-clicked" if double else ("right-clicked" if right else "clicked")
            return f"OK: {action} '{el['n']}' ({el['t']}) at ({cx},{cy})"

        if not (x or y):
            return (
                f"Element '{name}' not found in UIA tree. "
                "To use coordinate fallback pass x and y in the SAME call: click(name=..., x=..., y=...)."
            )
        result_prefix = f"UIA: '{name}' not found — coordinate fallback. "
    else:
        result_prefix = ""

    if not (x or y):
        return "Error: provide either name or x,y coordinates."

    btn = "right" if right else "left"
    if double:
        pyautogui.doubleClick(x, y)
        action = "double-clicked"
    else:
        pyautogui.click(x, y, button=btn)
        action = "right-clicked" if right else "clicked"

    return f"{result_prefix}OK: {action} at ({x},{y})"
