"""Measuring a request on session items and keeping it inside the window."""

import copy

from core.context_budget import (
    CLEARED_NOTE,
    IMAGE_TOKENS,
    clear_old_tool_outputs,
    context_budget_filter,
    item_tokens,
    request_tokens,
    session_transcript,
)
from schemas import CompactConfig


def call(n, tool="perceive", output="z" * 4000):
    return [
        {"type": "function_call", "call_id": f"c{n}", "name": tool, "arguments": '{"window": ""}'},
        {"type": "function_call_output", "call_id": f"c{n}", "output": output},
    ]


def steps(count, tool="perceive"):
    items = [{"role": "user", "content": "Open Notepad"}]
    for n in range(count):
        items += call(n, tool)
    return items


def test_tool_results_and_images_count_not_only_the_chat():
    text_only = [{"role": "user", "content": "hi"}]
    assert request_tokens(text_only) < 5
    with_output = call(0, output="a" * 4000)
    assert request_tokens(with_output) >= 1000
    image = {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 900_000}]}
    # An image costs IMAGE_TOKENS, not its base64 length (plus the role word).
    assert IMAGE_TOKENS <= item_tokens(image) < IMAGE_TOKENS + 10
    assert request_tokens(text_only, "x" * 400) >= 100  # instructions count too


def test_old_outputs_are_cleared_oldest_first_until_the_request_fits():
    items = steps(10)
    budget = request_tokens(items) - 3000
    cleared = clear_old_tool_outputs(items, budget=budget, keep_last=3, tools={"perceive"})
    outputs = [item["output"] for item in cleared if item.get("type") == "function_call_output"]
    note = CLEARED_NOTE.format(tool="perceive")
    assert outputs[0] == note  # the oldest goes first
    assert outputs[-1] != note  # the newest stays
    assert request_tokens(cleared) <= budget
    # Only as many as needed: later outputs are still whole.
    assert outputs.count(note) < 7


def test_the_newest_outputs_and_other_tools_are_never_cleared():
    items = steps(6) + call(99, tool="click", output="c" * 8000)
    cleared = clear_old_tool_outputs(items, budget=1, keep_last=2, tools={"perceive"})
    outputs = {item["call_id"]: item["output"] for item in cleared if item.get("type") == "function_call_output"}
    assert outputs["c4"] == outputs["c5"] == "z" * 4000
    assert outputs["c99"] == "c" * 8000  # not compactable
    assert all(outputs[f"c{n}"].startswith("[Output of perceive cleared") for n in range(4))


def test_the_session_items_are_never_modified():
    items = steps(8)
    before = copy.deepcopy(items)
    cleared = clear_old_tool_outputs(items, budget=10, keep_last=1, tools=None)
    assert items == before
    assert cleared[-1] is items[-1]


def test_within_budget_the_same_list_is_returned():
    items = steps(2)
    assert clear_old_tool_outputs(items, budget=10**9, keep_last=0, tools=None) is items


def test_the_filter_follows_the_compact_config():
    enabled = CompactConfig(micro={"enabled": True, "preserve_last_n": 1, "compactable_tools": ["perceive"]})
    heard = []
    apply = context_budget_filter(10_000, enabled, on_cleared=heard.append)
    sent = apply(steps(40), "instructions")
    assert request_tokens(sent, "instructions") < request_tokens(steps(40), "instructions")
    assert heard and heard[0] == sum(1 for before, after in zip(steps(40), sent) if before != after)
    assert context_budget_filter(10**9, enabled, on_cleared=heard.append)(steps(2), None) == steps(2)
    assert len(heard) == 1  # nothing left out, nothing heard
    assert context_budget_filter(10_000, CompactConfig(micro={"enabled": False})) is None
    assert context_budget_filter(10_000, CompactConfig(enabled=False)) is None


def test_the_summarizer_reads_calls_and_results_clipped():
    items = [
        {"role": "user", "content": "Find the bug"},
        {"type": "reasoning", "summary": [{"text": "thinking"}]},
        *call(1, tool="grep_tool", output="match " * 2000),
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Found it."}]},
        {"role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": "data:x"}]},
    ]
    transcript = session_transcript(items)
    texts = [message.get_text() for message in transcript]
    assert texts[0] == "Find the bug"
    assert texts[1].startswith("[Called grep_tool(")
    assert texts[2].startswith("[Result of grep_tool: match") and "more characters]" in texts[2]
    assert texts[3] == "Found it."
    assert texts[4] == "look\n[image]"
    assert all("thinking" not in text for text in texts)
