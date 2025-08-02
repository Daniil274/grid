"""
Advanced context management for Grid agents with memory and persistence.
"""

from typing import List, Dict, Optional, Any
from datetime import datetime
from threading import Lock
import json
from pathlib import Path

from schemas import ContextMessage, AgentExecution
from utils.exceptions import ContextError
from utils.logger import Logger

logger = Logger(__name__)


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
        
        self._conversation_history: List[ContextMessage] = []
        self._execution_history: List[AgentExecution] = []
        self._metadata: Dict[str, Any] = {}
        self._lock = Lock()
        
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
        with self._lock:
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
                    removed = self._conversation_history.pop(0)
                    logger.debug(f"Removed old message from context: {removed.role}")
                
                logger.debug(f"Added {role} message to context")
                
                # Persist if configured
                if self.persist_path:
                    self._save_to_file()
                    
            except Exception as e:
                raise ContextError(f"Failed to add message: {e}")
    
    def add_execution(self, execution: AgentExecution) -> None:
        """Add agent execution to history."""
        with self._lock:
            self._execution_history.append(execution)
            
            # Keep execution history reasonable
            if len(self._execution_history) > self.max_history * 2:
                self._execution_history.pop(0)
    
    def get_conversation_context(self, last_n: Optional[int] = None) -> str:
        """
        Get formatted conversation context.
        
        Args:
            last_n: Number of last messages to include (default: all)
            
        Returns:
            Formatted context string
        """
        with self._lock:
            if not self._conversation_history:
                return ""
            
            messages = self._conversation_history
            if last_n:
                messages = messages[-last_n:]
            
            context_parts = ["📋 Контекст предыдущих сообщений:"]
            
            for i, msg in enumerate(messages, 1):
                role_emoji = self._get_role_emoji(msg.role)
                
                # Truncate long messages
                content = msg.content
                if len(content) > 200:
                    content = content[:200] + "..."
                
                context_parts.append(f"{i}. {role_emoji} {msg.role.title()}: {content}")
            
            context_parts.append("\\nПожалуйста, учитывай этот контекст при ответе.")
            return "\\n".join(context_parts)
    
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
            
            logger.info(f"Cleared {cleared_count} messages from context")
            
            # Clear persistence file
            if self.persist_path and self.persist_path.exists():
                self.persist_path.unlink()
    
    def get_context_stats(self) -> Dict[str, Any]:
        """Get context statistics."""
        with self._lock:
            return {
                "conversation_messages": len(self._conversation_history),
                "execution_history": len(self._execution_history),
                "memory_usage_mb": self._estimate_memory_usage(),
                "last_user_message": self.get_last_user_message(),
                "last_assistant_message": self.get_last_assistant_message(),
            }
    
    def get_last_user_message(self) -> Optional[str]:
        """Get the last user message."""
        with self._lock:
            for msg in reversed(self._conversation_history):
                if msg.role == "user":
                    return msg.content
            return None
    
    def get_last_assistant_message(self) -> Optional[str]:
        """Get the last assistant message."""
        with self._lock:
            for msg in reversed(self._conversation_history):
                if msg.role == "assistant":
                    return msg.content
            return None
    
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
        if not self.persist_path:
            return
        
        try:
            data = {
                "conversation_history": [msg.model_dump() for msg in self._conversation_history],
                "execution_history": [ex.model_dump() for ex in self._execution_history],
                "metadata": self._metadata,
                "saved_at": datetime.now().isoformat()
            }
            
            # Ensure directory exists
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.persist_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save context: {e}")
    
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
            
            logger.info(f"Loaded context from {self.persist_path}")
            
        except Exception as e:
            logger.error(f"Failed to load context: {e}")
            # Reset to empty state on failure
            self._conversation_history = []
            self._execution_history = []
            self._metadata = {}