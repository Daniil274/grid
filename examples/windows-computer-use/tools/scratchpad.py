"""
Session scratchpad — persistent working memory for long multi-step tasks.
Survives context compaction. Stores only verified facts, not reasoning noise.
"""

from pathlib import Path
from agents import function_tool
from utils.path_utils import resolve_agent_path_auto


def _ensure() -> Path:
    """scratchpad.md in the agent working directory, like every other tool path."""
    path = Path(resolve_agent_path_auto("scratchpad.md"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@function_tool
def scratchpad_write(content: str) -> str:
    """
    Overwrite the scratchpad with a checkpoint summary.
    Call after completing each major task block.

    Args:
        content: Summary of current state (replaces all previous content).
    """
    _ensure().write_text(content, encoding="utf-8")
    return f"Scratchpad saved ({len(content)} chars)."


@function_tool
def scratchpad_read() -> str:
    """
    Read the scratchpad. Call at session start and after context compaction
    to restore task progress and verified facts.
    """
    path = _ensure()
    if not path.exists() or path.stat().st_size == 0:
        return "Scratchpad is empty."
    return path.read_text(encoding="utf-8")


@function_tool
def scratchpad_append(line: str) -> str:
    """
    Append one completed step or discovered fact to the scratchpad.

    Args:
        line: Single fact or step result to append.
    """
    with _ensure().open("a", encoding="utf-8") as f:
        f.write("\n" + line)
    return "Appended."
