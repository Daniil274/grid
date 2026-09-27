"""User limits: turns at once and per day, checked where every turn starts."""

import asyncio
import json
import sqlite3

import pytest

from schemas.schemas import UserLimitsPolicy
from tests.test_accounts import Clock
from tests.test_web_chat_session import Socket, running_agent, space_for, wait_for
from web_chat.accounts import passwords
from web_chat.accounts.service import Accounts
from web_chat.accounts.store import AccountStore
from web_chat.limits import TurnLimits
from web_chat.session import chat_session


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "LOG2_N", 10)


@pytest.fixture
def clock():
    return Clock(1_790_000_000.0)  # 2026-09-21 16:53 UTC


@pytest.fixture
def accounts(tmp_path, clock):
    store = AccountStore(tmp_path / "accounts.db")
    yield Accounts(store, clock=clock)
    store.close()


class Counter:
    def __init__(self, allow=True):
        self.allow, self.counted = allow, 0

    def count_turn(self, user_id, limit):
        if self.allow:
            self.counted += 1
        return self.allow


# -- the rules -------------------------------------------------------------------


def test_a_turn_over_the_running_limit_is_refused_and_not_counted():
    counter = Counter()
    limits = TurnLimits("u", lambda: UserLimitsPolicy(running_turns=2), counter)

    assert limits.admit(running=1) is None
    assert "running" in limits.admit(running=2)
    assert counter.counted == 1


def test_a_turn_past_the_daily_limit_is_refused():
    limits = TurnLimits("u", lambda: UserLimitsPolicy(turns_per_day=5), Counter(allow=False))

    assert "today's 5 turns" in limits.admit(running=0)


def test_the_policy_is_read_at_every_turn():
    policy = UserLimitsPolicy(running_turns=1)
    limits = TurnLimits("u", lambda: policy, Counter())
    assert limits.admit(running=1) is not None

    policy = UserLimitsPolicy(running_turns=3)

    assert limits.admit(running=1) is None


# -- counting per day ------------------------------------------------------------


def test_turns_are_counted_up_to_the_days_limit(accounts):
    user = accounts.create_user("alice", "correct horse battery")

    assert [accounts.count_turn(user.id, 2) for _ in range(3)] == [True, True, False]
    assert accounts.turns_today(user.id) == 2
    assert accounts.accounts()[0].turns_today == 2


def test_the_count_starts_again_on_the_next_utc_day(accounts, clock):
    user = accounts.create_user("alice", "correct horse battery")
    accounts.count_turn(user.id, 1)

    clock.now += 24 * 3600

    assert accounts.count_turn(user.id, 1)


def test_without_a_daily_limit_turns_are_still_counted(accounts):
    user = accounts.create_user("alice", "correct horse battery")

    assert all(accounts.count_turn(user.id, None) for _ in range(3))
    assert accounts.turns_today(user.id) == 3


def test_a_version_1_database_gains_the_usage_table(tmp_path):
    path = tmp_path / "accounts.db"
    AccountStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE usage")
        db.execute("PRAGMA user_version = 1")

    store = AccountStore(path)
    try:
        assert store.turns_on("nobody", "2026-09-21") == 0
    finally:
        store.close()


# -- in the chat -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_turn_says_why_and_does_not_run():
    run, started, release, seen = running_agent()
    space, socket = space_for(run), Socket()
    space.admit_turn = lambda: "You have used today's 5 turns."
    session = asyncio.create_task(chat_session(space, socket, "ctx"))

    await socket.incoming.put('{"message":"hello"}')

    assert (await socket.event("error"))["content"] == "You have used today's 5 turns."
    await socket.event("done")
    assert seen == [] and not space.turns.is_claimed("ctx")
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_queued_message_that_is_refused_stays_in_the_queue():
    run, started, release, seen = running_agent()
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"message":"later","delivery":"after_turn"}')
    await socket.event("queue")
    space.admit_turn = lambda: "Limit reached."

    release.set()  # the turn answers; the queue would move on

    assert (await socket.event("error"))["content"] == "Limit reached."
    items = (await socket.event("queue"))["items"]
    assert [item["text"] for item in items] == ["later"]
    assert seen == ["first"]

    space.admit_turn = lambda: None
    await socket.incoming.put(json.dumps({"action": "send_queued", "id": items[0]["id"]}))
    await wait_for(lambda: seen == ["first", "later"])
    await socket.incoming.put(None)
    await session


def test_a_space_applies_the_deployments_limits(tmp_path, monkeypatch):
    from tests.test_web_space import MINIMAL_CONFIG
    from web_chat.deployment import Deployment
    from web_chat.space import SpaceLayout, UserSpace

    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG + "user_limits:\n  running_turns: 1\n", encoding="utf-8")
    counter = Counter()
    space = UserSpace(
        Deployment(config_path=str(config)), user_id="u", layout=SpaceLayout.under(tmp_path / "u"), turn_counter=counter
    )

    assert space.admit_turn() is None
    space.turns.claim("ctx")
    assert "running" in space.admit_turn()
    assert counter.counted == 1
    assert UserSpace(Deployment(config_path=str(config))).admit_turn() is None  # one user: no limits
