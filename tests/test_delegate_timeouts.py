"""Delegated agents under their own time limits.

An agent whose limit runs out reports what it did (one answer, tools off),
and its caller learns how to continue it; the caller's own clock stops while
it waits, so a coordinator is never cut off by the time its agents take.
"""

import asyncio
from types import SimpleNamespace

import agents

from core.deadlines import Deadline
from tests.test_run_agent import factory, use  # noqa: F401 - the factory fixture
from tests.test_tool_error_recovery import sub_agent_tool, tool_ctx


class _Hanging:
    """A streamed run whose model never answers."""

    final_output = None
    new_items = []

    async def stream_events(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover


class _Answered:
    new_items = []

    def __init__(self, text):
        self.final_output = text

    async def stream_events(self):
        return
        yield  # pragma: no cover


async def test_a_dynamic_agent_out_of_time_reports_its_work(factory):  # noqa: F811
    inputs = []

    class Runner:
        @staticmethod
        def run_streamed(**kwargs):
            inputs.append(kwargs["input"])
            return _Hanging() if len(inputs) == 1 else _Answered("Read 3 files; next: patch b.py")

    factory.config.get_agent_timeout = lambda agent_key=None: 0.05
    with use(Runner):
        outcome = await factory.run_agent_object(agents.Agent(name="executor-1", instructions="x"), "task")

    assert outcome.stopped == "timeout"
    assert "ran out of time (0.05 s); its report of the work so far" in outcome.text
    assert outcome.text.endswith("Read 3 files; next: patch b.py")
    assert inputs[1].startswith("Your time for this task is up")


async def test_a_report_that_does_not_come_leaves_the_plain_summary(factory, monkeypatch):  # noqa: F811
    import core.factory.turns as turns

    monkeypatch.setattr(turns, "limit_report_seconds", lambda timeout: 0.05)

    class Runner:
        @staticmethod
        def run_streamed(**kwargs):
            return _Hanging()

    factory.config.get_agent_timeout = lambda agent_key=None: 0.05
    with use(Runner):
        outcome = await factory.run_agent_object(agents.Agent(name="executor-1", instructions="x"), "task")

    assert outcome.stopped == "timeout"
    assert "no answer within the agent timeout (0.05 s)" in outcome.text


async def test_a_sub_agent_out_of_time_says_how_to_continue_it(tmp_path, monkeypatch):
    factory, tool = sub_agent_tool(tmp_path, monkeypatch, lambda **kwargs: _Hanging())
    factory.config.get_agent_timeout = lambda agent_key=None: 0.1

    # The caller's own limit is shorter than the sub-agent's: its clock stops
    # while the sub-agent works, so only the sub-agent's limit ends the wait.
    async with Deadline(0.03):
        out = await tool.on_invoke_tool(tool_ctx("alice"), '{"input": "Start"}')

    assert "no answer within the agent timeout (0.1 s)" in out
    assert "call call_test_agent again with input that includes ctx-" in out


def test_the_report_gets_a_tenth_of_the_limit_within_bounds():
    from core.factory.turns import limit_report_seconds

    assert limit_report_seconds(60) == 30
    assert limit_report_seconds(900) == 90
    assert limit_report_seconds(7200) == 120
