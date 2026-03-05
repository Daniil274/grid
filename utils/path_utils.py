"""
Path resolution utilities for agent tools.

All agent tools that accept file paths from the agent should use
resolve_agent_path() to convert them to absolute host paths.
"""

import os
from pathlib import Path
from typing import Any


def resolve_agent_path(file_path: str, factory: Any) -> str:
    """
    Resolve a file path supplied by an agent to an absolute host path.

    Agents always pass paths relative to their working directory:
    - Without container: working_directory = host absolute path (e.g. .../workspace/user_123/)
    - With container:    working_directory = same host path, but the agent sees it as /workspace/

    So both relative ("workspace/foo.md") and container-absolute ("/workspace/foo.md")
    paths must be resolved against factory.config.get_working_directory() on the host.

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

    # Container-absolute path: /workspace/... → working_dir/...
    if normalized.startswith("/workspace/"):
        rel = normalized[len("/workspace/"):]
        return str(Path(working_dir) / rel)

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
