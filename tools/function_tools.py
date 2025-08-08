"""
Function tools for Grid agents - integration layer for file and git tools.
"""

from typing import List, Any, Dict
from .file_tools import FILE_TOOLS, get_file_tools, get_file_tools_by_names
from .git_tools import GIT_TOOLS, get_git_tools, get_git_tools_by_names

# ============================================================================
# COMBINED TOOLS REGISTRY
# ============================================================================

# Объединяем все инструменты
AVAILABLE_TOOLS = {
    **FILE_TOOLS,
    **GIT_TOOLS,
}

# Добавляем дополнительные инструменты для совместимости
TOOL_ALIASES = {
    # File operations
    "read_file": "file_read",
    "write_file": "file_write", 
    "list_files": "file_list",
    "get_file_info": "file_info",
    "search_files": "file_search",
    "edit_file_patch": "file_edit_patch",
    
    # Git operations - основные
    "git_status": "git_status",
    "git_log": "git_log",
    "git_diff": "git_diff",
    "git_branch_list": "git_branch_list",
    "git_add_file": "git_add_file",
    "git_add_all": "git_add_all",
    "git_commit": "git_commit",
    "git_checkout_branch": "git_checkout_branch",
    
    # Git operations - инициализация и настройка
    "git_init": "git_init",
    "git_config": "git_config",
    "git_clone": "git_clone",
    
    # Git operations - удаленные репозитории
    "git_remote_info": "git_remote_info",
    "git_remote_add": "git_remote_add",
    "git_remote_remove": "git_remote_remove",
    "git_fetch": "git_fetch",
    "git_pull": "git_pull",
    "git_push": "git_push",
    
    # Git operations - управление ветками и слияние
    "git_merge": "git_merge",
    "git_reset": "git_reset",
    "git_stash": "git_stash",
    
    # Git operations - теги
    "git_tag": "git_tag",
    "git_tag_list": "git_tag_list",
}

def get_tools_by_names(tool_names: List[str]) -> List[Any]:
    """
    Возвращает список инструментов по их именам.
    
    Args:
        tool_names: Список имен инструментов
        
    Returns:
        List[Any]: Список функций инструментов
    """
    tools = []
    
    for name in tool_names:
        # Проверяем прямое совпадение
        if name in AVAILABLE_TOOLS:
            tools.append(AVAILABLE_TOOLS[name])
        # Проверяем алиасы
        elif name in TOOL_ALIASES:
            actual_name = TOOL_ALIASES[name]
            if actual_name in AVAILABLE_TOOLS:
                tools.append(AVAILABLE_TOOLS[actual_name])
            else:
                from utils.logger import Logger
                Logger(__name__).warning(f"Инструмент '{actual_name}' (алиас для '{name}') не найден")
        else:
            # Попробуем найти в отдельных модулях
            if name.startswith('file_') or name in ['read_file', 'write_file', 'list_files', 'get_file_info', 'search_files', 'edit_file_patch']:
                file_tools = get_file_tools_by_names([name])
                tools.extend(file_tools)
            elif name.startswith('git_') or name in ['git_status', 'git_log', 'git_diff', 'git_branch_list', 'git_add_file', 'git_commit', 'git_checkout_branch', 'git_pull', 'git_remote_info']:
                git_tools = get_git_tools_by_names([name])
                tools.extend(git_tools)
            else:
                from utils.logger import Logger
                Logger(__name__).warning(f"Инструмент '{name}' не найден")
    
    return tools

def get_all_tools() -> List[Any]:
    """
    Возвращает все доступные инструменты.
    
    Returns:
        List[Any]: Список всех функций инструментов
    """
    return list(AVAILABLE_TOOLS.values())

def get_file_tools_list() -> List[Any]:
    """Возвращает только файловые инструменты."""
    return get_file_tools()

def get_git_tools_list() -> List[Any]:
    """Возвращает только Git инструменты."""
    return get_git_tools()

def get_available_tool_names() -> List[str]:
    """
    Возвращает список имен всех доступных инструментов.
    
    Returns:
        List[str]: Список имен инструментов
    """
    return list(AVAILABLE_TOOLS.keys()) + list(TOOL_ALIASES.keys())

def get_tool_info(tool_name: str) -> Dict[str, Any]:
    """
    Возвращает информацию об инструменте.
    
    Args:
        tool_name: Имя инструмента
        
    Returns:
        Dict[str, Any]: Информация об инструменте
    """
    # Получаем реальное имя через алиас если нужно
    actual_name = TOOL_ALIASES.get(tool_name, tool_name)
    
    if actual_name not in AVAILABLE_TOOLS:
        return {"error": f"Инструмент '{tool_name}' не найден"}
    
    tool_func = AVAILABLE_TOOLS[actual_name]
    
    return {
        "name": actual_name,
        "alias": tool_name if tool_name != actual_name else None,
        "description": tool_func.__doc__ or "Описание не доступно",
        "module": tool_func.__module__,
        "type": "file" if actual_name.startswith("file_") else "git" if actual_name.startswith("git_") else "other"
    }

# ============================================================================
# BACKWARDS COMPATIBILITY
# ============================================================================

# Экспортируем основные функции для обратной совместимости
from .file_tools import read_file, write_file, list_files, get_file_info, search_files, edit_file_patch

# Если git_tools.py экспортирует функции напрямую, добавим их
try:
    from .git_tools import (
        # Основные операции
        git_status, git_log, git_diff, git_branch_list, git_add_file, git_add_all,
        git_commit, git_checkout_branch,
        # Инициализация и настройка
        git_init, git_config, git_clone,
        # Удаленные репозитории
        git_remote_info, git_remote_add, git_remote_remove, git_fetch, git_pull, git_push,
        # Управление ветками и слияние
        git_merge, git_reset, git_stash,
        # Теги
        git_tag, git_tag_list
    )
except ImportError:
    # Git инструменты могут быть не готовы
    pass

# ============================================================================
# TOOL STATISTICS AND MONITORING  
# ============================================================================

def get_tool_stats() -> Dict[str, Any]:
    """
    Возвращает статистику по инструментам.
    
    Returns:
        Dict[str, Any]: Статистика инструментов
    """
    file_tools_count = len([name for name in AVAILABLE_TOOLS.keys() if name.startswith('file_')])
    git_tools_count = len([name for name in AVAILABLE_TOOLS.keys() if name.startswith('git_')])
    
    return {
        "total_tools": len(AVAILABLE_TOOLS),
        "file_tools": file_tools_count,
        "git_tools": git_tools_count,
        "aliases": len(TOOL_ALIASES),
        "available_names": get_available_tool_names()
    }

# Информация о модуле
__version__ = "2.0.0"
__description__ = "Enhanced Grid Agent Tools with beautiful logging"