"""
Orchestrator tools: meta-tools that can spawn dynamic agents and run reviewed/committee workflows.

These tools are intentionally conservative: they require being executed inside Grid,
where `context.context.factory` (see `core.agent_factory.GridRunContext`) provides access
to the current `AgentFactory` instance.

"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from agents import RunContextWrapper, function_tool

logger = logging.getLogger(__name__)
verbose_logger = logging.getLogger("grid.verbose")

# Семафор для последовательного выполнения orchestrate (лимит = 1)
_orchestrate_semaphore = asyncio.Semaphore(1)
# Глубина вложенности orchestrate в текущем asyncio-контексте (для защиты от deadlock при реэнтерансе)
_orchestrate_depth: contextvars.ContextVar[int] = contextvars.ContextVar("_orchestrate_depth", default=0)


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
    model_key: Optional[str] = None,
    executor_tools: Optional[Union[List[str], str]] = None,
    context_id: Optional[str] = None,
) -> str:
    """
    Мета-инструмент: запускает динамического исполнителя для решения задачи.
    Аргументы:
    - task: Задача, которую должен выполнить агент
    - agent_system_prompt: Системный промпт (роль и инструкции) для агента.
    - executor_tools: Список инструментов для агента 
    
    Примечание: Вызовы orchestrate выполняются последовательно (не параллельно)
    для предотвращения конфликтов и обеспечения предсказуемости выполнения.
    """
    depth = _orchestrate_depth.get()
    token = _orchestrate_depth.set(depth + 1)
    acquired = False
    try:
        # Используем семафор только на верхнем уровне.
        # Это делает orchestrate реентерабельным (если orchestrate вызывает orchestrate),
        # не создавая deadlock внутри одного и того же asyncio Task.
        if depth == 0:
            await _orchestrate_semaphore.acquire()
            acquired = True

        factory = _get_factory_from_context(context)
        if factory is None:
            return "❌ orchestrate: нет доступа к AgentFactory (ожидается context.context.factory)."

        # Import pipeline registry (inside function to avoid circular import)
        from core.pipeline_registry import PipelineRegistry, PipelineStatus

        # Register pipeline
        registry = PipelineRegistry()
        active_context_id = context_id or factory.get_active_context_id()
        ctx_user_id = getattr(getattr(context, "context", None), "user_id", None)

        pipeline_id = await registry.register_pipeline(
            orchestrator_name="orchestrate",
            context_id=active_context_id,
            user_id=ctx_user_id
        )

        logger.info(f"orchestrate: START | task_len={len(task)} | model_key={model_key} | pipeline_id={pipeline_id}")
        verbose_logger.debug(
            f"\n{'='*80}\nORCHESTRATE START\n{'='*80}\n"
            f"Task: {task}\nModel key: {model_key}\n"
            f"Agent system prompt: {agent_system_prompt[:500] if agent_system_prompt else 'None'}\n"
            f"Executor tools: {executor_tools}\n{'='*80}\n"
        )

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

        base_instructions = _coerce_optional_str(agent_system_prompt)

        executor = await factory.create_dynamic_agent(
            name=f"executor-{uuid.uuid4().hex[:6]}",
            instructions=base_instructions,
            model_key=resolved_model_key,
            tool_names=coerced_executor_tools,
        )

        # Wrap execution in asyncio.Task for pipeline tracking
        agent_task = asyncio.create_task(
            factory.run_agent_object_simple(executor, task, context_id=context_id)
        )

        # Register task in pipeline
        task_id = await registry.register_task(
            pipeline_id=pipeline_id,
            agent_name=executor.name,
            task=agent_task,
            metadata={"model_key": resolved_model_key, "tools": coerced_executor_tools}
        )

        # Execute with emergency shutdown handling
        try:
            draft = await agent_task
            await registry.mark_task_completed(pipeline_id, task_id, success=True)
        except asyncio.CancelledError:
            # Task was cancelled - check if it was emergency shutdown
            await registry.mark_task_completed(pipeline_id, task_id, success=False, error="Cancelled")
            status = await registry.get_pipeline_status(pipeline_id)

            if status.get("status") == PipelineStatus.EMERGENCY_STOPPED.value:
                # Return emergency shutdown information to orchestrator
                result = {
                    "emergency_stopped": True,
                    "emergency_reason": status.get("emergency_reason"),
                    "emergency_severity": status.get("emergency_severity"),
                    "pipeline_id": pipeline_id,
                    "completed_tasks": len(status.get("completed_tasks", [])),
                    "failed_tasks": len(status.get("failed_tasks", {})),
                    "total_tasks": status.get("all_tasks", 0),
                    "task": task,
                    "context_id": active_context_id
                }
                result_json = json.dumps(result, ensure_ascii=False, indent=2)
                logger.warning(f"orchestrate: EMERGENCY STOPPED | pipeline_id={pipeline_id} | reason={status.get('emergency_reason')}")
                verbose_logger.debug(
                    f"\n{'='*80}\nORCHESTRATE EMERGENCY STOPPED\n{'='*80}\n"
                    f"Pipeline ID: {pipeline_id}\n"
                    f"Result:\n{result_json}\n{'='*80}\n"
                )
                return result_json
            # Re-raise if not emergency shutdown
            raise
        except Exception as exec_err:
            await registry.mark_task_completed(pipeline_id, task_id, success=False, error=str(exec_err))
            logger.error(f"orchestrate: executor failed | error={exec_err}")
            draft = f"❌ Executor failed: {exec_err}"

        result = {
            "task": task,
            "context_id": active_context_id,
            "model_key": resolved_model_key,
            "executor_tools": coerced_executor_tools,
            "final": _extract_text(draft),
            "pipeline_id": pipeline_id
        }
        result_json = json.dumps(result, ensure_ascii=False, indent=2)
        logger.info(
            f"orchestrate: COMPLETE  | model={resolved_model_key} | "
            f"output_len={len(result.get('final', ''))}"
        )
        verbose_logger.debug(
            f"\n{'='*80}\nORCHESTRATE COMPLETE\n{'='*80}\n"
            f"Model: {resolved_model_key}\n"
            f"Result (first 3000 chars):\n{result_json[:3000]}\n{'='*80}\n"
        )
        return result_json
    finally:
        # Важно: сбрасываем depth корректно даже при исключениях
        _orchestrate_depth.reset(token)
        if acquired:
            _orchestrate_semaphore.release()


ORCHESTRATOR_TOOLS = {
    "orchestrate": orchestrate,
}




