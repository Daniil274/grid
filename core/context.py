"""
Advanced context management for Grid agents with memory and persistence.
"""

from typing import List, Dict, Optional, Any
from datetime import datetime
from threading import Lock
from contextlib import contextmanager
import json
from pathlib import Path
import logging
import uuid

from schemas import ContextMessage, AgentExecution
from utils.exceptions import ContextError
# Tracing is handled automatically by Agents SDK

logger = logging.getLogger("core.context")


@contextmanager
def safe_lock(lock, timeout=5.0):
    """Context manager для безопасного использования lock'а с таймаутом."""
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise ContextError(f"Lock timeout after {timeout} seconds")
    try:
        yield lock
    finally:
        lock.release()


class ContextManager:
    """Thread-safe context manager with persistence and memory optimization."""
    
    def __init__(self, max_history: int = 15, persist_path: Optional[str] = None):
        """
        Initialize context manager.
        
        Args:
            max_history: Maximum number of messages to keep in memory
            persist_path: Optional path for persistence (JSON file)
        """
        self.max_history = max_history
        self.persist_path = Path(persist_path) if persist_path else None

        self._lock = Lock()
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self._current_context_id: Optional[str] = None

        # Active buffers are assigned via _activate_context
        self._conversation_history: List[ContextMessage] = []
        self._execution_history: List[AgentExecution] = []
        self._metadata: Dict[str, Any] = {}

        # Load from persistence if available
        if self.persist_path and self.persist_path.exists():
            self._load_from_file()
        else:
            default_context = self._create_context()
            self._activate_context(default_context)

    def _generate_context_id(self) -> str:
        """Generate a short identifier for a context session."""
        return f"ctx-{uuid.uuid4().hex[:8]}"

    def _create_context(self, context_id: Optional[str] = None) -> str:
        """Create a context bucket if it does not exist and return its ID."""
        context_key = context_id or self._generate_context_id()
        if context_key not in self._contexts:
            self._contexts[context_key] = {
                "conversation": [],
                "executions": [],
                "metadata": {},
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
        return context_key

    def _activate_context(self, context_id: str) -> str:
        """Activate an existing context bucket and expose its buffers."""
        context_key = self._create_context(context_id)
        context_bucket = self._contexts[context_key]
        self._current_context_id = context_key
        self._conversation_history = context_bucket["conversation"]
        self._execution_history = context_bucket["executions"]
        self._metadata = context_bucket["metadata"]
        context_bucket["updated_at"] = datetime.now().isoformat()
        return context_key

    def activate_context(self, context_id: str) -> str:
        """Public helper to switch to a specific context ID, creating it if needed."""
        with safe_lock(self._lock, timeout=5.0):
            active_id = self._activate_context(context_id)
            if self.persist_path:
                self._save_to_file()
            return active_id

    def start_new_context(self, context_id: Optional[str] = None) -> str:
        """Create and switch to a brand new, empty context."""
        with safe_lock(self._lock, timeout=5.0):
            new_id = self._create_context(context_id)
            bucket = self._contexts[new_id]
            bucket["conversation"] = []
            bucket["executions"] = []
            bucket["metadata"] = {}
            now_iso = datetime.now().isoformat()
            bucket["created_at"] = now_iso
            bucket["updated_at"] = now_iso
            self._activate_context(new_id)
            if self.persist_path:
                self._save_to_file()
            return new_id

    def get_current_context_id(self) -> Optional[str]:
        """Return the identifier of the active context."""
        return self._current_context_id

    def list_context_ids(self) -> List[str]:
        """Return the list of known context identifiers."""
        return list(self._contexts.keys())
    
    def add_message(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add message to conversation history.
        
        Args:
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional metadata
        """
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 сек таймаут
                try:
                    message = ContextMessage(
                        role=role,
                        content=content,
                        timestamp=datetime.now().isoformat(),
                        metadata=metadata
                    )

                    self._conversation_history.append(message)

                    # Trim history if needed
                    if len(self._conversation_history) > self.max_history:
                        self._conversation_history.pop(0)

                    # Update bookkeeping for active context
                    active_bucket = self._contexts.get(self._current_context_id)
                    if active_bucket is not None:
                        active_bucket["updated_at"] = datetime.now().isoformat()

                    # Persist if configured
                    if self.persist_path:
                        self._save_to_file()

                except Exception as e:
                    raise ContextError(f"Failed to add message: {e}")
        except ContextError as exc:
            logger.error("Lock timeout in add_message", exc_info=exc)
            raise
    
    def add_execution(self, execution: AgentExecution) -> None:
        """Add agent execution to history."""
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 сек таймаут
                self._execution_history.append(execution)

                # Keep execution history reasonable
                if len(self._execution_history) > self.max_history * 2:
                    self._execution_history.pop(0)

                active_bucket = self._contexts.get(self._current_context_id)
                if active_bucket is not None:
                    active_bucket["updated_at"] = datetime.now().isoformat()

                # Persist if configured
                if self.persist_path:
                    self._save_to_file()
        except ContextError:
            logger.warning(
                "Execution history record dropped due to lock timeout",
                extra={"agent_execution": getattr(execution, "agent_name", None)},
            )
    
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
    
    def clear_history(self) -> str:
        """Clear all conversation history by starting a new context session."""
        return self.start_new_context()
    
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
                    "current_context_id": self._current_context_id,
                    "available_contexts": list(self._contexts.keys()),
                }
        except ContextError:
            # Lock timeout in get_context_stats
            return {
                "conversation_messages": 0,
                "execution_history": 0,
                "memory_usage_mb": 0.0,
                "last_user_message": None,
                "last_assistant_message": None,
                "current_context_id": None,
                "available_contexts": [],
            }

    def get_conversation_history(self) -> List[Dict[str, Any]]:
        """Return raw conversation history as list of dicts for external consumers."""
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 сек таймаут
                return [msg.model_dump() for msg in self._conversation_history]
        except ContextError:
            logger.warning("Lock timeout in get_conversation_history")
            return []
    
    def get_last_user_message(self) -> Optional[str]:
        """Get the last user message."""
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 сек таймаут
                for msg in reversed(self._conversation_history):
                    if msg.role == "user":
                        return msg.content
            return None
        except ContextError:
            logger.warning("Lock timeout in get_last_user_message")
            return None
    
    def get_last_assistant_message(self) -> Optional[str]:
        """Get the last assistant message."""
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 сек таймаут
                for msg in reversed(self._conversation_history):
                    if msg.role == "assistant":
                        return msg.content
            return None
        except ContextError:
            logger.warning("Lock timeout in get_last_assistant_message")
            return None
    
    def set_metadata(self, key: str, value: Any) -> None:
        """Set context metadata."""
        with safe_lock(self._lock, timeout=5.0):
            self._metadata[key] = value
            bucket = self._contexts.get(self._current_context_id)
            if bucket is not None:
                bucket["updated_at"] = datetime.now().isoformat()
            if self.persist_path:
                self._save_to_file()
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get context metadata."""
        with safe_lock(self._lock, timeout=5.0):
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
                "active_context_id": self._current_context_id,
                "contexts": {}
            }

            for context_id, bucket in self._contexts.items():
                conversation_dump = []
                for msg in bucket.get("conversation", []):
                    if hasattr(msg, "model_dump"):
                        conversation_dump.append(msg.model_dump())
                    else:
                        conversation_dump.append(msg)

                execution_dump = []
                for ex in bucket.get("executions", []):
                    if hasattr(ex, "model_dump"):
                        execution_dump.append(ex.model_dump())
                    else:
                        execution_dump.append(ex)

                data["contexts"][context_id] = {
                    "conversation_history": conversation_dump,
                    "execution_history": execution_dump,
                    "metadata": bucket.get("metadata", {}),
                    "created_at": bucket.get("created_at"),
                    "updated_at": bucket.get("updated_at"),
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

            contexts_data = data.get("contexts")
            if contexts_data:
                self._contexts = {}
                for context_id, bucket in contexts_data.items():
                    conversation = [
                        ContextMessage(**msg) if isinstance(msg, dict) else msg
                        for msg in bucket.get("conversation_history", [])
                    ]
                    executions = [
                        AgentExecution(**ex) if isinstance(ex, dict) else ex
                        for ex in bucket.get("execution_history", [])
                    ]
                    metadata = bucket.get("metadata", {})
                    self._contexts[context_id] = {
                        "conversation": conversation,
                        "executions": executions,
                        "metadata": metadata,
                        "created_at": bucket.get("created_at"),
                        "updated_at": bucket.get("updated_at"),
                    }

                active_id = data.get("active_context_id")
                if active_id and active_id in self._contexts:
                    self._activate_context(active_id)
                elif self._contexts:
                    # Pick the most recently updated context
                    sorted_contexts = sorted(
                        self._contexts.items(),
                        key=lambda item: item[1].get("updated_at", ""),
                        reverse=True,
                    )
                    self._activate_context(sorted_contexts[0][0])
                else:
                    default_context = self._create_context()
                    self._activate_context(default_context)
            else:
                # Backwards compatibility with legacy single-context format
                default_context = self._create_context(data.get("context_id"))
                conversation = [
                    ContextMessage(**msg) if isinstance(msg, dict) else msg
                    for msg in data.get("conversation_history", [])
                ]
                executions = [
                    AgentExecution(**ex) if isinstance(ex, dict) else ex
                    for ex in data.get("execution_history", [])
                ]
                bucket = self._contexts[default_context]
                bucket["conversation"] = conversation
                bucket["executions"] = executions
                bucket["metadata"] = data.get("metadata", {})
                bucket["updated_at"] = datetime.now().isoformat()
                self._activate_context(default_context)
            
        except Exception as e:
            # Failed to load context
            logger.error(f"Failed to load context from {self.persist_path}: {e}")
            # Reset to empty state on failure
            self._conversation_history = []
            self._execution_history = []
            self._metadata = {}
            self._contexts = {}
            self._current_context_id = None
            fallback_context = self._create_context()
            self._activate_context(fallback_context)
    
  
    
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

    def add_tool_result_as_message(self, tool_name: str, output_text: str) -> None:
        """Record tool result into conversation as assistant message for follow-ups."""
        if not output_text:
            return
        self.add_message("assistant", f"Результат инструмента {tool_name}: {output_text}")
