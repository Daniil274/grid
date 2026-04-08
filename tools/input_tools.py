"""
Keyboard and Input Tools - Emulate keyboard presses and text input.
"""

import logging
import platform
from typing import List, Union
from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)


def _get_pyautogui():
    import pyautogui
    return pyautogui


def _get_pyperclip():
    import pyperclip
    return pyperclip


def _paste_via_clipboard(text: str) -> None:
    pyautogui = _get_pyautogui()
    pyperclip = _get_pyperclip()
    old = pyperclip.paste()
    try:
        pyperclip.copy(text)
        if platform.system() == "Darwin":
            pyautogui.hotkey("command", "v")
        else:
            pyautogui.hotkey("ctrl", "v")
    finally:
        try:
            pyperclip.copy(old)
        except Exception:
            pass


@function_tool
async def keyboard_type(
    context: RunContextWrapper,
    text: str,
    interval: float = 0.1
) -> str:
    """
    Type text as if it were typed on a keyboard.

    Uses clipboard paste so the text appears exactly as given, regardless of
    the current keyboard layout (e.g. Russian vs English).

    Args:
        text: The text to type (any language, Unicode supported)
        interval: Unused; kept for API compatibility. Paste is instant.

    Returns:
        Confirmation message
    """
    try:
        if not text:
            return "No text to type."
        _paste_via_clipboard(text)
        return f"Successfully typed: '{text}'"
    except Exception as e:
        logger.error(f"Error typing text: {e}")
        return f"Error typing text: {e}"


@function_tool
async def keyboard_press(
    context: RunContextWrapper,
    keys: Union[str, List[str]]
) -> str:
    """
    Press one or more keys (e.g., 'enter', 'esc', ['ctrl', 'c']).

    Args:
        keys: A single key string or a list of keys to press simultaneously (hotkey)

    Returns:
        Confirmation message
    """
    try:
        pyautogui = _get_pyautogui()
        if isinstance(keys, list):
            pyautogui.hotkey(*keys)
            return f"Successfully pressed hotkey: {'+'.join(keys)}"
        else:
            pyautogui.press(keys)
            return f"Successfully pressed key: {keys}"
    except Exception as e:
        logger.error(f"Error pressing keys: {e}")
        return f"Error pressing keys: {e}"


@function_tool
async def keyboard_hotkey(
    context: RunContextWrapper,
    keys: List[str]
) -> str:
    """
    Press a combination of keys simultaneously (e.g., ['ctrl', 'alt', 'del']).

    Args:
        keys: List of keys to press together

    Returns:
        Confirmation message
    """
    try:
        pyautogui = _get_pyautogui()
        pyautogui.hotkey(*keys)
        return f"Successfully pressed hotkey: {'+'.join(keys)}"
    except Exception as e:
        logger.error(f"Error pressing hotkey: {e}")
        return f"Error pressing hotkey: {e}"


INPUT_TOOLS = {
    "keyboard_type": keyboard_type,
    "keyboard_press": keyboard_press,
    "keyboard_hotkey": keyboard_hotkey,
}
