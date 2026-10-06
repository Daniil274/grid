"""Policy gate for agent-mediated calls.

Every mediated call is routed before it runs (core.action_routing): what the
tool declares it does, the paths it names and what its run has taken in decide,
under the filter the user picked, whether it runs, goes to the operator's
policy model, waits for the user, or is refused. Only the judged rest costs a
model request, and the question that model answers is about harm, not about
whether the agent needed the call.

A call held for review waits in the chat it came from while someone can answer
there (``ActionRunState.interactive``); otherwise it is refused with an
approval id the host can resolve. Fail-closed in ``enforce``. Not an OS sandbox.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional
from uuid import uuid4

import httpx

from core.action_routing import Routing, effect_of, route_call
from core.decisions import DecisionsModel
from core.chat_policy import ChatPolicyModel
from core.policy_resilience import PolicyRunner
from schemas.action_policy import ActionPolicyConfig, ActionValidatorConfig
from utils.exceptions import ConfigError
from utils.path_utils import _sandbox_root, resolve_agent_path

logger = logging.getLogger("grid.action_policy")

VERDICTS = ("allow", "deny", "review")

# Run states whose call is executing in the current task: a call it makes in
# turn runs inside it rather than queueing behind it.
_EXECUTING: ContextVar[tuple] = ContextVar("grid_action_policy_executing", default=())


@dataclass
class ActionRunState:
    """One trusted task and the calls made under it.

    Never constructed from tool arguments. A sub-agent runs under a delegated
    state (:meth:`delegate`): the same trusted task, history, event log and
    lock as its caller - so text the caller wrote for the sub-agent never
    becomes a user instruction - with an attempt budget of its own inside the
    turn's total. What belongs to the whole turn lives on the root: the user's
    filter, the facts its calls collected and the answers the user gave.
    """

    task: str
    run_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: int = 0
    denials: int = 0
    stopped: bool = False
    events: deque = field(default_factory=lambda: deque(maxlen=20))
    history: deque = field(default_factory=lambda: deque(maxlen=200))
    # Reasoning belongs to this run. A compatibility callback is retained for
    # callers that supply their own per-run source, but factories should append
    # directly to ``reasoning_text`` so concurrent runs cannot see each other.
    reasoning_text: str = ""
    reasoning: Optional[Callable[[], str]] = None
    policy_event: Optional[Callable[[dict], None]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set on a delegated state: the run that started it, and what that run
    # asked for. The request explains the purpose; it grants no authority.
    parent: Optional["ActionRunState"] = None
    delegation: Optional[dict] = None
    # The rest is the turn's, read and written on the root.
    # Calls made in the whole turn, delegated runs included.
    turn_attempts: int = 0
    # The user's filter (ActionPolicyConfig.filters); None is the default one.
    filter: Optional[str] = None
    # Someone watches the turn live and can answer a held call there.
    interactive: bool = False
    # The conversation the turn belongs to: an answer from its chat only.
    context_id: Optional[str] = None
    # What the turn's calls took in (core.action_routing SECRET, UNTRUSTED).
    flows: list = field(default_factory=list)
    # Tools the user allowed for the rest of the turn.
    grants: set = field(default_factory=set)
    # Completes when the latest call issued in this run, and every call before
    # it, is finished. See take_turn.
    order_tail: Optional[asyncio.Future] = None

    def take_turn(self) -> tuple[Optional[asyncio.Future], asyncio.Future]:
        """Queue a call behind the calls this run issued before it.

        The SDK starts every call of one model response at once, and their
        verdicts arrive in any order. An action followed by a look at its result
        (click, then perceive) must still run in the order the model wrote.
        """
        previous = self.order_tail
        own = asyncio.get_running_loop().create_future()
        self.order_tail = own
        return previous, own

    @staticmethod
    def pass_turn(previous: Optional[asyncio.Future], own: asyncio.Future) -> None:
        """Let later calls go once this one and all earlier ones are finished."""

        def release(_=None):
            if not own.done():
                own.set_result(None)

        if previous is None or previous.done():
            release()
        else:
            previous.add_done_callback(release)

    def lineage(self) -> Iterator["ActionRunState"]:
        """This run, then the runs that delegated to it, outermost last."""
        state: Optional[ActionRunState] = self
        while state is not None:
            yield state
            state = state.parent

    @property
    def root(self) -> "ActionRunState":
        return list(self.lineage())[-1]

    @property
    def depth(self) -> int:
        return len(list(self.lineage())) - 1

    @property
    def halted(self) -> bool:
        """Stopped itself, or inside a run that was stopped."""
        return any(state.stopped for state in self.lineage())

    @property
    def flow_kinds(self) -> set:
        return {flow["fact"] for flow in self.root.flows}

    def delegate(self, tool: str, request: Any) -> "ActionRunState":
        """A state for a sub-agent this run starts through ``tool``."""
        return ActionRunState(
            task=self.task,
            run_id=self.run_id,
            events=self.events,
            history=self.history,
            policy_event=self.policy_event,
            lock=self.lock,
            parent=self,
            delegation={"tool": tool, "request": request},
        )


def delegated_state(parent: Any, tool: str, request: Any) -> Any:
    """A sub-agent's policy state: its caller's trusted task and trajectory.

    What the caller asked for travels as untrusted context, so a sub-agent can
    never be authorized by text an agent wrote. Without a policy state (the
    gate is off) there is nothing to delegate.
    """
    if isinstance(parent, ActionRunState):
        return parent.delegate(tool, request)
    return parent


def is_policy_block(result: Any) -> bool:
    """True when a tool result is the gate's own ``blocked`` answer."""
    if not isinstance(result, str) or '"blocked"' not in result:
        return False
    try:
        payload = json.loads(result)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "blocked"


