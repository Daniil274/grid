"""One message, one turn: the final text comes back; a running turn says busy."""

import asyncio
from types import SimpleNamespace

import pytest

import web_chat.session as session_module
from core.context import ContextManager
from web_chat.telegram.config import Settings
from web_chat.telegram.runner import TurnRunner
from web_chat.telegram.service import TelegramChannel
from web_chat.telegram.store import TelegramStore
from web_chat.turns import TurnBoard

NOW = 1_000_000.0


class Space:
    def __init__(self):
        self.turns = TurnBoard()
        self.conversations = ContextManager()
        self.started = []
        self.refusal = None
        self.watched = []

    def context_manager(self):
        return self.conversations

    def update_conversation_metadata(self, context_id, **updates):
        self.conversations.update_context_metadata(
            context_id, {key: value for key, value in updates.items() if value is not None}
        )


class Spaces:
    def __init__(self, space):
        self.space = space

    def use(self, _user_id):
        space = self.space

        class Lease:
            async def __aenter__(self):
                return space

            async def __aexit__(self, *exc):
                return False

        return Lease()


class Turn:
    def __init__(self, manager, context_id, answer):
        self.manager, self.context_id, self.answer = manager, context_id, answer
        self.outcome = None
        self.tokens = (0, 0)
        self.finish = asyncio.Event()

    def request_stop(self):
        self.outcome = "interrupted"
        self.finish.set()
        return True

    async def run(self):
        await self.finish.wait()
        if self.outcome is None:
            self.outcome = "answered"
            self.manager.add_message("assistant", self.answer, context_id=self.context_id)


class Bot:
    def __init__(self):
        self.sent = []
        self.voices = []

    async def send_message(self, chat_id, text, thread_id=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "thread_id": thread_id,
                          "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    async def send_voice(self, chat_id, ogg, thread_id=None, duration=0):
        self.voices.append({"chat_id": chat_id, "ogg": ogg})
        return {}


@pytest.fixture
def world(monkeypatch):
    space = Space()
    answer = {"text": "The report is ready.\n## Checkpoint\nShipped."}

    class Session:
        def __init__(self, space_, socket, context_id):
            self.space, self.socket, self.context_id = space_, socket, context_id

        async def _start(self, message, system_key, agent_key):
            if space.refusal:
                await self.socket.send_json({"type": "busy", "content": space.refusal})
                return False
            space.started.append(message)
            turn = Turn(space.conversations, self.context_id, answer["text"])
            space.turns.claim(self.context_id, turn)
            job = asyncio.create_task(turn.run())
            space.turns.register(self.context_id, turn, job)
            return True

    monkeypatch.setattr(session_module, "ChatSession", Session)
    return SimpleNamespace(space=space, spaces=Spaces(space), runner=TurnRunner(Spaces(space), poll=0.01),
                           answer=answer)


def current_turn(space):
    (_context_id, turn, _job), = space.turns.running_turns()
    return turn


async def test_a_message_runs_as_a_turn_and_returns_its_final_text(world):
    job = asyncio.create_task(world.runner.run("alice", "tg-chat", "write the report"))
    await asyncio.sleep(0.03)
    current_turn(world.space).finish.set()
    outcome = await job
    assert outcome.outcome == "answered"
    assert outcome.answer.endswith("Shipped.")
    assert world.space.started == ["write the report"]
    metadata = world.space.conversations.get_context_metadata("tg-chat")
    assert metadata["title"] == "Telegram" and metadata["created_by_web"] is True


async def test_a_watcher_sees_the_running_turn(world):
    seen = []

    async def watch(space, context_id):
        seen.append((space, context_id))

    job = asyncio.create_task(
        world.runner.run("alice", "tg-chat", "hello", on_watch=watch)
    )
    await asyncio.sleep(0.05)
    current_turn(world.space).finish.set()
    await job
    assert seen and seen[0][1] == "tg-chat"


async def test_a_message_while_a_turn_runs_is_busy(world):
    world.space.turns.claim("tg-chat", object())
    outcome = await world.runner.run("alice", "tg-chat", "again")
    assert outcome.outcome == "busy" and "active turn" in outcome.reason


async def test_a_refused_start_says_why(world):
    world.space.refusal = "Daily turn limit reached"
    outcome = await world.runner.run("alice", "tg-chat", "hello")
    assert outcome.outcome == "refused" and outcome.reason == "Daily turn limit reached"


async def test_a_long_turn_is_stopped_and_reported(world):
    runner = TurnRunner(world.spaces, poll=0.01, limit=-1.0, grace=0.5)
    outcome = await runner.run("alice", "tg-chat", "hello")
    assert outcome.outcome == "timeout" and "longer" in outcome.reason
    assert world.space.turns.running("tg-chat") is None


async def test_the_channel_sends_the_answer(world, tmp_path):
    bot = Bot()
    store = TelegramStore(tmp_path / "telegram.db")
    store.redeem_code(store.create_code("alice", now=NOW)["code"], 11, 0, now=NOW)
    service = TelegramChannel(store, world.spaces, Settings(enabled=True, token="1:x"),
                              bot=bot, runner=world.runner, clock=lambda: NOW)
    job = asyncio.create_task(service.dispatch(
        {"update_id": 1, "message": {"chat": {"id": 11, "type": "private"},
                                     "from": {"id": 11, "username": "alice"},
                                     "text": "write the report"}}
    ))
    await asyncio.sleep(0.05)
    current_turn(world.space).finish.set()
    await job
    assert bot.sent[-1]["text"].endswith("Shipped.")
    assert store.context(11, 0) is not None


async def test_the_channel_says_busy_instead_of_queueing(world, tmp_path):
    bot = Bot()
    store = TelegramStore(tmp_path / "telegram.db")
    store.redeem_code(store.create_code("alice", now=NOW)["code"], 11, 0, now=NOW)
    store.set_context(11, 0, "alice", "tg-chat", now=NOW)
    world.space.turns.claim("tg-chat", object())
    service = TelegramChannel(store, world.spaces, Settings(enabled=True, token="1:x"),
                              bot=bot, runner=world.runner, clock=lambda: NOW)
    await service.dispatch(
        {"update_id": 1, "message": {"chat": {"id": 11, "type": "private"},
                                     "from": {"id": 11, "username": "alice"}, "text": "again"}}
    )
    assert "still working" in bot.sent[-1]["text"]


def test_a_long_answer_is_split_into_telegram_sized_parts():
    from web_chat.telegram.bot import chunks

    parts = chunks("word " * 2000)
    assert len(parts) > 1
    assert all(len(part) <= 4000 for part in parts)
    assert "".join(parts).strip() == ("word " * 2000).strip()
    assert chunks("   ") == []
