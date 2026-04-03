"""
Structured prompt section types for model context assembly.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


PromptScope = Literal["static", "dynamic"]


@dataclass(frozen=True)
class PromptSection:
    """A named prompt section that can be assembled into model instructions."""

    key: str
    content: str
    scope: PromptScope = "dynamic"

    def to_debug_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "scope": self.scope,
            "length": len(self.content),
            "preview": self.content[:240],
        }


@dataclass(frozen=True)
class ModelContextAssembly:
    """Structured result of assembling prompt context for a model call."""

    instructions: str
    sections: List[PromptSection]
    context_id: Optional[str] = None
    history_strategy: str = "prompt"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_debug_payload(self) -> Dict[str, Any]:
        return {
            "context_id": self.context_id,
            "history_strategy": self.history_strategy,
            "instruction_length": len(self.instructions),
            "sections": [section.to_debug_dict() for section in self.sections],
            "metadata": dict(self.metadata),
        }
