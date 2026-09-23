"""A sub-agent's answer reaches its caller as text, never as a dump of the run object."""

from types import SimpleNamespace

from agents import Agent
from agents.items import MessageOutputItem
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from core.agent_factory import run_output_text


def message(text):
    raw = ResponseOutputMessage(
        id="m", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )
    return MessageOutputItem(agent=Agent(name="improver"), raw_item=raw)


def tool_call(name):
    return SimpleNamespace(type="tool_call_item", raw_item=SimpleNamespace(name=name, arguments="{}"))


def test_final_output_wins():
    assert run_output_text(SimpleNamespace(final_output="Report", new_items=[])) == "Report"


def test_empty_final_output_falls_back_to_the_last_message():
    result = SimpleNamespace(final_output="", new_items=[message("Причина: описание"), tool_call("file_read")])
    assert run_output_text(result) == "Причина: описание"


def test_no_text_at_all_is_said_plainly():
    result = SimpleNamespace(final_output="", new_items=[tool_call("file_read"), tool_call("file_replace")])
    text = run_output_text(result, "Agent improver")
    assert text.startswith("[Agent improver finished without a written report after 2 tool call(s)")
    assert "file_read, file_replace" in text and "RunResult" not in text
