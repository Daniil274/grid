"""
Function tools for Grid agents - integration layer for file and git tools.
"""

from typing import List, Any, Dict
from .file_tools import FILE_TOOLS, get_file_tools, get_file_tools_by_names
from .git_tools import GIT_TOOLS, get_git_tools, get_git_tools_by_names
from .orchestrator_tools import ORCHESTRATOR_TOOLS
from .ocr_tools import OCR_TOOLS
from .markdown_tools import MARKDOWN_TOOLS
from .memory_tools_v2 import MEMORY_TOOLS_V2  # SQLite-based memory tools
from .skill_tools import SKILL_TOOLS  # New file-based skill tools
from .vision_tools import VISION_TOOLS  # Vision tools for image viewing
from .document_tools import DOCUMENT_TOOLS  # Document conversion and export tools
from .beads_tools import BEADS_TOOLS  # Beads issue tracker tools
from .input_tools import INPUT_TOOLS  # Keyboard and input tools
from .screen_tools import SCREEN_TOOLS  # Screen capture tools
from .emergency_tools import EMERGENCY_TOOLS  # Emergency shutdown and pipeline status tools
from .search_tools import SEARCH_TOOLS  # Discovery tools: search_tools, search_skills
from .semantic_tools import SEMANTIC_TOOLS  # Semantic search: semantic_search_code, index_codebase
from .history_tools import HISTORY_TOOLS  # Claude.ai chat history search
from .semantic_memory_tools import SEMANTIC_MEMORY_TOOLS  # Hybrid semantic memory search

# ============================================================================
# COMBINED TOOLS REGISTRY
# ============================================================================

# Импортируем мок инструменты для тестов
try:
    from tests.mock_tools import MOCK_TOOLS
    HAS_MOCK_TOOLS = True
except ImportError:
    MOCK_TOOLS = {}
    HAS_MOCK_TOOLS = False

# Объединяем все инструменты
AVAILABLE_TOOLS = {
    **FILE_TOOLS,
    **GIT_TOOLS,
    **ORCHESTRATOR_TOOLS,
    **OCR_TOOLS,
    **MARKDOWN_TOOLS,
    **MEMORY_TOOLS_V2,  # New SQLite-based memory tools (3 tools)
    **SKILL_TOOLS,  # SQL-only skill tools (5 tools)
    **VISION_TOOLS,  # Vision tools for image viewing (2 tools)
    **DOCUMENT_TOOLS,  # Document conversion and export tools (4 tools)
    **BEADS_TOOLS,  # Beads issue tracker tools (7 tools)
    **INPUT_TOOLS,  # Keyboard and input tools (3 tools)
    **SCREEN_TOOLS,  # Screen capture tools (1 tool)
    **EMERGENCY_TOOLS,  # Emergency shutdown and pipeline status tools (2 tools)
    **SEARCH_TOOLS,  # Discovery tools: search_tools, search_skills (2 tools)
    **SEMANTIC_TOOLS,  # Semantic search tools: semantic_search_code, index_codebase (2 tools)
    **HISTORY_TOOLS,  # Claude.ai chat history search (1 tool)
    **SEMANTIC_MEMORY_TOOLS,  # Hybrid semantic memory search (1 tool)
    **MOCK_TOOLS,  # Добавляем мок инструменты
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
    "append_to_file": "file_append",
    
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

    # Orchestration
    "orchestrate": "orchestrate",

    # Memory operations V2 (new SQLite-based)
    "memory_save": "memory_save",
    "memory_search": "memory_search",
    "memory_delete": "memory_delete",
    "task_update": "task_update",

    # Memory aliases (old names -> SQLite tools)
    "save_memory": "memory_save",
    "recall_memory": "memory_search",

    # Document conversion and export operations
    "markdown_to_html": "markdown_to_html",
    "markdown_to_pdf": "markdown_to_pdf",
    "save_report": "save_report",
    "merge_reports": "merge_reports",

    # OCR and document processing
    "pdf": "pdf",
    "pdf-ocr": "pdf-ocr",
    "pdf_ocr": "pdf-ocr",
    "pdf_to_markdown": "pdf_to_markdown",
    "read_markdown": "read_markdown",

    # Input and screen tools
    "keyboard_type": "keyboard_type",
    "keyboard_press": "keyboard_press",
    "keyboard_hotkey": "keyboard_hotkey",
    "take_screenshot": "take_screenshot",
    "crop_image": "crop_image",
}

def get_tools_by_names(tool_names: List[str]) -> List[Any]:
    """
    Возвращает список инструментов по их именам.
    Поддерживает загрузку из:
    1. Проектных инструментов (если инициализирован project_tools_loader)
    2. Базовых системных инструментов
    3. Алиасов

    Args:
        tool_names: Список имен инструментов

    Returns:
        List[Any]: Список функций инструментов
    """
    from core.managers.project_tools_loader import get_project_loader

    tools = []
    project_loader = get_project_loader()

    for name in tool_names:
        # 1. Проверяем проектные инструменты (приоритет!)
        if project_loader and project_loader.has_tool(name):
            tool = project_loader.get_tool(name)
            if tool:
                tools.append(tool)
                continue

        # 2. Проверяем прямое совпадение в системных инструментах
        if name in AVAILABLE_TOOLS:
            tools.append(AVAILABLE_TOOLS[name])
        # 3. Проверяем алиасы
        elif name in TOOL_ALIASES:
            actual_name = TOOL_ALIASES[name]
            if actual_name in AVAILABLE_TOOLS:
                tools.append(AVAILABLE_TOOLS[actual_name])
            else:
                from utils.logger import Logger
                Logger(__name__).warning(f"Инструмент '{actual_name}' (алиас для '{name}') не найден")
        # 4. Попробуем найти в отдельных модулях
        else:
            if name.startswith('file_') or name in ['read_file', 'write_file', 'list_files', 'get_file_info', 'search_files', 'edit_file_patch', 'append_to_file']:
                file_tools = get_file_tools_by_names([name])
                tools.extend(file_tools)
            elif name.startswith('git_') or name in ['git_status', 'git_log', 'git_diff', 'git_branch_list', 'git_add_file', 'git_commit', 'git_checkout_branch', 'git_pull', 'git_remote_info']:
                git_tools = get_git_tools_by_names([name])
                tools.extend(git_tools)
            else:
                from utils.logger import Logger
                Logger(__name__).warning(f"Инструмент '{name}' не найден ни в проектных, ни в системных инструментах")

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
        "type": "file" if actual_name.startswith("file_") else "git" if actual_name.startswith("git_") else "ape" if actual_name == "automatic_prompt_engineer" else "other"
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