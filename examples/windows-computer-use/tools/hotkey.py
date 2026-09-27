"""
Hotkey and single-key press tool. Pure keyboard shortcuts, no text typing.
For text input use type_into().
"""

from utils.tool_requirements import Requires

# Declared before the Windows-only imports: when they fail, the health check
# still explains why instead of showing a bare import error.
_HINT = "Run on Windows with: pip install -r examples/windows-computer-use/requirements.txt"
TOOL_REQUIREMENTS = {
    "hotkey": Requires(platform="win32", hint=_HINT),
}


from agents import function_tool
from _win_input import press_combo


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
        keys: Key expression using key names (lowercase), e.g. ctrl, alt, shift,
              win, enter, esc, tab, f5, a-z, 0-9.
    """
    steps = [s.strip() for s in keys.split(",") if s.strip()]
    if not steps:
        return "Error: empty key string."
    try:
        for step in steps:
            parts = [p.strip().lower() for p in step.split("+") if p.strip()]
            press_combo(*parts)
        return f"OK: pressed {keys}"
    except Exception as e:
        return f"Error: {e}"
