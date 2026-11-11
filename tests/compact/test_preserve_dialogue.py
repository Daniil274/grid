"""Dialogue and hot-tool-tail preservation across a compaction.

A compaction replaces the technical bulk - tool calls and their results - with
a summary, but the conversation itself (what the user asked, what the agent
answered) stays in the session beside it, and a turn compacted between its own
tool steps keeps the newest results it is working from. Both are bounded so the
compacted session always ends up under the threshold and smaller than it was.
"""

import pytest

from core.context import COMPACTED_SUMMARY_NOTE
from core.context_budget import (
    CONTINUED_SESSION_NOTE,
    OVERFLOW_RETRY_NOTE,
    PREVIOUS_COMPACTION_NOTE,
    item_tokens,
    preserved_dialogue_items,
    recent_turn_tool_tail,
    request_tokens,
)


def auto_run_user(note="", request="Fix the bug"):
    """A stored user message with the turn's auto-run preamble in front of it."""
    text = (
        "=== AUTO-RUN TOOL RESULT 'file_read' (auto) ===\nThe file's contents.\n\n"
        "=== AUTO-RUN TOOL RESULT 'pytest' (auto) ===\n3 passed.\n"
    )
    if note:
        text += note
    text += f"\n[Current user request]\n{request}"
    return {"role": "user", "content": text}


def test_the_dialogue_is_kept_clean_beside_the_summary():
    """User messages lose their injected tool results, assistant answers their
    legacy service footer; both stay in the session a compaction writes."""
    items = [
        {"type": "reasoning", "summary": "thinking"},
        auto_run_user(),
        {
            "role": "assistant",
            "content": f"Done.\n\nContext ID: ctx-0a1b2c3d4e5f",
        },
        {"type": "function_call", "call_id": "c1", "name": "file_read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "file"},
        {"role": "user", "content": "Go on"},
    ]

    preserved = preserved_dialogue_items(items)

    assert [item["content"] for item in preserved] == ["Fix the bug", "Done.", "Go on"]
    assert all(item.get("type", "message") == "message" for item in preserved)


def test_the_preamble_leaves_the_missed_exchange_in_place():
    """What the notes before the request hold besides injected tool results -
    the exchange the agent missed - stays with the user's own words."""
    note = "[Meanwhile in this conversation - you did not see these messages; the user talked to another agent:]\n\nAgent coder:\nI fixed it.\n"
    preserved = preserved_dialogue_items([auto_run_user(note=note)])

    (content,) = [item["content"] for item in preserved]
    assert "I fixed it." in content
    assert "[Current user request]" not in content
    assert content.endswith("Fix the bug")


def test_summaries_retry_notes_and_empty_messages_are_not_dialogue():
    """A previous compaction's summary, an overflow-rerun note and an empty
    message never come back as dialogue beside a new summary."""
    items = [
        {"role": "user", "content": f"{COMPACTED_SUMMARY_NOTE}\n\nthe earlier summary"},
        {"role": "user", "content": CONTINUED_SESSION_NOTE + " Continue."},
        {"role": "user", "content": f"{PREVIOUS_COMPACTION_NOTE}\nmerged ledger"},
        {"role": "user", "content": OVERFLOW_RETRY_NOTE},
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "the real request"},
    ]

    assert [item["content"] for item in preserved_dialogue_items(items)] == ["the real request"]


def test_images_survive_the_preamble_strip():
    """A multimodal user message keeps its images; only the text is cleaned."""
    items = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": auto_run_user()["content"]},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        }
    ]

    (preserved,) = preserved_dialogue_items(items)
    assert preserved["content"][0]["text"] == "Fix the bug"
    assert preserved["content"][1]["type"] == "input_image"


def test_no_budget_keeps_every_dialogue_message():
    items = [{"role": "user", "content": "x" * 100_000}] * 20
    assert len(preserved_dialogue_items(items, budget=0)) == 20


def test_a_big_dialogue_fits_the_budget_by_clipping_old_answers():
    """The newest messages and every user message stay verbatim; an older
    assistant answer is clipped head-and-tail until the dialogue fits."""
    items = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a" * 100_000},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2 stays whole"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3 stays whole"},
    ]

    preserved = preserved_dialogue_items(items, budget=2_000, preserve_recent_messages=2)

    assert [item["content"] for item in preserved if item["role"] == "user"] == ["u1", "u2", "u3"]
    assert preserved[1]["content"].startswith("a" * 100)
    assert "more characters]" in preserved[1]["content"]
    assert len(preserved[1]["content"]) < 2_200
    assert preserved[3]["content"] == "a2 stays whole"
    assert preserved[5]["content"] == "a3 stays whole"
    assert request_tokens(preserved) <= 2_000


