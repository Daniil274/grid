"""
Session scratchpad — persistent working memory for long multi-step tasks.
Survives context compaction. Stores only verified facts, not reasoning noise.
"""

from pathlib import Path
from agents import function_tool

_FILE = Path(__file__).parent.parent / "workspace" / "scratchpad.md"


def _ensure():
    _FILE.parent.mkdir(parents=True, exist_ok=True)


@function_tool
def scratchpad_write(content: str) -> str:
    """
    Overwrite the scratchpad with a checkpoint summary.
    Call after completing each major task block.

    Args:
        content: Summary of current state (replaces all previous content).
    """
    _ensure()
    _FILE.write_text(content, encoding="utf-8")
    return f"Scratchpad saved ({len(content)} chars)."


@function_tool
def scratchpad_read() -> str:
    """
    Read the scratchpad. Call at session start and after context compaction
    to restore task progress and verified facts.
    """
    _ensure()
    if not _FILE.exists() or _FILE.stat().st_size == 0:
        return "Scratchpad is empty."
    return _FILE.read_text(encoding="utf-8")


@function_tool
def scratchpad_append(line: str) -> str:
    """
    Append one completed step or discovered fact to the scratchpad.

    Args:
        line: Single fact or step result to append.
    """
    _ensure()
    with _FILE.open("a", encoding="utf-8") as f:
        f.write("\n" + line)
    return "Appended."
