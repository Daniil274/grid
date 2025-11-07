"""
Advanced context management for Grid agents with memory and persistence.
"""

from typing import List, Dict, Optional, Any
from datetime import datetime
from threading import Lock
import json
import threading
from pathlib import Path
import logging

from schemas import ContextMessage, AgentExecution
from utils.exceptions import ContextError
# Tracing is handled automatically by Agents SDK

logger = logging.getLogger("core.context")

# Optional embeddings support
try:
    from core.embeddings import EmbeddingsManager, create_embeddings_manager
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    logger.info("Embeddings features not available. Install: pip install sentence-transformers chromadb")


def safe_lock(lock, timeout=5.0):
    """Context manager для безопасного использования lock'а с таймаутом."""
    class SafeLockContext:
        def __init__(self, lock, timeout):
            self.lock = lock
            self.timeout = timeout
            self.acquired = False
            
        def __enter__(self):
            self.acquired = self.lock.acquire(timeout=self.timeout)
            if not self.acquired:
                raise ContextError(f"Lock timeout after {self.timeout} seconds")
            return self
            
        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.acquired:
                self.lock.release()
    
    return SafeLockContext(lock, timeout)


class ContextManager:
    """Thread-safe context manager with persistence and memory optimization."""
    
    def __init__(
        self,
        max_history: int = 15,
        persist_path: Optional[str] = None,
        enable_embeddings: bool = True,
        embeddings_persist_path: Optional[str] = None
    ):
        """
        Initialize context manager.

        Args:
            max_history: Maximum number of messages to keep in memory
            persist_path: Optional path for persistence (JSON file)
            enable_embeddings: Enable semantic search features
            embeddings_persist_path: Path for embeddings persistence
        """
        self.max_history = max_history
        self.persist_path = Path(persist_path) if persist_path else None

        self._conversation_history: List[ContextMessage] = []
        self._execution_history: List[AgentExecution] = []
        self._metadata: Dict[str, Any] = {}
        self._lock = Lock()

        # Initialize embeddings manager if available and enabled
        self._embeddings_manager: Optional[Any] = None
        if enable_embeddings and EMBEDDINGS_AVAILABLE:
            try:
                enable_persist = embeddings_persist_path is not None
                self._embeddings_manager = create_embeddings_manager(
                    enable_persistence=enable_persist,
                    persist_directory=embeddings_persist_path
                )
                if self._embeddings_manager:
                    logger.info("Embeddings manager initialized for semantic search")
            except Exception as e:
                logger.warning(f"Failed to initialize embeddings manager: {e}")

        # Load from persistence if available
        if self.persist_path and self.persist_path.exists():
            self._load_from_file()
    
    def add_message(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add message to conversation history.
        
        Args:
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional metadata
        """
        acquired = self._lock.acquire(timeout=5.0)  # 5 сек таймаут
        if not acquired:
            # Lock timeout in add_message
            raise ContextError("Lock timeout in add_message")
        try:
            try:
                message = ContextMessage(
                    role=role,
                    content=content,
                    timestamp=datetime.now().isoformat(),
                    metadata=metadata
                )
                
                self._conversation_history.append(message)

                # Add to embeddings if available
                if self._embeddings_manager:
                    try:
                        msg_id = f"msg_{message.timestamp}_{role}"
                        msg_metadata = {
                            "role": role,
                            "timestamp": message.timestamp,
                            "source": "conversation"
                        }
                        if metadata:
                            msg_metadata.update(metadata)

                        self._embeddings_manager.add_texts(
                            texts=[content],
                            metadatas=[msg_metadata],
                            ids=[msg_id]
                        )
                    except Exception as e:
                        logger.warning(f"Failed to add message to embeddings: {e}")

                # Trim history if needed
                if len(self._conversation_history) > self.max_history:
                    removed = self._conversation_history.pop(0)
                    # Removed old message from context

                # Added message to context

                # Persist if configured
                if self.persist_path:
                    self._save_to_file()
                    
            except Exception as e:
                raise ContextError(f"Failed to add message: {e}")
        finally:
            self._lock.release()
    
    def add_execution(self, execution: AgentExecution) -> None:
        """Add agent execution to history."""
        acquired = self._lock.acquire(timeout=5.0)  # 5 сек таймаут
        if not acquired:
            # Lock timeout in add_execution
            return
        try:
            self._execution_history.append(execution)
            
            # Keep execution history reasonable
            if len(self._execution_history) > self.max_history * 2:
                self._execution_history.pop(0)
            
            # Persist if configured
            if self.persist_path:
                self._save_to_file()
        finally:
            self._lock.release()
    
    def get_conversation_context(self, last_n: Optional[int] = None) -> str:
        """
        Get formatted conversation context.
        
        Args:
            last_n: Number of last messages to include (default: all)
            
        Returns:
            Formatted context string
        """
        try:
            with safe_lock(self._lock):
                if not self._conversation_history:
                    return ""
                
                messages = self._conversation_history
                if last_n:
                    messages = messages[-last_n:]
                
                # Natural, concise dialogue transcript without emojis
                lines = ["Предыдущий диалог (сжатый):"]
                for msg in messages:
                    role = {
                        "user": "Пользователь",
                        "assistant": "Ассистент",
                        "system": "Система"
                    }.get(msg.role, msg.role)
                    content = msg.content.strip()
                    # Hard trim very long single messages to keep prompt lightweight
                    if len(content) > 2000:
                        content = content[:2000] + "…"
                    lines.append(f"{role}: {content}")
                lines.append("Пожалуйста, учитывай этот контекст при ответе.")
                return "\n".join(lines)
        except ContextError:
            # Lock timeout in get_conversation_context
            return "Context temporarily unavailable due to lock timeout."
    
    def get_recent_executions(self, agent_name: Optional[str] = None, limit: int = 5) -> List[AgentExecution]:
        """Get recent agent executions, optionally filtered by agent name."""
        with self._lock:
            executions = self._execution_history
            
            if agent_name:
                executions = [ex for ex in executions if ex.agent_name == agent_name]
            
            return executions[-limit:]
    
    def clear_history(self) -> None:
        """Clear all conversation history."""
        with self._lock:
            cleared_count = len(self._conversation_history)
            self._conversation_history.clear()
            self._execution_history.clear()
            
            # Cleared messages from context
            
            # Clear persistence file
            if self.persist_path and self.persist_path.exists():
                self.persist_path.unlink()
    
    def get_context_stats(self) -> Dict[str, Any]:
        """Get context statistics."""
        try:
            with safe_lock(self._lock):
                # Прямой доступ к данным внутри lock'а для избежания deadlock'ов
                last_user = None
                last_assistant = None
                
                for msg in reversed(self._conversation_history):
                    if msg.role == "user" and last_user is None:
                        last_user = msg.content
                    elif msg.role == "assistant" and last_assistant is None:
                        last_assistant = msg.content
                    
                    if last_user and last_assistant:
                        break
                
                return {
                    "conversation_messages": len(self._conversation_history),
                    "execution_history": len(self._execution_history),
                    "memory_usage_mb": self._estimate_memory_usage(),
                    "last_user_message": last_user,
                    "last_assistant_message": last_assistant,
                }
        except ContextError:
            # Lock timeout in get_context_stats
            return {
                "conversation_messages": 0,
                "execution_history": 0,
                "memory_usage_mb": 0.0,
                "last_user_message": None,
                "last_assistant_message": None,
            }

    def get_conversation_history(self) -> List[Dict[str, Any]]:
        """Return raw conversation history as list of dicts for external consumers."""
        acquired = self._lock.acquire(timeout=5.0)  # 5 сек таймаут
        if not acquired:
            # Lock timeout in get_conversation_history
            return []
        try:
            return [msg.model_dump() for msg in self._conversation_history]
        finally:
            self._lock.release()
    
    def get_last_user_message(self) -> Optional[str]:
        """Get the last user message."""
        acquired = self._lock.acquire(timeout=5.0)  # 5 сек таймаут
        if not acquired:
            # Lock timeout in get_last_user_message
            return None
        try:
            for msg in reversed(self._conversation_history):
                if msg.role == "user":
                    return msg.content
            return None
        finally:
            self._lock.release()
    
    def get_last_assistant_message(self) -> Optional[str]:
        """Get the last assistant message."""
        acquired = self._lock.acquire(timeout=5.0)  # 5 сек таймаут
        if not acquired:
            # Lock timeout in get_last_assistant_message
            return None
        try:
            for msg in reversed(self._conversation_history):
                if msg.role == "assistant":
                    return msg.content
            return None
        finally:
            self._lock.release()
    
    def set_metadata(self, key: str, value: Any) -> None:
        """Set context metadata."""
        with self._lock:
            self._metadata[key] = value
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get context metadata."""
        with self._lock:
            return self._metadata.get(key, default)
    
    def _get_role_emoji(self, role: str) -> str:
        """Get emoji for message role."""
        return {
            "user": "👤",
            "assistant": "🤖", 
            "system": "⚙️"
        }.get(role, "❓")
    
    def _estimate_memory_usage(self) -> float:
        """Estimate memory usage in MB."""
        try:
            # Rough estimation based on string lengths
            total_chars = sum(len(msg.content) for msg in self._conversation_history)
            total_chars += sum(len(ex.input_message) + len(ex.output or "") 
                             for ex in self._execution_history)
            
            # Assume ~2 bytes per character + overhead
            return (total_chars * 2 + len(self._conversation_history) * 100) / (1024 * 1024)
        except Exception:
            return 0.0
    
    def _save_to_file(self) -> None:
        """Save context to persistence file."""
        try:
            data = {
                "conversation_history": [m.model_dump() for m in self._conversation_history],
                "execution_history": [e.model_dump() for e in self._execution_history],
                "metadata": self._metadata
            }
            
            # Ensure directory exists
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.persist_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            # Failed to save context
            logger.error(f"Failed to save context to {self.persist_path}: {e}")
    
    def _load_from_file(self) -> None:
        """Load context from persistence file."""
        try:
            with open(self.persist_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Load conversation history
            self._conversation_history = [
                ContextMessage(**msg) for msg in data.get("conversation_history", [])
            ]
            
            # Load execution history
            self._execution_history = [
                AgentExecution(**ex) for ex in data.get("execution_history", [])
            ]
            
            # Load metadata
            self._metadata = data.get("metadata", {})
            
            # Loaded context from file
            
        except Exception as e:
            # Failed to load context
            logger.error(f"Failed to load context from {self.persist_path}: {e}")
            # Reset to empty state on failure
            self._conversation_history = []
            self._execution_history = []
            self._metadata = {}
    
    def _build_smart_context_json(self, task_input: str, depth: int, include_tools: bool) -> str:
        """Build smart context based on task analysis in JSON format."""
        # Analyze task to determine relevant context
        task_lower = task_input.lower()
        
        # Keywords that suggest need for conversation context
        conversation_keywords = [
            "продолжи", "далее", "следующий", "предыдущий", "раньше", "уже", "было",
            "continue", "next", "previous", "before", "already", "was", "что сказал",
            "ответь на", "отвечай на", "который", "этот", "тот", "тот же", "тот самый",
            "прочитал", "прочитал и", "анализировал", "оценил", "создал", "отредактировал"
        ]
        
        # Keywords that suggest need for tool history
        tool_keywords = [
            "файл", "git", "код", "изменения", "результат", "выполнил", "сделал",
            "file", "git", "code", "changes", "result", "executed", "done", "создал",
            "отредактировал", "прочитал", "написал", "весит", "размер", "вес", "байт",
            "проанализировал", "оценил", "проверил", "нашел", "создал файл"
        ]
        
        # Keywords that suggest reference to previous actions
        reference_keywords = [
            "который", "этот", "тот", "тот же", "тот самый", "прочитанный", "анализированный",
            "созданный", "отредактированный", "проверенный", "найденный", "тот файл",
            "этот файл", "прочитанный файл", "анализированный файл", "созданный файл"
        ]
        
        needs_conversation = any(keyword in task_lower for keyword in conversation_keywords)
        needs_tools = any(keyword in task_lower for keyword in tool_keywords)
        needs_reference = any(keyword in task_lower for keyword in reference_keywords)
        
        # Если есть ссылки на предыдущие действия - обязательно нужен полный контекст
        if needs_reference:
            return self._build_full_context_json(task_input, include_tools)
        elif needs_conversation and needs_tools:
            return self._build_full_context_json(task_input, include_tools)
        elif needs_conversation:
            return self._build_conversation_context_json(task_input, depth)
        elif needs_tools and include_tools:
            return self._build_tool_context_json(task_input)
        else:
            return task_input
    
    def get_context_for_agent_tool(
        self, 
        strategy: str = "minimal", 
        depth: int = 5, 
        include_tools: bool = False,
        task_input: str = ""
    ) -> str:
        """
        Get context for agent tools based on strategy.
        
        Args:
            strategy: Context strategy (minimal, conversation, smart, full)
            depth: Number of recent messages to include
            include_tools: Whether to include tool execution history
            task_input: The task input for smart analysis
            
        Returns:
            Formatted context string (human-readable transcript)
        """
        if strategy == "minimal":
            return task_input
        elif strategy == "conversation":
            return self._build_conversation_context_human(task_input, depth)
        elif strategy == "smart":
            return self._build_smart_context_human(task_input, depth, include_tools)
        elif strategy == "full":
            return self._build_full_context_human(task_input, include_tools)
        else:
            return task_input
    
    def _build_conversation_context_json(self, task_input: str, depth: int) -> str:
        """Build conversation context in JSON format."""
        with self._lock:
            if not self._conversation_history:
                return task_input
            
            recent_messages = self._conversation_history[-depth:] if depth > 0 else self._conversation_history
            
            context_parts = [
                "📋 Контекст диалога:",
                f"Текущая задача: {task_input}",
                "",
                "История сообщений (JSON формат):"
            ]
            
            messages_json = []
            for msg in recent_messages:
                message_obj = {
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp
                }
                messages_json.append(message_obj)
            
            import json
            context_parts.append(json.dumps(messages_json, ensure_ascii=False, indent=2))
            
            context_parts.extend([
                "",
                "💡 Используй эту информацию для понимания контекста задачи.",
                f"Задача: {task_input}"
            ])
            
            return "\n".join(context_parts)
    
    def _build_full_context_json(self, task_input: str, include_tools: bool) -> str:
        """Build full context including conversation and tool history in JSON format."""
        with self._lock:
            context_parts = [
                "📋 ПОЛНЫЙ КОНТЕКСТ:",
                f"Текущая задача: {task_input}",
                "",
                "💬 История диалога:"
            ]
            
            # Add conversation history
            if self._conversation_history:
                messages_json = []
                for msg in self._conversation_history:
                    message_obj = {
                        "role": msg.role,
                        "content": msg.content,
                        "timestamp": msg.timestamp
                    }
                    messages_json.append(message_obj)
                
                import json
                context_parts.append(json.dumps(messages_json, ensure_ascii=False, indent=2))
            else:
                context_parts.append("[]")
            
            # Add tool execution history if requested
            if include_tools and self._execution_history:
                context_parts.extend([
                    "",
                    "🔧 История выполнения операций:"
                ])
                
                tools_json = []
                for ex in self._execution_history[-10:]:  # Last 10 executions
                    tool_obj = {
                        "agent": ex.agent_name,
                        "input": ex.input_message,
                        "output": ex.output,
                        "timestamp": ex.start_time,
                        "duration": ex.end_time - ex.start_time if ex.end_time else 0
                    }
                    tools_json.append(tool_obj)
                
                import json
                context_parts.append(json.dumps(tools_json, ensure_ascii=False, indent=2))
            
            context_parts.extend([
                "",
                "🎯 ВАЖНО: Используй всю эту информацию для выполнения задачи!",
                f"Задача: {task_input}"
            ])
            
            return "\n".join(context_parts)
    
    def _build_tool_context_json(self, task_input: str) -> str:
        """Build tool execution context in JSON format."""
        with self._lock:
            if not self._execution_history:
                return task_input
            
            context_parts = [
                "🔧 Контекст операций:",
                f"Текущая задача: {task_input}",
                "",
                "История выполнения операций (JSON формат):"
            ]
            
            tools_json = []
            for ex in self._execution_history[-5:]:  # Last 5 executions
                tool_obj = {
                    "agent": ex.agent_name,
                    "input": ex.input_message,
                    "output": ex.output,
                    "timestamp": ex.start_time
                }
                tools_json.append(tool_obj)
            
            import json
            context_parts.append(json.dumps(tools_json, ensure_ascii=False, indent=2))
            
            context_parts.extend([
                "",
                "💡 Используй эту информацию о предыдущих операциях.",
                f"Задача: {task_input}"
            ])
            
            return "\n".join(context_parts)

    def _build_conversation_context_human(self, task_input: str, depth: int) -> str:
        """Conversation context as a readable dialogue excerpt."""
        with self._lock:
            if not self._conversation_history:
                return task_input
            recent_messages = self._conversation_history[-depth:] if depth > 0 else self._conversation_history
            lines = ["Контекст диалога:", f"Текущая задача: {task_input}", ""]
            for msg in recent_messages:
                role = {
                    "user": "Пользователь",
                    "assistant": "Ассистент",
                    "system": "Система"
                }.get(msg.role, msg.role)
                content = msg.content.strip()
                if len(content) > 2000:
                    content = content[:2000] + "…"
                lines.append(f"{role}: {content}")
            lines.append("")
            lines.append("Используй эту информацию для понимания контекста задачи.")
            return "\n".join(lines)

    def _build_full_context_human(self, task_input: str, include_tools: bool) -> str:
        """Full human-readable context: dialogue and recent tool results."""
        with self._lock:
            lines = ["ПОЛНЫЙ КОНТЕКСТ:", f"Текущая задача: {task_input}", "", "История диалога:"]
            if self._conversation_history:
                for msg in self._conversation_history:
                    role = {
                        "user": "Пользователь",
                        "assistant": "Ассистент",
                        "system": "Система"
                    }.get(msg.role, msg.role)
                    content = msg.content.strip()
                    if len(content) > 2000:
                        content = content[:2000] + "…"
                    lines.append(f"{role}: {content}")
            else:
                lines.append("(пусто)")
            if include_tools and self._execution_history:
                lines.extend(["", "Результаты последних операций:"])
                for ex in self._execution_history[-10:]:
                    summary_output = (ex.output or "").strip()
                    if len(summary_output) > 2000:
                        summary_output = summary_output[:2000] + "…"
                    lines.append(f"Инструмент/агент: {ex.agent_name}")
                    lines.append(f"Ввод: {ex.input_message}")
                    if summary_output:
                        lines.append(f"Вывод: {summary_output}")
                    if ex.error:
                        lines.append(f"Ошибка: {ex.error}")
                    lines.append("")
            lines.append("ВНИМАНИЕ: Используй информацию выше для решения задачи.")
            return "\n".join(lines)

    def _build_smart_context_human(self, task_input: str, depth: int, include_tools: bool) -> str:
        """Human-readable smart context selection."""
        task_lower = task_input.lower()
        conversation_keywords = [
            "продолжи", "далее", "следующий", "предыдущий", "раньше", "уже", "было",
            "continue", "next", "previous", "before", "already", "was", "что сказал",
            "ответь на", "отвечай на", "который", "этот", "тот", "тот же", "тот самый",
            "прочитал", "анализировал", "оценил", "создал", "отредактировал"
        ]
        tool_keywords = [
            "файл", "git", "код", "изменения", "результат", "выполнил", "сделал",
            "file", "git", "code", "changes", "result", "executed", "done", "создал",
            "отредактировал", "прочитал", "написал", "весит", "размер", "байт",
            "проанализировал", "оценил", "проверил", "нашел", "создал файл"
        ]
        reference_keywords = [
            "который", "этот", "тот", "тот же", "тот самый", "прочитанный", "анализированный",
            "созданный", "отредактированный", "проверенный", "найденный", "тот файл",
            "этот файл", "прочитанный файл", "анализированный файл", "созданный файл"
        ]
        needs_conversation = any(k in task_lower for k in conversation_keywords)
        needs_tools = any(k in task_lower for k in tool_keywords)
        needs_reference = any(k in task_lower for k in reference_keywords)
        if needs_reference or (needs_conversation and needs_tools):
            return self._build_full_context_human(task_input, include_tools)
        elif needs_conversation:
            return self._build_conversation_context_human(task_input, depth)
        elif needs_tools and include_tools:
            # Light tool-only summary
            with self._lock:
                lines = ["Контекст операций:", f"Текущая задача: {task_input}", ""]
                for ex in self._execution_history[-5:]:
                    summary_output = (ex.output or "").strip()
                    if len(summary_output) > 1200:
                        summary_output = summary_output[:1200] + "…"
                    lines.append(f"Инструмент/агент: {ex.agent_name}")
                    if summary_output:
                        lines.append(f"Вывод: {summary_output}")
                    lines.append("")
                lines.append("Используй эту информацию о предыдущих операциях.")
                return "\n".join(lines)
        else:
            return task_input

    def semantic_search_history(
        self,
        query: str,
        n_results: int = 5,
        role_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Semantic search through conversation history.

        Args:
            query: Search query
            n_results: Number of results to return
            role_filter: Filter by message role (user, assistant, system)

        Returns:
            List of relevant messages with similarity scores
        """
        if not self._embeddings_manager:
            logger.warning("Embeddings not available for semantic search")
            return []

        where = {"source": "conversation"}
        if role_filter:
            where["role"] = role_filter

        try:
            results = self._embeddings_manager.search(
                query=query,
                n_results=n_results,
                where=where
            )
            return results
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    def get_relevant_context_semantic(
        self,
        query: str,
        max_results: int = 5,
        include_tools: bool = False
    ) -> str:
        """
        Get semantically relevant context for a query (RAG pattern).

        Args:
            query: Query to find relevant context for
            max_results: Maximum number of relevant messages
            include_tools: Whether to include tool execution context

        Returns:
            Formatted context string with relevant messages
        """
        if not self._embeddings_manager:
            # Fallback to regular context
            return self.get_conversation_context(last_n=max_results)

        # Search for relevant messages
        results = self.semantic_search_history(query, n_results=max_results)

        if not results:
            return query

        # Build context
        lines = [
            "Релевантный контекст (семантический поиск):",
            f"Запрос: {query}",
            ""
        ]

        for result in results:
            metadata = result.get('metadata', {})
            role = metadata.get('role', 'unknown')
            timestamp = metadata.get('timestamp', '')
            similarity = result.get('similarity', 0.0)
            text = result.get('text', '')

            role_label = {
                "user": "Пользователь",
                "assistant": "Ассистент",
                "system": "Система"
            }.get(role, role)

            # Show only highly relevant results (similarity > 0.3)
            if similarity > 0.3:
                lines.append(f"{role_label} (релевантность: {similarity:.2f}):")
                lines.append(text[:500] + ("..." if len(text) > 500 else ""))
                lines.append("")

        # Add tool context if requested
        if include_tools and self._execution_history:
            lines.append("Недавние операции:")
            for ex in self._execution_history[-3:]:
                lines.append(f"- {ex.agent_name}: {ex.input_message[:100]}")
            lines.append("")

        lines.append("Используй этот контекст для ответа на запрос.")

        return "\n".join(lines)

    def is_embeddings_enabled(self) -> bool:
        """Check if embeddings/semantic search is enabled."""
        return self._embeddings_manager is not None

    def get_embeddings_stats(self) -> Dict[str, Any]:
        """Get embeddings statistics."""
        if not self._embeddings_manager:
            return {
                "enabled": False,
                "indexed_messages": 0
            }

        try:
            return {
                "enabled": True,
                "indexed_messages": self._embeddings_manager.get_collection_count()
            }
        except Exception as e:
            logger.error(f"Failed to get embeddings stats: {e}")
            return {
                "enabled": True,
                "indexed_messages": 0,
                "error": str(e)
            }

    def add_tool_result_as_message(self, tool_name: str, output_text: str) -> None:
        """Record tool result into conversation as assistant message for follow-ups."""
        if not output_text:
            return
        self.add_message("assistant", f"Результат инструмента {tool_name}: {output_text}")