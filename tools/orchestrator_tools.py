"""
Orchestrator tools: meta-tools that can spawn dynamic agents and run reviewed/committee workflows.

These tools are intentionally conservative: they require being executed inside Grid,
where `context.context.factory` (see `core.agent_factory.GridRunContext`) provides access
to the current `AgentFactory` instance.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from agents import RunContextWrapper, function_tool


@dataclass
class _ReviewDecision:
    decision: str  # "approve" | "revise"
    feedback: str = ""


@dataclass
class _Vote:
    vote: str  # "accept" | "reject"
    reason: str = ""


def _get_factory_from_context(ctx: RunContextWrapper) -> Any:
    raw = getattr(ctx, "context", None)
    return getattr(raw, "factory", None)


def _extract_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    # Best effort
    return str(output)


def _parse_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON parse: expects a JSON object, optionally wrapped in code fences."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove first fence line and the last fence if present
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    # Try direct
    try:
        val = json.loads(stripped)
        return val if isinstance(val, dict) else None
    except Exception:
        return None


def _coerce_optional_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        v = val.strip()
        if v == "" or v.lower() in ("none", "null"):
            return None
        return v
    return str(val)


def _coerce_tool_list(val: Any) -> Optional[List[str]]:
    """
    Accept:
    - list[str]
    - JSON string like '["filesystem","git"]'
    - comma-separated string like 'filesystem,git'
    """
    if val is None:
        return None
    if isinstance(val, list):
        out: List[str] = []
        for item in val:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out or None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # JSON list string
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return _coerce_tool_list(parsed)
            except Exception:
                pass
        # CSV fallback
        parts = [p.strip() for p in s.split(",")]
        parts = [p for p in parts if p]
        return parts or None
    # Fallback: stringified single value
    return [str(val).strip()] if str(val).strip() else None


async def _review_once(factory: Any, *, model_key: str, draft: str, goal: str) -> _ReviewDecision:
    reviewer_instructions = (
        "Ты проверяющий (reviewer). Тебе дадут цель и черновик решения.\n"
        "Нужно найти ошибки/пробелы/риски и решить: approve или revise.\n"
        "Верни строго JSON: {\"decision\":\"approve|revise\",\"feedback\":\"...\"}.\n"
        "Если approve — feedback может быть пустым."
    )
    reviewer = await factory.create_dynamic_agent(
        name=f"reviewer-{uuid.uuid4().hex[:6]}",
        instructions=reviewer_instructions,
        model_key=model_key,
        tool_names=[],
    )
    prompt = f"ЦЕЛЬ:\n{goal}\n\nЧЕРНОВИК:\n{draft}\n"
    out = await factory.run_agent_object_simple(reviewer, prompt)
    parsed = _parse_json_from_text(_extract_text(out)) or {}
    decision = str(parsed.get("decision", "")).strip().lower()
    feedback = str(parsed.get("feedback", "")).strip()
    if decision not in ("approve", "revise"):
        # Fail-safe: ask for revision
        return _ReviewDecision(decision="revise", feedback="Reviewer вернул некорректный формат; требуется переработка.")
    return _ReviewDecision(decision=decision, feedback=feedback)


async def _committee_vote(factory: Any, *, model_key: str, goal: str, draft: str, n: int) -> Tuple[bool, List[_Vote]]:
    judges: List[_Vote] = []
    for _ in range(max(1, n)):
        judge_instructions = (
            "Ты член комиссии (judge). Тебе дадут цель и решение.\n"
            "Оцени, достаточно ли это хорошо для принятия.\n"
            "Верни строго JSON: {\"vote\":\"accept|reject\",\"reason\":\"...\"}."
        )
        judge = await factory.create_dynamic_agent(
            name=f"judge-{uuid.uuid4().hex[:6]}",
            instructions=judge_instructions,
            model_key=model_key,
            tool_names=[],
        )
        prompt = f"ЦЕЛЬ:\n{goal}\n\nРЕШЕНИЕ:\n{draft}\n"
        out = await factory.run_agent_object_simple(judge, prompt)
        parsed = _parse_json_from_text(_extract_text(out)) or {}
        vote = str(parsed.get("vote", "")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()
        if vote not in ("accept", "reject"):
            vote = "reject"
            reason = reason or "Некорректный формат голоса."
        judges.append(_Vote(vote=vote, reason=reason))

    accept_count = sum(1 for j in judges if j.vote == "accept")
    reject_count = len(judges) - accept_count
    return accept_count > reject_count, judges


@function_tool
async def orchestrate(
    context: RunContextWrapper,
    task: str,
    agent_system_prompt: Optional[str] = None,
    mode: str = "basic",
    max_revisions: int = 2,
    committee_size: int = 3,
    model_key: Optional[str] = None,
    executor_tools: Optional[Any] = None,
) -> str:
    """
    Мета-инструмент: запускает динамического исполнителя для решения задачи.
    По умолчанию работает в режиме "basic" (только исполнение).
    Можно включить режимы "review" и "committee" через параметр `mode`.

    Аргументы:
    - task: Задача, которую должен выполнить агент
    - agent_system_prompt: Системный промпт (роль и инструкции) для агента.
    - mode: "basic" (дефолт), "review", "committee" (или "review+committee")
    - model_key: Ключ модели (если не задан, берется из конфига инструмента или дефолтный)
    - executor_tools: Список инструментов для агента
    """
    factory = _get_factory_from_context(context)
    if factory is None:
        return "❌ orchestrate: нет доступа к AgentFactory (ожидается context.context.factory)."

    # Пытаемся получить модель из конфига инструмента orchestrate, если не передана явно
    default_model_key = None
    try:
        tool_cfg = factory.config.get_tool("orchestrate")
        if tool_cfg.env_vars:
            default_model_key = tool_cfg.env_vars.get("DEFAULT_MODEL")
    except Exception:
        pass

    resolved_model_key = _coerce_optional_str(model_key) or default_model_key or factory.resolve_model_key(None)
    coerced_executor_tools = _coerce_tool_list(executor_tools)
    tools = coerced_executor_tools or [
        # разумный дефолт для этого репозитория (MCP-инструменты из config.yaml)
        "filesystem",
        "git",
        "terminal",
    ]

    base_instructions = _coerce_optional_str(agent_system_prompt) or (
        "Ты исполнитель (executor). Реши задачу максимально качественно.\n"
        "Если доступны инструменты — используй их.\n"
        "Дай итог в виде: (1) решение, (2) краткие допущения/ограничения, (3) следующие шаги."
    )
    
    executor = await factory.create_dynamic_agent(
        name=f"executor-{uuid.uuid4().hex[:6]}",
        instructions=base_instructions,
        model_key=resolved_model_key,
        tool_names=tools,
    )

    draft = await factory.run_agent_object_simple(executor, task)

    if "review" in mode:
        for _ in range(max(0, max_revisions)):
            decision = await _review_once(factory, model_key=resolved_model_key, draft=draft, goal=task)
            if decision.decision == "approve":
                break
            # revise
            revise_prompt = (
                f"ЦЕЛЬ:\n{task}\n\n"
                f"ПРОШЛАЯ ВЕРСИЯ:\n{draft}\n\n"
                f"ПРАВКИ ОТ REVIEWER:\n{decision.feedback}\n\n"
                "Сделай исправленную версию целиком."
            )
            draft = await factory.run_agent_object_simple(executor, revise_prompt)

    committee_block = ""
    if "committee" in mode:
        accepted, votes = await _committee_vote(
            factory,
            model_key=resolved_model_key,
            goal=task,
            draft=draft,
            n=committee_size,
        )
        committee_block = json.dumps(
            {
                "accepted": accepted,
                "votes": [asdict(v) for v in votes],
            },
            ensure_ascii=False,
            indent=2,
        )

    result = {
        "task": task,
        "mode": mode,
        "model_key": resolved_model_key,
        "executor_tools": tools,
        "final": _extract_text(draft),
    }
    if committee_block:
        result["committee"] = json.loads(committee_block)

    return json.dumps(result, ensure_ascii=False, indent=2)


ORCHESTRATOR_TOOLS = {
    "orchestrate": orchestrate,
}




