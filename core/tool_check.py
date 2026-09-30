"""Health check of a Grid system: which agent tools will fail, and why.

Runs before any agent does, so the person sees the problem at startup or when a
message is routed, not as a failed tool call in the middle of a task. It never
disables anything: every agent keeps all of its tools; a tool whose check fails
may still work (a service can come up, a key can be exported later). The one
exception is not its doing: a server that isolates its agents withholds tools
that act on the host, and the check says so.

Two kinds of issues:

* ``config`` - the config itself is wrong: an undeclared or unimplemented tool,
  an agent tool aimed at no agent, a missing skill. Fixed by editing the system.
* ``environment`` - the config is fine, this machine is not ready: a program,
  package, environment variable, API key or service is missing, or the tool is
  for another platform. Fixed on the machine, not in the config.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.config.config import Config
from core.managers.project_tools_loader import get_project_loader, set_project_loader
from schemas import ToolType
from utils.tool_isolation import is_confined
from utils.tool_requirements import unmet

CONFIG = "config"
ENVIRONMENT = "environment"


@dataclass(frozen=True)
class ToolIssue:
    """One reason a system, an agent or one of its tools will not work."""

    kind: str
    problem: str
    agent: Optional[str] = None
    tool: Optional[str] = None
    hint: str = ""

    @property
    def subject(self) -> str:
        if self.agent and self.tool:
            return f"agent '{self.agent}', tool '{self.tool}'"
        if self.agent:
            return f"agent '{self.agent}'"
        if self.tool:
            return f"tool '{self.tool}'"
        return "system"

    def text(self) -> str:
        hint = f" (fix: {self.hint})" if self.hint else ""
        return f"{self.subject}: {self.problem}{hint}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "tool": self.tool,
            "problem": self.problem,
            "hint": self.hint,
            "text": self.text(),
        }


def diagnose(config: Config, *, requires: Iterable[str] = (), confined: bool = False) -> List[ToolIssue]:
    """Every problem of one system, config and environment alike.

    ``confined``: the system runs in a space that isolates its agents whatever
    the config says (a server with accounts, AgentFactory ``confine_tools``):
    commands run in the user's container, and tools that act on the host are
    withheld."""
    previous_loader = get_project_loader()
    set_project_loader(config.project_tools_loader)
    try:
        return _Diagnosis(config, confined=confined).run(requires)
    finally:
        set_project_loader(previous_loader)


def agent_issues(config: Config, issues: Iterable[ToolIssue], agent_key: str) -> List[ToolIssue]:
    """Issues that affect a run of *agent_key*: the system-wide ones, its own,
    and those of every subagent it can call through agent tools."""
    return [issue for issue in issues if issue.agent is None or issue.agent in reachable_agents(config, agent_key)]


def reachable_agents(config: Config, agent_key: str) -> List[str]:
    """*agent_key* and every agent it can reach through agent tools, in call order."""
    grid = config.config
    order: List[str] = []
    pending = [agent_key]
    while pending:
        key = pending.pop(0)
        if key in order or key not in grid.agents:
            continue
        order.append(key)
        for tool_name in grid.agents[key].tools:
            tool = grid.tools.get(tool_name)
            if tool is not None and tool.type == ToolType.AGENT:
                pending.append(tool.target_agent or tool_name)
    return order


def summarize(issues: Iterable[ToolIssue]) -> List[str]:
    """One line per distinct problem: the tools and agents it hits are listed together."""
    groups: Dict[tuple, tuple] = {}
    for issue in issues:
        if not issue.tool:
            groups.setdefault((issue.text(),), ([], []))
            continue
        tools, agents = groups.setdefault((issue.kind, issue.problem, issue.hint), ([], []))
        if issue.tool not in tools:
            tools.append(issue.tool)
        if issue.agent and issue.agent not in agents:
            agents.append(issue.agent)
    lines = []
    for key, (tools, agents) in groups.items():
        if len(key) == 1:
            lines.append(key[0])
            continue
        _, problem, hint = key
        subject = f"tool '{tools[0]}'" if len(tools) == 1 else f"tools {', '.join(tools)}"
        if agents:
            subject += f" (agent '{agents[0]}')" if len(agents) == 1 else f" (agents {', '.join(agents)})"
        lines.append(f"{subject}: {problem}" + (f" (fix: {hint})" if hint else ""))
    return lines


class _Diagnosis:
    def __init__(self, config: Config, *, confined: bool = False) -> None:
        self.config = config
        self.grid = config.config
        self.confined = confined
        self.isolated = confined or bool(getattr(self.grid.isolation, "enabled", False))
        # A package is read once, however many agents use its tool.
        self._packages_checked: set = set()
        self.loader = config.project_tools_loader
        self.issues: List[ToolIssue] = []
        self._requirements_checked: Dict[str, List[str]] = {}

    def add(self, kind: str, problem: str, *, agent: str = None, tool: str = None, hint: str = "") -> None:
        self.issues.append(ToolIssue(kind, problem, agent=agent, tool=tool, hint=hint))

    # -- system ------------------------------------------------------------
    def run(self, requires: Iterable[str]) -> List[ToolIssue]:
        for program in requires:
            if shutil.which(program) is None:
                self.add(ENVIRONMENT, f"required program '{program}' is not on PATH")
        if self.isolated and shutil.which("docker") is None:
            self.add(
                ENVIRONMENT,
                "isolation is enabled, but docker is not on PATH: agents cannot start their container",
                hint="Install Docker or set isolation.enabled: false",
            )
        self._check_project_tools()
        for agent_key, agent in self.grid.agents.items():
            self._check_agent(agent_key, agent)
        return self.issues

    def _check_project_tools(self) -> None:
        settings = self.grid.settings.project_tools
        if settings is None or not settings.enabled:
            return
        if self.loader is None or not self.loader.tools_dir.is_dir():
            where = self.loader.tools_dir if self.loader else settings.tools_directory
            self.add(CONFIG, f"project tools directory {where} does not exist")
        for name in settings.base_tools or []:
            if not self._implemented(name):
                self.add(CONFIG, f"base tool '{name}' from settings.project_tools.base_tools is not implemented", tool=name)

    # -- agent -------------------------------------------------------------
    def _check_agent(self, agent_key: str, agent: Any) -> None:
        if agent.routable and not agent.description:
            self.add(CONFIG, "has no description, routing to it is blind", agent=agent_key)
        for skill_name in agent.system_skills:
            if self.config.skill_path(skill_name) is None:
                self.add(CONFIG, f"uses missing skill '{skill_name}'", agent=agent_key)
        self._check_models(agent_key, agent)
        for tool_name in agent.tools:
            self._check_tool(agent_key, agent, tool_name)
        for entry in agent.auto_run_tools or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if name and name not in agent.tools and name not in self.grid.tools:
                self.add(CONFIG, f"auto-run tool '{name}' is not declared in tools", agent=agent_key, tool=name)

    def _check_models(self, agent_key: str, agent: Any) -> None:
        missing = [key for key in agent.model_keys() if self._model_key_problem(key)]
        if not missing:
            return
        keys = agent.model_keys()
        if len(missing) == len(keys):
            reasons = "; ".join(self._model_key_problem(key) for key in missing)
            self.add(ENVIRONMENT, f"cannot run, no model is usable: {reasons}", agent=agent_key,
                     hint="Export the API key in the environment or .env")
        elif keys[0] in missing:
            self.add(ENVIRONMENT, f"primary model unusable ({self._model_key_problem(keys[0])}); fallback models will answer",
                     agent=agent_key, hint="Export the API key in the environment or .env")

    def _model_key_problem(self, model_key: str) -> Optional[str]:
        model = self.grid.models.get(model_key)
        if model is None:
            return None  # an unknown model is rejected when the config loads
        provider = self.grid.providers.get(model.provider)
        if provider is None or provider.api_key or not provider.api_key_env:
            return None
        if os.environ.get(provider.api_key_env):
            return None
        return f"model '{model_key}' needs {provider.api_key_env} (provider '{model.provider}')"

    # -- tools -------------------------------------------------------------
    def _check_tool(self, agent_key: str, agent: Any, tool_name: str) -> None:
        tool = self.grid.tools.get(tool_name)
        if tool is None:
            self.add(CONFIG, f"uses undeclared tool '{tool_name}'", agent=agent_key)
        elif tool.type == ToolType.AGENT:
            self._check_agent_tool(agent_key, tool_name, tool)
        elif tool.type == ToolType.FUNCTION:
            self._check_function_tool(agent_key, tool_name)
        elif tool.type == ToolType.MCP:
            self._check_mcp_tool(agent_key, agent, tool_name, tool)

    def _check_agent_tool(self, agent_key: str, tool_name: str, tool: Any) -> None:
        # The factory calls the agent named by target_agent, or by the tool key itself.
        target = tool.target_agent or tool_name
        if target not in self.grid.agents:
            self.add(CONFIG, f"calls unknown agent '{target}'", agent=agent_key, tool=tool_name,
                     hint="Set target_agent to an existing agent key")
            return
        target_agent = self.grid.agents[target]
        if all(self._model_key_problem(key) for key in target_agent.model_keys()):
            self.add(ENVIRONMENT, f"subagent '{target}' cannot run: none of its models has an API key",
                     agent=agent_key, tool=tool_name, hint="Export the API key in the environment or .env")

    def _check_mcp_tool(self, agent_key: str, agent: Any, tool_name: str, tool: Any) -> None:
        if not (agent.mcp_enabled or self.config.is_mcp_enabled()):
            self.add(CONFIG, "is an MCP tool, but MCP is off for this agent: its server will not start",
                     agent=agent_key, tool=tool_name,
                     hint="Set settings.mcp_enabled: true or mcp_enabled: true on the agent")
        if tool.tool_package:
            self._check_tool_package(agent_key, tool_name, tool.tool_package)
            return
        command = (tool.server_command or [None])[0]
        # Isolated, the server starts inside the agent's container (docker exec),
        # so the host's PATH says nothing about it; the image carries its tools.
        if command and not self.isolated and shutil.which(command) is None:
            self.add(ENVIRONMENT, f"MCP server needs '{command}' on PATH", agent=agent_key, tool=tool_name,
                     hint=(tool.prompt_addition and _install_hint(tool.prompt_addition)) or f"Install {command}")

    def _check_tool_package(self, agent_key: str, tool_name: str, relative: str) -> None:
        """Read the package, never run it (core.tool_packages): its problems are the config's."""
        from core.tool_packages import PackageError, analyze, package_dir

        if tool_name in self._packages_checked:
            return
        self._packages_checked.add(tool_name)
        try:
            info = analyze(package_dir(self.config.config_path.parent, relative))
        except PackageError as exc:
            self.add(CONFIG, str(exc), tool=tool_name)
            return
        for issue in info.issues:
            self.add(CONFIG, f"tool package: {issue}", tool=tool_name)

    def _check_function_tool(self, agent_key: str, tool_name: str) -> None:
        from tools.function_tools import tool_isolation

        if self.confined and not is_confined(tool_isolation(tool_name, self.loader)):
            self.add(CONFIG, "withheld on this server: it acts on the host, not in the user's container or workspace",
                     agent=agent_key, tool=tool_name,
                     hint="Declare TOOL_ISOLATION for it once it keeps to the user's side (utils/tool_isolation.py)")
            return
        requires = self._requirements(tool_name)
        if self._implemented(tool_name):
            for reason in self._unmet(tool_name, requires):
                self.add(ENVIRONMENT, reason, agent=agent_key, tool=tool_name, hint=requires.hint)
            return
        # Not loaded. A declared requirement explains best why its module failed.
        reasons = self._unmet(tool_name, requires) if requires else []
        failure = self._import_failure(tool_name)
        if reasons:
            for reason in reasons:
                self.add(ENVIRONMENT, f"not loaded, {reason}", agent=agent_key, tool=tool_name, hint=requires.hint)
        elif failure:
            file_name, error = failure
            self.add(ENVIRONMENT, f"not loaded, {file_name} failed to import ({error})", agent=agent_key,
                     tool=tool_name, hint=self._install_hint_for(error))
        else:
            failed = ", ".join(sorted(self._all_import_failures()))
            also = f" (tool files that failed to import: {failed})" if failed else ""
            self.add(CONFIG, f"is not implemented{also}", agent=agent_key, tool=tool_name,
                     hint="Add a function with this name to the project tools directory or to tools/")

    def _implemented(self, name: str) -> bool:
        from tools.function_tools import AVAILABLE_TOOLS, TOOL_ALIASES

        if self.loader and self.loader.has_tool(name):
            return True
        return TOOL_ALIASES.get(name, name) in AVAILABLE_TOOLS

    def _requirements(self, name: str) -> Any:
        from tools.function_tools import tool_requirements

        if self.loader and name in self.loader.requirements:
            return self.loader.requirements[name]
        return tool_requirements(name)

    def _unmet(self, name: str, requires: Any) -> List[str]:
        if requires is None:
            return []
        if name not in self._requirements_checked:
            self._requirements_checked[name] = unmet(requires)
        return self._requirements_checked[name]

    def _all_import_failures(self) -> Dict[str, str]:
        from tools.function_tools import load_errors

        failures = {f"tools/{path.rsplit('.', 1)[-1]}.py": error for path, error in load_errors().items()}
        if self.loader:
            failures.update(self.loader.load_errors)
        return failures

    def _import_failure(self, name: str) -> Optional[tuple]:
        for file_name, error in self._all_import_failures().items():
            if Path(file_name).stem == name:
                return file_name, error
        return None

    def _install_hint_for(self, error: str) -> str:
        if "No module named" not in error:
            return ""
        requirements = Path(self.config.config_path).resolve().parent / "requirements.txt"
        if requirements.is_file():
            return f"pip install -r {_relative(requirements)}"
        return "Install the missing Python package"


def _install_hint(prompt_addition: str) -> str:
    """An install command mentioned in the tool's own prompt, e.g. `npm i -g ...`."""
    for chunk in prompt_addition.split("`"):
        if chunk.startswith(("npm ", "pip ", "pipx ", "cargo ", "go install", "winget ", "brew ")):
            return chunk
    return ""


def _relative(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()
