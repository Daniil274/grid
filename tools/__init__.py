"""
Tools and utilities for Grid agents.
"""

from .function_tools import get_tools_by_names, get_all_tools, AVAILABLE_TOOLS
from .file_tools import *
from .git_tools import *

# Import semantic tools if available
try:
    from .semantic_tools import get_semantic_tools, get_semantic_tools_by_names, semantic_search_code, index_codebase
    SEMANTIC_TOOLS_AVAILABLE = True
except ImportError:
    SEMANTIC_TOOLS_AVAILABLE = False

__all__ = [
    "get_tools_by_names",
    "get_all_tools",
    "AVAILABLE_TOOLS",
    # File tools
    "read_file", "write_file", "list_files", "get_file_info", "search_files", "edit_file_patch",
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
]

# Add semantic tools to exports if available
if SEMANTIC_TOOLS_AVAILABLE:
    __all__.extend([
        "get_semantic_tools",
        "get_semantic_tools_by_names",
        "semantic_search_code",
        "index_codebase",
    ])