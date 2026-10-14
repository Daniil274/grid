"""A conversation's agent sessions: how full they are, compaction and forks.

An agent's SDK session holds everything it did in a conversation. It is
measured against the model's window (core.context_budget) and, past the
threshold, summarized and replaced by the summary - the chat people see stays
as it is. Every compaction starts a new session epoch, so a fork knows which
steps the summary already covers. A conversation forks before an edited
message: the branch's session is the original's up to that message. A fork
into a new chat cuts the same way after a turn, and deleting a turn cuts that
turn's items out of the session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from core.compact import COMPACTED_TYPE, compact_conversation
from core.context_budget import request_tokens, session_transcript
from core.interruption import CONTINUATION_TYPE

logger = logging.getLogger("grid.agent_factory")

#: Stored user messages that start a turn: one the user sent, or a Continue.
TURN_START_TYPES = ("user_input", CONTINUATION_TYPE)


def is_turn_start(message: Any) -> bool:
    metadata = getattr(message, "metadata", None) or {}
    return getattr(message, "role", None) == "user" and metadata.get("type", "user_input") in TURN_START_TYPES


def turn_bounds(messages: list, index: int) -> Tuple[Optional[int], int]:
    """(start, end) of the turn ``messages[index]`` belongs to: its request,
    and the next turn's (or the end). Start is None before the first turn."""
    start = next((i for i in range(index, -1, -1) if is_turn_start(messages[i])), None)
    end = next((i for i in range(index + 1, len(messages)) if is_turn_start(messages[i])), len(messages))
    return start, end


def _is_assistant_message(item: Any) -> bool:
    """An SDK session item that is the model's written message (not a tool call)."""
    return isinstance(item, dict) and item.get("role") == "assistant" and item.get("type", "message") == "message"


