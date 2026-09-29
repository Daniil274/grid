"""
Path resolution utilities for agent tools.

All agent tools that accept file paths from the agent should use
resolve_agent_path() or resolve_agent_path_auto() to convert them to absolute host paths.
"""

import contextvars
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any

# Current AgentFactory in the execution context (set by runner when starting agent).
_current_factory: contextvars.ContextVar[Any] = contextvars.ContextVar("current_agent_factory", default=None)


@contextmanager
def factory_path_context(factory: Any):
    """Bind path resolution for one invocation, restoring the caller's context."""
    token = _current_factory.set(factory)
    try:
        yield
    finally:
        _current_factory.reset(token)


def set_current_factory(factory: Any) -> None:
    """Set the factory for path resolution in tools (called from runner)."""
    _current_factory.set(factory)


def reset_current_factory() -> None:
    """Reset the factory (called after agent startup completes)."""
    try:
        _current_factory.set(None)
    except LookupError:
        pass


def get_current_factory() -> Any:
    """Get the current factory from the context (for tools)."""
    return _current_factory.get(None)


def _sandbox_root(factory: Any) -> Path:
    """The directory an agent's paths are confined to.

    The factory's working directory; outside an agent run (no factory bound),
    the process working directory. There is always a root: a path is never
    resolved without one.
    """
    if factory is None:
        return Path.cwd().resolve()
    return Path(factory.config.get_working_directory()).resolve()


#: Where a run's container mounts its workspace (core.managers.container_manager).
#: Commands there run in it, so ``pwd`` shows paths under it: an agent passes
#: them on to the file tools.
CONTAINER_WORKDIR = "/workspace"


def _is_within(base: Path, candidate: Path) -> bool:
    """Check that candidate is base or a child of base."""
    return candidate == base or base in candidate.parents


def _resolve_inside_working_dir(file_path: str, working_dir: Path, container_id: Any) -> Path:
    """
    Resolve an agent-supplied path inside the working directory.

    Agents must never access anything above the configured working directory.
    Absolute paths are interpreted as agent-root paths, except when they already
    point inside the working directory (a leaked host path) or, in a run with a
    container, under CONTAINER_WORKDIR - the path its shell shows.
    """
    normalized = (file_path or ".").replace("\\", "/")
    if normalized in ("", ".", "/"):
        return working_dir

    # Accept leaked absolute host paths only if they still point inside working_dir.
    if os.path.isabs(file_path):
        host_candidate = Path(file_path).resolve()
        if _is_within(working_dir, host_candidate):
            return host_candidate

        if container_id and (
            normalized == CONTAINER_WORKDIR or normalized.startswith(CONTAINER_WORKDIR + "/")
        ):
            normalized = normalized[len(CONTAINER_WORKDIR):] or "/"
        relative_part = normalized.lstrip("/")
        candidate = (working_dir / relative_part).resolve()
    else:
        candidate = (working_dir / file_path).resolve()

    if not _is_within(working_dir, candidate):
        raise ValueError("Path escapes working directory")

    return candidate


def resolve_agent_path(file_path: str, factory: Any) -> str:
    """
    Resolve a file path supplied by an agent to an absolute host path.

    Agents pass paths relative to their working directory:
    - Without container: working_directory = absolute host path; agents use it directly.
    - With container:    agent sees its working directory as "/" (root).
                        All absolute paths are resolved relative to the host working_directory.

    Args:
        file_path: Path as supplied by the agent.
        factory:   AgentFactory instance, or None outside an agent run.

    Returns:
        Absolute host path string.
    """
    working_dir = _sandbox_root(factory)
    container_id = getattr(factory, "container_id", None)
    return str(_resolve_inside_working_dir(file_path, working_dir, container_id))


def resolve_agent_path_from_ctx(file_path: str, ctx: Any) -> str:
    """
    Convenience wrapper: extract factory from RunContextWrapper and resolve path.
    """
    factory = getattr(getattr(ctx, "context", None), "factory", None)
    return resolve_agent_path(file_path, factory)


def resolve_agent_path_auto(file_path: str) -> str:
    """
    Resolve path using the current factory from context (set by runner).
    Use in tools that must not add a context parameter to their schema.
    """
    return resolve_agent_path(file_path, get_current_factory())


def display_agent_path(file_path: str, factory: Any) -> str:
    """
    Convert a host or agent path into a safe, agent-visible workspace-relative path.

    Never reveals directories above the working directory.
    """
    working_dir = _sandbox_root(factory)
    try:
        resolved = _resolve_inside_working_dir(file_path, working_dir, getattr(factory, "container_id", None))
    except Exception:
        return "."

    rel = os.path.relpath(resolved, working_dir)
    if rel == ".":
        return "."
    return rel.replace("\\", "/")


def display_agent_path_from_ctx(file_path: str, ctx: Any) -> str:
    """Context-based wrapper for safe agent-visible paths."""
    factory = getattr(getattr(ctx, "context", None), "factory", None)
    return display_agent_path(file_path, factory)


def display_agent_path_auto(file_path: str) -> str:
    """Current-context wrapper for safe agent-visible paths."""
    return display_agent_path(file_path, get_current_factory())


def sanitize_text_for_agent(text: str, factory: Any) -> str:
    """
    Best-effort scrub absolute workspace paths from human-facing tool output.
    """
    if not text:
        return text

    working_dir = _sandbox_root(factory)

    sanitized = str(text)
    variants = {
        str(working_dir),
        working_dir.as_posix(),
        working_dir.as_posix().rstrip("/"),
    }
    if os.name == "nt":
        variants.add(str(working_dir).replace("\\", "/"))
        variants.add(str(working_dir).replace("/", "\\"))

    for variant in sorted((v for v in variants if v), key=len, reverse=True):
        sanitized = sanitized.replace(variant + "/", "./")
        sanitized = sanitized.replace(variant, ".")

    return sanitized


def sanitize_text_for_agent_from_ctx(text: str, ctx: Any) -> str:
    """Context-based wrapper for output sanitization."""
    factory = getattr(getattr(ctx, "context", None), "factory", None)
    return sanitize_text_for_agent(text, factory)
