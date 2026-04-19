"""
Pipeline Registry for tracking dynamic agent orchestration pipelines.

Provides centralized tracking of all active pipelines and their tasks,
with emergency shutdown capabilities.
"""

import asyncio
import contextvars
import uuid
import logging
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Set, Optional, Any, List, Awaitable, Callable

logger = logging.getLogger(__name__)

_pipeline_lock_tokens: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "_pipeline_lock_tokens", default={}
)
_pipeline_step_ids: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "_pipeline_step_ids", default={}
)


class PipelineStatus(str, Enum):
    """Status of a pipeline execution."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EMERGENCY_STOPPED = "emergency_stopped"
    CANCELLING = "cancelling"


@dataclass
class AgentTaskInfo:
    """Information about a running agent task."""
    task_id: str
    agent_name: str
    task: asyncio.Task  # Reference to running asyncio Task
    started_at: datetime
    parent_task_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    completed_at: Optional[datetime] = None
    success: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class PipelineInfo:
    """Information about a pipeline execution."""
    pipeline_id: str
    created_at: datetime
    status: PipelineStatus
    orchestrator_name: str
    context_id: str
    user_id: Optional[str] = None

    tasks: Dict[str, AgentTaskInfo] = field(default_factory=dict)
    task_dependencies: Dict[str, Set[str]] = field(default_factory=dict)

    completed_tasks: Set[str] = field(default_factory=set)
    failed_tasks: Dict[str, str] = field(default_factory=dict)  # task_id -> error

    emergency_reason: Optional[str] = None
    emergency_severity: Optional[str] = None
    shutdown_requested_at: Optional[datetime] = None
    shutdown_completed_at: Optional[datetime] = None
    serial_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    serial_owner_token: Optional[str] = None
    serial_depth: int = 0


class PipelineRegistry:
    """
    Singleton registry for managing all active pipelines.

    Tracks running agents, handles emergency shutdowns, and provides
    pipeline status information.
    """

    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._pipelines: Dict[str, PipelineInfo] = {}
        self._context_to_pipeline: Dict[str, str] = {}  # context_id -> pipeline_id
        self._memory_store = None  # Will be set by AgentFactory
        self._initialized = True
        logger.info("PipelineRegistry initialized")

    async def register_pipeline(
        self,
        orchestrator_name: str,
        context_id: str,
        user_id: Optional[str] = None
    ) -> str:
        """
        Register a new pipeline.

        Args:
            orchestrator_name: Name of the orchestrator creating the pipeline
            context_id: Context ID for this pipeline
            user_id: Optional user ID

        Returns:
            Unique pipeline ID
        """
        async with self._lock:
            pipeline_id = f"pipeline-{uuid.uuid4().hex[:8]}"

            pipeline = PipelineInfo(
                pipeline_id=pipeline_id,
                created_at=datetime.now(),
                status=PipelineStatus.RUNNING,
                orchestrator_name=orchestrator_name,
                context_id=context_id,
                user_id=user_id
            )

            self._pipelines[pipeline_id] = pipeline
            self._context_to_pipeline[context_id] = pipeline_id

            logger.info(
                f"Registered pipeline: {pipeline_id} "
                f"(orchestrator={orchestrator_name}, context={context_id})"
            )

            return pipeline_id

    async def get_or_create_pipeline(
        self,
        orchestrator_name: str,
        context_id: str,
        user_id: Optional[str] = None,
    ) -> str:
        """
        Return an active pipeline for the context or create a new one.

        This lets nested orchestrators and agent-tools share a single serial runtime.
        """
        async with self._lock:
            pipeline_id = self._context_to_pipeline.get(context_id)
            if pipeline_id:
                pipeline = self._pipelines.get(pipeline_id)
                if pipeline and pipeline.status in (
                    PipelineStatus.RUNNING,
                    PipelineStatus.CANCELLING,
                ):
                    return pipeline_id

        return await self.register_pipeline(
            orchestrator_name=orchestrator_name,
            context_id=context_id,
            user_id=user_id,
        )

    async def register_task(
        self,
        pipeline_id: str,
        agent_name: str,
        task: asyncio.Task,
        parent_task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """
        Register a task in a pipeline.

        Args:
            pipeline_id: Pipeline ID
            agent_name: Name of the agent running the task
            task: The asyncio Task object
            parent_task_id: Optional parent task ID for dependencies
            metadata: Optional metadata about the task

        Returns:
            Unique task ID
        """
        async with self._lock:
            if pipeline_id not in self._pipelines:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"

            task_info = AgentTaskInfo(
                task_id=task_id,
                agent_name=agent_name,
                task=task,
                started_at=datetime.now(),
                parent_task_id=parent_task_id,
                metadata=metadata or {}
            )

            pipeline = self._pipelines[pipeline_id]
            pipeline.tasks[task_id] = task_info

            # Track dependencies
            if parent_task_id:
                if parent_task_id not in pipeline.task_dependencies:
                    pipeline.task_dependencies[parent_task_id] = set()
                pipeline.task_dependencies[parent_task_id].add(task_id)

            logger.debug(
                f"Registered task: {task_id} in pipeline {pipeline_id} "
                f"(agent={agent_name})"
            )

            return task_id

    def get_current_step_id(self, pipeline_id: str) -> Optional[str]:
        """Return the currently executing step ID for the pipeline in this task context."""
        return _pipeline_step_ids.get().get(pipeline_id)

    def _set_context_map_value(
        self,
        var: contextvars.ContextVar[Dict[str, str]],
        pipeline_id: str,
        value: Optional[str],
    ) -> contextvars.Token:
        current = dict(var.get())
        if value is None:
            current.pop(pipeline_id, None)
        else:
            current[pipeline_id] = value
        return var.set(current)

    async def _enter_serial_scope(self, pipeline_id: str) -> Optional[contextvars.Token]:
        async with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                raise ValueError(f"Pipeline {pipeline_id} not found")

            current_owner = _pipeline_lock_tokens.get().get(pipeline_id)
            if current_owner and current_owner == pipeline.serial_owner_token:
                pipeline.serial_depth += 1
                return None

            serial_lock = pipeline.serial_lock

        await serial_lock.acquire()

        owner_token = uuid.uuid4().hex
        token = self._set_context_map_value(_pipeline_lock_tokens, pipeline_id, owner_token)

        async with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                _pipeline_lock_tokens.reset(token)
                serial_lock.release()
                raise ValueError(f"Pipeline {pipeline_id} not found")

            pipeline.serial_owner_token = owner_token
            pipeline.serial_depth = 1

        return token

    async def _exit_serial_scope(self, pipeline_id: str, token: Optional[contextvars.Token]) -> None:
        should_release = False

        async with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                return

            current_owner = _pipeline_lock_tokens.get().get(pipeline_id)
            if not current_owner or current_owner != pipeline.serial_owner_token:
                return

            pipeline.serial_depth = max(0, pipeline.serial_depth - 1)
            if pipeline.serial_depth == 0:
                pipeline.serial_owner_token = None
                should_release = True

        if token is not None:
            _pipeline_lock_tokens.reset(token)

        if should_release:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline and pipeline.serial_lock.locked():
                pipeline.serial_lock.release()

    async def run_serialized_step(
        self,
        *,
        pipeline_id: str,
        agent_name: str,
        step_coro_factory: Callable[[], Awaitable[Any]],
        parent_task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> Any:
        """
        Run a step under the per-pipeline serial runtime.

        Nested calls within the same pipeline are re-entrant, which preserves depth-first
        execution of precompiled agent/tool subtrees.
        """
        serial_token = await self._enter_serial_scope(pipeline_id)
        async with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                await self._exit_serial_scope(pipeline_id, serial_token)
                raise ValueError(f"Pipeline {pipeline_id} not found")
            if pipeline.status not in (
                PipelineStatus.EMERGENCY_STOPPED,
                PipelineStatus.CANCELLING,
            ):
                pipeline.status = PipelineStatus.RUNNING
        step_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        parent_id = parent_task_id or self.get_current_step_id(pipeline_id)
        step_token = self._set_context_map_value(_pipeline_step_ids, pipeline_id, step_id)
        task: Optional[asyncio.Task] = None

        try:
            task = asyncio.create_task(step_coro_factory())
            await self.register_task(
                pipeline_id=pipeline_id,
                agent_name=agent_name,
                task=task,
                parent_task_id=parent_id,
                metadata=metadata,
                task_id=step_id,
            )
            result = await task
            await self.mark_task_completed(pipeline_id, step_id, success=True)
            return result
        except asyncio.CancelledError:
            await self.mark_task_completed(pipeline_id, step_id, success=False, error="Cancelled")
            raise
        except Exception as exc:
            await self.mark_task_completed(pipeline_id, step_id, success=False, error=str(exc))
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            _pipeline_step_ids.reset(step_token)
            await self._exit_serial_scope(pipeline_id, serial_token)

    async def get_pipeline_by_context(self, context_id: str) -> Optional[PipelineInfo]:
        """
        Get pipeline info by context ID.

        Args:
            context_id: Context ID to look up

        Returns:
            PipelineInfo if found, None otherwise
        """
        async with self._lock:
            pipeline_id = self._context_to_pipeline.get(context_id)
            if pipeline_id:
                return self._pipelines.get(pipeline_id)
            return None

    async def get_running_tasks(self, pipeline_id: str) -> List[AgentTaskInfo]:
        """
        Get all running tasks in a pipeline.

        Args:
            pipeline_id: Pipeline ID

        Returns:
            List of running AgentTaskInfo objects
        """
        async with self._lock:
            if pipeline_id not in self._pipelines:
                return []

            pipeline = self._pipelines[pipeline_id]
            return [
                task_info for task_info in pipeline.tasks.values()
                if not task_info.task.done()
            ]

    async def emergency_shutdown(
        self,
        pipeline_id: str,
        reason: str,
        severity: str = "critical"
    ) -> Dict[str, Any]:
        """
        Execute emergency shutdown of a pipeline.

        Cancels all running tasks and updates pipeline status.

        Args:
            pipeline_id: Pipeline ID to shut down
            reason: Detailed reason for shutdown
            severity: Severity level (warning, error, critical)

        Returns:
            Dict with shutdown results
        """
        async with self._lock:
            if pipeline_id not in self._pipelines:
                return {
                    "success": False,
                    "error": f"Pipeline {pipeline_id} not found"
                }

            pipeline = self._pipelines[pipeline_id]

            if pipeline.status == PipelineStatus.EMERGENCY_STOPPED:
                return {
                    "success": False,
                    "error": "Pipeline already stopped",
                    "pipeline_id": pipeline_id
                }

            logger.warning(
                f"[!] EMERGENCY SHUTDOWN: Pipeline {pipeline_id} | "
                f"Reason: {reason} | Severity: {severity}"
            )

            # Update pipeline status
            pipeline.status = PipelineStatus.CANCELLING
            pipeline.emergency_reason = reason
            pipeline.emergency_severity = severity
            pipeline.shutdown_requested_at = datetime.now()

            # Get all running tasks
            running_tasks = [
                task_info for task_info in pipeline.tasks.values()
                if not task_info.task.done()
            ]

            logger.info(
                f"Cancelling {len(running_tasks)} running tasks in pipeline {pipeline_id}"
            )

            # Cancel all tasks
            for task_info in running_tasks:
                try:
                    task_info.task.cancel()
                    logger.debug(f"Cancelled task {task_info.task_id} ({task_info.agent_name})")
                except Exception as e:
                    logger.error(f"Error cancelling task {task_info.task_id}: {e}")

        # Wait for tasks to complete (outside lock)
        if running_tasks:
            try:
                await asyncio.wait(
                    [task_info.task for task_info in running_tasks],
                    timeout=5.0,
                    return_when=asyncio.ALL_COMPLETED
                )
                logger.info(f"All tasks in pipeline {pipeline_id} completed gracefully")
            except asyncio.TimeoutError:
                logger.warning(
                    f"Timeout waiting for tasks in pipeline {pipeline_id} to complete"
                )

        # Final status update
        async with self._lock:
            pipeline.status = PipelineStatus.EMERGENCY_STOPPED
            pipeline.shutdown_completed_at = datetime.now()

            # Save to memory store if available
            if self._memory_store:
                try:
                    await self._save_emergency_event(pipeline)
                except Exception as e:
                    logger.error(f"Failed to save emergency event: {e}")

            result = {
                "success": True,
                "pipeline_id": pipeline_id,
                "reason": reason,
                "severity": severity,
                "cancelled_tasks": len(running_tasks),
                "completed_tasks": len(pipeline.completed_tasks),
                "failed_tasks": len(pipeline.failed_tasks),
                "total_tasks": len(pipeline.tasks)
            }

            logger.info(f"Emergency shutdown completed: {result}")
            return result

    async def mark_task_completed(
        self,
        pipeline_id: str,
        task_id: str,
        success: bool,
        error: Optional[str] = None
    ) -> None:
        """
        Mark a task as completed.

        Args:
            pipeline_id: Pipeline ID
            task_id: Task ID
            success: Whether task completed successfully
            error: Optional error message if failed
        """
        async with self._lock:
            if pipeline_id not in self._pipelines:
                logger.warning(f"Pipeline {pipeline_id} not found when marking task completed")
                return

            pipeline = self._pipelines[pipeline_id]

            if task_id not in pipeline.tasks:
                logger.warning(f"Task {task_id} not found in pipeline {pipeline_id}")
                return

            task_info = pipeline.tasks[task_id]
            task_info.completed_at = datetime.now()
            task_info.success = success
            task_info.error = error

            if success:
                pipeline.completed_tasks.add(task_id)
                logger.debug(f"Task {task_id} completed successfully")
            else:
                pipeline.failed_tasks[task_id] = error or "Unknown error"
                logger.warning(f"Task {task_id} failed: {error}")

            if pipeline.status == PipelineStatus.RUNNING:
                has_running = any(not info.task.done() for info in pipeline.tasks.values())
                if not has_running:
                    if pipeline.failed_tasks:
                        pipeline.status = PipelineStatus.FAILED
                    else:
                        pipeline.status = PipelineStatus.COMPLETED

    async def get_pipeline_status(self, pipeline_id: str) -> Dict[str, Any]:
        """
        Get detailed status of a pipeline.

        Args:
            pipeline_id: Pipeline ID

        Returns:
            Dict with pipeline status information
        """
        async with self._lock:
            if pipeline_id not in self._pipelines:
                return {
                    "error": f"Pipeline {pipeline_id} not found"
                }

            pipeline = self._pipelines[pipeline_id]

            running_tasks = [
                {
                    "task_id": t.task_id,
                    "agent_name": t.agent_name,
                    "started_at": t.started_at.isoformat(),
                }
                for t in pipeline.tasks.values()
                if not t.task.done()
            ]

            return {
                "pipeline_id": pipeline_id,
                "status": pipeline.status.value,
                "orchestrator_name": pipeline.orchestrator_name,
                "context_id": pipeline.context_id,
                "created_at": pipeline.created_at.isoformat(),
                "running_tasks": running_tasks,
                "completed_tasks": list(pipeline.completed_tasks),
                "failed_tasks": dict(pipeline.failed_tasks),
                "all_tasks": len(pipeline.tasks),
                "emergency_reason": pipeline.emergency_reason,
                "emergency_severity": pipeline.emergency_severity,
                "shutdown_requested_at": (
                    pipeline.shutdown_requested_at.isoformat()
                    if pipeline.shutdown_requested_at else None
                )
            }

    async def _save_emergency_event(self, pipeline: PipelineInfo) -> None:
        """Save emergency shutdown event to memory store."""
        if not self._memory_store:
            return

        try:
            event_data = {
                "pipeline_id": pipeline.pipeline_id,
                "orchestrator": pipeline.orchestrator_name,
                "reason": pipeline.emergency_reason,
                "severity": pipeline.emergency_severity,
                "timestamp": pipeline.shutdown_requested_at.isoformat() if pipeline.shutdown_requested_at else None,
                "completed_tasks": len(pipeline.completed_tasks),
                "failed_tasks": len(pipeline.failed_tasks),
                "total_tasks": len(pipeline.tasks)
            }

            await self._memory_store.save(
                memory_type="task",
                content=f"Emergency shutdown: {pipeline.emergency_reason}",
                metadata=event_data,
                task_id=pipeline.pipeline_id,
                status="abandoned"
            )

            logger.debug(f"Saved emergency event for pipeline {pipeline.pipeline_id}")
        except Exception as e:
            logger.error(f"Error saving emergency event: {e}", exc_info=True)

    async def cleanup_completed_pipelines(self, max_age_minutes: int = 60) -> int:
        """
        Clean up old completed pipelines.

        Args:
            max_age_minutes: Remove pipelines older than this many minutes

        Returns:
            Number of pipelines removed
        """
        async with self._lock:
            now = datetime.now()
            to_remove = []

            for pipeline_id, pipeline in self._pipelines.items():
                if pipeline.status in (PipelineStatus.COMPLETED, PipelineStatus.FAILED, PipelineStatus.EMERGENCY_STOPPED):
                    age_minutes = (now - pipeline.created_at).total_seconds() / 60
                    if age_minutes > max_age_minutes:
                        to_remove.append(pipeline_id)

            for pipeline_id in to_remove:
                pipeline = self._pipelines[pipeline_id]
                del self._pipelines[pipeline_id]
                if pipeline.context_id in self._context_to_pipeline:
                    del self._context_to_pipeline[pipeline.context_id]

            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} old pipelines")

            return len(to_remove)
