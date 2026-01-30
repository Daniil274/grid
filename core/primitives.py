"""
Pipeline Primitives - Atomic operations for emergent agent coordination.

Instead of hardcoded patterns like "Debate" or "Consensus", we provide
atomic operations that agents combine dynamically:
- execute: Run a task with an agent
- critique: Critical analysis of content
- vote: Cast a vote from a perspective
- validate: Check for hallucinations/facts
- synthesize: Combine multiple inputs
- branch: Parallel execution with different approaches
- filter_noise: Remove low-information content

Agents (especially meta-orchestrators) combine these primitives
to build emergent workflows that adapt to the task at hand.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .blackboard import Blackboard, BlackboardEntry, EntryType

if TYPE_CHECKING:
    from .agent_factory import AgentFactory


@dataclass
class PrimitiveResult:
    """Result of a primitive operation."""
    primitive: str
    success: bool
    output: Any
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    execution_time_ms: int = 0
    agent_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CritiqueResult:
    """Result of a critique operation."""
    issues: List[str]
    strengths: List[str]
    suggestions: List[str]
    severity: str  # "none", "minor", "major", "critical"
    overall_score: float  # 0-1
    raw_output: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VoteResult:
    """Result of a vote operation."""
    vote: str  # "approve", "reject", "abstain"
    confidence: float
    reasoning: str
    perspective: str
    conditions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    """Result of a validation operation."""
    valid: bool
    issues: List[Dict[str, Any]]  # {type, description, severity}
    confidence: float
    checks_performed: List[str]
    raw_output: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Primitives:
    """
    Atomic operations for building agent pipelines.

    These primitives are designed to be combined by meta-orchestrators
    to create emergent workflows. Each primitive:
    - Has a single, clear purpose
    - Posts results to blackboard (optional)
    - Returns structured results
    - Can be composed with other primitives
    - Loads prompts from configuration (not hardcoded)
    """

    # Default prompts (fallback if not in config)
    DEFAULT_PROMPTS = {
        "execute_default": (
            "Ты исполнитель задач. Выполни задачу максимально качественно.\n"
            "Дай чёткий, структурированный результат."
        ),
        "critique": (
            "Ты критический аналитик. Проведи детальный анализ и верни JSON:\n"
            '{"issues": [], "strengths": [], "suggestions": [], "severity": "none|minor|major|critical", "overall_score": 0.0-1.0}'
        ),
        "vote": (
            "Оцени предложение и проголосуй. Верни JSON:\n"
            '{"vote": "approve|reject|abstain", "confidence": 0.0-1.0, "reasoning": "", "conditions": []}'
        ),
        "validate": (
            "Ты валидатор контента. Проверь на галлюцинации, ошибки, противоречия.\n"
            "Верни JSON: {\"valid\": true|false, \"issues\": [], \"confidence\": 0.0-1.0, \"checks_performed\": []}"
        ),
        "synthesize": (
            "Ты синтезатор информации. Объедини несколько источников в единый связный результат."
        ),
        "filter_noise": (
            "Оцени информационную ценность каждого сообщения. Верни JSON:\n"
            '{"scores": [{"index": 0, "score": 0.0-1.0, "reason": ""}]}'
        ),
        "revise": (
            "Ты ревизор контента. Тебе предоставляется:\n"
            "1. Оригинальная задача\n"
            "2. Текущая версия (требующая улучшений)\n"
            "3. Обратная связь (критика, найденные проблемы)\n\n"
            "Твоя задача:\n"
            "- Исправить ВСЕ указанные проблемы\n"
            "- Сохранить всё хорошее из оригинала\n"
            "- Вернуть полную улучшенную версию\n\n"
            "Не объясняй что ты изменил — просто верни исправленную версию."
        ),
    }

    def __init__(
        self,
        factory: "AgentFactory",
        blackboard: Optional[Blackboard] = None,
        default_model_key: Optional[str] = None,
        prompts: Optional[Dict[str, str]] = None
    ):
        """
        Initialize primitives.

        Args:
            factory: AgentFactory for creating agents
            blackboard: Shared blackboard for posting results
            default_model_key: Default model to use for primitive agents
            prompts: Override prompts (usually loaded from config)
        """
        self.factory = factory
        self.blackboard = blackboard
        self.default_model_key = default_model_key
        self._prompts = prompts or {}

        # Try to load prompts from factory config
        self._load_prompts_from_config()

    def _load_prompts_from_config(self) -> None:
        """Load prompts from factory configuration."""
        try:
            if hasattr(self.factory, 'config') and self.factory.config:
                config = self.factory.config
                if hasattr(config, 'config') and hasattr(config.config, 'dict'):
                    raw_config = config.config.dict() if hasattr(config.config, 'dict') else {}
                    si_prompts = raw_config.get('si_prompts', {})
                    if si_prompts:
                        self._prompts.update(si_prompts)
        except Exception:
            pass  # Use defaults if loading fails

    def get_prompt(self, key: str) -> str:
        """Get a prompt by key, falling back to defaults."""
        return self._prompts.get(key) or self.DEFAULT_PROMPTS.get(key, "")

    def _inject_blackboard_context(
        self,
        base_instructions: str,
        agent_name: str,
        include_types: Optional[List[str]] = None,
        max_entries: int = 10
    ) -> str:
        """
        Inject blackboard context into agent instructions.

        This is critical for agents to see what other agents have discovered.
        Since Agent instructions are immutable after creation (OpenAI SDK limitation),
        we must build the complete instructions string BEFORE calling create_dynamic_agent().

        Args:
            base_instructions: Base system prompt
            agent_name: Name of the agent (for context retrieval)
            include_types: Optional filter for entry types
            max_entries: Maximum blackboard entries to include

        Returns:
            Enhanced instructions with blackboard context prepended
        """
        if not self.blackboard:
            return base_instructions

        bb_context = self.blackboard.get_context_for_agent(
            agent_name=agent_name,
            max_entries=max_entries,
            include_types=include_types,
            exclude_self=True
        )

        # Don't inject if blackboard is empty
        if not bb_context or "Пусто" in bb_context:
            return base_instructions

        return f"{bb_context}\n\n--- ТВОЯ ЗАДАЧА ---\n{base_instructions}"

    async def execute(
        self,
        task: str,
        system_prompt: Optional[str] = None,
        tools: Optional[List[str]] = None,
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True,
        tags: Optional[List[str]] = None
    ) -> PrimitiveResult:
        """
        Execute a task with a dynamically created agent.

        Args:
            task: The task to execute
            system_prompt: System prompt for the agent
            tools: List of tools to give the agent
            model_key: Model to use (or default)
            post_to_blackboard: Whether to post result to blackboard
            tags: Tags for blackboard entry

        Returns:
            PrimitiveResult with the execution output
        """
        start_time = datetime.now()

        default_prompt = self.get_prompt("execute_default")

        agent_name = f"executor-{uuid.uuid4().hex[:6]}"

        try:
            # Inject blackboard context before creating agent
            base_prompt = system_prompt or default_prompt
            effective_prompt = self._inject_blackboard_context(
                base_prompt,
                agent_name,
                include_types=None,  # All types relevant for executors
                max_entries=10
            )

            agent = await self.factory.create_dynamic_agent(
                name=agent_name,
                instructions=effective_prompt,
                model_key=model_key or self.default_model_key,
                tool_names=tools or []
            )

            output = await self.factory.run_agent_object_simple(agent, task)

            result = PrimitiveResult(
                primitive="execute",
                success=True,
                output=output,
                confidence=1.0,
                agent_name=agent_name,
                execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000)
            )

            if post_to_blackboard and self.blackboard:
                self.blackboard.post(
                    entry_type=EntryType.ARTIFACT,
                    author=agent_name,
                    content=output,
                    tags=tags or ["execute"]
                )

            return result

        except Exception as e:
            return PrimitiveResult(
                primitive="execute",
                success=False,
                output=None,
                error=str(e),
                agent_name=agent_name,
                execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000)
            )

    async def critique(
        self,
        content: str,
        criteria: Optional[str] = None,
        perspective: Optional[str] = None,
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True,
        tools: Optional[List[str]] = None
    ) -> CritiqueResult:
        """
        Perform critical analysis of content.

        Args:
            content: Content to critique
            criteria: Specific criteria to evaluate against
            perspective: Perspective to critique from (e.g., "security", "performance")
            model_key: Model to use
            post_to_blackboard: Whether to post to blackboard

        Returns:
            CritiqueResult with issues, strengths, and suggestions
        """
        critic_name = f"critic-{uuid.uuid4().hex[:6]}"

        perspective_str = f"с позиции {perspective}" if perspective else ""
        criteria_str = f"\n\nКРИТЕРИИ ОЦЕНКИ:\n{criteria}" if criteria else ""

        base_prompt = self.get_prompt("critique")
        prompt = f"{base_prompt}\n\nПерспектива: {perspective_str}{criteria_str}"

        # Inject blackboard context
        effective_prompt = self._inject_blackboard_context(
            prompt,
            critic_name,
            include_types=[EntryType.ARTIFACT, EntryType.CRITIQUE, EntryType.FACT],
            max_entries=5
        )

        try:
            agent = await self.factory.create_dynamic_agent(
                name=critic_name,
                instructions=effective_prompt,
                model_key=model_key or self.default_model_key,
                tool_names=tools or []
            )

            raw_output = await self.factory.run_agent_object_simple(
                agent, f"КОНТЕНТ ДЛЯ АНАЛИЗА:\n{content}"
            )

            # Parse JSON from output
            parsed = self._parse_json(raw_output)

            result = CritiqueResult(
                issues=parsed.get("issues", []),
                strengths=parsed.get("strengths", []),
                suggestions=parsed.get("suggestions", []),
                severity=parsed.get("severity", "minor"),
                overall_score=float(parsed.get("overall_score", 0.5)),
                raw_output=raw_output
            )

            if post_to_blackboard and self.blackboard:
                self.blackboard.post(
                    entry_type=EntryType.CRITIQUE,
                    author=critic_name,
                    content=result.to_dict(),
                    confidence=result.overall_score,
                    tags=["critique", perspective or "general"]
                )

            return result

        except Exception as e:
            return CritiqueResult(
                issues=[f"Ошибка критики: {e}"],
                strengths=[],
                suggestions=[],
                severity="critical",
                overall_score=0.0,
                raw_output=str(e)
            )

    async def vote(
        self,
        proposal: str,
        perspective: str,
        context: Optional[str] = None,
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True
    ) -> VoteResult:
        """
        Cast a vote on a proposal from a specific perspective.

        Args:
            proposal: The proposal to vote on
            perspective: The perspective to vote from (e.g., "security expert", "UX designer")
            context: Additional context for the vote
            model_key: Model to use
            post_to_blackboard: Whether to post to blackboard

        Returns:
            VoteResult with vote, reasoning, and conditions
        """
        voter_name = f"voter-{perspective[:10]}-{uuid.uuid4().hex[:4]}"

        context_str = f"\n\nКОНТЕКСТ:\n{context}" if context else ""

        base_prompt = self.get_prompt("vote")
        prompt = f"Ты эксперт с позицией: {perspective}\n\n{base_prompt}{context_str}"

        # Inject blackboard context
        effective_prompt = self._inject_blackboard_context(
            prompt,
            voter_name,
            include_types=[EntryType.VOTE, EntryType.ARTIFACT, EntryType.CRITIQUE],
            max_entries=10
        )

        try:
            agent = await self.factory.create_dynamic_agent(
                name=voter_name,
                instructions=effective_prompt,
                model_key=model_key or self.default_model_key,
                tool_names=[]
            )

            raw_output = await self.factory.run_agent_object_simple(
                agent, f"ПРЕДЛОЖЕНИЕ:\n{proposal}"
            )

            parsed = self._parse_json(raw_output)

            result = VoteResult(
                vote=parsed.get("vote", "abstain").lower(),
                confidence=float(parsed.get("confidence", 0.5)),
                reasoning=parsed.get("reasoning", ""),
                perspective=perspective,
                conditions=parsed.get("conditions", [])
            )

            if post_to_blackboard and self.blackboard:
                self.blackboard.post(
                    entry_type=EntryType.VOTE,
                    author=voter_name,
                    content=result.to_dict(),
                    confidence=result.confidence,
                    tags=["vote", perspective]
                )

            return result

        except Exception as e:
            return VoteResult(
                vote="abstain",
                confidence=0.0,
                reasoning=f"Ошибка голосования: {e}",
                perspective=perspective
            )

    async def validate(
        self,
        content: str,
        context: Optional[str] = None,
        checks: Optional[List[str]] = None,
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True,
        tools: Optional[List[str]] = None
    ) -> ValidationResult:
        """
        Validate content for hallucinations, factual errors, and consistency.

        Args:
            content: Content to validate
            context: Context to validate against
            checks: Specific checks to perform (default: all)
            model_key: Model to use
            post_to_blackboard: Whether to post to blackboard

        Returns:
            ValidationResult with validity status and issues
        """
        validator_name = f"validator-{uuid.uuid4().hex[:6]}"

        default_checks = [
            "hallucination_detection",
            "factual_consistency",
            "logical_coherence",
            "context_alignment"
        ]
        checks_to_run = checks or default_checks

        context_str = f"\n\nКОНТЕКСТ ДЛЯ ПРОВЕРКИ:\n{context}" if context else ""

        base_prompt = self.get_prompt("validate")
        prompt = f"{base_prompt}\n\nПроверки: {', '.join(checks_to_run)}{context_str}"

        # Inject blackboard context
        effective_prompt = self._inject_blackboard_context(
            prompt,
            validator_name,
            include_types=[EntryType.ARTIFACT, EntryType.HYPOTHESIS, EntryType.FACT],
            max_entries=8
        )

        try:
            agent = await self.factory.create_dynamic_agent(
                name=validator_name,
                instructions=effective_prompt,
                model_key=model_key or self.default_model_key,
                tool_names=tools or []
            )

            raw_output = await self.factory.run_agent_object_simple(
                agent, f"КОНТЕНТ:\n{content}"
            )

            parsed = self._parse_json(raw_output)

            result = ValidationResult(
                valid=parsed.get("valid", True),
                issues=parsed.get("issues", []),
                confidence=float(parsed.get("confidence", 0.8)),
                checks_performed=parsed.get("checks_performed", checks_to_run),
                raw_output=raw_output
            )

            if post_to_blackboard and self.blackboard:
                entry_type = EntryType.FACT if result.valid else EntryType.SIGNAL
                self.blackboard.post(
                    entry_type=entry_type,
                    author=validator_name,
                    content=result.to_dict(),
                    confidence=result.confidence,
                    tags=["validation", "valid" if result.valid else "invalid"]
                )

            return result

        except Exception as e:
            return ValidationResult(
                valid=False,
                issues=[{"type": "error", "description": str(e), "severity": "high"}],
                confidence=0.0,
                checks_performed=[],
                raw_output=str(e)
            )

    async def synthesize(
        self,
        inputs: List[str],
        goal: Optional[str] = None,
        style: str = "comprehensive",
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True
    ) -> PrimitiveResult:
        """
        Synthesize multiple inputs into a coherent output.

        Args:
            inputs: List of inputs to synthesize
            goal: Goal of the synthesis
            style: "comprehensive", "concise", "structured"
            model_key: Model to use
            post_to_blackboard: Whether to post to blackboard

        Returns:
            PrimitiveResult with synthesized output
        """
        synthesizer_name = f"synthesizer-{uuid.uuid4().hex[:6]}"

        style_instructions = {
            "comprehensive": "Дай полный, детальный синтез, сохраняя все важные детали.",
            "concise": "Дай краткий синтез, только ключевые моменты.",
            "structured": "Дай структурированный синтез с разделами и пунктами."
        }

        goal_str = f"\n\nЦЕЛЬ СИНТЕЗА: {goal}" if goal else ""

        base_prompt = self.get_prompt("synthesize")
        prompt = f"{base_prompt}\n\n{style_instructions.get(style, style_instructions['comprehensive'])}{goal_str}"

        # Format inputs
        formatted_inputs = "\n\n---\n\n".join([
            f"ИСТОЧНИК {i+1}:\n{inp}" for i, inp in enumerate(inputs)
        ])

        # Inject blackboard context
        effective_prompt = self._inject_blackboard_context(
            prompt,
            synthesizer_name,
            include_types=None,
            max_entries=15
        )

        try:
            agent = await self.factory.create_dynamic_agent(
                name=synthesizer_name,
                instructions=effective_prompt,
                model_key=model_key or self.default_model_key,
                tool_names=[]
            )

            output = await self.factory.run_agent_object_simple(agent, formatted_inputs)

            result = PrimitiveResult(
                primitive="synthesize",
                success=True,
                output=output,
                confidence=0.9,
                agent_name=synthesizer_name,
                metadata={"num_inputs": len(inputs), "style": style}
            )

            if post_to_blackboard and self.blackboard:
                self.blackboard.post(
                    entry_type=EntryType.ARTIFACT,
                    author=synthesizer_name,
                    content=output,
                    tags=["synthesis", style]
                )

            return result

        except Exception as e:
            return PrimitiveResult(
                primitive="synthesize",
                success=False,
                output=None,
                error=str(e),
                agent_name=synthesizer_name
            )

    async def branch(
        self,
        task: str,
        perspectives: List[str],
        system_prompt: Optional[str] = None,
        tools: Optional[List[str]] = None,
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True
    ) -> List[PrimitiveResult]:
        """
        Execute task in parallel from multiple perspectives.

        Args:
            task: Task to execute
            perspectives: List of perspectives (e.g., ["security", "performance", "usability"])
            system_prompt: Base system prompt (perspective will be added)
            tools: Tools for agents
            model_key: Model to use
            post_to_blackboard: Whether to post to blackboard

        Returns:
            List of PrimitiveResults, one per perspective
        """
        base_prompt = system_prompt or "Выполни задачу качественно."

        async def run_branch(perspective: str) -> PrimitiveResult:
            branch_prompt = f"""{base_prompt}

