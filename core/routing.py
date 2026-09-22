"""Automatic routing of a user message to a Grid system and one of its agents.

A system is a Grid config file with its own agents. The router model reads the
message plus the descriptions of the candidates and names one of them: first a
system from ``routing.systems``, then an agent inside that system.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import httpx

from core.config.config import Config
from core.managers.model_manager import ModelManager
from core.managers.project_tools_loader import get_project_loader, set_project_loader
from schemas import ToolType

logger = logging.getLogger("grid.routing")

ROUTER_PROMPT = """You route a user message to the single best-fitting candidate.

Message:
{task}

Candidates (id: description):
{candidates}
{previous}
Reply with JSON only: {{"choice": "<candidate id>"}}"""

PREVIOUS_HINT = (
    "\nThe previous message went to '{previous}'. Keep it for follow-ups "
    "and continuations; switch only when the message clearly needs another candidate.\n"
)

DECISION_INSTRUCTIONS = "Pick the single candidate best suited to handle the user message."

MAX_TASK_CHARS = 4000
DEFAULT_SYSTEM = "default"


def check_system(config: Config, *, requires: Iterable[str] = ()) -> List[str]:
    """Problems that would break one system at run time; empty when it is healthy.

    Checks what config loading does not: routable agents are described, every
    function tool has an implementation, agent tools target existing agents, MCP
    servers and required programs are on PATH, and system skills exist.
    """
    from tools.function_tools import AVAILABLE_TOOLS, TOOL_ALIASES

    issues: List[str] = []
    declared = config.config.tools
    loader = config.project_tools_loader
    previous_loader = get_project_loader()
    set_project_loader(loader)
    try:
        for agent_key, agent in config.config.agents.items():
            if agent.routable and not agent.description:
                issues.append(f"agent '{agent_key}' has no description, routing to it is blind")
            for skill_name in agent.system_skills:
                if config.skill_path(skill_name) is None:
                    issues.append(f"agent '{agent_key}' uses missing skill '{skill_name}'")
            for tool_name in agent.tools:
                tool = declared.get(tool_name)
                if tool is None:
                    issues.append(f"agent '{agent_key}' uses undeclared tool '{tool_name}'")
                elif tool.type == ToolType.AGENT:
                    if tool.target_agent and tool.target_agent not in config.config.agents:
                        issues.append(f"tool '{tool_name}' targets unknown agent '{tool.target_agent}'")
                elif tool.type == ToolType.FUNCTION:
                    resolved = TOOL_ALIASES.get(tool_name, tool_name)
                    if not ((loader and loader.has_tool(tool_name)) or resolved in AVAILABLE_TOOLS):
                        issues.append(f"tool '{tool_name}' of agent '{agent_key}' is not implemented")
                elif tool.type == ToolType.MCP:
                    command = (tool.server_command or [None])[0]
                    if command and shutil.which(command) is None:
                        issues.append(f"MCP tool '{tool_name}' requires '{command}' on PATH")
    finally:
        set_project_loader(previous_loader)

    for program in requires:
        if shutil.which(program) is None:
            issues.append(f"required program '{program}' is not on PATH")
    return issues


class Router:
    """Choose one candidate id for a task with a small chat model."""

    def __init__(self, client, model_name: str) -> None:
        self.client = client
        self.model_name = model_name

    async def choose(
        self,
        task: str,
        candidates: Dict[str, str],
        *,
        default: Optional[str] = None,
        previous: Optional[str] = None,
    ) -> str:
        """Return the id of the best candidate; *candidates* maps id -> description."""
        if not candidates:
            raise ValueError("No candidates to route between")
        if len(candidates) == 1:
            return next(iter(candidates))

        fallback = default if default in candidates else next(iter(candidates))
        try:
            choice = await self._pick(
                task[:MAX_TASK_CHARS],
                candidates,
                previous if previous in candidates else None,
            )
        except Exception as exc:
            logger.warning("Routing call failed, using '%s': %s", fallback, exc)
            return fallback
        if choice not in candidates:
            logger.warning("Router returned unknown candidate %r, using '%s'", choice, fallback)
            return fallback
        return choice

    async def _pick(self, task: str, candidates: Dict[str, str], previous: Optional[str]) -> Optional[str]:
        listing = "\n".join(f"- {cid}: {desc or cid}" for cid, desc in candidates.items())
        hint = PREVIOUS_HINT.format(previous=previous) if previous else ""
        prompt = ROUTER_PROMPT.format(task=task, candidates=listing, previous=hint)
        response = await self.client.chat.completions.create(
            model=self.model_name,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_choice(response.choices[0].message.content or "")


class DecisionsRouter(Router):
    """Router backed by a decisions model such as typesafe/jev-1.13.

    Decisions models are not chat models: OpenRouter serves them at
    ``/api/alpha/decisions``, which takes the context as ``state`` and a
    ``choice`` question whose ``criteria`` map each option to its description.
    """

    def __init__(self, http: httpx.AsyncClient, url: str, api_key: str, model_name: str) -> None:
        super().__init__(None, model_name)
        self.http = http
        self.url = url
        self.api_key = api_key

    async def _pick(self, task: str, candidates: Dict[str, str], previous: Optional[str]) -> Optional[str]:
        state = f"User message:\n{task}"
        if previous:
            state += PREVIOUS_HINT.format(previous=previous)
        response = await self.http.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model_name,
                "state": state,
                "questions": {
                    "route": {
                        "type": "choice",
                        "instructions": DECISION_INSTRUCTIONS,
                        "criteria": {cid: desc or cid for cid, desc in candidates.items()},
                    }
                },
            },
        )
        response.raise_for_status()
        answer = response.json()["answers"]["route"]
        logger.debug("Decision %s (confidence %s)", answer.get("choice"), answer.get("confidence"))
        return answer.get("choice")


def _parse_choice(text: str) -> Optional[str]:
    """Extract ``choice`` from a JSON reply, tolerating code fences around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            choice = json.loads(match.group(0)).get("choice")
            return str(choice).strip() if choice is not None else None
        except (json.JSONDecodeError, AttributeError):
            pass
    return text.strip().strip('"`') or None