class PolicyDenied(Exception):
    """An operator rule, the user's filter or the user blocked a call."""

    def __init__(
        self,
        rule: str,
        approval_id: str | None = None,
        verdicts: dict | None = None,
    ):
        super().__init__(rule)
        self.rule = rule
        self.approval_id = approval_id
        self.verdicts = verdicts


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    run_id: str
    action_sha256: str
    tool: str
    kind: str
    created_at: float
    expires_at: float
    # The conversation it came from: the user answers it there only.
    context_id: Optional[str] = None
    # Why it is held, argument-free (core.action_routing Routing.reasons).
    reasons: tuple[str, ...] = ()


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
        self.fallbacks: list[ActionValidator] = []
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_config(cls, root_config, *, policy=None, transport=None):
        """Validators on ``root_config``'s models, asking ``policy``'s question.

        ``policy`` defaults to the config's own; a factory passes the base policy
        with its system's part added (ActionPolicyConfig.with_system).
        """
        policy = policy or root_config.config.settings.action_policy
        config = policy.validator
        if not config.model:
            raise ConfigError(
                "settings.action_policy.validator.model must reference a key from models"
            )
        question = policy.prompts.action
        if not question.instructions.strip() or set(question.criteria) != set(VERDICTS):
            raise ConfigError(
                "settings.action_policy.prompts.action must define instructions "
                "and allow/deny/review criteria"
            )

        def build(key, route_config):
            chat = root_config.get_model(key).policy_api == "chat"
            validator_type = ChatActionValidator if chat else cls
            model_type = ChatPolicyModel if chat else DecisionsModel
            return validator_type(
                route_config,
                model_type.from_config(root_config, key),
                policy.prompts,
                transport=transport,
            )

        # Tried in this order: the first model, then each next one only when
        # those before it are unavailable (core.policy_resilience).
        primary, *reserves = [
            build(key, config.model_copy(update={"model": key, "fallback_models": ()}))
            for key in config.routes
        ]
        primary.fallbacks = reserves
        return primary

    def questions(self) -> dict:
        action = self.prompts.action
        return {
            "action": {
                "type": "choice",
                "instructions": action.instructions,
                "criteria": dict(action.criteria),
            }
        }

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

    async def evaluate(self, state: dict) -> dict:
        """Return the verdict per question; a missing or malformed answer raises."""
        questions = self.questions()
        # One request may take the model's request timeout, never the whole
        # budget: a stalled request is dropped and the gate tries again.
        timeout = min(self.config.timeout_seconds, self.model.timeout)
        if self._client is None:
            self._client = self.model.http_client(
                timeout=timeout, transport=self.transport
            )
        answers = await asyncio.wait_for(
            self.model.evaluate(self._client, state, questions), timeout=timeout
        )
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("Invalid validator answer set")
        return {key: self._verdict(answers[key]) for key in questions}

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        for validator in self.fallbacks:
            await validator.aclose()


