"""Spaces: one per user, lent per request, retired only when nothing uses them."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.context import ContextManager
from web_chat.identity import User
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool
from web_chat.turns import TurnBoard

#: What the chat's own pages send with a request that changes something.
SAME_SITE = {"Origin": "http://testserver"}


class FakeSpace:
    """A space with a TurnBoard and real conversations; records its closing."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.turns = TurnBoard()
        self.conversations = ContextManager()
        self.closed = False

    @property
    def idle(self) -> bool:
        return self.turns.idle

    async def close(self) -> None:
        self.closed = True

    def context_manager(self):
        return self.conversations

    def update_conversation_metadata(self, context_id, **updates):
        self.conversations.update_context_metadata(context_id, {k: v for k, v in updates.items() if v is not None})

    def schedule_warmup(self) -> None:
        pass

    async def registry_for(self, context_id):
        """Every chat of the fake works in the space's one workspace."""
        return self.registry


def counting_pool(**options):
    built = []

    def build(user_id):
        built.append(user_id)
        return FakeSpace(user_id)

    return SpacePool(build, **options), built


async def test_a_user_gets_one_space_and_other_users_their_own():
    pool, built = counting_pool()

    async with pool.use("a") as first, pool.use("a") as again, pool.use("b") as other:
        assert first is again
        assert other is not first
    assert built == ["a", "b"]


async def test_simultaneous_first_requests_build_the_space_once():
    pool, built = counting_pool()

    async def lease():
        async with pool.use("a") as space:
            return space

    spaces = await asyncio.gather(*(lease() for _ in range(5)))

    assert built == ["a"]
    assert all(space is spaces[0] for space in spaces)


async def test_a_stale_space_is_rebuilt_only_once_nothing_uses_it():
    pool, built = counting_pool()
    async with pool.use("a") as old:
        pool.invalidate()
        assert await pool.sweep() == 0  # leased: kept
        async with pool.use("a") as same:
            assert same is old

    async with pool.use("a") as new:
        assert new is not old
    assert old.closed and built == ["a", "a"]


async def test_a_running_turn_keeps_a_stale_space_alive():
    pool, _ = counting_pool()
    async with pool.use("a") as space:
        space.turns.claim("ctx")
    pool.invalidate()

    assert await pool.sweep() == 0
    async with pool.use("a") as same:
        assert same is space

    space.turns.release("ctx")
    assert await pool.sweep() == 1
    assert space.closed


async def test_unused_spaces_are_retired_after_the_idle_time():
    pool, built = counting_pool(idle_seconds=0)
    async with pool.use("a") as space:
        pass

    assert await pool.sweep() == 1
    assert space.closed and list(pool.live()) == []


async def test_without_an_idle_time_spaces_stay_loaded():
    pool, _ = counting_pool()
    async with pool.use("a"):
        pass

    assert await pool.sweep() == 0
    assert len(list(pool.live())) == 1


async def test_closing_the_pool_closes_every_space():
    pool, _ = counting_pool()
    async with pool.use("a") as a, pool.use("b") as b:
        pass

    await pool.close()

    assert a.closed and b.closed and list(pool.live()) == []


# -- turns -----------------------------------------------------------------------


async def test_a_conversation_takes_one_turn_at_a_time():
    board = TurnBoard()

    assert board.claim("ctx") and not board.claim("ctx")
    assert not board.idle
    board.release("ctx")
    assert board.claim("ctx")


async def test_background_work_keeps_the_board_busy_until_it_ends():
    board = TurnBoard()
    release = asyncio.Event()
    task = board.spawn(release.wait())

    assert not board.idle
    release.set()
    await task
    await asyncio.sleep(0)
    assert board.idle


async def test_a_turn_is_forgotten_only_by_itself():
    board = TurnBoard()
    loop = asyncio.get_running_loop()
    first, second = object(), object()
    board.register("ctx", first, loop.create_future())
    board.register("ctx", second, loop.create_future())

    board.forget("ctx", first)

    assert board.get("ctx")[0] is second


# -- the server serves each user their own space ---------------------------------


def two_user_server():
    """A server that takes the user from a test header, over a pool of fake spaces."""

    async def identify(connection):
        name = connection.headers["X-Test-User"]
        return User(id=name, username=name, role="user")

    pool = SpacePool(FakeSpace)
    deployment = SimpleNamespace(voice_source=lambda: ("voice.yaml", None), voice_config_dict=lambda: {})
    return WebChatServer(deployment, pool, identify=identify, warm_user=None)


def test_conversations_of_one_user_are_invisible_to_another():
    client = TestClient(two_user_server().app, headers=SAME_SITE)
    alice, bob = {"X-Test-User": "alice"}, {"X-Test-User": "bob"}

    created = client.post("/api/chat/conversations", headers=alice).json()

    assert [item["id"] for item in client.get("/api/chat/conversations", headers=alice).json()] == [created["id"]]
    assert client.get("/api/chat/conversations", headers=bob).json() == []
    assert client.get(f"/api/chat/conversations/{created['id']}", headers=bob).status_code == 404
    assert client.delete(f"/api/chat/conversations/{created['id']}", headers=bob).status_code == 404


def test_every_api_route_acts_for_an_identified_user():
    """No route under /api is reachable without the server's identification."""
    server = two_user_server()

    def dependencies(dependant):
        for sub in dependant.dependencies:
            yield sub.call
            yield from dependencies(sub)

    api_routes = [route for route in server.app.routes if getattr(route, "path", "").startswith("/api")]
    assert api_routes
    for route in api_routes:
        assert server.current_user in set(dependencies(route.dependant)), route.path


@pytest.mark.parametrize("path", ["/api/chat/bootstrap", "/api/chat/conversations", "/api/voice/status"])
def test_a_request_without_a_user_is_refused(path):
    from fastapi import HTTPException

    async def nobody(connection):
        raise HTTPException(status_code=401, detail="Sign in first")

    server = WebChatServer(
        SimpleNamespace(voice_source=lambda: ("voice.yaml", None), voice_config_dict=lambda: {}),
        SpacePool(FakeSpace),
        identify=nobody,
        warm_user=None,
    )

    assert TestClient(server.app, headers=SAME_SITE).get(path).status_code == 401
