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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config.config import Config
from core.routing import AutoRouter
from core.tool_check import ToolIssue, agent_issues, diagnose

logger = logging.getLogger("grid.web_chat.systems")

#: A routing call must not hold up a turn; past this we use the default.
ROUTING_TIMEOUT_SECONDS = 6.0
#: A system's tool check is reused this long, so a fixed machine shows up without a restart.
ISSUES_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class SystemInfo:
    """One selectable system, as the picker shows it."""

    key: str
    name: str
    description: str
    config_path: Path
    #: "" for a catalog system; "draft" or "mine" for a system of this space only.
    badge: str = ""


@dataclass(frozen=True)
class ExtraSystem:
    """A system one space has beside the catalog's (web_chat.system_hub).

    An admin's space has the created systems still in draft, to test them; a
    user's space has the systems the user built from their own agents. Only a
    ``routable`` one is offered to the router; the others run when picked by hand.
    """

    key: str
    name: str
    description: str
    config_path: Path
    load: Callable[[], Config]
    routable: bool
    badge: str
    requires: Tuple[str, ...] = ()


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
        build_factory_for_key: Optional[Callable[[Config, str], Any]] = None,
        catalog: Optional[Config] = None,
        working_directory: Optional[str] = None,
        customize: Optional[Callable[[str, Config], set[str]]] = None,
        confined: bool = False,
        extras: Optional[Callable[[], List[ExtraSystem]]] = None,
        admin: bool = True,
    ) -> None:
        """``customize(system_key, config)`` adjusts a system's config once, when
        it is first loaded - the space's personal agents (web_chat.personal_agents) -
        and returns the agent keys it changed. ``confined``: the space isolates
        its agents whatever a config says (core.tool_check.diagnose). ``extras()``
        lists the systems of this space only (:class:`ExtraSystem`); it is asked
        again by :meth:`refresh_extras`. ``admin`` False hides the catalog's
        ``admins_only`` systems: they are not listed, pinned or routed to."""
        self._base_config = base_config
        self._build_factory = build_factory
        self._build_factory_for_key = build_factory_for_key
        self._customize = customize
        self._confined = confined
        # The configs customized so far, by system key.
        self._customized: Dict[str, Config] = {}
        self._catalog = catalog
        # With a catalog the router picks between systems; without one it can
        # still pick between the agents of the single loaded system.
        self._router = AutoRouter.from_config(catalog or base_config, working_directory=working_directory)
        self._factories: Dict[str, Any] = {}
        self._issues: Dict[str, Tuple[float, List[ToolIssue]]] = {}
        self._base_key = _key_for(base_config.config_path)
        self._admin = admin
        self._extras_source = extras
        self._extras: Dict[str, ExtraSystem] = {}
        self._extra_configs: Dict[str, Config] = {}
        self.refresh_extras()

    # -- the space's own systems ---------------------------------------------
    def refresh_extras(self) -> None:
        """Ask again for the space's own systems; what they built is dropped.

        Only with a catalog: the extras stand beside the catalog's systems.
        """
        dropped = set(self._extras)
        extras: Dict[str, ExtraSystem] = {}
        if self._extras_source is not None and self.has_catalog:
            listed = set(self._router.systems())
            for extra in self._extras_source():
                if extra.key in listed or extra.key in extras:
                    logger.error("System '%s' of this space collides with another; left out", extra.key)
                    continue
                extras[extra.key] = extra
        self._extras = extras
        for key in dropped | set(extras):
            self._extra_configs.pop(key, None)
            self._factories.pop(key, None)
            self._issues.pop(key, None)

    def extra(self, system_key: str) -> Optional[ExtraSystem]:
        return self._extras.get(system_key)

    def route_candidates(self) -> Dict[str, str]:
        """The systems the router picks from in this space, as key -> description."""
        return self._route_candidates() if self.has_catalog else {}

    async def probe(self, message: str, candidates: Dict[str, str]) -> str:
        """The system the router would pick for *message* among *candidates*."""
        return await self._choose(message, candidates, self.default_key(), None)

    def _hidden(self) -> set:
        """The catalog systems this space may not use: admins-only ones for a user."""
        if getattr(self, "_admin", True) or self._catalog is None:
            return set()
        return {key for key, system in self._catalog.config.routing.systems.items() if system.admins_only}

    def _route_candidates(self) -> Dict[str, str]:
        """What the router picks a system from: the catalog's and the routable extras."""
        hidden = self._hidden()
        candidates = {key: text for key, text in self._router.systems().items() if key not in hidden}
        candidates.update({key: extra.description for key, extra in self._extras.items() if extra.routable})
        return candidates

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
        return self._catalog_systems() + [
            SystemInfo(
                key=extra.key,
                name=extra.name,
                description=" ".join((extra.description or "").split()),
                config_path=extra.config_path,
                badge=extra.badge,
            )
            for extra in self._extras.values()
        ]

    def _catalog_systems(self) -> list[SystemInfo]:
        if not self.has_catalog:
            return [
                SystemInfo(
                    key=self._base_key,
                    name=_title(self._base_key),
                    description="",
                    config_path=self._base_config.config_path,
                )
            ]
        return [
            SystemInfo(
                key=key,
                name=self._router.system_name(key) or _title(key),
                description=" ".join((description or "").split()),
                config_path=self._router.system_config_path(key),
            )
            for key, description in self._router.systems().items()
            if key not in self._hidden()
        ]

    def keys(self) -> list[str]:
        return [system.key for system in self.systems()]

    def default_key(self) -> str:
        return self._router.default_system() if self.has_catalog else self._base_key

    def config(self, system_key: str) -> Config:
        extra = self._extras.get(system_key)
        if extra is not None:
            if system_key not in self._extra_configs:
                self._extra_configs[system_key] = extra.load()
            return self._extra_configs[system_key]
        routed = self.has_catalog and system_key in self._router.systems()
        config = self._router.system_config(system_key) if routed else self._base_config
        key = system_key if routed else self._base_key
        if self._customize is not None and key not in self._customized:
            self._customize(key, config)
            self._customized[key] = config
        return config

    def recustomize(self) -> Dict[str, set[str]]:
        """Customize the loaded configs again; the changed agent keys by system.

        Factories built earlier keep built agents; the caller has them forget
        the changed ones.
        """
        if self._customize is None:
            return {}
        return {key: self._customize(key, config) for key, config in self._customized.items()}

    def factory(self, system_key: str) -> Any:
        """The factory for a system, built on first use and cached after."""
        if system_key not in self._factories:
            config = self.config(system_key)
            build_for_key = getattr(self, "_build_factory_for_key", None)
            self._factories[system_key] = (
                build_for_key(config, system_key)
                if build_for_key is not None
                else self._build_factory(config)
            )
        return self._factories[system_key]

    def agents(self, system_key: str) -> Dict[str, Any]:
        """Every agent of a system, routable or not, keyed by agent key."""
        return dict(self.config(system_key).config.agents)

    def has_agent(self, system_key: str, agent_key: str) -> bool:
        return agent_key in self.agents(system_key)

    def built_factories(self) -> Dict[str, Any]:
        """The factories built so far, by system; none is built by asking."""
        return dict(self._factories)

    def agent_label(self, system_key: str, agent_key: str) -> str:
        """Display name of an agent, falling back to its key."""
        try:
            agent = self.agents(system_key).get(agent_key)
        except Exception:  # a broken system still gets its agent named
            return agent_key
        return getattr(agent, "name", None) or agent_key

    def selection_is_valid(self, system_key: Optional[str], agent_key: Optional[str]) -> bool:
        """Whether a pinned selection names what exists; ``None`` is `auto`, always valid."""
        if system_key is not None and system_key not in self.keys():
            return False
        if agent_key is None:
            return True
        return self.has_agent(system_key or self.default_key(), agent_key)

    # -- health ------------------------------------------------------------
    def issues(self, system_key: str) -> List[ToolIssue]:
        """Why tools of a system will fail on this machine; cached briefly."""
        cached = self._issues.get(system_key)
        if cached and time.monotonic() - cached[0] < ISSUES_TTL_SECONDS:
            return cached[1]
        extra = self._extras.get(system_key)
        if extra is not None:
            requires = list(extra.requires)
        else:
            requires = self._router.system_requires(system_key) if self.has_catalog else []
        issues = diagnose(self.config(system_key), requires=requires, confined=self._confined)
        self._issues[system_key] = (time.monotonic(), issues)
        return issues

    def agent_issues(self, system_key: str, agent_key: str) -> List[ToolIssue]:
        """Issues a run of one agent meets: the system's, its own and its subagents'."""
        return agent_issues(self.config(system_key), self.issues(system_key), agent_key)

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
        candidates = self._route_candidates() if self.has_catalog else {}
        routed_system = pinned_system is None and len(candidates) > 1
        chosen_system = pinned_system or (
            await self._choose(message, candidates, self.default_key(), previous[0] if previous else None)
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
        for key, factory in list(self._factories.items()):
            cleanup = getattr(factory, "cleanup", None)
            if cleanup is not None:
                try:
                    await cleanup()
                except Exception:
                    logger.exception("Closing factory for system %s failed", key)
        self._factories.clear()
        http = getattr(getattr(self._router, "router", None), "http", None)
        if http is not None:
            await http.aclose()