class ChatActionValidator(ActionValidator):
    """Same question and lifetime, with a direct verdict instead of probabilities."""

    def _verdict(self, answer: Any) -> str:
        if not isinstance(answer, str) or answer not in VERDICTS:
            raise ValueError("Invalid chat policy verdict")
        return answer


#: What a blocked call that leaves the run going tells the agent. Probing the
#: gate - the same target by another tool, path or wording - is what the user
#: would have to answer again; ordinary work on the task is not.
_KEEP_WORKING = (
    "Do not repeat it or reach the same target another way: another tool, path "
    "or wording for it is the same attempt. Your other calls are judged on their "
    "own, so go on with the work the task needs and name the blocked step in "
    "your final report."
)


def _next_step_after_block(halted: bool, rule: str, label: str = "") -> str:
    """What the agent should do after a blocked call, from why it was blocked."""
    if halted:
        return (
            "This call did not run, and the run has used up its blocked calls: "
            "no further tool calls will run. Write your final report from what "
            "you have and name the blocked steps."
        )
    if rule == "policy_filter":
        return (
            f"This call did not run: the user's policy filter ({label}) does not "
            "allow this kind of action. " + _KEEP_WORKING + " Say which step needs "
            "another filter; the user can switch it and ask again."
        )
    if rule == "declined":
        return "The user (or the operator) declined this call. " + _KEEP_WORKING
    if rule == "review_timeout":
        return (
            "This call did not run: it waited for the user's permission and "
            "nobody answered in time. " + _KEEP_WORKING
        )
    if rule == "policy_review":
        return (
            "This call is held for approval and did not run. " + _KEEP_WORKING
        )
    if rule == "policy_deny":
        return (
            "This call did not run: the policy judged it harmful or not "
            "authorized by the user. " + _KEEP_WORKING
        )
    return "This call did not run. " + _KEEP_WORKING


