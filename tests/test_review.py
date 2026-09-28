"""Reviews: evidence frozen behind an answer, scrubbed, stored, filed by its user only."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.context import ContextManager
from schemas.schemas import AgentConfig, ModelConfig, ReviewPolicy, ToolConfig
from web_chat.identity import User
from web_chat.review import evidence as evidence_module
from web_chat.review.desk import ReviewDesk, ReviewRefused
from web_chat.review.evidence import EvidenceError, collect
from web_chat.review.redact import Redactor, environment_secrets
from web_chat.review.store import Review, ReviewStore
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool
from web_chat.turns import TurnBoard

ALICE = User(id="a" * 32, username="alice", role="user")
SAME_SITE = {"Origin": "http://testserver"}


# -- a space with a real conversation ---------------------------------------------
class Registry:
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            agents={"coder": AgentConfig(name="Coder", model="fast", tools=["bash_tool"])},
            tools={"bash_tool": ToolConfig(type="function", name="bash_tool", env_vars={"TOKEN": "tool-env-secret"})},
            models={"fast": ModelConfig(name="gpt-x", provider="openrouter")},
        )

    def keys(self):
        return ["engineering"]

    def default_key(self):
        return "engineering"

    def config(self, key):
        return SimpleNamespace(config=self._config)


class Space:
    def __init__(self, root: Path) -> None:
        self.user_id = ALICE.id
        self.turns = TurnBoard()
        self.registry = Registry()
        self.agent_sessions_path = root / "agent_sessions.db"
        self.manager = ContextManager(max_history=100, persist_path=str(root / "conversations.json"))
        self.context_id = self.manager.start_new_context()
        self.manager.update_context_metadata(self.context_id, {"routed_system": "engineering", "routed_agent": "coder"})

    def context_manager(self):
        return self.manager

    @property
    def idle(self):
        return True

    async def close(self):
        return None

    def say(self, role: str, text: str, **metadata) -> str:
        self.manager.add_message(role, text, metadata or None)
        return self.manager.conversation_view(self.context_id)["messages"][-1].metadata["message_id"]

    def session_items(self, *items: dict) -> None:
        with sqlite3.connect(self.agent_sessions_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS agent_messages (id INTEGER PRIMARY KEY, session_id TEXT, message_data TEXT)")
            for item in items:
                db.execute(
                    "INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)",
                    (f"agent_coder_{self.context_id}", json.dumps(item)),
                )


@pytest.fixture
def space(tmp_path):
    return Space(tmp_path)


# -- redaction ------------------------------------------------------------------------
def test_secrets_are_scrubbed_by_value_and_by_shape():
    redactor = Redactor({"OPENROUTER_API_KEY": "or-key-1234567890"})

    text = redactor.text(
        "key or-key-1234567890, sk-abcdefghijklmnopqrstuv, Authorization: Bearer abcdefghijklmnop.qrst "
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    )

    assert "or-key-1234567890" not in text and "[redacted: OPENROUTER_API_KEY]" in text
    assert "sk-abc" not in text and "abcdefghijklmnop.qrst" not in text and "MIIE" not in text


def test_only_secret_looking_environment_values_count():
    found = environment_secrets({"OPENROUTER_API_KEY": "or-key-1234567890", "PATH": "/usr/bin:/bin", "GH_TOKEN": "short"})

    assert found == {"OPENROUTER_API_KEY": "or-key-1234567890"}


def test_long_text_is_cut_and_says_how_much():
    text = Redactor({}, max_text=10).text("x" * 25)

    assert text.startswith("x" * 10) and "15 more characters left out" in text


# -- evidence -------------------------------------------------------------------------
def test_the_evidence_is_what_the_answer_had_and_did(space):
    space.say("user", "list the files")
    answer = space.say(
        "assistant", "done: a.txt", agent="coder", turn_id="t1",
        trace=[{"id": "s1", "kind": "tool", "title": "bash_tool", "body": "ls\na.txt"}],
    )
    space.say("user", "and now?")
    space.say("assistant", "b.txt too", agent="coder")
    space.session_items({"role": "user", "content": "list the files"}, {"type": "function_call", "name": "bash_tool"})

    found = collect(space, space.context_id, answer, redactor=Redactor({}))

    assert found["target"] == {
        "context_id": space.context_id, "message_id": answer, "system": "engineering", "agent": "coder", "turn_id": "t1",
    }
    assert [m["content"] for m in found["conversation"]["messages"]] == ["list the files", "done: a.txt"]
    assert found["turn"]["steps"][0]["title"] == "bash_tool"
    assert found["agent_session"]["items"][1] == {"type": "function_call", "name": "bash_tool"}
    assert found["config"]["agent"]["tools"] == ["bash_tool"]
    assert found["config"]["models"]["fast"]["name"] == "gpt-x"
    assert set(found["config"]["models"]["fast"]) == {"name", "provider", "temperature", "max_tokens", "context_window", "reasoning"}
    # A later answer would have replaced the model context: an older one gets none.
    assert found["model_context"]["assembly"] is None and found["model_context"]["note"]


def test_the_latest_answer_carries_the_model_context(space):
    space.manager.update_context_metadata(
        space.context_id, {"last_context_assembly": {"sections": [{"key": "base", "content": "You are Coder."}]}}
    )
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")

    found = collect(space, space.context_id, answer, redactor=Redactor({}))

    assert found["model_context"]["assembly"]["sections"][0]["content"] == "You are Coder."
    assert found["model_context"]["note"] is None


def test_the_turn_brings_only_its_own_agent_runs(space):
    from datetime import datetime

    from schemas.schemas import AgentExecution

    def run(name, at):
        space.manager.add_execution(AgentExecution(agent_name=name, input_message="x", start_time=at, context_id=space.context_id))

    space.say("user", "first")
    space.say("assistant", "one", agent="coder")
    earlier = datetime.now().timestamp()
    run("earlier-turn", earlier - 3600)
    space.say("user", "second")
    run("this-turn", datetime.now().timestamp())
    answer = space.say("assistant", "two", agent="coder")

    found = collect(space, space.context_id, answer, redactor=Redactor({}))

    assert [run["agent_name"] for run in found["turn"]["executions"]] == ["this-turn"]


def test_tool_environment_and_provider_details_stay_out(space):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")

    found = collect(space, space.context_id, answer, redactor=Redactor({}))

    assert "env_vars" not in found["config"]["tools"]["bash_tool"]
    assert "tool-env-secret" not in json.dumps(found)


def test_only_an_agents_answer_can_be_reviewed(space):
    question = space.say("user", "hi")

    with pytest.raises(EvidenceError):
        collect(space, space.context_id, question)
    with pytest.raises(EvidenceError):
        collect(space, space.context_id, "no-such-message")


def test_grid_version_names_the_running_commit():
    evidence_module.grid_version.cache_clear()

    version = evidence_module.grid_version()

    assert version["commit"] is None or len(version["commit"]) == 40


# -- store ----------------------------------------------------------------------------
def _review(review_id: str = "r1", **changes) -> Review:
    fields = dict(
        id=review_id, created_at=100.0, user_id=ALICE.id, username="alice", context_id="ctx-1", message_id="m1",
        system="engineering", agent="coder", note="wrong file", status="new", origin="user",
    )
    return Review(**{**fields, **changes})


def test_a_review_is_stored_with_its_evidence(tmp_path):
    store = ReviewStore(tmp_path)
    store.add(_review(), {"target": {"message_id": "m1"}})

    assert store.get("r1") == _review()
    assert store.evidence("r1") == {"target": {"message_id": "m1"}}
    assert store.of_user(ALICE.id) == [_review()]
    assert store.set_status("r1", "in_review") and store.get("r1").status == "in_review"
    store.close()


def test_the_daily_count_is_of_reviews_the_user_filed(tmp_path):
    store = ReviewStore(tmp_path)
    store.add(_review("r1", created_at=10.0), {})
    store.add(_review("r2", created_at=20.0), {})
    store.add(_review("r3", created_at=20.0, origin="admin", opened_by="b" * 32), {})

    assert store.count_filed_since(ALICE.id, 15.0) == 1
    store.close()


def test_a_review_id_is_never_a_path(tmp_path):
    store = ReviewStore(tmp_path)

    with pytest.raises(ValueError):
        store.evidence("../accounts")
    store.close()


# -- desk -----------------------------------------------------------------------------
def _desk(tmp_path, **policy) -> ReviewDesk:
    return ReviewDesk(ReviewStore(tmp_path / "reviews"), lambda: ReviewPolicy(**policy), clock=lambda: 1000.0)


async def test_filing_freezes_the_answer(tmp_path, space):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")
    desk = _desk(tmp_path)

    review = await desk.file(space, ALICE, space.context_id, answer, "  it ignored me  ")

    assert review.note == "it ignored me" and review.agent == "coder" and review.origin == "user"
    assert desk.store.evidence(review.id)["target"]["message_id"] == answer


@pytest.mark.parametrize(
    "policy,note,status",
    [({"enabled": False}, "", 403), ({"max_note_chars": 3}, "too long", 400)],
)
async def test_the_policy_decides_what_may_be_filed(tmp_path, space, policy, note, status):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")

    with pytest.raises(ReviewRefused) as refused:
        await _desk(tmp_path, **policy).file(space, ALICE, space.context_id, answer, note)

    assert refused.value.status == status


async def test_a_user_files_at_most_the_daily_number(tmp_path, space):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")
    desk = _desk(tmp_path, reviews_per_day=1)
    await desk.file(space, ALICE, space.context_id, answer, "")

    with pytest.raises(ReviewRefused) as refused:
        await desk.file(space, ALICE, space.context_id, answer, "")

    assert refused.value.status == 429


# -- routes ---------------------------------------------------------------------------
async def _alice(connection):
    return ALICE


def test_a_user_files_a_review_from_their_chat_and_sees_it(tmp_path, space):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")
    server = WebChatServer(
        SimpleNamespace(voice_enabled=lambda: False), SpacePool(lambda user_id: space), identify=_alice,
        warm_user=None, reviews=_desk(tmp_path),
    )
    client = TestClient(server.app, headers=SAME_SITE)

    filed = client.post(f"/api/chat/conversations/{space.context_id}/reviews", json={"message_id": answer, "note": "why?"})
    missing = client.post(f"/api/chat/conversations/{space.context_id}/reviews", json={"message_id": "nope"})
    mine = client.get("/api/reviews")

    assert filed.status_code == 201 and filed.json()["note"] == "why?"
    assert "opened_by" not in filed.json() and "user_id" not in filed.json()
    assert missing.status_code == 404
    assert [row["id"] for row in mine.json()] == [filed.json()["id"]]


# -- admins ---------------------------------------------------------------------------
BOSS = User(id="b" * 32, username="boss", role="admin")


def _clients(tmp_path, space, **policy):
    """Alice and an admin on one server; the X-User header says who asks.

    Without accounts the routes know no user but the asking admin, so the
    admin's "own" chats here are the shared test space.
    """
    users = {ALICE.id: ALICE, BOSS.id: BOSS}

    async def identify(connection):
        return users[connection.headers["X-User"]]

    server = WebChatServer(
        SimpleNamespace(voice_enabled=lambda: False), SpacePool(lambda user_id: space), identify=identify,
        warm_user=None, reviews=_desk(tmp_path, **policy),
    )
    return (
        TestClient(server.app, headers={**SAME_SITE, "X-User": ALICE.id}),
        TestClient(server.app, headers={**SAME_SITE, "X-User": BOSS.id}),
    )


def test_admins_read_every_review_with_its_evidence_and_move_it_on(tmp_path, space):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")
    as_alice, as_boss = _clients(tmp_path, space)
    filed = as_alice.post(f"/api/chat/conversations/{space.context_id}/reviews", json={"message_id": answer}).json()

    listed = as_boss.get("/api/admin/reviews").json()
    case = as_boss.get(f"/api/admin/reviews/{filed['id']}").json()
    moved = as_boss.patch(f"/api/admin/reviews/{filed['id']}", json={"status": "in_review"})

    assert [row["username"] for row in listed["reviews"]] == ["alice"] and listed["admin_any_chat"] is False
    assert case["evidence"]["target"]["message_id"] == answer
    assert moved.status_code == 200 and as_alice.get("/api/reviews").json()[0]["status"] == "in_review"
    assert as_alice.get("/api/admin/reviews").status_code == 403
    assert as_boss.patch(f"/api/admin/reviews/{filed['id']}", json={"status": "gone"}).status_code == 422


def test_unreported_chats_stay_closed_to_admins_by_default(tmp_path, space):
    _, as_boss = _clients(tmp_path, space)

    assert as_boss.get(f"/api/admin/review-chats/{BOSS.id}").status_code == 403


def test_with_the_policy_an_admin_picks_an_answer_and_opens_a_review(tmp_path, space, caplog):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")
    as_alice, as_boss = _clients(tmp_path, space, admin_any_chat=True)

    chats = as_boss.get(f"/api/admin/review-chats/{BOSS.id}").json()
    answers = as_boss.get(f"/api/admin/review-chats/{BOSS.id}/{space.context_id}").json()
    with caplog.at_level("WARNING", logger="grid.web_chat.review.audit"):
        opened = as_boss.post(
            f"/api/admin/review-chats/{BOSS.id}/{space.context_id}/reviews", json={"message_id": answer, "note": "check"}
        ).json()

    assert space.context_id in [chat["id"] for chat in chats]
    assert answers == [{"id": answer, "agent": "coder", "timestamp": answers[0]["timestamp"], "preview": "hello"}]
    assert opened["origin"] == "admin" and opened["opened_by"] == BOSS.id
    assert any("opened review" in record.getMessage() for record in caplog.records)
    assert as_alice.get(f"/api/admin/review-chats/{ALICE.id}").status_code == 403
    assert as_boss.get("/api/admin/review-chats/nobody").status_code == 404


async def test_a_review_an_admin_opened_shows_in_its_owners_list(tmp_path, space):
    space.say("user", "hi")
    answer = space.say("assistant", "hello", agent="coder")
    desk = _desk(tmp_path, admin_any_chat=True)

    review = await desk.open(space, BOSS, ALICE, space.context_id, answer, "")

    assert review.user_id == ALICE.id and review.opened_by == BOSS.id
    assert [mine.id for mine in desk.of_user(ALICE)] == [review.id]
    # An admin's review is not the user's: it does not count against their day.
    assert desk.store.count_filed_since(ALICE.id, 0) == 0


def test_the_review_page_is_there_only_with_reviews(tmp_path, space):
    def page(reviews):
        server = WebChatServer(
            SimpleNamespace(voice_enabled=lambda: False), SpacePool(lambda user_id: space), identify=_alice,
            warm_user=None, reviews=reviews,
        )
        return TestClient(server.app).get("/admin/review", follow_redirects=False)

    shown = page(_desk(tmp_path))

    assert shown.status_code == 200 and "Grid Reviews" in shown.text
    assert page(None).status_code == 404
