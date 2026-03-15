"""
Memory Tools V2 - SQLite-based memory system with 3 simplified tools.

Replaces 8 old tools with 3:
- memory_save: Save information to memory
- memory_search: Search memory
- task_update: Update task state
""" 

import logging
from typing import Any, Tuple, Optional
from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)

GLOBAL_MEMORY_TYPES = {"long_term", "insight", "skill"}
SESSION_MEMORY_TYPES = {"short_term", "task", "task_plan"}


def _get_memory_store(context: RunContextWrapper) -> Any:
    """
    Get MemoryStore from context (same pattern as orchestrator_tools).

    Args:
        context: Run context wrapper

    Returns:
        MemoryStore instance or None
    """
    try:
        # Get factory from context
        raw = getattr(context, "context", None)
        if raw is None:
            logger.error("❌ No context.context in RunContextWrapper")
            return None

        factory = getattr(raw, "factory", None)
        if factory is None:
            logger.error("❌ No factory in context")
            return None

        memory_store = getattr(factory, "memory_store", None)
        if memory_store is None:
            logger.error("❌ No memory_store in factory")
            return None

        return memory_store

    except Exception as e:
        logger.error(f"❌ Failed to get memory_store from context: {e}")
        return None

def _get_memory_optimizer(context: RunContextWrapper) -> Any:
    """
    Get MemoryOptimizer from context.
    """
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        factory = getattr(raw, "factory", None)
        if factory is None:
            return None
        return getattr(factory, "memory_optimizer", None)
    except Exception as e:
        logger.error(f"❌ Failed to get memory_optimizer from context: {e}")
        return None


