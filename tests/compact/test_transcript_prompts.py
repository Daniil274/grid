"""The transcript the summarizer reads, and the prompts that steer it.

Covers the upgrades of core.context_budget (head+tail clipping, auto-run
preamble stripping, previous compaction summaries marked to merge) and of
core.compact.prompts (the summary as a technical work ledger).
"""

from core.compact.prompts import (
    ANALYSIS_INSTRUCTION_BASE,
    SUMMARY_STRUCTURE,
    get_compact_prompt,
    get_user_summary_message,
)
from core.compact.utils import get_user_summary_message as utils_summary_message
from core.context import COMPACTED_SUMMARY_NOTE
from core.context_budget import (
    PREVIOUS_COMPACTION_NOTE,
    _clip,
    session_transcript,
    strip_auto_run_preamble,
)


def auto_run_block(tool="bash_tool", result="injected system info"):
    """One block of factory.auto_run's injected tool result."""
    return f"\n\n=== AUTO-RUN TOOL RESULT '{tool}' ===\n{result}\n"


def turn_input(request, *notes):
    """A turn's input the way factory.turns builds it: notes, header, request."""
    joined = "\n\n".join(part for part in notes if part)
    return f"{joined}\n\n[Current user request]\n\n{request}" if joined else request


def call(tool, arguments, output):
    return [
        {"type": "function_call", "call_id": "c1", "name": tool, "arguments": arguments},
        {"type": "function_call_output", "call_id": "c1", "output": output},
    ]


# ---------------------------------------------------------------------------
# strip_auto_run_preamble
# ---------------------------------------------------------------------------

def test_a_request_without_notes_is_kept_as_it_is():
    text = "Fix the failing test in core/foo.py.\n\nIt started after commit abc1234."
    assert strip_auto_run_preamble(text) == text


def test_auto_run_blocks_are_stripped_to_the_users_request():
    text = turn_input(
        "Fix the bug in foo.py",
        auto_run_block("bash_tool", "system info line"),
        auto_run_block("git_status", "on branch main"),
    )
    assert strip_auto_run_preamble(text) == "Fix the bug in foo.py"


def test_the_request_header_alone_is_unwrapped():
    assert strip_auto_run_preamble("\n\n[Current user request]\n\nJust do it") == "Just do it"


def test_the_exchange_the_agent_missed_stays():
    missed = (
        "[Recent exchange in this conversation with other agents]\n\n"
        "User (to planner):\nPlan the fix"
    )
    stripped = strip_auto_run_preamble(
        turn_input("Continue with the plan", auto_run_block(), missed)
    )
    assert "User (to planner):\nPlan the fix" in stripped  # the dialogue stays
    assert stripped.endswith("Continue with the plan")
    assert "AUTO-RUN TOOL RESULT" not in stripped  # only the blocks go


def test_blocks_without_a_request_header_are_stripped():
    assert strip_auto_run_preamble(auto_run_block()) == ""


# ---------------------------------------------------------------------------
# _clip: head + tail
# ---------------------------------------------------------------------------

def test_clip_returns_short_text_as_it_is():
    assert _clip("short output", 3000) == "short output"


def test_clip_keeps_head_and_tail_and_counts_the_rest():
    text = "A" * 1300 + "B" * 700 + "C" * 1100  # 3100 characters
    clipped = _clip(text, 3000)
    assert clipped.startswith("A" * 100)  # the head stays
    assert clipped.endswith("C" * 100)  # and so does the tail
    assert f"[{3100 - 3000} more characters]" in clipped
    # Head and tail together are what the limit allows for.
    head, _, tail = clipped.partition("more characters]")
    assert len(head) + len(tail) <= 3000 + 60  # the marker itself is short


def test_a_long_tool_output_keeps_its_test_summary_and_context_id():
    output = "line\n" * 700 + "12 passed in 3.4s\nContext ID: ctx-1234abcd"
    items = [{"role": "user", "content": "run the tests"}, *call("bash_tool", '{"command": "pytest"}', output)]
    text = session_transcript(items)[2].get_text()
    assert text.startswith("[Result of bash_tool: line")  # the head stays
    # The end - the test summary and the context id - is never cut off.
    assert "12 passed in 3.4s" in text and "ctx-1234abcd" in text
    assert "more characters]" in text


