"""
Orchestrator tools: meta-tools that can spawn dynamic agents and run reviewed/committee workflows.

These tools are intentionally conservative: they require being executed inside Grid,
where `context.context.factory` (see `core.agent_factory.GridRunContext`) provides access
to the current `AgentFactory` instance.

Social Intelligence Framework integration:
- Blackboard for shared memory between agents
- Primitives for atomic operations (critique, validate, synthesize, etc.)
- Pipeline Memory for learning and reusing successful pipelines
- Emergent orchestration: agents build their own pipelines
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
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

        logger.info(f"orchestrate: START | task_len={len(task)} | model_key={model_key}")
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

        draft = await factory.run_agent_object_simple(executor, task, context_id=context_id)
        active_context_id = context_id or factory.get_active_context_id()

        result = {
            "task": task,
            "context_id": active_context_id,
            "model_key": resolved_model_key,
            "executor_tools": coerced_executor_tools,
            "final": _extract_text(draft),
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


def _get_si_components(ctx: RunContextWrapper) -> Tuple[Any, Any, Any]:
    """
    Get Social Intelligence components from context.

    Returns:
        Tuple of (blackboard, primitives, pipeline_memory) or (None, None, None)
    """
    factory = _get_factory_from_context(ctx)
    if factory is None:
        return None, None, None

    blackboard = getattr(factory, '_blackboard', None)
    primitives = getattr(factory, '_primitives', None)
    pipeline_memory = getattr(factory, '_pipeline_memory', None)

    return blackboard, primitives, pipeline_memory


def _should_explore(exploration_rate: float = 0.2) -> bool:
    """Determine if we should explore (try new approach) vs exploit (use known)."""
    return random.random() < exploration_rate


async def _build_dynamic_pipeline(
    factory: Any,
    task: str,
    blackboard: Any,
    model_key: str,
    pipeline_memory: Any = None
) -> List[Dict[str, Any]]:
    """
    Let a meta-agent design the pipeline dynamically with full context.

    This is the key emergent behavior: the agent decides
    which primitives to combine based on:
    - The task
    - Current blackboard state
    - Previously successful pipelines
    """
    # Try to load planner prompt from config
    planner_prompt = None
    try:
        if hasattr(factory, 'config') and factory.config:
            config = factory.config
            if hasattr(config, 'config'):
                raw_config = config.config.dict() if hasattr(config.config, 'dict') else {}
                si_prompts = raw_config.get('si_prompts', {})
                planner_prompt = si_prompts.get('pipeline_planner')
    except Exception:
        pass

    # Fallback to default prompt
    if not planner_prompt:
        planner_prompt = """Ты архитектор мультиагентных систем. Проанализируй задачу и спроектируй оптимальный пайплайн из примитивов.

ДОСТУПНЫЕ ПРИМИТИВЫ:
- execute: Выполнить задачу агентом (params: system_prompt, tools)
- critique: Критический анализ (params: criteria, perspective, tools)
- validate: Проверка на галлюцинации/ошибки (params: checks, tools)
- revise: Исправить контент по обратной связи (params: feedback_from, tools)
- synthesize: Объединить несколько результатов (params: style)
- branch: Параллельное выполнение с разных перспектив (params: perspectives)
- vote: Голосование эксперта (params: perspective)

Верни JSON:
{
    "pipeline_name": "краткое имя",
    "rationale": "почему именно такой пайплайн",
    "steps": [
        {"primitive": "execute", "params": {"system_prompt": "...", "tools": ["filesystem"]}, "output_key": "draft"},
        {"primitive": "critique", "params": {"perspective": "security"}, "input_from": "draft", "output_key": "critique"},
        {"primitive": "validate", "params": {}, "input_from": "draft", "output_key": "validation"}
    ]
}

