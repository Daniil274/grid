"""The factory that builds and runs Grid's agents.

AgentFactory reads a system's config and gives out its agents - models,
tools, MCP servers, instructions - and runs conversation turns with them. Its
parts live in core.factory (see there): the objects it holds (models, MCP
servers, the run journal) and the behaviour mixed into it (turns, tools,
auto-run tools, the policy wiring). This module keeps what ties them
together: construction, the agent builders, caches and the context API.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import agents
from agents import Agent, SQLiteSession
from dotenv import load_dotenv

from core import sdk_patches
from core.application.agent_runtime_support import AgentRuntimeSupport
from core.compact import AutoCompactTrackingState
from core.config.config import Config
from core.context import ContextManager
from core.factory.auto_run import AutoRunTools
from core.factory.journal import RunJournal
from core.factory.mcp import McpServers
from core.factory.models import ModelProvider
from core.factory.policy import PolicyWiring
from core.factory.run_context import (
    AutoRunToolContext,
    GridRunContext,
    layered_compact,
    with_images,
)
from core.factory.sessions import SessionUpkeep
from core.factory.tools import ToolAssembly
from core.factory.turns import TOOL_CALL_CORRECTION, TurnRunner
from core.fallback_model import FallbackModel, ModelCandidate
from core.interruption import RunControl
from core.run_stream import ConsoleStreamObserver, StreamObserver
from core.tracing.config import ImmediateTraceProcessor, get_tracing_config
from utils.exceptions import AgentError, ConfigError

#: What callers import from here; the parts live in core.factory.
__all__ = [
    "AgentFactory",
    "AutoRunToolContext",
    "ConsoleStreamObserver",
    "GridRunContext",
    "TOOL_CALL_CORRECTION",
    "layered_compact",
    "with_images",
]

sdk_patches.install()
load_dotenv()
tracing_config = get_tracing_config()


logger = logging.getLogger("grid.agent_factory")
verbose_logger = logging.getLogger("grid.verbose")
_TRACING_CONFIGURED = False
_TRACING_CONFIG_LOCK = threading.Lock()


def __getattr__(name: str) -> Any:
    """``core.agent_factory.Runner`` is the SDK's Runner as it is now (patched
    or not): runs resolve it at call time (core.factory.run_context.get_runner)."""
    if name == "Runner":
        return getattr(agents, "Runner")
    raise AttributeError(name)


class AgentFactory(TurnRunner, SessionUpkeep, ToolAssembly, AutoRunTools, PolicyWiring):
    """Builds a system's agents and runs its conversation turns.

    One factory per system config. It caches the agents it built, keeps each
    agent's SDK session per conversation, and shares one context manager with
    the other factories of a web space, so a conversation survives routing.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        working_directory: Optional[str] = None,
        *,
        tracing_level: Optional[str] = "INFO",
        stream_observer: Optional[StreamObserver] = None,
        context_manager: Optional[ContextManager] = None,
        container_id: Optional[str] = None,
        policy_config: Optional[Config] = None,
        session_db_path: Optional[str] = None,
        logs_directory: Optional[str] = None,
        confine_tools: bool = False,
    ):
        """
        Initialize Agent Factory.

        Args:
            config: Configuration instance (creates default if None)
            working_directory: Working directory override
            tracing_level: Tracing level for debugging
            stream_observer: Observer for agent stream events
            context_manager: Conversation history to share with other factories
                (the web runtime keeps one across systems); a new one otherwise
            container_id: Docker container ID for isolation
            policy_config: Config whose action policy and model registry win over
                this factory's own config (the root routing config when the CLI
                routes a message between systems); its ``compact`` section is
                the base this config's own section refines (layered_compact)
            session_db_path: SQLite file of the agents' SDK sessions; the logs
                directory's ``agent_sessions.db`` by default. The web chat keeps
                each user's sessions in that user's space.
            logs_directory: Where the factory's runs write their session logs;
                the config's logs directory by default. The web chat gives each
                user's space its own.
            confine_tools: Give agents only the function tools that keep to the
                user's side - their container or their workspace
                (utils.tool_isolation). A space that must isolate its agents
                sets it; an undeclared tool is withheld.
        """
        if tracing_level is not None:
            self._configure_tracing_once(tracing_level)

        # Set up minimal logging for agents SDK to avoid spam
        agents_logger = logging.getLogger("openai.agents")
        agents_logger.setLevel(logging.WARNING)

        self.config = config or Config()
        self.container_id = container_id
        self._logs_directory = Path(logs_directory) if logs_directory else None
        self.confine_tools = confine_tools
        if working_directory:
            self.config.set_working_directory(working_directory)

        # Initialize image processing config
        from utils.image_utils import ImageUtils

        if hasattr(self.config.config, "settings"):
            ImageUtils.set_config(self.config.config.settings.image_processing)

        self.context_manager = context_manager or ContextManager(
            max_history=self.config.get_max_history(),
            persist_path=str(Path(self.config.get_logs_directory()) / "context.json"),
        )

        # What is recorded about the runs, for recovery after a crash and the timeline.
        self.journal = RunJournal(self.context_manager, self.config)
        self._agent_session_db_path = session_db_path or self._build_agent_session_db_path()

        self._runtime_support = AgentRuntimeSupport(
            config=self.config,
            context_manager=self.context_manager,
            container_id=self.container_id,
            session_factory=self._create_persistent_sqlite_session,
        )
        self.instructions_builder = self._runtime_support.instructions_builder

        # Initialize ContainerManager
        from core.managers.container_manager import CONTAINER_WORKDIR, ContainerManager

        self.container_manager = ContainerManager(self.config)
        self._container_workdir = CONTAINER_WORKDIR
        # MCP servers, started once and shared by this factory's agents.
        self.mcp = McpServers(self.config, self.context_manager, self.container_id, CONTAINER_WORKDIR)
        if self.container_id:
            logger.info(
                "AgentFactory initialized with container isolation: %s",
                self.container_id,
            )

        # Caches
        self._agent_cache: Dict[str, Agent] = {}
        self._tool_cache: Dict[str, List[Any]] = {}

        # Session management for agent memory (per agent/context pair)
        self._agent_sessions = self._runtime_support.agent_sessions
        self._stream_observer: StreamObserver = (
            stream_observer or ConsoleStreamObserver()
        )

        # One policy gate per factory, snapshotted from operator configuration.
        self.action_gate = self._build_action_gate(policy_config)

        # Track logged agents to log prompt only once
        self._logged_agents: set[str] = set()

        # Track initialized agents (auto_run_tools executed) per user
        self._initialized_agents: set[str] = set()
        # Stop switches of the top-level turns running now, by conversation.
        self._run_controls: Dict[str, RunControl] = {}

        # Per-session compact tracking state (circuit breaker lives here)
        self._compact_tracking = AutoCompactTrackingState()
        self.compact_config = layered_compact(policy_config, self.config)
        # Models, clients and call settings of this config.
        self.models = ModelProvider(self.config, self._runtime_support, self.compact_config)

        # Initialize pipeline registry for emergency shutdown
        from core.tracing.pipeline_registry import PipelineRegistry

        self._pipeline_registry = PipelineRegistry()
        logger.info("PipelineRegistry initialized")


    def _build_agent_session_db_path(self) -> str:
        """Return durable SQLite path for agent sessions."""
        base_dir = self._logs_directory_path()
        base_dir.mkdir(parents=True, exist_ok=True)
        return str(base_dir / "agent_sessions.db")

    def _create_persistent_sqlite_session(self, session_id: str) -> SQLiteSession:
        """Create a file-backed SDK session so history survives process restarts."""
        return SQLiteSession(session_id=session_id, db_path=self._agent_session_db_path)


    def _logs_directory_path(self) -> Path:
        return self._logs_directory or Path(self.config.get_logs_directory())


    @staticmethod
    def _configure_tracing_once(level: str) -> None:
        global _TRACING_CONFIGURED
        if _TRACING_CONFIGURED:
            return
        with _TRACING_CONFIG_LOCK:
            if _TRACING_CONFIGURED:
                return
            tracing_config.configure_console_tracing(level)
            # Timeline tracer: same DB path as serve_timeline / configure_tracing_from_env
            if os.getenv("GRID_TIMELINE_ENABLED", "true").lower() not in (
                "0",
                "false",
                "no",
            ):
                try:
                    from core.tracing.tracer import get_tracer

                    timeline_exporter = get_tracer()
                    timeline_processor = ImmediateTraceProcessor(
                        timeline_exporter, export_span_start=True
                    )
                    tracing_config._processors.append(timeline_processor)
                except Exception as e:
                    logging.getLogger("grid.tracing").warning(
                        f"Timeline tracer init failed: {e}"
                    )
            tracing_config.apply()
            _TRACING_CONFIGURED = True

    async def initialize(self) -> None:
        """Async init hook for compatibility with API lifespan."""
        return None


    def _get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
        """Get or create a session scoped to an agent/context pair."""
        return self._runtime_support.get_agent_session(agent_key, context_id)


    def resolve_model_key(self, key: Optional[str]) -> str:
        """The model key *key* stands for (aliases, the default); see ModelProvider."""
        return self.models.resolve_key(key)

    def get_openai_client_for_model(self, model_key: str) -> tuple[Any, str]:
        """(client, model name) for *model_key*; see ModelProvider."""
        return self.models.client_for(model_key)

    async def create_agent(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        force_reload: bool = False,
    ) -> Agent:
        """
        Create or retrieve cached agent.

        Args:
            agent_key: Agent configuration key
            context_path: Optional context path for agent
            force_reload: Force recreation even if cached

        Returns:
            Configured Agent instance

        Raises:
            AgentError: If agent creation fails
            ConfigError: If configuration is invalid
        """
        # Use agent_key only for caching to ensure consistent sessions
        cache_key = agent_key

        if not force_reload and cache_key in self._agent_cache:
            return self._agent_cache[cache_key]

        try:
            # Get configurations
            agent_config = self.config.get_agent(agent_key)
            model_keys = agent_config.model_keys()
            candidates: list[ModelCandidate] = []
            for model_key in model_keys:
                sdk_model, candidate_config = self.models.sdk_model(model_key)
                candidates.append(
                    ModelCandidate(
                        key=model_key,
                        model=sdk_model,
                        settings=self.models.settings(
                            candidate_config, agent_config.parallel_tool_calls
                        ),
                    )
                )
            model_config = self.config.get_model(agent_config.primary_model)
            model = (
                candidates[0].model
                if len(candidates) == 1
                else FallbackModel(candidates)
            )

            # Build instructions with context (include conversation context for agents)
            instructions = self._build_agent_instructions(
                agent_key,
                context_path,
                include_conversation_context=False,
            )

            # Get tools (function and agent tools only; MCP tools handled via mcp_servers)
            tools = await self._get_agent_tools(agent_config, agent_key=agent_key)

            # Prepare MCP servers for this agent (if enabled)
            mcp_server_names: list[str] = []
            for tool_key in agent_config.tools:
                try:
                    tool_cfg = self.config.get_tool(tool_key)
                    if tool_cfg.type == "mcp":
                        mcp_server_names.append(tool_key)
                except ConfigError:
                    continue

            mcp_servers_list: list[Any] = []
            if mcp_server_names and (
                agent_config.mcp_enabled or self.config.is_mcp_enabled()
            ):
                mcp_servers_list = await self.mcp.for_tools(mcp_server_names)

            # Create agent
            agent = Agent(
                name=agent_config.name,
                instructions=instructions,
                model=model,
                model_settings=self.models.settings(
                    model_config, agent_config.parallel_tool_calls
                ),
                tools=tools,
                mcp_servers=mcp_servers_list,
            )
            setattr(agent, "_grid_agent_key", agent_key)
            setattr(agent, "_grid_model_key", agent_config.primary_model)
            setattr(agent, "_grid_model_keys", model_keys)
            # Auto-run tools (beads_init, beads_ready, etc.) run only once per user/agent
            # in run_agent() when handling the first request — see _initialized_agents.

            self._agent_cache[cache_key] = agent

            return agent

        except Exception as e:
            error_msg = f"Failed to create agent '{agent_key}': {e}"
            raise AgentError(error_msg, details={"agent_key": agent_key}) from e


    # Dynamic agents (not declared in config.yaml)
    async def create_dynamic_agent(
        self,
        *,
        name: str,
        instructions: str,
        model_key: Optional[str] = None,
        tool_names: Optional[List[str]] = None,
        mcp_tool_names: Optional[List[str]] = None,
        init_tools: Optional[List[Dict[str, Any]]] = None,
        system_skills: Optional[List[str]] = None,
        action_state: Optional[Any] = None,
    ) -> Agent:
        """
        Create an ad-hoc Agent instance not backed by config.yaml.

        This is the core building block for orchestration/meta-agent patterns.
        ``action_state`` is the policy state the agent will run under; its
        ``init_tools`` are judged under it too, since an agent wrote them.
        """
        resolved_model_key = self.resolve_model_key(model_key)

        allowed_models = self.config.config.settings.allowed_models or []

        # Log every dynamic agent creation attempt
        logger.info(
            f"Creating dynamic agent: name={name}, model_key_requested={model_key}, "
            f"model_key_resolved={resolved_model_key}, allowed_models={allowed_models}"
        )

        # CRITICAL: Validate that the model is in the allowed models whitelist
        if not self.models.is_allowed(resolved_model_key):
            error_msg = (
                f"❌ MODEL VALIDATION FAILED ❌\n"
                f"Model '{resolved_model_key}' is not in the allowed models whitelist.\n"
                f"Allowed models: {allowed_models}\n"
                f"Agent requested: {name}\n"
                f"Instructions preview: {instructions[:100]}..."
            )
            logger.error(
                f"❌ BLOCKED: Agent '{name}' tried to use non-whitelisted model '{resolved_model_key}'",
                extra={
                    "model_key_requested": model_key,
                    "model_key_resolved": resolved_model_key,
                    "agent_name": name,
                    "allowed_models": allowed_models,
                    "validation_status": "FAILED",
                },
            )
            raise AgentError(
                error_msg,
                details={
                    "model_key": resolved_model_key,
                    "agent_name": name,
                    "allowed_models": allowed_models,
                },
            )

        # Log successful validation
        logger.info(
            f"✅ MODEL VALIDATION PASSED: Agent '{name}' using model '{resolved_model_key}'"
        )

        # The same model, client and flags create_agent gives a configured agent.
        model, model_cfg = self.models.sdk_model(resolved_model_key)

        tools: List[Any] = []
        mcp_servers_list: List[Any] = []
        effective_tool_names = tool_names or []

        # Gather all tool names (including MCP)
        all_tool_names = list(effective_tool_names)
        if mcp_tool_names:
            all_tool_names.extend(mcp_tool_names)

        if effective_tool_names:
            tools, inferred_mcp = await self._resolve_tools_for_names(
                effective_tool_names
            )
            if inferred_mcp:
                mcp_tool_names = list(
                    dict.fromkeys([*(mcp_tool_names or []), *inferred_mcp])
                )
                # Add inferred_mcp to all_tool_names, removing duplicates
                for mcp_name in inferred_mcp:
                    if mcp_name not in all_tool_names:
                        all_tool_names.append(mcp_name)

        if mcp_tool_names:
            # Only if enabled (globally or per caller)
            if self.config.is_mcp_enabled():
                mcp_servers_list = await self.mcp.for_tools(mcp_tool_names)

        # Add prompt_addition from tool configuration to instructions
        enhanced_instructions = self._build_dynamic_agent_instructions(
            instructions, all_tool_names
        )

        # Load system_skills and prepend to instructions
        if system_skills:
            for skill_name in system_skills:
                skill_content = self._load_system_skill(skill_name)
                if skill_content:
                    enhanced_instructions = f"## System Skill: {skill_name}\n\n{skill_content}\n\n{enhanced_instructions}"

        # Run init_tools and prepend results to instructions
        if init_tools:
            try:
                temp_ctx = GridRunContext(
                    factory=self,
                    context_id=self.get_active_context_id() or "init",
                    user_id=None,
                    agent_id=name,
                    container_id=getattr(self, "container_id", None),
                    action_state=action_state,
                )
                init_info = await self._execute_init_tools(init_tools, tools, temp_ctx)
                if init_info:
                    enhanced_instructions = init_info + "\n\n" + enhanced_instructions
                    logger.info(
                        f"init_tools: injected {len(init_tools)} context result(s) into '{name}'"
                    )
            except Exception as e:
                logger.warning(f"⚠️ init_tools failed for agent '{name}': {e}")

        # Log final tool configuration
        function_tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        logger.info(
            f"Dynamic agent '{name}' created with {len(tools)} function tools and {len(mcp_servers_list)} MCP servers"
        )
        verbose_logger.debug(
            f"\n{'='*80}\nDYNAMIC AGENT CREATED\n{'='*80}\n"
            f"Agent name: {name}\n"
            f"Model: {resolved_model_key}\n"
            f"Requested tool names: {effective_tool_names}\n"
            f"Resolved function tools: {function_tool_names}\n"
            f"MCP tool names: {mcp_tool_names}\n"
            f"MCP servers created: {len(mcp_servers_list)}\n"
            f"{'='*80}\n"
        )

        agent = Agent(
            name=name,
            instructions=enhanced_instructions,
            model=model,
            model_settings=self.models.settings(model_cfg),
            tools=tools,
            mcp_servers=mcp_servers_list,
        )
        setattr(agent, "_grid_model_key", resolved_model_key)
        return agent

    def _load_system_skill(self, skill_name: str) -> Optional[str]:
        """Load a system skill file, delegating to config."""
        return self.config._load_skill_file(skill_name)


    def _build_dynamic_agent_instructions(
        self, base_instructions: str, tool_names: List[str]
    ) -> str:
        """
        Build complete instructions for dynamic agent including tool prompt_additions.

        This mirrors the logic from Config.build_agent_prompt but for dynamic agents.
        """
        # Combine parts
        parts = [base_instructions]

        if not tool_names:
            return "\n\n".join(parts)

        # Collect prompt_addition from tool configuration
        tool_descriptions = []
        for tool_name in tool_names:
            try:
                tool_config = self.config.get_tool(tool_name)
                if tool_config.prompt_addition:
                    tool_descriptions.append(tool_config.prompt_addition)
            except Exception:
                # Ignore unknown tools (for compatibility)
                logger.debug(
                    f"Tool '{tool_name}' not found in config, skipping prompt_addition"
                )
                continue

        # If no tool descriptions, return base instructions + memory
        if not tool_descriptions:
            return "\n\n".join(parts)

        # General rules for tools (if set)
        common_rules = getattr(self.config.config.settings, "tools_common_rules", None)
        if common_rules:
            parts.append("\nRules for using tools (general):")
            parts.append(str(common_rules))

        # Add tool descriptions
        parts.append("\nAvailable tools:")
        parts.extend(tool_descriptions)

        return "\n\n".join(parts)


    # ------------------------------------------------------------------
    # Running agents
    # ------------------------------------------------------------------


    def _build_agent_instructions(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        include_conversation_context: bool = True,
    ) -> str:
        """The agent's instructions, as the instructions builder assembles them."""
        return self.instructions_builder.assemble_model_context(
            agent_key,
            context_path,
            include_conversation_context=include_conversation_context,
            include_path_context=True,
        ).instructions

    # ------------------------------------------------------------------
    # SDK exception helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tool output truncation
    # ------------------------------------------------------------------


    # Context management methods
    def add_to_context(
        self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add message to conversation context."""
        if metadata is None:
            metadata = {"context_id": self.context_manager.get_current_context_id()}
        self.context_manager.add_message(role, content, metadata=metadata)

    def clear_context(self) -> str:
        """Clear conversation context and start a new session."""
        return self.context_manager.clear_history()

    def get_active_context_id(self) -> Optional[str]:
        """Return the current active context identifier."""
        return self.context_manager.get_current_context_id()

    def activate_context(self, context_id: str) -> str:
        """Activate a specific context session by identifier."""
        return self.context_manager.activate_context(context_id)

    def list_context_ids(self) -> List[str]:
        """Return the list of known context identifiers."""
        return self.context_manager.list_context_ids()

    def get_context_info(self) -> Dict[str, Any]:
        """Get context information."""
        return self.context_manager.get_context_stats()

    def get_recent_executions(self, limit: int = 3) -> List[Any]:
        """Get recent executions from context manager."""
        return self.context_manager.get_recent_executions(limit=limit)

    # Cache management
    def clear_cache(self) -> None:
        """Clear all caches."""
        self._agent_cache.clear()
        self._tool_cache.clear()

    def forget_agent(self, agent_key: str) -> None:
        """Drop the built agent *agent_key*: its next use builds it from the
        config as it is now. A run already using it is not affected."""
        self._agent_cache.pop(agent_key, None)
        self._logged_agents.discard(agent_key)

    async def cleanup(self) -> None:
        """Cleanup resources."""
        gate = getattr(self, "action_gate", None)
        if gate is not None:
            await gate.aclose()
        await self.mcp.close()

        # Clear agent sessions
        await self._runtime_support.cleanup_sessions()

        # Kill stale dolt server started by beads inside the container (network_mode=host
        # makes container ports visible on the host, so orphaned dolt processes persist).
        if self.container_id:
            try:
                import subprocess

                subprocess.run(
                    ["docker", "exec", self.container_id, "pkill", "-f", "dolt"],
                    capture_output=True,
                    timeout=5,
                )
                logger.debug("Sent pkill dolt to container %s", self.container_id[:12])
            except Exception as e:
                logger.debug("Could not pkill dolt in container: %s", e)

        # Clear caches
        self.clear_cache()
        self._agent_sessions.clear()

