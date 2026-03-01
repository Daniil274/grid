"""
Tools and utilities for Grid agents.
"""

from .function_tools import get_tools_by_names, get_all_tools, AVAILABLE_TOOLS
from .file_tools import *
from .git_tools import *
from .markdown_tools import *
from .orchestrator_tools import *
from .voice_tools import send_voice_reply, send_voice_ssml
from .input_tools import keyboard_type, keyboard_press, keyboard_hotkey
from .screen_tools import take_screenshot

__all__ = [
    "get_tools_by_names",
    "get_all_tools",
    "AVAILABLE_TOOLS",
    # Voice tools
    "send_voice_reply",
    "send_voice_ssml",
    # Input tools
    "keyboard_type", "keyboard_press", "keyboard_hotkey",
    # Screen tools
    "take_screenshot",
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
]