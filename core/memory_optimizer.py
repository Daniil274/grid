import json
import logging
import asyncio
import sqlite3
import time
from typing import Optional, List, Dict, Any

from core.memory_store import MemoryStore
from core.config import Config

logger = logging.getLogger(__name__)
verbose_logger = logging.getLogger("grid.verbose")

try:
    from agents.tracing import trace, custom_span
    _TRACING_AVAILABLE = True
except Exception:
    trace = None
    custom_span = None
    _TRACING_AVAILABLE = False

class MemoryOptimizer:
    """
    Asynchronous optimizer for memory entries.
    Uses LLM to extract entities, summaries, and connections,
    and performs periodic consolidation into insights.
    
    Configured via 'memory_optimizer' agent in config.yaml.
    """
    
    def __init__(self, memory_store: MemoryStore, config: Config, agent_factory: Any):
        self.store = memory_store
        self.config = config
        self.agent_factory = agent_factory
        self.agent_key = "memory_optimizer"

    def _get_client_and_model(self):
        """Get the AsyncOpenAI client and model name from the configured agent."""
        try:
            agent_config = self.config.get_agent(self.agent_key)
            model_name = agent_config.model
            client, resolved_model = self.agent_factory.get_openai_client_for_model(model_name)
            return client, resolved_model
        except Exception as e:
            logger.warning(f"Could not load memory_optimizer agent config: {e}")
            return None, None

    def _get_prompt(self, template_key: str, default_prompt: str) -> str:
        """Get prompt from config or use default."""
        try:
            return self.config.get_prompt_template(template_key)
        except Exception:
            return default_prompt

    async def process_new_entry(self, entry_id: int):
        """
        Process a newly added memory entry:
        1. Extract summary and entities.
        2. Find connections to existing memories.
        3. Trigger consolidation if needed.
        """
        t0 = time.monotonic()
        logger.debug("MemoryOptimizer.process_new_entry start entry_id=%s", entry_id)
        if _TRACING_AVAILABLE and trace is not None and custom_span is not None:
            trace_ctx = trace("MemoryOptimizer", metadata={"entry_id": entry_id})
            trace_ctx.start()
            span_ctx = custom_span("process_new_entry", data={"entry_id": entry_id})
            span_ctx.start()
        else:
            trace_ctx = span_ctx = None

        try:
            client, model = self._get_client_and_model()
            if not client:
                logger.warning("MemoryOptimizer: No LLM client available, skipping optimization.")
                return

            # Fetch the entry
            with self.store._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM memory WHERE id = ?", (entry_id,))
                row = cursor.fetchone()
                if not row:
                    logger.debug("MemoryOptimizer.process_new_entry entry_id=%s not found, skip", entry_id)
                    return
                entry = dict(row)

            # Skip if already processed or if it's an insight
            if entry["type"] == "insight" or entry.get("summary"):
                logger.debug("MemoryOptimizer.process_new_entry entry_id=%s already processed or insight, skip", entry_id)
                return

            verbose_logger.debug(
                "MemoryOptimizer.process_new_entry entry_id=%s content_len=%s type=%s",
                entry_id, len(entry.get("content", "")), entry.get("type"),
            )

            # Get extraction prompt
            default_extract = """Analyze the following memory entry and extract a concise summary and key entities.
Also, classify the entities into categories like Person, Organization, Technology, Concept, etc.

Memory Content:
{content}

Respond ONLY with a JSON object in the following format:
{
    "summary": "1-2 sentence summary of the core fact",
    "entities": ["Entity1", "Entity2", "Entity3"]
}"""
            prompt_template = self._get_prompt("memory_extract_prompt", default_extract)
            prompt = prompt_template.replace("{content}", entry['content'])

            t_llm = time.monotonic()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            duration_llm_ms = (time.monotonic() - t_llm) * 1000

            result = json.loads(response.choices[0].message.content)
            summary = result.get("summary", entry["content"][:100])
            entities = json.dumps(result.get("entities", []))

            # Update the entry
            self.store.update(
                entry_id=entry_id,
                summary=summary,
                entities=entities
            )
            duration_total_ms = (time.monotonic() - t0) * 1000
            logger.info(
                "🧠 Optimized memory #%s: %s (llm_ms=%.0f total_ms=%.0f)",
                entry_id, summary[:80] + ("..." if len(summary) > 80 else ""),
                duration_llm_ms, duration_total_ms,
            )
            verbose_logger.debug(
                "MemoryOptimizer.process_new_entry entry_id=%s summary=%s entities=%s llm_ms=%.0f",
                entry_id, summary, result.get("entities", []), duration_llm_ms,
            )

            # Trigger consolidation check
            await self.consolidate_recent(
                user_id=entry.get("user_id"),
                session_id=entry.get("session_id"),
                agent_id=entry.get("agent_id")
            )
        except sqlite3.DatabaseError as e:
            logger.error(
                "❌ Error optimizing memory #%s: database error (%s). "
                "If you see 'database disk image is malformed', restart the app: MemoryStore will run integrity_check and recreate the DB if needed. Corrupted file will be saved as memory.db.corrupted.",
                entry_id,
                e,
            )
            if span_ctx is not None:
                if hasattr(span_ctx, "span_data") and span_ctx.span_data is not None:
                    span_ctx.span_data.data = span_ctx.span_data.data or {}
                    span_ctx.span_data.data["error"] = str(e)
            # Do not re-raise: skip this optimization, let the rest of the pipeline continue
        except Exception as e:
            logger.error("❌ Error optimizing memory #%s: %s", entry_id, e, exc_info=True)
            verbose_logger.debug("MemoryOptimizer.process_new_entry entry_id=%s error=%s", entry_id, e, exc_info=True)
            if span_ctx is not None:
                if hasattr(span_ctx, "span_data") and span_ctx.span_data is not None:
                    span_ctx.span_data.data = span_ctx.span_data.data or {}
                    span_ctx.span_data.data["error"] = str(e)
        finally:
            if span_ctx is not None:
                try:
                    span_ctx.finish()
                except Exception:
                    pass
            if trace_ctx is not None:
                try:
                    trace_ctx.finish()
                except Exception:
                    pass

    async def consolidate_recent(self, user_id: Optional[str], session_id: Optional[str], agent_id: Optional[str], batch_size: int = 5):
        """
        Consolidate recent short_term memories into a high-level insight.
        """
        t0 = time.monotonic()
        logger.debug(
            "MemoryOptimizer.consolidate_recent start user_id=%s session_id=%s agent_id=%s batch_size=%s",
            user_id, session_id, agent_id, batch_size,
        )
        if _TRACING_AVAILABLE and custom_span is not None:
            try:
                from agents.tracing import get_current_trace
                parent = get_current_trace()
            except Exception:
                parent = None
            if parent is None and trace is not None:
                trace_ctx = trace("MemoryOptimizer.consolidate", metadata={"user_id": user_id, "agent_id": agent_id})
                trace_ctx.start()
            else:
                trace_ctx = None
            span_ctx = custom_span(
                "consolidate_recent",
                data={"user_id": user_id, "session_id": session_id, "agent_id": agent_id, "batch_size": batch_size},
            )
            span_ctx.start()
        else:
            trace_ctx = span_ctx = None

        try:
            client, model = self._get_client_and_model()
            if not client:
                return

            # Fetch recent unconsolidated short_term memories
            recent = self.store.search(
                type="short_term",
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                limit=batch_size
            )

            if len(recent) < batch_size:
                logger.debug(
                    "MemoryOptimizer.consolidate_recent skip: only %s recent (need %s)",
                    len(recent), batch_size,
                )
                return

            verbose_logger.debug(
                "MemoryOptimizer.consolidate_recent user_id=%s recent_ids=%s",
                user_id, [m.id for m in recent],
            )

            # Prepare content for consolidation
            memories_text = "\n".join([f"ID {m.id}: {m.content}" for m in recent])
            source_ids = [m.id for m in recent]

            default_consolidate = """Analyze the following recent memory entries and synthesize them into a single high-level insight or pattern.
If they are unrelated, respond with {"insight": null}.
Otherwise, provide a consolidated insight and list the source IDs.

Memories:
{content}

Respond ONLY with a JSON object in the following format:
{
    "insight": "High-level summary or pattern discovered across these memories",
    "entities": ["Entity1", "Entity2"],
    "connections": [{"from_id": 1, "to_id": 2, "relationship": "depends on"}]
}"""
            prompt_template = self._get_prompt("memory_consolidate_prompt", default_consolidate)
            prompt = prompt_template.replace("{content}", memories_text)

            t_llm = time.monotonic()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            duration_llm_ms = (time.monotonic() - t_llm) * 1000

            result = json.loads(response.choices[0].message.content)
            insight_text = result.get("insight")

            if insight_text:
                entities = json.dumps(result.get("entities", []))
                connections = json.dumps(result.get("connections", []))

                # Save the insight
                insight_id = self.store.save(
                    content=insight_text,
                    type="insight",
                    summary=insight_text[:100],
                    entities=entities,
                    connections=connections,
                    source_ids=json.dumps(source_ids),
                    user_id=user_id,
                    session_id=None,
                    agent_id=agent_id,
                    importance=0.8
                )

                # Archive the original short_term memories
                for m_id in source_ids:
                    self.store.update(entry_id=m_id, is_archived=True)

                duration_total_ms = (time.monotonic() - t0) * 1000
                logger.info(
                    "🔄 Consolidated %s memories into insight #%s (llm_ms=%.0f total_ms=%.0f)",
                    len(source_ids), insight_id, duration_llm_ms, duration_total_ms,
                )
                verbose_logger.debug(
                    "MemoryOptimizer.consolidate_recent insight_id=%s source_ids=%s insight=%s",
                    insight_id, source_ids, insight_text[:200],
                )
                if span_ctx is not None and hasattr(span_ctx, "span_data") and span_ctx.span_data is not None:
                    span_ctx.span_data.data = span_ctx.span_data.data or {}
                    span_ctx.span_data.data["insight_id"] = insight_id
                    span_ctx.span_data.data["source_count"] = len(source_ids)
            else:
                logger.debug("MemoryOptimizer.consolidate_recent LLM returned no insight (unrelated memories)")
        except Exception as e:
            logger.error("❌ Error consolidating memories: %s", e, exc_info=True)
            verbose_logger.debug("MemoryOptimizer.consolidate_recent error=%s", e, exc_info=True)
            if span_ctx is not None and hasattr(span_ctx, "span_data") and span_ctx.span_data is not None:
                span_ctx.span_data.data = span_ctx.span_data.data or {}
                span_ctx.span_data.data["error"] = str(e)
        finally:
            if span_ctx is not None:
                try:
                    span_ctx.finish()
                except Exception:
                    pass
            if trace_ctx is not None:
                try:
                    trace_ctx.finish()
                except Exception:
                    pass
