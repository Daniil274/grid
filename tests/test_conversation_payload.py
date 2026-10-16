"""A long chat loads light: lazy trace payloads, an ETag, compression."""

from tests.test_branches import three_turns, web_client
from tests.test_run_agent import factory  # noqa: F401 - fixture
from web_chat.views import LAZY_FIELD_BYTES


async def test_a_chat_answers_304_until_it_changes_and_steps_load_on_demand(factory):
    context_id = await three_turns(factory)
    manager = factory.context_manager
    big = "y" * (LAZY_FIELD_BYTES * 4)
    manager.update_last_message_metadata(
        context_id,
        {"trace": [{"id": "s1", "kind": "tool", "title": "read", "status": "done", "body": big}]},
        when=lambda last: last.role == "assistant",
    )
    client = web_client(factory)

    first = client.get(f"/api/chat/conversations/{context_id}", headers={"Accept-Encoding": "gzip"})
    assert first.status_code == 200 and first.headers["content-encoding"] == "gzip"
    step = first.json()["messages"][-1]["trace"][0]
    assert step["lazy"] is True and "body" not in step
    etag = first.headers["etag"]
    assert "no-store" not in first.headers["cache-control"]

    again = client.get(f"/api/chat/conversations/{context_id}", headers={"If-None-Match": etag})
    assert again.status_code == 304 and not again.content

    message_id = first.json()["messages"][-1]["id"]
    full = client.get(f"/api/chat/conversations/{context_id}/messages/{message_id}/steps/s1").json()
    assert full["body"] == big
    assert client.get(f"/api/chat/conversations/{context_id}/messages/{message_id}/steps/nope").status_code == 404

    manager.append_message_to(context_id, "user", "fourth", {"type": "user_input"})
    changed = client.get(f"/api/chat/conversations/{context_id}", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
