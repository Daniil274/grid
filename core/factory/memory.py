"""The agents' long-term memory around a turn: what reaches the prompt, what is kept.

Mixed into AgentFactory (core.agent_factory); relies on self.config,
self.models, self.context_manager, get_memory_manager and
get_workspace_memory_manager.

Before a turn, :meth:`MemoryUpkeep.memory_prompt_section` picks what the agent
is told: the user's profile (L3) always, and the facts most similar in meaning
to the request - by embeddings (core.memory.embeddings), above a calibrated
similarity, never "the newest few". After an answered turn,
:meth:`MemoryUpkeep.remember_turn_later` has a model write down what the turn
taught (core.memory.llm_extract), in the background.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.compact import CompactMessage
from core.compact.compact_conversation import _call_llm_for_summary
from core.memory import MemoryLayer, MemoryRecord
from core.memory.embeddings import DEFAULT_QUERY_INSTRUCTION, Embedder

logger = logging.getLogger("grid.agent_factory")

TOPICAL = (MemoryLayer.L1, MemoryLayer.L2)
#: A request shorter than this ("Работай", "Continue") is searched together with
#: the user's previous messages: on its own it says nothing about the topic.
SHORT_REQUEST = 40
QUERY_CHARS = 1500
EXTRACTION_TIMEOUT = 180


class MemoryUpkeep:
    """Semantic recall into the prompt and model-written memories after each turn."""

    # -- embeddings ---------------------------------------------------------------

    def memory_embedder(self) -> Embedder | None:
        """The embedder of ``memory.embeddings``; None when it is off or cannot be built.

        Its client comes from this factory's model access: in the web chat the
        user's own credentials, metered like any other request.
        """
        settings = self.config.get_memory_config().embeddings
        if not settings.provider:
            return None
        key = (settings.provider, settings.model, settings.query_instruction, settings.batch_size)
        cached = self.__dict__.get("_memory_embedder")
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            provider = self.config.get_provider(settings.provider)
            client = self.models.make_client(
                api_key=self.models.access.api_key(self.config, settings.provider),
                base_url=provider.base_url,
                timeout=provider.timeout,
                max_retries=provider.max_retries,
                provider_key=settings.provider,
            )
            instruction = (DEFAULT_QUERY_INSTRUCTION if settings.query_instruction is None
                           else settings.query_instruction)
            embedder = Embedder(client, settings.model, query_instruction=instruction,
                                batch_size=settings.batch_size)
        except Exception:
            logger.warning("Memory embeddings unavailable; recall falls back to keywords", exc_info=True)
            embedder = None
        self.__dict__["_memory_embedder"] = (key, embedder)
        return embedder

    async def memory_index(self, manager: Any, embedder: Embedder) -> int:
        """Embed the records that have no vector yet (new, edited, or from before)."""
        pending = await asyncio.to_thread(manager.unembedded, embedder.model)
        if not pending:
            return 0
        vectors = await embedder.embed_documents([text for _, text in pending])
        return await asyncio.to_thread(
            manager.store_vectors, embedder.model, [(record, vector) for (record, _), vector in zip(pending, vectors)])

    async def memory_search(
        self, manager: Any, query: str, *, layers: tuple = TOPICAL, limit: int = 8,
        min_similarity: float | None = None,
    ) -> list[tuple[MemoryRecord, float | None]]:
        """Records about *query* by meaning, best first, with their similarity.

        Without an embedder (or when it fails) only records the full-text index
        literally matches are returned, with no score.
        """
        if not query.strip():
            return []
        embedder = self.memory_embedder()
        if embedder is not None and manager.vectors is not None:
            settings = self.config.get_memory_config().embeddings
            floor = settings.min_similarity if min_similarity is None else min_similarity
            try:
                await self.memory_index(manager, embedder)
                vector = await embedder.embed_query(query[-QUERY_CHARS * 4:])
                return await asyncio.to_thread(
                    manager.nearest, embedder.model, vector, layers=layers, limit=limit, min_similarity=floor)
            except Exception:
                logger.warning("Semantic memory search failed; using keywords", exc_info=True)
        records = await asyncio.to_thread(manager.keyword_hits, query, limit, tuple(layers))
        return [(record, None) for record in records]

    def memory_query(self, context_id: str | None, request: str) -> str:
        """What to search memory for: the request, with earlier ones when it is short."""
        request = (request or "").strip()
        if len(request) >= SHORT_REQUEST or not context_id:
            return request
        try:
            messages = self.context_manager.conversation_view(context_id)["messages"]
        except Exception:  # noqa: BLE001 - an unknown chat: the request alone
            return request
        earlier: list[str] = []
        for message in reversed(messages):
            if getattr(message, "role", None) != "user":
                continue
            text = message.get_text_content() if hasattr(message, "get_text_content") else ""
            if text and text.strip() != request:
                earlier.append(text.strip())
            if len(earlier) == 2 or sum(map(len, earlier)) >= QUERY_CHARS:
                break
        return "\n".join([*reversed(earlier), request])[-QUERY_CHARS:]

    async def memory_prompt_section(self, context_id: str | None, request: Any) -> str | None:
        """The ``<historical_memory>`` block for this turn; None when memory is not injected.

        The profile (L3) always; L1/L2 records only when semantically close to the
        request (``memory.embeddings.min_similarity``), at most ``max_records``.
        Never raises: a broken memory leaves the prompt without it.
        """
        try:
            settings = self.config.get_memory_config()
            if not settings.enabled or not settings.auto_inject:
                return None
            manager = self.get_memory_manager()
            if manager is None:
                return None
            query = self.memory_query(context_id, request if isinstance(request, str) else "")
            profile = await asyncio.to_thread(manager.profile)
            topical = await self.memory_search(manager, query, limit=settings.embeddings.max_records)
            return manager.render_records([*profile, *(record for record, _ in topical)],
                                          settings.auto_inject_tokens).content
        except Exception:
            logger.warning("Memory for the prompt unavailable", exc_info=True)
            return None

    # -- after the turn -------------------------------------------------------------

    def remember_turn(self, context_id: str | None, request: Any, answer: Any) -> list:
        """Heuristic distillation of a turn (core.memory.distill), for setups without a model.

        Runs when ``memory.enabled`` and ``extract_on_turn`` are on. Storing is
        idempotent, so nothing is saved twice. Never raises.
        """
        try:
            settings = self._turn_memory_settings(request, answer)
            if settings is None:
                return []
            manager = self.get_memory_manager()
            if manager is None:
                return []
            return manager.extract_and_store_from_messages(
                [{"role": "user", "content": request}, {"role": "assistant", "content": answer}],
                source_ref=f"chat:{context_id}" if context_id else None,
                snapshot=bool(settings.snapshot_turns),
            )
        except Exception:
            logger.warning("Memory extraction after the turn failed; the turn is unaffected", exc_info=True)
            return []

    def _turn_memory_settings(self, request: Any, answer: Any) -> Any:
        config = getattr(self, "config", None)
        if config is None or not hasattr(config, "get_memory_config"):
            return None
        settings = config.get_memory_config()
        if not settings.enabled or not settings.extract_on_turn:
            return None
        if not isinstance(request, str) or not isinstance(answer, str) or not request.strip() or not answer.strip():
            return None
        return settings

    def remember_turn_later(self, context_id: str | None, request: Any, answer: Any,
                            agent_key: str | None = None) -> asyncio.Task | None:
        """Run :meth:`remember_turn_async` in the background: the turn does not wait on it."""
        try:
            task = asyncio.get_running_loop().create_task(
                self.remember_turn_async(context_id, request, answer, agent_key))
        except RuntimeError:
            return None
        tasks = self.__dict__.setdefault("_memory_tasks", set())
        tasks.add(task)  # held until done: the loop keeps only weak references
        task.add_done_callback(tasks.discard)
        return task

    async def remember_turn_async(self, context_id: str | None, request: Any, answer: Any,
                                  agent_key: str | None = None) -> list:
        """Store what a finished turn taught, written by a model. Never raises.

        With ``memory.extract_with_model`` a model reads the turn together with the
        related known memories and stores the few lasting facts it finds, removing
        extracted records the turn proved wrong. A failed call stores nothing -
        better no memory than a fragment. Without a model the heuristic
        :meth:`remember_turn` runs.
        """
        try:
            settings = self._turn_memory_settings(request, answer)
            if settings is None:
                return []
            if not settings.extract_with_model or getattr(self, "models", None) is None:
                return await asyncio.to_thread(self.remember_turn, context_id, request, answer)
            manager = self.get_memory_manager()
            if manager is None:
                return []
            source_ref = f"chat:{context_id}" if context_id else None
            extraction = await self.extract_turn_with_model(manager, request, answer, agent_key, settings,
                                                            source_ref=source_ref)
            if extraction is None:
                return []
            stored = await asyncio.to_thread(
                manager.store_extracted, extraction, source_ref=source_ref,
                exchange=(request, answer) if settings.snapshot_turns else None,
            )
            embedder = self.memory_embedder()
            if embedder is not None and stored:
                try:
                    await self.memory_index(manager, embedder)
                except Exception:
                    logger.warning("Embedding new memories failed; retried at the next search", exc_info=True)
            return stored
        except Exception:
            logger.warning("Memory extraction after the turn failed; the turn is unaffected", exc_info=True)
            return []

    async def extract_turn_with_model(self, manager: Any, request: str, answer: str,
                                      agent_key: str | None, settings: Any, *,
                                      source_ref: str | None = None) -> Any:
        """The model's :class:`~core.memory.llm_extract.Extraction` of one turn; None on failure.

        The model sees what is already known about the turn - the records closest
        in meaning and the latest ones from this chat - so it neither repeats them
        nor leaves those the turn disproved.
        """
        from core.memory.llm_extract import (
            SYSTEM_PROMPT,
            extraction_prompt,
            parse_extraction,
        )

        models = getattr(self, "models", None)
        if models is None:
            return None
        try:
            model_key = settings.extract_model
            client, model, options = (
                models.model_request(model_key) if model_key else models.compact_request(agent_key))
            if client is None or not model:
                return None
            known = await self._known_for_extraction(manager, f"{request}\n{answer[:1500]}", source_ref)
            text = await asyncio.wait_for(_call_llm_for_summary(
                [CompactMessage(role="system", content=SYSTEM_PROMPT)],
                extraction_prompt(request, answer, known),
                client, model, max_tokens=2000, responses_api=options,
            ), timeout=EXTRACTION_TIMEOUT)
            return parse_extraction(text, [record.id for record in known])
        except Exception:
            logger.warning("Model memory extraction failed; nothing stored for this turn", exc_info=True)
            return None

    async def _known_for_extraction(self, manager: Any, text: str, source_ref: str | None) -> list:
        layers = (MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3)
        similar = [record for record, _ in await self.memory_search(
            manager, text, layers=layers, limit=10, min_similarity=0.3)]
        recent = []
        if source_ref:
            recent = [record for record in await asyncio.to_thread(manager.live_records, layers)
                      if record.source_ref == source_ref][:5]
        known: dict[str, MemoryRecord] = {}
        for record in (*similar, *recent):
            known.setdefault(record.id, record)
        return list(known.values())
