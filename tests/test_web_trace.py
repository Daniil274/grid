"""The web chat's reasoning trace: recording, streaming, and legacy upgrades."""

from types import SimpleNamespace

from agents import RawResponsesStreamEvent, RunItemStreamEvent

from web_chat.observer import WebStreamObserver
from web_chat.trace import (
    StepKind,
    StepStatus,
    TraceRecorder,
    context_refs,
    normalize_steps,
)


def raw(data_type: str, delta: str) -> RawResponsesStreamEvent:
    return RawResponsesStreamEvent(data=SimpleNamespace(type=data_type, delta=delta))


def tool_called(name: str, call_id: str, arguments: str) -> RunItemStreamEvent:
    item = SimpleNamespace(
        raw_item=SimpleNamespace(
            name=name, call_id=call_id, arguments=arguments, type="function_call"
        )
    )
    return RunItemStreamEvent(name="tool_called", item=item)


def tool_output(call_id: str, output: str) -> RunItemStreamEvent:
    item = SimpleNamespace(
        raw_item=SimpleNamespace(call_id=call_id, type="function_call_output"),
        output=output,
    )
    return RunItemStreamEvent(name="tool_output", item=item)


class Harness:
    """Collects everything an observer emits for one turn."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.tokens: list[str] = []
        self.recorder = TraceRecorder(self.events.append)
        self.observer = WebStreamObserver(
            self.recorder, emit_token=self.tokens.append, agent_label="Agent"
        )

    def feed(self, *events) -> None:
        for event in events:
            self.observer.handle_event(event)

    def steps(self) -> list[dict]:
        return self.recorder.snapshot()


def test_reasoning_never_leaks_into_the_answer():
    harness = Harness()
    harness.feed(
        raw("response.reasoning_text.delta", "I should "),
        raw("response.reasoning_text.delta", "read the config."),
        raw("response.output_text.delta", "Here is "),
        raw("response.output_text.delta", "the answer."),
    )

    assert harness.tokens == ["Here is ", "the answer."]
    reasoning = [
        step for step in harness.steps() if step["kind"] == StepKind.REASONING.value
    ]
    assert len(reasoning) == 1
    assert reasoning[0]["body"] == "I should read the config."
    # Visible output ends the thinking step rather than leaving it open forever.
    assert reasoning[0]["status"] == StepStatus.DONE.value


def test_reasoning_summary_deltas_are_treated_as_reasoning():
    harness = Harness()
    harness.feed(raw("response.reasoning_summary_text.delta", "Planning the edit."))

    assert harness.tokens == []
    assert [event["type"] for event in harness.events] == ["step", "reasoning"]


def test_empty_reasoning_step_is_retracted():
    harness = Harness()
    harness.feed(raw("response.reasoning_text.delta", "   "))
    harness.recorder.end_reasoning()

    # Whitespace-only thinking is not worth a row; the client is told to drop it.
    assert harness.steps() == []
    assert harness.events[-1]["type"] == "step_removed"


def test_tool_call_and_output_collapse_into_one_timed_step():
    harness = Harness()
    harness.feed(
        tool_called("read_file", "call-1", '{"path": "core/config.py"}'),
        tool_output("call-1", "file contents"),
    )

    steps = harness.steps()
    assert len(steps) == 1
    step = steps[0]
    assert step["title"] == "Read file"
    assert step["subtitle"] == "core/config.py"
    assert step["status"] == StepStatus.DONE.value
    assert step["body"] == "file contents"
    assert step["duration_ms"] is not None
    assert [ref["label"] for ref in step["refs"]] == ["config.py"]


def test_interleaved_tool_calls_match_their_own_output():
    harness = Harness()
    harness.feed(
        tool_called("search", "a", '{"query": "grid"}'),
        tool_called("read_file", "b", '{"path": "README.md"}'),
        tool_output("b", "readme"),
        tool_output("a", "results"),
    )

    bodies = {step["title"]: step["body"] for step in harness.steps()}
    assert bodies == {"search": "results", "Read file": "readme"}


def test_orphan_tool_output_still_appears():
    harness = Harness()
    harness.feed(tool_output("unknown", "result without a call"))

    steps = harness.steps()
    assert len(steps) == 1
    assert steps[0]["body"] == "result without a call"


def test_mcp_listing_reports_the_tools_it_found():
    harness = Harness()
    item = SimpleNamespace(
        raw_item=SimpleNamespace(
            server_label="codex",
            tools=[SimpleNamespace(name="run"), SimpleNamespace(name="edit")],
        )
    )
    harness.feed(RunItemStreamEvent(name="mcp_list_tools", item=item))

    step = harness.steps()[0]
    assert step["kind"] == StepKind.MCP.value
    assert step["subtitle"] == "2 tools available"
    assert step["body"] == "run\nedit"


def test_policy_decision_is_attached_to_action_without_extra_row():
    harness = Harness()
    harness.feed(tool_called("file_delete", "call-1", '{"path": "old.txt"}'))

    harness.observer.handle_policy_event(
        {
            "rule": "policy_check",
            "decision": "review",
            "mode": "enforce",
            "tool": "file_delete",
            "kind": "function",
            "source": "validator",
            "latency_ms": 0,
            "action": "review",
            "action_sha256": "a" * 64,
        }
    )

    step = harness.steps()[0]
    assert len(harness.steps()) == 1
    assert step["kind"] == StepKind.TOOL.value
    assert step["title"] == "file_delete"
    assert step["tone"] == "warning"
    assert step["policy"]["decision"] == "review"
    assert step["policy"]["label"] == "Policy: review · action"
    assert "file_delete" in step["policy"]["title"]


def test_policy_execution_audit_does_not_create_a_duplicate_step():
    harness = Harness()

    harness.observer.handle_policy_event(
        {"rule": "policy_check", "decision": "executed", "tool": "file_read"}
    )

    assert harness.steps() == []


def test_context_refs_only_trust_known_argument_names():
    refs = context_refs(
        '{"url": "https://www.example.com/a", "path": "src/app.py", "note": "ignored"}'
    )

    assert [(ref["kind"], ref["label"]) for ref in (ref.as_dict() for ref in refs)] == [
        ("url", "example.com"),
        ("file", "app.py"),
    ]


def test_context_refs_ignore_non_json_arguments():
    assert context_refs("just a string") == []
    assert context_refs(None) == []


def test_legacy_traces_are_upgraded_to_steps():
    steps = normalize_steps(
        [
            {
                "kind": "tool_call",
                "title": "Running tool grep",
                "details": "args",
                "status": "running",
                "ts": 120,
            },
            {"kind": "thinking", "title": "Thinking", "details": "hmm"},
        ]
    )

    assert [step["kind"] for step in steps] == [
        StepKind.TOOL.value,
        StepKind.REASONING.value,
    ]
    assert steps[0]["at_ms"] == 120
    assert steps[0]["status"] == StepStatus.RUNNING.value
    assert steps[1]["body"] == "hmm"


def test_current_traces_pass_through_normalization_untouched():
    recorder = TraceRecorder(lambda event: None)
    recorder.note(StepKind.PREPARE, "Preparing the runtime")
    snapshot = recorder.snapshot()

    assert normalize_steps(snapshot) == snapshot


def test_tool_argument_deltas_never_reach_the_answer():
    """`response.function_call_arguments.delta` streams a call's JSON, not text.

    It carries a `delta` exactly like output text does, so a sink that takes
    whatever has a delta on it prints tool arguments into the reply.
    """
    harness = Harness()
    harness.feed(
        raw("response.output_text.delta", "Searching. "),
        raw("response.function_call_arguments.delta", '{"query": "elections'),
        raw("response.function_call_arguments.delta", '", "max_results": 5}'),
        raw("response.output_text.delta", "Here are the results."),
    )

    assert harness.tokens == ["Searching. ", "Here are the results."]


def test_other_non_text_deltas_are_ignored_too():
    harness = Harness()
    harness.feed(
        raw("response.audio.delta", "<binary>"),
        raw("response.code_interpreter_call.code.delta", "print(1)"),
        raw("response.refusal.delta", "I cannot help with that."),
    )

    assert harness.tokens == ["I cannot help with that."]


def test_unknown_provider_shapes_are_still_accepted():
    """A delta with no type is a provider the SDK did not normalize.

    Dropping those would silence that provider, so they stay on the text path.
    """
    harness = Harness()
    harness.observer.handle_event(
        RawResponsesStreamEvent(data=SimpleNamespace(delta="raw text"))
    )

    assert harness.tokens == ["raw text"]


def test_policy_decision_before_tool_called_waits_for_its_row():
    harness = Harness()

    harness.observer.handle_policy_event(
        {"rule": "policy_check", "decision": "allow", "tool": "bash_tool"}
    )
    harness.feed(tool_called("bash_tool", "call-1", '{"command": "ls"}'))
    harness.feed(tool_called("bash_tool", "call-2", '{"command": "pwd"}'))

    first, second = harness.steps()
    assert first["policy"]["decision"] == "allow"
    assert second["policy"] is None


def test_parallel_calls_each_get_their_own_policy_badge():
    harness = Harness()
    harness.feed(tool_called("bash_tool", "call-1", '{"command": "ls"}'))
    harness.feed(tool_called("bash_tool", "call-2", '{"command": "pwd"}'))

    for decision in ("allow", "review"):
        harness.observer.handle_policy_event(
            {"rule": "policy_check", "decision": decision, "tool": "bash_tool"}
        )

    assert [step["policy"]["decision"] for step in harness.steps()] == ["allow", "review"]


def test_parallel_verdicts_follow_their_call_id_not_arrival_order():
    harness = Harness()
    harness.observer.handle_policy_event(
        {"rule": "policy_check", "decision": "review", "tool": "bash_tool", "call_id": "call-2"}
    )
    harness.feed(tool_called("bash_tool", "call-1", '{"command": "ls"}'))
    harness.feed(tool_called("bash_tool", "call-2", '{"command": "rm x"}'))
    harness.observer.handle_policy_event(
        {"rule": "policy_check", "decision": "allow", "tool": "bash_tool", "call_id": "call-1"}
    )

    assert [step["policy"]["decision"] for step in harness.steps()] == ["allow", "review"]


def test_narration_before_an_action_moves_into_the_trace():
    harness = Harness()
    resets = []
    harness.observer._reset_answer = lambda: resets.append(True)
    harness.feed(
        raw("response.output_text.delta", "Delegating to a subagent."),
        tool_called("Agent", "call-1", '{"input": "analyze"}'),
        raw("response.output_text.delta", "Final answer."),
    )

    message, call = harness.steps()
    assert message["kind"] == StepKind.MESSAGE.value
    assert message["body"] == "Delegating to a subagent."
    assert call["title"] == "Delegate to agent"
    assert call["subtitle"] == "analyze"
    assert resets == [True]
    assert harness.tokens[-1] == "Final answer."


def test_subagent_work_lands_in_the_trace_but_not_in_the_answer():
    harness = Harness()
    harness.feed(tool_called("Agent", "call-1", '{"input": "analyze"}'))
    child = harness.observer.nested("general-purpose")
    child.handle_event(tool_called("bash_tool", "call-2", '{"command": "git diff"}'))
    child.handle_event(raw("response.output_text.delta", "sub-agent report"))
    harness.observer.handle_policy_event(
        {"rule": "policy_check", "decision": "allow", "tool": "bash_tool"}
    )

    parent, nested = harness.steps()
    assert nested["title"] == "Run command"
    assert nested["subtitle"] == "git diff"
    assert nested["parent_id"] == parent["id"]
    assert nested["policy"]["decision"] == "allow"
    assert parent["policy"] is None
    assert harness.tokens == []


def test_subagent_run_becomes_the_block_of_its_call():
    harness = Harness()
    harness.feed(tool_called("Agent", "call-1", '{"input": "analyze"}'))
    child = harness.observer.nested("general-purpose", call_id="call-1")
    child.handle_event(tool_called("bash_tool", "call-2", '{"command": "ls"}'))
    child.handle_event(tool_output("call-2", "files"))
    child.finish()
    harness.feed(tool_output("call-1", "sub-agent report"))

    block, nested = harness.steps()
    assert block["kind"] == StepKind.AGENT.value
    assert block["title"] == "general-purpose"
    assert block["tool"] == "Agent"  # the call keeps its name for scenario checks
    assert block["detail"]  # what the sub-agent was asked stays on its block
    assert block["body"] == "sub-agent report"
    assert block["status"] == StepStatus.DONE.value
    assert block["parent_id"] is None
    assert nested["parent_id"] == block["id"]


def test_subagent_block_opened_before_its_call_is_streamed_is_not_duplicated():
    harness = Harness()
    child = harness.observer.nested("general-purpose", call_id="call-1")
    harness.feed(tool_called("Agent", "call-1", '{"input": "analyze"}'))
    harness.observer.handle_policy_event(
        {"rule": "policy_check", "decision": "allow", "tool": "Agent", "call_id": "call-1"}
    )
    child.handle_event(tool_called("bash_tool", "call-2", '{"command": "ls"}'))
    harness.feed(tool_output("call-1", "report"))

    block, nested = harness.steps()
    assert block["kind"] == StepKind.AGENT.value
    assert '"analyze"' in block["detail"]
    assert block["policy"]["decision"] == "allow"
    assert block["body"] == "report"
    assert nested["parent_id"] == block["id"]


def test_parallel_subagents_think_in_their_own_steps():
    harness = Harness()
    harness.feed(
        tool_called("Agent", "call-a", '{"input": "a"}'),
        tool_called("Agent", "call-b", '{"input": "b"}'),
    )
    first = harness.observer.nested("alpha", call_id="call-a")
    second = harness.observer.nested("beta", call_id="call-b")
    first.handle_event(raw("response.reasoning_text.delta", "alpha thinks"))
    second.handle_event(raw("response.reasoning_text.delta", "beta thinks"))
    # One sub-agent acting must not cut the other one's thinking short.
    first.handle_event(tool_called("bash_tool", "call-c", '{"command": "ls"}'))
    second.handle_event(raw("response.reasoning_text.delta", " more"))
    harness.recorder.end_all_reasoning()

    blocks = {step["id"]: step["title"] for step in harness.steps() if step["kind"] == "agent"}
    thoughts = {
        blocks[step["parent_id"]]: step["body"]
        for step in harness.steps()
        if step["kind"] == StepKind.REASONING.value
    }
    assert thoughts == {"alpha": "alpha thinks", "beta": "beta thinks more"}


def test_nested_subagents_form_a_tree():
    harness = Harness()
    harness.feed(tool_called("Agent", "call-1", '{"input": "plan"}'))
    child = harness.observer.nested("planner", call_id="call-1")
    child.handle_event(tool_called("Agent", "call-2", '{"input": "code"}'))
    grandchild = child.nested("coder", call_id="call-2")
    grandchild.handle_event(tool_called("bash_tool", "call-3", '{"command": "ls"}'))

    outer, inner, leaf = harness.steps()
    assert (outer["kind"], inner["kind"]) == ("agent", "agent")
    assert inner["parent_id"] == outer["id"]
    assert leaf["parent_id"] == inner["id"]


def test_failed_subagent_run_does_not_spin_forever():
    harness = Harness()
    child = harness.observer.nested("general-purpose", call_id="call-1")
    child.handle_event(raw("response.reasoning_text.delta", "hmm"))
    child.finish(error="Max turns exceeded")

    block, thinking = harness.steps()
    assert block["status"] == StepStatus.ERROR.value
    assert block["body"] == "Max turns exceeded"
    assert thinking["status"] == StepStatus.DONE.value


def test_tool_steps_get_human_titles_and_argument_subtitles():
    harness = Harness()
    harness.feed(tool_called("search_files", "call-1", '{"pattern": "grid", "directory": "core"}'))
    harness.feed(tool_called("run_command", "call-2", '{"command": "pytest -q\\nmore"}'))
    harness.feed(tool_called("orchestrate", "call-3", '{"task": "Investigate the bug"}'))

    steps = harness.steps()
    assert [(s["title"], s["subtitle"]) for s in steps] == [
        ("Search files", "grid in core"),
        ("Run command", "pytest -q"),
        ("Delegate to agent", "Investigate the bug"),
    ]
    # The raw tool name (used for policy badges) is preserved on the wire.
    assert [s["tool"] for s in steps] == ["search_files", "run_command", "orchestrate"]
    # Full arguments stay available in the expandable detail.
    assert '"pattern": "grid"' in steps[0]["detail"]
    assert '"directory": "core"' in steps[0]["detail"]


def test_unknown_tool_keeps_raw_name_and_json_subtitle():
    harness = Harness()
    harness.feed(tool_called("mystery_tool", "call-1", '{"a": 1}'))
    step = harness.steps()[0]
    assert step["title"] == "mystery_tool"
    assert "a" in step["subtitle"]


def test_orphan_tool_output_uses_human_title():
    harness = Harness()
    harness.feed(tool_output("call-1", "ok"))
    # no matching call: a bare output carries no tool name, so it stays generic
    harness.feed(tool_called("read_file", "call-2", '{"filepath": "a.py"}'))
    harness.feed(tool_output("call-2", "contents"))

    steps = harness.steps()
    assert steps[0]["title"] == "tool"
    assert steps[-1]["title"] == "Read file"
    assert steps[-1]["subtitle"] == "a.py"


def test_malformed_arguments_fall_back_to_flat_json():
    harness = Harness()
    harness.feed(tool_called("read_file", "call-1", "not json"))
    step = harness.steps()[0]
    assert step["title"] == "Read file"
    # Formatter dropped the caption; the old summarize-based subtitle remains.
    assert step["subtitle"] == "not json"


def completed(input_tokens: int, output_tokens: int) -> RawResponsesStreamEvent:
    """A ``response.completed`` raw event carrying the model's usage report."""

    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return RawResponsesStreamEvent(
        data=SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=usage))
    )


