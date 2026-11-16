"""The declarative interface document and its stored state, checked.

Run on its own (no pytest needed, and no heavy package __init__)::

    python3 tests/test_dynamic_ui.py

The two modules under test import only the standard library and each other, so
the cases below exercise the same validation the server runs - a model tool, a
save request and the storage all pass through these functions.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_chat.ui_document import (  # noqa: E402
    UiDocumentError,
    action_of,
    validate_document,
    validate_values,
    value_labels,
)
from web_chat.ui_state import (  # noqa: E402
    SUBMITS_KEY,
    UiSink,
    UiStateError,
    claim_submit,
    compose_submission,
    find_block,
    known_submit,
    new_block,
    origin_of,
    save_values,
    state_of,
)


def comparison() -> dict:
    """The scenario the design note starts with: compare two options, pick one."""
    return {
        "schema_version": 1,
        "id": "comparison",
        "revision": 3,
        "title": "Compare options",
        "fallback_markdown": "Basic costs 10, Pro costs 25.",
        "state_schema": {
            "sort": {"type": "sort", "table": "options", "label": "Sort by"},
            "choice": {"type": "row", "table": "options", "label": "Chosen option"},
        },
        "initial_state": {"sort": "price:asc", "choice": "a"},
        "root": {
            "id": "layout",
            "type": "stack",
            "children": [
                {
                    "id": "sortsel",
                    "type": "select",
                    "label": "Sort by",
                    "bind": "sort",
                    "options": [
                        {"value": "price:asc", "label": "Price ascending"},
                        {"value": "price:desc", "label": "Price descending"},
                    ],
                },
                {
                    "id": "options",
                    "type": "table",
                    "title": "Options",
                    "columns": [
                        {"key": "name", "label": "Name"},
                        {"key": "price", "label": "Price", "align": "end"},
                    ],
                    "rows": [
                        {"id": "a", "name": "Basic", "price": 10},
                        {"id": "b", "name": "Pro", "price": 25},
                    ],
                    "row_label": "name",
                    "sort_state": "sort",
                    "select_state": "choice",
                },
                {"id": "discuss", "type": "button", "label": "Discuss", "action": "discuss", "tone": "primary"},
            ],
        },
        "actions": {
            "discuss": {
                "kind": "submit_to_agent",
                "label": "Discuss",
                "prompt": "Discuss the option I chose.",
            }
        },
    }


class FakeMessage:
    def __init__(self, message_id: str, role: str = "assistant", metadata: dict | None = None):
        self.role = role
        self.metadata = {"message_id": message_id, **(metadata or {})}


class FakeManager:
    """Only what ui_state touches: a conversation of messages and its metadata."""

    def __init__(self, messages: list[FakeMessage], metadata: dict | None = None):
        self._messages = messages
        self._metadata = dict(metadata or {})

    def conversation_view(self, context_id: str):
        if context_id != "chat-1":
            return None
        return {"messages": self._messages, "metadata": self._metadata}

    def update_message_metadata_by_id(self, context_id: str, message_id: str, updates: dict) -> bool:
        if context_id != "chat-1":
            return False
        for message in self._messages:
            if message.metadata.get("message_id") == message_id:
                message.metadata.update(updates)
                return True
        return False

    def mutate_message_metadata_by_id(self, context_id: str, message_id: str, mutate):
        if context_id != "chat-1":
            return None
        for message in self._messages:
            if message.metadata.get("message_id") == message_id:
                updated = mutate(message.metadata)
                if updated is not None:
                    message.metadata = updated
                return dict(message.metadata)
        return None

    def get_context_metadata(self, context_id: str):
        return dict(self._metadata) if context_id == "chat-1" else {}

    def update_context_metadata(self, context_id: str, updates: dict) -> None:
        self._metadata.update(updates)


def stored_chat() -> tuple[FakeManager, dict]:
    document = validate_document(comparison())
    block = new_block(document, [])
    message = FakeMessage("msg-1", metadata={"ui": [block]})
    return FakeManager([FakeMessage("msg-0", role="user"), message]), block


class DocumentTests(unittest.TestCase):
    def test_the_comparison_document_is_canonical_and_round_trips(self):
        checked = validate_document(comparison())
        self.assertEqual(checked["id"], "comparison")
        self.assertEqual(checked["initial_state"], {"sort": "price:asc", "choice": "a"})
        # A JSON string is accepted exactly as a dict would be.
        self.assertEqual(validate_document(json.dumps(comparison())), checked)

    def test_a_table_and_a_select_may_share_a_sort_state(self):
        # The known bug: this is the normal way to sort a table from a control.
        checked = validate_document(comparison())
        self.assertEqual(checked["root"]["children"][1]["sort_state"], "sort")

    def test_a_select_must_offer_the_value_its_state_starts_with(self):
        """A select that cannot show the initial value would lie about the state."""
        broken = comparison()
        broken["initial_state"]["sort"] = "name:asc"
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(broken)
        self.assertEqual(raised.exception.code, "ui.schema")
        self.assertIn("does not offer the initial value", raised.exception.message)

    def test_a_second_select_for_one_state_is_refused(self):
        document = comparison()
        document["root"]["children"].append(
            {
                "id": "sortsel2",
                "type": "select",
                "label": "Sort again",
                "bind": "sort",
                "options": [{"value": "price:asc", "label": "Price ascending"}],
            }
        )
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertIn("more than one select", raised.exception.message)

    def test_a_state_no_control_can_change_is_refused(self):
        document = comparison()
        document["state_schema"]["extra"] = {"type": "enum", "values": ["x"]}
        document["initial_state"]["extra"] = "x"
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.schema")
        self.assertIn("not bound", raised.exception.message)

    def test_unknown_components_and_fields_are_refused(self):
        document = comparison()
        document["root"]["children"].append({"id": "raw", "type": "html", "text": "<b>x</b>"})
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.unknown_component")

        document = comparison()
        document["root"]["children"][2]["onclick"] = "alert(1)"
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.schema")
        self.assertIn("unknown field", raised.exception.message)

    def test_a_nested_object_in_a_cell_is_refused(self):
        document = comparison()
        document["root"]["children"][1]["rows"][0]["price"] = {"nested": 1}
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.schema")

    def test_duplicate_node_and_row_ids_are_refused(self):
        document = comparison()
        document["root"]["children"].append({"id": "discuss", "type": "text", "text": "same id"})
        with self.assertRaises(UiDocumentError):
            validate_document(document)

        document = comparison()
        document["root"]["children"][1]["rows"].append({"id": "a", "name": "Again", "price": 1})
        with self.assertRaises(UiDocumentError):
            validate_document(document)

    def test_a_wrong_schema_version_is_refused_not_half_read(self):
        document = comparison()
        document["schema_version"] = 2
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.schema")

    def test_a_button_needs_a_known_action(self):
        document = comparison()
        document["root"]["children"][2]["action"] = "launch-missiles"
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.unknown_action")

    def test_limits_are_enforced(self):
        document = comparison()
        document["root"]["children"][1]["rows"] = [
            {"id": f"r{i}", "name": f"Row {i}", "price": i} for i in range(101)
        ]
        with self.assertRaises(UiDocumentError) as raised:
            validate_document(document)
        self.assertEqual(raised.exception.code, "ui.limit")

    def test_values_are_checked_against_the_document(self):
        document = validate_document(comparison())
        self.assertEqual(
            validate_values(document, {"sort": "price:desc", "choice": "b"}),
            {"sort": "price:desc", "choice": "b"},
        )
        for bad in (
            {"sort": "price:desc"},  # missing key
            {"sort": "price:desc", "choice": "b", "other": 1},  # unknown key
            {"sort": "price:sideways", "choice": "b"},  # bad sort
            {"sort": "missing:asc", "choice": "b"},  # a column the table lacks
            {"sort": "price:desc", "choice": "zzz"},  # a row the table lacks
        ):
            with self.subTest(values=bad):
                try:
                    validate_values(document, bad)
                except UiDocumentError as exc:
                    self.assertEqual(exc.code, "ui.bad_state")
                else:
                    self.fail(f"{bad!r} was accepted")

    def test_values_are_named_as_the_person_saw_them(self):
        document = validate_document(comparison())
        lines = value_labels(document, {"sort": "price:desc", "choice": "b"})
        self.assertIn("- Sort by: Price, descending", lines)
        self.assertIn("- Chosen option: Pro", lines)

    def test_an_unknown_action_is_named(self):
        document = validate_document(comparison())
        with self.assertRaises(UiDocumentError) as raised:
            action_of(document, "missing")
        self.assertEqual(raised.exception.code, "ui.unknown_action")


class StateTests(unittest.TestCase):
    def test_a_block_starts_at_initial_state_and_revision_zero(self):
        manager, block = stored_chat()
        self.assertEqual(state_of(block), {"values": {"sort": "price:asc", "choice": "a"}, "state_revision": 0})
        self.assertEqual(block["revision"], 3)
        self.assertTrue(block["id"].startswith("ui-"))

    def test_saving_increments_the_revision_and_is_visible_on_the_message(self):
        manager, block = stored_chat()
        answer = save_values(
            manager, "chat-1", "msg-1", block["id"],
            {"values": {"sort": "price:desc", "choice": "b"}, "state_revision": 0, "request_id": "r1"},
        )
        self.assertEqual(answer["state_revision"], 1)
        self.assertFalse(answer["duplicate"])
        _, stored = find_block(manager, "chat-1", "msg-1", block["id"])
        self.assertEqual(stored["values"], {"sort": "price:desc", "choice": "b"})

    def test_a_duplicate_request_id_answers_what_is_stored_and_changes_nothing(self):
        manager, block = stored_chat()
        payload = {"values": {"sort": "price:desc", "choice": "b"}, "state_revision": 0, "request_id": "r1"}
        first = save_values(manager, "chat-1", "msg-1", block["id"], payload)
        second = save_values(manager, "chat-1", "msg-1", block["id"], payload)
        self.assertEqual(first["state_revision"], 1)
        self.assertEqual(second["state_revision"], 1)
        self.assertTrue(second["duplicate"])

    def test_a_stale_revision_is_refused_with_the_current_state(self):
        manager, block = stored_chat()
        save_values(
            manager, "chat-1", "msg-1", block["id"],
            {"values": {"sort": "price:desc", "choice": "a"}, "state_revision": 0, "request_id": "r1"},
        )
        with self.assertRaises(UiStateError) as raised:
            save_values(
                manager, "chat-1", "msg-1", block["id"],
                {"values": {"sort": "price:asc", "choice": "b"}, "state_revision": 0, "request_id": "r2"},
            )
        self.assertEqual(raised.exception.code, "ui.stale")
        self.assertEqual(raised.exception.details["state_revision"], 1)
        self.assertEqual(raised.exception.details["values"]["sort"], "price:desc")

    def test_an_invalid_value_is_refused_before_it_is_stored(self):
        manager, block = stored_chat()
        with self.assertRaises(UiStateError) as raised:
            save_values(
                manager, "chat-1", "msg-1", block["id"],
                {"values": {"sort": "nope:asc", "choice": "a"}, "state_revision": 0, "request_id": "r1"},
            )
        self.assertEqual(raised.exception.code, "ui.schema")
        _, stored = find_block(manager, "chat-1", "msg-1", block["id"])
        self.assertEqual(stored["state_revision"], 0)

    def test_a_missing_request_id_is_refused(self):
        manager, block = stored_chat()
        with self.assertRaises(UiStateError) as raised:
            save_values(
                manager, "chat-1", "msg-1", block["id"],
                {"values": {"sort": "price:asc", "choice": "a"}, "state_revision": 0},
            )
        self.assertEqual(raised.exception.code, "ui.schema")

    def test_a_message_of_another_chat_is_not_found(self):
        manager, block = stored_chat()
        with self.assertRaises(UiStateError) as raised:
            find_block(manager, "chat-2", "msg-1", block["id"])
        self.assertEqual(raised.exception.code, "ui.not_found")
        with self.assertRaises(UiStateError) as raised:
            find_block(manager, "chat-1", "msg-1", "ui-not-here")
        self.assertEqual(raised.exception.code, "ui.not_found")

    def test_submission_text_is_composed_from_the_stored_document(self):
        manager, block = stored_chat()
        document = block["document"]
        text = compose_submission(document, "discuss", {"sort": "price:desc", "choice": "b"})
        self.assertIn("Discuss the option I chose.", text)
        self.assertIn("- Chosen option: Pro", text)
        # Nothing the browser might have typed is part of it: the caller only
        # passes ids and values, and labels come from the document.
        self.assertNotIn("http", text)

    def test_origin_names_the_block_and_the_values_the_agent_received(self):
        manager, block = stored_chat()
        document = block["document"]
        origin = origin_of("msg-1", block["id"], "discuss", document, {"sort": "price:desc", "choice": "b"}, 4)
        self.assertEqual(origin["source"], "ui")
        self.assertEqual(origin["action_label"], "Discuss")
        self.assertEqual(origin["document_revision"], 3)
        self.assertEqual(origin["state_revision"], 4)
        self.assertEqual(origin["values"], {"sort": "price:desc", "choice": "b"})

    def test_a_submit_request_id_is_honoured_once(self):
        manager, _ = stored_chat()
        record = {"text": "Discuss it.", "origin": {"source": "ui"}}
        self.assertTrue(claim_submit(manager, "chat-1", "req-1", record))
        self.assertFalse(claim_submit(manager, "chat-1", "req-1", record))
        self.assertEqual(known_submit(manager, "chat-1", "req-1")["text"], "Discuss it.")
        self.assertIsNone(known_submit(manager, "chat-1", "req-2"))
        self.assertIn("req-1", manager.get_context_metadata("chat-1")[SUBMITS_KEY])

    def test_the_sink_refuses_a_second_block_only_after_the_limit(self):
        sink = UiSink()
        document = comparison()
        for revision in (1, 2, 3):
            document = {**copy.deepcopy(document), "revision": revision, "id": f"doc-{revision}"}
            sink.present(document)
        self.assertEqual(len(sink.blocks), 3)
        with self.assertRaises(UiStateError):
            sink.present({**copy.deepcopy(document), "revision": 4, "id": "doc-4"})

    def test_the_sink_validates_at_the_door(self):
        sink = UiSink()
        broken = comparison()
        broken["root"]["children"][0]["bind"] = "missing"
        with self.assertRaises(UiDocumentError):
            sink.present(broken)
        self.assertEqual(sink.blocks, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
