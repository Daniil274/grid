"""
ISKOR-window-autotest tools package.
Provides functions for automated UI testing via GUI window capture.
"""

from .ping_device import ping_device
from .key_press import key_press
from .key_hold import key_hold
from .get_screen import get_screen

__all__ = [
    'ping_device',
    'key_press',
    'key_hold',
    'get_screen',
]
