"""
Emergency control tools for pipeline management.

Provides tools for agents to trigger emergency shutdown of pipelines
and query pipeline status.
"""

import json
import logging
from typing import Any, Optional

from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)


def _get_factory_from_context(context: RunContextWrapper) -> Any:
    """
    Extract AgentFactory from context.

    Args:
        context: The run context wrapper

    Returns:
        AgentFactory instance or None
    """
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        return getattr(raw, "factory", None)
    except Exception as e:
        logger.error(f"Failed to get factory from context: {e}")
        return None


@function_tool
async def emergency_shutdown(
    context: RunContextWrapper,
    reason: str,
    severity: str = "critical"
) -> str:
    """
    ЭКСТРЕННАЯ ОСТАНОВКА всего текущего pipeline.

    Останавливает все запущенные агенты в текущем pipeline и удаляет задачи из очереди.
    Этот инструмент следует использовать только при критических ошибках, которые делают
    невозможным продолжение выполнения всего pipeline.

    Args:
        reason: Детальное описание причины остановки. Должно быть максимально конкретным,
                чтобы оркестратор мог понять проблему и принять решение о перезапуске.
                Примеры:
                - "Database connection failed after 3 retries: ConnectionRefusedError"
                - "Required dependency 'libpq' not found, cannot proceed with PostgreSQL operations"
                - "Infinite recursion detected: spawned >20 concurrent tasks"

        severity: Уровень критичности проблемы:
                - "warning": Проблема может быть решена, но требует внимания
                - "error": Серьезная ошибка, но система может продолжить работу
                - "critical" (default): Критическая ошибка, система не может продолжить работу

    Returns:
        JSON строка с результатом остановки, включающая:
        - success: true/false
        - pipeline_id: ID остановленного pipeline
        - reason: причина остановки
        - severity: уровень критичности
        - cancelled_tasks: количество отмененных задач
        - completed_tasks: количество завершенных задач до остановки
        - failed_tasks: количество проваленных задач
        - total_tasks: общее количество задач в pipeline

    Example:
        ```python
        # Агент обнаруживает критическую проблему
        try:
            connection = connect_to_database()
        except ConnectionError as e:
            # Останавливаем весь pipeline, т.к. все последующие задачи требуют БД
            result = emergency_shutdown(
                reason=f"Database unavailable: {e}. All subsequent tasks require DB access.",
                severity="critical"
            )
            return result
        ```
    """
    # Import inside function to avoid circular import
    from core.pipeline_registry import PipelineRegistry

    # Validate severity
    valid_severities = {"warning", "error", "critical"}
    if severity not in valid_severities:
        logger.warning(f"Invalid severity '{severity}', using 'critical'")
        severity = "critical"

    # Get factory and context_id
    factory = _get_factory_from_context(context)
    if factory is None:
        error_msg = "❌ emergency_shutdown: Cannot access AgentFactory from context"
        logger.error(error_msg)
        return json.dumps({
            "success": False,
            "error": error_msg
        }, ensure_ascii=False, indent=2)

    try:
        raw_ctx = getattr(context, "context", None)
        context_id = getattr(raw_ctx, "context_id", None) or factory.get_active_context_id()
        direct_pipeline_id = getattr(raw_ctx, "pipeline_id", None)
        if not context_id:
            error_msg = "❌ emergency_shutdown: No active context ID"
            logger.error(error_msg)
            return json.dumps({
                "success": False,
                "error": error_msg
            }, ensure_ascii=False, indent=2)

        # Get pipeline registry
        registry = PipelineRegistry()

        pipeline_id = None
        if direct_pipeline_id:
            status = await registry.get_pipeline_status(direct_pipeline_id)
            if "error" not in status:
                pipeline_id = direct_pipeline_id

        if pipeline_id is None:
            pipeline = await registry.get_pipeline_by_context(context_id)
            if pipeline is not None:
                pipeline_id = pipeline.pipeline_id

        if pipeline_id is None:
            error_msg = f"❌ emergency_shutdown: No active pipeline found for context {context_id}"
            logger.warning(error_msg)
            return json.dumps({
                "success": False,
                "error": error_msg,
                "context_id": context_id
            }, ensure_ascii=False, indent=2)

        logger.critical(
            f"🚨 EMERGENCY SHUTDOWN TRIGGERED 🚨\n"
            f"Pipeline: {pipeline_id}\n"
            f"Context: {context_id}\n"
            f"Reason: {reason}\n"
            f"Severity: {severity}"
        )

        # Execute emergency shutdown
        result = await registry.emergency_shutdown(
            pipeline_id=pipeline_id,
            reason=reason,
            severity=severity
        )

        # Format response
        response = {
            "success": result.get("success", False),
            "pipeline_id": result.get("pipeline_id"),
            "reason": reason,
            "severity": severity,
            "cancelled_tasks": result.get("cancelled_tasks", 0),
            "completed_tasks": result.get("completed_tasks", 0),
            "failed_tasks": result.get("failed_tasks", 0),
            "total_tasks": result.get("total_tasks", 0),
            "message": "🚨 Emergency shutdown executed. All tasks in this pipeline have been cancelled."
        }

        return json.dumps(response, ensure_ascii=False, indent=2)

    except Exception as e:
        error_msg = f"❌ Error during emergency shutdown: {e}"
        logger.error(error_msg, exc_info=True)
        return json.dumps({
            "success": False,
            "error": error_msg
        }, ensure_ascii=False, indent=2)


