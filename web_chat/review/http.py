"""The review routes: a user files and sees their own; admins examine them all.

A user, in their own space (web_chat.server's dependencies):

    POST /api/chat/conversations/{context_id}/reviews   {message_id, note} -> the review
    GET  /api/reviews                                   their reviews, newest first

Admins:

    GET   /api/admin/reviews[?status=]                  every review, newest first
    GET   /api/admin/reviews/{id}                       one review with its evidence
    PATCH /api/admin/reviews/{id}                       {status}
    GET   /api/admin/reviews/{id}/analysis              the review agents' conversation and proposals
    POST  /api/admin/reviews/{id}/analysis              {message?}: start a turn of the analysis
    POST  /api/admin/reviews/{id}/proposals/{p}/check   does the proposal's patch apply to this code
    POST  /api/admin/reviews/{id}/proposals/{p}/evolve  send it to the evolution loop as a task
    GET   /api/admin/reviews/{id}/proposals/{p}/task    what became of that task

and, only when the policy's admin_any_chat is on (web_chat.review.desk):

    GET  /api/admin/review-chats/{user_id}                          the user's conversations
    GET  /api/admin/review-chats/{user_id}/{context_id}             its agent answers
    POST /api/admin/review-chats/{user_id}/{context_id}/reviews     {message_id, note}
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from web_chat.identity import User
from web_chat.review.agents import AnalysisBusy
from web_chat.review.desk import ReviewDesk, ReviewRefused, audit
from web_chat.review.evolution import EvolutionError, check_patch
from web_chat.review.store import Status

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


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(default="", max_length=20000)


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status


def register_admin_review_routes(
    api: APIRouter,
    desk: ReviewDesk,
    admin: Callable[..., Any],
    spaces: Any,
    find_user: Callable[[str], Optional[User]],
) -> None:
    """*admin* passes admins only; *spaces* lends a user's space (web_chat.spaces);
    *find_user* names the owner of a user id - the admin themself is always known."""

    def owner_of(user_id: str, actor: User) -> User:
        owner = actor if user_id == actor.id else find_user(user_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="No such user")
        return owner

    def refused(exc: ReviewRefused) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    # Proposals being sent right now: a second click must not send a second task.
    sending: set[tuple[str, str]] = set()

    @api.get("/api/admin/reviews")
    async def all_reviews(status: Optional[Status] = None, _: User = Depends(admin)) -> dict[str, Any]:
        return {
            "reviews": [review.to_dict() for review in desk.all(status)],
            "admin_any_chat": desk.admin_any_chat,
            "evolution": desk.evolution is not None,
        }

    @api.get("/api/admin/reviews/{review_id}")
    async def one_review(review_id: str, _: User = Depends(admin)) -> dict[str, Any]:
        found = desk.case(review_id) if review_id.isalnum() else None
        if found is None:
            raise HTTPException(status_code=404, detail="Review not found")
        review, evidence = found
        return {"review": review.to_dict(), "evidence": evidence}

    @api.patch("/api/admin/reviews/{review_id}")
    async def change_status(review_id: str, body: StatusChange, _: User = Depends(admin)) -> dict[str, Any]:
        if not (review_id.isalnum() and desk.set_status(review_id, body.status)):
            raise HTTPException(status_code=404, detail="Review not found")
        return {"id": review_id, "status": body.status}

    def analysis_agents() -> Any:
        if desk.agents is None:
            raise HTTPException(status_code=404, detail="Review agents are not set up on this server")
        return desk.agents

    @api.get("/api/admin/reviews/{review_id}/analysis")
    async def analysis(review_id: str, _: User = Depends(admin)) -> dict[str, Any]:
        agents = analysis_agents()
        if not review_id.isalnum() or desk.store.get(review_id) is None:
            raise HTTPException(status_code=404, detail="Review not found")
        return agents.state(review_id)

    @api.post("/api/admin/reviews/{review_id}/analysis", status_code=202)
    async def analyse(review_id: str, body: AnalysisRequest, _: User = Depends(admin)) -> dict[str, Any]:
        agents = analysis_agents()
        if not review_id.isalnum():
            raise HTTPException(status_code=404, detail="Review not found")
        try:
            await agents.ask(review_id, body.message)
        except LookupError:
            raise HTTPException(status_code=404, detail="Review not found") from None
        except AnalysisBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return agents.state(review_id)

    def proposal_of(review_id: str, proposal_id: str) -> tuple[Any, dict[str, Any]]:
        agents = analysis_agents()
        if not review_id.isalnum():
            raise HTTPException(status_code=404, detail="Review not found")
        workbench = agents.workbench(review_id)
        proposal = workbench.proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return workbench, proposal

    @api.post("/api/admin/reviews/{review_id}/proposals/{proposal_id}/check")
    async def check(review_id: str, proposal_id: str, _: User = Depends(admin)) -> dict[str, Any]:
        _, proposal = proposal_of(review_id, proposal_id)
        return await asyncio.to_thread(check_patch, proposal.get("change") or "")

    @api.post("/api/admin/reviews/{review_id}/proposals/{proposal_id}/evolve", status_code=201)
    async def evolve(review_id: str, proposal_id: str, actor: User = Depends(admin)) -> dict[str, Any]:
        if desk.evolution is None:
            raise HTTPException(
                status_code=503,
                detail="The evolution loop is not set up: GRID_CONTROL_URL and GRID_CONTROL_TASK_TOKEN",
            )
        workbench, proposal = proposal_of(review_id, proposal_id)
        key = (review_id, proposal_id)
        if proposal.get("task") or key in sending:
            raise HTTPException(status_code=409, detail="This proposal was sent already")
        sending.add(key)
        try:
            task_id = await asyncio.to_thread(desk.evolution.send, review_id, proposal)
            record = workbench.mark_sent(proposal_id, task_id)
        except EvolutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        finally:
            sending.discard(key)
        audit.warning(
            "Admin %s (%s) sent proposal %s of review %s as task %s",
            actor.username, actor.id, proposal_id, review_id, task_id,
        )
        return record

    @api.get("/api/admin/reviews/{review_id}/proposals/{proposal_id}/task")
    async def task(review_id: str, proposal_id: str, _: User = Depends(admin)) -> dict[str, Any]:
        _, proposal = proposal_of(review_id, proposal_id)
        if not proposal.get("task") or desk.evolution is None:
            raise HTTPException(status_code=404, detail="This proposal was not sent")
        try:
            return await asyncio.to_thread(desk.evolution.status, proposal["task"]["id"])
        except EvolutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @api.get("/api/admin/review-chats/{user_id}")
    async def chats(user_id: str, actor: User = Depends(admin)) -> list[dict[str, Any]]:
        owner = owner_of(user_id, actor)
        try:
            # Before the space: leasing it may start the user's container.
            desk.require_any_chat()
            async with spaces.use(owner.id) as space:
                return desk.chats_of(space, actor, owner)
        except ReviewRefused as exc:
            raise refused(exc) from None

    @api.get("/api/admin/review-chats/{user_id}/{context_id}")
    async def answers(user_id: str, context_id: str, actor: User = Depends(admin)) -> list[dict[str, Any]]:
        owner = owner_of(user_id, actor)
        try:
            # Before the space: leasing it may start the user's container.
            desk.require_any_chat()
            async with spaces.use(owner.id) as space:
                return desk.answers_in(space, actor, owner, context_id)
        except ReviewRefused as exc:
            raise refused(exc) from None

    @api.post("/api/admin/review-chats/{user_id}/{context_id}/reviews", status_code=201)
    async def open_review(
        user_id: str, context_id: str, body: ReviewRequest, actor: User = Depends(admin)
    ) -> dict[str, Any]:
        owner = owner_of(user_id, actor)
        try:
            # Before the space: leasing it may start the user's container.
            desk.require_any_chat()
            async with spaces.use(owner.id) as space:
                review = await desk.open(space, actor, owner, context_id, body.message_id, body.note)
        except ReviewRefused as exc:
            raise refused(exc) from None
        return review.to_dict()


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in _PUBLIC_FIELDS}
