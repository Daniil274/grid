"""
Keyboard and Input Tools - Emulate keyboard presses and text input.
In headless environments (no DISPLAY) these tools are disabled.
"""

import logging
import os
import platform
from typing import List, Optional, Union
from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)

if os.environ.get("DISPLAY"):
    import pyautogui
    import pyperclip

    # Disable PyAutoGUI fail-safe for background operation if needed,
    # but keeping it enabled (default) is safer.
    # pyautogui.FAILSAFE = True

    def _paste_via_clipboard(text: str) -> None:
        """
        Вставить текст через буфер обмена (Ctrl+V / Cmd+V).
        Не зависит от раскладки клавиатуры — текст вставляется как есть.
        """
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
                return "⚠️ No text to type."
            _paste_via_clipboard(text)
            return f"✅ Successfully typed: '{text}'"
        except Exception as e:
            logger.error(f"❌ Error typing text: {e}")
            return f"❌ Error typing text: {e}"

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
            if isinstance(keys, list):
                pyautogui.hotkey(*keys)
                return f"✅ Successfully pressed hotkey: {'+'.join(keys)}"
            else:
                pyautogui.press(keys)
                return f"✅ Successfully pressed key: {keys}"
        except Exception as e:
            logger.error(f"❌ Error pressing keys: {e}")
            return f"❌ Error pressing keys: {e}"

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
            pyautogui.hotkey(*keys)
            return f"✅ Successfully pressed hotkey: {'+'.join(keys)}"
        except Exception as e:
            logger.error(f"❌ Error pressing hotkey: {e}")
            return f"❌ Error pressing hotkey: {e}"

    INPUT_TOOLS = {
        "keyboard_type": keyboard_type,
        "keyboard_press": keyboard_press,
        "keyboard_hotkey": keyboard_hotkey,
    }
else:
    logger.debug("DISPLAY not set; keyboard/input tools disabled")
    INPUT_TOOLS = {}

    @function_tool
    async def keyboard_type(context: RunContextWrapper, text: str, interval: float = 0.1) -> str:
        return "⚠️ Keyboard tools unavailable (no DISPLAY)."

    @function_tool
    async def keyboard_press(context: RunContextWrapper, keys: Union[str, List[str]]) -> str:
        return "⚠️ Keyboard tools unavailable (no DISPLAY)."

    @function_tool
    async def keyboard_hotkey(context: RunContextWrapper, keys: List[str]) -> str:
        return "⚠️ Keyboard tools unavailable (no DISPLAY)."
