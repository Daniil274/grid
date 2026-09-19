"""
Layout-independent keyboard input via Win32 SendInput.

pyautogui builds its char -> virtual-key table once at import with VkKeyScanA,
i.e. through the keyboard layout active at that moment, and then sends raw VK
codes that the target window re-interprets through *its* layout. With a Russian
layout active, Latin letters either map to nothing (silently dropped, and
Ctrl+V/Ctrl+A stop working) or come out as Cyrillic ("hello" -> "руддщ").

Here text goes through KEYEVENTF_UNICODE (the character itself, no layout
involved) and shortcuts use fixed VK codes (VK 'A'..'Z' are the physical keys
regardless of layout), so both work under any input language.
"""

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; it must be present so sizeof(INPUT) is right.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

# Named keys -> VK codes (layout-independent).
_NAMED_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "winleft": 0x5B, "winright": 0x5C, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    "shiftleft": 0xA0, "shiftright": 0xA1, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "altleft": 0xA4, "altright": 0xA5,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "playpause": 0xB3,
    # Punctuation keys by US position.
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "plus": 0xBB, "minus": 0xBD,
}
_NAMED_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_NAMED_VK.update({f"num{i}": 0x60 + i for i in range(10)})

_EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
                0x5B, 0x5C, 0x5D, 0x90, 0xA3, 0xA5}


def _vk_for(key: str) -> int:
    k = key.strip().lower()
    if k in _NAMED_VK:
        return _NAMED_VK[k]
    if len(k) == 1 and ("a" <= k <= "z" or "0" <= k <= "9"):
        return ord(k.upper())
    if len(k) == 1:
        # Non-Latin char (e.g. Cyrillic) — resolve through the current layout.
        res = user32.VkKeyScanW(ord(k))
        if res != -1 and (res & 0xFF) != 0xFF:
            return res & 0xFF
    raise ValueError(f"Unknown key: {key!r}")


def _key_input(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=user32.MapVirtualKeyW(vk, 0), dwFlags=flags)
    return inp


def _unicode_input(code_unit: int, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags)
    return inp


def _send(inputs: list) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent}/{len(inputs)} events "
                      f"(error {ctypes.get_last_error()}; blocked by UIPI?)")


def press_combo(*keys: str) -> None:
    """Press keys together (modifiers first) and release in reverse order."""
    vks = [_vk_for(k) for k in keys]
    _send([_key_input(vk, False) for vk in vks] +
          [_key_input(vk, True) for vk in reversed(vks)])


def type_unicode(text: str, interval: float = 0.0) -> None:
    """Type text character by character, independent of the keyboard layout."""
    text = text.replace("\r\n", "\n")
    for ch in text:
        if ch == "\n":
            events = [_key_input(0x0D, False), _key_input(0x0D, True)]
        elif ch == "\t":
            events = [_key_input(0x09, False), _key_input(0x09, True)]
        else:
            data = ch.encode("utf-16-le")
            units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
            events = ([_unicode_input(u, False) for u in units] +
                      [_unicode_input(u, True) for u in units])
        _send(events)
        if interval:
            time.sleep(interval)
