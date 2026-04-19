"""Tools for invoking the live system-builder prototype from agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from agents import RunContextWrapper, function_tool

from core.config import Config
from core.platform.builder import LiveSystemBuilder


def _get_factory(context: RunContextWrapper) -> Any:
    raw = getattr(context, "context", None)
    return getattr(raw, "factory", None)


def _get_config(context: RunContextWrapper) -> Config:
    factory = _get_factory(context)
    config = getattr(factory, "config", None) if factory is not None else None
    if config is None:
        return Config("config.yaml")
    return config


@function_tool
async def system_build_bundle(
    context: RunContextWrapper,
    request_text: str,
    mode: str,
    requested_system_id: str,
    base_system_id: Optional[str] = None,
    auto_promote: bool = False,
    output_dir: str = "workspace/generated_systems_from_agent",
    model_key: str = "kimi-k2.5-opencode",
) -> str:
    """Generate a new or improved system bundle, register it, and run a candidate test."""
    config = _get_config(context)
    builder = LiveSystemBuilder(config, model_key=model_key)
    output_root = Path(config.get_absolute_path(output_dir))
    registry_path = Path(config.get_system_registry_path())

    report = await builder.build_from_request(
        request_text,
        mode=mode,
        output_root=output_root,
        registry_path=registry_path,
        requested_system_id=requested_system_id,
        base_system_id=base_system_id,
        auto_promote=auto_promote,
    )
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)


SYSTEM_BUILDER_TOOLS: Dict[str, Any] = {
    "system_build_bundle": system_build_bundle,
}