class SessionUpkeep:
    """Measures, compacts and forks the agents' sessions of a conversation.

    Mixed into AgentFactory (core.agent_factory); relies on
    self.context_manager, self._get_agent_session, self.models,
    self.compact_config and self._compact_tracking.
    """

    def sync_memory_from_context(self, messages) -> list:
        """Best-effort distillation of messages about to leave the SDK session."""
        try:
            config = getattr(self, "config", None)
            if config is None or not hasattr(config, "get_memory_config"):
                return []
            settings = config.get_memory_config()
            if not settings.enabled or not settings.extract_on_compact:
                return []
            manager = self.get_memory_manager()
            if manager is None:
                return []
            normalized = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                item = dict(message)
                content = item.get("content", "")
                if isinstance(content, list):
                    item["content"] = "\n".join(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text")
                    )
                normalized.append(item)
            return manager.extract_and_store_from_messages(normalized)
        except Exception:
            logger.warning("Memory extraction failed; compaction continues", exc_info=True)
            return []

    def remember_turn(self, context_id: Optional[str], request: Any, answer: Any) -> list:
        """Best-effort: distil a finished turn (request and answer) into memory.

        Runs after every answered turn when ``memory.enabled`` and ``extract_on_turn``
        are on - not only when a session is compacted, which a chat rarely reaches.
        The turn's own two messages are the increment, and storing is idempotent, so
        nothing is saved twice. The exchange itself becomes an L0 record
        (``snapshot_turns``); facts, rules and preferences become L1/L3 and the
        scenario L2. The manager is the one the tools and the web panel use.
        """
        try:
            config = getattr(self, "config", None)
            if config is None or not hasattr(config, "get_memory_config"):
                return []
            settings = config.get_memory_config()
            if not settings.enabled or not getattr(settings, "extract_on_turn", True):
                return []
            if not isinstance(request, str) or not isinstance(answer, str):
                return []
            if not request.strip() or not answer.strip():
                return []
            manager = self.get_memory_manager()
            if manager is None:
                return []
            return manager.extract_and_store_from_messages(
                [{"role": "user", "content": request}, {"role": "assistant", "content": answer}],
                source_ref=f"chat:{context_id}" if context_id else None,
                snapshot=bool(getattr(settings, "snapshot_turns", True)),
            )
        except Exception:
            logger.warning("Memory extraction after the turn failed; the turn is unaffected", exc_info=True)
            return []

    def _session_epoch(self, context_id: str, agent_key: str) -> int:
        """How many times *agent_key*'s session in *context_id* was replaced by a summary."""
        epochs = self.context_manager.get_context_metadata(context_id).get("session_epochs") or {}
        return int(epochs.get(agent_key, 0))

    def _bump_session_epoch(self, context_id: str, agent_key: str) -> None:
        epochs = dict(self.context_manager.get_context_metadata(context_id).get("session_epochs") or {})
        epochs[agent_key] = int(epochs.get(agent_key, 0)) + 1
        self.context_manager.update_context_metadata(context_id, {"session_epochs": epochs})

    async def _strip_legacy_context_footers(self, session: Any) -> None:
        """Retain legacy history until a provenance-aware atomic migration exists.

        The footer's text is not evidence of service ownership: file reads and
        assistant quotations can contain the exact same suffix. The SDK Session
        protocol also has no atomic compare-and-replace; clear/add loses history
        on failure or concurrent append. Never rewrite persistent data here.
        Kept as a safe hook for the pending footer-free turn/tool contract.
        """
        return

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
        await self._copy_sessions(context_id, branch_id, messages, index)
        return branch_id

    async def fork_to_new(self, context_id: str, message_id: str) -> str:
        """A new conversation that is *context_id* up to the end of *message_id*'s turn.

        The fork holds every message through that turn and, like a branch, each
        agent's session up to the same point; the original is left as it is.
        Returns the new conversation's id.
        """
        messages = self._messages(context_id)
        index = self._message_index(messages, message_id)
        _, end = turn_bounds(messages, index)
        fork_id = self.context_manager.copy_context(context_id, end)
        await self._copy_sessions(context_id, fork_id, messages, end)
        return fork_id

    async def delete_message(self, context_id: str, message_id: str) -> Dict[str, Any]:
        """Delete a message from the chat and from what the agent remembers.

        A message the user sent as a turn takes its whole turn with it: the
        answer, the interruption, the messages added mid-turn, and the part of
        the agent's session the turn wrote - its request, tool calls and
        results. Any other message (an answer, say) goes alone; an answer also
        leaves the session as the turn's last assistant message. When the
        session was summarized since that turn, there is nothing exact to cut:
        only the chat changes. Returns ``{"removed": [ids], "context": bool}``,
        ``context`` telling whether the agent's session changed.
        """
        messages = self._messages(context_id)
        index = self._message_index(messages, message_id)
        start, end = turn_bounds(messages, index)
        whole_turn = index == start
        doomed = messages[start:end] if whole_turn else [messages[index]]
        target = messages[index]
        answer = target.role == "assistant" and (target.metadata or {}).get("type") == "agent_response"

        context = False
        span = await self._turn_span(context_id, messages, start, end) if start is not None else None
        if span is not None and (whole_turn or answer):
            agent, epoch, session, items, low, high = span
            if whole_turn:
                cut = list(range(low, high))
            else:
                answers = [i for i in range(low, high) if _is_assistant_message(items[i])]
                cut = answers[-1:]
            if cut:
                dropped = set(cut)
                kept = [item for i, item in enumerate(items) if i not in dropped]
                await session.clear_session()
                if kept:
                    await session.add_items(kept)
                context = True

        removed = [(m.metadata or {}).get("message_id") for m in doomed]
        removed = [mid for mid in removed if mid]
        self.context_manager.remove_messages(context_id, removed)
        if context:
            self.context_manager.shift_session_marks(context_id, agent, epoch, high, -len(cut))
        return {"removed": removed, "context": context}

    def _messages(self, context_id: str) -> list:
        view = self.context_manager.conversation_view(context_id)
        if view is None:
            raise KeyError(f"Unknown conversation {context_id}")
        return view["messages"]

    @staticmethod
    def _message_index(messages: list, message_id: str) -> int:
        for index, message in enumerate(messages):
            if (message.metadata or {}).get("message_id") == message_id:
                return index
        raise KeyError(f"No message {message_id}")

    async def _turn_span(self, context_id: str, messages: list, start: int, end: int) -> Optional[tuple]:
        """Where the turn of ``messages[start:end]`` sits in its agent's session:
        (agent, epoch, session, items, first item, end item); None when unknown -
        no mark, or the session was summarized since."""
        mark = (messages[start].metadata or {}).get("session_mark") or {}
        agent = mark.get("agent")
        if not agent:
            return None
        epoch = int(mark.get("epoch", 0))
        if epoch != self._session_epoch(context_id, agent):
            return None
        session = self._get_agent_session(agent, context_id)
        items = await session.get_items()
        following = next(
            (
                (m.metadata or {})["session_mark"]
                for m in messages[end:]
                if ((m.metadata or {}).get("session_mark") or {}).get("agent") == agent
            ),
            None,
        )
        low = int(mark.get("items", 0))
        high = int(following["items"]) if following and int(following.get("epoch", 0)) == epoch else len(items)
        if not 0 <= low <= high <= len(items):
            return None
        return agent, epoch, session, items, low, high

    async def _copy_sessions(self, source_id: str, target_id: str, messages: list, index: int) -> None:
        """Give *target_id* each agent's session of *source_id* as it was when
        ``messages[index]``'s turn started (all of it past the last turn)."""
        epochs = self.context_manager.get_context_metadata(source_id).get("session_epochs") or {}
        target_epochs = dict(epochs)
        agents = {(m.metadata or {}).get("agent") for m in messages} - {None}
        for agent in sorted(agents):
            items = await self._get_agent_session(agent, source_id).get_items()
            if not items:
                continue
            # The first turn of this agent at or after the cut.
            mark = next(
                (
                    (m.metadata or {})["session_mark"]
                    for m in messages[index:]
                    if ((m.metadata or {}).get("session_mark") or {}).get("agent") == agent
                ),
                None,
            )
            if mark is None:
                cut = len(items)  # the agent did nothing after the cut
            elif int(mark.get("epoch", 0)) == int(epochs.get(agent, 0)) and mark["items"] <= len(items):
                cut = mark["items"]
            else:
                # Summarized since: the copied marks of this agent no longer apply.
                target_epochs[agent] = int(epochs.get(agent, 0)) + 1
                logger.info("Conversation %s: %s starts from the transcript (session was compacted)", target_id, agent)
                continue
            if cut:
                await self._get_agent_session(agent, target_id).add_items(items[:cut])
        self.context_manager.update_context_metadata(target_id, {"session_epochs": target_epochs})

    async def context_usage(self, agent_key: str, context_id: Optional[str] = None) -> Dict[str, int]:
        """How full *agent_key*'s context is in a conversation: tokens, window, threshold.

        Measured on the agent's SDK session - what the model reads - not on the
        stored chat, which holds only the visible text.
        """
        context_id = context_id or self.get_active_context_id()
        items = await self._get_agent_session(agent_key, context_id).get_items()
        return {
            "tokens": request_tokens(items),
            "window": self.models.context_window(agent_key),
            "threshold": self.models.compact_threshold(agent_key, self.compact_config),
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
            if tokens_before <= self.models.compact_threshold(agent_key, compact_cfg):
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
            client, model, responses_api = self.models.compact_request(agent_key)
            result = await compact_conversation(
                messages=session_transcript(items),
                llm_client=client,
                model=model,
                suppress_followup_questions=True,
                is_auto_compact=not force,
                max_output_tokens=compact_cfg.summary_max_output_tokens,
                compact_cfg=compact_cfg,
                responses_api=responses_api,
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
        await asyncio.to_thread(self.sync_memory_from_context, items)
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
