from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.context import safe_lock
from web_chat.runtime import WebChatRuntime
from web_chat.schemas import (
    ConversationCreateRequest,
    PrepareAgentRequest,
    SettingsStructuredUpdateRequest,
    SettingsYamlUpdateRequest,
)

logger = logging.getLogger("grid.web_chat.server")
ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"


def _truncate(value: Any, limit: int = 2000) -> str:
    if isinstance(value, dict):
        if not value:
            return ""
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n..."


class WebStreamObserver:
    def __init__(self, queue: asyncio.Queue, agent_label: str) -> None:
        self.queue = queue
        self.agent_label = agent_label
        self.started_at = time.time()
        self.tokens_seen = False

    def _push(self, payload: dict[str, Any]) -> None:
        payload.setdefault("ts", round((time.time() - self.started_at) * 1000))
        self.queue.put_nowait(payload)

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        from agents import RawResponsesStreamEvent, RunItemStreamEvent

        try:
            if isinstance(event, RawResponsesStreamEvent):
                content: Optional[str] = None
                if getattr(event, "content", None):
                    content = event.content
                elif getattr(event, "delta", None):
                    content = event.delta
                elif getattr(event, "text", None):
                    content = event.text
                elif getattr(event, "data", None):
                    data = event.data
                    if getattr(data, "delta", None):
                        content = data.delta
                    elif getattr(data, "content", None):
                        content = data.content
                    elif getattr(data, "text", None):
                        content = data.text
                    elif isinstance(data, dict):
                        content = data.get("content") or data.get("delta") or data.get("text")
                if content:
                    self.tokens_seen = True
                    self._push({"type": "token", "content": content})
                    return content
                return None

            if not isinstance(event, RunItemStreamEvent):
                return None

            name = getattr(event, "name", "")
            item = getattr(event, "item", None)
            raw_item = getattr(item, "raw_item", None) if item is not None else None

            if name == "tool_called" and raw_item is not None:
                tool_name = getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "tool"
                server_label = getattr(raw_item, "server_label", None)
                arguments = getattr(raw_item, "arguments", None)
                title = f"{server_label}.{tool_name}" if server_label else tool_name
                self._push(
                    {
                        "type": "trace",
                        "kind": "tool_call",
                        "title": f"Running tool {title}",
                        "subtitle": self.agent_label,
                        "details": _truncate(arguments or {}),
                        "status": "running",
                    }
                )
            elif name == "tool_output" and item is not None:
                tool_name = getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "tool"
                server_label = getattr(raw_item, "server_label", None)
                title = f"{server_label}.{tool_name}" if server_label else tool_name
                output = getattr(item, "output", "")
                self._push(
                    {
                        "type": "trace",
                        "kind": "tool_output",
                        "title": f"Tool result {title}",
                        "subtitle": self.agent_label,
                        "details": _truncate(output),
                        "status": "done",
                    }
                )
            elif name == "handoff_requested" and raw_item is not None:
                target = getattr(raw_item, "name", None) or "agent"
                self._push(
                    {
                        "type": "trace",
                        "kind": "handoff_requested",
                        "title": f"Handoff to {target}",
                        "subtitle": self.agent_label,
                        "details": "Agent is delegating the next part of the task.",
                        "status": "running",
                    }
                )
            elif name == "handoff_occured" and item is not None:
                src_agent = getattr(item, "source_agent", None)
                dst_agent = getattr(item, "target_agent", None)
                src_name = getattr(src_agent, "name", None) or agent_key or self.agent_label
                dst_name = getattr(dst_agent, "name", None) or "agent"
                self._push(
                    {
                        "type": "trace",
                        "kind": "handoff",
                        "title": f"{src_name} → {dst_name}",
                        "subtitle": "Handoff",
                        "details": "Sub-agent has taken control of this branch.",
                        "status": "done",
                    }
                )
            elif name == "mcp_list_tools" and raw_item is not None:
                server_label = getattr(raw_item, "server_label", None) or "mcp"
                tools = getattr(raw_item, "tools", None) or []
                self._push(
                    {
                        "type": "trace",
                        "kind": "mcp",
                        "subtitle": self.agent_label,
                        "details": _truncate([getattr(tool, "name", str(tool)) for tool in tools], 600),
                        "status": "done",
                    }
                )
        except Exception:
            logger.exception("Failed to process web stream event")
        return None


