"""Calls waiting for the user's answer, as inline buttons in the chat.

The web chat shows them in a rail (``space.awaiting_approval``); here each
waiting call becomes one message with *Approve* and *Decline*. The answer goes
back through ``space.answer_action_review`` - the same door the web chat uses,
and the only one: a button carries no capability of its own, just the id of a
call that waits in a conversation that belongs to the user.

A call is announced once: the store remembers it, so a restart or the next
poll does not repeat the message. When its turn ends, what is left is
forgotten together with it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("grid.web_chat.telegram")

APPROVE = "approve:"
DECLINE = "decline:"
_ANSWERED = "answered"


def keyboard(approval_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"{APPROVE}{approval_id}"},
            {"text": "✖ Decline", "callback_data": f"{DECLINE}{approval_id}"},
        ]]
    }


def answered_markup(approve: bool) -> dict:
    """What the buttons become once the user has answered."""
    return {"inline_keyboard": [[{"text": "Approved" if approve else "Declined", "callback_data": _ANSWERED}]]}


def parse_callback(data: str) -> tuple[bool, str] | None:
    """``("approve:<id>")`` -> ``(True, id)``; None for anything else."""
    data = str(data or "")
    for prefix, approve in ((APPROVE, True), (DECLINE, False)):
        if data.startswith(prefix) and len(data) > len(prefix):
            return approve, data[len(prefix):]
    return None


class Approvals:
    def __init__(self, store: Any, bot: Any) -> None:
        self.store = store
        self.bot = bot

    async def publish(self, space: Any, user_id: str, chat_id: int, thread_id: int,
                      context_id: str) -> int:
        """Announce the calls waiting in *context_id* that were not announced yet."""
        waiting = (space.awaiting_approval() or {}).get(context_id) or []
        sent = 0
        for review in waiting:
            approval_id = str(review.get("approval_id") or "")
            if not approval_id:
                continue
            if not self.store.announce_approval(approval_id, user_id, chat_id, thread_id, context_id):
                continue
            tool = str(review.get("tool") or "a tool")
            message = await self.bot.send_message(
                chat_id,
                f"Approval needed: {tool}\nAnswer here or in the web chat.",
                thread_id=thread_id,
                reply_markup=keyboard(approval_id),
            )
            message_id = message.get("message_id") if isinstance(message, dict) else None
            if message_id:
                self.store.set_approval_message(approval_id, int(message_id))
            sent += 1
        return sent

    def forget_context(self, context_id: str) -> None:
        """Drop what was announced for a conversation whose turn has ended."""
        self.store.forget_context_approvals(context_id)

    async def answer(self, space: Any, approval_id: str, *, approve: bool) -> bool:
        """The user's answer, from the conversation the call waits in."""
        record = self.store.approval(approval_id)
        if record is None:
            return False
        try:
            resolved = space.answer_action_review(
                record.context_id, approval_id, approve=approve
            )
        except Exception:  # noqa: BLE001 - a broken review must not kill the poller
            logger.warning("Telegram approval could not be answered")
            return False
        if resolved:
            await self._mark(record, approve)
            self.store.forget_approval(approval_id)
        return bool(resolved)

    async def _mark(self, record: Any, approve: bool) -> None:
        if record.message_id is None:
            return
        try:
            await self.bot.edit_message_reply_markup(
                record.chat_id, int(record.message_id), answered_markup(approve)
            )
        except Exception:  # noqa: BLE001 - the message may be too old to edit
            logger.info("Telegram approval message could not be updated")
