"""Who may file a review, and what filing one does.

A user files a review of an answer in their own conversation; the server
lends their space for it, so nobody files one on another's chat this way.
The deployment's ``review`` policy decides whether reviews are on, how many
a user may file per UTC day and how long a note may be.

Filing freezes the evidence (web_chat.review.evidence) in a worker thread -
it reads files and a database - and stores it with the review.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any, Callable

from schemas.schemas import ReviewPolicy
from web_chat.identity import User
from web_chat.review.evidence import EvidenceError, collect
from web_chat.review.store import Review, ReviewStore

DAY_SECONDS = 24 * 60 * 60


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
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
        note = note.strip()
        if len(note) > policy.max_note_chars:
            raise ReviewRefused(400, f"The note must be at most {policy.max_note_chars} characters long.")
        now = self._clock()
        if self.store.count_filed_since(user.id, now - DAY_SECONDS) >= policy.reviews_per_day:
            raise ReviewRefused(429, f"At most {policy.reviews_per_day} reviews a day; try again later.")
        try:
            evidence = await asyncio.to_thread(collect, space, context_id, message_id)
        except EvidenceError as exc:
            raise ReviewRefused(404, str(exc)) from None
        target = evidence["target"]
        review = Review(
            id=uuid.uuid4().hex,
            created_at=now,
            user_id=user.id,
            username=user.username,
            context_id=context_id,
            message_id=message_id,
            system=target.get("system"),
            agent=target.get("agent"),
            note=note,
            status="new",
            origin="user",
        )
        await asyncio.to_thread(self.store.add, review, evidence)
        return review

    def of_user(self, user: User) -> list[Review]:
        return self.store.of_user(user.id)
