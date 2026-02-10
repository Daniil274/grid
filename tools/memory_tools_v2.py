"""
Memory Tools V2 - SQLite-based memory system with 3 simplified tools.

Replaces 8 old tools with 3:
- memory_save: Save information to memory
- memory_search: Search memory
- task_update: Update task state
"""

import logging
from typing import Any
from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)


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

        # Get session_id from context if available
        session_id = None
        try:
            raw_context = getattr(context, "context", None)
            if raw_context:
                session_id = getattr(raw_context, "session_id", None)
        except Exception:
            pass

        # Save to store
        entry_id = store.save(
            content=text,
            type=type,
            tags=tags,
            importance=importance,
            session_id=session_id
        )

        logger.info(f"💾 Saved memory #{entry_id} [{type}]: {text[:50]}...")
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
        if type and type not in {"long_term", "short_term", "task", "task_plan"}:
            return f"❌ Invalid type '{type}'. Use: long_term, short_term, task, task_plan, or empty for all"

        # Get session_id from context if available
        session_id = None
        try:
            raw_context = getattr(context, "context", None)
            if raw_context:
                session_id = getattr(raw_context, "session_id", None)
        except Exception:
            pass

        # Search
        results = store.search(
            query=query,
            type=type or None,
            session_id=session_id,
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
        # Get session_id from context if available
        session_id = None
        try:
            raw_context = getattr(context, "context", None)
            if raw_context:
                session_id = getattr(raw_context, "session_id", None)
        except Exception:
            pass

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
                session_id=session_id,
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

        # Get session_id and task_id from context
        session_id = None
        task_id = None
        try:
            raw_context = getattr(context, "context", None)
            if raw_context:
                session_id = getattr(raw_context, "session_id", None)
                task_id = getattr(raw_context, "task_id", None)
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
                status="active"
            )
            logger.info(f"📋 Added task entry #{entry_id}: {content[:50]}...")
            return f"✅ Added task entry (ID: {entry_id})"

        elif action == "plan":
            # Set task plan
            entry_id = store.save(
                content=content,
                type="task_plan",
                importance=importance,
                session_id=session_id,
                task_id=task_id,
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
# TOOL REGISTRY
# ============================================================================

MEMORY_TOOLS_V2 = {
    "memory_save": memory_save,
    "memory_search": memory_search,
    "memory_delete": memory_delete,
    "task_update": task_update,
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