ТВОЯ ПЕРСПЕКТИВА: {perspective}
Анализируй и выполняй задачу с этой точки зрения."""

            return await self.execute(
                task=task,
                system_prompt=branch_prompt,
                tools=tools,
                model_key=model_key,
                post_to_blackboard=post_to_blackboard,
                tags=["branch", perspective]
            )

        # Run all branches in parallel
        results = await asyncio.gather(*[
            run_branch(p) for p in perspectives
        ])

        return list(results)

    async def filter_noise(
        self,
        messages: List[str],
        min_information_gain: float = 0.3,
        model_key: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Filter out low-information messages.

        Args:
            messages: List of messages to filter
            min_information_gain: Minimum information gain threshold (0-1)
            model_key: Model to use

        Returns:
            List of dicts with message, score, and keep status
        """
        filter_name = f"filter-{uuid.uuid4().hex[:6]}"

        prompt = self.get_prompt("filter_noise")

        try:
            agent = await self.factory.create_dynamic_agent(
                name=filter_name,
                instructions=prompt,
                model_key=model_key or self.default_model_key,
                tool_names=[]
            )

            formatted = "\n\n".join([
                f"[{i}] {msg}" for i, msg in enumerate(messages)
            ])

            raw_output = await self.factory.run_agent_object_simple(agent, formatted)
            parsed = self._parse_json(raw_output)

            scores = parsed.get("scores", [])

            results = []
            for i, msg in enumerate(messages):
                score_entry = next((s for s in scores if s.get("index") == i), None)
                score = score_entry.get("score", 0.5) if score_entry else 0.5

                results.append({
                    "message": msg,
                    "score": score,
                    "keep": score >= min_information_gain,
                    "reason": score_entry.get("reason", "") if score_entry else ""
                })

            return results

        except Exception as e:
            # On error, keep all messages
            return [
                {"message": msg, "score": 1.0, "keep": True, "reason": f"Error: {e}"}
                for msg in messages
            ]

    async def revise(
        self,
        original_content: str,
        feedback: str,
        task: str,
        system_prompt: Optional[str] = None,
        tools: Optional[List[str]] = None,
        model_key: Optional[str] = None,
        post_to_blackboard: bool = True,
        tags: Optional[List[str]] = None
    ) -> PrimitiveResult:
        """
        Revise content based on critique/validation feedback.

        This is the key primitive for iterative refinement loops.
        The reviser receives: original content + feedback + original task,
        and produces an improved version.

        Args:
            original_content: The content to revise
            feedback: Critique or validation feedback
            task: The original task (for context)
            system_prompt: Optional custom prompt for the reviser
            tools: Tools for the reviser agent
            model_key: Model to use
            post_to_blackboard: Whether to post result to blackboard
            tags: Tags for blackboard entry

        Returns:
            PrimitiveResult with revised output
        """
        start_time = datetime.now()
        agent_name = f"reviser-{uuid.uuid4().hex[:6]}"

        default_prompt = self.get_prompt("revise")
        base_prompt = system_prompt or default_prompt

        # Inject blackboard context
        effective_prompt = self._inject_blackboard_context(
            base_prompt,
            agent_name,
            max_entries=10
        )

        try:
            agent = await self.factory.create_dynamic_agent(
                name=agent_name,
                instructions=effective_prompt,
                model_key=model_key or self.default_model_key,
                tool_names=tools or []
            )

            revision_task = (
                f"ORIGINAL TASK:\n{task}\n\n"
                f"CURRENT VERSION:\n{original_content}\n\n"
                f"FEEDBACK/ISSUES:\n{feedback}\n\n"
                "Produce an improved version that addresses all feedback."
            )

            output = await self.factory.run_agent_object_simple(agent, revision_task)

            result = PrimitiveResult(
                primitive="revise",
                success=True,
                output=output,
                confidence=0.85,
                agent_name=agent_name,
                execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000),
                metadata={"revision_of": "previous_step"}
            )

            if post_to_blackboard and self.blackboard:
                self.blackboard.post(
                    entry_type=EntryType.ARTIFACT,
                    author=agent_name,
                    content=output,
                    tags=tags or ["revise", "iteration"]
                )

            return result

        except Exception as e:
            return PrimitiveResult(
                primitive="revise",
                success=False,
                output=None,
                error=str(e),
                agent_name=agent_name,
                execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000)
            )

    def _parse_json(self, text: str) -> Dict[str, Any]:
        """Best-effort JSON parsing from LLM output."""
        if not text:
            return {}

        # Try to find JSON in the text
        text = text.strip()

        # Remove markdown code fences
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                # Remove first and last lines (fences)
                text = "\n".join(lines[1:-1]).strip()

        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in text
        import re
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return {}


# Helper function to create primitives with factory
def create_primitives(
    factory: "AgentFactory",
    blackboard: Optional[Blackboard] = None,
    default_model_key: Optional[str] = None
) -> Primitives:
    """
    Create a Primitives instance with the given factory.

    Args:
        factory: AgentFactory instance
        blackboard: Optional blackboard for shared memory
        default_model_key: Default model to use

    Returns:
        Configured Primitives instance
    """
    return Primitives(
        factory=factory,
        blackboard=blackboard,
        default_model_key=default_model_key
    )
