"""A conversation's agent sessions: how full they are, compaction and forks.

An agent's SDK session holds everything it did in a conversation. It is
measured against the model's window (core.context_budget) and, past the
threshold, summarized and replaced by the summary - the chat people see stays
as it is. Every compaction starts a new session epoch, so a fork knows which
steps the summary already covers. A conversation forks before an edited
message: the branch's session is the original's up to that message.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from core.compact import COMPACTED_TYPE, compact_conversation, get_auto_compact_threshold
from core.context_budget import request_tokens, session_transcript

logger = logging.getLogger("grid.agent_factory")


class SessionUpkeep:
    """Measures, compacts and forks the agents' sessions of a conversation.

    Mixed into AgentFactory (core.agent_factory); relies on
    self.context_manager, self._get_agent_session, self.models,
    self.compact_config and self._compact_tracking.
    """

    def _session_epoch(self, context_id: str, agent_key: str) -> int:
        """How many times *agent_key*'s session in *context_id* was replaced by a summary."""
        epochs = self.context_manager.get_context_metadata(context_id).get("session_epochs") or {}
        return int(epochs.get(agent_key, 0))

    def _bump_session_epoch(self, context_id: str, agent_key: str) -> None:
        epochs = dict(self.context_manager.get_context_metadata(context_id).get("session_epochs") or {})
        epochs[agent_key] = int(epochs.get(agent_key, 0)) + 1
        self.context_manager.update_context_metadata(context_id, {"session_epochs": epochs})

    async def fork_conversation(self, context_id: str, message_id: str) -> str:
        """Branch *context_id* at user message *message_id*, for an edited version of it.

        The branch holds the messages before it (ContextManager.fork_context)
        and, for each agent that worked in the conversation, its session up to
        the point where the turn of that message started - so the agent in the
        branch remembers every step before the edit, tool calls included, and
        nothing after. The cut is the turn's ``session_mark``. When the session
        was summarized since that mark, no exact cut exists: that agent's
        session in the branch starts empty and it reads the copied messages as a
        transcript instead. Returns the branch id.
        """
        branch_id, _ = self.context_manager.fork_context(context_id, message_id)
        messages = self.context_manager.conversation_view(context_id)["messages"]
        index = next(i for i, m in enumerate(messages) if (m.metadata or {}).get("message_id") == message_id)
        epochs = self.context_manager.get_context_metadata(context_id).get("session_epochs") or {}
        branch_epochs = dict(epochs)
        agents = {(m.metadata or {}).get("agent") for m in messages} - {None}
        for agent in sorted(agents):
            items = await self._get_agent_session(agent, context_id).get_items()
            if not items:
                continue
            # The first turn of this agent at or after the edit point.
            mark = next(
                (
                    (m.metadata or {})["session_mark"]
                    for m in messages[index:]
                    if ((m.metadata or {}).get("session_mark") or {}).get("agent") == agent
                ),
                None,
            )
            if mark is None:
                cut = len(items)  # the agent did nothing after the edit point
            elif int(mark.get("epoch", 0)) == int(epochs.get(agent, 0)) and mark["items"] <= len(items):
                cut = mark["items"]
            else:
                # Summarized since: the copied marks of this agent no longer apply.
                branch_epochs[agent] = int(epochs.get(agent, 0)) + 1
                logger.info("Branch %s: %s starts from the transcript (session was compacted)", branch_id, agent)
                continue
            if cut:
                await self._get_agent_session(agent, branch_id).add_items(items[:cut])
        self.context_manager.update_context_metadata(branch_id, {"session_epochs": branch_epochs})
        return branch_id

    async def context_usage(self, agent_key: str, context_id: Optional[str] = None) -> Dict[str, int]:
        """How full *agent_key*'s context is in a conversation: tokens, window, threshold.

        Measured on the agent's SDK session - what the model reads - not on the
        stored chat, which holds only the visible text.
        """
        context_id = context_id or self.get_active_context_id()
        items = await self._get_agent_session(agent_key, context_id).get_items()
        window = self.models.context_window(agent_key)
        return {
            "tokens": request_tokens(items),
            "window": window,
            "threshold": get_auto_compact_threshold(window, self.compact_config),
        }

    async def compact_session(
        self, agent_key: str, context_id: str, *, force: bool = False
    ) -> Optional[Dict[str, int]]:
        """Summarize *agent_key*'s session in a conversation into one message.

        Without ``force`` only when the session is past the auto-compact
        threshold (core.context_budget) and compaction has not failed
        ``compact.auto.max_consecutive_failures`` times in a row. The compaction
        model reads the session - messages, tool calls and their results - and
        the session is replaced by its summary. The stored chat users see keeps
        every message; it only gains a display-only marker (COMPACTED_TYPE).
        Returns ``{"tokens_before", "tokens_after"}``, or None when
        nothing was compacted; a failure is logged, never raised.
        """
        compact_cfg = self.compact_config
        if not compact_cfg.enabled or not (force or compact_cfg.auto.enabled):
            return None
        session = self._get_agent_session(agent_key, context_id)
        items = await session.get_items()
        if not items:
            return None
        tokens_before = request_tokens(items)
        window = self.models.context_window(agent_key)
        if not force:
            if tokens_before <= get_auto_compact_threshold(window, compact_cfg):
                return None
            if self._compact_tracking.consecutive_failures >= compact_cfg.auto.max_consecutive_failures:
                logger.warning(
                    "Auto-compact of %s skipped after %d failures in a row",
                    agent_key,
                    self._compact_tracking.consecutive_failures,
                )
                return None
        logger.info("Compacting the session of %s: ~%d/%d tokens", agent_key, tokens_before, window)
        try:
            client, model = self.models.compact_client_and_model(agent_key)
            result = await compact_conversation(
                messages=session_transcript(items),
                llm_client=client,
                model=model,
                suppress_followup_questions=True,
                is_auto_compact=not force,
                max_output_tokens=compact_cfg.summary_max_output_tokens,
                compact_cfg=compact_cfg,
            )
        except Exception:
            self._compact_tracking.consecutive_failures += 1
            logger.warning("Compacting the session of %s failed", agent_key, exc_info=True)
            return None
        summary = [
            {"role": message.role, "content": message.get_text()}
            for message in result.summary_messages
            if message.get_text().strip()
        ]
        if not result.success() or not summary:
            self._compact_tracking.consecutive_failures += 1
            logger.warning(
                "Compacting the session of %s produced no summary: %s",
                agent_key,
                result.user_display_message or result.error_message or result.status.value,
            )
            return None
        self._compact_tracking.consecutive_failures = 0
        await session.clear_session()
        await session.add_items(summary)
        # Turn marks taken before this point no longer count items of this session.
        self._bump_session_epoch(context_id, agent_key)
        tokens_after = request_tokens(summary)
        logger.info("Compacted the session of %s: ~%d -> ~%d tokens", agent_key, tokens_before, tokens_after)
        # The chat keeps its own visible log beside the agent session, so a
        # compaction would otherwise leave no trace in the thread. Drop a
        # display-only marker into it: it never reaches the model, but the chat
        # shows where the context was rewritten. ``append_message_to`` returns
        # False for contexts that are not chat conversations, which is fine.
        self.context_manager.append_message_to(
            context_id,
            "assistant",
            "",
            metadata={
                "type": COMPACTED_TYPE,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            },
        )
        return {"tokens_before": tokens_before, "tokens_after": tokens_after}
