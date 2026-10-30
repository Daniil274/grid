"""One explicit way for a run to say something to people while it works.

A run's last message is its answer, and what it says between actions is
narration for its trace. A *sub-agent*'s prose is neither: it is a tool
result its caller reads, so it never reaches the conversation by itself. On
the Responses API a provider can mark such words ``commentary``; over Chat
Completions and the Claude CLI it cannot. This tool closes that gap: the run
asks to be heard, by name and with the text it chose.

The text becomes a typed public communication of the turn
(``web_chat.observer``, :class:`EventKind.MESSAGE`) under the speaking run's
own author and its place in the timeline - live, in the stored message, and in
a later replay. Because it is a tool call and not a message phase, every
provider reaches the same channel with the same result.

Nothing else is published: not reasoning, not a tool payload, not a report to
a caller, not output nobody labelled. A view with no public channel (the
console, an ``init_tools`` run) answers that nothing was sent instead of
pretending it was.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from agents import RunContextWrapper, function_tool

from utils import tool_effects as effects
from utils.tool_isolation import WORKSPACE

#: The tool's name, as the model calls it and the trace shows it.
SAY_PUBLIC = "say_public"


def _observer(context: RunContextWrapper) -> Optional[Any]:
    """The view this run reports into, if it has one (core.factory.run_context)."""
    raw = getattr(context, "context", None)
    return getattr(raw, "stream_observer", None)


def _answer(sent: bool, **fields: Any) -> str:
    return json.dumps({"sent": sent, **fields}, ensure_ascii=False)


async def say_public_tool(context: RunContextWrapper, text: str) -> str:
    """Publish *text* as a public message of this run, or say why not."""
    if not (text or "").strip():
        return _answer(False, reason="Nothing was sent: the text was empty.")
    view = _observer(context)
    publish = getattr(view, "record_public_message", None)
    if publish is None:
        return _answer(
            False,
            reason="This run has no public channel, so the text was not shown to anyone.",
        )
    event = publish(text)
    if event is None:
        return _answer(False, reason="Nothing was sent: this run has no public channel.")
    return _answer(True, id=event.id, author=event.author, chars=len(event.content or ""))


@function_tool
async def say_public(context: RunContextWrapper, text: str) -> str:
    """Say something to the people in this chat now, before your final answer.

    Use it for a finding, a question or a short update the user should see
    while the work goes on: the text appears on their screen at once, as a
    message from you, and stays in the chat's history. In a run started by
    another agent it is the one thing you say that people read directly - the
    rest you produce is a report to your caller. Do not use it for your answer
    (that is your last message), for reasoning, or for internal notes.
    """
    return await say_public_tool(context, text)


MESSAGE_TOOLS = {SAY_PUBLIC: say_public}
#: It says something; it touches nothing - no file, no machine, no network.
TOOL_ISOLATION = {SAY_PUBLIC: WORKSPACE}
TOOL_EFFECTS = {SAY_PUBLIC: effects.read()}