@dataclass
class Route:
    """Where a message should run."""

    system: str
    agent: str
    config: Config
    warning: str = ""


class AutoRouter:
    """Route a message to a system, then to an agent of that system."""

    def __init__(
        self,
        root_config: Config,
        router: Router,
        *,
        working_directory: Optional[str] = None,
    ) -> None:
        self.root_config = root_config
        self.router = router
        self.working_directory = working_directory
        self._configs: Dict[str, Config] = {}
        self._last: Optional[Route] = None

    @classmethod
    def from_config(
        cls, root_config: Config, *, working_directory: Optional[str] = None
    ) -> Optional["AutoRouter"]:
        """Build a router from ``routing:`` in *root_config*, or None when routing is off."""
        model_key = root_config.config.routing.model
        if not model_key:
            return None
        if root_config.config.routing.api == "decisions":
            model = root_config.get_model(model_key)
            provider = root_config.get_provider(model.provider)
            base_url = provider.base_url.rstrip("/").removesuffix("/v1")
            http = httpx.AsyncClient(
                timeout=float(provider.timeout),
                proxy=root_config.get_proxy_for_provider(model.provider),
                trust_env=False,
            )
            router: Router = DecisionsRouter(
                http, f"{base_url}/alpha/decisions", root_config.get_api_key(model.provider) or "", model.name
            )
        else:
            client, model_name = ModelManager(root_config).get_openai_client_for_model(model_key)
            router = Router(client, model_name)
        return cls(root_config, router, working_directory=working_directory)

    def systems(self) -> Dict[str, str]:
        """Candidate systems as name -> description."""
        routing = self.root_config.config.routing
        if not routing.systems:
            return {DEFAULT_SYSTEM: ""}
        return {name: system.description for name, system in routing.systems.items()}

    def default_system(self) -> str:
        """System used at startup and when routing to another one fails."""
        systems = self.systems()
        default = self.root_config.config.routing.default_system
        return default if default in systems else next(iter(systems))

    def system_config_path(self, name: str) -> Path:
        """Resolved path of the config file of system *name*."""
        routing = self.root_config.config.routing
        if name == DEFAULT_SYSTEM and not routing.systems:
            return self.root_config.config_path.resolve()
        path = Path(routing.systems[name].config)
        if not path.is_absolute():
            path = self.root_config.config_path.resolve().parent / path
        return path.resolve()

    def system_config(self, name: str) -> Config:
        """Load (once) and return the config of system *name*."""
        if name not in self._configs:
            path = self.system_config_path(name)
            if path == self.root_config.config_path.resolve():
                self._configs[name] = self.root_config
            else:
                self._configs[name] = Config(str(path), self.working_directory)
        return self._configs[name]

    def check_systems(self) -> Dict[str, List[str]]:
        """Report broken systems: unloadable configs, missing tools or required programs.

        Loading every system here is deliberate: a system that only breaks when a
        message is routed to it would otherwise stay invisible until then.
        """
        problems: Dict[str, List[str]] = {}
        routing = self.root_config.config.routing
        # Loading a config replaces the process-wide project tools loader.
        previous_loader = get_project_loader()
        try:
            for name in self.systems():
                try:
                    config = self.system_config(name)
                except Exception as exc:
                    problems[name] = [f"config failed to load: {exc}"]
                    continue
                system = routing.systems.get(name)
                issues = check_system(config, requires=system.requires if system else ())
                if issues:
                    problems[name] = issues
        finally:
            set_project_loader(previous_loader)
        return problems

    @staticmethod
    def agents(config: Config) -> Dict[str, str]:
        """Routable agents of a system as key -> description."""
        return {
            key: agent.description or agent.name
            for key, agent in config.config.agents.items()
            if agent.routable
        }

    async def route(self, message: str) -> Route:
        """Pick the system and the agent that should handle *message*."""
        systems = self.systems()
        routing = self.root_config.config.routing
        last = self._last
        system = await self.router.choose(
            message,
            systems,
            default=routing.default_system,
            previous=last.system if last else None,
        )
        fallback_system = self.default_system()
        warning = ""
        try:
            config = self.system_config(system)
        except Exception as exc:
            if system == fallback_system:
                raise
            logger.error("System '%s' failed to load, using '%s': %s", system, fallback_system, exc)
            warning = f"system '{system}' failed to load ({exc}); using '{fallback_system}'"
            system = fallback_system
            config = self.system_config(system)
        # Project tools are resolved through one process-wide loader; point it at this system.
        set_project_loader(config.project_tools_loader)
        agents = self.agents(config) or {config.get_default_agent(): ""}
        agent = await self.router.choose(
            message,
            agents,
            default=config.get_default_agent(),
            previous=last.agent if last and last.system == system else None,
        )
        self._last = Route(system=system, agent=agent, config=config, warning=warning)
        logger.info("Routed message to %s/%s", system, agent)
        return self._last
