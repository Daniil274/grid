"""
Rerun logic: воспроизведение агента из снимка контекста (generation span).

Берём input_data generation-спана (полная история сообщений),
позволяем изменить любое сообщение и запускаем агента заново.
"""

from __future__ import annotations

import json
import uuid
import logging
from typing import Any

logger = logging.getLogger("grid.timeline.rerun")


def _parse(s: Any) -> Any:
    if isinstance(s, str):
        try:
            return json.loads(s)
        except Exception:
            return s
    return s


def extract_messages(input_data: Any) -> list[dict]:
    """
    Извлечь список сообщений из input_data generation-спана.
    SDK сохраняет input как list[{role, content}].
    """
    data = _parse(input_data)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Иногда обёрнуто: {"messages": [...]}
        return data.get("messages", [])
    return []


def split_system_and_conversation(messages: list[dict]) -> tuple[str, list[dict]]:
    """Разделить system-сообщения и диалог."""
    system_parts = []
    conversation = []
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, list):
                # Multipart content
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            system_parts.append(str(content))
        else:
            conversation.append(m)
    return "\n\n".join(system_parts), conversation


async def rerun_from_node(
    factory: Any,
    node: dict,
    modified_messages: list[dict],
    user_id: str = "default_user",
) -> dict:
    """
    Запустить агента с изменённым снимком контекста.

    Args:
        factory: AgentFactory instance
        node: raw node dict from SQLite
        modified_messages: полная история сообщений (изменённая пользователем)
        user_id: user ID для workspace isolation

    Returns:
        {"output": str, "trace_id": str | None}
    """
    from agents import Runner
    from core.agent_factory import GridRunContext

    instructions, conversation = split_system_and_conversation(modified_messages)

    if not instructions:
        instructions = "You are a helpful assistant."

    # Создаём динамического агента с теми же инструкциями
    agent_name = f"rerun-{uuid.uuid4().hex[:6]}"
    try:
        agent = await factory.create_dynamic_agent(
            name=agent_name,
            instructions=instructions,
            model_key=None,  # default model
            tool_names=[],   # без инструментов — чистый диалог
        )
    except Exception as e:
        logger.error(f"Failed to create rerun agent: {e}")
        raise

    # Построить GridRunContext
    active_context_id = factory.context_manager.start_new_context()
    run_ctx = GridRunContext(
        factory=factory,
        context_id=active_context_id,
        session=None,
        user_id=user_id,
        container_id=getattr(factory, "container_id", None),
    )

    # Запускаем с conversation messages как input
    # SDK Runner.run_streamed принимает list[dict] в качестве input
    input_messages: Any = conversation if conversation else "Continue."

    try:
        run_result_streaming = Runner.run_streamed(
            agent,
            input_messages,
            context=run_ctx,
            max_turns=20,
        )
        final_output = ""
        async for _ in run_result_streaming.stream_events():
            pass
        final_output = run_result_streaming.final_output or ""
    except Exception as e:
        logger.error(f"Rerun execution failed: {e}")
        raise

    return {
        "output": final_output,
        "agent_name": agent_name,
        "context_id": active_context_id,
    }
