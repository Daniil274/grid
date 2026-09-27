"""The action policy's hold on the factory's runs (core.action_policy).

Builds the factory's ActionGate from the operator's config, gives each
run the trusted task it is judged against, and wraps every tool so its calls
pass the gate.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.action_policy import ActionGate, ActionRunState, ActionValidator
from core.config.config import Config
from core.sdk_patches import tool_error_output

logger = logging.getLogger("grid.agent_factory")


class PolicyWiring:
    """The factory's side of the action policy.

    Mixed into AgentFactory (core.agent_factory); relies on self.config,
    self.action_gate, self.context_manager.
    """

    def _build_action_gate(
        self, policy_config: Optional[Config]
    ) -> Optional[ActionGate]:
        """Snapshot the policy for this factory. Operator configuration only.

        The routing config wins when it enables a policy, so a routed system is
        mediated by the host's policy and validator model without repeating them
        in every system config. Unknown validator model keys fail here.
        """
        for candidate in (policy_config, self.config):
            if candidate is None:
                continue
            policy = getattr(candidate.config.settings, "action_policy", None)
            if policy is None or policy.mode == "off":
                continue
            gate = ActionGate(policy, validator=ActionValidator.from_config(candidate))
            logger.info(
                "Action policy enabled: mode=%s version=%s kinds=%s chain=%s",
                policy.mode,
                policy.version,
                ",".join(policy.kinds),
                policy.check_chain,
            )
            return gate
        return None

    def _action_state(self, task: str, parent: Any = None) -> Optional[ActionRunState]:
        """Trusted task for a run: one chain and one budget per user task."""
        if self.action_gate is None:
            return None
        if isinstance(parent, ActionRunState):
            return parent
        return ActionRunState(task=task if isinstance(task, str) else str(task))

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
        """Build trusted task context from user messages, newest first on trim."""
        if self.action_gate is None:
            return str(message)
        policy = self.action_gate.config
        prior: list[str] = []
        if context_id:
            snapshot = self.context_manager.get_context_messages(
                context_id,
                limit=policy.max_task_context_messages * 2,
                preview_limit=policy.max_task_context_bytes,
                include_full=True,
            )
            for item in (snapshot or {}).get("messages", []):
                if item.get("role") != "user":
                    continue
                content = item.get("content", item.get("preview", ""))
                text = self._policy_message_text(content).strip()
                if text:
                    prior.append(text)
        current = self._policy_message_text(message).strip()
        if not prior or prior[-1] != current:
            prior.append(current)
        prior = prior[-policy.max_task_context_messages :]
        while prior:
            task = "\n\n".join(
                f"User instruction {index + 1}:\n{text}"
                for index, text in enumerate(prior)
            )
            if len(task.encode("utf-8")) <= policy.max_task_context_bytes:
                return task
            prior.pop(0)
        encoded = current.encode("utf-8")[-policy.max_task_context_bytes :]
        return encoded.decode("utf-8", "ignore")

    def _wrap_tool_with_policy(self, tool: Any, tool_name: str, kind: str) -> Any:
        """Route a call through the policy gate of the factory that runs it.

        Tool objects are shared between agents and factories, so the gate is
        resolved from the run context and the wrapper is applied once.
        """
        if not hasattr(tool, "on_invoke_tool") or getattr(
            tool, "_grid_policy_gated", False
        ):
            return tool
        inner = tool.on_invoke_tool
        descriptor = ActionGate.describe_tool(tool, tool_name, kind)

        async def gated_invoke(ctx, args):
            raw_ctx = getattr(ctx, "context", None)
            factory = getattr(raw_ctx, "factory", None) or self
            gate = getattr(factory, "action_gate", None)
            try:
                if gate is None:
                    return await inner(ctx, args)
                return await gate.invoke(
                    tool_name, kind, ctx, args, inner, descriptor=descriptor
                )
            except Exception as exc:
                # Outermost wrapper: anything raised past the tool's own error
                # handler would make the SDK abort the whole run.
                return tool_error_output(tool_name, exc)

        tool.on_invoke_tool = gated_invoke
        tool._grid_policy_gated = True
        return tool