Правила:
- Минимум примитивов для задачи
- Для простых задач: только execute
- Для сложных: execute → critique → revise → validate
- Для критичных: добавь validate
- revise получает feedback_from (output_key шага critique/validate)"""

    # === INJECT BLACKBOARD CONTEXT ===
    context_sections = []

    if blackboard:
        bb_context = blackboard.get_context_for_agent(
            agent_name="pipeline-planner",
            max_entries=10,
            include_types=None
        )
        if bb_context and "Пусто" not in bb_context:
            context_sections.append(
                f"CURRENT BLACKBOARD STATE (what agents have produced so far):\n{bb_context}"
            )

    # === INJECT PIPELINE MEMORY CONTEXT ===
    if pipeline_memory:
        try:
            similar = pipeline_memory.find_similar(task, limit=3, min_effectiveness=0.3)
            if similar:
                memory_lines = ["PREVIOUSLY SUCCESSFUL PIPELINES FOR SIMILAR TASKS:"]
                for pipeline, similarity in similar:
                    steps_summary = " -> ".join(s.primitive for s in pipeline.steps)
                    memory_lines.append(
                        f"- '{pipeline.name}' (similarity: {similarity:.0%}, "
                        f"effectiveness: {pipeline.get_effectiveness():.0%}): {steps_summary}"
                    )
                context_sections.append("\n".join(memory_lines))
        except Exception:
            pass  # Ignore errors in pipeline memory

    # Build enhanced prompt
    if context_sections:
        full_prompt = "\n\n".join(context_sections) + "\n\n" + planner_prompt
    else:
        full_prompt = planner_prompt

    try:
        logger.info(f"_build_dynamic_pipeline: Creating pipeline planner for task: {task[:100]}...")
        planner = await factory.create_dynamic_agent(
            name=f"pipeline-planner-{uuid.uuid4().hex[:6]}",
            instructions=full_prompt,
            model_key=model_key,
            tool_names=[]
        )

        logger.info(f"_build_dynamic_pipeline: Running planner agent")
        raw_output = await factory.run_agent_object_simple(
            planner, f"ЗАДАЧА:\n{task}"
        )
        logger.info(f"_build_dynamic_pipeline: Planner raw output: {raw_output[:500]}...")

        parsed = _parse_json_from_text(raw_output)
        if parsed and "steps" in parsed:
            steps = parsed.get("steps", [])
            logger.info(f"_build_dynamic_pipeline: Successfully parsed {len(steps)} steps from planner")
            return steps
        else:
            logger.warning("_build_dynamic_pipeline: Failed to parse steps from planner output")

    except Exception as e:
        logger.error(f"Pipeline planning failed: {e}", exc_info=True)

    # Fallback: simple execute
    logger.warning("_build_dynamic_pipeline: Falling back to simple execute")
    return [{"primitive": "execute", "params": {}, "output_key": "result"}]


def _build_step_context(outputs: Dict[str, Any], input_from: Optional[str], task: str) -> str:
    """
    Build accumulated context for a pipeline step.

    Each step sees:
    - Primary input (from specified previous step or task)
    - Accumulated context from ALL prior steps

    This ensures agents don't miss important findings from earlier stages.
    """
    # Primary input: specified step output or original task
    primary_input = outputs.get(input_from, task) if input_from else task

    # Build accumulated context from all prior outputs
    prior_outputs = []
    for key, value in outputs.items():
        if key == "task":
            continue
        if key == input_from:
            continue  # Already the primary input
        if value is None:
            continue

        val_str = str(value)
        if len(val_str) > 500:
            val_str = val_str[:500] + "..."
        prior_outputs.append(f"[{key}]: {val_str}")

    if not prior_outputs:
        return primary_input

    accumulated = "\n".join(prior_outputs)
    return (
        f"ACCUMULATED CONTEXT FROM PRIOR STEPS:\n{accumulated}\n\n"
        f"PRIMARY INPUT:\n{primary_input}"
    )


async def _execute_pipeline(
    factory: Any,
    task: str,
    steps: List[Dict[str, Any]],
    blackboard: Any,
    primitives: Any,
    model_key: str,
    tools: List[str],
    max_refinement_iterations: int = 2
) -> Tuple[str, float, List[str]]:
    """
    Execute a pipeline of primitives.

    Returns:
        Tuple of (final_output, success_score, blackboard_entry_ids)
    """
    outputs = {"task": task}
    entry_ids = []
    total_score = 0.0
    score_count = 0

    logger.info(f"_execute_pipeline: Starting pipeline with {len(steps)} steps for task: {task[:100]}...")
    verbose_logger.debug(
        f"\n{'='*80}\nPIPELINE EXECUTION START\n{'='*80}\n"
        f"Task: {task}\nSteps: {json.dumps(steps, ensure_ascii=False, indent=2)}\n{'='*80}\n"
    )

    for i, step in enumerate(steps):
        primitive = step.get("primitive", "execute")
        params = step.get("params", {})
        input_from = step.get("input_from")
        output_key = step.get("output_key", f"step_{i}")

        # Build context with accumulated knowledge from all prior steps
        input_content = _build_step_context(outputs, input_from, task)

        logger.info(f"_execute_pipeline: Step {i+1}/{len(steps)} | primitive={primitive} | input_from={input_from} | output_key={output_key}")
        verbose_logger.debug(
            f"\n{'─'*80}\nPIPELINE STEP {i+1}/{len(steps)}\n{'─'*80}\n"
            f"Primitive: {primitive}\nParams: {json.dumps(params, ensure_ascii=False, default=str)}\n"
            f"Input from: {input_from}\nOutput key: {output_key}\n"
            f"Input content (first 2000 chars):\n{str(input_content)[:2000]}\n{'─'*80}\n"
        )

        try:
            if primitive == "execute":
                result = await primitives.execute(
                    task=input_content,
                    system_prompt=params.get("system_prompt"),
                    tools=params.get("tools", tools),
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None,
                    tags=["pipeline", primitive]
                )
                outputs[output_key] = result.output
                if result.success:
                    total_score += 1.0
                    score_count += 1

            elif primitive == "critique":
                result = await primitives.critique(
                    content=input_content,
                    criteria=params.get("criteria"),
                    perspective=params.get("perspective"),
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None,
                    tools=params.get("tools")
                )
                outputs[output_key] = result.raw_output
                total_score += result.overall_score
                score_count += 1

            elif primitive == "validate":
                result = await primitives.validate(
                    content=input_content,
                    context=task,
                    checks=params.get("checks"),
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None,
                    tools=params.get("tools")
                )
                outputs[output_key] = result.raw_output

                # === ITERATIVE REFINEMENT LOOP ===
                if not result.valid and max_refinement_iterations > 0:
                    # Find the content that was validated (the input to this step)
                    content_to_revise = input_content
                    feedback = result.raw_output

                    for iteration in range(max_refinement_iterations):
                        # Revise based on validation feedback
                        revise_result = await primitives.revise(
                            original_content=content_to_revise,
                            feedback=feedback,
                            task=task,
                            tools=params.get("tools", tools),
                            model_key=model_key,
                            post_to_blackboard=blackboard is not None,
                            tags=["revision", f"iteration_{iteration+1}"]
                        )

                        if not revise_result.success:
                            break

                        content_to_revise = revise_result.output

                        # Re-validate
                        re_validation = await primitives.validate(
                            content=content_to_revise,
                            context=task,
                            checks=params.get("checks"),
                            model_key=model_key,
                            post_to_blackboard=blackboard is not None,
                            tools=params.get("tools")
                        )

                        if re_validation.valid:
                            # Success! Update the outputs map so subsequent
                            # steps see the revised content
                            if input_from and input_from in outputs:
                                outputs[input_from] = content_to_revise
                            result = re_validation
                            break
                        else:
                            feedback = re_validation.raw_output

                    # Update output with final validation
                    outputs[output_key] = result.raw_output
                # === END ITERATIVE REFINEMENT ===

                if result.valid:
                    total_score += result.confidence
                else:
                    total_score += 0.3  # Penalty for invalid
                score_count += 1

            elif primitive == "revise":
                # Handle explicit revise steps in pipeline
                feedback_from = params.get("feedback_from")
                feedback_content = outputs.get(feedback_from, "") if feedback_from else ""

                result = await primitives.revise(
                    original_content=input_content,
                    feedback=feedback_content,
                    task=task,
                    system_prompt=params.get("system_prompt"),
                    tools=params.get("tools", tools),
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None,
                    tags=["pipeline", "revise"]
                )
                outputs[output_key] = result.output
                if result.success:
                    total_score += 1.0
                    score_count += 1

            elif primitive == "synthesize":
                # Get all previous outputs as inputs
                inputs_to_synth = [
                    str(v) for k, v in outputs.items()
                    if k != "task" and v
                ]
                if not inputs_to_synth:
                    inputs_to_synth = [input_content]

                result = await primitives.synthesize(
                    inputs=inputs_to_synth,
                    goal=task,
                    style=params.get("style", "comprehensive"),
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None
                )
                outputs[output_key] = result.output
                if result.success:
                    total_score += result.confidence
                    score_count += 1

            elif primitive == "branch":
                perspectives = params.get("perspectives", ["general"])
                results = await primitives.branch(
                    task=input_content,
                    perspectives=perspectives,
                    tools=params.get("tools", tools),
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None
                )
                outputs[output_key] = [r.output for r in results if r.success]
                total_score += sum(1 for r in results if r.success) / len(results)
                score_count += 1

            elif primitive == "vote":
                result = await primitives.vote(
                    proposal=input_content,
                    perspective=params.get("perspective", "expert"),
                    context=task,
                    model_key=model_key,
                    post_to_blackboard=blackboard is not None
                )
                outputs[output_key] = result.to_dict()
                total_score += result.confidence
                score_count += 1

            # Log step output
            step_output = outputs.get(output_key, "")
            step_output_str = str(step_output)
            logger.info(
                f"_execute_pipeline: Step {i+1}/{len(steps)} | primitive={primitive} | "
                f"output_len={len(step_output_str)} | score_so_far={total_score}/{score_count}"
            )
            verbose_logger.debug(
                f"\n{'─'*80}\nPIPELINE STEP {i+1} OUTPUT ({primitive})\n{'─'*80}\n"
                f"{step_output_str[:3000]}{'...(truncated)' if len(step_output_str) > 3000 else ''}\n{'─'*80}\n"
            )

        except Exception as e:
            outputs[output_key] = f"Error in {primitive}: {e}"
            score_count += 1  # Count as failed
            logger.error(f"_execute_pipeline: Step {i+1}/{len(steps)} FAILED | primitive={primitive} | error={e}")
            verbose_logger.debug(
                f"\n{'─'*80}\nPIPELINE STEP {i+1} ERROR ({primitive})\n{'─'*80}\n"
                f"Error: {e}\n{'─'*80}\n"
            )

    # Determine final output (last step or synthesized)
    final_output = outputs.get(steps[-1].get("output_key", "result"), "")
    if isinstance(final_output, list):
        final_output = "\n\n".join(str(x) for x in final_output)
    elif isinstance(final_output, dict):
        final_output = json.dumps(final_output, ensure_ascii=False, indent=2)

    avg_score = total_score / score_count if score_count > 0 else 0.5

    logger.info(
        f"_execute_pipeline: COMPLETE | steps={len(steps)} | avg_score={avg_score:.2f} | "
        f"output_len={len(str(final_output))}"
    )
    verbose_logger.debug(
        f"\n{'='*80}\nPIPELINE EXECUTION COMPLETE\n{'='*80}\n"
        f"Steps executed: {len(steps)}\nAverage score: {avg_score:.2f}\n"
        f"Final output (first 3000 chars):\n{str(final_output)[:3000]}\n{'='*80}\n"
    )

    return str(final_output), avg_score, entry_ids


@function_tool
async def orchestrate_emergent(
    context: RunContextWrapper,
    task: str,
    agent_system_prompt: Optional[str] = None,
    executor_tools: Optional[Union[List[str], str]] = None,
    model_key: Optional[str] = None,
    allow_pipeline_creation: bool = True,
    save_on_success: bool = True,
    use_blackboard: bool = True,
    validation_enabled: bool = True,
    exploration_rate: float = 0.2,
) -> str:
    """
    Эмерджентная оркестрация с самообучением.

    Мета-инструмент, который:
    1. Ищет похожий успешный пайплайн в памяти
    2. Если нашёл и не в режиме exploration — использует его
    3. Если не нашёл или exploration — строит новый пайплайн из примитивов
    4. Выполняет пайплайн с записью в blackboard
    5. Сохраняет успешные пайплайны для будущего

    Args:
        task: Задача для выполнения
        agent_system_prompt: Системный промпт (если указан, использует простой execute)
        executor_tools: Список инструментов
        model_key: Модель для использования
        allow_pipeline_creation: Разрешить создание новых пайплайнов
        save_on_success: Сохранять успешные пайплайны
        use_blackboard: Использовать общую память
        validation_enabled: Включить валидацию результата
        exploration_rate: Частота exploration vs exploitation (0-1)

    Returns:
        JSON с результатом, pipeline_id, blackboard entries
    """
    factory = _get_factory_from_context(context)
    if factory is None:
        return json.dumps({"error": "No AgentFactory access"}, ensure_ascii=False)

    # Get SI components
    blackboard, primitives, pipeline_memory = _get_si_components(context)

    # Resolve model key
    default_model_key = None
    try:
        tool_cfg = factory.config.get_tool("orchestrate")
        if tool_cfg.env_vars:
            default_model_key = tool_cfg.env_vars.get("DEFAULT_MODEL")
    except Exception:
        pass

    resolved_model_key = _coerce_optional_str(model_key) or default_model_key or factory.resolve_model_key(None)
    tools = _coerce_tool_list(executor_tools) or ["filesystem", "git", "terminal"]
    context_id = factory.get_active_context_id()

    result = {
        "task": task,
        "context_id": context_id,
        "mode": "emergent",
        "model_key": resolved_model_key,
        "executor_tools": tools,
        "pipeline_id": None,
        "pipeline_source": None,
        "blackboard_entries": [],
    }

    # REMOVED: agent_system_prompt check that was blocking emergent pipeline creation
    # Now orchestrate_emergent ALWAYS builds emergent pipelines (unless allow_pipeline_creation=False)
    # If LLM agent passes agent_system_prompt - it will be ignored to enable proper emergent behavior
    logger.info(
        f"orchestrate_emergent: START | task_len={len(task)} | "
        f"allow_pipeline_creation={allow_pipeline_creation} | "
        f"primitives={'exists' if primitives else 'none'} | "
        f"agent_system_prompt={'passed_but_ignored' if agent_system_prompt else 'none'}"
    )
    verbose_logger.debug(
        f"\n{'='*80}\nORCHESTRATE_EMERGENT START\n{'='*80}\n"
        f"Task: {task}\n"
        f"Allow pipeline creation: {allow_pipeline_creation}\n"
        f"Use blackboard: {use_blackboard}\n"
        f"Validation enabled: {validation_enabled}\n"
        f"Exploration rate: {exploration_rate}\n"
        f"Model key: {model_key}\n"
        f"Executor tools: {executor_tools}\n{'='*80}\n"
    )

    # Check pipeline memory for similar tasks
    pipeline_to_use = None
    should_explore = _should_explore(exploration_rate)
    logger.info(
        f"orchestrate_emergent: exploration_rate={exploration_rate}, "
        f"should_explore={should_explore}, "
        f"pipeline_memory={'exists' if pipeline_memory else 'none'}"
    )

    if pipeline_memory and not should_explore:
        similar = pipeline_memory.find_similar(task, limit=1, min_effectiveness=0.5)
        if similar:
            pipeline_to_use, similarity = similar[0]
            result["pipeline_source"] = f"memory:{pipeline_to_use.id} (similarity: {similarity:.2f})"
            logger.info(f"Using pipeline from memory: {pipeline_to_use.id} (similarity: {similarity:.2f})")

    # Build or use pipeline
    if pipeline_to_use:
        steps = [s.to_dict() for s in pipeline_to_use.steps]
        logger.info(f"Using existing pipeline with {len(steps)} steps")
    elif allow_pipeline_creation and primitives:
        logger.info("Building dynamic pipeline via pipeline planner")
        steps = await _build_dynamic_pipeline(
            factory, task, blackboard, resolved_model_key, pipeline_memory
        )
        result["pipeline_source"] = "dynamic_generation"
        logger.info(f"Pipeline planner returned {len(steps)} steps: {[s.get('primitive') for s in steps]}")
    else:
        # Fallback: simple execute
        logger.warning(
            f"Falling back to simple execute: allow_pipeline_creation={allow_pipeline_creation}, "
            f"primitives={'exists' if primitives else 'none'}"
        )
        steps = [{"primitive": "execute", "params": {}, "output_key": "result"}]
        result["pipeline_source"] = "fallback"

    # Execute pipeline
    if primitives:
        final_output, success_score, entry_ids = await _execute_pipeline(
            factory=factory,
            task=task,
            steps=steps,
            blackboard=blackboard if use_blackboard else None,
            primitives=primitives,
            model_key=resolved_model_key,
            tools=tools
        )
        result["final"] = final_output
        result["blackboard_entries"] = entry_ids
        result["success_score"] = success_score

        # Optionally validate with post-pipeline refinement
        if validation_enabled and success_score >= 0.6:
            validation = await primitives.validate(
                content=final_output,
                context=task,
                model_key=resolved_model_key,
                post_to_blackboard=use_blackboard and blackboard is not None
            )
            result["validation"] = validation.to_dict()

            # === POST-PIPELINE ITERATIVE REFINEMENT ===
            if not validation.valid:
                # Attempt refinement on the final output
                for _iter in range(2):  # Max 2 iterations
                    revise_result = await primitives.revise(
                        original_content=final_output,
                        feedback=validation.raw_output,
                        task=task,
                        tools=tools,
                        model_key=resolved_model_key,
                        post_to_blackboard=use_blackboard and blackboard is not None,
                        tags=["post_pipeline", f"iteration_{_iter+1}"]
                    )

                    if not revise_result.success:
                        break

                    final_output = revise_result.output

                    # Re-validate
                    re_validation = await primitives.validate(
                        content=final_output,
                        context=task,
                        model_key=resolved_model_key,
                        post_to_blackboard=use_blackboard and blackboard is not None
                    )
                    validation = re_validation

                    if re_validation.valid:
                        break

                result["final"] = final_output
                result["validation"] = validation.to_dict()
                if not validation.valid:
                    success_score *= 0.7  # Penalty
            # === END POST-PIPELINE REFINEMENT ===

        # Save successful pipeline
        if save_on_success and pipeline_memory and success_score >= 0.6:
            if not pipeline_to_use:  # New pipeline
                pipeline_id = pipeline_memory.save_pipeline(
                    name=f"auto-{uuid.uuid4().hex[:6]}",
                    description=f"Auto-generated for task: {task[:50]}...",
                    task=task,
                    steps=steps,
                    success_score=success_score,
                    tags=["auto", "emergent"]
                )
                result["pipeline_id"] = pipeline_id
            else:  # Record usage of existing
                pipeline_memory.record_usage(
                    pipeline_to_use.id,
                    success=success_score >= 0.6,
                    score=success_score,
                    task=task
                )
                result["pipeline_id"] = pipeline_to_use.id
    else:
        # No primitives available, fallback to simple execution
        logger.warning("orchestrate_emergent: No primitives, falling back to simple execution")
        executor = await factory.create_dynamic_agent(
            name=f"executor-{uuid.uuid4().hex[:6]}",
            instructions=(
                "Ты исполнитель (executor). Реши задачу максимально качественно.\n"
                "Если доступны инструменты — используй их.\n"
                "Дай итог в виде: (1) решение, (2) краткие допущения/ограничения, (3) следующие шаги."
            ),
            model_key=resolved_model_key,
            tool_names=tools,
        )
        output = await factory.run_agent_object_simple(executor, task)
        result["final"] = _extract_text(output)

    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    logger.info(
        f"orchestrate_emergent: COMPLETE | pipeline_source={result.get('pipeline_source')} | "
        f"pipeline_id={result.get('pipeline_id')} | "
        f"success_score={result.get('success_score', 'n/a')} | "
        f"output_len={len(result.get('final', ''))}"
    )
    verbose_logger.debug(
        f"\n{'='*80}\nORCHESTRATE_EMERGENT COMPLETE\n{'='*80}\n"
        f"Pipeline source: {result.get('pipeline_source')}\n"
        f"Pipeline ID: {result.get('pipeline_id')}\n"
        f"Success score: {result.get('success_score', 'n/a')}\n"
        f"Result (first 3000 chars):\n{result_json[:3000]}\n{'='*80}\n"
    )
    return result_json


ORCHESTRATOR_TOOLS = {
    "orchestrate": orchestrate,
    "orchestrate_emergent": orchestrate_emergent,
}




