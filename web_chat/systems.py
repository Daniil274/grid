"""The systems the web chat can run, and the routing between them.

A *system* is a Grid config with its own agents - the same unit ``agent_chat``
routes across. A catalog (``routing.yaml``) lists them; each system gets its own
``AgentFactory``, and they all share the runtime's memory, so one conversation
can be answered by different systems without losing its history.

Selection is two levels, each independently pinnable:

    system: auto | <key>      agent: auto | <key>

Whatever is left on ``auto`` is decided per message by the router - exactly what
a CLI session started without ``--agent``/``--config`` does. Pinning a level
bypasses the router for that level and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.config.config import Config
from core.managers.project_tools_loader import set_project_loader
from core.routing import AutoRouter

logger = logging.getLogger("grid.web_chat.systems")

#: A routing call must not hold up a turn; past this we use the default.
ROUTING_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class SystemInfo:
    """One selectable system, as the picker shows it."""

    key: str
    name: str
    description: str
    config_path: Path


@dataclass(frozen=True)
class Resolution:
    """Where one message will run, and how that was decided."""

    system: str
    agent: str
    config: Config
    factory: Any
    routed_system: bool
    routed_agent: bool
    warning: str = ""

    @property
    def routed(self) -> bool:
        return self.routed_system or self.routed_agent


def _title(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title() or key


def _key_for(config_path: Path) -> str:
    """Name a single system after something a person would recognize.

    Config files are usually called ``config.yaml`` (or ``.yaml.example``), so
    the file name says nothing; the directory holding it does.
    """
    stem = config_path.name
    for suffix in (".example", ".yml", ".yaml"):
        stem = stem[: -len(suffix)] if stem.endswith(suffix) else stem
    if stem and stem.lower() not in {"config", "settings"}:
        return stem
    return config_path.parent.name or stem or "system"


class SystemRegistry:
    """Resolves a turn to a system and an agent, and owns a factory per system."""

    def __init__(
        self,
        *,
        base_config: Config,
        build_factory: Callable[[Config], Any],
        catalog: Optional[Config] = None,
        working_directory: Optional[str] = None,
    ) -> None:
        self._base_config = base_config
        self._build_factory = build_factory
        self._catalog = catalog
        # With a catalog the router picks between systems; without one it can
        # still pick between the agents of the single loaded system.
        self._router = AutoRouter.from_config(catalog or base_config, working_directory=working_directory)
        self._factories: Dict[str, Any] = {}
        self._base_key = _key_for(base_config.config_path)

    # -- catalog -----------------------------------------------------------
    @property
    def has_catalog(self) -> bool:
        """True when more than one system is selectable."""
        return bool(self._catalog and self._router and self._router.systems())

    @property
    def can_route(self) -> bool:
        """True when `auto` is a real choice rather than a fallback."""
        return self._router is not None

    def systems(self) -> list[SystemInfo]:
        if not self.has_catalog:
            return [
                SystemInfo(
                    key=self._base_key,
                    name=_title(self._base_key),
                    description=str(self._base_config.config_path),
                    config_path=self._base_config.config_path,
                )
            ]
        return [
            SystemInfo(
                key=key,
                name=_title(key),
                description=" ".join((description or "").split()),
                config_path=self._router.system_config_path(key),
            )
            for key, description in self._router.systems().items()
        ]

    def keys(self) -> list[str]:
        return [system.key for system in self.systems()]

    def default_key(self) -> str:
        return self._router.default_system() if self.has_catalog else self._base_key

    def config(self, system_key: str) -> Config:
        if not self.has_catalog or system_key not in self._router.systems():
            return self._base_config
        return self._router.system_config(system_key)

    def factory(self, system_key: str) -> Any:
        """The factory for a system, built on first use and cached after."""
        if system_key not in self._factories:
            self._factories[system_key] = self._build_factory(self.config(system_key))
        return self._factories[system_key]

    def agents(self, system_key: str) -> Dict[str, Any]:
        """Every agent of a system, routable or not, keyed by agent key."""
        return dict(self.config(system_key).config.agents)

    def has_agent(self, system_key: str, agent_key: str) -> bool:
        return agent_key in self.agents(system_key)

    # -- resolution --------------------------------------------------------
    async def resolve(
        self,
        message: str,
        *,
        system_key: Optional[str] = None,
        agent_key: Optional[str] = None,
        previous: Optional[tuple[str, str]] = None,
    ) -> Resolution:
        """Decide where *message* runs. ``None`` on either level means `auto`.

        *previous* is the ``(system, agent)`` of the last turn in the same
        conversation: the router keeps follow-ups with whoever handled them.
        """
        known = self.keys()
        pinned_system = system_key if system_key in known else None
        routed_system = pinned_system is None and len(known) > 1
        chosen_system = pinned_system or (
            await self._choose(message, self._router.systems(), self.default_key(), previous[0] if previous else None)
            if routed_system
            else self.default_key()
        )

        warning = ""
        try:
            config = self.config(chosen_system)
        except Exception as exc:  # a broken system must not take the turn down
            fallback = self.default_key()
            logger.error("System '%s' failed to load, using '%s': %s", chosen_system, fallback, exc)
            warning = f"system '{chosen_system}' failed to load ({exc}); using '{fallback}'"
            chosen_system, config = fallback, self.config(fallback)

        # Project tools resolve through one process-wide loader; aim it here.
        set_project_loader(config.project_tools_loader)

        pinned_agent = agent_key if self.has_agent(chosen_system, agent_key or "") else None
        candidates = AutoRouter.agents(config) or {config.get_default_agent(): ""}
        routed_agent = pinned_agent is None and len(candidates) > 1
        chosen_agent = pinned_agent or (
            await self._choose(
                message,
                candidates,
                config.get_default_agent(),
                previous[1] if previous and previous[0] == chosen_system else None,
            )
            if routed_agent
            else config.get_default_agent()
        )

        return Resolution(
            system=chosen_system,
            agent=chosen_agent,
            config=config,
            factory=self.factory(chosen_system),
            routed_system=routed_system,
            routed_agent=routed_agent,
            warning=warning,
        )

    async def _choose(
        self,
        message: str,
        candidates: Dict[str, str],
        default: str,
        previous: Optional[str],
    ) -> str:
        """One routing call, bounded in time and never fatal."""
        if self._router is None or len(candidates) <= 1:
            return default if default in candidates else next(iter(candidates), default)
        try:
            return await asyncio.wait_for(
                self._router.router.choose(message, candidates, default=default, previous=previous),
                ROUTING_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Routing timed out after %ss; using '%s'", ROUTING_TIMEOUT_SECONDS, default)
        except Exception:
            logger.exception("Routing failed; using '%s'", default)
        return default

    async def close(self) -> None:
        http = getattr(getattr(self._router, "router", None), "http", None)
        if http is not None:
            await http.aclose()
