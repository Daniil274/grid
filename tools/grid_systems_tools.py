"""Read-only view of the Grid systems in a repository, for the administrator system.

A Grid system is one config file with its own agents; ``routing.yaml`` at the
repository root lists the systems the router can pick. These tools let an agent
see that catalog and check a system it created or changed before committing it.
Both only read: they never edit configs or the catalog.

Paths are resolved inside the agent's working directory, which is the repository
being changed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from agents import function_tool

from core.config.config import Config
from core.managers.project_tools_loader import get_project_loader, set_project_loader
from core.routing import check_system
from utils.path_utils import display_agent_path_auto, resolve_agent_path_auto

CATALOG_FILE = "routing.yaml"


@contextmanager
def _isolated_loader() -> Iterator[None]:
    """Loading a config replaces the process-wide project tools loader; restore it."""
    previous = get_project_loader()
    try:
        yield
    finally:
        set_project_loader(previous)


def _catalog() -> Optional[Config]:
    path = Path(resolve_agent_path_auto(CATALOG_FILE))
    return Config(str(path)) if path.is_file() else None


def _system_path(catalog: Config, config: str) -> Path:
    path = Path(config)
    if not path.is_absolute():
        path = catalog.config_path.resolve().parent / path
    return path.resolve()


def _describe_agents(config: Config) -> List[Dict[str, Any]]:
    return [
        {
            "key": key,
            "name": agent.name,
            "model": agent.primary_model,
            "models": agent.model_keys(),
            "routable": agent.routable,
            "description": agent.description,
            "tools": list(agent.tools),
            "skills": list(agent.system_skills),
        }
        for key, agent in config.config.agents.items()
    ]


@function_tool
def grid_systems_catalog() -> str:
    """List the systems registered in routing.yaml with their descriptions and agents.

    Read it before creating a system (to avoid overlapping an existing one) and
    before changing a system (to see its agents, tools and skills).
    """
    with _isolated_loader():
        catalog = _catalog()
        if catalog is None:
            return json.dumps({"error": f"{CATALOG_FILE} not found in the working directory"})
        routing = catalog.config.routing
        systems = []
        for name, entry in routing.systems.items():
            path = _system_path(catalog, entry.config)
            item: Dict[str, Any] = {
                "name": name,
                "config": display_agent_path_auto(str(path)),
                "description": entry.description,
                "requires": list(entry.requires),
            }
            try:
                config = Config(str(path))
                item["default_agent"] = config.get_default_agent()
                item["agents"] = _describe_agents(config)
            except Exception as exc:
                item["error"] = f"config failed to load: {exc}"
            systems.append(item)
    return json.dumps(
        {"default_system": routing.default_system, "systems": systems},
        ensure_ascii=False,
        indent=2,
    )


@function_tool
def grid_check_system(config_path: str) -> str:
    """Check one system config for problems that would break it at run time.

    Reports config load errors, undeclared or unimplemented tools, agent tools
    targeting unknown agents, missing skills, routable agents without a
    description, and whether the system is registered in routing.yaml. Run it on
    every system you create or change; healthy means ready to commit.

    Args:
        config_path: Path to the system's config.yaml, relative to the repository root.
    """
    path = Path(resolve_agent_path_auto(config_path)).resolve()
    visible = display_agent_path_auto(str(path))
    if not path.is_file():
        return json.dumps({"config": visible, "healthy": False, "issues": ["config file not found"]})

    with _isolated_loader():
        registered_as = None
        requires: List[str] = []
        catalog = _catalog()
        if catalog is not None:
            for name, entry in catalog.config.routing.systems.items():
                if _system_path(catalog, entry.config) == path:
                    registered_as, requires = name, list(entry.requires)
                    break
        try:
            issues = check_system(Config(str(path)), requires=requires)
        except Exception as exc:
            issues = [f"config failed to load: {exc}"]

    return json.dumps(
        {
            "config": visible,
            "registered_as": registered_as,
            "healthy": not issues,
            "issues": issues,
        },
        ensure_ascii=False,
        indent=2,
    )


GRID_SYSTEMS_TOOLS: Dict[str, Any] = {
    "grid_systems_catalog": grid_systems_catalog,
    "grid_check_system": grid_check_system,
}
