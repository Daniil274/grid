"""HTTP and websocket routes of the web chat.

Every API route and the chat socket act for one user, found by the server's
``Identify`` (web_chat.identity), and work in that user's space, lent by the
server's pool for the duration of the request (web_chat.spaces). A route never
reaches a space but through that lease, so what it reads and changes belongs
to the user who asked. Before identifying anyone, a request that changes
something and every socket must come from the chat's own pages
(web_chat.security).

Without accounts the server has one local user, who owns it. With accounts
(web_chat.accounts) a user signs in on ``/login``, and the pages and the API
refuse anyone who has not.

The system configs are the deployment's (web_chat.deployment), shared by all
spaces; saving them marks every space stale, and each is rebuilt on the new
configs once nothing uses it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection

from web_chat.delivery import MessageQueue
from web_chat.deployment import Deployment
from web_chat.identity import DEFAULT_USER, Identify, User, admins_only, single_user
from web_chat.schemas import (
    ActionReviewRequest,
    BranchRequest,
    ConversationCreateRequest,
    ConversationRenameRequest,
    PrepareAgentRequest,
    SettingsStructuredUpdateRequest,
    SettingsYamlUpdateRequest,
)
from web_chat.security import OriginGuard, refusal
from web_chat.space import IsolationUnavailable, UserSpace
from web_chat.spaces import SpacePool
from web_chat.trace import is_tool_result
from web_chat.views import UNTITLED, agent_options, conversation_title, serialize_message, system_options

logger = logging.getLogger("grid.web_chat.server")
ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"
LOGIN_HTML = ROOT / "login.html"

#: How often idle and stale spaces are looked for and retired.
SWEEP_INTERVAL_SECONDS = 60.0


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser must revalidate before reuse.

    The UI is ES modules importing each other. Left to heuristic caching, a
    browser mixes a fresh ``chat.js`` with a stale module it imports after an
    update. ``no-cache`` still allows cheap 304s via ETag/Last-Modified.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


#: Sent with every response, whatever proxy stands in front: no framing by
#: other sites (clickjacking the sign-in page), no content-type guessing, no
#: full URLs in Referer headers to other sites.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class WebChatServer:
    def __init__(
        self,
        deployment: Deployment,
        spaces: SpacePool,
        *,
        auth: Optional[Any] = None,
        identify: Optional[Identify] = None,
        allowed_origins: tuple[str, ...] = (),
        action_review_token: Optional[str] = None,
        warm_user: Optional[str] = DEFAULT_USER,
    ) -> None:
        """``auth`` is a web_chat.accounts.http.SessionAuth for a server with
        accounts; without it ``identify`` decides, by default the single local
        user. ``allowed_origins`` are other sites' origins trusted like the
        server's own (a reverse proxy's public address). ``warm_user``'s space is
        built and its default agent warmed at startup; None warms nobody."""
        if auth is not None and identify is not None:
            raise ValueError("Pass auth or identify, not both")
        self.deployment = deployment
        self.spaces = spaces
        self.auth = auth
        self.action_review_token = action_review_token
        self._identify = auth.identify if auth is not None else identify or single_user()
        self._guard = OriginGuard(allowed_origins)
        self._warm_user = warm_user
        self._sweeper: Optional[asyncio.Task] = None

        self.voice: Optional[Any] = None

        self.app = FastAPI(title="Grid Web Chat", docs_url=None, redoc_url=None, lifespan=self._lifespan)

        @self.app.middleware("http")
        async def security_headers(request: Request, call_next):
            response = await call_next(request)
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
            return response

        self._mount_static()
        self.current_user, self.current_space = self._dependencies()
        # Everything under /api acts for an identified user.
        identified = [Depends(self.current_user)]
        api = APIRouter(dependencies=identified)
        self._register_pages()
        self._register_routes(api)
        from web_chat.agents_api import register_personal_agent_routes

        register_personal_agent_routes(api, self.current_space)
        if auth is not None:
            auth.register_routes(self.app, guard=self._guard, current_user=self.current_user)
        else:
            self._register_voice(api, identified)
        self.app.include_router(api)

    def _register_voice(self, api: APIRouter, identified: list) -> None:
        """Speech, for a server without accounts only.

        It runs on models loaded once for the whole server and no user limit
        counts it, so a server with accounts does not offer it at all: its
        routes are not there. A single-user server offers it when the config
        turns it on (``voice.enabled``).
        """
        from web_chat.voice import register_voice_routes
        from web_chat.voice_turns import register_turn_routes

        register_turn_routes(api, self.current_space)
        self.voice = register_voice_routes(self.app, self.deployment, dependencies=identified)

    def _dependencies(self):
        """The route dependencies: the identified user, and that user's space."""
        identify, spaces, guard = self._identify, self.spaces, self._guard

        async def current_user(connection: HTTPConnection) -> User:
            guard.check(connection)
            return await identify(connection)

        async def current_space(
            connection: HTTPConnection, user: User = Depends(current_user)
        ) -> AsyncIterator[UserSpace]:
            # Only building a space raises IsolationUnavailable. It is refused,
            # never degraded: the agents do not run on the host instead.
            try:
                async with spaces.use(user.id) as space:
                    yield space
            except IsolationUnavailable as exc:
                logger.error("Space of %s not built: %s", user.id, exc)
                raise refusal(connection, status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None

        return current_user, current_space

    def _mount_static(self) -> None:
        self.app.mount("/static", RevalidatedStaticFiles(directory=str(ROOT)), name="static")

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        """Warm the first space and sweep spaces while the app serves; then close
        the spaces and speech."""
        if self._warm_user is not None:
            async with self.spaces.use(self._warm_user) as space:
                space.schedule_warmup()
        self._sweeper = asyncio.get_running_loop().create_task(self._sweep_forever(), name="space-sweeper")
        try:
            yield
        finally:
            self._sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await self._sweeper
            await self.spaces.close()
            if self.voice is not None:
                self.voice.close()

    async def _sweep_forever(self) -> None:
        """Periodic upkeep: idle spaces go, and with accounts, dead sessions."""
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await self.spaces.sweep()
                if self.auth is not None:
                    self.auth.maintain()
            except Exception:
                logger.exception("Server upkeep failed")

    def _require_action_review_token(self, supplied: Optional[str]) -> None:
        expected = self.action_review_token
        if not expected:
            raise HTTPException(status_code=503, detail="Action review API is disabled")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="Invalid action review token")

    def _reviewer(self):
        """Who resolves the actions the operator's policy sends to review.

        With accounts: admins. The policy is the operator's, so the user whose
        agent asked must not be the one to wave the action through. Without
        accounts: whoever holds the operator token the launcher printed - never
        given to agents.
        """
        current_user = self.current_user

        async def reviewer(
            user: User = Depends(current_user),
            review_token: Optional[str] = Header(default=None, alias="X-Grid-Action-Review-Token"),
        ) -> User:
            if self.auth is not None:
                if not user.is_admin:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
            else:
                self._require_action_review_token(review_token)
            return user

        return reviewer

    def _reviewed_spaces(self, own: UserSpace) -> list[UserSpace]:
        """The spaces whose pending reviews a reviewer sees: every loaded one.

        A review lives in its space's policy gate, in memory; a space is only
        unloaded when idle, and a review expires on its own anyway.
        """
        return [own, *(space for space in self.spaces.live() if space is not own)]

    def _usernames(self) -> dict[str, str]:
        if self.auth is None:
            return {}
        return {account.user.id: account.user.username for account in self.auth.accounts.accounts()}

    def _settings_payload(self, space: UserSpace) -> dict[str, Any]:
        raw = self.deployment.config_dict()
        cfg = space.config.config
        return {
            "config": raw,
            "raw_yaml": self.deployment.config_yaml(),
            "meta": {
                "default_agent": cfg.settings.default_agent,
                "config_path": str(self.deployment.config_path),
                "workspace_path": str(space.workspace_path),
                "persist_path": str(space.conversations_path),
                "isolation_enabled": bool(getattr(cfg.isolation, "enabled", False)),
                "container_id": space.container_id,
                "agent_options": agent_options(space.registry),
                "systems": system_options(space.registry),
                "model_keys": sorted((raw.get("models") or {}).keys()),
                "tool_keys": sorted((raw.get("tools") or {}).keys()),
                "prompt_keys": sorted((raw.get("prompt_templates") or {}).keys()),
            },
        }

    async def _configs_changed(self, user: User) -> dict[str, Any]:
        """After a config edit: retire what is free, answer from a fresh space."""
        self.spaces.invalidate()
        await self.spaces.sweep()
        async with self.spaces.use(user.id) as space:
            space.schedule_warmup()
            return self._settings_payload(space)

    def _register_pages(self) -> None:
        """The chat page, and with accounts the sign-in page in front of it."""
        app, auth = self.app, self.auth

        def page(path: Path) -> HTMLResponse:
            if not path.exists():
                return HTMLResponse(f"<h1>web_chat/{path.name} not found</h1>", status_code=503)
            return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

        @app.get("/", response_class=HTMLResponse)
        async def index(request: Request) -> Any:
            if auth is not None and auth.user_of(request) is None:
                return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
            return page(INDEX_HTML)

        if auth is None:
            return

        @app.get("/login", response_class=HTMLResponse)
        async def login(request: Request) -> Any:
            if auth.user_of(request) is not None:
                return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
            return page(LOGIN_HTML)

    def _register_routes(self, api: APIRouter) -> None:
        current_user, current_space = self.current_user, self.current_space
        admin = admins_only(current_user)

        @api.get("/api/chat/bootstrap")
        async def bootstrap(
            space: UserSpace = Depends(current_space), user: User = Depends(current_user)
        ) -> JSONResponse:
            registry = space.registry
            personal = space.personal_agents
            return JSONResponse(
                {
                    "user": {"id": user.id, "username": user.username, "role": user.role},
                    "accounts": self.auth is not None,
                    "personal_agents": personal is not None and personal.enabled,
                    "voice": self.voice is not None and self.deployment.voice_enabled(),
                    "systems": system_options(
                        registry,
                        frozenset(personal.keys()) if personal else frozenset(),
                        reveal_paths=user.is_admin,
                    ),
                    "default_system": registry.default_key(),
                    "routing_enabled": registry.can_route,
                    "multi_system": registry.has_catalog,
                    "current_context_id": space.context_manager().get_current_context_id(),
                    # Where the workspace lives on the server: for admins only.
                    "workspace_path": str(space.workspace_path) if user.is_admin else "",
                    "isolation_enabled": bool(space.container_id),
                }
            )

        @api.post("/api/chat/prepare-agent")
        async def prepare_agent(body: PrepareAgentRequest, space: UserSpace = Depends(current_space)) -> JSONResponse:
            try:
                await space.warm_agent(body.agent_key, body.system_key)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse({"ok": True, "agent_key": body.agent_key, "system_key": body.system_key})

        reviewer = self._reviewer()

        @api.get("/api/action-policy/reviews", dependencies=[Depends(reviewer)])
        async def pending_action_reviews(space: UserSpace = Depends(current_space)) -> JSONResponse:
            names = self._usernames()
            reviews = [
                {**review, "user_id": owner.user_id, "username": names.get(owner.user_id, owner.user_id)}
                for owner in self._reviewed_spaces(space)
                for review in owner.pending_action_reviews()
            ]
            return JSONResponse(sorted(reviews, key=lambda review: review["created_at"]))

        @api.post("/api/action-policy/reviews/{approval_id}", dependencies=[Depends(reviewer)])
        async def resolve_action_review(
            approval_id: str, body: ActionReviewRequest, space: UserSpace = Depends(current_space)
        ) -> JSONResponse:
            approve = body.decision == "approve"
            if not any(owner.resolve_action_review(approval_id, approve=approve) for owner in self._reviewed_spaces(space)):
                raise HTTPException(status_code=404, detail="Review not found or expired")
            return JSONResponse({"ok": True, "approval_id": approval_id, "decision": body.decision})

        @api.get("/api/chat/conversations")
        async def list_conversations(space: UserSpace = Depends(current_space)) -> JSONResponse:
            manager = space.context_manager()
            views = {view["id"]: view for view in manager.conversation_views()}
            items: list[dict[str, Any]] = []
            for view in views.values():
                metadata = view["metadata"]
                if metadata.get("branch_root") in views:
                    continue  # a branch shows under its conversation's row
                if not view["messages"] and not metadata.get("created_by_web"):
                    continue
                # One row per conversation; it opens the branch last worked on.
                family = [views[key] for key in manager.family(view["id"]) if key in views]
                shown = views.get(manager.open_branch(view["id"]), view)
                # The pinned selection is what the user chose; the routed pair
                # is who actually answered last - the rail shows the latter.
                items.append(
                    {
                        "id": view["id"],
                        "open_id": shown["id"],
                        "branches": len(family),
                        "title": conversation_title(view),
                        "updated_at": max((member["updated_at"] or "") for member in family),
                        "system_key": metadata.get("system_key"),
                        "agent_key": metadata.get("agent_key"),
                        "routed_system": metadata.get("routed_system"),
                        "routed_agent": metadata.get("routed_agent"),
                        "message_count": sum(
                            1 for msg in shown["messages"] if not is_tool_result(msg)
                        ),
                        "active": any(space.turns.is_running(member["id"]) for member in family),
                    }
                )
            items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
            return JSONResponse(items)

        @api.post("/api/chat/conversations")
        async def create_conversation(
            body: ConversationCreateRequest | None = None, space: UserSpace = Depends(current_space)
        ) -> JSONResponse:
            context_id = space.context_manager().start_new_context()
            selection = {
                "system_key": body.system_key if body else None,
                "agent_key": body.agent_key if body else None,
            }
            space.update_conversation_metadata(context_id, created_by_web=True, title=UNTITLED, **selection)
            return JSONResponse({"id": context_id, **selection})

        @api.patch("/api/chat/conversations/{context_id}")
        async def rename_conversation(
            context_id: str, body: ConversationRenameRequest, space: UserSpace = Depends(current_space)
        ) -> JSONResponse:
            title = " ".join(body.title.split())
            if not title:
                raise HTTPException(status_code=400, detail="Title must not be empty")
            # A title the user chose outranks the one derived from messages.
            if not space.context_manager().update_context_metadata(
                context_id, {"title": title, "title_locked": True}, create=False
            ):
                raise HTTPException(status_code=404, detail="Conversation not found")
            return JSONResponse({"id": context_id, "title": title})

        @api.delete("/api/chat/conversations/{context_id}")
        async def delete_conversation(context_id: str, space: UserSpace = Depends(current_space)) -> JSONResponse:
            manager = space.context_manager()
            family = manager.family(context_id)
            if any(space.turns.is_running(member) for member in family):
                raise HTTPException(status_code=409, detail="Stop the running turn before deleting this chat")
            # A conversation goes with all its branches.
            deleted = manager.delete_family(context_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Conversation not found")
            return JSONResponse({"id": context_id, "deleted": True, "contexts": deleted})

        @api.post("/api/chat/conversations/{context_id}/branches")
        async def create_branch(
            context_id: str, body: BranchRequest, space: UserSpace = Depends(current_space)
        ) -> JSONResponse:
            """Fork the conversation before a user message, for an edited version of it.

            The client then sends the new text into the branch with ``edit_of``.
            A turn running in the conversation must be stopped first.
            """
            manager = space.context_manager()
            if manager.conversation_view(context_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if space.turns.is_running(context_id):
                raise HTTPException(status_code=409, detail="Stop the running turn before editing a message")
            metadata = manager.get_context_metadata(context_id)
            system = metadata.get("routed_system") or space.registry.default_key()
            factory = space.registry.factory(system)
            try:
                branch_id = await factory.fork_conversation(context_id, body.message_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="Message not found") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            slot = manager.get_context_metadata(branch_id).get("branch_slot")
            return JSONResponse({"id": branch_id, "edit_of": slot, "root": manager.branch_root(branch_id)})

        @api.post("/api/chat/conversations/{context_id}/compact")
        async def compact_conversation(context_id: str, space: UserSpace = Depends(current_space)) -> JSONResponse:
            """Summarize the agent's session in this conversation now (core.context_budget)."""
            manager = space.context_manager()
            if manager.conversation_view(context_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if space.turns.is_running(context_id):
                raise HTTPException(status_code=409, detail="Stop the running turn before compacting")
            metadata = manager.get_context_metadata(context_id)
            agent = metadata.get("routed_agent")
            if not agent:
                raise HTTPException(status_code=400, detail="No agent has worked in this chat yet")
            system = metadata.get("routed_system") or space.registry.default_key()
            outcome = await space.registry.factory(system).compact_session(agent, context_id, force=True)
            if outcome is None:
                raise HTTPException(status_code=422, detail="Nothing was compacted: the context is empty or the summary failed")
            return JSONResponse({"id": context_id, **outcome})

        @api.post("/api/chat/conversations/{context_id}/activate")
        async def activate_branch(context_id: str, space: UserSpace = Depends(current_space)) -> JSONResponse:
            """Make this branch the one its conversation opens with."""
            manager = space.context_manager()
            if manager.conversation_view(context_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            manager.set_active_branch(context_id)
            return JSONResponse({"id": context_id, "root": manager.branch_root(context_id)})

        @api.get("/api/chat/conversations/{context_id}")
        async def get_conversation(context_id: str, space: UserSpace = Depends(current_space)) -> JSONResponse:
            manager = space.context_manager()
            active = space.turns.running(context_id)
            if active is None:
                # A turn the record says is running died with an earlier
                # process: show it as interrupted, with Continue.
                manager.recover_abandoned_turn(context_id)
            view = manager.conversation_view(context_id)
            if view is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            shown = [msg for msg in view["messages"] if not is_tool_result(msg)]
            pending = manager.pending_interruption(context_id) is not None
            versions = manager.message_versions(context_id)
            messages = [
                serialize_message(
                    msg,
                    resumable=pending and index == len(shown) - 1,
                    versions=versions.get((msg.metadata or {}).get("message_id")),
                )
                for index, msg in enumerate(shown)
            ]
            active_turn = (
                {
                    "message": active[0].message,
                    "resumes": active[0].resumes,
                    "elapsed_ms": active[0].elapsed_ms,
                }
                if active is not None
                else None
            )
            return JSONResponse({
                "id": context_id,
                "root": manager.branch_root(context_id),
                "messages": messages,
                # Messages waiting for their turn (web_chat.delivery).
                "pending": MessageQueue(manager, context_id).public(),
                "metadata": view["metadata"],
                "active_turn": active_turn,
            })

        # The system configs are shared by every user: only admins see or edit them.
        @api.get("/api/settings", dependencies=[Depends(admin)])
        async def get_settings(space: UserSpace = Depends(current_space)) -> JSONResponse:
            return JSONResponse(self._settings_payload(space))

        # Saving takes no lease on the user's space: the edit retires spaces,
        # and one held by the request itself could not be rebuilt.
        @api.put("/api/settings/structured")
        async def save_structured_settings(
            body: SettingsStructuredUpdateRequest, user: User = Depends(admin)
        ) -> JSONResponse:
            try:
                self.deployment.save_structured_config(body.config)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(await self._configs_changed(user))

        @api.put("/api/settings/yaml")
        async def save_yaml_settings(
            body: SettingsYamlUpdateRequest, user: User = Depends(admin)
        ) -> JSONResponse:
            try:
                self.deployment.save_yaml_config(body.yaml_content)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(await self._configs_changed(user))

        @api.websocket("/api/chat/ws/{context_id}")
        async def chat_ws(websocket: WebSocket, context_id: str, space: UserSpace = Depends(current_space)) -> None:
            from web_chat.session import chat_session

            await chat_session(space, websocket, context_id)


def create_app(
    deployment: Deployment,
    spaces: SpacePool,
    **options: Any,
) -> FastAPI:
    return WebChatServer(deployment, spaces, **options).app
