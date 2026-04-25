import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

@dataclass
class ProgressEvent:
    """
    Progress event from an agent or tool.

    Event types:
    - agent_start: Agent started execution
    - agent_end: Agent completed execution
    - tool_call_start: Tool started execution
    - tool_call_end: Tool completed execution
    - tool_error: Tool error
    - blackboard_post: Blackboard post
    - subagent_spawn: Sub-agent spawned
    - error: Agent error
    """

    event_type: str
    agent_name: str
    content: str
    parent_id: Optional[str] = None  # For building agent tree
    status: str = "running"  # 'running', 'completed', 'failed'
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    details: Optional[Dict[str, Any]] = None  # Additional data for spoiler
    event_id: str = field(default_factory=lambda: f"{int(time.time() * 1000)}")

    def __post_init__(self):
        """Ensure details is a dict"""
        if self.details is None:
            self.details = {}
