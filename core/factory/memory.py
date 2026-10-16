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

Levels. A chat's memory comes in up to three levels, nearest first
(:meth:`MemoryUpkeep.memory_levels`): the chat's own, its project's, and the
user's - the namespace memory always had, which holds in any project. Each is a
namespace of its own in the same file (AgentFactory.level_memory_manager); the
host says which levels a chat has (``memory_scopes``, set by web_chat.space).
Recall searches them together, a nearer level winning a close call
(:data:`LEVEL_BONUS`); the model that writes a turn's memories says whose each
one is, and it is stored there.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.compact import CompactMessage
from core.compact.compact_conversation import _call_llm_for_summary
from core.memory import MemoryCategory, MemoryLayer, MemoryRecord, MemoryScope
from core.memory.embeddings import DEFAULT_QUERY_INSTRUCTION, Embedder
from core.memory.llm_extract import EPISODE_KEY

logger = logging.getLogger("grid.agent_factory")

TOPICAL = (MemoryLayer.L1, MemoryLayer.L2)
#: A chat's memory levels, nearest first.
LEVELS = ("chat", "project", "user")
#: What a nearer level adds to a record's similarity when records compete for the prompt.
LEVEL_BONUS = {"chat": 0.04, "project": 0.02, "user": 0.0}
#: What each level is, for the model that files a turn's memories.
LEVEL_ABOUT = {
    "chat": "this chat's own memory",
    "project": "the project {title}",
    "user": "the user, in any project",
}
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

    # -- levels -----------------------------------------------------------------------

    def memory_level_scopes(self, context_id: str | None) -> dict[str, tuple[str, str]]:
        """The levels chat *context_id* has besides the user's: level -> (key, title)."""
        scopes = getattr(self, "memory_scopes", None)
        if scopes is None:
            return {}
        try:
            return {level: value for level, value in (scopes(context_id) or {}).items() if level in LEVELS and value}
        except Exception:  # noqa: BLE001 - a broken resolver leaves the user's memory
            logger.warning("Memory levels of %s unknown; the user's only", context_id, exc_info=True)
            return {}

    def memory_levels(self, context_id: str | None, *, force: bool = False) -> list[tuple[str, Any]]:
        """(level, manager) of each memory level chat *context_id* has, nearest first.

        The user's is the factory's own memory (``force``: even where the config
        leaves memory off, as :meth:`get_workspace_memory_manager`); none at all
        when it is unavailable.
        """
        user = self.get_workspace_memory_manager() if force else self.get_memory_manager()
        if user is None:
            return []
        levels: list[tuple[str, Any]] = []
        scopes = self.memory_level_scopes(context_id)
        for level in ("chat", "project"):
            if level in scopes:
                manager = self.level_memory_manager(level, scopes[level][0], force=force)
                if manager is not None:
                    levels.append((level, manager))
        levels.append(("user", user))
        return levels

    def memory_level_titles(self, context_id: str | None) -> dict[str, str]:
        """What each level of the chat is, in words, nearest first."""
        scopes = self.memory_level_scopes(context_id)
        titles = {}
        for level in LEVELS:
            if level == "user" or level in scopes:
                title = scopes.get(level, ("", ""))[1]
                titles[level] = LEVEL_ABOUT[level].format(title=f'"{title}"' if title else "")
        return titles

    async def memory_search_levels(
        self, levels: list[tuple[str, Any]], query: str, *, layers: tuple = TOPICAL, limit: int = 8,
        min_similarity: float | None = None,
    ) -> list[tuple[str, MemoryRecord, float | None]]:
        """Records of every level about *query*, best first: (level, record, similarity).

        One query embedding serves all levels; a nearer level wins a close call
        (:data:`LEVEL_BONUS`). Without an embedder only full-text matches count,
        nearer levels first.
        """
        if not query.strip() or not levels:
            return []
        embedder = self.memory_embedder()
        if embedder is not None and all(manager.vectors is not None for _, manager in levels):
            settings = self.config.get_memory_config().embeddings
            floor = settings.min_similarity if min_similarity is None else min_similarity
            try:
                for _level, manager in levels:
                    await self.memory_index(manager, embedder)
                vector = await embedder.embed_query(query[-QUERY_CHARS * 4:])
                found = []
                for level, manager in levels:
                    hits = await asyncio.to_thread(
                        manager.nearest, embedder.model, vector, layers=layers, limit=limit, min_similarity=floor)
                    found.extend((level, record, score) for record, score in hits)
                found.sort(key=lambda item: item[2] + LEVEL_BONUS.get(item[0], 0.0), reverse=True)
                return _distinct(found)[:limit]
            except Exception:
                logger.warning("Semantic memory search failed; using keywords", exc_info=True)
        found = []
        for level, manager in levels:
            records = await asyncio.to_thread(manager.keyword_hits, query, limit, tuple(layers))
            found.extend((level, record, None) for record in records)
        return _distinct(found)[:limit]

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
            levels = self.memory_levels(context_id)
            if not levels:
                return None
            user = levels[-1][1]
            query = self.memory_query(context_id, request if isinstance(request, str) else "")
            profile = await asyncio.to_thread(user.profile)
            topical = await self.memory_search_levels(levels, query, limit=settings.embeddings.max_records)
            # Whose each record is, said only where a chat has more than the user's.
            named = None
            if len(levels) > 1:
                named = {record.id: "user" for record in profile}
                named.update({record.id: level for level, record, _ in topical})
            return user.render_records([*profile, *(record for _, record, _ in topical)],
                                       settings.auto_inject_tokens, levels=named).content
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
            levels = self.memory_levels(context_id)
            if not levels:
                return []
            source_ref = f"chat:{context_id}" if context_id else None
            current = await asyncio.to_thread(chat_episode, levels, context_id)
            extraction = await self.extract_turn_with_model(
                levels, request, answer, agent_key, settings, source_ref=source_ref,
                titles=self.memory_level_titles(context_id),
                episode=current[1].content if current else None)
            if extraction is None:
                logger.info("Memory after the turn of %s: the model gave nothing usable", context_id)
                return []
            stored = await asyncio.to_thread(
                store_by_level, levels, extraction, source_ref=source_ref,
                exchange=(request, answer) if settings.snapshot_turns else None,
            )
            episode = await asyncio.to_thread(store_episode, levels, context_id, extraction.episode, current,
                                              source_ref=source_ref)
            if episode is not None:
                stored.append(episode)
            logger.info("Memory after the turn of %s: %s", context_id,
                        ", ".join(f"{r.layer.value} {r.category.value}" for r in stored) or "nothing to keep")
            embedder = self.memory_embedder()
            if embedder is not None and stored:
                for _level, manager in levels:
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
                                      source_ref: str | None = None,
                                      titles: dict[str, str] | None = None,
                                      episode: str | None = None) -> Any:
        """The model's :class:`~core.memory.llm_extract.Extraction` of one turn; None on failure.

        The model sees what is already known about the turn - the records closest
        in meaning and the latest ones from this chat - so it neither repeats them
        nor leaves those the turn disproved. *manager* is one memory, or the
        chat's levels - (level, manager), nearest first - with *titles* saying
        what each is; the model then also says whose each memory is. *episode* is
        the chat's current episode, which the model rewrites (:func:`store_episode`).
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
            levels = manager if isinstance(manager, list) else [("user", manager)]
            leveled = len(levels) > 1
            known = await self._known_for_extraction(levels, f"{request}\n{answer[:1500]}", source_ref)
            prompt = extraction_prompt(
                request, answer, [record for _, record in known],
                levels=(titles or {level: level for level, _ in levels}) if leveled else None,
                known_levels={record.id: level for level, record in known} if leveled else None,
                episode=episode,
            )
            text = ""
            for _attempt in range(2):  # an empty answer happens now and then; once more, then give up
                text = await asyncio.wait_for(_call_llm_for_summary(
                    [CompactMessage(role="system", content=SYSTEM_PROMPT)],
                    prompt, client, model, max_tokens=3000, responses_api=options,
                ), timeout=EXTRACTION_TIMEOUT)
                if text.strip():
                    break
            return parse_extraction(text, [record.id for _, record in known],
                                    levels=[level for level, _ in levels] if leveled else ())
        except Exception:
            logger.warning("Model memory extraction failed; nothing stored for this turn", exc_info=True)
            return None

    async def _known_for_extraction(self, levels: list[tuple[str, Any]], text: str,
                                    source_ref: str | None) -> list[tuple[str, MemoryRecord]]:
        """(level, record) the model should know of: the closest in meaning and this chat's latest."""
        layers = (MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3)
        similar = [(level, record) for level, record, _ in await self.memory_search_levels(
            levels, text, layers=layers, limit=10, min_similarity=0.3)]
        recent = []
        if source_ref:
            for level, manager in levels:
                records = await asyncio.to_thread(manager.live_records, layers)
                recent.extend((level, record) for record in records if record.source_ref == source_ref)
        own_episode = f"{EPISODE_KEY}{source_ref[5:]}" if source_ref and source_ref.startswith("chat:") else None
        known: dict[str, tuple[str, MemoryRecord]] = {}
        for level, record in (*similar, *recent[:5]):
            if own_episode and record.key == own_episode:
                continue  # given apart, as CURRENT EPISODE
            known.setdefault(record.id, (level, record))
        return list(known.values())


