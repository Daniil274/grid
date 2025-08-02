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
    import time
    from utils.logger import log_tool_start, log_tool_end, log_tool_error, log_tool_usage
    
    start_time = time.time()
    args = {"query": query}
    log_tool_start("web_search", args)
    
    try:
        result = f"Поиск '{query}' временно недоступен. Нужен API ключ для поисковой системы."
        duration = time.time() - start_time
        log_tool_end("web_search", result, duration)
        log_tool_usage("web_search", args, True, duration)
        return result
    except Exception as e:
        log_tool_error("web_search", e)
        result = f"ОШИБКА при поиске: {str(e)}"
        duration = time.time() - start_time
        log_tool_end("web_search", result, duration)
        log_tool_usage("web_search", args, False, duration)
        return result

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