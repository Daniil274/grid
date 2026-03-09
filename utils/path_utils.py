"""
Path resolution utilities for agent tools.

All agent tools that accept file paths from the agent should use
resolve_agent_path() or resolve_agent_path_auto() to convert them to absolute host paths.
"""

import contextvars
import os
from pathlib import Path
from typing import Any

# Текущая AgentFactory в контексте выполнения (устанавливается раннером при запуске агента).
_current_factory: contextvars.ContextVar[Any] = contextvars.ContextVar("current_agent_factory", default=None)


def set_current_factory(factory: Any) -> None:
    """Установить фабрику для резолва путей в инструментах (вызывается из раннера)."""
    _current_factory.set(factory)


def reset_current_factory() -> None:
    """Сбросить фабрику (вызывается после завершения запуска агента)."""
    try:
        _current_factory.set(None)
    except LookupError:
        pass


def get_current_factory() -> Any:
    """Получить текущую фабрику из контекста (для инструментов)."""
    return _current_factory.get(None)


def resolve_agent_path(file_path: str, factory: Any) -> str:
    """
    Resolve a file path supplied by an agent to an absolute host path.

    Agents pass paths relative to their working directory:
    - Without container: working_directory = absolute host path; agents use it directly.
    - With container:    agent sees its working directory as "/" (root).
                        All absolute paths are resolved relative to the host working_directory.

    Args:
        file_path: Path as supplied by the agent.
        factory:   AgentFactory instance (or None).

    Returns:
        Absolute host path string.
    """
    if factory is None:
        return file_path

    working_dir = factory.config.get_working_directory()
    normalized = file_path.replace("\\", "/")
    container_id = getattr(factory, "container_id", None)

    if container_id and normalized.startswith("/"):
        # Container mounts workspace at /workspace; agent paths start with /workspace/...
        # Strip the mount prefix (/workspace) and resolve the rest against host working_dir.
        # E.g. "/workspace/sub/file.txt" → "sub/file.txt" → working_dir/sub/file.txt
        parts = Path(normalized).parts  # ('/', 'workspace', 'sub', 'file.txt')
        if len(parts) > 2:
            rel = str(Path(*parts[2:]))
            return str(Path(working_dir) / rel)
        # Bare mount root ("/workspace") or bare "/" → return working_dir itself
        return working_dir

    # Already an absolute host path — return as-is
    if os.path.isabs(file_path):
        return file_path

    # Relative path → working_dir / file_path
    return str(Path(working_dir) / file_path)


def resolve_agent_path_from_ctx(file_path: str, ctx: Any) -> str:
    """
    Convenience wrapper: extract factory from RunContextWrapper and resolve path.
    """
    factory = None
    try:
        raw = getattr(ctx, "context", None)
        if raw is not None:
            factory = getattr(raw, "factory", None)
    except Exception:
        pass
    return resolve_agent_path(file_path, factory)


def resolve_agent_path_auto(file_path: str) -> str:
    """
    Resolve path using the current factory from context (set by runner).
    Use in tools that must not add a context parameter to their schema.
    """
    return resolve_agent_path(file_path, get_current_factory())
