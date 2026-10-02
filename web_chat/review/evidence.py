"""What a review freezes: everything behind one agent answer, at one moment.

:func:`collect` reads the user's space - it never changes it - and returns
plain JSON:

- ``target``: the answer - conversation, message, system, agent, turn;
- ``conversation``: its title and the messages up to and including the
  answer, sub-agent reports among them (images are counted, not copied);
- ``turn``: the steps the chat recorded for the answer (tool calls and
  their results, policy decisions, errors) and the agent runs of the turn;
- ``model_context``: the instructions and sections last sent to the model.
  They are recorded once per conversation, so they belong to the answer
  only when it is the conversation's latest; otherwise they are left out and
  ``note`` says why;
- ``agent_session``: the agent's SDK session - the exact items the model
  sees - as it is now;
- ``config``: the agent's definition, its tools and models, without the
  providers' addresses and keys;
- ``grid``: the commit the server runs.

Every string is scrubbed and bounded (web_chat.review.redact).
"""

from __future__ import annotations

import functools
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.context import is_tool_result
from core.managers.session_manager import agent_session_id
from web_chat.review.redact import Redactor, bounded, environment_secrets

FORMAT = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Most messages of the conversation kept, counting back from the answer.
MAX_MESSAGES = 200
#: Most SDK session items kept, the latest ones.
MAX_SESSION_ITEMS = 400
#: Most agent runs of the turn kept.
MAX_EXECUTIONS = 50


class EvidenceError(LookupError):
    """The answer to review is not there, or is not an agent's answer."""


def collect(space: Any, context_id: str, message_id: str, *, redactor: Optional[Redactor] = None) -> dict[str, Any]:
    """The evidence behind answer *message_id* of conversation *context_id*."""
    manager = space.context_manager()
    view = manager.conversation_view(context_id)
    if view is None:
        raise EvidenceError("Conversation not found")
    messages = view["messages"]
    index = _index_of(messages, message_id)
    answer = messages[index]
    if getattr(answer, "role", None) != "assistant" or is_tool_result(answer):
        raise EvidenceError("Only an agent's answer can be reviewed")

    answer_meta = dict(getattr(answer, "metadata", None) or {})
    conversation_meta = view.get("metadata") or {}
    agent = answer_meta.get("agent") or conversation_meta.get("routed_agent")
    system = answer_meta.get("system") or conversation_meta.get("routed_system")
    shown, earlier = bounded(messages[: index + 1], MAX_MESSAGES)
    latest = not any(
        getattr(message, "role", None) == "assistant" and not is_tool_result(message)
        for message in messages[index + 1 :]
    )

    evidence = {
        "format": FORMAT,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "grid": grid_version(),
        "target": {
            "context_id": context_id,
            "message_id": message_id,
            "system": system,
            "agent": agent,
            "turn_id": answer_meta.get("turn_id"),
        },
        "conversation": {
            "title": conversation_meta.get("title"),
            "messages": [_message(message) for message in shown],
            "earlier_messages_left_out": earlier,
        },
        "turn": {
            "steps": answer_meta.get("trace") or [],
            "executions": _executions(manager, context_id, _turn_start(messages, index), getattr(answer, "timestamp", None)),
        },
        "model_context": _model_context(conversation_meta, latest),
        "agent_session": _agent_session(space, agent, context_id, system),
        "config": _config(space, system, agent),
    }
    return (redactor or Redactor(environment_secrets())).value(evidence)