@function_tool
async def get_pipeline_status(
    context: RunContextWrapper
) -> str:
    """
    Получить детальный статус текущего pipeline.

    Возвращает информацию о текущем pipeline, включая список запущенных задач,
    завершенных задач, проваленных задач и общий статус.

    Args:
        context: Контекст выполнения (автоматически передается)

    Returns:
        JSON строка со статусом pipeline:
        - pipeline_id: ID pipeline
        - status: текущий статус (running/completed/failed/emergency_stopped/cancelling)
        - orchestrator_name: имя оркестратора
        - context_id: ID контекста
        - created_at: время создания (ISO format)
        - running_tasks: список запущенных задач с их деталями
        - completed_tasks: список ID завершенных задач
        - failed_tasks: словарь {task_id: error_message}
        - all_tasks: общее количество задач
        - emergency_reason: причина emergency shutdown (если был)
        - emergency_severity: уровень критичности (если был)
        - shutdown_requested_at: время запроса остановки (если был)

    Example:
        ```python
        # Агент проверяет текущий статус pipeline перед началом работы
        status_json = get_pipeline_status()
        status = json.loads(status_json)

        if status["status"] == "emergency_stopped":
            return "Pipeline already stopped, aborting task"

        if len(status["running_tasks"]) > 10:
            emergency_shutdown(
                reason="Too many concurrent tasks detected",
                severity="warning"
            )
        ```
    """
    # Import inside function to avoid circular import
    from core.pipeline_registry import PipelineRegistry

    # Get factory and context_id
    factory = _get_factory_from_context(context)
    if factory is None:
        error_msg = "❌ get_pipeline_status: Cannot access AgentFactory from context"
        logger.error(error_msg)
        return json.dumps({
            "error": error_msg
        }, ensure_ascii=False, indent=2)

    try:
        raw_ctx = getattr(context, "context", None)
        context_id = getattr(raw_ctx, "context_id", None) or factory.get_active_context_id()
        direct_pipeline_id = getattr(raw_ctx, "pipeline_id", None)
        if not context_id:
            error_msg = "❌ get_pipeline_status: No active context ID"
            logger.warning(error_msg)
            return json.dumps({
                "error": error_msg
            }, ensure_ascii=False, indent=2)

        # Get pipeline registry
        registry = PipelineRegistry()

        pipeline_id = None
        if direct_pipeline_id:
            status = await registry.get_pipeline_status(direct_pipeline_id)
            if "error" not in status:
                pipeline_id = direct_pipeline_id

        if pipeline_id is None:
            pipeline = await registry.get_pipeline_by_context(context_id)
            if pipeline is not None:
                pipeline_id = pipeline.pipeline_id

        if pipeline_id is None:
            return json.dumps({
                "error": f"No active pipeline found for context {context_id}",
                "context_id": context_id
            }, ensure_ascii=False, indent=2)

        # Get detailed status
        status = await registry.get_pipeline_status(pipeline_id)

        return json.dumps(status, ensure_ascii=False, indent=2)

    except Exception as e:
        error_msg = f"❌ Error getting pipeline status: {e}"
        logger.error(error_msg, exc_info=True)
        return json.dumps({
            "error": error_msg
        }, ensure_ascii=False, indent=2)


# Registry for this module
EMERGENCY_TOOLS = {
    "emergency_shutdown": emergency_shutdown,
    "get_pipeline_status": get_pipeline_status,
}
