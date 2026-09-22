"""Policy-driven semantic gate for agent-mediated calls.

The operator owns both rules and classifier prompts in YAML. The runtime makes
no guesses from tool names and has no inventory of action types. Fail-closed in
``enforce``. Not an OS sandbox.
"""

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from core.decisions import DecisionsModel
from schemas.action_policy import ActionPolicyConfig, ActionValidatorConfig
from utils.exceptions import ConfigError

logger = logging.getLogger("grid.action_policy")

VERDICTS = ("allow", "deny", "review")


@dataclass
class ActionRunState:
    """One trusted task and the chain of calls made under it.

    Shared by the agents of a run, so budgets and the chain are global to the
    task rather than per agent. Never constructed from tool arguments.
    """

    task: str
    run_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: int = 0
    denials: int = 0
    stopped: bool = False
    events: deque = field(default_factory=lambda: deque(maxlen=20))
    chain: deque = field(default_factory=lambda: deque(maxlen=200))
    # Reasoning belongs to this run. A compatibility callback is retained for
    # callers that supply their own per-run source, but factories should append
    # directly to ``reasoning_text`` so concurrent runs cannot see each other.
    reasoning_text: str = ""
    reasoning: Optional[Callable[[], str]] = None
    policy_event: Optional[Callable[[dict], None]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PolicyDenied(Exception):
    """An operator rule blocked a call."""

    def __init__(self, rule: str, approval_id: str | None = None):
        super().__init__(rule)
        self.rule = rule
        self.approval_id = approval_id


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    run_id: str
    action_sha256: str
    tool: str
    kind: str
    created_at: float
    expires_at: float


class ActionValidator:
    def __init__(
        self,
        config: ActionValidatorConfig,
        model: DecisionsModel,
        prompts,
        transport=None,
    ):
        self.config = config
        self.model = model
        self.prompts = prompts
        self.transport = transport

    @classmethod
    def from_config(cls, root_config, *, transport=None):
        policy = root_config.config.settings.action_policy
        config = policy.validator
        if not config.model:
            raise ConfigError(
                "settings.action_policy.validator.model must reference a key from models"
            )
        for name in ("action", "chain"):
            question = getattr(policy.prompts, name)
            if not question.instructions.strip() or set(question.criteria) != set(
                VERDICTS
            ):
                raise ConfigError(
                    f"settings.action_policy.prompts.{name} must define instructions "
                    "and allow/deny/review criteria"
                )
        return cls(
            config,
            DecisionsModel.from_config(root_config, config.model),
            policy.prompts,
            transport=transport,
        )

    def questions(self, chain: bool) -> dict:
        action = self.prompts.action
        questions = {
            "action": {
                "type": "choice",
                "instructions": action.instructions,
                "criteria": dict(action.criteria),
            }
        }
        if chain:
            chain_prompt = self.prompts.chain
            questions["chain"] = {
                "type": "choice",
                "instructions": chain_prompt.instructions,
                "criteria": dict(chain_prompt.criteria),
            }
        return questions

    def _verdict(self, answer: Any) -> str:
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ValueError("Invalid answer type")
        choice = answer["choice"]
        probabilities = answer["probabilities"]
        confidence = answer["confidence"]
        if choice not in VERDICTS or set(probabilities) != set(VERDICTS):
            raise ValueError("Invalid answer options")
        values = [confidence, *probabilities.values()]
        if any(
            type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1
            for v in values
        ):
            raise ValueError("Invalid answer probabilities")
        if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.001):
            raise ValueError("Invalid probability sum")
        if probabilities[choice] < max(probabilities.values()):
            raise ValueError("Inconsistent choice")
        return choice

    async def evaluate(self, state: dict, *, chain: bool = True) -> dict:
        """Return a verdict per question; a missing or malformed answer raises."""
        questions = self.questions(chain)
        async with self.model.http_client(
            timeout=min(self.config.timeout_seconds, self.model.timeout),
            transport=self.transport,
        ) as client:
            answers = await self.model.evaluate(client, state, questions)
        return {key: self._verdict(answers[key]) for key in questions}


def merge(verdicts: dict) -> str:
    """The strictest verdict wins; a call executes only when nothing objects."""
    values = set(verdicts.values())
    if "deny" in values:
        return "deny"
    if "review" in values or not values:
        return "review"
    return "allow"