def test_usage_lands_on_the_thinking_step_of_its_own_response():
    harness = Harness()
    harness.feed(
        raw("response.reasoning_text.delta", "Let me check."),
        completed(120, 30),
    )

    reasoning = [
        step for step in harness.steps() if step["kind"] == StepKind.REASONING.value
    ]
    assert len(reasoning) == 1
    assert reasoning[0]["tokens_in"] == 120
    assert reasoning[0]["tokens_out"] == 30
    # The same totals roll up to the turn.
    assert harness.recorder.tokens_in == 120
    assert harness.recorder.tokens_out == 30


def test_usage_without_an_open_step_waits_for_the_next_one():
    harness = Harness()
    # A tool-calling model reports usage before its calls arrive, when no
    # reasoning step is open: the tokens wait for the tool step that follows.
    harness.feed(completed(10, 5), tool_called("read_file", "call-1", '{"path": "a.py"}'))

    step = harness.steps()[0]
    assert step["kind"] == StepKind.TOOL.value
    assert step["tokens_in"] == 10
    assert step["tokens_out"] == 5


def test_usage_accepts_prompt_and_completion_aliases():
    harness = Harness()
    usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
    harness.recorder.record_usage(usage)

    assert harness.recorder.tokens_in == 7
    assert harness.recorder.tokens_out == 3


def test_missing_usage_is_a_no_op():
    harness = Harness()
    harness.recorder.record_usage(None)
    harness.recorder.record_usage(SimpleNamespace())

    assert harness.recorder.tokens_in == 0
    assert harness.recorder.tokens_out == 0
    assert harness.steps() == []


def test_usage_callback_fires_and_its_errors_never_break_accounting():
    seen: list[tuple[int, int]] = []
    recorder = TraceRecorder(lambda event: None, on_usage=lambda tin, tout: seen.append((tin, tout)))
    recorder.record_usage(SimpleNamespace(input_tokens=3, output_tokens=4))
    assert seen == [(3, 4)]

    def boom(tokens_in: int, tokens_out: int) -> None:
        raise RuntimeError("budget check blew up")

    recorder = TraceRecorder(lambda event: None, on_usage=boom)
    # A raising on_usage (e.g. a budget check) must not lose the count.
    recorder.record_usage(SimpleNamespace(input_tokens=1, output_tokens=2))
    assert recorder.tokens_in == 1
    assert recorder.tokens_out == 2
