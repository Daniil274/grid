"""What the chat API returns: stored messages, systems and agents as JSON.

Pure functions of a space's records - no request, no server state - so every
route and the voice control describe things the same way.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from core.compact import COMPACTED_TYPE
from core.interruption import CONTINUATION_TYPE, interruption_of
from core.tool_check import summarize
from schemas.schemas import ImageContent
from web_chat.systems import SystemRegistry
from web_chat.trace import normalize_steps

logger = logging.getLogger("grid.web_chat.views")

UNTITLED = "New chat"


def conversation_title(view: dict[str, Any]) -> str:
    """The chat's name: the user's title, else its first words, else "New chat"."""
    metadata = view["metadata"]
    if metadata.get("title") and metadata.get("title") != UNTITLED:
        return metadata["title"]
    for msg in view["messages"]:
        if getattr(msg, "role", None) == "user":
            text = msg.get_text_content() if hasattr(msg, "get_text_content") else str(getattr(msg, "content", ""))
            text = " ".join(text.split())
            return text[:42] + ("..." if len(text) > 42 else "")
    return UNTITLED


def serialize_message(msg: Any, *, resumable: bool = False, versions: Optional[dict] = None) -> dict[str, Any]:
    """A stored message as the chat renders it.

    ``interruption`` marks a turn that stopped early; ``resumable`` is set
    only on the one Continue can still resume. ``kind`` "continuation" is
    the user entry of a Continue, shown as a marker, not a bubble.
    ``images`` are the data URLs of the images the message carries.
    ``compaction`` carries the token counts of the marker the thread shows
    where its context was compacted (``kind`` "compaction").
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
        "kind": (
            "continuation"
            if metadata.get("type") == CONTINUATION_TYPE
            else "compaction"
            if metadata.get("type") == COMPACTED_TYPE
            else None
        ),
        "compaction": (
            {"tokens_before": metadata.get("tokens_before"), "tokens_after": metadata.get("tokens_after")}
            if metadata.get("type") == COMPACTED_TYPE
            else None
        ),
        "images": images,
        "interruption": (
            {**record.to_dict(), "resumable": resumable} if record is not None else None
        ),
    }


def issue_payload(compute: Callable[[], Any]) -> dict[str, Any]:
    """Tool issues as JSON: each one, plus one line per distinct problem for display.

    A check that itself breaks is reported, never fatal.
    """
    try:
        issues = compute()
    except Exception as exc:
        logger.warning("Tool check failed: %s", exc)
        return {"items": [], "summary": [f"tool check failed: {exc}"]}
    return {"items": [issue.to_dict() for issue in issues], "summary": summarize(issues)}


def agent_options(
    registry: SystemRegistry, system_key: Optional[str] = None, personal: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Agents of one system, as the picker shows them; *personal* are the user's own."""
    system = system_key or registry.default_key()
    models = registry.config(system).config.models or {}
    options: list[dict[str, Any]] = []
    for agent_key, agent in registry.agents(system).items():
        model = models.get(agent.primary_model)
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
                "personal": agent_key in personal,
                # Tools that will fail and why; shown before the agent is used.
                "issues": issue_payload(lambda key=agent_key: registry.agent_issues(system, key)),
            }
        )
    return options


def system_options(
    registry: SystemRegistry, personal: frozenset[str] = frozenset(), *, reveal_paths: bool = True
) -> list[dict[str, Any]]:
    """Every selectable system with its agents - the whole picker payload.

    ``reveal_paths`` False leaves out where the configs live on the server:
    users who are not admins have no business with its file system.
    """
    options: list[dict[str, Any]] = []
    for system in registry.systems():
        try:
            agents = agent_options(registry, system.key, personal)
            default_agent = registry.config(system.key).get_default_agent()
            error = ""
            issues = issue_payload(
                lambda key=system.key: [issue for issue in registry.issues(key) if issue.agent is None]
            )
        except Exception as exc:  # a broken system stays visible and labelled
            logger.warning("System '%s' could not be loaded: %s", system.key, exc)
            agents, default_agent, error, issues = [], None, str(exc), {"items": [], "summary": []}
        options.append(
            {
                "key": system.key,
                "name": system.name,
                "description": system.description,
                # "draft": an admin's untested created system; "mine": the user's own.
                "badge": system.badge,
                "config_path": str(system.config_path) if reveal_paths else "",
                "default_agent": default_agent,
                "agents": agents,
                "error": error,
                "issues": issues,
            }
        )
    return options