class ActionGate:
    def __init__(self, config: ActionPolicyConfig, *, validator=None):
        self.config = config.model_copy(deep=True)
        self.validator = validator
        self._runner = PolicyRunner(
            self.config.validator,
            [validator, *getattr(validator, "fallbacks", ())],
        )
        self._reviews: dict[str, PendingApproval] = {}
        self._approved: dict[tuple[str, str], float] = {}
        # Held calls someone can answer live: the answer, and the run it grants to.
        self._waiters: dict[str, tuple[asyncio.Future, ActionRunState]] = {}
        self._review_lock = threading.Lock()

    async def aclose(self):
        if self.validator is not None and hasattr(self.validator, "aclose"):
            await self.validator.aclose()

    @property
    def enabled(self) -> bool:
        return self.config.mode != "off"

    def mediates(self, kind: str) -> bool:
        return self.enabled and kind in self.config.kinds

    def filters(self) -> dict:
        """The user's switch, argument-free: every filter and the default one."""
        return {
            "default": self.config.default_filter,
            "approvals": self.config.approvals,
            "filters": [
                {"key": key, "label": item.label, "description": item.description}
                for key, item in self.config.filters.items()
            ],
        }

    # -- reviews -------------------------------------------------------------
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
        self,
        run: ActionRunState,
        tool: str,
        kind: str,
        digest: str,
        reasons: tuple[str, ...] = (),
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
                context_id=run.root.context_id,
                reasons=tuple(reasons),
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
                    "context_id": item.context_id,
                    "reasons": list(item.reasons),
                    "waiting": item.approval_id in self._waiters,
                }
                for item in sorted(
                    self._reviews.values(), key=lambda review: review.created_at
                )
            ]

    def resolve_review(
        self,
        approval_id: str,
        *,
        approve: bool,
        remember: bool = False,
        context_id: Optional[str] = None,
    ) -> bool:
        """Answer a held call: approval is one-shot and bound to the action.

        A call waiting in its chat runs (or is refused) at once; ``remember``
        also allows that tool for the rest of the turn. Otherwise an approval
        lets the agent's retry of the exact same call through. ``context_id``
        limits the answer to a review from that conversation.
        """
        now = time.time()
        with self._review_lock:
            self._prune_reviews(now)
            review = self._reviews.get(approval_id)
            if review is None or (context_id is not None and review.context_id != context_id):
                return False
            del self._reviews[approval_id]
            waiter = self._waiters.pop(approval_id, None)
            if waiter is None:
                if approve:
                    self._approved[(review.run_id, review.action_sha256)] = review.expires_at
                return True
        future, run = waiter
        if approve and remember:
            run.root.grants.add(review.tool)

        def answer():
            if not future.done():
                future.set_result(approve)

        future.get_loop().call_soon_threadsafe(answer)
        return True

    def cancel_reviews(self, run_id: str) -> int:
        """Refuse every call of run *run_id* still waiting for an answer: its
        turn is stopping. Returns how many there were."""
        with self._review_lock:
            ids = [key for key, review in self._reviews.items() if review.run_id == run_id]
        return sum(self.resolve_review(key, approve=False) for key in ids)

    def _consume_approval(self, run_id: str, digest: str) -> bool:
        now = time.time()
        with self._review_lock:
            self._prune_reviews(now)
            return self._approved.pop((run_id, digest), None) is not None

    # -- the packet the policy model sees --------------------------------------
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

    def _delegation(self, run: ActionRunState) -> list:
        """What each caller asked of its sub-agent, outermost first.

        Written by agents, not by the user: it tells the validator why the
        sub-agent acts, never that it may.
        """
        requests = []
        for state in reversed(list(run.lineage())):
            if state.delegation is None:
                continue
            request = state.delegation.get("request")
            if isinstance(request, str):
                limit = self.config.max_argument_value_bytes
                encoded = request.encode("utf-8")
                if len(encoded) > limit:
                    request = encoded[:limit].decode("utf-8", "ignore") + " [truncated]"
            requests.append({"tool": state.delegation.get("tool"), "request": request})
        return requests

    def _history(self, run: ActionRunState) -> dict:
        # Only calls that ran belong to what the run did: a blocked attempt had
        # no effect. A call that failed while running may have had one.
        ran = [
            event
            for event in run.history
            if event.get("outcome") in ("executed", "failed")
        ]
        window = ran[-self.config.max_history_events :]
        executed = self._fit(window, self.config.max_history_bytes)
        block: dict = {"executed": executed}
        dropped = len(ran) - len(executed)
        if dropped > 0:
            block["dropped_earlier_calls"] = dropped
        reasoning = run.reasoning_text
        if not reasoning and run.reasoning is not None:
            try:
                reasoning = run.reasoning() or ""
            except Exception:
                reasoning = ""
        if reasoning and self.config.max_reasoning_bytes:
            tail = reasoning.encode("utf-8")[-self.config.max_reasoning_bytes :]
            block["reasoning_tail"] = tail.decode("utf-8", "ignore")
        return block

    def _packet(self, run, tool_name, kind, arguments, descriptor, routing, filter_name) -> dict:
        selected = self.config.filters[filter_name]
        packet = {
            "trusted_task": run.task,
            "trusted_policy": {
                "version": self.config.version,
                "rules": [rule.model_dump() for rule in self.config.rules],
                "filter": {
                    "name": filter_name,
                    "label": selected.label,
                    "description": selected.description,
                },
            },
            "untrusted_action": {
                "kind": kind,
                "tool": tool_name,
                "arguments": arguments,
                "tool_metadata": descriptor or {},
            },
            # Established by the host from declared effects, not by any agent.
            "host_facts": {
                "effect": routing.effect,
                "why_judged": list(routing.reasons),
                "run_took_in": [dict(flow) for flow in run.root.flows[-10:]],
            },
            "untrusted_history": self._history(run),
            "run": {"attempt": run.attempts, "denials": run.denials},
        }
        delegation = self._delegation(run)
        if delegation:
            packet["untrusted_delegation"] = delegation
        return packet

    @staticmethod
    def _locator(raw_ctx: Any) -> Callable[[str], Optional[str]]:
        """Turns an argument path into a workspace path, as the tools resolve it."""
        factory = getattr(raw_ctx, "factory", None)
        try:
            root = _sandbox_root(factory)
        except Exception:
            root = None

        def locate(raw: str) -> Optional[str]:
            if root is None:
                return None
            try:
                from utils.path_utils import CONTAINER_WORKDIR

                value = raw.replace("\\", "/")
                # A foreign Windows path must not be interpreted as a relative
                # POSIX path under the workspace. Native Windows paths still
                # pass through Path's absolute/containment checks below.
                if os.name != "nt" and (
                    re.match(r"^[A-Za-z]:", value)
                    or value.startswith("//")
                ):
                    return None
                candidate = Path(value)
                if candidate.is_absolute():
                    # A container path is workspace-relative only for container runs.
                    if getattr(factory, "container_id", None) and (
                        value == CONTAINER_WORKDIR or value.startswith(CONTAINER_WORKDIR + "/")
                    ):
                        candidate = root / value[len(CONTAINER_WORKDIR):].lstrip("/")
                    elif candidate != root and root not in candidate.parents:
                        return None
                else:
                    candidate = root / candidate
                normalized = Path(os.path.abspath(candidate))
                return normalized.relative_to(root).as_posix()
            except Exception:
                return None

        return locate

    # -- audit ---------------------------------------------------------------
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

    def _deny(self, run, tool, kind, digest, rule, approval_id=None, verdicts=None):
        # Neither a pending review, a request nobody answered nor an
        # infrastructure outage is a violation. Attempt budgets still bound
        # repeated calls.
        if rule not in ("policy_review", "policy_unavailable", "review_timeout"):
            run.denials += 1
            if run.denials >= self.config.max_denials_per_run:
                run.stopped = True
        run.history.append(
            {"kind": kind, "tool": tool, "outcome": "blocked", "rule": rule}
        )
        decision = "unavailable" if rule == "policy_unavailable" else "deny"
        self._record(run, tool, kind, digest, decision, rule)
        filter_name, selected = self.config.filter(run.root.filter)
        payload = {
            "status": "blocked",
            "rule": rule,
            "run_stopped": run.halted,
            "next_step": _next_step_after_block(run.halted, rule, selected.label),
        }
        if approval_id is not None:
            payload["approval_id"] = approval_id
        if verdicts:
            payload["verdicts"] = dict(verdicts)
        if rule == "policy_filter":
            payload["filter"] = filter_name
        if rule == "policy_unavailable":
            payload["infrastructure_error"] = True
            payload["next_step"] = (
                "The policy validator is unavailable; this action did not execute. "
                "This is not a policy violation. The host has already attempted "
                "bounded recovery. Continue independent work and report this step "
                "as blocked by validator availability. Do not probe or work around "
                "the gate. The host can retry after service recovery."
            )
        return json.dumps(payload)

    # -- calls ---------------------------------------------------------------
    async def _operator_call(self, run, tool_name, kind, ctx, raw_args, invoke):
        """Run a call the operator configured, such as an agent's auto_run_tools.

        Its tool and arguments come from host configuration, not from a model,
        so there is no proposed action to judge against the user's task: asking
        whether the user requested it would deny every setup step. It is audited,
        stays out of the agent's history, and a stopped run still refuses it.
        """
        if not isinstance(run, ActionRunState):
            run = ActionRunState(task="")
        serialized = json.dumps(
            {"kind": kind, "tool": tool_name, "arguments": self._as_data(raw_args)},
            sort_keys=True,
            ensure_ascii=True,
            default=str,
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        async with run.lock:
            if run.halted:
                return self._deny(run, tool_name, kind, digest, "run_stopped")
        result = await invoke(ctx, raw_args)
        async with run.lock:
            self._record(
                run,
                tool_name,
                kind,
                digest,
                "executed",
                "operator_config",
                source="operator_config",
            )
        return result

    async def invoke(
        self,
        tool_name: str,
        kind: str,
        ctx: Any,
        raw_args: Any,
        invoke: Callable[[Any, Any], Awaitable[Any]],
        descriptor: dict | None = None,
        effect: Any = None,
    ):
        """Route one call, then run it. ``invoke`` receives the original input.

        ``effect`` is what the tool declares it does (utils.tool_effects).
        """
        if not self.mediates(kind):
            return await invoke(ctx, raw_args)
        raw_ctx = getattr(ctx, "context", None)
        run = getattr(raw_ctx, "action_state", None)
        if getattr(ctx, "operator_configured", False):
            return await self._operator_call(
                run, tool_name, kind, ctx, raw_args, invoke
            )
        if not isinstance(run, ActionRunState):
            # A call outside a trusted task carries no authorization to act.
            return self._deny(
                ActionRunState(task=""),
                tool_name,
                kind,
                "unparsed",
                "missing_trusted_task",
            )
        call = (tool_name, kind, ctx, raw_args, invoke, descriptor, effect)
        if any(active is run for active in _EXECUTING.get()):
            return await self._mediated_call(run, *call, None)
        previous, own = run.take_turn()
        try:
            return await self._mediated_call(run, *call, previous)
        finally:
            run.pass_turn(previous, own)

    async def _judge(self, packet: dict) -> tuple[str, dict]:
        """The policy model's verdict on a call, with how it was reached."""
        started = time.monotonic()
        judgment = await self._runner.evaluate(packet)
        verdict = judgment.verdicts.get("action") if judgment.verdicts else None
        meta = {
            "source": "validator",
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "validator_model": judgment.model,
            "validator_attempts": judgment.attempts,
            "queue_ms": judgment.queue_ms,
            "packet_bytes": len(json.dumps(packet, ensure_ascii=False).encode("utf-8")),
        }
        if judgment.failures:
            meta["validator_failures"] = judgment.failures
            if verdict is None:
                logger.warning(
                    "Action-policy validator unavailable for %s: %s",
                    packet["untrusted_action"]["tool"],
                    "; ".join(judgment.failures),
                )
        return verdict or "unavailable", meta

    async def _ask(self, run, tool_name, kind, digest, routing, decision_meta, call_meta) -> Optional[bool]:
        """Hold the call until the user answers in the chat; None when nobody does."""
        approval_id = self._request_review(run, tool_name, kind, digest, routing.reasons)
        future = asyncio.get_running_loop().create_future()
        with self._review_lock:
            self._waiters[approval_id] = (future, run)
        async with run.lock:
            self._record(
                run, tool_name, kind, digest, "review", "policy_check",
                awaiting=True, approval_id=approval_id, approvals=self.config.approvals,
                **decision_meta, **call_meta,
            )
        try:
            return await asyncio.wait_for(future, timeout=self.config.review_ttl_seconds)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._review_lock:
                self._waiters.pop(approval_id, None)
                self._reviews.pop(approval_id, None)

    async def _mediated_call(
        self, run, tool_name, kind, ctx, raw_args, invoke, descriptor, effect, previous
    ):
        """Route one call alongside its siblings, then run it in its turn."""
        raw_ctx = getattr(ctx, "context", None)
        digest = "unparsed"
        # Parallel calls to one tool are routed concurrently; the call id lets an
        # event sink attach each verdict to the exact call it belongs to.
        call_id = getattr(ctx, "tool_call_id", None)
        call_meta = {"call_id": call_id} if isinstance(call_id, str) and call_id else {}
        root = run.root
        try:
            async with run.lock:
                if run.halted:
                    raise PolicyDenied("run_stopped")
                run.attempts += 1
                root.turn_attempts += 1
                if run.attempts > self.config.max_attempts_per_run:
                    # A delegated run spends its own budget; its caller goes on.
                    run.stopped = True
                    raise PolicyDenied("attempt_limit")
                if root.turn_attempts > self.config.max_attempts_per_turn:
                    root.stopped = True
                    raise PolicyDenied("turn_attempt_limit")
                if (
                    self.config.max_delegation_depth is not None
                    and run.depth > self.config.max_delegation_depth
                ):
                    # However the sub-agent was started - agent tool or a
                    # function such as orchestrate - nesting stays bounded.
                    raise PolicyDenied("delegation_depth")
                if not isinstance(run.task, str) or not run.task.strip():
                    raise PolicyDenied("missing_trusted_task")
                arguments = self._as_data(raw_args)
                serialized = json.dumps(
                    {"kind": kind, "tool": tool_name, "arguments": arguments},
                    sort_keys=True,
                    ensure_ascii=True,
                    default=str,
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
                filter_name, selected = self.config.filter(root.filter)
                routing: Routing = route_call(
                    self.config,
                    selected,
                    effect_of(self.config, tool_name, kind, effect),
                    arguments,
                    run.flow_kinds,
                    self._locator(raw_ctx),
                )
                packet = (
                    self._packet(
                        run, tool_name, kind, projected_arguments, descriptor, routing, filter_name
                    )
                    if routing.route == "judge"
                    else None
                )
            route_meta = {
                "effect": routing.effect,
                "route": routing.route,
                "filter": filter_name,
                "reasons": list(routing.reasons),
            }
            if packet is not None:
                # Judged outside the lock: a mediated call may itself make mediated calls.
                verdict, decision_meta = await self._judge(packet)
            else:
                verdict, decision_meta = routing.route, {"source": "filter"}
            decision_meta = {**route_meta, **decision_meta}
            if verdict in ("review", "unavailable"):
                if tool_name in root.grants:
                    verdict, decision_meta["source"] = "allow", "user_grant"
                elif verdict == "review" and self._consume_approval(run.run_id, digest):
                    verdict, decision_meta["source"] = "allow", "host_approval"
            ask = (
                verdict in ("review", "unavailable")
                and self.config.mode == "enforce"
                and root.interactive
            )
            stop_after_shadow_call = False
            if not ask:
                async with run.lock:
                    self._record(
                        run, tool_name, kind, digest, verdict, "policy_check",
                        **decision_meta, **call_meta,
                    )
                    if run.halted:
                        raise PolicyDenied("run_stopped")
                    if verdict != "allow" and self.config.mode == "enforce":
                        if decision_meta["source"] == "filter" and verdict == "deny":
                            raise PolicyDenied("policy_filter")
                        approval_id = (
                            self._request_review(run, tool_name, kind, digest, routing.reasons)
                            if verdict == "review"
                            else None
                        )
                        raise PolicyDenied("policy_" + verdict, approval_id, {"action": verdict})
                    if verdict not in ("allow", "unavailable") and self.config.mode == "shadow":
                        run.denials += 1
                        stop_after_shadow_call = run.denials >= self.config.max_denials_per_run
            else:
                answer = await self._ask(
                    run, tool_name, kind, digest, routing, decision_meta, call_meta
                )
                async with run.lock:
                    self._record(
                        run, tool_name, kind, digest,
                        "allow" if answer else "deny", "policy_check",
                        **route_meta, source="user" if answer is not None else "no_answer",
                        **call_meta,
                    )
                    if not answer:
                        raise PolicyDenied("declined" if answer is False else "review_timeout")
                    if run.halted:
                        raise PolicyDenied("run_stopped")
        except PolicyDenied as exc:
            async with run.lock:
                return self._deny(
                    run,
                    tool_name,
                    kind,
                    digest,
                    exc.rule,
                    approval_id=exc.approval_id,
                    verdicts=exc.verdicts,
                )
        except (ValueError, TypeError, KeyError, OSError):
            async with run.lock:
                return self._deny(run, tool_name, kind, digest, "invalid_call")

        if previous is not None:
            # Routed concurrently, executed in the order the model issued them.
            await asyncio.shield(previous)
            if run.halted:
                async with run.lock:
                    return self._deny(run, tool_name, kind, digest, "run_stopped")

        # Executed outside the judgment: a tool's own error is neither a policy
        # denial nor a reason to stop the run - the agent sees it and goes on.
        # Only an interruption (cancellation, shutdown) stops the run.
        entry = {
            "kind": kind,
            "tool": tool_name,
            "arguments": projected_arguments,
            "effect": routing.effect,
        }
        executing = _EXECUTING.set((*_EXECUTING.get(), run))
        try:
            result = await invoke(ctx, raw_args)
        except Exception:
            async with run.lock:
                run.history.append({**entry, "outcome": "failed"})
                self._take_in(run, tool_name, routing)
                self._record(
                    run, tool_name, kind, digest, "failed", "execution_error", **call_meta
                )
            raise
        except BaseException:
            async with run.lock:
                run.stopped = True
                self._record(
                    run, tool_name, kind, digest, "stopped", "execution_interrupted"
                )
            raise
        finally:
            _EXECUTING.reset(executing)
        async with run.lock:
            if stop_after_shadow_call:
                run.stopped = True
            run.history.append({**entry, "outcome": "executed"})
            self._take_in(run, tool_name, routing)
            self._record(
                run,
                tool_name,
                kind,
                digest,
                "executed",
                "policy_check",
                **call_meta,
            )
        return result

    @staticmethod
    def _take_in(run: ActionRunState, tool_name: str, routing: Routing) -> None:
        """Remember what a call that ran took into the turn."""
        root = run.root
        for fact in routing.flows:
            root.flows.append({"fact": fact, "tool": tool_name, "call": root.turn_attempts})
