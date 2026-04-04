"""
Tools and utilities for Grid agents.
"""

from .function_tools import get_tools_by_names, get_all_tools, AVAILABLE_TOOLS
from .file_tools import *
from .git_tools import *
from .markdown_tools import *
from .orchestrator_tools import *
from .system_platform_tools import (
    system_list_systems,
    system_get_system_info,
    system_get_system_versions,
    system_invoke_system,
    system_create_version,
    system_clone_version,
    system_apply_mutations,
    system_promote_version,
    system_reject_version,
    system_rollback_stable,
)
from .system_builder_tools import system_build_bundle
from .input_tools import keyboard_type, keyboard_press, keyboard_hotkey
from .screen_tools import take_screenshot
from .emergency_tools import emergency_shutdown, get_pipeline_status

__all__ = [
    "get_tools_by_names",
    "get_all_tools",
    "AVAILABLE_TOOLS",
    # Input tools
    "keyboard_type", "keyboard_press", "keyboard_hotkey",
    # Screen tools
    "take_screenshot",
    # Emergency tools
    "emergency_shutdown", "get_pipeline_status",
    # File tools
    "read_file", "write_file", "list_files", "get_file_info", "search_files", "edit_file_patch", "append_to_file",
    # Git tools - основные операции
    "git_status", "git_log", "git_diff", "git_branch_list", "git_add_file", "git_add_all",
    "git_commit", "git_checkout_branch",
    # Git tools - инициализация и настройка
    "git_init", "git_config", "git_clone",
    # Git tools - удаленные репозитории
    "git_remote_info", "git_remote_add", "git_remote_remove", "git_fetch", "git_pull", "git_push",
    # Git tools - управление ветками и слияние
    "git_merge", "git_reset", "git_stash",
    # Git tools - теги
    "git_tag", "git_tag_list",
    # Markdown tools
    "read_markdown",
    # Orchestration tools
    "orchestrate",
    # System platform tools
    "system_list_systems", "system_get_system_info", "system_get_system_versions", "system_invoke_system",
    "system_create_version", "system_clone_version", "system_apply_mutations",
    "system_promote_version", "system_reject_version", "system_rollback_stable",
    "system_build_bundle",
]