def test_hundreds_of_turns_drop_the_oldest_pairs_to_fit():
    """When clipping cannot shrink enough, the oldest pairs go: the newest
    dialogue fits the budget, whatever the conversation's length."""
    items = []
    for n in range(20):
        items.append({"role": "user", "content": f"question {n}"})
        items.append({"role": "assistant", "content": "answer " * 2_000})

    preserved = preserved_dialogue_items(items, budget=12_000)

    assert request_tokens(preserved) <= 12_000
    assert preserved[-1]["content"] == "answer " * 2_000  # the newest pair stays whole
    assert preserved[-2]["content"] == "question 19"
    assert len(preserved) < 40


def test_a_monstrous_recent_message_is_clipped_too():
    """A single message bigger than the whole budget is clipped head-and-tail;
    the dialogue cannot be smaller than its own newest message."""
    items = [
        {"role": "user", "content": "u" * 40_000},
        {"role": "assistant", "content": "a" * 40_000},
    ]

    preserved = preserved_dialogue_items(items, budget=1_000)

    assert request_tokens(preserved) <= 1_000
    assert len(preserved) == 1  # the newest survives, clipped to its head and tail
    assert preserved[0]["content"].startswith("a" * 100)
    assert "more characters]" in preserved[0]["content"]


def test_the_tool_tail_keeps_the_newest_matched_pairs_of_the_turn():
    """The active turn's newest call/output pairs stay beside the summary, every
    call with its matching output - no orphan of either, nothing before the
    turn's request."""
    items = [
        {"role": "user", "content": "old request"},
        {"type": "function_call", "call_id": "old", "name": "file_read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "old", "output": "old result"},
        {"role": "user", "content": "the running request"},
        {"type": "function_call", "call_id": "c1", "name": "pytest", "arguments": '{"k": 1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "1 failed"},
        {"type": "function_call", "call_id": "c2", "name": "file_read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "the file"},
        {"type": "function_call", "call_id": "c3", "name": "bash", "arguments": "{}"},  # no output yet
    ]

    tail = recent_turn_tool_tail(items)

    assert [(item["type"], item.get("call_id")) for item in tail] == [
        ("function_call", "c1"),
        ("function_call_output", "c1"),
        ("function_call", "c2"),
        ("function_call_output", "c2"),
    ]
    assert all(item.get("output") != "old result" for item in tail)


def test_the_tool_tail_never_takes_an_orphan_call_or_output():
    """A call without its output and an output without its call are both left
    out: the model's API rejects an orphan of either."""
    items = [
        {"role": "user", "content": "the running request"},
        {"type": "function_call", "call_id": "c1", "name": "pytest", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "function_call_output", "call_id": "c9", "output": "orphan output"},
    ]

    tail = recent_turn_tool_tail(items)

    assert [item.get("call_id") for item in tail] == ["c1", "c1"]
    assert all(item.get("call_id") != "c9" for item in tail)

    items = [
        {"role": "user", "content": "the running request"},
        {"type": "function_call", "call_id": "c1", "name": "pytest", "arguments": "{}"},
    ]
    assert recent_turn_tool_tail(items) == []


def test_the_tool_tail_clips_a_huge_result_to_fit_its_budget():
    """A huge result is clipped head-and-tail until the whole tail fits: the
    end of an output - a test summary, a traceback - is what matters most."""
    items = [
        {"role": "user", "content": "the running request"},
        {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": '{"command": "pytest"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 400_000 + "\n3 failed"},
    ]

    tail = recent_turn_tool_tail(items, max_tokens=1_000)

    assert len(tail) == 2
    output = tail[1]["output"]
    assert len(output) < 400_000
    assert "more characters]" in output
    assert output.endswith("3 failed")  # the tail of the output survives
    assert request_tokens(tail) <= 1_000


def test_the_tool_tail_drops_older_pairs_it_cannot_shrink_small_enough():
    """Pairs that cannot be clipped below the budget drop from the oldest kept
    first; the newest pair survives with its output intact."""
    huge = {"type": "function_call_output", "call_id": "c1", "output": "y" * 400_000}
    items = [
        {"role": "user", "content": "the running request"},
        {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "z" * 100_000},
        huge,
        {"type": "function_call", "call_id": "c2", "name": "pytest", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "3 passed"},
    ]

    tail = recent_turn_tool_tail(items, max_pairs=2, max_tokens=10)

    assert [item.get("call_id") for item in tail] == ["c2", "c2"]
    assert tail[1]["output"] == "3 passed"


def test_the_tool_tail_is_empty_without_a_token_budget():
    items = [
        {"role": "user", "content": "the running request"},
        {"type": "function_call", "call_id": "c1", "name": "pytest", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    assert recent_turn_tool_tail(items, max_tokens=0) == []
