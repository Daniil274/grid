"""
Advanced context management for Grid agents with memory and persistence.
"""

from typing import List, Dict, Optional, Any, Union
from datetime import datetime
from threading import Lock
from contextlib import contextmanager
import json
from pathlib import Path
import uuid
from utils.logger import Logger

from schemas import ContextMessage, AgentExecution
from utils.exceptions import ContextError
from utils.image_utils import ImageUtils
# Tracing is handled automatically by Agents SDK

logger = Logger.get_logger("context")


@contextmanager
def safe_lock(lock, timeout=5.0):
    """Context manager for safe lock usage with timeout."""
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise ContextError(f"Lock timeout after {timeout} seconds")
    try:
        yield lock
    finally:
        lock.release()


class ContextManager:
    """Thread-safe context manager with persistence and memory optimization."""
    
    def __init__(
        self,
        max_history: int = 15,
        persist_path: Optional[str] = None,
        *,
        read_only: bool = False,
        create_initial_context: bool = True,
    ):
        """
        Initialize context manager.
        
        Args:
            max_history: Maximum number of messages to keep in memory
            persist_path: Optional path for persistence (JSON file)
            read_only: When True, never write persistence and do not create a fresh
                empty session context after loading (inspector mode)
            create_initial_context: When False, keep only loaded buckets and may
                leave no active context if persistence is empty
        """
        self.max_history = max_history
        self.persist_path = Path(persist_path) if persist_path else None
        self.read_only = bool(read_only)

        self._lock = Lock()
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self._current_context_id: Optional[str] = None

        # Active buffers are assigned via _activate_context
        self._conversation_history: List[ContextMessage] = []
        self._execution_history: List[AgentExecution] = []
        self._metadata: Dict[str, Any] = {}

        # Load from persistence if available (but don't auto-activate old contexts)
        if self.persist_path and self.persist_path.exists():
            self._load_from_file(auto_activate=False, normalize_and_save=not self.read_only)

        if create_initial_context and not self.read_only:
            # Always start with a fresh context
            # Old contexts are preserved and accessible by Context ID
            new_context = self._create_context()
            self._activate_context(new_context)
            logger.info(f"Started new context session: {new_context}")
        elif self._contexts and self._current_context_id is None:
            # Inspector / read-only: expose the newest bucket without creating a new session
            sorted_contexts = sorted(
                self._contexts.items(),
                key=lambda item: item[1].get("updated_at") or item[1].get("created_at") or "",
                reverse=True,
            )
            self._activate_context(sorted_contexts[0][0])
            logger.info(
                "Loaded contexts in read-only/inspect mode; active=%s count=%s",
                self._current_context_id,
                len(self._contexts),
            )
        else:
            logger.info(
                "Context manager ready without new session (read_only=%s, buckets=%s)",
                self.read_only,
                len(self._contexts),
            )

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
            if self.persist_path and not self.read_only:
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
            if self.persist_path and not self.read_only:
                self._save_to_file()
            return new_id

    def get_current_context_id(self) -> Optional[str]:
        """Return the identifier of the active context."""
        return self._current_context_id

    def list_context_ids(self) -> List[str]:
        """Return the list of known context identifiers."""
        return list(self._contexts.keys())

    def list_context_summaries(self) -> List[Dict[str, Any]]:
        """Return safe, read-only summaries for all known context buckets."""
        try:
            with safe_lock(self._lock, timeout=5.0):
                summaries = []
                for context_id, bucket in self._contexts.items():
                    summaries.append(
                        self._build_context_summary_unlocked(
                            context_id,
                            bucket,
                            is_active=context_id == self._current_context_id,
                        )
                    )
                summaries.sort(
                    key=lambda item: item.get("updated_at") or item.get("created_at") or "",
                    reverse=True,
                )
                return summaries
        except ContextError:
            logger.warning("Lock timeout in list_context_summaries")
            return []

    def get_context_bucket(
        self,
        context_id: str,
        *,
        include_messages: bool = True,
        include_executions: bool = True,
        include_assembly: bool = True,
        include_raw_metadata: bool = False,
        preview_limit: int = 240,
        include_full_messages: bool = False,
        include_full_executions: bool = False,
        include_full_runtime: bool = False,
        max_messages: Optional[int] = None,
        max_executions: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return a copied, redacted view of one context bucket."""
        try:
            with safe_lock(self._lock, timeout=5.0):
                bucket = self._contexts.get(context_id)
                if bucket is None:
                    return None
                return self._build_context_detail_unlocked(
                    context_id,
                    bucket,
                    is_active=context_id == self._current_context_id,
                    include_messages=include_messages,
                    include_executions=include_executions,
                    include_assembly=include_assembly,
                    include_raw_metadata=include_raw_metadata,
                    preview_limit=preview_limit,
                    include_full_messages=include_full_messages,
                    include_full_executions=include_full_executions,
                    include_full_runtime=include_full_runtime,
                    max_messages=max_messages,
                    max_executions=max_executions,
                )
        except ContextError:
            logger.warning("Lock timeout in get_context_bucket")
            return None

    def get_context_assembly(self, context_id: str) -> Optional[Dict[str, Any]]:
        """Return last stored model-context assembly for a bucket."""
        try:
            with safe_lock(self._lock, timeout=5.0):
                bucket = self._contexts.get(context_id)
                if bucket is None:
                    return None
                metadata = bucket.get("metadata") or {}
                assembly = metadata.get("last_context_assembly")
                if not isinstance(assembly, dict):
                    return {
                        "context_id": context_id,
                        "available": False,
                        "assembly": None,
                    }
                return {
                    "context_id": context_id,
                    "available": True,
                    "assembly": self._sanitize_assembly_unlocked(assembly),
                }
        except ContextError:
            logger.warning("Lock timeout in get_context_assembly")
            return None

    def get_context_messages(
        self,
        context_id: str,
        *,
        limit: Optional[int] = None,
        preview_limit: int = 500,
        include_full: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return sanitized conversation messages for one context."""
        try:
            with safe_lock(self._lock, timeout=5.0):
                bucket = self._contexts.get(context_id)
                if bucket is None:
                    return None
                messages = bucket.get("conversation") or []
                if limit is not None and limit >= 0:
                    messages = messages[-limit:]
                return {
                    "context_id": context_id,
                    "count": len(bucket.get("conversation") or []),
                    "messages": [
                        self._sanitize_message_unlocked(
                            msg,
                            preview_limit=preview_limit,
                            include_full=include_full,
                        )
                        for msg in messages
                    ],
                }
        except ContextError:
            logger.warning("Lock timeout in get_context_messages")
            return None

    def get_context_executions(
        self,
        context_id: str,
        *,
        limit: Optional[int] = None,
        preview_limit: int = 500,
        include_full: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return sanitized execution history for one context."""
        try:
            with safe_lock(self._lock, timeout=5.0):
                bucket = self._contexts.get(context_id)
                if bucket is None:
                    return None
                executions = bucket.get("executions") or []
                if limit is not None and limit >= 0:
                    executions = executions[-limit:]
                return {
                    "context_id": context_id,
                    "count": len(bucket.get("executions") or []),
                    "executions": [
                        self._sanitize_execution_unlocked(
                            execution,
                            preview_limit=preview_limit,
                            include_full=include_full,
                        )
                        for execution in executions
                    ],
                }
        except ContextError:
            logger.warning("Lock timeout in get_context_executions")
            return None

    def add_message(self, role: str, content: Union[str, List[Any]], metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add message to conversation history.

        Args:
            role: Message role (user, assistant, system)
            content: Message content (string or list of content parts)
            metadata: Optional metadata
        """
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 sec timeout
                try:
                    # Normalize content to convert file images to base64
                    # This ensures images remain accessible after restart
                    normalized_content = self._normalize_message_content(content)

                    message = ContextMessage(
                        role=role,
                        content=normalized_content,
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
                    if self.persist_path and not self.read_only:
                        self._save_to_file()

                except Exception as e:
                    raise ContextError(f"Failed to add message: {e}")
        except ContextError as exc:
            logger.error("Lock timeout in add_message", exc_info=exc)
            raise

    def replace_conversation_history(self, messages: List[ContextMessage]) -> None:
        """Replace the active conversation history with a new message list."""
        try:
            with safe_lock(self._lock, timeout=5.0):
                self._conversation_history.clear()
                self._conversation_history.extend(messages[-self.max_history:])

                active_bucket = self._contexts.get(self._current_context_id)
                if active_bucket is not None:
                    active_bucket["updated_at"] = datetime.now().isoformat()

                if self.persist_path and not self.read_only:
                    self._save_to_file()
        except ContextError as exc:
            logger.error("Lock timeout in replace_conversation_history", exc_info=exc)
            raise
    
    def add_execution(self, execution: AgentExecution) -> None:
        """Add agent execution to history."""
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 sec timeout
                self._execution_history.append(execution)

                # Keep execution history reasonable
                if len(self._execution_history) > self.max_history * 2:
                    self._execution_history.pop(0)

                active_bucket = self._contexts.get(self._current_context_id)
                if active_bucket is not None:
                    active_bucket["updated_at"] = datetime.now().isoformat()

                # Persist if configured
                if self.persist_path and not self.read_only:
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
                lines = ["Previous dialogue (compressed):"]
                from utils.multimodal_converter import MultimodalConverter

                for msg in messages:
                    role = {
                        "user": "User",
                        "assistant": "Assistant",
                        "system": "System"
                    }.get(msg.role, msg.role)

                    raw_content = msg.content
                    if isinstance(raw_content, str):
                        content = raw_content.strip()
                    else:
                        # Extract textual summary from multimodal content to keep context readable
                        try:
                            content = MultimodalConverter.extract_text_from_multimodal(raw_content).strip()
                        except Exception:
                            content = "[multimodal content]"

                    # Hard trim very long single messages to keep prompt lightweight
                    if len(content) > 2000:
                        content = content[:2000] + "…"
                    lines.append(f"{role}: {content}")
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
                # Direct access to data within lock to avoid deadlocks
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
            with safe_lock(self._lock, timeout=5.0):  # 5 sec timeout
                return [msg.model_dump() for msg in self._conversation_history]
        except ContextError:
            logger.warning("Lock timeout in get_conversation_history")
            return []
    
    def get_conversation_history_as_sdk_messages(self, last_n: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get conversation history as list of messages in Agents SDK format.
        
        Converts ContextMessage objects to SDK format with support for multimodal content.
        This is used when session is disabled (e.g., for multimodal messages with images).
        
        Args:
            last_n: Number of last messages to include (default: all)
            
        Returns:
            List of messages in SDK format: [{"role": "user", "content": ...}, ...]
        """
        try:
            with safe_lock(self._lock, timeout=5.0):
                if not self._conversation_history:
                    return []
                
                messages = self._conversation_history
                if last_n:
                    messages = messages[-last_n:]
                
                # Convert ContextMessage to SDK format
                from utils.multimodal_converter import MultimodalConverter
                sdk_messages = []
                
                for msg in messages:
                    # Convert to SDK format
                    if isinstance(msg.content, str):
                        # Simple text message
                        sdk_messages.append({
                            "role": msg.role,
                            "content": msg.content
                        })
                    else:
                        # Multimodal message - convert content parts
                        # Check if message has images for logging
                        has_imgs = msg.has_images() if hasattr(msg, "has_images") else False
                        if has_imgs:
                            logger.debug(f"Converting multimodal message with images to SDK format (role: {msg.role})")
                        
                        content = MultimodalConverter.context_message_to_agents_sdk(msg)
                        if isinstance(content, str):
                            sdk_messages.append({
                                "role": msg.role,
                                "content": content
                            })
                        else:
                            # List of content parts
                            # Verify images are present in converted content
                            img_count = sum(1 for part in content if isinstance(part, dict) and part.get("type") == "input_image")
                            if has_imgs and img_count == 0:
                                logger.warning(f"Image lost during conversion! Original had images but converted content doesn't")
                            elif img_count > 0:
                                logger.debug(f"Converted message has {img_count} image(s) in SDK format")
                            
                            sdk_messages.append({
                                "role": msg.role,
                                "content": content
                            })
                
                return sdk_messages
        except ContextError:
            logger.warning("Lock timeout in get_conversation_history_as_sdk_messages")
            return []
        except Exception as e:
            logger.error(f"Failed to convert conversation history to SDK format: {e}")
            return []
    
    def get_last_user_message(self) -> Optional[str]:
        """Get the last user message."""
        try:
            with safe_lock(self._lock, timeout=5.0):  # 5 sec timeout
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
            with safe_lock(self._lock, timeout=5.0):  # 5 sec timeout
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
            if self.persist_path and not self.read_only:
                self._save_to_file()
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get context metadata."""
        with safe_lock(self._lock, timeout=5.0):
            return self._metadata.get(key, default)

    def get_all_metadata(self) -> Dict[str, Any]:
        """Get all context metadata."""
        with safe_lock(self._lock, timeout=5.0):
            return self._metadata.copy()

    def append_metadata_event(
        self,
        key: str,
        event: Dict[str, Any],
        *,
        max_items: int = 100,
    ) -> List[Dict[str, Any]]:
        """Append an event dict to a metadata list and persist it."""
        with safe_lock(self._lock, timeout=5.0):
            current = self._metadata.get(key)
            items: List[Dict[str, Any]]
            if isinstance(current, list):
                items = [item for item in current if isinstance(item, dict)]
            else:
                items = []

            items.append(event)
            if max_items > 0 and len(items) > max_items:
                items = items[-max_items:]

            self._metadata[key] = items
            bucket = self._contexts.get(self._current_context_id)
            if bucket is not None:
                bucket["updated_at"] = datetime.now().isoformat()
            if self.persist_path and not self.read_only:
                self._save_to_file()
            return list(items)

    def get_incomplete_run_summary(
        self,
        *,
        max_events: int = 8,
        max_field_length: int = 300,
    ) -> str:
        """Build a compact prompt-safe summary for an unfinished previous run."""
        with safe_lock(self._lock, timeout=5.0):
            payload = self._metadata.get("pending_agent_run")
            if not isinstance(payload, dict):
                return ""

            status = str(payload.get("status") or "").strip() or "unknown"
            if status == "completed":
                return ""

            lines = [
                "Unfinished previous execution attempt:",
            ]

            agent = payload.get("agent")
            if agent:
                lines.append(f"Agent: {agent}")

            last_error = payload.get("last_error")
            if isinstance(last_error, str) and last_error.strip():
                err = last_error.strip()
                if len(err) > max_field_length:
                    err = err[:max_field_length] + "…"
                lines.append(f"Last error: {err}")

            retries = payload.get("retry_count")
            if retries is not None:
                lines.append(f"Retry count: {retries}")

            input_preview = payload.get("input_preview")
            if isinstance(input_preview, str) and input_preview.strip():
                preview = input_preview.strip()
                if len(preview) > max_field_length:
                    preview = preview[:max_field_length] + "…"
                lines.append(f"Original request: {preview}")

            tool_events = payload.get("tool_events")
            if isinstance(tool_events, list):
                compact_events = [item for item in tool_events if isinstance(item, dict)][-max_events:]
            else:
                compact_events = []

            if compact_events:
                lines.append("Recent execution events:")
                for item in compact_events:
                    event_type = str(item.get("event_type") or "event")
                    tool_name = str(item.get("tool_name") or "").strip()
                    details = []
                    if tool_name:
                        details.append(tool_name)
                    arguments = item.get("arguments")
                    if arguments is not None and event_type == "tool_called":
                        arg_text = str(arguments).strip()
                        if len(arg_text) > max_field_length:
                            arg_text = arg_text[:max_field_length] + "…"
                        details.append(f"args={arg_text}")
                    output = item.get("output")
                    if output is not None and event_type == "tool_output":
                        output_text = str(output).strip()
                        if len(output_text) > max_field_length:
                            output_text = output_text[:max_field_length] + "…"
                        details.append(f"output={output_text}")
                    suffix = " | ".join(part for part in details if part)
                    if suffix:
                        lines.append(f"- {event_type}: {suffix}")
                    else:
                        lines.append(f"- {event_type}")

            lines.append(
                "If appropriate, continue taking into account already completed steps and do not repeat completed operations unnecessarily."
            )
            return "\n".join(lines)

    def _normalize_message_content(self, content: Union[str, List[Any]]) -> Union[str, List[Any]]:
        """
        Normalize message content by converting file images to base64.

        This ensures that images remain accessible even after file system changes
        or when context is restored in a new session.

        Args:
            content: Message content (string or list of content parts)

        Returns:
            Normalized content with file images converted to base64
        """
        if isinstance(content, str):
            return content

        # Import here to avoid circular dependency
        from schemas import FileImageContent, ImageContent, ImageUrl, TextContent

        normalized_parts = []
        for part in content:
            if isinstance(part, FileImageContent):
                # Convert file to base64
                base64_url = ImageUtils.file_to_base64(part.file_path)
                if base64_url:
                    # Replace FileImageContent with ImageContent containing base64
                    normalized_parts.append(ImageContent(
                        type="image_url",
                        image_url=ImageUrl(url=base64_url, detail=part.detail or "auto")
                    ))
                    logger.debug(f"Converted file image to base64: {part.file_path}")
                else:
                    logger.warning(f"Failed to convert image file to base64: {part.file_path}")
                    # Keep original part even if conversion failed
                    normalized_parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "image_file":
                # Handle dict-based file image content
                file_path = part.get("file_path")
                if file_path:
                    base64_url = ImageUtils.file_to_base64(file_path)
                    if base64_url:
                        normalized_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": base64_url,
                                "detail": part.get("detail", "auto")
                            }
                        })
                        logger.debug(f"Converted file image dict to base64: {file_path}")
                    else:
                        logger.warning(f"Failed to convert image file dict to base64: {file_path}")
                        normalized_parts.append(part)
                else:
                    normalized_parts.append(part)
            else:
                # Keep other parts as-is
                normalized_parts.append(part)

        return normalized_parts if normalized_parts else content

    def _normalize_loaded_message(self, msg: ContextMessage) -> ContextMessage:
        """
        Normalize a loaded message by converting file images to base64.

        This is used when loading messages from persistence to ensure
        file images are converted to base64.

        Args:
            msg: ContextMessage to normalize

        Returns:
            Normalized ContextMessage
        """
        normalized_content = self._normalize_message_content(msg.content)

        # Only create new message if content changed
        if normalized_content is not msg.content:
            return ContextMessage(
                role=msg.role,
                content=normalized_content,
                timestamp=msg.timestamp,
                metadata=msg.metadata
            )
        return msg

    @staticmethod
    def _truncate_text(value: Any, limit: int) -> str:
        text = "" if value is None else str(value)
        if limit is not None and limit >= 0 and len(text) > limit:
            return text[:limit] + "…"
        return text

    @classmethod
    def _content_preview(
        cls,
        content: Any,
        *,
        preview_limit: int = 240,
    ) -> Dict[str, Any]:
        """Build a compact, image-safe preview of message content."""
        has_image = False
        image_count = 0
        part_types: List[str] = []
        text_parts: List[str] = []

        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    part_type = str(part.get("type") or "part")
                    part_types.append(part_type)
                    if part_type in {"image_url", "input_image", "image_file"}:
                        has_image = True
                        image_count += 1
                        continue
                    if part_type in {"text", "input_text"}:
                        text_parts.append(str(part.get("text") or part.get("content") or ""))
                    elif "text" in part:
                        text_parts.append(str(part.get("text") or ""))
                else:
                    part_type = getattr(part, "type", None) or type(part).__name__
                    part_types.append(str(part_type))
                    if str(part_type) in {"image_url", "input_image", "image_file"}:
                        has_image = True
                        image_count += 1
                        continue
                    text_value = getattr(part, "text", None)
                    if text_value is not None:
                        text_parts.append(str(text_value))
                    else:
                        text_parts.append(str(part))
        elif content is not None:
            text_parts.append(str(content))

        preview = "\n".join(part for part in text_parts if part).strip()
        if not preview and has_image:
            preview = f"[{image_count} image(s)]"
        elif not preview:
            preview = ""

        return {
            "preview": cls._truncate_text(preview, preview_limit),
            "length": len(preview),
            "has_image": has_image,
            "image_count": image_count,
            "part_types": part_types,
            "is_multimodal": not isinstance(content, str),
        }

    def _sanitize_message_unlocked(
        self,
        msg: Any,
        *,
        preview_limit: int = 240,
        include_full: bool = False,
    ) -> Dict[str, Any]:
        if hasattr(msg, "model_dump"):
            payload = msg.model_dump()
        elif isinstance(msg, dict):
            payload = dict(msg)
        else:
            payload = {
                "role": getattr(msg, "role", "unknown"),
                "content": getattr(msg, "content", ""),
                "timestamp": getattr(msg, "timestamp", None),
                "metadata": getattr(msg, "metadata", None),
            }

        content = payload.get("content")
        preview = self._content_preview(content, preview_limit=preview_limit)
        result = {
            "role": payload.get("role"),
            "timestamp": payload.get("timestamp"),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            "preview": preview["preview"],
            "content_length": preview["length"],
            "has_image": preview["has_image"],
            "image_count": preview["image_count"],
            "is_multimodal": preview["is_multimodal"],
        }
        if include_full and isinstance(content, str) and not preview["has_image"]:
            result["content"] = content
        elif include_full and isinstance(content, list):
            # Only return non-image textual parts to avoid shipping huge base64 blobs.
            safe_parts = []
            for part in content:
                if isinstance(part, dict):
                    part_type = str(part.get("type") or "")
                    if part_type in {"image_url", "input_image", "image_file"}:
                        safe_parts.append(
                            {
                                "type": part_type,
                                "omitted": True,
                                "reason": "image_payload_omitted",
                            }
                        )
                    else:
                        safe_parts.append(part)
                else:
                    part_type = str(getattr(part, "type", type(part).__name__))
                    if part_type in {"image_url", "input_image", "image_file"}:
                        safe_parts.append(
                            {
                                "type": part_type,
                                "omitted": True,
                                "reason": "image_payload_omitted",
                            }
                        )
                    elif hasattr(part, "model_dump"):
                        safe_parts.append(part.model_dump())
                    else:
                        safe_parts.append(str(part))
            result["content_parts"] = safe_parts
        return result

    def _sanitize_execution_unlocked(
        self,
        execution: Any,
        *,
        preview_limit: int = 500,
        include_full: bool = False,
    ) -> Dict[str, Any]:
        if hasattr(execution, "model_dump"):
            payload = execution.model_dump()
        elif isinstance(execution, dict):
            payload = dict(execution)
        else:
            payload = {
                "agent_name": getattr(execution, "agent_name", None),
                "start_time": getattr(execution, "start_time", None),
                "end_time": getattr(execution, "end_time", None),
                "input_message": getattr(execution, "input_message", None),
                "output": getattr(execution, "output", None),
                "error": getattr(execution, "error", None),
                "context_id": getattr(execution, "context_id", None),
            }

        result = {
            "agent_name": payload.get("agent_name"),
            "start_time": payload.get("start_time"),
            "end_time": payload.get("end_time"),
            "duration": payload.get("duration"),
            "context_id": payload.get("context_id"),
            "error": self._truncate_text(payload.get("error"), preview_limit) if payload.get("error") else None,
            "input_preview": self._truncate_text(payload.get("input_message"), preview_limit),
            "output_preview": self._truncate_text(payload.get("output"), preview_limit),
            "input_length": len(str(payload.get("input_message") or "")),
            "output_length": len(str(payload.get("output") or "")),
            "has_error": bool(payload.get("error")),
        }
        if include_full:
            result["input"] = payload.get("input_message")
            result["output"] = payload.get("output")
            result["error_full"] = payload.get("error")
        return result

    def _sanitize_assembly_unlocked(self, assembly: Dict[str, Any]) -> Dict[str, Any]:
        sections_in = assembly.get("sections") if isinstance(assembly.get("sections"), list) else []
        sections: List[Dict[str, Any]] = []
        total_from_sections = 0
        for section in sections_in:
            if not isinstance(section, dict):
                continue
            content = section.get("content")
            length = section.get("length")
            if length is None:
                length = len(str(content or section.get("preview") or ""))
            total_from_sections += int(length or 0)
            preview = section.get("preview")
            if preview is None and content is not None:
                preview = self._truncate_text(content, 240)
            sections.append(
                {
                    "key": section.get("key"),
                    "scope": section.get("scope") or "dynamic",
                    "length": int(length or 0),
                    "preview": self._truncate_text(preview, 240),
                    "content": content if isinstance(content, str) else None,
                }
            )

        instruction_length = assembly.get("instruction_length")
        if instruction_length is None:
            instruction_length = total_from_sections

        return {
            "context_id": assembly.get("context_id"),
            "history_strategy": assembly.get("history_strategy"),
            "instruction_length": instruction_length,
            "section_count": len(sections),
            "sections": sections,
            "metadata": dict(assembly.get("metadata") or {}) if isinstance(assembly.get("metadata"), dict) else {},
        }

    def _sanitize_pending_run_unlocked(
        self,
        pending: Any,
        *,
        include_full: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(pending, dict):
            return None
        events_in = pending.get("tool_events") if isinstance(pending.get("tool_events"), list) else []
        events: List[Dict[str, Any]] = []
        for item in events_in:
            if not isinstance(item, dict):
                continue
            events.append(
                {
                    "event_type": item.get("event_type"),
                    "timestamp": item.get("timestamp"),
                    "tool_name": item.get("tool_name"),
                    "arguments": self._truncate_text(item.get("arguments"), None if include_full else 500)
                    if item.get("arguments") is not None
                    else None,
                    "output": self._truncate_text(item.get("output"), None if include_full else 500)
                    if item.get("output") is not None
                    else None,
                }
            )
        return {
            "agent": pending.get("agent"),
            "context_id": pending.get("context_id"),
            "status": pending.get("status"),
            "retry_count": pending.get("retry_count"),
            "updated_at": pending.get("updated_at"),
            "input_preview": self._truncate_text(pending.get("input_preview"), 300)
            if pending.get("input_preview") is not None
            else None,
            "last_error": self._truncate_text(pending.get("last_error"), 500)
            if pending.get("last_error") is not None
            else None,
            "tool_events": events,
            "tool_event_count": len(events),
        }

    def _build_context_summary_unlocked(
        self,
        context_id: str,
        bucket: Dict[str, Any],
        *,
        is_active: bool,
    ) -> Dict[str, Any]:
        conversation = bucket.get("conversation") or []
        executions = bucket.get("executions") or []
        metadata = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else {}

        last_invocation = metadata.get("last_invocation") if isinstance(metadata.get("last_invocation"), dict) else {}
        assembly = metadata.get("last_context_assembly") if isinstance(metadata.get("last_context_assembly"), dict) else None
        pending = self._sanitize_pending_run_unlocked(metadata.get("pending_agent_run"))

        agent = (
            last_invocation.get("agent")
            or (assembly or {}).get("metadata", {}).get("agent_key")
            if isinstance((assembly or {}).get("metadata"), dict)
            else last_invocation.get("agent")
        )
        if agent is None and isinstance(assembly, dict):
            agent = (assembly.get("metadata") or {}).get("agent_key")

        last_message_preview = None
        last_message_role = None
        if conversation:
            last_msg = conversation[-1]
            sanitized = self._sanitize_message_unlocked(last_msg, preview_limit=160)
            last_message_preview = sanitized.get("preview")
            last_message_role = sanitized.get("role")

        return {
            "id": context_id,
            "is_active": is_active,
            "created_at": bucket.get("created_at"),
            "updated_at": bucket.get("updated_at"),
            "message_count": len(conversation),
            "execution_count": len(executions),
            "agent": agent,
            "user_id": metadata.get("user_id"),
            "history_strategy": (assembly or {}).get("history_strategy") if assembly else None,
            "instruction_length": (assembly or {}).get("instruction_length") if assembly else None,
            "section_count": len((assembly or {}).get("sections") or []) if assembly else 0,
            "has_assembly": assembly is not None,
            "pending_status": pending.get("status") if pending else None,
            "tool_event_count": pending.get("tool_event_count") if pending else 0,
            "last_message_role": last_message_role,
            "last_message_preview": last_message_preview,
            "metadata_keys": sorted(str(key) for key in metadata.keys()),
        }

    def _build_context_detail_unlocked(
        self,
        context_id: str,
        bucket: Dict[str, Any],
        *,
        is_active: bool,
        include_messages: bool = True,
        include_executions: bool = True,
        include_assembly: bool = True,
        include_raw_metadata: bool = False,
        preview_limit: int = 240,
        include_full_messages: bool = False,
        include_full_executions: bool = False,
        include_full_runtime: bool = False,
        max_messages: Optional[int] = None,
        max_executions: Optional[int] = None,
    ) -> Dict[str, Any]:
        summary = self._build_context_summary_unlocked(
            context_id,
            bucket,
            is_active=is_active,
        )
        metadata = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else {}
        detail: Dict[str, Any] = {
            **summary,
            "metadata_summary": {
                "context_id": metadata.get("context_id") or context_id,
                "user_id": metadata.get("user_id"),
                "last_invocation": metadata.get("last_invocation")
                if isinstance(metadata.get("last_invocation"), dict)
                else None,
                "has_agent_instructions": bool(metadata.get("agent_instructions")),
                "metadata_keys": sorted(str(key) for key in metadata.keys()),
            },
            "pending_agent_run": self._sanitize_pending_run_unlocked(
                metadata.get("pending_agent_run"),
                include_full=include_full_runtime,
            ),
        }

        if include_assembly:
            assembly = metadata.get("last_context_assembly")
            detail["assembly"] = (
                self._sanitize_assembly_unlocked(assembly)
                if isinstance(assembly, dict)
                else None
            )

        if include_messages:
            messages = list(bucket.get("conversation") or [])
            if max_messages is not None and max_messages >= 0:
                messages = messages[-max_messages:]
            detail["messages"] = [
                self._sanitize_message_unlocked(
                    msg,
                    preview_limit=preview_limit,
                    include_full=include_full_messages,
                )
                for msg in messages
            ]

        if include_executions:
            executions = list(bucket.get("executions") or [])
            if max_executions is not None and max_executions >= 0:
                executions = executions[-max_executions:]
            detail["executions"] = [
                self._sanitize_execution_unlocked(
                    execution,
                    preview_limit=preview_limit,
                    include_full=include_full_executions,
                )
                for execution in executions
            ]

        if include_raw_metadata:
            # Keep raw keys, but never ship huge base64 blobs / full image payloads.
            safe_metadata: Dict[str, Any] = {}
            for key, value in metadata.items():
                if key == "last_context_assembly" and isinstance(value, dict):
                    safe_metadata[key] = self._sanitize_assembly_unlocked(value)
                elif key == "pending_agent_run":
                    safe_metadata[key] = self._sanitize_pending_run_unlocked(value)
                elif key == "agent_instructions" and isinstance(value, str):
                    safe_metadata[key] = self._truncate_text(value, 2000)
                else:
                    try:
                        dumped = json.dumps(value, ensure_ascii=False, default=str)
                    except Exception:
                        dumped = str(value)
                    safe_metadata[key] = (
                        json.loads(dumped)
                        if len(dumped) <= 4000 and dumped not in {"", "null"}
                        else self._truncate_text(dumped, 4000)
                    )
            detail["raw_metadata"] = safe_metadata

        return detail

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
        if self.read_only or not self.persist_path:
            return
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
    
    def _load_from_file(self, auto_activate: bool = True, normalize_and_save: bool = True) -> None:
        """
        Load context from persistence file.

        Args:
            auto_activate: If True, automatically activate the last used context.
                          If False, just load contexts without activating any.
        """
        try:
            with open(self.persist_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            contexts_data = data.get("contexts")
            if contexts_data:
                self._contexts = {}
                for context_id, bucket in contexts_data.items():
                    # Load and normalize messages to convert file images to base64
                    conversation = []
                    for msg in bucket.get("conversation_history", []):
                        loaded_msg = ContextMessage(**msg) if isinstance(msg, dict) else msg
                        normalized_msg = self._normalize_loaded_message(loaded_msg)
                        conversation.append(normalized_msg)
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

                # Only activate context if auto_activate is True
                if auto_activate:
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

                logger.info(f"Loaded {len(self._contexts)} context(s) from persistence")
            else:
                # Backwards compatibility with legacy single-context format
                default_context = self._create_context(data.get("context_id"))
                # Load and normalize messages to convert file images to base64
                conversation = []
                for msg in data.get("conversation_history", []):
                    loaded_msg = ContextMessage(**msg) if isinstance(msg, dict) else msg
                    normalized_msg = self._normalize_loaded_message(loaded_msg)
                    conversation.append(normalized_msg)
                executions = [
                    AgentExecution(**ex) if isinstance(ex, dict) else ex
                    for ex in data.get("execution_history", [])
                ]
                bucket = self._contexts[default_context]
                bucket["conversation"] = conversation
                bucket["executions"] = executions
                bucket["metadata"] = data.get("metadata", {})
                bucket["updated_at"] = datetime.now().isoformat()

                # Only activate if auto_activate is True
                if auto_activate:
                    self._activate_context(default_context)

            # Save normalized context back to file if we normalized any images
            # This ensures file images are converted to base64 in persistence
            if self.persist_path and auto_activate and normalize_and_save and not self.read_only:
                self._save_to_file()
                logger.info("Context loaded and normalized, saved back to persistence")

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
                "🔧 Operation context:",
                f"Current task: {task_input}",
                "",
                "Execution history (JSON format):"
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
                "💡 Use this information about previous operations.",
                f"Task: {task_input}"
            ])
            
            return "\n".join(context_parts)

    def _build_conversation_context_human(self, task_input: str, depth: int) -> str:
        """Conversation context as a readable dialogue excerpt."""
        with self._lock:
            if not self._conversation_history:
                return task_input
            recent_messages = self._conversation_history[-depth:] if depth > 0 else self._conversation_history
            lines = ["Conversation context:", f"Current task: {task_input}", ""]
            for msg in recent_messages:
                role = {
                    "user": "User",
                    "assistant": "Assistant",
                    "system": "System"
                }.get(msg.role, msg.role)
                content = msg.content.strip()
                if len(content) > 2000:
                    content = content[:2000] + "…"
                lines.append(f"{role}: {content}")
            lines.append("")
            lines.append("Use this information to understand the task context.")
            return "\n".join(lines)

    def _build_full_context_human(self, task_input: str, include_tools: bool) -> str:
        """Full human-readable context: dialogue and recent tool results."""
        with self._lock:
            lines = ["FULL CONTEXT:", f"Current task: {task_input}", "", "Conversation history:"]
            if self._conversation_history:
                for msg in self._conversation_history:
                    role = {
                        "user": "User",
                        "assistant": "Assistant",
                        "system": "System"
                    }.get(msg.role, msg.role)
                    content = msg.content.strip()
                    if len(content) > 2000:
                        content = content[:2000] + "…"
                    lines.append(f"{role}: {content}")
            else:
                lines.append("(empty)")
            if include_tools and self._execution_history:
                lines.extend(["", "Recent operation results:"])
                for ex in self._execution_history[-10:]:
                    summary_output = (ex.output or "").strip()
                    if len(summary_output) > 2000:
                        summary_output = summary_output[:2000] + "…"
                    lines.append(f"Tool/Agent: {ex.agent_name}")
                    lines.append(f"Input: {ex.input_message}")
                    if summary_output:
                        lines.append(f"Output: {summary_output}")
                    if ex.error:
                        lines.append(f"Error: {ex.error}")
                    lines.append("")
            lines.append("ATTENTION: Use the information above to solve the task.")
            return "\n".join(lines)

    def _build_smart_context_human(self, task_input: str, depth: int, include_tools: bool) -> str:
        """Human-readable smart context selection."""
        task_lower = task_input.lower()
        conversation_keywords = [
            "continue", "next", "previous", "before", "already", "was",
            "what did you say", "answer", "reply to", "which", "this", "that",
            "read", "analyzed", "evaluated", "created", "edited"
        ]
        tool_keywords = [
            "edited", "read", "wrote", "weighs", "size", "bytes",
            "analyzed", "evaluated", "checked", "found", "created file"
        ]
        reference_keywords = [
            "which", "this", "that", "the same", "the very", "read", "analyzed",
            "created", "edited", "checked", "found", "that file",
            "this file", "the read file", "the analyzed file", "the created file"
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
                lines = ["Operation context:", f"Current task: {task_input}", ""]
                for ex in self._execution_history[-5:]:
                    summary_output = (ex.output or "").strip()
                    if len(summary_output) > 1200:
                        summary_output = summary_output[:1200] + "…"
                    lines.append(f"Tool/Agent: {ex.agent_name}")
                    if summary_output:
                        lines.append(f"Output: {summary_output}")
                    lines.append("")
                lines.append("Use this information about previous operations.")
                return "\n".join(lines)
        else:
            return task_input

    def get_context_for_agent_tool(
        self, 
        strategy: str = "smart", 
        depth: int = 5, 
        include_tools: bool = True, 
        task_input: str = ""
    ) -> str:
        """
        Get context for agent tool based on strategy.
        
        Args:
            strategy: Context strategy (minimal, conversation, smart)
            depth: Context depth for conversation strategy
            include_tools: Whether to include tool history
            task_input: Input task text
            
        Returns:
            Formatted context string
        """
        if strategy == "minimal":
            return task_input
        elif strategy == "conversation":
            return self._build_conversation_context_human(task_input, depth)
        elif strategy == "smart":
            return self._build_smart_context_human(task_input, depth, include_tools)
        else:
            # Default to smart strategy
            return self._build_smart_context_human(task_input, depth, include_tools)

    def add_tool_result_as_message(self, tool_name: str, output_text: str) -> None:
        """Record tool result into conversation as assistant message for follow-ups."""
        if not output_text:
            return
        # Tagged so chat views can tell a sub-agent report from an answer.
        self.add_message(
            "assistant",
            f"Tool result of {tool_name}: {output_text}",
            metadata={"kind": "tool_result", "tool": tool_name},
        )
