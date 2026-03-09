"""
Instructions Builder for agent instructions.

Builds complete instructions for agents by combining:
- Base prompts from configuration
- Path context information
- Conversation context
- Context identifiers
"""

import logging
from typing import Optional
from core.protocols import IConfig, IContextManager

logger = logging.getLogger("grid.instructions_builder")


class InstructionsBuilder:
    """
    Builds instructions for agents with context and configuration.

    This class is responsible for constructing the complete
    instruction text that agents receive, including:
    - Base prompts from configuration
    - Working directory and path information
    - Conversation history (if requested)
    - Context identifiers for resuming conversations
    """

    def __init__(
        self,
        config: IConfig,
        context_manager: IContextManager,
        container_id: Optional[str] = None,
    ) -> None:
        """
        Initialize InstructionsBuilder.

        Args:
            config: Configuration instance
            context_manager: Context manager instance
            container_id: Optional Docker container ID
        """
        self.config = config
        self.context_manager = context_manager
        self.container_id = container_id

    def build_agent_instructions(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        include_conversation_context: bool = True
    ) -> str:
        """
        Build complete agent instructions with context.

        Args:
            agent_key: Agent configuration key
            context_path: Optional context path for agent
            include_conversation_context: Whether to include conversation history

        Returns:
            Complete instructions string with all context information

        Example:
            >>> builder = InstructionsBuilder(config, context_manager)
            >>> instructions = builder.build_agent_instructions(
            ...     "my_agent",
            ...     context_path="/path/to/project",
            ...     include_conversation_context=True
            ... )
        """
        # Get base instructions from configuration
        base_instructions = self.config.build_agent_prompt(agent_key)

        # Build path context
        path_context = self.build_path_context(context_path)

        # Start building parts
        parts = [base_instructions]

        if path_context:
            parts.append(path_context)

        # Add context identifier instructions
        context_identifier = self.context_manager.get_current_context_id()
        if context_identifier:
            context_instruction = (
                f"Context reference: {context_identifier}. "
                f"Always append the line \"Контекст ID: {context_identifier}\" "
                "to every reply so humans or agents can return to this dialogue via that identifier."
            )
            parts.append(context_instruction)

        # Add conversation context if requested
        if include_conversation_context:
            conversation_context = self.context_manager.get_conversation_context()
            if conversation_context:
                parts.append(conversation_context)
                logger.debug(
                    "Including conversation context in instructions",
                    extra={
                        "agent_key": agent_key,
                        "context_id": context_identifier,
                        "context_length": len(conversation_context),
                    },
                )

        instructions = "\n\n".join(parts)

        logger.debug(
            "Built agent instructions",
            extra={
                "agent_key": agent_key,
                "context_path": context_path,
                "include_conversation_context": include_conversation_context,
                "instructions_length": len(instructions),
            },
        )

        return instructions

    def build_path_context(self, context_path: Optional[str] = None) -> str:
        """
        Build path context information.

        This creates a structured description of the working directories
        and paths that the agent should use for file operations.

        Args:
            context_path: Optional context path to include

        Returns:
            Formatted path context string

        Example:
            >>> builder = InstructionsBuilder(config, context_manager)
            >>> path_context = builder.build_path_context("/path/to/project")
            >>> print(path_context)
            Информация о путях:
            Рабочая директория: /workspace
            Контекстный путь: project
            Абсолютный контекстный путь: /workspace/project

            Используй эти пути для работы с файлами и директориями.
        """
        # In container mode the agent sees "/" as its root.
        working_dir = "/" if self.container_id else self.config.get_working_directory()
        config_dir = self.config.get_config_directory()

        context_parts = [
            "Информация о путях:",
            f"Рабочая директория: {working_dir}",
        ]
        
        # Only add config dir if not in container
        if not self.container_id:
            context_parts.append(f"Директория конфигурации: {config_dir}")

        if context_path:
            # If in container, we try to make the path relative to the workspace
            if self.container_id:
                import os
                from pathlib import Path
                # If it's already relative, keep it. If absolute host path, try to convert.
                if os.path.isabs(context_path):
                    host_wd = self.config.get_working_directory()
                    if context_path.startswith(host_wd):
                        rel_path = os.path.relpath(context_path, host_wd)
                        absolute_path = (Path("/") / rel_path).as_posix()
                        context_path = rel_path
                    else:
                        # Outside host workspace — strip leading slash to avoid leaking container paths
                        absolute_path = context_path.lstrip("/") or "/"
                        context_path = absolute_path
                else:
                    absolute_path = (Path("/") / context_path).as_posix()
            else:
                # Get absolute path if available (some configs may implement this)
                absolute_path = context_path
                if hasattr(self.config, 'get_absolute_path'):
                    try:
                        absolute_path = self.config.get_absolute_path(context_path)
                    except Exception as e:
                        logger.debug(
                            "Failed to get absolute path",
                            extra={"context_path": context_path, "error": str(e)},
                        )

            context_parts.extend([
                f"Контекстный путь: {context_path}",
                f"Абсолютный контекстный путь: {absolute_path}"
            ])

        context_parts.extend([
            "",
            "Используй эти пути для работы с файлами и директориями."
        ])

        return "\n".join(context_parts)