def chat_episode(levels: list[tuple[str, Any]], context_id: str | None) -> tuple[str, MemoryRecord] | None:
    """(level, record) of chat *context_id*'s episode, wherever it is kept; None: it has none yet."""
    if not context_id:
        return None
    key = f"{EPISODE_KEY}{context_id}"
    for level, manager in levels:
        for record in manager.live_records((MemoryLayer.L2,)):
            if record.key == key:
                return level, record
    return None


def store_episode(levels: list[tuple[str, Any]], context_id: str | None, text: str | None,
                  current: tuple[str, MemoryRecord] | None, *, source_ref: str | None = None,
                  ) -> MemoryRecord | None:
    """Keep chat *context_id*'s episode as *text* says it now: one L2 record per chat.

    It lives with the project when the chat has one (other chats of the project
    find it), else with the user - never in the chat's own level, which only that
    chat reads. The previous version is replaced, not kept beside it. None: no
    episode (the model gave none, or the text did not change).
    """
    if not context_id or not text or not levels:
        return None
    if current is not None and " ".join(current[1].content.split()) == text:
        return None
    have = dict(levels)
    level = "project" if "project" in have else "user"
    if level not in have:
        level = levels[-1][0]
    record = MemoryRecord(content=text, layer=MemoryLayer.L2, scope=MemoryScope.PROJECT,
                          category=MemoryCategory.NOTE, key=f"{EPISODE_KEY}{context_id}",
                          tags=("episode", "эпизод", "chat", "чат"), source_ref=source_ref, confidence=0.8)
    stored = have[level].remember(record)
    if current is not None and current[1].id != stored.id:
        dict(levels)[current[0]].forget(current[1].id)
    return stored


