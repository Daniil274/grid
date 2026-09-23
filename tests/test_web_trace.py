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
    assert step["title"] == "read_file"
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
    assert bodies == {"search": "results", "read_file": "readme"}


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
    assert step["policy"]["label"] == "Policy: review"
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
    assert call["title"] == "Agent"
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
    assert nested["title"] == "general-purpose › bash_tool"
    assert nested["policy"]["decision"] == "allow"
    assert parent["policy"] is None
    assert harness.tokens == []