class WebChatServer:
    def __init__(self, runtime: WebChatRuntime) -> None:
        self.runtime = runtime
        self.app = FastAPI(title="Grid Web Chat", docs_url=None, redoc_url=None)
        self._mount_static()
        self._register_routes()
        self._register_lifecycle()

    def _mount_static(self) -> None:
        self.app.mount("/static", StaticFiles(directory=str(ROOT)), name="static")

    def _register_lifecycle(self) -> None:
        @self.app.on_event("startup")
        async def _startup() -> None:
            asyncio.create_task(self.runtime.warm_default_agent())

    def _conversation_title(self, bucket: dict[str, Any]) -> str:
        metadata = bucket.get("metadata") or {}
        if metadata.get("title") and metadata.get("title") != "New chat":
            return metadata["title"]
        for msg in bucket.get("conversation", []):
            if getattr(msg, "role", None) == "user":
                text = msg.get_text_content() if hasattr(msg, "get_text_content") else str(getattr(msg, "content", ""))
                text = " ".join(text.split())
                return text[:42] + ("..." if len(text) > 42 else "")
        return "New chat"

    def _serialize_message(self, msg: Any) -> dict[str, Any]:
        if hasattr(msg, "get_text_content"):
            content = msg.get_text_content()
        else:
            content = str(getattr(msg, "content", ""))
        return {
            "role": getattr(msg, "role", "assistant"),
            "content": content,
            "timestamp": getattr(msg, "timestamp", None),
            "metadata": getattr(msg, "metadata", None),
        }

    def _agent_options(self) -> list[dict[str, Any]]:
        raw = self.runtime.config_dict()
        agents = raw.get("agents") or {}
        models = raw.get("models") or {}
        result: list[dict[str, Any]] = []
        for agent_key, agent_cfg in agents.items():
            model_key = agent_cfg.get("model")
            model_cfg = models.get(model_key) or {}
            result.append(
                {
                    "key": agent_key,
                    "name": agent_cfg.get("name") or agent_key,
                    "description": agent_cfg.get("description") or "",
                    "model_key": model_key,
                    "model_name": model_cfg.get("name") or model_key,
                    "model_description": model_cfg.get("description") or "",
                    "tool_count": len(agent_cfg.get("tools") or []),
                    "mcp_enabled": bool(agent_cfg.get("mcp_enabled")),
                }
            )
        return result

    def _structured_settings_payload(self) -> dict[str, Any]:
        raw = self.runtime.config_dict()
        cfg = self.runtime.config.config
        return {
            "config": raw,
            "raw_yaml": self.runtime.config_path.read_text(encoding="utf-8"),
            "meta": {
                "default_agent": cfg.settings.default_agent,
                "workspace_path": str(self.runtime.workspace_path),
                "persist_path": str(self.runtime.persist_path),
                "isolation_enabled": bool(getattr(cfg.isolation, "enabled", False)),
                "container_id": self.runtime.container_id,
                "agent_options": self._agent_options(),
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
            return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))

        @app.get("/api/chat/bootstrap")
        async def bootstrap() -> JSONResponse:
            current_context_id = self.runtime.context_manager().get_current_context_id()
            return JSONResponse(
                {
                    "default_agent": self.runtime.config.config.settings.default_agent,
                    "agents": self._agent_options(),
                    "current_context_id": current_context_id,
                    "workspace_path": str(self.runtime.workspace_path),
                    "isolation_enabled": bool(self.runtime.container_id),
                }
            )

        @app.post("/api/chat/prepare-agent")
        async def prepare_agent(body: PrepareAgentRequest) -> JSONResponse:
            try:
                await self.runtime.warm_agent(body.agent_key)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse({"ok": True, "agent_key": body.agent_key})

        @app.get("/api/chat/conversations")
        async def list_conversations() -> JSONResponse:
            items: list[dict[str, Any]] = []
            context_manager = self.runtime.context_manager()
            with safe_lock(context_manager._lock):
                for context_id, bucket in context_manager._contexts.items():
                    metadata = bucket.get("metadata") or {}
                    if not bucket.get("conversation") and not metadata.get("created_by_web"):
                        continue
                    items.append(
                        {
                            "id": context_id,
                            "title": self._conversation_title(bucket),
                            "updated_at": bucket.get("updated_at"),
                            "agent_key": metadata.get("agent_key") or self.runtime.config.config.settings.default_agent,
                            "agent_name": next((agent["name"] for agent in self._agent_options() if agent["key"] == metadata.get("agent_key")), None),
                            "message_count": len(bucket.get("conversation") or []),
                        }
                    )
            items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
            return JSONResponse(items)

        @app.post("/api/chat/conversations")
        async def create_conversation(body: ConversationCreateRequest | None = None) -> JSONResponse:
            context_manager = self.runtime.context_manager()
            context_id = context_manager.start_new_context()
            agent_key = (body.agent_key if body else None) or self.runtime.config.config.settings.default_agent
            self.runtime.update_conversation_metadata(
                context_id,
                created_by_web=True,
                agent_key=agent_key,
                title="New chat",
            )
            return JSONResponse({"id": context_id, "agent_key": agent_key})

        @app.get("/api/chat/conversations/{context_id}")
        async def get_conversation(context_id: str) -> JSONResponse:
            context_manager = self.runtime.context_manager()
            with safe_lock(context_manager._lock):
                bucket = context_manager._contexts.get(context_id)
                if not bucket:
                    raise HTTPException(status_code=404, detail="Conversation not found")
                messages = [self._serialize_message(msg) for msg in bucket.get("conversation") or []]
                metadata = dict(bucket.get("metadata") or {})
            return JSONResponse({"id": context_id, "messages": messages, "metadata": metadata})

        @app.get("/api/settings")
        async def get_settings() -> JSONResponse:
            return JSONResponse(self._structured_settings_payload())

        @app.put("/api/settings/structured")
        async def save_structured_settings(body: SettingsStructuredUpdateRequest) -> JSONResponse:
            try:
                self.runtime.save_structured_config(body.config)
                asyncio.create_task(self.runtime.warm_default_agent())
                return JSONResponse(self._structured_settings_payload())
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.put("/api/settings/yaml")
        async def save_yaml_settings(body: SettingsYamlUpdateRequest) -> JSONResponse:
            try:
                self.runtime.save_yaml_config(body.yaml_content)
                asyncio.create_task(self.runtime.warm_default_agent())
                return JSONResponse(self._structured_settings_payload())
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.websocket("/api/chat/ws/{context_id}")
        async def chat_ws(websocket: WebSocket, context_id: str) -> None:
            await websocket.accept()
            context_manager = self.runtime.context_manager()
            run_task: Optional[asyncio.Task] = None
            try:
                with safe_lock(context_manager._lock):
                    if context_id not in context_manager._contexts:
                        context_manager._create_context(context_id)

                while True:
                    raw = await websocket.receive_text()
                    payload = json.loads(raw)
                    if payload.get("action") == "stop":
                        if run_task and not run_task.done():
                            run_task.cancel()
                        await websocket.send_json({"type": "done", "stopped": True})
                        continue

                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue

                    agent_key = payload.get("agent_key") or self.runtime.config.config.settings.default_agent
                    await websocket.send_json(
                        {
                            "type": "trace",
                            "kind": "prepare",
                            "title": "Environment setup",
                            "subtitle": agent_key,
                            "details": f"Workspace: {self.runtime.workspace_path}",
                            "status": "running",
                        }
                    )
                    await self.runtime.warm_agent(agent_key)
                    self.runtime.update_conversation_metadata(
                        context_id,
                        created_by_web=True,
                        agent_key=agent_key,
                        title=(" ".join(message.split())[:42] + ("..." if len(" ".join(message.split())) > 42 else "")) or "New chat",
                    )
                    observer_queue: asyncio.Queue = asyncio.Queue()
                    agent_label = next((agent["name"] for agent in self._agent_options() if agent["key"] == agent_key), agent_key)
                    observer = WebStreamObserver(observer_queue, agent_label)
                    token_buffer: list[str] = []

                    async def run_agent() -> None:
                        try:
                            await observer_queue.put(
                                {
                                    "type": "trace",
                                    "kind": "agent_start",
                                    "title": f"Agent start {agent_label}",
                                    "subtitle": agent_key,
                                    "details": "Request passed to Grid runtime.",
                                    "status": "running",
                                }
                            )
                            result = await self.runtime.factory.run_agent(
                                agent_key=agent_key,
                                message=message,
                                context_id=context_id,
                                stream=True,
                                user_id=self.runtime.user_id,
                                stream_observer=observer,
                            )
                            if result:
                                await observer_queue.put({"type": "final_output", "content": str(result)})
                            await observer_queue.put(
                                {
                                    "type": "trace",
                                    "kind": "agent_end",
                                    "title": f"Completed: {agent_label}",
                                    "subtitle": agent_key,
                                    "details": "Response ready and saved in conversation history.",
                                    "status": "done",
                                }
                            )
                            await observer_queue.put({"type": "done"})
                        except asyncio.CancelledError:
                            await observer_queue.put(
                                {
                                    "type": "trace",
                                    "kind": "cancelled",
                                    "title": "Generation stopped",
                                    "subtitle": agent_key,
                                    "details": "Stream was stopped by the user.",
                                    "status": "warning",
                                }
                            )
                            await observer_queue.put({"type": "done", "stopped": True})
                            raise
                        except Exception as exc:
                            logger.exception("Web chat run failed")
                            await observer_queue.put({"type": "error", "content": str(exc)})

                    run_task = asyncio.create_task(run_agent())

                    trace_events = []
                    token_buffer = []
                    while True:
                        event = await observer_queue.get()
                        if event["type"] == "trace":
                            if event.get("kind") == "tool_call" and token_buffer:
                                text = "".join(token_buffer).strip()
                                if text:
                                    thinking_event = {
                                        "type": "trace",
                                        "kind": "thinking",
                                        "title": "Thinking",
                                        "subtitle": agent_key,
                                        "details": text,
                                        "status": "done",
                                        "ts": event.get("ts")
                                    }
                                    trace_events.append(thinking_event)
                                    await websocket.send_json(thinking_event)
                                token_buffer = []
                            trace_events.append(event)
                        if event["type"] == "token":
                            token_buffer.append(event["content"])
                        await websocket.send_json(event)
                        if event["type"] in {"done", "error"}:
                            break
                            
                    with safe_lock(context_manager._lock):
                        bucket = context_manager._contexts.get(context_id)
                        if bucket and bucket.get("conversation"):
                            last_msg = bucket["conversation"][-1]
                            if getattr(last_msg, "role", None) == "assistant":
                                metadata = last_msg.metadata or {}
                                metadata["trace_events"] = trace_events
                                last_msg.metadata = metadata
                                if context_manager.persist_path:
                                    context_manager._save_to_file()
            except WebSocketDisconnect:
                if run_task and not run_task.done():
                    run_task.cancel()
            except Exception:
                logger.exception("Websocket session failed")
                if run_task and not run_task.done():
                    run_task.cancel()
                try:
                    await websocket.send_json({"type": "error", "content": "Websocket session failed"})
                except Exception:
                    pass


def create_app(runtime: Optional[WebChatRuntime] = None) -> FastAPI:
    server = WebChatServer(runtime or WebChatRuntime())
    return server.app
