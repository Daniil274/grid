from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.interruption import CONTINUATION_TYPE, interruption_of
from core.tool_check import summarize
from schemas.schemas import ImageContent
from web_chat.runtime import WebChatRuntime
from web_chat.schemas import (
    ActionReviewRequest,
    BranchRequest,
    ConversationCreateRequest,
    ConversationRenameRequest,
    PrepareAgentRequest,
    SettingsStructuredUpdateRequest,
    SettingsYamlUpdateRequest,
)
from web_chat.delivery import MessageQueue
from web_chat.trace import is_tool_result, normalize_steps

logger = logging.getLogger("grid.web_chat.server")
ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"


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

class WebChatServer:
    def __init__(self, runtime: WebChatRuntime) -> None:
        self.runtime = runtime
        self._active_chat_contexts: set[str] = set()
        # Running turns by conversation; they outlive the sockets watching them.
        self._chat_turns: dict[str, Any] = {}
        self.app = FastAPI(title="Grid Web Chat", docs_url=None, redoc_url=None)
        self._mount_static()
        self._register_routes()
        from web_chat.voice import register_voice_routes

        register_voice_routes(self.app, runtime)
        self._register_lifecycle()

    def _mount_static(self) -> None:
        self.app.mount("/static", RevalidatedStaticFiles(directory=str(ROOT)), name="static")

    def _register_lifecycle(self) -> None:
        @self.app.on_event("startup")
        async def _startup() -> None:
            self.runtime.schedule_warmup()

        @self.app.on_event("shutdown")
        async def _shutdown() -> None:
            if hasattr(self.runtime, "close"):
                await self.runtime.close()

    def turn_is_running(self, context_id: str) -> bool:
        active = self._chat_turns.get(context_id)
        return active is not None and not active[1].done()

    def _conversation_title(self, view: dict[str, Any]) -> str:
        metadata = view["metadata"]
        if metadata.get("title") and metadata.get("title") != "New chat":
            return metadata["title"]
        for msg in view["messages"]:
            if getattr(msg, "role", None) == "user":
                text = msg.get_text_content() if hasattr(msg, "get_text_content") else str(getattr(msg, "content", ""))
                text = " ".join(text.split())
                return text[:42] + ("..." if len(text) > 42 else "")
        return "New chat"

    def _serialize_message(
        self, msg: Any, *, resumable: bool = False, versions: Optional[dict] = None
    ) -> dict[str, Any]:
        """A stored message as the chat renders it.

        ``interruption`` marks a turn that stopped early; ``resumable`` is set
        only on the one Continue can still resume. ``kind`` "continuation" is
        the user entry of a Continue, shown as a marker, not a bubble.
        ``images`` are the data URLs of the images the message carries.
        ``id`` is the message's stable id; ``versions`` lists the branches with
        other versions of an edited user message (ContextManager.message_versions).
        """
        if hasattr(msg, "get_text_content"):
            content = msg.get_text_content()
        else:
            content = str(getattr(msg, "content", ""))
        images = [
            part.image_url.url
            for part in (msg.get_images() if hasattr(msg, "get_images") else [])
            if isinstance(part, ImageContent)
        ]
        if images and content == "[multimodal content]":
            content = ""  # an image without words
        metadata = getattr(msg, "metadata", None) or {}
        # Older builds stored a flat "trace_events" list; normalize_steps keeps
        # those conversations renderable by the current timeline component.
        trace = metadata.get("trace") or normalize_steps(metadata.get("trace_events"))
        record = interruption_of(msg)
        return {
            "id": metadata.get("message_id"),
            "versions": versions,
            "role": getattr(msg, "role", "assistant"),
            "content": content,
            "timestamp": getattr(msg, "timestamp", None),
            "trace": trace,
            "kind": "continuation" if metadata.get("type") == CONTINUATION_TYPE else None,
            "images": images,
            "interruption": (
                {**record.to_dict(), "resumable": resumable} if record is not None else None
            ),
        }

    def _agent_options(self, system_key: Optional[str] = None) -> list[dict[str, Any]]:
        """Agents of one system, as the picker shows them."""
        registry = self.runtime.registry
        system = system_key or registry.default_key()
        models = registry.config(system).config.models or {}
        options: list[dict[str, Any]] = []
        for agent_key, agent in registry.agents(system).items():
            model = models.get(agent.primary_model)
            issues = self._issue_payload(lambda: registry.agent_issues(system, agent_key))
            options.append(
                {
                    "key": agent_key,
                    "name": agent.name or agent_key,
                    "description": agent.description or "",
                    "model_key": agent.primary_model,
                    "model_keys": agent.model_keys(),
                    "model_name": getattr(model, "name", None) or agent.primary_model,
                    "model_description": getattr(model, "description", "") or "",
                    "tool_count": len(agent.tools or []),
                    "mcp_enabled": bool(getattr(agent, "mcp_enabled", False)),
                    "routable": bool(getattr(agent, "routable", True)),
                    # Tools that will fail and why; shown before the agent is used.
                    "issues": issues,
                }
            )
        return options

    @staticmethod
    def _issue_payload(compute: Any) -> dict[str, Any]:
        """Issues as JSON: each one, plus one line per distinct problem for display.

        A check that itself breaks is reported, never fatal.
        """
        try:
            issues = compute()
        except Exception as exc:
            logger.warning("Tool check failed: %s", exc)
            return {"items": [], "summary": [f"tool check failed: {exc}"]}
        return {"items": [issue.to_dict() for issue in issues], "summary": summarize(issues)}

    def _system_options(self) -> list[dict[str, Any]]:
        """Every selectable system with its agents - the whole picker payload."""
        options: list[dict[str, Any]] = []
        for system in self.runtime.registry.systems():
            try:
                agents = self._agent_options(system.key)
                default_agent = self.runtime.registry.config(system.key).get_default_agent()
                error = ""
                issues = self._issue_payload(
                    lambda: [issue for issue in self.runtime.registry.issues(system.key) if issue.agent is None]
                )
            except Exception as exc:  # a broken system stays visible and labelled
                logger.warning("System '%s' could not be loaded: %s", system.key, exc)
                agents, default_agent, error, issues = [], None, str(exc), {"items": [], "summary": []}
            options.append(
                {
                    "key": system.key,
                    "name": system.name,
                    "description": system.description,
                    "config_path": str(system.config_path),
                    "default_agent": default_agent,
                    "agents": agents,
                    "error": error,
                    "issues": issues,
                }
            )
        return options

    def agent_label(self, system_key: str, agent_key: str) -> str:
        """Display name of an agent, falling back to its key."""
        try:
            agent = self.runtime.registry.agents(system_key).get(agent_key)
        except Exception:
            return agent_key
        return getattr(agent, "name", None) or agent_key

    def selection_is_valid(self, system_key: Any, agent_key: Any) -> bool:
        """`None` means `auto` on that level and is always valid."""
        registry = self.runtime.registry
        if system_key is not None and system_key not in registry.keys():
            return False
        if agent_key is None:
            return True
        return registry.has_agent(system_key or registry.default_key(), agent_key)

    def _require_action_review_token(self, supplied: Optional[str]) -> None:
        expected = getattr(self.runtime, "action_review_token", None)
        if not expected:
            raise HTTPException(status_code=503, detail="Action review API is disabled")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="Invalid action review token")

    def _structured_settings_payload(self) -> dict[str, Any]:
        raw = self.runtime.config_dict()
        cfg = self.runtime.config.config
        return {
            "config": raw,
            "raw_yaml": self.runtime.config_path.read_text(encoding="utf-8"),
            "meta": {
                "default_agent": cfg.settings.default_agent,
                "config_path": str(self.runtime.config_path),
                "workspace_path": str(self.runtime.workspace_path),
                "persist_path": str(self.runtime.persist_path),
                "isolation_enabled": bool(getattr(cfg.isolation, "enabled", False)),
                "container_id": self.runtime.container_id,
                "agent_options": self._agent_options(),
                "systems": self._system_options(),
                "model_keys": sorted((raw.get("models") or {}).keys()),
                "tool_keys": sorted((raw.get("tools") or {}).keys()),
                "prompt_keys": sorted((raw.get("prompt_templates") or {}).keys()),
            },
        }

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            if not INDEX_HTML.exists():
                return HTMLResponse("<h1>web_chat/index.html not found</h1>", status_code=503)
            return HTMLResponse(
                INDEX_HTML.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/api/chat/bootstrap")
        async def bootstrap() -> JSONResponse:
            registry = self.runtime.registry
            return JSONResponse(
                {
                    "systems": self._system_options(),
                    "default_system": registry.default_key(),
                    "routing_enabled": registry.can_route,
                    "multi_system": registry.has_catalog,
                    "current_context_id": self.runtime.context_manager().get_current_context_id(),
                    "workspace_path": str(self.runtime.workspace_path),
                    "isolation_enabled": bool(self.runtime.container_id),
                }
            )

        @app.post("/api/chat/prepare-agent")
        async def prepare_agent(body: PrepareAgentRequest) -> JSONResponse:
            try:
                await self.runtime.warm_agent(body.agent_key, body.system_key)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse({"ok": True, "agent_key": body.agent_key, "system_key": body.system_key})

        @app.get("/api/action-policy/reviews")
        async def pending_action_reviews(
            review_token: Optional[str] = Header(
                default=None, alias="X-Grid-Action-Review-Token"
            ),
        ) -> JSONResponse:
            self._require_action_review_token(review_token)
            return JSONResponse(self.runtime.pending_action_reviews())

        @app.post("/api/action-policy/reviews/{approval_id}")
        async def resolve_action_review(
            approval_id: str,
            body: ActionReviewRequest,
            review_token: Optional[str] = Header(
                default=None, alias="X-Grid-Action-Review-Token"
            ),
        ) -> JSONResponse:
            self._require_action_review_token(review_token)
            resolved = self.runtime.resolve_action_review(
                approval_id, approve=body.decision == "approve"
            )
            if not resolved:
                raise HTTPException(
                    status_code=404, detail="Review not found or expired"
                )
            return JSONResponse(
                {"ok": True, "approval_id": approval_id, "decision": body.decision}
            )

        @app.get("/api/chat/conversations")
        async def list_conversations() -> JSONResponse:
            manager = self.runtime.context_manager()
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
                        "title": self._conversation_title(view),
                        "updated_at": max((member["updated_at"] or "") for member in family),
                        "system_key": metadata.get("system_key"),
                        "agent_key": metadata.get("agent_key"),
                        "routed_system": metadata.get("routed_system"),
                        "routed_agent": metadata.get("routed_agent"),
                        "message_count": sum(
                            1 for msg in shown["messages"] if not is_tool_result(msg)
                        ),
                        "active": any(self.turn_is_running(member["id"]) for member in family),
                    }
                )
            items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
            return JSONResponse(items)

        @app.post("/api/chat/conversations")
        async def create_conversation(body: ConversationCreateRequest | None = None) -> JSONResponse:
            context_manager = self.runtime.context_manager()
            context_id = context_manager.start_new_context()
            selection = {
                "system_key": body.system_key if body else None,
                "agent_key": body.agent_key if body else None,
            }
            self.runtime.update_conversation_metadata(
                context_id, created_by_web=True, title="New chat", **selection
            )
            return JSONResponse({"id": context_id, **selection})

        @app.patch("/api/chat/conversations/{context_id}")
        async def rename_conversation(context_id: str, body: ConversationRenameRequest) -> JSONResponse:
            title = " ".join(body.title.split())
            if not title:
                raise HTTPException(status_code=400, detail="Title must not be empty")
            # A title the user chose outranks the one derived from messages.
            if not self.runtime.context_manager().update_context_metadata(
                context_id, {"title": title, "title_locked": True}, create=False
            ):
                raise HTTPException(status_code=404, detail="Conversation not found")
            return JSONResponse({"id": context_id, "title": title})

        @app.delete("/api/chat/conversations/{context_id}")
        async def delete_conversation(context_id: str) -> JSONResponse:
            manager = self.runtime.context_manager()
            family = manager.family(context_id)
            if any(self.turn_is_running(member) for member in family):
                raise HTTPException(status_code=409, detail="Stop the running turn before deleting this chat")
            # A conversation goes with all its branches.
            deleted = manager.delete_family(context_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Conversation not found")
            return JSONResponse({"id": context_id, "deleted": True, "contexts": deleted})

        @app.post("/api/chat/conversations/{context_id}/branches")
        async def create_branch(context_id: str, body: BranchRequest) -> JSONResponse:
            """Fork the conversation before a user message, for an edited version of it.

            The client then sends the new text into the branch with ``edit_of``.
            A turn running in the conversation must be stopped first.
            """
            manager = self.runtime.context_manager()
            if manager.conversation_view(context_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if self.turn_is_running(context_id):
                raise HTTPException(status_code=409, detail="Stop the running turn before editing a message")
            metadata = manager.get_context_metadata(context_id)
            system = metadata.get("routed_system") or self.runtime.registry.default_key()
            factory = self.runtime.registry.factory(system)
            try:
                branch_id = await factory.fork_conversation(context_id, body.message_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="Message not found") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            slot = manager.get_context_metadata(branch_id).get("branch_slot")
            return JSONResponse({"id": branch_id, "edit_of": slot, "root": manager.branch_root(branch_id)})

        @app.post("/api/chat/conversations/{context_id}/compact")
        async def compact_conversation(context_id: str) -> JSONResponse:
            """Summarize the agent's session in this conversation now (core.context_budget)."""
            manager = self.runtime.context_manager()
            if manager.conversation_view(context_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if self.turn_is_running(context_id):
                raise HTTPException(status_code=409, detail="Stop the running turn before compacting")
            metadata = manager.get_context_metadata(context_id)
            agent = metadata.get("routed_agent")
            if not agent:
                raise HTTPException(status_code=400, detail="No agent has worked in this chat yet")
            system = metadata.get("routed_system") or self.runtime.registry.default_key()
            outcome = await self.runtime.registry.factory(system).compact_session(agent, context_id, force=True)
            if outcome is None:
                raise HTTPException(status_code=422, detail="Nothing was compacted: the context is empty or the summary failed")
            return JSONResponse({"id": context_id, **outcome})

        @app.post("/api/chat/conversations/{context_id}/activate")
        async def activate_branch(context_id: str) -> JSONResponse:
            """Make this branch the one its conversation opens with."""
            manager = self.runtime.context_manager()
            if manager.conversation_view(context_id) is None:
                raise HTTPException(status_code=404, detail="Conversation not found")
            manager.set_active_branch(context_id)
            return JSONResponse({"id": context_id, "root": manager.branch_root(context_id)})

        @app.get("/api/chat/conversations/{context_id}")
        async def get_conversation(context_id: str) -> JSONResponse:
            manager = self.runtime.context_manager()
            if not self.turn_is_running(context_id):
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
                self._serialize_message(
                    msg,
                    resumable=pending and index == len(shown) - 1,
                    versions=versions.get((msg.metadata or {}).get("message_id")),
                )
                for index, msg in enumerate(shown)
            ]
            metadata = view["metadata"]
            active = self._chat_turns.get(context_id)
            active_turn = (
                {
                    "message": active[0].message,
                    "resumes": active[0].resumes,
                    "elapsed_ms": active[0].elapsed_ms,
                }
                if active is not None and not active[1].done()
                else None
            )
            return JSONResponse({
                "id": context_id,
                "root": manager.branch_root(context_id),
                "messages": messages,
                # Messages waiting for their turn (web_chat.delivery).
                "pending": MessageQueue(manager, context_id).public(),
                "metadata": metadata,
                "active_turn": active_turn,
            })

        @app.get("/api/settings")
        async def get_settings() -> JSONResponse:
            return JSONResponse(self._structured_settings_payload())

        @app.put("/api/settings/structured")
        async def save_structured_settings(body: SettingsStructuredUpdateRequest) -> JSONResponse:
            try:
                self.runtime.save_structured_config(body.config)
                self.runtime.schedule_warmup()
                return JSONResponse(self._structured_settings_payload())
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.put("/api/settings/yaml")
        async def save_yaml_settings(body: SettingsYamlUpdateRequest) -> JSONResponse:
            try:
                self.runtime.save_yaml_config(body.yaml_content)
                self.runtime.schedule_warmup()
                return JSONResponse(self._structured_settings_payload())
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.websocket("/api/chat/ws/{context_id}")
        async def chat_ws(websocket: WebSocket, context_id: str) -> None:
            from web_chat.session import chat_session

            await chat_session(self, websocket, context_id)



def create_app(runtime: Optional[WebChatRuntime] = None) -> FastAPI:
    server = WebChatServer(runtime or WebChatRuntime())
    return server.app
