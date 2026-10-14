"""Durable workflows: typed DAG specs, a transactional journal, an executable
engine, crash reconciliation, and the API/tool front-end.

The package is intentionally import-light: ``__init__`` exposes the stable
public names, while the heavy internals stay in their own modules
(``spec``, ``journal``, ``engine``, ``reconcile``, ``api``).
"""

from .api import (
    WorkflowScopeError,
    WorkflowService,
    build_invoker,
    default_checks,
    get_service,
    register_check,
)

__all__ = [
    "WorkflowScopeError",
    "WorkflowService",
    "build_invoker",
    "default_checks",
    "get_service",
    "register_check",
]
