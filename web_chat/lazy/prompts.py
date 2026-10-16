"""What a step asks of the agent: the message that starts its turn.

Every step runs in the task's own conversation, so the agent sees what the
earlier steps did. The message still restates the task, the step's job and
its budget: a long conversation may have been compacted since, and each step
must be able to start from its message alone. Each answer ends with a
``Checkpoint`` - what is done, what is verified, what is left - for the next
step and for the user's page; a scout names the size it found, a verifier its
verdict, and the service reads both (core.lazy.estimate).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from core.lazy.estimate import SIZE_LABELS
from core.lazy.model import Step, Task

JOBS = {
    "scout": (
        "Look just enough to plan the work: find what it touches, what is unclear and what could go wrong. "
        "Do not do the work yet.\n"
        "End with the plan in a few lines, then a line `Size: S`, `Size: M` or `Size: L` for the work it needs "
        "(S - a small change, M - an hour of focused work, L - a large or uncertain job)."
    ),
    "work": "Do the work as planned. Keep to the task; leave anything optional for its own step.",
    "extra": "Do the optional part. It is the first thing to drop: stop at a clean point if it grows.",
    "verify": (
        "Check the result against what \"done\" means for this task - run the tests and checks it needs, "
        "read what was produced. Fix small problems right away.\n"
        "End with a line `Verdict: pass` when every criterion holds, else `Verdict: fail` and what is still wrong."
    ),
    "fix": "Fix what the last verification found wrong, nothing else.",
}


def _when(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "no fixed deadline"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def step_message(task: Task, step: Step) -> str:
    """The message that starts *step* of *task*."""
    position = [s.id for s in task.steps].index(step.id) + 1 if step in task.steps else len(task.steps)
    lines = [
        f"**Lazy task · step {position} of {len(task.steps)}: {step.title}**",
        "",
        f"**Task:** {task.title}",
    ]
    if task.brief.strip():
        lines += ["", task.brief.strip()]
    if task.acceptance.strip():
        lines += ["", f"**Done when:** {task.acceptance.strip()}"]
    if task.optional.strip():
        lines += ["", f"**Optional part:** {task.optional.strip()}"]
    lines += ["", f"**Deadline:** {_when(task.deadline)} · size: {SIZE_LABELS.get(task.size, task.size)}"]
    done = [s for s in task.steps if s.status == "done" and s.summary]
    if done:
        last = done[-1]
        lines += ["", f"**Where the last step ({last.title.lower()}) left it:**", last.summary]
    lines += [
        "",
        f"**This step:** {JOBS.get(step.kind, JOBS['work'])}",
        "",
        "**Budget:** this runs on a small agreed share of a subscription. "
        f"Aim for about {step.tokens // 1000}k tokens: send independent tool calls together, "
        "prefer tools and scripts to long reasoning, and do not reread what you already know. "
        "If the step outgrows the budget, stop at a safe point - the next run continues from your checkpoint.",
        "",
        "End your answer with a `Checkpoint` section: what is done, what is verified, what is left, the next concrete step.",
    ]
    return "\n".join(lines)