def _get_context_ids(context: RunContextWrapper) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Get session_id, user_id, agent_id from context.
    
    Returns:
        (session_id, user_id, agent_id)
    """
    session_id = None
    user_id = None
    agent_id = None
    
    try:
        raw = getattr(context, "context", None)
        if raw:
            # Direct attributes
            session_id = getattr(raw, "session_id", None)
            user_id = getattr(raw, "user_id", None)
            agent_id = getattr(raw, "agent_id", None)
            
            # Metadata fallback (if context manager is used)
            if hasattr(raw, "context_manager"):
                cm = raw.context_manager
                if hasattr(cm, "get_metadata"):
                    if not user_id:
                        user_id = cm.get_metadata("user_id")
                    if not agent_id:
                        agent_id = cm.get_metadata("agent_id")
    except Exception:
        pass
        
    return session_id, user_id, agent_id


def _scope_session_for_type(memory_type: Optional[str], session_id: Optional[str]) -> Optional[str]:
    """
    Only session-scoped memory types should be tied to the current session.

    Long-term memory and insights must remain visible across sessions.
    """
    if memory_type in SESSION_MEMORY_TYPES:
        return session_id
    return None


def _scope_agent_for_type(memory_type: Optional[str], agent_id: Optional[str]) -> Optional[str]:
    """
    Shared memory must be visible across agents for the same user.

    We still keep creator agent_id as metadata in storage, but do not use it
    as a default filter for global memory reads.
    """
    if memory_type in SESSION_MEMORY_TYPES:
        return agent_id
    return None


# ============================================================================
# TOOL 1: memory_save - Save information to memory
# ============================================================================

@function_tool
async def memory_save(
    context: RunContextWrapper,
    text: str,
    type: str = "long_term",
    tags: str = "",
    importance: float = 0.5
) -> str:
    """
    Save information to memory.

    Use this to remember important information for future reference.
    Long-term memory persists across sessions, short-term is for recent context.

    Args:
        text: What to remember (content to save)
        type: Memory type - long_term | short_term | task | task_plan
              - long_term: Important facts, preferences, learnings (default)
              - short_term: Recent context, temporary notes
              - task: Current task description
              - task_plan: Plan for current task
        tags: Comma-separated tags for categorization (e.g., "python,api")
        importance: 0.0 to 1.0 (higher = more important, affects pre-loading)

    Returns:
        Confirmation message with entry ID

    Examples:
        memory_save("User prefers concise responses", type="long_term", tags="preference")
        memory_save("Fixed bug in auth.py line 42", type="short_term", tags="bugfix")
        memory_save("Implement SQLite memory system", type="task", importance=0.9)
    """
    store = _get_memory_store(context)
    if store is None:
        return "❌ Memory store not available"

    try:
        # Validate type
        valid_types = {"long_term", "short_term", "task", "task_plan"}
        if type not in valid_types:
            return f"❌ Invalid type '{type}'. Use: {', '.join(valid_types)}"

        # Validate importance
        if not 0 <= importance <= 1:
            return f"❌ Invalid importance {importance}. Must be 0.0 to 1.0"

        # Get context IDs
        session_id, user_id, agent_id = _get_context_ids(context)

        scoped_session_id = _scope_session_for_type(type, session_id)

        # Save to store
        entry_id = store.save(
            content=text,
            type=type,
            tags=tags,
            importance=importance,
            session_id=scoped_session_id,
            user_id=user_id,
            agent_id=agent_id
        )

        logger.info(f"💾 Saved memory #{entry_id} [{type}]: {text[:50]}...")

        # Trigger async optimization
        optimizer = _get_memory_optimizer(context)
        if optimizer:
            import asyncio
            try:
                # Use the running event loop to schedule the task
                loop = asyncio.get_running_loop()
                loop.create_task(optimizer.process_new_entry(entry_id))
                logger.debug(f"⚡ Scheduled optimization for memory #{entry_id}")
            except Exception as e:
                logger.error(f"⚠️ Could not schedule memory optimization: {e}")

        return f"✅ Saved to {type} memory (ID: {entry_id})"

    except Exception as e:
        logger.error(f"❌ Failed to save memory: {e}")
        return f"❌ Error saving memory: {e}"


# ============================================================================
# TOOL 2: memory_search - Search memory
# ============================================================================

@function_tool
async def memory_search(
    context: RunContextWrapper,
    query: str = "",
    type: str = "",
    limit: int = 20
) -> str:
    """
    Search memory by keywords.

    Use this to recall previously saved information.
    Empty query returns recent entries.

    Args:
        query: Search keywords (FTS5 full-text search). Empty = recent entries.
        type: Filter by type - long_term | short_term | task | task_plan. Empty = all types.
        limit: Maximum results to return (default: 20)

    Returns:
        Formatted list of matching memory entries

    Examples:
        memory_search("Python API")  # Search all memory
        memory_search("", type="long_term")  # Recent long-term memory
        memory_search("bugfix", type="short_term", limit=5)  # Recent bugfixes
        memory_search()  # All recent memory
    """
    store = _get_memory_store(context)
    if store is None:
        return "❌ Memory store not available"

    try:
        # Validate type if provided
        if type and type not in {"long_term", "short_term", "task", "task_plan", "insight"}:
            return f"❌ Invalid type '{type}'. Use: long_term, short_term, task, task_plan, insight, or empty for all"

        # Get context IDs
        session_id, user_id, agent_id = _get_context_ids(context)
        scoped_session_id = _scope_session_for_type(type or None, session_id)
        scoped_agent_id = _scope_agent_for_type(type or None, agent_id)

        # Search
        results = store.search(
            query=query,
            type=type or None,
            session_id=scoped_session_id,
            user_id=user_id,
            agent_id=scoped_agent_id,
            limit=limit
        )

        if not results:
            search_desc = f"'{query}'" if query else "recent entries"
            type_desc = f" in {type}" if type else ""
            return f"ℹ️ No memory found for {search_desc}{type_desc}"

        # Format results
        lines = []
        if query:
            lines.append(f"🔍 Found {len(results)} result(s) for '{query}':")
        else:
            type_desc = f" ({type})" if type else ""
            lines.append(f"📝 Recent memory{type_desc} ({len(results)} entries):")

        lines.append("")

        for entry in results:
            # Format timestamp
            timestamp = entry.created_at[:10] if entry.created_at else "N/A"

            # Format tags
            tags_str = f" [{entry.tags}]" if entry.tags else ""

            # Format importance indicator
            if entry.importance >= 0.8:
                importance_str = " ⭐⭐⭐"
            elif entry.importance >= 0.6:
                importance_str = " ⭐⭐"
            elif entry.importance >= 0.4:
                importance_str = " ⭐"
            else:
                importance_str = ""

            # Format content (truncate if too long)
            content = entry.content
            if len(content) > 200:
                content = content[:200] + "..."

            lines.append(f"[{entry.type}] {timestamp}{importance_str}{tags_str}")
            lines.append(f"  {content}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"❌ Failed to search memory: {e}")
        return f"❌ Error searching memory: {e}"


# ============================================================================
# TOOL 3: memory_delete - Delete memory entry
# ============================================================================

@function_tool
async def memory_delete(
    context: RunContextWrapper,
    query: str = "",
    entry_id: int = 0
) -> str:
    """
    Delete memory entry by ID or search query.

    Use this to remove incorrect, outdated, or unwanted information from memory.
    You can either provide entry_id directly or search with query to find entries first.

    Args:
        query: Search query to find entries to delete (shows entries without deleting)
        entry_id: Specific entry ID to delete (0 = search only, don't delete)

    Returns:
        Confirmation message or list of entries to delete

    Examples:
        memory_delete(query="мембрана ULP22-8040")  # Search first
        memory_delete(entry_id=42)  # Delete specific entry
    """
    store = _get_memory_store(context)
    if store is None:
        return "❌ Memory store not available"

    try:
        # Get context IDs
        session_id, user_id, agent_id = _get_context_ids(context)

        # If entry_id provided, delete it
        if entry_id > 0:
            success = store.delete(entry_id, hard=False)
            if success:
                logger.info(f"🗑️ Deleted memory entry #{entry_id}")
                return f"✅ Удалена запись #{entry_id} из памяти"
            else:
                return f"❌ Запись #{entry_id} не найдена"

        # If query provided, search and show entries
        if query:
            results = store.search(
                query=query,
                session_id=None,
                user_id=user_id,
                agent_id=agent_id,
                limit=10
            )

            if not results:
                return f"ℹ️ Записей по запросу '{query}' не найдено"

            # Format results with IDs
            lines = [f"🔍 Найдено {len(results)} записей по запросу '{query}':", ""]

            for entry in results:
                timestamp = entry.created_at[:10] if entry.created_at else "N/A"
                tags_str = f" [{entry.tags}]" if entry.tags else ""

                # Truncate content
                content = entry.content
                if len(content) > 100:
                    content = content[:100] + "..."

                lines.append(f"ID: {entry.id} | [{entry.type}] {timestamp}{tags_str}")
                lines.append(f"  {content}")
                lines.append("")

            lines.append("💡 Используй memory_delete(entry_id=N) для удаления конкретной записи")
            return "\n".join(lines)

        return "❌ Укажи query (для поиска) или entry_id (для удаления)"

    except Exception as e:
        logger.error(f"❌ Failed to delete memory: {e}")
        return f"❌ Error deleting memory: {e}"


# ============================================================================
# TOOL 4: task_update - Update task state
# ============================================================================

@function_tool
async def task_update(
    context: RunContextWrapper,
    content: str,
    action: str = "add",
    entry_id: int = 0,
    importance: float = 0.7
) -> str:
    """
    Update current task: add findings, set plan, mark complete.

    Use this to track task progress and store task-related information.

    Args:
        content: Task content, finding, plan step, or status text
        action: Action to perform:
                - add: Add task finding/observation (creates task entry)
                - plan: Set task plan (creates task_plan entry)
                - complete: Mark task as completed
                - update: Update existing entry
                - delete: Delete/archive entry
        entry_id: Required for update/delete actions (entry ID)
        importance: 0.0 to 1.0 (affects pre-loading, default: 0.7)

    Returns:
        Confirmation message

    Examples:
        task_update("Implement SQLite memory", action="add")
        task_update("1. Create schema\\n2. Write MemoryStore\\n3. Test", action="plan")
        task_update("Found bug in line 42", action="add", importance=0.9)
        task_update("Task finished successfully", action="complete")
        task_update("Updated plan step 2", action="update", entry_id=123)
    """
    store = _get_memory_store(context)
    if store is None:
        return "❌ Memory store not available"

    try:
        # Validate action
        valid_actions = {"add", "plan", "complete", "update", "delete"}
        if action not in valid_actions:
            return f"❌ Invalid action '{action}'. Use: {', '.join(valid_actions)}"

        # Validate entry_id for update/delete
        if action in {"update", "delete"} and entry_id == 0:
            return f"❌ entry_id required for {action} action"

        # Get context IDs
        session_id, user_id, agent_id = _get_context_ids(context)
        
        # Get task_id from context
        task_id = None
        try:
            raw = getattr(context, "context", None)
            if raw:
                task_id = getattr(raw, "task_id", None)
        except Exception:
            pass

        # Generate task_id if not available
        if not task_id:
            from datetime import datetime
            task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        # Execute action
        if action == "add":
            # Add task finding
            entry_id = store.save(
                content=content,
                type="task",
                importance=importance,
                session_id=session_id,
                task_id=task_id,
                user_id=user_id,
                agent_id=agent_id,
                status="active"
            )
            logger.info(f"📋 Added task entry #{entry_id}: {content[:50]}...")
            
            # Trigger async optimization
            optimizer = _get_memory_optimizer(context)
            if optimizer:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(optimizer.process_new_entry(entry_id))
                except Exception as e:
                    logger.error(f"⚠️ Could not schedule memory optimization: {e}")
                    
            return f"✅ Added task entry (ID: {entry_id})"

        elif action == "plan":
            # Set task plan
            entry_id = store.save(
                content=content,
                type="task_plan",
                importance=importance,
                session_id=session_id,
                task_id=task_id,
                user_id=user_id,
                agent_id=agent_id,
                status="active"
            )
            logger.info(f"📝 Set task plan #{entry_id}")
            return f"✅ Task plan saved (ID: {entry_id})"

        elif action == "complete":
            # Mark all active tasks for this task_id as completed
            tasks = store.search(
                type="task",
                task_id=task_id,
                status="active",
                limit=100
            )

            if not tasks:
                return "ℹ️ No active tasks to complete"

            for task in tasks:
                store.update(task.id, status="completed")

            # Add completion note
            entry_id = store.save(
                content=content or "Task completed",
                type="task",
                importance=0.5,
                session_id=session_id,
                task_id=task_id,
                user_id=user_id,
                agent_id=agent_id,
                status="completed"
            )

            logger.info(f"✅ Completed {len(tasks)} task(s)")
            return f"✅ Marked {len(tasks)} task(s) as completed"

        elif action == "update":
            # Update existing entry
            success = store.update(entry_id, content=content, importance=importance)
            if success:
                logger.info(f"✏️ Updated entry #{entry_id}")
                return f"✅ Updated entry {entry_id}"
            else:
                return f"❌ Entry {entry_id} not found"

        elif action == "delete":
            # Archive entry (soft delete)
            success = store.delete(entry_id, hard=False)
            if success:
                logger.info(f"🗑️ Archived entry #{entry_id}")
                return f"✅ Archived entry {entry_id}"
            else:
                return f"❌ Entry {entry_id} not found"

    except Exception as e:
        logger.error(f"❌ Failed to update task: {e}")
        return f"❌ Error updating task: {e}"


# ============================================================================
# TOOL 5: memory_ingest_file - Ingest file into memory
# ============================================================================

@function_tool
async def memory_ingest_file(
    context: RunContextWrapper,
    filepath: str,
    type: str = "long_term",
    tags: str = "",
    importance: float = 0.5
) -> str:
    """
    Read a file and ingest its contents into memory.
    
    Use this to quickly remember the contents of a text file, log, or document.
    The file will be read, saved to memory, and asynchronously optimized (summarized and entities extracted).

    Args:
        filepath: Path to the file to ingest
        type: Memory type (default: long_term)
        tags: Comma-separated tags
        importance: 0.0 to 1.0

    Returns:
        Confirmation message
    """
    import os
    from pathlib import Path
    
    # Try to read the file
    try:
        path = Path(filepath)
        if not path.exists():
            return f"❌ File not found: {filepath}"
            
        # Basic text reading
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Truncate if too large (e.g., > 100KB)
        if len(content) > 100000:
            content = content[:100000] + "\\n...[TRUNCATED]"
            
        # Format the memory content
        memory_content = f"File: {path.name}\\nPath: {filepath}\\n\\nContent:\\n{content}"
        
        # Save to memory
        return await memory_save(
            context=context,
            text=memory_content,
            type=type,
            tags=f"file,{tags}" if tags else "file",
            importance=importance
        )
        
    except UnicodeDecodeError:
        return f"❌ Cannot read {filepath}: Not a text file or unsupported encoding."
    except Exception as e:
        logger.error(f"❌ Failed to ingest file {filepath}: {e}")
        return f"❌ Error ingesting file: {e}"


# ============================================================================
# TOOL 6: memory_explore_entity - Explore entity in knowledge graph
# ============================================================================

@function_tool
async def memory_explore_entity(
    context: RunContextWrapper,
    entity: str,
    limit: int = 10
) -> str:
    """
    Найти все записи с указанной сущностью через MemoryStore.get_entity_graph().

    Вернуть форматированный список записей с summary, type, created_at для каждой записи.

    Args:
        entity: Сущность для поиска (e.g. "Python", "Даниил")
        limit: Максимум записей (default: 10)
    """
    store = _get_memory_store(context)
    if store is None:
        return "❌ Memory store not available"

    try:
        session_id, user_id, agent_id = _get_context_ids(context)
        records = store.get_entity_graph(entity=entity, user_id=user_id, limit=limit)

        if not records:
            return f"ℹ️ No records found for entity '{entity}'"

        lines = [f"🔍 Records for entity '{entity}' ({len(records)}):", ""]
        for rec in records:
            summary = getattr(rec, 'summary', getattr(rec, 'content', 'N/A')[:100] + '...')
            typ = getattr(rec, 'type', 'unknown')
            created_at = getattr(rec, 'created_at', 'N/A')[:10] if getattr(rec, 'created_at', None) else 'N/A'
            lines.append(f"[{typ}] {created_at}: {summary}")
            lines.append("")

        return '\\n'.join(lines)

    except Exception as e:
        logger.error(f"❌ Failed to explore entity: {e}")
        return f"❌ Error exploring entity '{entity}': {e}"


# ============================================================================
# TOOL 7: memory_graph_path - Find path in knowledge graph
# ============================================================================

@function_tool
async def memory_graph_path(
    context: RunContextWrapper,
    from_entity: str,
    to_entity: str,
    max_depth: int = 3
) -> str:
    """
    Найти путь между двумя сущностями через MemoryStore.find_entity_connections().

    Вернуть форматированное описание пути. Если не найден - сообщение.

    Args:
        from_entity: Начальная сущность
        to_entity: Целевая сущность
        max_depth: Максимальная глубина поиска (default: 3)
    """
    store = _get_memory_store(context)
    if store is None:
        return "❌ Memory store not available"

    try:
        session_id, user_id, agent_id = _get_context_ids(context)
        path = store.find_entity_connections(from_entity=from_entity, to_entity=to_entity, user_id=user_id, max_depth=max_depth)

        if not path:
            return f"ℹ️ No path found between '{from_entity}' and '{to_entity}' (max_depth={max_depth})"

        # Format path, assuming list of str or entities
        if isinstance(path, list) and len(path) > 0:
            if isinstance(path[0], str):
                path_str = " → ".join(path)
            else:
                path_str = " → ".join([str(node) for node in path])
        else:
            path_str = str(path)

        return f"✅ Path: {path_str}"

    except Exception as e:
        logger.error(f"❌ Failed to find graph path: {e}")
        return f"❌ Error finding path from '{from_entity}' to '{to_entity}': {e}"


# ============================================================================
# TOOL REGISTRY
# ============================================================================

MEMORY_TOOLS_V2 = {
    "memory_save": memory_save,
    "memory_search": memory_search,
    "memory_delete": memory_delete,
    "task_update": task_update,
    "memory_ingest_file": memory_ingest_file,
    "memory_explore_entity": memory_explore_entity,
    "memory_graph_path": memory_graph_path,
}


# ============================================================================
# HELPER FUNCTIONS FOR BACKWARD COMPATIBILITY
# ============================================================================

def get_memory_tools_v2():
    """Get list of memory tools v2."""
    return list(MEMORY_TOOLS_V2.values())


def get_memory_tools_v2_by_names(tool_names: list):
    """Get memory tools v2 by names."""
    return [MEMORY_TOOLS_V2[name] for name in tool_names if name in MEMORY_TOOLS_V2]
