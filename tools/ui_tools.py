"""One tool that puts a working interface inside the answer.

``present_ui`` is the model's only way to build a UI block, and it carries data:
a declarative document from a small catalog (``web_chat.ui_document``). The
server checks it whole before anything is stored or sent - components, fields,
bindings, actions, limits - and the client renders only what the check let
through. No HTML, no JavaScript, no expression evaluation reaches a browser.

The block belongs to the turn, not to this tool's payload: it is published to
the viewer at once and stored on the answer (``web_chat.ui_state``), so a
reload shows it again, and a tool payload trimmed by compaction cannot take it
away. A run with no viewer - the console, a sub-agent's internal report - is
told so instead of pretending it showed something.

What the model gets back is a short acknowledgement with the block's id and the
actions it may expect; the document itself is already on its way to the screen.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from agents import RunContextWrapper, function_tool

from utils import tool_effects as effects
from utils.tool_isolation import WORKSPACE
from web_chat.ui_document import UiDocumentError

#: The tool's name, as the model calls it and the trace shows it.
PRESENT_UI = "present_ui"


def _observer(context: RunContextWrapper) -> Optional[Any]:
    """The view this run reports into, if it has one (core.factory.run_context)."""
    raw = getattr(context, "context", None)
    return getattr(raw, "stream_observer", None)


def _answer(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def present_ui_document(context: RunContextWrapper, document: Any) -> str:
    """Validate and publish *document*; the model-facing result as JSON text."""
    view = _observer(context)
    present = getattr(view, "present_ui", None)
    if present is None:
        return _answer(
            ok=False,
            code="ui.unavailable",
            message="This run has no interface channel, so nothing was shown. Answer in text instead.",
        )
    try:
        block = present(document)
    except UiDocumentError as exc:
        return _answer(ok=False, code=exc.code, message=exc.message)
    except Exception as exc:  # a bad document must never break the turn
        return _answer(ok=False, code="ui.failed", message=str(exc))
    doc = block["document"]
    return _answer(
        ok=True,
        ui_id=block["id"],
        revision=block["revision"],
        actions=[{"id": key, "label": value["label"]} for key, value in doc["actions"].items()],
        note="The interface is shown below your answer. Its sorting and selection work locally; "
        "its buttons arrive as the user's next message.",
    )


@function_tool
async def present_ui(context: RunContextWrapper, document: str) -> str:
    """Show a small working interface inside this answer, built from a checked catalog.

    ``document`` is a JSON-encoded string holding one object (not HTML and not
    JavaScript). Encode the object as JSON text; this schema is a plain string
    so a strict provider accepts it:

    {
      "schema_version": 1, "id": "comparison", "revision": 1,
      "fallback_markdown": "the same comparison as text, for clients without UI",
      "state_schema": {"sort": {"type": "sort", "table": "options", "label": "Sort by"},
                       "choice": {"type": "row", "table": "options", "label": "Chosen option"}},
      "initial_state": {"sort": "price:asc", "choice": "a"},
      "root": {"id": "layout", "type": "stack", "children": [
        {"id": "sort", "type": "select", "label": "Sort by", "bind": "sort",
         "options": [{"value": "price:asc", "label": "Price ↑"}, {"value": "price:desc", "label": "Price ↓"}]},
        {"id": "options", "type": "table", "columns": [{"key": "name", "label": "Option"},
                                                       {"key": "price", "label": "Price", "align": "end"}],
         "rows": [{"id": "a", "name": "Basic", "price": 10}, {"id": "b", "name": "Pro", "price": 25}],
         "row_label": "name", "sort_state": "sort", "select_state": "choice"},
        {"id": "discuss", "type": "button", "label": "Discuss the chosen option", "action": "discuss", "tone": "primary"}
      ]},
      "actions": {"discuss": {"kind": "submit_to_agent", "label": "Discuss the chosen option",
                              "prompt": "Discuss the option I chose in this comparison."}}
    }

    Components: stack, text, metric, table, select, button. Limits: at most 300
    nodes, depth 8, 100 rows, 12 columns, 128 KB. Unknown components or fields,
    HTML, JavaScript and URLs are refused. Use it when comparing options,
    choosing a criterion or confirming a short form is clearer than prose - not
    for every answer. Sorting and selecting happen on the user's device without
    a model call; a button starts a normal turn carrying exactly the values the
    user chose.
    """
    return present_ui_document(context, document)


UI_TOOLS = {PRESENT_UI: present_ui}
#: It draws in the answer; it touches nothing - no file, no machine, no network.
TOOL_ISOLATION = {PRESENT_UI: WORKSPACE}
TOOL_EFFECTS = {PRESENT_UI: effects.read()}
