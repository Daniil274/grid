"""The review agents at work: the context-review system on one review's workbench.

The analysis of a review is a conversation with the reviewer agent
(examples/context-review), kept in that review's workbench
(web_chat.review.workbench) together with the agents' SDK sessions, so no
review sees another's. An admin starts it and asks follow-up questions on the review
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
import time
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


class ReviewProgress:
    """Bounded activity summary for the polling view; no tool arguments or outputs."""

    def __init__(self):
        self.tool_calls = 0
        self.last_event = "Waiting for model"
        self.updated_at = time.time()

    def handle_event(self, event, *, agent_key=None):
        from core.run_stream import is_output_delta, tool_event_info
        name = getattr(event, "name", None)
        if name in {"tool_called", "tool_output"}:
            tool = tool_event_info(event.item).get("tool_name") or "tool"
            if name == "tool_called":
                self.tool_calls += 1
            self.last_event = f"{agent_key or AGENT}: {tool} — {'started' if name == 'tool_called' else 'finished'}"
            self.updated_at = time.time()
        data = getattr(event, "data", None)
        if data is not None:
            self.updated_at = time.time()
            if is_output_delta(getattr(data, "type", None)):
                self.last_event = f"{agent_key or AGENT}: writing response"
                return getattr(data, "delta", None)
        return None

    def snapshot(self):
        return {"tool_calls": self.tool_calls, "last_event": self.last_event, "updated_at": self.updated_at}


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
        logs; *build_factory* makes the AgentFactory of one turn (tests)."""
        self.store = store
        self.root = root
        self._config_path = config_path
        self._slots = asyncio.Semaphore(max_running)
        self._build_factory = build_factory or self._agent_factory
        self._turns: dict[str, asyncio.Task] = {}
        self._errors: dict[str, str] = {}
        self._progress: dict[str, ReviewProgress] = {}
        self._asking = asyncio.Lock()

    def workbench(self, review_id: str) -> Workbench:
        if not review_id.isalnum():
            raise ValueError(f"Not a review id: {review_id!r}")
        return Workbench(self.root / "work" / review_id)

    def running(self, review_id: str) -> bool:
        turn = self._turns.get(review_id)
        return turn is not None and not turn.done()

    @property
    def busy(self) -> bool:
        return self._asking.locked() or any(not task.done() for task in self._turns.values())

    async def ask(self, review_id: str, message: Optional[str] = None) -> None:
        """Start a turn of *review_id*'s analysis; the first by default asks for the review.

        Raises LookupError for an unknown review and AnalysisBusy while a turn runs.
        """
        # One ask at a time: preparing the workbench awaits, and a second ask in
        # between would otherwise start a second turn of the same review.
        async with self._asking:
            await self._ask(review_id, message)

    async def _ask(self, review_id: str, message: Optional[str]) -> None:
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
        self._progress[review_id] = ReviewProgress()
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
            "progress": self._progress[review_id].snapshot() if review_id in self._progress else None,
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
                await factory.run_agent(AGENT, message, context_id=CONVERSATION,
                                        stream=True, stream_observer=self._progress[review_id])
                interruption = manager.pending_interruption(CONVERSATION)
                if interruption is not None:
                    self._errors[review_id] = interruption.summary()
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
            # In the workbench: the session id is the same for every review
            # (agent and conversation are), so one store for all would mix them.
            session_db_path=str(workbench.root / "agent_sessions.db"),
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
