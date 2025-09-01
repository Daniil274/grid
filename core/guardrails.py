"""
Minimal, config-driven output guardrails for agents.

This module provides a lightweight hallucination verification guardrail that
reads all behavior from configuration (models, prompts, thresholds) and avoids
hardcoded prompts or logic in code. It uses the OpenAI Agents SDK primitives
only.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from openai import AsyncOpenAI

from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    output_guardrail,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    ModelSettings,
)

from schemas import HallucinationCheckOutput

logger = logging.getLogger(__name__)


def _get_config_from_agent(agent: Agent) -> Optional[Any]:
    """Return project Config attached to agent by factory, if present."""
    return getattr(agent, "_config", None)


def _get_target_agent_config(agent: Agent) -> Optional[Any]:
    """Return AgentConfig attached to agent by factory, if present."""
    return getattr(agent, "_agent_config", None)


async def _build_verifier_agent(config: Any, verifier_key: str, temperature: float, max_tokens: int) -> Optional[Agent]:
    """
    Build a minimal verifier Agent from configuration without any hardcoded prompts.
    - Uses the verifier agent's model and the prompt_templates referenced by it
    - No tools are attached (verification should not call tools by default)
    """
    try:
        verifier_cfg = config.get_agent(verifier_key)
        model_cfg = config.get_model(verifier_cfg.model)
        provider_cfg = config.get_provider(model_cfg.provider)
        api_key = config.get_api_key(model_cfg.provider)
        if not api_key:
            logger.warning("No API key for verifier provider; skipping hallucination check")
            return None

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=provider_cfg.base_url,
            timeout=provider_cfg.timeout,
            max_retries=provider_cfg.max_retries,
        )

        model = OpenAIChatCompletionsModel(
            model=model_cfg.name,
            openai_client=client,
        )

        instructions = config.build_agent_prompt(verifier_key)

        settings = ModelSettings(
            temperature=float(temperature),
            max_tokens=int(max_tokens),
        )

        return Agent(
            name=verifier_cfg.name or verifier_key,
            instructions=instructions,
            model=model,
            tools=[],
            output_type=HallucinationCheckOutput,
            model_settings=settings,
        )
    except Exception as e:
        logger.error(f"Failed to build verifier agent '{verifier_key}': {e}")
        return None


def _compose_verification_input(output_text: str, agent_instructions: Optional[str], context_text: Optional[str]) -> str:
    """
    Build the input payload for the verifier agent. This is data-only, not instructions.
    Instructions for how to evaluate live in the prompt template configured for
    the verifier agent.
    """
    parts: list[str] = []
    parts.append("ASSISTANT RESPONSE:\n" + (output_text or "").strip())
    if agent_instructions:
        parts.append("\nAGENT INSTRUCTIONS:\n" + str(agent_instructions).strip())
    if context_text:
        parts.append("\nCONTEXT SNIPPET:\n" + str(context_text).strip())
    return "\n\n".join(parts)


@output_guardrail
async def hallucination_guardrail(ctx: RunContextWrapper, agent: Agent, output: str) -> GuardrailFunctionOutput:
    """
    Config-driven hallucination verification guardrail.

    Behavior sources (no hardcoded prompts):
    - Which verifier agent to use: settings.verification_agent
    - How strict and thresholds: settings.verification_defaults + target agent overrides
    - Verifier prompts: prompt_templates used by the configured verifier agent
    """
    try:
        # Read configuration attached by AgentFactory
        config = _get_config_from_agent(agent)
        if not config:
            # No config available -> cannot verify safely
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

        settings = getattr(config.config, "settings", None)
        if not settings or not getattr(settings, "verify_hallucinations", False):
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

        verifier_key: str = getattr(settings, "verification_agent", "hallucination_checker")

        # Merge verification settings: global defaults then target agent overrides
        defaults = dict(getattr(settings, "verification_defaults", {}) or {})
        target_agent_cfg = _get_target_agent_config(agent)
        agent_overrides = {}
        if target_agent_cfg is not None and getattr(target_agent_cfg, "verification_settings", None):
            agent_overrides = dict(target_agent_cfg.verification_settings or {})

        merged = {**defaults, **agent_overrides}
        temperature = float(merged.get("temperature", 0.1))
        max_tokens = int(merged.get("max_tokens", 1000))
        confidence_threshold = float(merged.get("confidence_threshold", 0.7))
        strict_mode = bool(merged.get("strict_mode", True))

        # Build verifier agent
        verifier = await _build_verifier_agent(config, verifier_key, temperature, max_tokens)
        if verifier is None:
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

        # Determine what context to pass (data, not instructions)
        context_strategy = None
        if target_agent_cfg is not None and getattr(target_agent_cfg, "verification_context", None):
            context_strategy = target_agent_cfg.verification_context

        # We keep this minimal: include no conversation for last_turn; include none/placeholder
        # for full since we don't have a canonical conversation export here.
        # Consumers may extend to inject richer context via factory in the future.
        context_text: Optional[str] = None
        if context_strategy == "full":
            # If the SDK context exposes a string, include it; otherwise, keep minimal
            try:
                ctx_str = str(getattr(ctx, "context", ""))
                if ctx_str and ctx_str != "{}":
                    context_text = ctx_str
            except Exception:
                context_text = None

        # Attach agent instructions to help the verifier understand intended behavior
        agent_instructions = getattr(agent, "instructions", None)
        print(f"Agent instructions for verifier:\n{agent_instructions}")
        print(f"Context for verifier:\n{getattr(ctx, "context", None)}")
        verifier_input = _compose_verification_input(
            output_text=output,
            agent_instructions=agent_instructions,
            context_text=context_text,
        )

        # Run verifier
        result = await Runner.run(
            starting_agent=verifier,
            input=verifier_input,
            context=getattr(ctx, "context", None),
        )

        # Extract structured verification output
        verification: Optional[HallucinationCheckOutput] = None
        try:
            if hasattr(result, "final_output") and isinstance(result.final_output, HallucinationCheckOutput):
                verification = result.final_output
                print(verification)
        except Exception:
            verification = None

        if verification is None:
            # Fallback: try to coerce from dict-like
            try:
                value = getattr(result, "final_output", None) or getattr(result, "output", None)
                if isinstance(value, dict):
                    verification = HallucinationCheckOutput(**value)
                elif isinstance(value, str):
                    # Very lightweight JSON attempt
                    import json
                    data = json.loads(value)
                    if isinstance(data, dict):
                        verification = HallucinationCheckOutput(**data)
            except Exception:
                verification = None

        if verification is None:
            # Cannot interpret output -> do not trip
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

        # Decide whether to trip the guardrail
        if strict_mode:
            trip = bool(verification.has_hallucination)
        else:
            trip = bool(verification.has_hallucination and float(verification.confidence or 0.0) >= confidence_threshold)

        return GuardrailFunctionOutput(output_info=verification, tripwire_triggered=trip)

    except Exception as e:
        logger.error(f"hallucination_guardrail failed: {e}")
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

