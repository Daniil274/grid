"""
Structured model-context assembly for agents.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

from core.config.prompt_sections import ModelContextAssembly, PromptSection
from core.config.protocols import IConfig, IContextManager
from utils.path_utils import CONTAINER_WORKDIR

logger = logging.getLogger("grid.instructions_builder")


class InstructionsBuilder:
    """
    Build agent instructions from structured prompt sections.

    The context store remains the source of truth, while this builder controls
    how that state is projected into model-facing instructions.
    """

    def __init__(
        self,
        config: IConfig,
        context_manager: IContextManager,
        container_id: Optional[str] = None,
        *,
        memory_manager=None,
    ) -> None:
        self.config = config
        self.context_manager = context_manager
        self.container_id = container_id
        self.memory_manager = memory_manager
        self._explicit_memory_manager = memory_manager is not None
        self.memory_manager_provider = None
        #: Where the agent's tools act when the chat works on a device: a callable
        #: returning {"name", "platform", "platform_label", "workdir"} or None
        #: (AgentFactory.execution_device, core.devices.execution).
        self.execution_target_info = None
        # Sections a deployment appends for every agent, right after the path
        # context. Nothing in core sets them; the web chat uses one to teach
        # the workspace-file link convention (web_chat/space.py).
        self.platform_sections: List[PromptSection] = []

    def assemble_model_context(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        *,
        include_conversation_context: bool = True,
        include_path_context: bool = True,
        memory_section: Optional[str] = None,
    ) -> ModelContextAssembly:
        """Assemble the full model-facing prompt context for an agent."""
        sections = list(self._get_base_sections(agent_key))
        context_id = self.context_manager.get_current_context_id()

        if include_path_context:
            path_context = self.build_path_context(context_path)
            if path_context:
                sections.append(
                    PromptSection(
                        key="path_context",
                        content=path_context,
                        scope="dynamic",
                    )
                )

        if self.platform_sections:
            sections.extend(self.platform_sections)

        try:
            settings = self.config.get_memory_config() if hasattr(self.config, "get_memory_config") else None
            explicit = self._explicit_memory_manager
            if explicit or (settings is not None and settings.enabled and settings.auto_inject):
                if self.memory_manager_provider is not None:
                    manager = self.memory_manager_provider()
                else:
                    if self.memory_manager is None:
                        self.memory_manager = self.config.create_memory_manager()
                    manager = self.memory_manager
                if memory_section is not None:
                    # Chosen for this turn's request by the factory (core.factory.memory).
                    content = memory_section
                elif manager is not None:
                    # No request to choose by: the user's profile only.
                    content = manager.render_prompt_section(
                        "", max_tokens=settings.auto_inject_tokens if settings is not None else 1000,
                    ).content
                else:
                    content = ""
                if content:
                    sections.append(PromptSection(key="agent_memory", content=content, scope="dynamic"))
        except Exception:
            logger.warning("Memory injection unavailable", exc_info=True)

        history_strategy = "none"
        if include_conversation_context:
            conversation_context = self.context_manager.get_conversation_context()
            if conversation_context:
                sections.append(
                    PromptSection(
                        key="conversation_context",
                        content=conversation_context,
                        scope="dynamic",
                    )
                )
                history_strategy = "prompt"

        instructions = "\n\n".join(
            section.content.strip()
            for section in sections
            if section.content and section.content.strip()
        )

        assembly = ModelContextAssembly(
            instructions=instructions,
            sections=sections,
            context_id=context_id,
            history_strategy=history_strategy,
            metadata={
                "agent_key": agent_key,
                "context_path": context_path,
                "include_conversation_context": include_conversation_context,
                "include_path_context": include_path_context,
            },
        )

        logger.debug(
            "Assembled model context",
            extra={
                "agent_key": agent_key,
                "context_id": context_id,
                "history_strategy": history_strategy,
                "section_count": len(sections),
                "instructions_length": len(instructions),
            },
        )
        return assembly

    def build_agent_instructions(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        include_conversation_context: bool = True,
    ) -> str:
        """Compatibility helper returning the joined instruction text."""
        return self.assemble_model_context(
            agent_key,
            context_path,
            include_conversation_context=include_conversation_context,
            include_path_context=True,
        ).instructions

    def build_path_context(self, context_path: Optional[str] = None) -> str:
        """Build path context information for the agent."""
        device = self._execution_device()
        if device is not None:
            return self._device_path_context(device, context_path)
        if self.container_id:
            # Not "/": an agent told its working directory is "/" asks for the
            # filesystem root, and the action policy rightly refuses that.
            context_parts = [
                "Path information:",
                f"Working directory: the workspace ({CONTAINER_WORKDIR} in the shell).",
                "File tools take paths relative to it: `.` is the workspace itself, "
                "`src/app.py` a file in it.",
            ]
        else:
            working_directory = self._config_value("get_working_directory")
            config_directory = self._config_value("get_config_directory")
            context_parts = [
                "Path information:",
                f"Working directory: {working_directory}",
            ]
            if config_directory:
                context_parts.append(f"Configuration directory: {config_directory}")

        if context_path:
            if self.container_id:
                host_wd = self._config_value("get_working_directory") or ""
                if os.path.isabs(context_path) and context_path.startswith(host_wd):
                    context_path = os.path.relpath(context_path, host_wd)
                else:
                    context_path = context_path.lstrip("/") or "."
                context_path = Path(context_path).as_posix()
                absolute_path = (Path(CONTAINER_WORKDIR) / context_path).as_posix()
            else:
                absolute_path = context_path
                if hasattr(self.config, "get_absolute_path"):
                    absolute_path = self.config.get_absolute_path(context_path)

            context_parts.extend(
                [
                    f"Context path: {context_path}",
                    f"Absolute context path: {absolute_path}",
                ]
            )

        context_parts.extend(
            [
                "",
                "Use these paths to work with files and directories.",
            ]
        )

        return "\n".join(context_parts)

    def _config_value(self, name: str) -> Optional[str]:
        """One path setting of the config, or what the process has when it is absent.

        The prompt describes where the work happens; a builder without a config
        - a preview, a test, a deployment that made its own builder - still has
        to say something true, and never fail a run over a missing setting.
        """
        getter = getattr(self.config, name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001 - a broken setting is not a broken run
                value = None
            if value:
                return str(value)
        if name == "get_working_directory":
            return str(Path.cwd())
        return None

    def _execution_device(self) -> Optional[dict[str, str]]:
        """The device the chat's tools act on, when there is one."""
        source = self.execution_target_info
        if source is None:
            return None
        try:
            info = source() if callable(source) else source
        except Exception:  # noqa: BLE001 - a prompt never fails a run
            logger.warning("Execution target unavailable for the prompt", exc_info=True)
            return None
        return info if isinstance(info, dict) and info.get("name") else None

    def _device_path_context(self, device: dict[str, str], context_path: Optional[str]) -> str:
        """The paths of a chat that works directly on the user's device.

        The work is not on the server: a relative path means a path on that
        machine, and the folder it means from is the device's, so the agent is
        told both plainly rather than being left to guess from the server's.
        """
        name = device.get("name", "")
        platform = device.get("platform_label") or device.get("platform") or "the device"
        workdir = device.get("workdir") or "(the device's own working folder)"
        windows = (device.get("platform") or "").lower() == "windows"
        parts = [
            "Path information:",
            f"You work directly on the user's device {name} ({platform}), not on the server.",
            f"Working folder on that device: {workdir}",
            "file_read, file_write, file_edit, file_append, glob_tool, grep_tool and "
            "bash_tool run on the device: `.` is that working folder, `src/app.py` is "
            "a file in it, and a relative working_dir of bash_tool is inside it too.",
        ]
        if windows:
            parts.append(
                "The device is Windows: paths may use backslashes (src\\app.py) and "
                "commands run in PowerShell."
            )
        else:
            parts.append(
                "The device is Linux or macOS: paths use forward slashes (src/app.py) "
                "and commands run in the shell."
            )
        parts.append(
            "A path outside the working folder is an action outside it: the user is "
            "asked to confirm it before it runs."
        )
        if context_path:
            parts.append(f"Context path: {context_path}")
        parts.extend(["", "Use these paths to work with files and directories."])
        return "\n".join(parts)

    def _get_base_sections(self, agent_key: str) -> list[PromptSection]:
        if hasattr(self.config, "build_agent_prompt_sections"):
            return list(self.config.build_agent_prompt_sections(agent_key))

        return [
            PromptSection(
                key="base_prompt",
                content=self.config.build_agent_prompt(agent_key),
                scope="static",
            )
        ]
