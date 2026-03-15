import json
import logging
import asyncio
import sqlite3
import time
from datetime import datetime, timedelta
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

        self.consolidation_batch_size = self.config.get('memory_optimizer.consolidation_batch_size', 5)
        self.consolidation_trigger = self.config.get('memory_optimizer.consolidation_trigger', 'on_save')
        self.consolidation_interval_seconds = self.config.get('memory_optimizer.consolidation_interval_seconds', 3600)
        self.min_short_term_age_hours = self.config.get('memory_optimizer.min_short_term_age_hours', 1)
        self.ttl_cleanup_interval_seconds = self.config.get('memory_optimizer.ttl_cleanup_interval_seconds', 3600)
        
        # DB Size Management
        self.max_db_size_mb = self.config.get('memory_optimizer.max_db_size_mb', 100)
        self.cleanup_strategy = self.config.get('memory_optimizer.cleanup_strategy', 'balanced')
        self.cleanup_on_startup = self.config.get('memory_optimizer.cleanup_on_startup', False)
        self.vacuum_after_cleanup = self.config.get('memory_optimizer.vacuum_after_cleanup', True)

        self.periodic_task = None
        self._periodic_running = False
        if self.consolidation_trigger == 'periodic':
            self._periodic_running = True
            self.periodic_task = asyncio.create_task(self._periodic_loop())
        
        # TTL cleanup loop - only start if there's a running event loop
        self.ttl_cleanup_task = None
        self._ttl_cleanup_running = False
        try:
            loop = asyncio.get_running_loop()
            self._ttl_cleanup_running = True
            self.ttl_cleanup_task = asyncio.create_task(self._ttl_cleanup_loop())
            
            # Run startup cleanup if enabled
            if self.cleanup_on_startup:
                asyncio.create_task(self._run_startup_cleanup())
        except RuntimeError:
            # No running event loop - TTL cleanup will need to be started manually
            pass

    async def trigger_consolidation(self, user_id: Optional[str] = None, session_id: Optional[str] = None, agent_id: Optional[str] = None):
        """
        Manually trigger memory consolidation.
        This method is used for the 'manual' trigger mode or for explicit consolidation calls.
        """
        logger.info("MemoryOptimizer.trigger_consolidation called (manual trigger) user_id=%s session_id=%s agent_id=%s", user_id, session_id, agent_id)
        await self.consolidate_recent(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            batch_size=self.consolidation_batch_size
        )

    async def _periodic_loop(self):
        """
        Periodic consolidation loop - runs when consolidation_trigger is 'periodic'.
        Consolidates memories every consolidation_interval_seconds.
        """
        logger.info("MemoryOptimizer._periodic_loop started with interval=%s seconds", self.consolidation_interval_seconds)
        while self._periodic_running:
            try:
                await asyncio.sleep(self.consolidation_interval_seconds)
                logger.debug("MemoryOptimizer._periodic_loop triggering consolidation")
                await self.trigger_consolidation()
            except asyncio.CancelledError:
                logger.info("MemoryOptimizer._periodic_loop cancelled")
                break
            except Exception as e:
                logger.error("MemoryOptimizer._periodic_loop error: %s", e)

    async def stop_periodic_loop(self):
        """Stop the periodic consolidation loop."""
        if self.periodic_task:
            self._periodic_running = False
            self.periodic_task.cancel()
            try:
                await self.periodic_task
            except asyncio.CancelledError:
                pass
            logger.info("MemoryOptimizer.periodic_loop stopped")
        
        # Stop TTL cleanup loop
        if self.ttl_cleanup_task:
            self._ttl_cleanup_running = False
            self.ttl_cleanup_task.cancel()
            try:
                await self.ttl_cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("MemoryOptimizer.ttl_cleanup_loop stopped")

    async def _ttl_cleanup_loop(self):
        """
        Periodic TTL cleanup loop.
        Archives expired TTL entries every ttl_cleanup_interval_seconds.
        Also checks DB size and runs cleanup if needed.
        """
        logger.info("MemoryOptimizer._ttl_cleanup_loop started with interval=%s seconds", self.ttl_cleanup_interval_seconds)
        while self._ttl_cleanup_running:
            try:
                await asyncio.sleep(self.ttl_cleanup_interval_seconds)
                logger.debug("MemoryOptimizer._ttl_cleanup_loop triggering cleanup")
                
                # 1. TTL cleanup
                count = self.store.cleanup_expired()
                if count > 0:
                    logger.info("🧹 TTL cleanup archived %d entries", count)
                
                # 2. Check DB size and run cleanup if needed
                await self._check_and_cleanup_db_size()
                
            except asyncio.CancelledError:
                logger.info("MemoryOptimizer._ttl_cleanup_loop cancelled")
                break
            except Exception as e:
                logger.error("MemoryOptimizer._ttl_cleanup_loop error: %s", e)
    
    async def _run_startup_cleanup(self):
        """
        Run cleanup on startup if cleanup_on_startup is enabled.
        """
        try:
            logger.info("🚀 Running startup cleanup...")
            result = self.store.run_cleanup(strategy=self.cleanup_strategy)
            total = sum(result.values())
            if total > 0:
                logger.info("🧹 Startup cleanup: %s", result)
                
                # Vacuum after cleanup if enabled
                if self.vacuum_after_cleanup:
                    vacuum_result = self.store.vacuum_db()
                    if vacuum_result["success"]:
                        logger.info("✅ Startup vacuum: reclaimed %.2f MB", vacuum_result["reclaimed_mb"])
        except Exception as e:
            logger.error("❌ Startup cleanup failed: %s", e)
    
    async def _check_and_cleanup_db_size(self):
        """
        Check DB size and run cleanup if threshold exceeded.
        Called periodically by _ttl_cleanup_loop.
        """
        try:
            check = self.store.check_cleanup_needed(threshold_mb=self.max_db_size_mb)
            
            if check["needed"]:
                logger.warning(
                    "⚠️ DB size %.2f MB exceeds threshold %.2f MB, running cleanup",
                    check["current_size_mb"], self.max_db_size_mb
                )
                
                result = self.store.run_cleanup(strategy=self.cleanup_strategy)
                logger.info("🧹 Automatic cleanup result: %s", result)
                
                # Vacuum after cleanup if enabled
                if self.vacuum_after_cleanup:
                    vacuum_result = self.store.vacuum_db()
                    if vacuum_result["success"]:
                        logger.info(
                            "✅ Auto vacuum: %.2f MB -> %.2f MB (reclaimed %.2f MB)",
                            vacuum_result["size_before_mb"],
                            vacuum_result["size_after_mb"],
                            vacuum_result["reclaimed_mb"]
                        )
        except Exception as e:
            logger.error("❌ DB size check/cleanup failed: %s", e)
    
    def get_db_status(self) -> Dict[str, Any]:
        """
        Get current DB status and cleanup recommendations.
        Useful for monitoring and manual intervention.
        
        Returns:
            Dict with DB stats and cleanup recommendations
        """
        check = self.store.check_cleanup_needed(threshold_mb=self.max_db_size_mb)
        stats = self.store.get_stats()
        
        return {
            "db_size_mb": check["current_size_mb"],
            "threshold_mb": self.max_db_size_mb,
            "exceeds_threshold": check["needed"],
            "total_entries": stats["total_entries"],
            "archived_entries": check["archived_count"],
            "expired_ttl_count": check["expired_ttl_count"],
            "low_importance_count": check["low_importance_count"],
            "cleanup_strategy": self.cleanup_strategy,
            "recommendation": self._get_cleanup_recommendation(check)
        }
    
    def _get_cleanup_recommendation(self, check: Dict[str, Any]) -> str:
        """
        Generate cleanup recommendation based on current state.
        """
        if not check["needed"]:
            return "No cleanup needed"
        
        if check["archived_count"] > 100:
            return f"Run cleanup with strategy='{self.cleanup_strategy}' to delete {check['archived_count']} archived entries"
        
        if check["expired_ttl_count"] > 50:
            return f"Run cleanup to archive {check['expired_ttl_count']} expired TTL entries"
        
        return f"DB size {check['current_size_mb']:.1f}MB exceeds threshold. Consider running cleanup."

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

    def find_related_entries(self, entities: List[str], limit_per_entity: int = 5) -> List[Dict[str, Any]]:
        """
        Find existing memory entries related to the given entities.
        Uses get_entity_graph from MemoryStore for each entity.
        
        Args:
            entities: List of entity names to search for
            limit_per_entity: Max entries to return per entity
            
        Returns:
            List of dicts with entry_id, content, summary, entities, matching_entity
        """
        if not entities:
            return []
        
        related = []
        seen_ids = set()
        
        for entity in entities[:10]:  # Limit to 10 entities to avoid overload
            try:
                entries = self.store.get_entity_graph(entity, limit=limit_per_entity)
                for entry in entries:
                    if entry.id not in seen_ids:
                        seen_ids.add(entry.id)
                        related.append({
                            "entry_id": entry.id,
                            "content": entry.content[:200] + ("..." if len(entry.content) > 200 else ""),
                            "summary": entry.summary or "",
                            "entities": json.loads(entry.entities or "[]"),
                            "matching_entity": entity
                        })
            except Exception as e:
                logger.warning("find_related_entries error for entity '%s': %s", entity, e)
        
        return related

    async def _extract_connections_async(
        self,
        entry_id: int,
        content: str,
        summary: str,
        entities: List[str],
        client: Any,
        model: str
    ) -> None:
        """
        Asynchronously extract connections between this entry and existing memories.
        Uses LLM to identify meaningful relationships.
        
        Args:
            entry_id: ID of the new entry
            content: Full content of the entry
            summary: Summary of the entry
            entities: List of extracted entities
            client: AsyncOpenAI client
            model: Model name
        """
        try:
            # Find related entries based on entities
            related_entries = self.find_related_entries(entities, limit_per_entity=3)
            
            if not related_entries:
                logger.debug("No related entries found for entry_id=%s, skipping connection extraction", entry_id)
                return
            
            # Prepare context for LLM
            related_context = "\n".join([
                f"ID {r['entry_id']}: {r['summary'] or r['content']}"
                for r in related_entries[:10]  # Limit to 10 candidates
            ])
            
            # Build prompt for connection extraction
            default_connection_prompt = """Analyze the new memory entry and find meaningful connections to existing memories.

New Memory (ID {new_id}):
Summary: {summary}
Content: {content}

Existing Related Memories:
{related_entries}

Identify specific relationships between the new memory and existing ones.
Relationships can be:
- "relates_to" - general connection
- "depends_on" - new memory depends on existing one
- "extends" - new memory extends or elaborates existing one
- "contradicts" - new memory contradicts existing one
- "caused_by" - new memory is a result of existing one

Respond ONLY with a JSON object:
{
    "connections": [
        {"target_id": <existing_memory_id>, "relation": "<relationship_type>"},
        ...
    ]
}

If no meaningful connections found, respond with {"connections": []}"""
            
            prompt_template = self._get_prompt("memory_connection_prompt", default_connection_prompt)
            prompt = prompt_template.format(
                new_id=entry_id,
                summary=summary,
                content=content[:500],
                related_entries=related_context
            )
            
            t_conn = time.monotonic()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            duration_conn_ms = (time.monotonic() - t_conn) * 1000
            
            result = json.loads(response.choices[0].message.content)
            connections = result.get("connections", [])
            
            # Validate and filter connections
            valid_connections = []
            valid_ids = {r['entry_id'] for r in related_entries}
            for conn in connections:
                target_id = conn.get("target_id")
                relation = conn.get("relation", "relates_to")
                
                # Validate target_id exists in our candidates
                if target_id and int(target_id) in valid_ids:
                    valid_connections.append({
                        "target_id": int(target_id),
                        "relation": relation
                    })
            
            if valid_connections:
                # Update the entry with connections
                self.store.update(
                    entry_id=entry_id,
                    connections=json.dumps(valid_connections)
                )
                logger.info(
                    "🔗 Extracted %d connections for memory #%s (conn_ms=%.0f)",
                    len(valid_connections), entry_id, duration_conn_ms
                )
                verbose_logger.debug(
                    "MemoryOptimizer._extract_connections_async entry_id=%s connections=%s",
                    entry_id, valid_connections
                )
            else:
                logger.debug(
                    "No valid connections extracted for memory #%s (conn_ms=%.0f)",
                    entry_id, duration_conn_ms
                )
                
        except Exception as e:
            logger.warning("Error extracting connections for entry_id=%s: %s", entry_id, e)
            # Don't fail the main process - connections are optional

    async def process_new_entry(self, entry_id: int):
        """
        Process a newly added memory entry:
        1. Extract summary and entities.
        2. Find connections to existing memories via LLM.
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
            extracted_entities = result.get("entities", [])
            entities = json.dumps(extracted_entities)

            # Update the entry with summary and entities
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
                entry_id, summary, extracted_entities, duration_llm_ms,
            )

            # Step 2: Extract connections to existing memories
            await self._extract_connections_async(
                entry_id=entry_id,
                content=entry['content'],
                summary=summary,
                entities=extracted_entities,
                client=client,
                model=model
            )

            # Trigger consolidation check based on trigger mode
            if self.consolidation_trigger == 'on_save':
                await self.consolidate_recent(
                    user_id=entry.get("user_id"),
                    session_id=entry.get("session_id"),
                    agent_id=entry.get("agent_id")
                )
            elif self.consolidation_trigger == 'periodic':
                # Periodic trigger handles consolidation in background
                pass
            # 'manual' trigger does nothing here - user must call trigger_consolidation()
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

    async def consolidate_recent(self, user_id: Optional[str], session_id: Optional[str], agent_id: Optional[str], batch_size: int = None):
        """
        Consolidate recent short_term memories into a high-level insight.
        Respects min_short_term_age_hours - only consolidates memories older than this threshold.
        """
        if batch_size is None:
            batch_size = self.consolidation_batch_size
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
                limit=batch_size * 2  # Fetch more to filter by age
            )

            # Filter by min_short_term_age_hours
            now = datetime.now()
            age_threshold = timedelta(hours=self.min_short_term_age_hours)
            aged_recent = []
            for m in recent:
                try:
                    created = datetime.fromisoformat(m.created_at.replace('Z', '+00:00').replace('+00:00', ''))
                    if now - created >= age_threshold:
                        aged_recent.append(m)
                except Exception as e:
                    logger.warning("Could not parse created_at for memory %s: %s", m.id, e)
                    aged_recent.append(m)  # Include if can't parse
            
            recent = aged_recent[:batch_size]

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
