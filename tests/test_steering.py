"""Messages sent to a running turn reach the agent at its next step, in place."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from agents import SQLiteSession
from agents.run import CallModelData, ModelInputData

from core.steering import STEER_PREFIX, SteerMessage, Steering
from tests.test_interruption import OBSERVER, FakeStream, called, returned
from tests.test_run_agent import factory  # noqa: F401 - fixture


def text_of(item):
    content = item.get("content")
    return content if isinstance(content, str) else content[0]["text"]


async def test_a_message_is_stored_after_the_last_step_and_kept_in_every_later_call():
    steering = Steering()
    session = SQLiteSession("steer-unit")
    delivered = []
    await session.add_items([{"role": "user", "content": "task"}, {"role": "assistant", "content": "step 1"}])
    run_items = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "step 1"}]

    steering.add(SteerMessage("m1", "the file is in docs/", on_delivered=delivered.append))
    first = await steering.apply(run_items, session)
    assert text_of(first[-1]) == STEER_PREFIX + "the file is in docs/"
    assert [m.message_id for m in delivered] == ["m1"]
    stored = await session.get_items()
    assert text_of(stored[-1]).endswith("the file is in docs/")

    # The next call's input grew by the step that followed: the message stays
    # where it was delivered, and is stored only once.
    later = run_items + [{"role": "assistant", "content": "step 2"}]
    second = await steering.apply(later, session)
    assert [text_of(item) for item in second] == ["task", "step 1", STEER_PREFIX + "the file is in docs/", "step 2"]
    assert len(await session.get_items()) == 3


def test_a_message_no_call_read_goes_back_to_its_sender():
    steering = Steering()
    back = []
    steering.add(SteerMessage("m1", "later", on_undelivered=back.append))
    steering.hand_back()
    assert [m.message_id for m in back] == ["m1"]
    assert steering.undelivered() == []


class FilteringRunner:
    """A streamed run whose steps call the run's input filter, as the SDK does."""

    def __init__(self, stream_steps, final_output="Done."):
        self.stream_steps = stream_steps
        self.final_output = final_output
        self.inputs = []

    def run_streamed(self, agent, run_input, *, context, max_turns, session, run_config=None):
        runner = self

        async def model_call(items):
            data = CallModelData(model_data=ModelInputData(input=items, instructions="i"), agent=agent, context=context)
            sent = await run_config.call_model_input_filter(data)
            runner.inputs.append(sent.input)

        steps = self.stream_steps(model_call)
        return FakeStream(*steps, final_output=self.final_output)


async def test_a_running_turn_takes_a_message_at_its_next_step(factory):
    context_id = factory.context_manager.start_new_context()
    at_tool, release = asyncio.Event(), asyncio.Event()
    base = [{"role": "user", "content": "Refactor the module"}]

    async def tool_runs():
        at_tool.set()
        await release.wait()

    def steps(model_call):
        async def first_call():
            await model_call(base)

        async def second_call():
            await model_call(base + [{"role": "assistant", "content": "read the file"}])

        return [[first_call, called("c1"), tool_runs, returned("c1")], [second_call]]

    runner = FilteringRunner(steps)
    delivered = []
    with patch("agents.Runner", runner):
        turn = asyncio.create_task(
            factory.run_agent("worker", "Refactor the module", context_id=context_id, stream=True, stream_observer=OBSERVER)
        )
        await at_tool.wait()
        assert factory.steer(context_id, SteerMessage("s1", "keep the public API", on_delivered=delivered.append))
        release.set()
        await turn

    assert all(STEER_PREFIX not in str(item) for item in runner.inputs[0])
    assert text_of(runner.inputs[1][-1]) == STEER_PREFIX + "keep the public API"
    assert [m.message_id for m in delivered] == ["s1"]
    chat = factory.context_manager.conversation_view(context_id)["messages"]
    steer = next(m for m in chat if (m.metadata or {}).get("type") == "steer")
    assert steer.content == "keep the public API" and steer.role == "user"
    assert not factory.steer(context_id, SteerMessage("s2", "too late"))


async def test_a_message_sent_as_the_turn_answers_is_handed_back(factory):
    context_id = factory.context_manager.start_new_context()
    answering = asyncio.Event()
    release = asyncio.Event()

    async def last_step():
        answering.set()
        await release.wait()

    def steps(model_call):
        return [[last_step]]

    back = []
    with patch("agents.Runner", FilteringRunner(steps)):
        turn = asyncio.create_task(
            factory.run_agent("worker", "Go", context_id=context_id, stream=True, stream_observer=OBSERVER)
        )
        await answering.wait()
        factory.steer(context_id, SteerMessage("s1", "and then this", on_undelivered=back.append))
        release.set()
        await turn
    assert [m.message_id for m in back] == ["s1"]
    chat = factory.context_manager.conversation_view(context_id)["messages"]
    assert not any((m.metadata or {}).get("type") == "steer" for m in chat)


async def test_a_delivered_message_joins_the_task_the_policy_gate_judges(factory):
    """What the user adds mid-turn authorizes actions like the request itself."""
    from core.action_policy import ActionRunState
    from core.interruption import RunControl

    context_id = factory.context_manager.start_new_context()
    control = RunControl()
    control.action_state = ActionRunState(task="Refactor the module")
    factory._run_controls[context_id] = control
    try:
        assert factory.steer(context_id, SteerMessage("s1", "also run the tests"))
        await control.steering.apply([{"role": "user", "content": "Refactor the module"}], None)
    finally:
        del factory._run_controls[context_id]
    assert control.action_state.task.endswith("also run the tests")
    assert control.action_state.task.startswith("Refactor the module")