@functools.lru_cache(maxsize=1)
def grid_version() -> dict[str, Any]:
    """The commit this process runs, and whether its checkout had changes.

    Read once: the code a process runs does not change under it.
    """
    def git(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(
                ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10, check=True
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip()

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "changed": bool(status) if status is not None else None}


# -- parts ---------------------------------------------------------------------
def _index_of(messages: list[Any], message_id: str) -> int:
    for index, message in enumerate(messages):
        if (getattr(message, "metadata", None) or {}).get("message_id") == message_id:
            return index
    raise EvidenceError("Message not found in this conversation")


def _message(message: Any) -> dict[str, Any]:
    metadata = getattr(message, "metadata", None) or {}
    images = message.get_images() if hasattr(message, "get_images") else []
    text = message.get_text_content() if hasattr(message, "get_text_content") else str(getattr(message, "content", ""))
    return {
        "id": metadata.get("message_id"),
        "role": getattr(message, "role", None),
        "agent": metadata.get("agent"),
        "kind": "tool_result" if is_tool_result(message) else metadata.get("type"),
        "timestamp": getattr(message, "timestamp", None),
        "content": "" if images and text == "[multimodal content]" else text,
        "images": len(images),
    }


def _turn_start(messages: list[Any], index: int) -> Optional[str]:
    """Where the answer's turn begins: right after the previous answer.

    Not the user's message: a run is recorded as started before that message
    is stored.
    """
    for message in reversed(messages[:index]):
        if getattr(message, "role", None) == "assistant" and not is_tool_result(message):
            return getattr(message, "timestamp", None)
    return None


def _executions(manager: Any, context_id: str, start: Optional[str], end: Optional[str]) -> list[dict[str, Any]]:
    """The agent runs started after the previous answer and up to this one.

    Runs keep Unix times; messages keep local ISO times from the same clock.
    """
    since, until = _epoch(start), _epoch(end)
    payload = manager.get_context_executions(context_id, include_full=True) or {}
    runs = []
    for run in payload.get("executions") or []:
        started = _epoch(run.get("start_time"))
        if started is None:
            continue
        if (since is None or started >= since) and (until is None or started <= until):
            runs.append(run)
    return bounded(runs, MAX_EXECUTIONS)[0]


def _epoch(value: Any) -> Optional[float]:
    """A Unix time from a Unix time or a local ISO time; None when neither."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _model_context(metadata: dict[str, Any], latest: bool) -> dict[str, Any]:
    if not latest:
        return {
            "assembly": None,
            "instructions": None,
            "note": "Recorded for the conversation's latest answer only; a later answer replaced it.",
        }
    return {
        "assembly": metadata.get("last_context_assembly"),
        "instructions": metadata.get("agent_instructions"),
        "note": None,
    }


def _agent_session(space: Any, agent: Optional[str], context_id: str,
                   system: Optional[str] = None) -> dict[str, Any]:
    namespace = None
    if system and getattr(space, "layout", None) is not None and system != space.registry.default_key():
        namespace = system
    session_id = agent_session_id(agent, context_id, namespace) if agent else None
    path = getattr(space, "agent_sessions_path", None)
    if session_id is None or path is None or not Path(path).exists():
        return {"session_id": session_id, "items": [], "earlier_items_left_out": 0}
    # Read-only: the review must not touch the store the agent writes.
    with sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT message_data FROM agent_messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    items, earlier = bounded((_json(row[0]) for row in rows), MAX_SESSION_ITEMS)
    return {"session_id": session_id, "items": items, "earlier_items_left_out": earlier}


def _config(space: Any, system: Optional[str], agent: Optional[str]) -> dict[str, Any]:
    """The agent's definition with its tools and models; providers only by name."""
    registry = space.registry
    key = system if system in registry.keys() else registry.default_key()
    config = registry.config(key).config
    definition = config.agents.get(agent) if agent else None
    if definition is None:
        return {"system": key, "agent": None, "tools": {}, "models": {}}
    return {
        "system": key,
        "agent": definition.model_dump(mode="json"),
        "tools": {
            name: config.tools[name].model_dump(mode="json", exclude={"env_vars"})
            for name in definition.tools
            if name in config.tools
        },
        "models": {
            name: config.models[name].model_dump(
                mode="json", include={"name", "provider", "temperature", "max_tokens", "context_window", "reasoning"}
            )
            for name in definition.model_keys()
            if name in config.models
        },
    }


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text
