"""The review agents at work: the context-review system on one review's workbench.

The analysis of a review is a conversation with the reviewer agent
(examples/context-review), kept in that review's workbench
(web_chat.review.workbench) with the agents' SDK sessions beside all the
workbenches. An admin starts it and asks follow-up questions on the review
page; each question is one turn, run in the background while the page polls
:meth:`ReviewAgents.state`.

One turn runs at a time per review, and at most ``max_running`` across the
server: the agents use the operator's model keys. The first turn moves a new
review to ``in_review``; a turn that leaves proposals moves it to
``proposed``. The agents read the workbench and write proposals, nothing else
(examples/context-review/config.yaml).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from core.context import ContextManager
from web_chat.review.evidence import PROJECT_ROOT
from web_chat.review.store import ReviewStore
from web_chat.review.workbench import Workbench

logger = logging.getLogger("grid.web_chat.review.agents")

CONFIG_PATH = PROJECT_ROOT / "examples" / "context-review" / "config.yaml"
AGENT = "reviewer"
#: The conversation's id inside its workbench's own ContextManager.
CONVERSATION = "review"
FIRST_REQUEST = (
    "Разбери этот случай: установи, почему агент ответил не так, докажи причину "
    "ссылками на case/ и source/ и запиши предложения через propose_change."
)


class AnalysisBusy(RuntimeError):
    """A turn of this review's analysis is running already."""


class ReviewAgents:
    """Runs and records the analysis of reviews."""

    def __init__(
        self,
        store: ReviewStore,
        root: Path,
        *,
        config_path: Path = CONFIG_PATH,
        max_running: int = 2,
        build_factory: Optional[Callable[[Workbench, ContextManager], Any]] = None,
    ) -> None:
        """*root* holds the workbenches (``root/work/<id>``) and the agents'
        sessions; *build_factory* makes the AgentFactory of one turn (tests)."""
        self.store = store
        self.root = root
        self._config_path = config_path
        self._slots = asyncio.Semaphore(max_running)
        self._build_factory = build_factory or self._agent_factory
        self._turns: dict[str, asyncio.Task] = {}
        self._errors: dict[str, str] = {}

    def workbench(self, review_id: str) -> Workbench:
        if not review_id.isalnum():
            raise ValueError(f"Not a review id: {review_id!r}")
        return Workbench(self.root / "work" / review_id)

    def running(self, review_id: str) -> bool:
        turn = self._turns.get(review_id)
        return turn is not None and not turn.done()

    async def ask(self, review_id: str, message: Optional[str] = None) -> None:
        """Start a turn of *review_id*'s analysis; the first by default asks for the review.

        Raises LookupError for an unknown review and AnalysisBusy while a turn runs.
        """
        if self.running(review_id):
            raise AnalysisBusy("The review agents are still working on this review.")
        review = self.store.get(review_id)
        if review is None:
            raise LookupError("Review not found")
        workbench = self.workbench(review_id)
        if not workbench.ready:
            evidence = self.store.evidence(review_id) or {}
            await asyncio.to_thread(workbench.prepare, review.to_dict(), evidence)
        if review.status == "new":
            self.store.set_status(review_id, "in_review")
        self._errors.pop(review_id, None)
        self._turns[review_id] = asyncio.get_running_loop().create_task(
            self._turn(review_id, workbench, (message or "").strip() or FIRST_REQUEST),
            name=f"review-{review_id}",
        )

    def state(self, review_id: str) -> dict[str, Any]:
        """The analysis as the page shows it: the conversation, proposals, whether a turn runs."""
        workbench = self.workbench(review_id)
        return {
            "running": self.running(review_id),
            "error": self._errors.get(review_id),
            "messages": _conversation(workbench.root / "conversation.json"),
            "proposals": workbench.proposals(),
        }

    async def close(self) -> None:
        turns = [turn for turn in self._turns.values() if not turn.done()]
        for turn in turns:
            turn.cancel()
        await asyncio.gather(*turns, return_exceptions=True)

    # -- one turn --------------------------------------------------------------------
    async def _turn(self, review_id: str, workbench: Workbench, message: str) -> None:
        proposed_before = len(workbench.proposals())
        async with self._slots:
            manager = ContextManager(persist_path=str(workbench.root / "conversation.json"))
            manager.ensure_context(CONVERSATION)
            factory = self._build_factory(workbench, manager)
            try:
                await factory.run_agent(AGENT, message, context_id=CONVERSATION)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Review %s: the analysis turn failed", review_id)
                self._errors[review_id] = f"{type(exc).__name__}: {exc}"
            finally:
                await factory.cleanup()
        if len(workbench.proposals()) > proposed_before:
            self.store.set_status(review_id, "proposed")

    def _agent_factory(self, workbench: Workbench, manager: ContextManager) -> Any:
        from core.agent_factory import AgentFactory
        from core.config.config import Config

        return AgentFactory(
            config=Config(str(self._config_path), str(workbench.root)),
            working_directory=str(workbench.root),
            context_manager=manager,
            session_db_path=str(self.root / "agent_sessions.db"),
            logs_directory=str(self.root / "logs"),
        )


def _conversation(path: Path) -> list[dict[str, Any]]:
    """The analysis conversation, read from its file without opening a
    ContextManager (that could write the file a running turn is writing)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    history = ((data.get("contexts") or {}).get(CONVERSATION) or {}).get("conversation_history") or []
    return [
        {"role": message.get("role"), "content": _text(message.get("content")), "timestamp": message.get("timestamp")}
        for message in history
        if message.get("role") in ("user", "assistant") and (message.get("metadata") or {}).get("kind") != "tool_result"
    ]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = (part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") in ("text", "input_text"))
        return "\n".join(parts)
    return ""
