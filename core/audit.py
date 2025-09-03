from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Dict, Optional

from agents import Agent, AgentHooks, RunContextWrapper, Runner, OpenAIChatCompletionsModel
from agents.guardrail import OutputGuardrail, GuardrailFunctionOutput
from openai import AsyncOpenAI

from core.config import Config
from utils.logger import Logger

_audit_logger = Logger("audit")


@dataclass
class TaskAuditContext:
    """Локальный контекст проверяемого агента для Output guardrail.
    Хранит текст задачи (исходный input для агента) и список использованных инструментов.
    Дополнительно может содержать ключ проверяющего (verifier_key) из конфигурации.
    """
    task_text: str
    tools_used: List[str] = field(default_factory=list)
    verifier_key: Optional[str] = None


class ToolUsageHooks(AgentHooks[TaskAuditContext]):
    """Простые хуки: фиксируем имена инструментов, вызываемых агентом.
    Никаких блокировок/перезапусков — только сбор факта использования.
    """

    async def on_tool_start(self, ctx: RunContextWrapper[TaskAuditContext], agent: Agent, tool) -> None:  # type: ignore[override]
        try:
            name = getattr(tool, "name", str(tool))
            ctx.context.tools_used.append(name)
            _audit_logger.info(
                "AUDIT_TOOL_USED",
                phase="on_tool_start",
                agent=getattr(agent, "name", "agent"),
                tool=name,
            )
        except Exception:
            pass


async def _create_verifier_agent(checker_key: str | None) -> Agent:
    cfg = Config()
    selected_key = checker_key or "default_audit"
    checker = (cfg.config.checkers or {}).get(selected_key) if cfg.config else None

    instructions = (
        (checker.get("prompt") if checker else None)
        or (
            "Ты проверяющий агент. Проверь корректность и надёжность ответа исполняющего агента.\n"
            "Входные данные придут одной строкой JSON с полями: task_text, tools_used, agent_output.\n"
            "Верни ровно JSON: {\"status\": \"ok|failed\", \"reason\": <string>, \"details\": <string>} без лишнего текста."
        )
    )

    if checker and (model_key := checker.get("model")):
        model_cfg = cfg.get_model(model_key)
        provider_cfg = cfg.get_provider(model_cfg.provider)
        client = AsyncOpenAI(
            api_key=cfg.get_api_key(model_cfg.provider),
            base_url=provider_cfg.base_url,
            timeout=provider_cfg.timeout,
            max_retries=provider_cfg.max_retries,
        )
        model = OpenAIChatCompletionsModel(model=model_cfg.name, openai_client=client)
        return Agent(name=f"Verifier:{selected_key}", instructions=instructions, model=model)

    return Agent(name=f"Verifier:{selected_key}", instructions=instructions)


async def _create_tool_verifier_agent(checker_key: str | None) -> Agent:
    cfg = Config()
    selected_key = checker_key or "default_audit"
    checker = (cfg.config.checkers or {}).get(selected_key) if cfg.config else None

    tool_prompt = None
    if checker and isinstance(checker.get("tool_prompt"), str):
        tool_prompt = checker.get("tool_prompt")
    if not tool_prompt:
        tool_prompt = (
            "Ты проверяющий агент вызовов инструментов. Оцени, корректен ли вызов инструмента с учётом цели задачи.\n"
            "Вход: одна строка JSON с полями: task_text, tools_used, tool_name, tool_arguments.\n"
            "Верни строго JSON: {\"status\":\"ok|failed\",\"reason\":<string>,\"details\":<string>} без лишнего текста."
        )

    if checker and (model_key := checker.get("model")):
        model_cfg = cfg.get_model(model_key)
        provider_cfg = cfg.get_provider(model_cfg.provider)
        client = AsyncOpenAI(
            api_key=cfg.get_api_key(model_cfg.provider),
            base_url=provider_cfg.base_url,
            timeout=provider_cfg.timeout,
            max_retries=provider_cfg.max_retries,
        )
        model = OpenAIChatCompletionsModel(model=model_cfg.name, openai_client=client)
        return Agent(name=f"ToolVerifier:{selected_key}", instructions=tool_prompt, model=model)

    return Agent(name=f"ToolVerifier:{selected_key}", instructions=tool_prompt)