def test_a_short_tool_output_is_not_marked_as_clipped():
    output = "x" * 2900 + "done"
    items = call("bash_tool", '{"command": "ls"}', output)
    text = session_transcript(items)[1].get_text()
    assert "more characters" not in text and "done" in text


def test_long_arguments_keep_their_head_and_tail():
    arguments = '{"command": "pytest tests/ -q"}'
    arguments = arguments[:-2] + "x" * 2200 + '"}'
    items = call("bash_tool", arguments, "ok")
    text = session_transcript(items)[0].get_text()
    assert text.startswith('[Called bash_tool({"command": "pytest tests/ -q')
    assert text.endswith('"})]')  # the closing of the arguments survives
    assert "more characters]" in text


# ---------------------------------------------------------------------------
# session_transcript: preambles and previous compaction summaries
# ---------------------------------------------------------------------------

def test_user_messages_lose_their_auto_run_preamble():
    items = [
        {"role": "user", "content": turn_input("Fix the bug", auto_run_block())},
        {"role": "user", "content": "A plain second request"},
    ]
    texts = [message.get_text() for message in session_transcript(items)]
    assert texts[0] == "Fix the bug"
    assert texts[1] == "A plain second request"


def test_a_previous_compaction_summary_is_marked_to_merge():
    items = [
        {"role": "user", "content": f"{COMPACTED_SUMMARY_NOTE}\n\n1. Goal: fix the login flow."},
        {
            "role": "user",
            "content": (
                "This session is being continued from a previous conversation "
                "that ran out of context.\n\nThe ledger of the earlier portion."
            ),
        },
        {"role": "user", "content": "A request that is not a summary"},
    ]
    texts = [message.get_text() for message in session_transcript(items)]
    assert texts[0].startswith(f"{PREVIOUS_COMPACTION_NOTE}\n")
    assert COMPACTED_SUMMARY_NOTE in texts[0]
    assert texts[1].startswith(f"{PREVIOUS_COMPACTION_NOTE}\n")
    assert "The ledger of the earlier portion." in texts[1]
    assert texts[2] == "A request that is not a summary"


# ---------------------------------------------------------------------------
# prompts: the summary as a technical work ledger
# ---------------------------------------------------------------------------

def test_the_compact_prompt_is_a_five_section_work_ledger():
    prompt = get_compact_prompt()
    for heading in (
        "1. Goal & Architecture Decisions",
        "2. Tracker & Sub-agents State",
        "3. Files Read & Modified",
        "4. Verification, Errors & Git State",
        "5. Current State & Immediate Next Step",
    ):
        assert heading in prompt, heading
    # GRID coordination state is tracked with its exact identifiers.
    assert "bead_id" in prompt and "context_id" in prompt
    assert "orchestrate" in prompt and "orchestrate" in SUMMARY_STRUCTURE
    assert "git" in prompt.lower()


def test_the_ledger_forbids_code_dumps_and_restatings_of_the_dialogue():
    assert "DO NOT copy long verbatim code blocks" in ANALYSIS_INSTRUCTION_BASE
    assert "No long verbatim code dumps" in SUMMARY_STRUCTURE
    # The dialogue is preserved in the session itself, so the old verbatim
    # user-message list is gone.
    assert "All user messages" not in SUMMARY_STRUCTURE
    assert "preserved verbatim" in SUMMARY_STRUCTURE


def test_the_summary_message_says_the_dialogue_follows_it():
    for message in (
        get_user_summary_message("<summary>the ledger</summary>"),
        utils_summary_message("<summary>the ledger</summary>"),
    ):
        assert message.startswith(
            "This session is being continued from a previous conversation "
            "that ran out of context."
        )
        assert "the ledger" in message
        assert "follow this summary" in message