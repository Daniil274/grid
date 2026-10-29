"""A lazy step as a chat turn: started, watched, stopped at a safe point, read back."""

import asyncio
from types import SimpleNamespace

import pytest

import web_chat.session as session_module
from core.context import ContextManager
from core.lazy.model import Settings, Step, Task
from web_chat.lazy.runner import StepRunner
from web_chat.turns import TurnBoard


class Space:
    def __init__(self):
        self.turns = TurnBoard()
        self.conversations = ContextManager()
        self.refusal = None
        self.started = []

    def context_manager(self):
        return self.conversations

    def update_conversation_metadata(self, context_id, **updates):
        self.conversations.update_context_metadata(context_id, {k: v for k, v in updates.items() if v is not None})


class Spaces:
    def __init__(self, space):
        self.space = space

    def use(self, user_id):
        space = self.space

        class Lease:
            async def __aenter__(self):
                return space

            async def __aexit__(self, *exc):
                return False

        return Lease()


class Turn:
    """Answers when told to; a graceful stop ends it as interrupted."""

    def __init__(self, manager, context_id):
        self.manager, self.context_id = manager, context_id
        self.outcome = None
        self.tokens = (0, 0)
        self.finish = asyncio.Event()
        self.stop_asked = False

    def request_stop(self):
        self.stop_asked = True
        self.outcome = "interrupted"
        self.finish.set()
        return True

    async def run(self, answer):
        await self.finish.wait()
        self.tokens = (9000, 1000)
        if self.outcome is None:
            self.outcome = "answered"
            self.manager.add_message("assistant", answer, context_id=self.context_id)


@pytest.fixture
def world(monkeypatch):
    space = Space()
    answer = {"text": "All done.\n## Checkpoint\nShipped."}

    class Session:
        def __init__(self, space_, socket, context_id):
            self.space, self.socket, self.context_id = space_, socket, context_id

        async def _start(self, message, system_key, agent_key):
            if space.refusal:
                await self.socket.send_json({"type": "error", "content": space.refusal})
                return False
            space.started.append(message)
            turn = Turn(space.conversations, self.context_id)
            space.turns.claim(self.context_id, turn)
            job = asyncio.create_task(turn.run(answer["text"]))
            space.turns.register(self.context_id, turn, job)
            return True

    monkeypatch.setattr(session_module, "ChatSession", Session)
    runner = StepRunner(Spaces(space), poll=0.01, grace=1.0)
    task = Task(id="lazy-1", user_id="u", title="Report", brief="Write the report",
                steps=[Step("s1", "scout", 100_000, 5), Step("s2", "work", 100_000, 5), Step("s3", "verify", 100_000, 5)])
    return SimpleNamespace(space=space, runner=runner, task=task, answer=answer, settings=Settings())


def current_turn(space):
    (context_id, turn, _job), = space.turns.running_turns()
    return turn


async def test_a_step_runs_as_a_turn_of_the_tasks_own_conversation(world):
    seen = []
    job = asyncio.create_task(world.runner.run(world.task, world.task.steps[0], world.settings, on_conversation=seen.append))
    await asyncio.sleep(0.03)
    current_turn(world.space).finish.set()
    outcome = await job
    assert outcome.outcome == "answered" and outcome.answer.endswith("Shipped.")
    assert outcome.tokens == 10_000
    context_id = seen[0]
    metadata = world.space.conversations.get_context_metadata(context_id)
    assert metadata["lazy_task"] == "lazy-1" and metadata["title"] == "Lazy · Report"
    assert "step 1 of 3: Scout" in world.space.started[0]


async def test_a_stop_is_asked_at_a_safe_point_and_reported(world):
    reasons = iter([None, "Paused by you"])
    outcome = await world.runner.run(world.task, world.task.steps[0], world.settings, should_stop=lambda: next(reasons, None))
    assert outcome.outcome == "interrupted" and outcome.reason == "Paused by you"
    assert outcome.answer == ""


async def test_the_step_yields_when_the_user_starts_working(world):
    async def users_turn():
        await asyncio.sleep(10)

    mine = asyncio.create_task(users_turn())
    world.space.turns.register("ctx-users-chat", object(), mine)
    outcome = await world.runner.run(world.task, world.task.steps[0], world.settings)
    mine.cancel()
    assert outcome.reason == "You started working: the subscription is yours"
    relaxed = Settings(yield_to_user=False)
    assert StepRunner._user_working(world.space, "ctx-lazy", relaxed) is None


async def test_an_interrupted_step_continues_its_turn(world, monkeypatch):
    world.task.conversation_id = world.space.conversations.start_new_context()
    step = world.task.steps[1]
    step.resume = True
    monkeypatch.setattr(world.space.conversations, "pending_interruption", lambda context_id: object())
    job = asyncio.create_task(world.runner.run(world.task, step, world.settings))
    await asyncio.sleep(0.03)
    current_turn(world.space).finish.set()
    await job
    assert world.space.started == [None]  # Continue, not a new message


async def test_a_refused_start_says_why(world):
    world.space.refusal = "Daily turn limit reached"
    outcome = await world.runner.run(world.task, world.task.steps[0], world.settings)
    assert outcome.outcome == "refused" and outcome.reason == "Daily turn limit reached"


async def test_a_chat_pinned_to_a_changed_system_goes_on_in_a_new_one(world):
    # Agents edit the system configs too: the task's chat, pinned before the
    # edit, is refused on every retry - the task used to fail on it.
    old = world.space.conversations.start_new_context()
    world.task.conversation_id = old
    world.task.steps[0].status, world.task.steps[0].summary = "done", "Scouted: three files to change."
    refusals = ["Conversation x cannot be continued: the resolved system configuration changed since it "
                "was pinned. Start a new conversation. (revision.system_config_changed)"]
    session = session_module.ChatSession
    original = session._start

    async def start_once_refused(self, message, system_key, agent_key):
        if refusals:
            await self.socket.send_json({"type": "error", "content": refusals.pop()})
            return False
        return await original(self, message, system_key, agent_key)

    session._start = start_once_refused
    seen = []
    step = world.task.steps[1]
    run = asyncio.create_task(world.runner.run(world.task, step, world.settings, on_conversation=seen.append))
    while not world.space.turns.running_turns():
        await asyncio.sleep(0.01)
    current_turn(world.space).finish.set()
    outcome = await run
    assert outcome.outcome == "answered"
    assert outcome.conversation_id != old and seen[-1] == outcome.conversation_id
    assert "Scouted: three files to change." in world.space.started[-1]  # the checkpoint carries the work


async def test_a_turn_already_running_after_a_restart_is_followed_not_restarted(world):
    context_id = world.space.conversations.start_new_context()
    world.task.conversation_id = context_id
    resumed = Turn(world.space.conversations, context_id)
    world.space.turns.claim(context_id, resumed)
    job = asyncio.create_task(resumed.run("Resumed and done."))
    world.space.turns.register(context_id, resumed, job)
    running = asyncio.create_task(world.runner.run(world.task, world.task.steps[0], world.settings))
    await asyncio.sleep(0.03)
    resumed.finish.set()
    outcome = await running
    assert world.space.started == [] and outcome.answer == "Resumed and done."
