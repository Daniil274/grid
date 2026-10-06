"""
Session scratchpad — persistent working memory for long multi-step tasks.
Survives context compaction. Stores only verified facts, not reasoning noise.
"""

from pathlib import Path
from agents import function_tool
from utils import confined_fs
from utils.path_utils import resolve_agent_path_auto


def _ensure() -> Path:
    """scratchpad.md in the agent working directory, like every other tool path."""
    path = Path(resolve_agent_path_auto("scratchpad.md"))
    confined_fs.make_dirs(path.parent)
    return path


@function_tool
def scratchpad_write(content: str) -> str:
    """
    Overwrite the scratchpad with a checkpoint summary.
    Call after completing each major task block.

    Args:
        content: Summary of current state (replaces all previous content).
    """
    confined_fs.write_text(_ensure(), content)
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
    return confined_fs.read_text(path)


@function_tool
def scratchpad_append(line: str) -> str:
    """
    Append one completed step or discovered fact to the scratchpad.

    Args:
        line: Single fact or step result to append.
    """
    with confined_fs.open_file(_ensure(), "a", encoding="utf-8") as f:
        f.write("\n" + line)
    return "Appended."


# Where these tools act (utils.tool_isolation): a file inside the run's workspace
from utils.tool_isolation import WORKSPACE as _WORKSPACE  # noqa: E402

TOOL_ISOLATION = {
    "scratchpad_write": _WORKSPACE,
    "scratchpad_read": _WORKSPACE,
    "scratchpad_append": _WORKSPACE,
}

# What these tools do (utils.tool_effects), for the action policy
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {
    "scratchpad_write": _effects.write(),
    "scratchpad_read": _effects.read(),
    "scratchpad_append": _effects.write(),
}
