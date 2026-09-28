"""The review routes a user has: file one from their chat, see their own.

    POST /api/chat/conversations/{context_id}/reviews   {message_id, note} -> the review
    GET  /api/reviews                                   the user's reviews, newest first

Both act for the identified user in their own space (web_chat.server's
dependencies); a user never reaches another's conversation or reviews here.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from web_chat.identity import User
from web_chat.review.desk import ReviewDesk, ReviewRefused

#: What a user sees of their review: origin says whether they filed it or an
#: admin opened it on their chat; which admin stays with the admins.
_PUBLIC_FIELDS = ("id", "created_at", "context_id", "message_id", "system", "agent", "note", "status", "origin")


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    note: str = Field(default="", max_length=20000)


def register_review_routes(
    api: APIRouter,
    desk: ReviewDesk,
    current_user: Callable[..., Any],
    current_space: Callable[..., Any],
) -> None:
    @api.post("/api/chat/conversations/{context_id}/reviews", status_code=201)
    async def file_review(
        context_id: str,
        body: ReviewRequest,
        user: User = Depends(current_user),
        space: Any = Depends(current_space),
    ) -> dict[str, Any]:
        try:
            review = await desk.file(space, user, context_id, body.message_id, body.note)
        except ReviewRefused as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        return _public(review.to_dict())

    @api.get("/api/reviews")
    async def my_reviews(user: User = Depends(current_user)) -> list[dict[str, Any]]:
        return [_public(review.to_dict()) for review in desk.of_user(user)]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in _PUBLIC_FIELDS}
