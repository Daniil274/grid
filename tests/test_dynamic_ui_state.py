"""Interface state and button presses against the real stores and session.

These cases cover what the stdlib tests in ``tests/test_dynamic_ui.py`` cannot
import: the real ``ContextManager`` lock (a concurrent save must not lose a
choice) and the websocket command loop's refusal frames (a refused button must
settle its own attempt without touching a running turn).

Run with the project environment::

    .map-venv/bin/python -m pytest -q tests/test_dynamic_ui_state.py
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, patch

import pytest

import web_chat.ui_state as ui_state
from core.context import ContextManager
from tests.test_dynamic_ui import comparison
from tests.test_web_chat_session import Socket, space_for
from web_chat.session import chat_session
from web_chat.ui_document import validate_document
from web_chat.ui_state import new_block, save_values


def stored_block(manager: ContextManager) -> tuple[str, dict]:
    """Store one assistant answer carrying a block; return its id and block."""
    document = validate_document(comparison())
    block = new_block(document, [])
    manager.add_message("assistant", "An answer", metadata={"ui": [block]}, context_id="ctx")
    message = manager.conversation_view("ctx")["messages"][-1]
    return message.metadata["message_id"], block


def test_two_concurrent_saves_of_one_revision_never_both_win():
    """The read-check-write is atomic: one saves, the other is told it is stale."""
    manager = ContextManager()
    manager.ensure_context("ctx")
    message_id, block = stored_block(manager)

    barrier = threading.Barrier(2)
    original = ui_state.validate_values

    def both_validated(document, raw, **kwargs):
        # Both threads pass validation together, so only the write step decides:
        # without a lock spanning read and write both would answer "saved".
        values = original(document, raw, **kwargs)
        barrier.wait(timeout=5)
        return values

    saved: list[dict] = []
    refused: list[ui_state.UiStateError] = []

    def save(sort: str, choice: str, request_id: str) -> None:
        try:
            saved.append(save_values(
                manager, "ctx", message_id, block["id"],
                {
                    "values": {"sort": sort, "choice": choice},
                    "state_revision": 0,
                    "request_id": request_id,
                },
            ))
        except ui_state.UiStateError as exc:  # noqa: PERF203 - one refusal is expected
            refused.append(exc)

    with patch.object(ui_state, "validate_values", both_validated):
        threads = [
            threading.Thread(target=save, args=("price:desc", "a", "r1")),
            threading.Thread(target=save, args=("price:asc", "b", "r2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    assert len(saved) == 1, saved
    assert len(refused) == 1, refused
    assert refused[0].code == "ui.stale"
    stored = ui_state.find_block(manager, "ctx", message_id, block["id"])[1]
    assert stored["state_revision"] == 1
    assert stored["values"] == saved[0]["values"]


def ui_payload(answer_id: str, block: dict, **overrides) -> str:
    payload = {
        "action": "ui_action",
        "message_id": answer_id,
        "ui_id": block["id"],
        "action_id": "discuss",
        "document_revision": block["revision"],
        "state_revision": 0,
        "values": {"sort": "price:desc", "choice": "b"},
        "request_id": "submit-1",
    }
    payload.update(overrides)
    return json.dumps(payload)


async def refused_action(space, socket, task, payload: str) -> dict:
    await socket.incoming.put(payload)
    frame = await socket.event("ui_refused")
    await socket.incoming.put(None)
    await task
    return frame


@pytest.mark.asyncio
async def test_a_stale_button_press_is_a_scoped_refusal_not_a_stuck_turn():
    async def run(**kwargs):  # pragma: no cover - no turn may start
        raise AssertionError("a stale press must not start a turn")

    space, socket = space_for(run), Socket()
    message_id, block = stored_block(space.context_manager())
    task = asyncio.create_task(chat_session(space, socket, "ctx"))

    frame = await refused_action(
        space, socket, task,
        ui_payload(message_id, block, state_revision=7),
    )

    assert frame["type"] == "ui_refused"
    assert frame["code"] == "ui.stale"
    assert frame["message_id"] == message_id
    assert frame["ui_id"] == block["id"]
    # The frame carries the state to adopt and settles this one attempt; the
    # generic error/busy frames would leave a page streaming forever.
    assert frame["state_revision"] == 0
    assert socket.outgoing.empty()


@pytest.mark.asyncio
async def test_a_button_press_while_busy_is_scoped_to_the_press():
    async def run(**kwargs):  # pragma: no cover - the running turn is elsewhere
        raise AssertionError("the busy press must not start another turn")

    space, socket = space_for(run), Socket()
    message_id, block = stored_block(space.context_manager())
    assert space.turns.claim("ctx")
    task = asyncio.create_task(chat_session(space, socket, "ctx"))

    frame = await refused_action(space, socket, task, ui_payload(message_id, block))

    assert frame["code"] == "ui.busy"
    # The conversation's own turn was not touched or settled by the refusal.
    assert space.turns.is_claimed("ctx")
    assert socket.outgoing.empty()


@pytest.mark.asyncio
async def test_a_button_press_for_another_chat_is_refused():
    async def run(**kwargs):  # pragma: no cover
        raise AssertionError("a missing block must not start a turn")

    space, socket = space_for(run), Socket()
    message_id, block = stored_block(space.context_manager())
    task = asyncio.create_task(chat_session(space, socket, "ctx"))

    frame = await refused_action(
        space, socket, task,
        ui_payload(message_id, block, message_id="not-this-answer"),
    )

    assert frame["code"] == "ui.not_found"
    assert socket.outgoing.empty()


@pytest.mark.asyncio
async def test_a_button_press_becomes_one_ordinary_turn_and_a_repeat_starts_none():
    run = AsyncMock(return_value="Ready")
    space, socket = space_for(run), Socket()
    message_id, block = stored_block(space.context_manager())
    task = asyncio.create_task(chat_session(space, socket, "ctx"))

    await socket.incoming.put(ui_payload(message_id, block))
    submitted = await socket.event("ui_submitted")
    # The sentence is the server's: the action prompt plus labelled values.
    assert submitted["text"].startswith("Discuss the option I chose.")
    assert "- Sort by: Price, descending" in submitted["text"]
    assert "- Chosen option: Pro" in submitted["text"]
    assert submitted["origin"]["message_id"] == message_id
    assert submitted["origin"]["state_revision"] == 1
    assert (await socket.event("final_output"))["content"] == "Ready"
    await socket.event("done")

    assert run.await_count == 1
    # The agent reads the composed sentence, not anything the browser sent.
    assert run.await_args.kwargs["message"].startswith("Discuss the option I chose.")

    # The same request id is the same press: acknowledge, start nothing.
    await socket.incoming.put(ui_payload(message_id, block))
    repeat = await socket.event("ui_submitted")
    assert repeat.get("duplicate") is True
    assert run.await_count == 1

    await socket.incoming.put(None)
    await task


@pytest.mark.asyncio
async def test_an_honoured_press_retried_while_busy_is_acknowledged():
    run = AsyncMock(return_value="Unexpected")
    space, socket = space_for(run), Socket()
    manager = space.context_manager()
    message_id, block = stored_block(manager)
    record = {"text": "Already sent", "origin": {"message_id": message_id, "ui_id": block["id"]}}
    assert ui_state.claim_submit(manager, "ctx", "submit-1", record)
    assert space.turns.claim("ctx")
    task = asyncio.create_task(chat_session(space, socket, "ctx"))

    await socket.incoming.put(ui_payload(message_id, block))
    repeat = await socket.event("ui_submitted")
    assert repeat["duplicate"] is True
    assert repeat["text"] == record["text"]
    assert repeat["origin"] == record["origin"]
    assert space.turns.is_claimed("ctx")
    run.assert_not_awaited()
    await socket.incoming.put(None)
    await task
