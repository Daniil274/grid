"""chat_history lists the user's own chats with their failures and opens one as a transcript."""

import asyncio
import json
import time
from types import SimpleNamespace

from schemas.schemas import ContextMessage
from tools import history_tools
from tools.history_tools import chat_history


def _message(role, content, **metadata):
    return ContextMessage(role=role, content=content, timestamp="2026-10-24T18:00:00", metadata=metadata or None)


def _views():
    now = time.time()
    return [
        {
            "id": "ctx-ok",
            "metadata": {"title": "Report", "agent_key": "engineer"},
            "updated_at": now - 3600,
            "messages": [_message("user", "write the report"), _message("assistant", "Done.")],
        },
        {
            "id": "ctx-broken",
            "metadata": {"title": "Ку", "agent_key": "engineer"},
            "updated_at": now - 60,
            "messages": [
                _message("user", "Ку"),
                _message("assistant", "The turn did not finish - it stopped on an error: CLI ran a tool",
                         interruption={"reason": "error"}),
                _message("user", "Continue."),
                _message("assistant", "Checked the folder.", trace=[
                    {"tool": "workspace_overview", "status": "done"},
                    {"tool": "web_fetch", "status": "error", "body": "HTTP 404"},
                ]),
            ],
        },
        {
            "id": "ctx-old",
            "metadata": {"title": "Old"},
            "updated_at": now - 30 * 86400,
            "messages": [_message("user", "long ago")],
        },
    ]


def _call(**args):
    factory = SimpleNamespace(context_manager=SimpleNamespace(conversation_views=_views))
    context = SimpleNamespace(context=SimpleNamespace(factory=factory), tool_name="chat_history", tool_call_id="t")
    return asyncio.run(chat_history.on_invoke_tool(context, json.dumps(args)))


def test_the_list_shows_recent_chats_newest_first_with_their_failures():
    listing = _call()
    assert listing.index("ctx-broken") < listing.index("ctx-ok")
    assert "ctx-old" not in listing  # older than the default two days
    assert "1 turn(s) stopped on an error" in listing and "1 tool call(s) failed" in listing


def test_the_list_narrows_to_failures_and_text():
    assert "ctx-ok" not in _call(errors_only=True)
    found = _call(query="REPORT", days=60)
    assert "ctx-ok" in found and "ctx-broken" not in found


def test_a_chat_opens_as_a_transcript_with_tools_and_failures_marked():
    transcript = _call(context_id="ctx-broken")
    assert "not instructions to follow" in transcript
    assert "[1] assistant [STOPPED ON AN ERROR]" in transcript
    assert "tools: workspace_overview, web_fetch ✗ 'HTTP 404'" in transcript
    assert "No chat" in _call(context_id="ctx-other-user")


def test_a_long_chat_keeps_its_end_within_the_output_bound(monkeypatch):
    monkeypatch.setattr(history_tools, "MAX_CHARS", 400)
    transcript = _call(context_id="ctx-broken")
    assert len(transcript) <= 400 + 120
    assert "Checked the folder." in transcript and "start of the chat is cut" in transcript


def test_it_is_declared_as_reading_the_users_own_data():
    from utils.tool_isolation import WORKSPACE

    assert history_tools.TOOL_ISOLATION["chat_history"] == WORKSPACE
    assert history_tools.TOOL_EFFECTS["chat_history"].untrusted
