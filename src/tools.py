"""
Система инструментов для агентов.

Поддерживает:
- Файловые операции (чтение, запись, поиск)
- Git операции с поддержкой кириллицы в именах авторов и веток
- Web поиск
- MCP интеграцию
"""

import json
from typing import List, Any
from agents import function_tool

# Импортируем инструменты из отдельных модулей
from file_tools import (
    read_file, write_file, list_files, get_file_info, search_files, edit_file_patch,
    FILE_TOOLS, get_file_tools, get_file_tools_by_names
)

from git_tools import (
    git_status, git_log, git_diff, git_branch_list, git_add_file,
    git_commit, git_checkout_branch, git_pull, git_remote_info,
    GIT_TOOLS, get_git_tools, get_git_tools_by_names
)

# ============================================================================
# WEB ПОИСК
# ============================================================================

@function_tool
def web_search(query: str) -> str:
    """Поиск информации в интернете."""
    return f"Поиск '{query}' временно недоступен. Нужен API ключ для поисковой системы."

# ============================================================================
# СЛОВАРЬ ВСЕХ ДОСТУПНЫХ ИНСТРУМЕНТОВ
# ============================================================================

AVAILABLE_TOOLS = {
    # Файловые операции
    **FILE_TOOLS,
    
    # Git операции
    **GIT_TOOLS,
    
    # Web поиск
    "web_search": web_search,
}

def get_tools_by_names(tool_names: List[str]) -> List[Any]:
    """Возвращает список инструментов по их именам."""
    tools = []
    for name in tool_names:
        if name in AVAILABLE_TOOLS:
            tools.append(AVAILABLE_TOOLS[name])
        else:
            print(f"⚠️  Инструмент '{name}' не найден")
    return tools

def get_all_tools() -> List[Any]:
    """Возвращает список всех доступных инструментов."""
    return list(AVAILABLE_TOOLS.values())

# Экспортируем функции для обратной совместимости
__all__ = [
    # Файловые инструменты
    'read_file', 'write_file', 'list_files', 'get_file_info', 'search_files', 'edit_file_patch',
    'FILE_TOOLS', 'get_file_tools', 'get_file_tools_by_names',
    
    # Git инструменты
    'git_status', 'git_log', 'git_diff', 'git_branch_list', 'git_add_file',
    'git_commit', 'git_checkout_branch', 'git_pull', 'git_remote_info',
    'GIT_TOOLS', 'get_git_tools', 'get_git_tools_by_names',
    
    # Web поиск
    'web_search',
    
    # Общие функции
    'AVAILABLE_TOOLS', 'get_tools_by_names', 'get_all_tools'
]