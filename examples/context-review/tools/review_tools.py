"""propose_change: the review agents' one way to write anything.

A proposal is a JSON file in the workbench's ``proposals/`` directory; the
review page (web_chat.review.agents) collects it after the run and stores it
with the review. The fields are checked here, so what reaches the admins is
well formed: a known cause, evidence, and a diff whose paths stay inside
``source/``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import List

from agents import function_tool

from utils import confined_fs
from utils.path_utils import resolve_agent_path_auto

PROPOSALS_DIR = "proposals"
CAUSES = (
    "routing",
    "prompt",
    "tool_description",
    "tool_bug",
    "missing_tool",
    "context",
    "model",
    "policy",
    "framework_bug",
    "user_expectation",
)
CONFIDENCE = ("high", "medium", "low")
MAX_TEXT = 40_000

# "--- a/path" and "+++ b/path" (or /dev/null) of a unified diff.
_DIFF_PATH = re.compile(r"^(?:---|\+\+\+) (\S+)", re.MULTILINE)


def _diff_problems(change: str) -> list[str]:
    """Why *change* is not a unified diff of files under source/; empty when it is."""
    paths = _DIFF_PATH.findall(change)
    if not paths:
        return ["change must be a unified diff with --- a/<path> and +++ b/<path> lines"]
    problems = []
    for path in paths:
        if path == "/dev/null":
            continue
        if not path.startswith(("a/", "b/")):
            problems.append(f"diff path {path!r} must start with a/ or b/")
            continue
        relative = Path(path[2:])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            problems.append(f"diff path {path!r} must stay inside source/")
    return problems


@function_tool
def propose_change(
    title: str,
    cause: str,
    summary: str,
    evidence: List[str],
    change: str = "",
    scenario: str = "",
    confidence: str = "medium",
) -> str:
    """Record one proposal for the admins: a cause, its evidence and, if you have one, the fix.

    Args:
        title: One line: what to fix.
        cause: One of routing, prompt, tool_description, tool_bug, missing_tool,
            context, model, policy, framework_bug, user_expectation.
        summary: What happened and why, in a few sentences.
        evidence: References such as "steps#4", "session#12",
            "source:examples/coder/config.yaml:120".
        change: A unified diff against source/ (paths a/<path> and b/<path>);
            empty when there is no code or config change.
        scenario: How to check the fix: the user's request and what the answer
            must do differently.
        confidence: high, medium or low.
    """
    problems = []
    if not title.strip():
        problems.append("title is empty")
    if cause not in CAUSES:
        problems.append(f"cause must be one of {', '.join(CAUSES)}")
    if not summary.strip():
        problems.append("summary is empty")
    if not [ref for ref in evidence if ref.strip()]:
        problems.append("evidence needs at least one reference")
    if confidence not in CONFIDENCE:
        problems.append(f"confidence must be one of {', '.join(CONFIDENCE)}")
    if change.strip():
        problems.extend(_diff_problems(change))
    if any(len(text) > MAX_TEXT for text in (summary, change, scenario)):
        problems.append(f"summary, change and scenario must each be at most {MAX_TEXT} characters")
    if problems:
        return "❌ Proposal not recorded: " + "; ".join(problems)

    proposal = {
        "title": title.strip()[:300],
        "cause": cause,
        "summary": summary.strip(),
        "evidence": [ref.strip() for ref in evidence if ref.strip()],
        "change": change.strip(),
        "scenario": scenario.strip(),
        "confidence": confidence,
        "created_at": time.time(),
    }
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    path = Path(resolve_agent_path_auto(f"{PROPOSALS_DIR}/{name}"))
    confined_fs.make_dirs(path.parent)
    confined_fs.write_text(path, json.dumps(proposal, ensure_ascii=False, indent=2))
    return f"✅ Proposal recorded: {proposal['title']}"


# Where these tools act (utils.tool_isolation): in the server, inside the run's workspace
from utils.tool_isolation import WORKSPACE as _WORKSPACE  # noqa: E402

TOOL_ISOLATION = {
    "propose_change": _WORKSPACE,
}
