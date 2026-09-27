"""What a run carries: its context object for tools, its progress, and small helpers.

``GridRunContext`` is the object the Agents SDK hands to every tool of a run
(``context.context``): the live factory, the conversation, the user, the
container, the policy state and the run's stop control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Union,
)

import agents

from core.config.config import Config
from core.interruption import CallLedger, RunControl, StopReason
from schemas import CompactConfig
from schemas.schemas import ImageContent, ImageUrl, TextContent

if TYPE_CHECKING:
    from core.agent_factory import AgentFactory


@dataclass
class _RunProgress:
    """Where a turn stands: its pending-run record and what its runs did so far."""

    input_preview: str
    attempt: int = 0
    recorded_failure: bool = False
    #: Tool calls of the turn, fed from the stream (see CallLedger).
    ledger: CallLedger = field(default_factory=CallLedger)
    #: The SDK result of the latest attempt, as soon as it exists.
    result: Any = None

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged

def layered_compact(catalog: Optional[Config], own: Config) -> CompactConfig:
    """The ``compact`` settings a system runs with.

    Routed from a catalog (routing.yaml) that has a ``compact`` section, that
    section is the base for every system and the system's own section overrides
    it field by field - thresholds in one place, compactable tools per system.
    Otherwise the system's own section, or the defaults.
    """
    own_config = own.config
    if catalog is None or "compact" not in catalog.config.model_fields_set:
        return own_config.compact
    base = catalog.config.compact.model_dump(exclude_unset=True)
    override = own_config.compact.model_dump(exclude_unset=True) if "compact" in own_config.model_fields_set else {}
    return CompactConfig(**_deep_merge(base, override))

def with_images(text: str, images: List[str]) -> Union[str, List[Any]]:
    """Message content: *text*, plus the images generated with it, if any."""
    if not images:
        return text
    return [
        TextContent(type="text", text=text),
        *(ImageContent(type="image_url", image_url=ImageUrl(url=url)) for url in images),
    ]

class _TurnStopped(Exception):
    """A run that ended before its answer without failing: timeout, turn limit,
    a model-side error that retrying would repeat, or the user's graceful Stop.
    The turn records it as an interruption and answers with its summary."""

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail

# Helper class to mock the SDK's ToolContext for auto-run tools
class AutoRunToolContext:
    """Mock context that mimics SDK's ToolContext for direct tool invocation."""

    def __init__(
        self, context: Any, tool_name: str = "", operator_configured: bool = False
    ):
        self.context = context
        self.tool_name = tool_name
        # True only for calls taken verbatim from host configuration
        # (auto_run_tools); the action policy then runs them unjudged.
        self.operator_configured = operator_configured

@dataclass
class GridRunContext:
    """
    Runtime context object passed into Agents SDK Runner.

    It enables `function_tool` implementations to access the live AgentFactory instance
    via `context.context.factory`.

    Also provides access to the current agent's session for local context injection.

    user_id provides workspace isolation for tools that need per-user storage.
    """

    factory: "AgentFactory"  # core.agent_factory; typed by name to avoid the import cycle
    context_id: Optional[str] = None
    session: Optional[Any] = None  # SQLiteSession for local agent history
    user_id: Optional[str] = None  # User identifier for workspace isolation
    agent_id: Optional[str] = None  # Agent identifier for isolation
    metadata: Optional[dict] = None  # Additional metadata from context manager
    container_id: Optional[str] = None  # Docker container ID for isolation
    pipeline_id: Optional[str] = (
        None  # Shared serial pipeline for nested agent/tool trees
    )
    step_id: Optional[str] = None  # Current serialized execution step
    parent_step_id: Optional[str] = None  # Parent serialized execution step
    execution_mode: Optional[str] = None  # Runtime execution mode (e.g. serial_subtree)
    action_state: Optional[Any] = (
        None  # Trusted task and call chain for the policy gate
    )
    action_depth: int = (
        0  # Context-local delegation depth (safe across parallel branches)
    )
    stream_observer: Optional[Any] = (
        None  # The run's own observer, so sub-agents report into the same view
    )
    run_control: Optional[RunControl] = (
        None  # Graceful Stop of the user's turn, shared with its sub-agents
    )

def _get_runner() -> Any:
    """The SDK's Runner as it is now, so a patched ``agents.Runner`` applies."""
    return agents.Runner