def _distinct(found: list) -> list:
    """*found* - (level, record, score), best first - without a record repeated in a farther level."""
    seen: set[str] = set()
    out = []
    for item in found:
        text = " ".join(item[1].content.casefold().split())
        if text in seen:
            continue
        seen.add(text)
        out.append(item)
    return out


def level_for(extracted: Any, levels: list[tuple[str, Any]]) -> str:
    """Where an extracted memory goes: the level it was given, if the chat has
    it; otherwise the user's for a preference, else the project's, else the user's."""
    have = {level for level, _ in levels}
    if extracted.level in have:
        return extracted.level
    if extracted.record.layer == MemoryLayer.L3:
        return "user"
    return "project" if "project" in have else "user"


def store_by_level(levels: list[tuple[str, Any]], extraction: Any, *, source_ref: str | None = None,
                   exchange: tuple[str, str] | None = None) -> list:
    """Store a turn's extraction, each memory in its level; the records stored.

    A record the turn showed wrong is removed from the level that holds it
    (only one extracted from a chat: MemoryManager.store_extracted). The
    exchange itself, when kept, is the chat's.
    """
    from core.memory.llm_extract import Extraction

    by_level: dict[str, list] = {level: [] for level, _ in levels}
    for item in extraction.memories:
        by_level[level_for(item, levels)].append(item)
    obsolete: dict[str, list] = {level: [] for level, _ in levels}
    for record_id in extraction.obsolete:
        for level, manager in levels:
            if manager.get(record_id) is not None:
                obsolete[level].append(record_id)
                break
    nearest = levels[0][0]
    stored = []
    for level, manager in levels:
        if not by_level[level] and not obsolete[level] and not (exchange and level == nearest):
            continue
        stored.extend(manager.store_extracted(
            Extraction(by_level[level], obsolete[level]), source_ref=source_ref,
            exchange=exchange if level == nearest else None,
        ))
    return stored