async def verify_tool_call(
    *,
    verifier_key: Optional[str],
    task_text: str,
    tools_used: List[str],
    tool_name: str,
    tool_arguments: Dict[str, Any],
) -> Dict[str, Any]:
    import json

    agent = await _create_tool_verifier_agent(verifier_key)
    payload = json.dumps(
        {
            "task_text": task_text,
            "tools_used": tools_used,
            "tool_name": tool_name,
            "tool_arguments": tool_arguments,
        },
        ensure_ascii=False,
    )
    _audit_logger.info(
        "AUDIT_TOOL_VERIFY_START",
        verifier=verifier_key or "default_audit",
        tool_name=tool_name,
    )
    result = await Runner.run(starting_agent=agent, input=payload)

    try:
        if isinstance(result, str):
            verdict = json.loads(result)
        elif hasattr(result, "final_output") and result.final_output:
            verdict = json.loads(str(result.final_output))
        else:
            verdict = {"status": "ok", "reason": "empty_output"}
    except Exception:
        verdict = {"status": "ok", "reason": "unparsed"}

    _audit_logger.info(
        "AUDIT_TOOL_VERIFY_END",
        verifier=verifier_key or "default_audit",
        tool_name=tool_name,
        verdict_status=str(verdict.get("status", "")),
        verdict_reason=str(verdict.get("reason", "")),
    )

    return verdict


def create_output_audit_guardrail(name: str = "output_audit") -> OutputGuardrail[TaskAuditContext]:
    """Фабрика Output guardrail: делегирует проверку LLM-проверяющему агенту по конфигу."""

    async def _fn(ctx: RunContextWrapper[TaskAuditContext], agent: Agent, agent_output: Any) -> GuardrailFunctionOutput:
        try:
            task_text = getattr(ctx.context, "task_text", "") if ctx and ctx.context else ""
            tools_used = list(dict.fromkeys(getattr(ctx.context, "tools_used", []) or [])) if ctx and ctx.context else []
            verifier_key = getattr(ctx.context, "verifier_key", None) if ctx and ctx.context else None
            output_text = agent_output if isinstance(agent_output, str) else str(agent_output)

            verifier_agent = await _create_verifier_agent(verifier_key)

            import json
            payload = json.dumps({
                "task_text": task_text,
                "tools_used": tools_used,
                "agent_output": output_text,
            }, ensure_ascii=False)

            _audit_logger.info(
                "AUDIT_OUTPUT_VERIFY_START",
                verifier=verifier_key or "default_audit",
                agent=getattr(agent, "name", "agent"),
            )

            verify_result = await Runner.run(starting_agent=verifier_agent, input=payload)

            verdict = None
            try:
                if isinstance(verify_result, str):
                    verdict = json.loads(verify_result)
                elif hasattr(verify_result, 'final_output') and verify_result.final_output:
                    verdict = json.loads(str(verify_result.final_output))
            except Exception:
                verdict = None

            report = {
                "task_text": task_text,
                "tools_used": tools_used,
                "agent": getattr(agent, "name", "agent"),
                "verifier_key": verifier_key or "default_audit",
            }

            status_failed = isinstance(verdict, dict) and str(verdict.get("status", "")).lower() == "failed"
            _audit_logger.info(
                "AUDIT_OUTPUT_VERIFY_END",
                verifier=verifier_key or "default_audit",
                agent=getattr(agent, "name", "agent"),
                verdict_status="failed" if status_failed else "ok",
                verdict_reason=(verdict or {}).get("reason") if isinstance(verdict, dict) else "",
            )

            if status_failed:
                report["verdict"] = verdict
                return GuardrailFunctionOutput(output_info={"status": "failed", "report": report}, tripwire_triggered=True)

            report["verdict"] = verdict or {"status": "ok", "reason": "unparsed_or_empty"}
            return GuardrailFunctionOutput(output_info={"status": "ok", "report": report}, tripwire_triggered=False)
        except Exception as e:
            _audit_logger.error("AUDIT_OUTPUT_VERIFY_ERROR", error=str(e))
            return GuardrailFunctionOutput(output_info={"status": "ok", "report": {}}, tripwire_triggered=False)

    return OutputGuardrail(guardrail_function=_fn, name=name) 