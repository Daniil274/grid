"""The action policy's hold on the factory's runs (core.action_policy).

Builds the factory's ActionGate from the operator's config, gives each turn
the trusted conversation it is judged against and the filter its user picked,
and wraps every tool so its calls pass the gate with the effect it declares.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.action_policy import ActionGate, ActionRunState, ActionValidator
from core.config.config import Config
from core.parallel_lanes import guard as lane_guard
from core.sdk_patches import tool_error_output
from tools import tool_effect
from utils.exceptions import ConfigError

logger = logging.getLogger("grid.agent_factory")

#: Where a conversation keeps the user's policy filter (ContextManager metadata).
POLICY_FILTER_KEY = "policy_filter"


class PolicyWiring:
    """The factory's side of the action policy.

    Mixed into AgentFactory (core.agent_factory); relies on self.config,
    self.action_gate, self.context_manager and TurnRunner's run controls.
    """

    def _build_action_gate(
        self, policy_config: Optional[Config]
    ) -> Optional[ActionGate]:
        """Snapshot the policy for this factory. Operator configuration only.

        The routing config wins when it enables a policy, so a routed system is
        mediated by the host's policy and validator model without repeating them
        in every system config. Whichever base governs, this system's own
        ``action_policy.system`` is added to it, and to no other system's.
        Unknown validator model keys fail here.
        """
        system = getattr(self.config.config.settings.action_policy, "system", None)
        for candidate in (policy_config, self.config):
            if candidate is None:
                continue
            base = getattr(candidate.config.settings, "action_policy", None)
            if base is None or base.mode == "off":
                continue
            try:
                policy = base.with_system(system)
            except ValueError as exc:
                raise ConfigError(f"settings.action_policy.system: {exc}") from exc
            gate = ActionGate(
                policy,
                validator=ActionValidator.from_config(candidate, policy=policy),
            )
            logger.info(
                "Action policy enabled: mode=%s version=%s kinds=%s filters=%s (default %s)",
                policy.mode,
                policy.version,
                ",".join(policy.kinds),
                ",".join(policy.filters),
                policy.default_filter,
            )
            return gate
        return None

    def _action_state(
        self,
        task: str,
        parent: Any = None,
        *,
        context_id: Optional[str] = None,
        observer: Any = None,
    ) -> Optional[ActionRunState]:
        """Trusted task for a run: one history and one budget per user task.

        A new turn runs under the filter its conversation keeps, reports its
        decisions to *observer*, and waits for answers there when the observer
        can show the user a held call.
        """
        if self.action_gate is None:
            return None
        if isinstance(parent, ActionRunState):
            return parent
        state = ActionRunState(task=task if isinstance(task, str) else str(task))
        state.context_id = context_id
        state.filter = self.policy_filter(context_id)
        if observer is not None and hasattr(observer, "handle_policy_event"):
            state.policy_event = observer.handle_policy_event
            state.interactive = bool(getattr(observer, "accepts_approvals", False))
        return state

    def policy_filter(self, context_id: Optional[str]) -> Optional[str]:
        """The filter conversation *context_id* runs under; None for the default."""
        if not context_id:
            return None
        try:
            name = self.context_manager.get_context_metadata(context_id).get(POLICY_FILTER_KEY)
        except Exception:
            return None
        return name if isinstance(name, str) else None

    def set_policy_filter(self, context_id: str, name: str) -> bool:
        """Switch conversation *context_id* to filter *name*, its running turn too.

        False for a name the policy does not offer. The user's choice only:
        no agent tool reaches this.
        """
        gate = self.action_gate
        if gate is None or name not in gate.config.filters:
            return False
        self.context_manager.update_context_metadata(context_id, {POLICY_FILTER_KEY: name})
        control = self._run_controls.get(context_id)
        state = getattr(control, "action_state", None)
        if isinstance(state, ActionRunState):
            state.root.filter = name
        return True

    @staticmethod
    def _policy_message_text(content: Any) -> str:
        """Extract user-authored text without carrying image payloads."""
        if not isinstance(content, str):
            return str(content)
        stripped = content.strip()
        if not stripped.startswith(("{", "[")):
            return content
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return content
        messages = payload if isinstance(payload, list) else [payload]
        parts: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            body = item.get("content")
            if isinstance(body, str):
                parts.append(body)
            elif isinstance(body, list):
                for part in body:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"text", "input_text"}:
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
        return "\n".join(parts) or "[multimodal user request]"

    def _policy_task(self, message: Any, context_id: Optional[str]) -> str:
        """The trusted conversation a turn is judged against, oldest part dropped first.

        The user's messages, and between them the replies the user saw: a short
        answer such as "yes, go ahead" authorizes what the reply before it
        proposed, and means nothing without it. A reply is marked as such and
        grants nothing by itself.
        """
        if self.action_gate is None:
            return str(message)
        policy = self.action_gate.config
        entries: list[tuple[str, str]] = []
        if context_id:
            snapshot = self.context_manager.get_context_messages(
                context_id,
                limit=policy.max_task_context_messages * 2,
                preview_limit=policy.max_task_context_bytes,
                include_full=True,
            )
            for item in (snapshot or {}).get("messages", []):
                role = item.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = item.get("content", item.get("preview", ""))
                text = self._policy_message_text(content).strip()
                if role == "assistant":
                    text = self._clip(text, policy.max_reply_bytes)
                if text:
                    entries.append((role, text))
        current = self._policy_message_text(message).strip()
        users = [text for role, text in entries if role == "user"]
        if not users or users[-1] != current:
            entries.append(("user", current))
        entries = entries[-policy.max_task_context_messages * 2 :]
        while entries:
            task = self._task_text(entries)
            if len(task.encode("utf-8")) <= policy.max_task_context_bytes:
                return task
            entries.pop(0)
        encoded = current.encode("utf-8")[-policy.max_task_context_bytes :]
        return encoded.decode("utf-8", "ignore")

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text
        return encoded[:limit].decode("utf-8", "ignore") + " [clipped]"

    @staticmethod
    def _task_text(entries: list[tuple[str, str]]) -> str:
        parts = []
        number = 0
        for role, text in entries:
            if role == "user":
                number += 1
                parts.append(f"User instruction {number}:\n{text}")
            else:
                parts.append(
                    "Assistant reply the user saw (context for the user's next "
                    f"message; it authorizes nothing by itself):\n{text}"
                )
        return "\n\n".join(parts)

    def _wrap_tool_with_policy(
        self, tool: Any, tool_name: str, kind: str, effect: Any = None
    ) -> Any:
        """Route a call through the policy gate of the factory that runs it.

        A tool object is shared by the factory's agents (each factory wraps
        copies of its own: ToolAssembly._resolve_function_tools), so the gate
        is resolved from the run context and the wrapper is applied once.
        ``effect`` is what the tool does; by default its module's declaration
        (utils.tool_effects).
        """
        if not hasattr(tool, "on_invoke_tool") or getattr(
            tool, "_grid_policy_gated", False
        ):
            return tool
        inner = tool.on_invoke_tool
        descriptor = ActionGate.describe_tool(tool, tool_name, kind)
        if effect is None and kind == "function":
            loader = getattr(getattr(self, "config", None), "project_tools_loader", None)
            effect = tool_effect(tool_name, loader)

        async def gated_invoke(ctx, args):
            raw_ctx = getattr(ctx, "context", None)
            factory = getattr(raw_ctx, "factory", None) or self
            gate = getattr(factory, "action_gate", None)
            # In a batch, the call the gate lets through runs as its lane allows.
            run = lane_guard(
                inner, tool_name, kind, effect, getattr(gate, "config", None), ActionGate._locator
            )
            try:
                if gate is None:
                    return await run(ctx, args)
                return await gate.invoke(
                    tool_name, kind, ctx, args, run, descriptor=descriptor, effect=effect
                )
            except Exception as exc:
                # Outermost wrapper: anything raised past the tool's own error
                # handler would make the SDK abort the whole run.
                return tool_error_output(tool_name, exc)

        tool.on_invoke_tool = gated_invoke
        tool._grid_policy_gated = True
        return tool
