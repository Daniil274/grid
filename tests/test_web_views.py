"""How a stored message serializes: the carried-context seed becomes a card.

The chat only draws the "Context carried from <source>" card when a message
serializes with ``kind`` "context" and a ``context`` object (see
web_chat/js/ui/message.js and transcript.js). A seed written by
web_chat.projects_api carries ``type`` "context_seed"; these tests pin the
mapping that keeps the card rendering.
"""

from types import SimpleNamespace

from web_chat.views import CONTEXT_SEED_TYPE, serialize_message


def test_a_context_seed_serializes_as_a_context_card():
    message = SimpleNamespace(
        role="user",
        content="Context carried from the source chat.",
        metadata={"type": CONTEXT_SEED_TYPE, "source_id": "ctx-source", "source_title": "Source chat"},
    )
    view = serialize_message(message)
    assert view["kind"] == "context"
    assert view["context"] == {"source_title": "Source chat", "source_id": "ctx-source", "project": None}
    # The card shows the seed text; a user bubble would have shown it raw.
    assert view["content"] == "Context carried from the source chat."


def test_a_project_seed_names_the_project_it_came_from():
    message = SimpleNamespace(
        role="user",
        content="Project context: notes",
        metadata={"type": CONTEXT_SEED_TYPE, "source_id": "project:p1", "source_title": "Roadmap"},
    )
    view = serialize_message(message)
    assert view["kind"] == "context"
    assert view["context"]["source_title"] == "Roadmap"
    assert view["context"]["source_id"] == "project:p1"


def test_a_context_seed_carries_an_explicit_project_when_present():
    message = SimpleNamespace(
        role="user",
        content="seed",
        metadata={"type": CONTEXT_SEED_TYPE, "source_title": "t", "project_id": "p1"},
    )
    assert serialize_message(message)["context"]["project"] == "p1"


def test_an_ordinary_message_has_no_context():
    message = SimpleNamespace(role="user", content="hello", metadata={})
    view = serialize_message(message)
    assert view["kind"] is None
    assert view["context"] is None


def test_other_marker_kinds_do_not_become_context():
    message = SimpleNamespace(role="assistant", content="", metadata={"type": "compaction_marker"})
    view = serialize_message(message)
    assert view["kind"] is None
    assert view["context"] is None


def test_a_long_trace_leaves_its_payloads_for_later():
    from web_chat.views import LAZY_FIELD_BYTES, slim_steps

    big = "x" * (LAZY_FIELD_BYTES + 1)
    steps = [
        {"id": "s1", "kind": "tool", "title": "read", "result_payload": {"parts": [big]}, "detail": "{}"},
        {"id": "s2", "kind": "agent", "title": "coder", "detail": big, "body": "done"},
        {"id": "s3", "kind": "tool", "title": "ls", "body": "a b"},
    ]
    slim = slim_steps(steps)
    assert slim[0]["lazy"] and "result_payload" not in slim[0] and slim[0]["detail"] == "{}"
    assert slim[1]["lazy"] and slim[1]["detail"] == big[:400] and slim[1]["body"] == "done"
    assert slim[2] == steps[2]  # small: left as it was
    assert "result_payload" in steps[0]  # the stored trace is untouched
