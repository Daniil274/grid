"""Who may file a review, and what filing one does.

A user files a review of an answer in their own conversation; the server
lends their space for it, so nobody files one on another's chat this way.
The deployment's ``review`` policy decides whether reviews are on, how many
a user may file per UTC day and how long a note may be.

Filing freezes the evidence (web_chat.review.evidence) in a worker thread -
it reads files and a database - and stores it with the review.

Admins read every review and move it through its statuses. With the policy's
``admin_any_chat`` an admin may also look through a user's conversations and
open a review on an answer the user did not report; each look and each such
review is written to the audit log, and the review names the admin and shows
in the user's own list.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Optional

from core.context import is_tool_result
from schemas.schemas import ReviewPolicy
from web_chat.identity import User
from web_chat.review.evidence import EvidenceError, collect
from web_chat.review.store import Review, ReviewStore, Status

DAY_SECONDS = 24 * 60 * 60
#: How much of an answer the admin's picker shows.
PREVIEW_CHARS = 200

audit = logging.getLogger("grid.web_chat.review.audit")


class ReviewRefused(Exception):
    """A review the policy does not allow; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ReviewDesk:
    """Files reviews under the deployment's policy, and lists them."""

    def __init__(
        self,
        store: ReviewStore,
        policy: Callable[[], ReviewPolicy],
        *,
        agents: Optional[Any] = None,
        evolution: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """*agents* (web_chat.review.agents.ReviewAgents) analyse reviews for
        the admins; *evolution* (web_chat.review.evolution.EvolutionOutbox)
        sends their proposals to the evolution loop. None offers neither."""
        self.store = store
        self.agents = agents
        self.evolution = evolution
        self._policy = policy
        self._clock = clock
        # One filing per user at a time, so the daily limit counts every one.
        self._filing: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def enabled(self) -> bool:
        return self._policy().enabled

    async def file(self, space: Any, user: User, context_id: str, message_id: str, note: str) -> Review:
        """Freeze answer *message_id* of the user's conversation as a new review."""
        async with self._filing[user.id]:
            return await self._file(space, user, context_id, message_id, note)

    async def _file(self, space: Any, user: User, context_id: str, message_id: str, note: str) -> Review:
        policy = self._policy()
        if not policy.enabled:
            raise ReviewRefused(403, "Reviews are turned off on this server.")
        now = self._clock()
        if self.store.count_filed_since(user.id, now - DAY_SECONDS) >= policy.reviews_per_day:
            raise ReviewRefused(429, f"At most {policy.reviews_per_day} reviews a day; try again later.")
        return await self._freeze(space, user, context_id, message_id, note, origin="user")

    async def _freeze(
        self, space: Any, owner: User, context_id: str, message_id: str, note: str, *, origin: str,
        opened_by: Optional[str] = None,
    ) -> Review:
        """Collect the evidence of *owner*'s answer and store it as a new review."""
        policy = self._policy()
        note = note.strip()
        if len(note) > policy.max_note_chars:
            raise ReviewRefused(400, f"The note must be at most {policy.max_note_chars} characters long.")
        try:
            evidence = await asyncio.to_thread(collect, space, context_id, message_id)
        except EvidenceError as exc:
            raise ReviewRefused(404, str(exc)) from None
        target = evidence["target"]
        review = Review(
            id=uuid.uuid4().hex,
            created_at=self._clock(),
            user_id=owner.id,
            username=owner.username,
            context_id=context_id,
            message_id=message_id,
            system=target.get("system"),
            agent=target.get("agent"),
            note=note,
            status="new",
            origin=origin,
            opened_by=opened_by,
        )
        await asyncio.to_thread(self.store.add, review, evidence)
        return review

    def of_user(self, user: User) -> list[Review]:
        return self.store.of_user(user.id)

    # -- admins ------------------------------------------------------------------
    def all(self, status: Optional[Status] = None) -> list[Review]:
        return self.store.all(status)

    def case(self, review_id: str) -> Optional[tuple[Review, dict[str, Any]]]:
        """A review with its evidence, or None."""
        review = self.store.get(review_id)
        if review is None:
            return None
        return review, self.store.evidence(review_id) or {}

    def set_status(self, review_id: str, status: Status) -> bool:
        return self.store.set_status(review_id, status)

    @property
    def admin_any_chat(self) -> bool:
        policy = self._policy()
        return policy.enabled and policy.admin_any_chat

    def chats_of(self, space: Any, admin: User, owner: User) -> list[dict[str, Any]]:
        """*owner*'s conversations, for an admin choosing an answer to review."""
        self.require_any_chat()
        audit.warning("Admin %s (%s) listed the conversations of %s (%s)", admin.username, admin.id, owner.username, owner.id)
        views = space.context_manager().conversation_views()
        return sorted(
            (
                {"id": view["id"], "title": (view.get("metadata") or {}).get("title") or "", "updated_at": view.get("updated_at")}
                for view in views
            ),
            key=lambda chat: str(chat["updated_at"] or ""),
            reverse=True,
        )

    def answers_in(self, space: Any, admin: User, owner: User, context_id: str) -> list[dict[str, Any]]:
        """The agent answers of one of *owner*'s conversations, newest first."""
        self.require_any_chat()
        view = space.context_manager().conversation_view(context_id)
        if view is None:
            raise ReviewRefused(404, "Conversation not found")
        audit.warning(
            "Admin %s (%s) read the answers of %s's conversation %s", admin.username, admin.id, owner.username, context_id
        )
        answers = [
            {
                "id": (message.metadata or {}).get("message_id"),
                "agent": (message.metadata or {}).get("agent"),
                "timestamp": message.timestamp,
                "preview": message.get_text_content()[:PREVIEW_CHARS],
            }
            for message in view["messages"]
            if message.role == "assistant" and not is_tool_result(message)
        ]
        return list(reversed(answers))

    async def open(self, space: Any, admin: User, owner: User, context_id: str, message_id: str, note: str) -> Review:
        """Open a review on an answer *owner* did not report; it names *admin*."""
        self.require_any_chat()
        review = await self._freeze(space, owner, context_id, message_id, note, origin="admin", opened_by=admin.id)
        audit.warning(
            "Admin %s (%s) opened review %s on %s's answer %s", admin.username, admin.id, review.id, owner.username, message_id
        )
        return review

    def require_any_chat(self) -> None:
        """Refuse unless the policy lets admins review chats their users did not report."""
        if not self.admin_any_chat:
            raise ReviewRefused(403, "This server does not let admins review chats their users did not report.")
