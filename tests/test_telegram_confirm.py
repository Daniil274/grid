"""Calls waiting for the user's answer: one message, two buttons, the real click path."""

import pytest

from web_chat.telegram.config import Settings
from web_chat.telegram.confirm import Approvals, parse_callback
from web_chat.telegram.service import TelegramChannel
from web_chat.telegram.store import TelegramStore

NOW = 1_000_000.0
CONTEXT = "ctx-1"


class Bot:
    def __init__(self):
        self.sent = []
        self.buttons = []
        self.answers = []

    async def send_message(self, chat_id, text, thread_id=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": 100 + len(self.sent)}

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.buttons.append((chat_id, message_id, reply_markup))

    async def answer_callback_query(self, callback_id, text=""):
        self.answers.append((callback_id, text))


class Space:
    def __init__(self, waiting=None):
        self.waiting = waiting or {}
        self.answered = []

    def awaiting_approval(self):
        return self.waiting

    def answer_action_review(self, context_id, approval_id, approve=False, remember=False):
        self.answered.append((context_id, approval_id, approve))
        for reviews in self.waiting.values():
            reviews[:] = [item for item in reviews if item["approval_id"] != approval_id]
        return True


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


REVIEW = {"approval_id": "ap-1", "tool": "shell", "waiting": True, "context_id": CONTEXT}


@pytest.fixture
def store():
    result = TelegramStore(":memory:")
    yield result
    result.close()


def channel(store, space, bot, *, allowed=lambda user_id: True):
    return TelegramChannel(
        store, Spaces(space), Settings(enabled=True, token="1:x"), bot=bot,
        allowed=allowed, clock=lambda: NOW,
    )


def callback(data, callback_id="cb-1", chat_id=11, message_id=101, sender_id=None,
             chat_type="private"):
    """A press as Telegram delivers it: the message's chat, and who pressed."""
    return {"update_id": 9, "callback_query": {
        "id": callback_id, "data": data,
        "from": {"id": chat_id if sender_id is None else sender_id},
        "message": {"chat": {"id": chat_id, "type": chat_type}, "message_id": message_id},
    }}


def link(store):
    store.redeem_code(store.create_code("alice", now=NOW)["code"], 11, 0, now=NOW)


def test_callback_data_is_read_and_nothing_else():
    assert parse_callback("approve:ap-1") == (True, "ap-1")
    assert parse_callback("decline:ap-1") == (False, "ap-1")
    assert parse_callback("approve:") is None
    assert parse_callback("answered") is None
    assert parse_callback("") is None


async def test_a_waiting_call_is_announced_once_with_two_buttons(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    approvals = Approvals(store, bot)
    assert await approvals.publish(space, "alice", 11, 0, CONTEXT) == 1
    assert len(bot.sent) == 1
    keyboard = bot.sent[0]["reply_markup"]["inline_keyboard"][0]
    assert [button["callback_data"] for button in keyboard] == ["approve:ap-1", "decline:ap-1"]
    assert store.approval("ap-1").message_id == 101
    # The same call is not announced twice.
    assert await approvals.publish(space, "alice", 11, 0, CONTEXT) == 0


async def test_approve_resolves_the_call_in_its_own_conversation(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = channel(store, space, bot)
    await service.approvals.publish(space, "alice", 11, 0, CONTEXT)

    await service.dispatch(callback("approve:ap-1"))
    assert space.answered == [(CONTEXT, "ap-1", True)]
    assert bot.answers[-1] == ("cb-1", "Approved")
    assert bot.buttons[-1][2]["inline_keyboard"][0][0]["text"] == "Approved"
    assert store.approval("ap-1") is None


async def test_decline_is_the_same_door_with_false(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = channel(store, space, bot)
    await service.approvals.publish(space, "alice", 11, 0, CONTEXT)

    await service.dispatch(callback("decline:ap-1"))
    assert space.answered == [(CONTEXT, "ap-1", False)]
    assert bot.answers[-1] == ("cb-1", "Declined")


async def test_an_unknown_call_is_answered_but_not_resolved(store):
    link(store)
    bot, space = Bot(), Space()
    service = channel(store, space, bot)
    await service.dispatch(callback("approve:gone"))
    assert space.answered == []
    assert "no longer waiting" in bot.answers[-1][1]


async def test_a_disabled_user_cannot_answer(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = channel(store, space, bot, allowed=lambda user_id: False)
    store.announce_approval("ap-1", "alice", 11, 0, CONTEXT, now=NOW)
    await service.dispatch(callback("approve:ap-1"))
    assert space.answered == []
    assert "closed" in bot.answers[-1][1]


async def test_a_broken_review_does_not_leave_the_button_spinning(store):
    link(store)
    bot = Bot()

    class Broken(Space):
        def answer_action_review(self, *args, **kwargs):
            raise RuntimeError("boom")

    space = Broken({CONTEXT: [dict(REVIEW)]})
    service = channel(store, space, bot)
    store.announce_approval("ap-1", "alice", 11, 0, CONTEXT, now=NOW)
    await service.dispatch(callback("approve:ap-1"))
    assert bot.answers[-1] == ("cb-1", "It is no longer waiting.")


async def test_a_button_that_is_not_ours_is_acknowledged(store):
    link(store)
    bot, space = Bot(), Space()
    service = channel(store, space, bot)
    await service.dispatch(callback("something-else"))
    assert bot.answers == [("cb-1", "")]


async def announced(store, space, bot):
    """The real path: the channel announces a waiting call, then a button comes."""
    service = channel(store, space, bot)
    await service.approvals.publish(space, "alice", 11, 0, CONTEXT)
    return service


async def test_a_button_forwarded_to_another_chat_resolves_nothing(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = await announced(store, space, bot)
    await service.dispatch(callback("approve:ap-1", chat_id=-1009999, sender_id=999))
    assert space.answered == []
    assert "does not belong" in bot.answers[-1][1]
    assert store.approval("ap-1") is not None


async def test_a_button_pressed_by_someone_else_resolves_nothing(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = await announced(store, space, bot)
    await service.dispatch(callback("approve:ap-1", sender_id=999))
    assert space.answered == []
    assert "does not belong" in bot.answers[-1][1]
    assert store.approval("ap-1") is not None


async def test_a_button_in_a_group_resolves_nothing(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = await announced(store, space, bot)
    await service.dispatch(callback("approve:ap-1", chat_type="group"))
    assert space.answered == []
    assert "does not belong" in bot.answers[-1][1]


async def test_a_button_on_another_message_resolves_nothing(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = await announced(store, space, bot)
    await service.dispatch(callback("approve:ap-1", message_id=999))
    assert space.answered == []
    assert "does not belong" in bot.answers[-1][1]


async def test_a_button_pressed_after_the_user_relinked_resolves_nothing(store):
    link(store)
    bot, space = Bot(), Space({CONTEXT: [dict(REVIEW)]})
    service = await announced(store, space, bot)
    # The user moves the link to another chat: the old chat's buttons die with it.
    store.redeem_code(store.create_code("alice", now=NOW + 1)["code"], 22, 0,
                      now=NOW + 1, tg_user_id=22)
    await service.dispatch(callback("approve:ap-1", chat_id=22, sender_id=22))
    assert space.answered == []
    assert store.approval("ap-1") is None


def test_forgetting_a_conversation_drops_what_was_announced(store):
    store.announce_approval("ap-1", "alice", 11, 0, CONTEXT, now=NOW)
    Approvals(store, Bot()).forget_context(CONTEXT)
    assert store.approval("ap-1") is None


def test_the_approval_record_carries_the_conversation_not_a_capability(store):
    store.announce_approval("ap-1", "alice", 11, 7, CONTEXT, now=NOW)
    record = store.approval("ap-1")
    assert record.context_id == CONTEXT and record.chat_id == 11
