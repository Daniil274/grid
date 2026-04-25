"""
Structured model-context assembly for agents.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from core.config.prompt_sections import ModelContextAssembly, PromptSection
from core.config.protocols import IConfig, IContextManager

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
    ) -> None:
        self.config = config
        self.context_manager = context_manager
        self.container_id = container_id

    def assemble_model_context(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        *,
        include_conversation_context: bool = True,
        include_path_context: bool = True,
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

        context_reference = self._build_context_reference(context_id)
        if context_reference:
            sections.append(
                PromptSection(
                    key="context_reference",
                    content=context_reference,
                    scope="dynamic",
                )
            )

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

        incomplete_run_summary = ""
        get_incomplete_run_summary = getattr(
            self.context_manager,
            "get_incomplete_run_summary",
            None,
        )
        if callable(get_incomplete_run_summary):
            try:
                incomplete_run_summary = get_incomplete_run_summary()
            except Exception:
                logger.debug("Failed to get incomplete run summary", exc_info=True)

        if incomplete_run_summary:
            sections.append(
                PromptSection(
                    key="incomplete_run_summary",
                    content=incomplete_run_summary,
                    scope="dynamic",
                )
            )

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
        working_dir = "/" if self.container_id else self.config.get_working_directory()
        config_dir = self.config.get_config_directory()

        context_parts = [
            "Path information:",
            f"Working directory: {working_dir}",
        ]

        if not self.container_id:
            context_parts.append(f"Configuration directory: {config_dir}")

        if context_path:
            if self.container_id:
                if os.path.isabs(context_path):
                    host_wd = self.config.get_working_directory()
                    if context_path.startswith(host_wd):
                        rel_path = os.path.relpath(context_path, host_wd)
                        absolute_path = (Path("/") / rel_path).as_posix()
                        context_path = rel_path
                    else:
                        absolute_path = context_path.lstrip("/") or "/"
                        context_path = absolute_path
                else:
                    absolute_path = (Path("/") / context_path).as_posix()
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

    def _build_context_reference(self, context_id: Optional[str]) -> Optional[str]:
        if not context_id:
            return None
        return (
            f"Context reference: {context_id}. "
            f'Always append the line "Context ID: {context_id}" '
            "to every reply so humans or agents can return to this dialogue via that identifier."
        )

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