class ActionGate:
    def __init__(self, config: ActionPolicyConfig, *, validator=None):
        self.config = config.model_copy(deep=True)
        self.validator = validator
        self._reviews: dict[str, PendingApproval] = {}
        self._approved: dict[tuple[str, str], float] = {}
        self._review_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.config.mode != "off"

    def mediates(self, kind: str) -> bool:
        return self.enabled and kind in self.config.kinds

    def _prune_reviews(self, now: float) -> None:
        self._reviews = {
            key: review
            for key, review in self._reviews.items()
            if review.expires_at > now
        }
        self._approved = {
            key: expiry for key, expiry in self._approved.items() if expiry > now
        }

    def _request_review(
        self, run: ActionRunState, tool: str, kind: str, digest: str
    ) -> str:
        now = time.time()
        with self._review_lock:
            self._prune_reviews(now)
            for review in self._reviews.values():
                if review.run_id == run.run_id and review.action_sha256 == digest:
                    return review.approval_id
            if len(self._reviews) >= self.config.max_pending_reviews:
                oldest = min(self._reviews.values(), key=lambda item: item.created_at)
                self._reviews.pop(oldest.approval_id, None)
            approval_id = uuid4().hex
            self._reviews[approval_id] = PendingApproval(
                approval_id=approval_id,
                run_id=run.run_id,
                action_sha256=digest,
                tool=tool,
                kind=kind,
                created_at=now,
                expires_at=now + self.config.review_ttl_seconds,
            )
            return approval_id

    def pending_reviews(self) -> list[dict]:
        """Return argument-free review metadata for a trusted host UI."""
        now = time.time()
        with self._review_lock:
            self._prune_reviews(now)
            return [
                {
                    "approval_id": item.approval_id,
                    "run_id": item.run_id,
                    "action_sha256": item.action_sha256,
                    "tool": item.tool,
                    "kind": item.kind,
                    "created_at": item.created_at,
                    "expires_at": item.expires_at,
                }
                for item in sorted(
                    self._reviews.values(), key=lambda review: review.created_at
                )
            ]

    def resolve_review(self, approval_id: str, *, approve: bool) -> bool:
        """Resolve a pending review; approval is one-shot and action-bound."""
        now = time.time()
        with self._review_lock:
            self._prune_reviews(now)
            review = self._reviews.pop(approval_id, None)
            if review is None:
                return False
            if approve:
                self._approved[(review.run_id, review.action_sha256)] = (
                    review.expires_at
                )
            return True

    def _consume_approval(self, run_id: str, digest: str) -> bool:
        now = time.time()
        with self._review_lock:
            self._prune_reviews(now)
            return self._approved.pop((run_id, digest), None) is not None

    @staticmethod
    def _as_data(raw: Any) -> Any:
        """Tool input as data for the packet, whatever shape the model produced."""
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return raw
        try:
            return json.loads(json.dumps(raw))
        except (TypeError, ValueError):
            return repr(raw)

    def _fit(self, items: list, budget: int) -> list:
        """Keep the newest entries that fit the declared byte budget."""
        if budget <= 0:
            return []
        while items:
            encoded = len(json.dumps(items, ensure_ascii=True, default=str).encode())
            if encoded <= budget:
                break
            items = items[1:]
        return items

    @staticmethod
    def _redacted(value: Any) -> dict:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        return {
            "redacted": True,
            "type": type(value).__name__,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _project(self, value: Any, sensitive_fields: set[str]) -> Any:
        """Build the bounded, secret-minimizing view sent to the validator."""
        if isinstance(value, dict):
            projected = {}
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                field_parts = set(normalized.split("_"))
                if normalized in sensitive_fields or field_parts & sensitive_fields:
                    projected[key] = self._redacted(item)
                else:
                    projected[key] = self._project(item, sensitive_fields)
            return projected
        if isinstance(value, list):
            return [self._project(item, sensitive_fields) for item in value]
        if (
            isinstance(value, str)
            and len(value.encode("utf-8")) > self.config.max_argument_value_bytes
        ):
            return self._redacted(value)
        return value

    @staticmethod
    def describe_tool(tool: Any, tool_name: str, kind: str) -> dict:
        """Expose tool metadata to the classifier without interpreting it."""
        metadata: dict[str, Any] = {}
        description = getattr(tool, "description", None)
        if description:
            metadata["description"] = str(description)
        schema = getattr(tool, "params_json_schema", None) or getattr(
            tool, "input_schema", None
        )
        if isinstance(schema, dict):
            metadata["input_schema"] = schema
        annotations = getattr(tool, "annotations", None)
        if annotations is not None:
            if hasattr(annotations, "model_dump"):
                annotations = annotations.model_dump(exclude_none=True)
            elif not isinstance(annotations, dict):
                annotations = {
                    key: getattr(annotations, key)
                    for key in (
                        "title",
                        "readOnlyHint",
                        "destructiveHint",
                        "idempotentHint",
                        "openWorldHint",
                    )
                    if getattr(annotations, key, None) is not None
                }
            if isinstance(annotations, dict):
                metadata["annotations"] = annotations
        return metadata

    def _chain(self, run: ActionRunState) -> dict:
        # Only actions that actually ran belong to the execution trajectory.
        # Feeding blocked/reviewed attempts back into the semantic chain makes
        # one uncertain verdict self-reinforcing: the next check sees the old
        # review and repeats it even though no side effect occurred.
        executed_chain = [
            event for event in run.chain if event.get("outcome") == "executed"
        ]
        window = executed_chain[-self.config.max_chain_events :]
        executed = self._fit(window, self.config.max_chain_bytes)
        block: dict = {"executed": executed}
        dropped = len(executed_chain) - len(executed)
        if dropped > 0:
            block["dropped_earlier_calls"] = dropped
        reasoning = run.reasoning_text
        if not reasoning and run.reasoning is not None:
            try:
                reasoning = run.reasoning() or ""
            except Exception:
                reasoning = ""
        if reasoning and self.config.max_reasoning_bytes:
            tail = reasoning.encode("utf-8", "ignore")[
                -self.config.max_reasoning_bytes :
            ]
            block["reasoning_tail"] = tail.decode("utf-8", "ignore")
        return block

    def _record(self, run, tool, kind, digest, decision, rule, **extra):
        event = {
            "run_id": run.run_id,
            "tool": tool,
            "kind": kind,
            "action_sha256": digest,
            "policy_version": self.config.version,
            "mode": self.config.mode,
            "decision": decision,
            "rule": rule,
            **extra,
        }
        run.events.append(event)
        if run.policy_event is not None:
            try:
                run.policy_event(dict(event))
            except Exception:
                logger.debug("Action-policy event sink failed", exc_info=True)
        # Never log arguments, tool output, credentials or provider error bodies.
        logger.info("ACTION_POLICY %s", json.dumps(event, ensure_ascii=True))

    def _deny(self, run, tool, kind, digest, rule, approval_id=None):
        # A pending human review is neither approval nor denial. Attempts still
        # bound repeated retries, while the denial budget remains meaningful.
        if rule != "policy_review":
            run.denials += 1
            if run.denials >= self.config.max_denials_per_run:
                run.stopped = True
        run.chain.append(
            {"kind": kind, "tool": tool, "outcome": "blocked", "rule": rule}
        )
        self._record(run, tool, kind, digest, "deny", rule)
        payload = {
            "status": "blocked",
            "rule": rule,
            "run_stopped": run.stopped,
            "next_step": "Revise the action within the task and policy; review requires the host.",
        }
        if approval_id is not None:
            payload["approval_id"] = approval_id
        return json.dumps(payload)

    async def invoke(
        self,
        tool_name: str,
        kind: str,
        ctx: Any,
        raw_args: Any,
        invoke: Callable[[Any, Any], Awaitable[Any]],
        descriptor: dict | None = None,
    ):
        """Judge one call, then run it. ``invoke`` receives the original input."""
        if not self.mediates(kind):
            return await invoke(ctx, raw_args)
        raw_ctx = getattr(ctx, "context", None)
        run = getattr(raw_ctx, "action_state", None)
        if not isinstance(run, ActionRunState):
            # A call outside a trusted task carries no authorization to act.
            return self._deny(
                ActionRunState(task=""),
                tool_name,
                kind,
                "unparsed",
                "missing_trusted_task",
            )
        digest = "unparsed"
        try:
            async with run.lock:
                if run.stopped:
                    raise PolicyDenied("run_stopped")
                run.attempts += 1
                if run.attempts > self.config.max_attempts_per_run:
                    run.stopped = True
                    raise PolicyDenied("attempt_limit")
                if not isinstance(run.task, str) or not run.task.strip():
                    raise PolicyDenied("missing_trusted_task")
                arguments = self._as_data(raw_args)
                canonical_action = {
                    "kind": kind,
                    "tool": tool_name,
                    "arguments": arguments,
                }
                serialized = json.dumps(
                    canonical_action, sort_keys=True, ensure_ascii=True, default=str
                )
                digest = hashlib.sha256(serialized.encode()).hexdigest()
                if len(serialized.encode()) > self.config.max_action_bytes:
                    raise PolicyDenied("action_size_limit")
                if (
                    kind == "agent"
                    and self.config.max_delegation_depth is not None
                    and (getattr(raw_ctx, "action_depth", 0) or 0)
                    >= self.config.max_delegation_depth
                ):
                    raise PolicyDenied("delegation_depth")
                sensitive_fields = {
                    field.casefold().replace("-", "_")
                    for field in self.config.sensitive_fields
                }
                projected_arguments = self._project(arguments, sensitive_fields)
                packet = {
                    "trusted_task": run.task,
                    "trusted_policy": {
                        "version": self.config.version,
                        "rules": [rule.model_dump() for rule in self.config.rules],
                    },
                    "untrusted_action": {
                        "kind": kind,
                        "tool": tool_name,
                        "arguments": projected_arguments,
                        "tool_metadata": descriptor or {},
                    },
                    "untrusted_chain": self._chain(run),
                    "run": {
                        "attempt": run.attempts,
                        "denials": run.denials,
                    },
                }
            # Judged outside the lock: a mediated call may itself make mediated calls.
            validator_started = time.monotonic()
            try:
                verdicts = await asyncio.wait_for(
                    self.validator.evaluate(packet, chain=self.config.check_chain),
                    timeout=self.config.validator.timeout_seconds,
                )
                if not isinstance(verdicts, dict) or not verdicts:
                    raise ValueError("Invalid validator answer set")
                if any(v not in VERDICTS for v in verdicts.values()):
                    raise ValueError("Invalid validator verdict")
                verdict = merge(verdicts)
            except Exception:
                verdicts, verdict = {"action": "unavailable"}, "unavailable"
            decision_meta = {
                "source": "validator",
                "latency_ms": round((time.monotonic() - validator_started) * 1000, 3),
                "validator_model": getattr(
                    getattr(self.validator, "model", None), "model_name", None
                ),
            }
            if verdict == "review" and self._consume_approval(run.run_id, digest):
                verdicts = {**verdicts, "host_approval": "allow"}
                verdict = "allow"
            stop_after_shadow_call = False
            async with run.lock:
                self._record(
                    run,
                    tool_name,
                    kind,
                    digest,
                    verdict,
                    "policy_check",
                    **verdicts,
                    **decision_meta,
                )
                if run.stopped:
                    raise PolicyDenied("run_stopped")
                if verdict != "allow" and self.config.mode == "enforce":
                    approval_id = (
                        self._request_review(run, tool_name, kind, digest)
                        if verdict == "review"
                        else None
                    )
                    raise PolicyDenied("policy_" + verdict, approval_id)
                if verdict != "allow" and self.config.mode == "shadow":
                    run.denials += 1
                    stop_after_shadow_call = (
                        run.denials >= self.config.max_denials_per_run
                    )
            try:
                result = await invoke(ctx, raw_args)
            except BaseException:
                async with run.lock:
                    run.stopped = True
                    self._record(
                        run, tool_name, kind, digest, "stopped", "execution_interrupted"
                    )
                raise
            async with run.lock:
                if stop_after_shadow_call:
                    run.stopped = True
                run.chain.append(
                    {
                        "kind": kind,
                        "tool": tool_name,
                        "arguments": projected_arguments,
                        "outcome": "executed",
                        "verdict": verdict,
                    }
                )
                self._record(run, tool_name, kind, digest, "executed", "policy_check")
            return result
        except PolicyDenied as exc:
            async with run.lock:
                return self._deny(
                    run,
                    tool_name,
                    kind,
                    digest,
                    exc.rule,
                    approval_id=exc.approval_id,
                )
        except (ValueError, TypeError, KeyError, OSError):
            async with run.lock:
                return self._deny(run, tool_name, kind, digest, "invalid_call")
