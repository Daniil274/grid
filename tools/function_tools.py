"""
Function tools for Grid agents - integration layer for file and git tools.

Tool modules are discovered automatically from the tools/ directory.
Each *_tools.py file that exports a dict named *_TOOLS is registered.
Modules are imported lazily — only when a specific tool is first requested.
"""

import importlib
import pkgutil
import sys
from pathlib import Path
from typing import List, Any, Dict
from .file_tools import FILE_TOOLS, get_file_tools
from .git_tools import GIT_TOOLS, get_git_tools

# ============================================================================
# AUTO-DISCOVERY: find all *_tools.py in this package (tools/)
# Maps module_name -> list of *_TOOLS dict names to try
# ============================================================================

def _discover_tool_modules() -> Dict[str, str]:
    """
    Scan the tools/ package for *_tools.py files.
    Returns {dotted_module_path: dict_attr_name} for each candidate.
    Skips file_tools and git_tools (already loaded eagerly above).
    """
    skip = {"tools.file_tools", "tools.git_tools", "tools.function_tools"}
    result = {}
    tools_path = Path(__file__).parent
    for finder, mod_name, _ in pkgutil.iter_modules([str(tools_path)]):
        if not mod_name.endswith("_tools"):
            continue
        full_name = f"tools.{mod_name}"
        if full_name in skip:
            continue
        # Convention: FOO_tools.py → FOO_TOOLS dict
        dict_attr = mod_name.upper()
        result[full_name] = dict_attr
    return result


_MODULE_MAP: Dict[str, str] = _discover_tool_modules()  # {mod_path: dict_attr}
_loaded_modules: Dict[str, Dict] = {}  # {mod_path: tool_dict}
_all_loaded = False


def _load_module(mod_path: str) -> Dict:
    if mod_path not in _loaded_modules:
        dict_attr = _MODULE_MAP[mod_path]
        try:
            mod = importlib.import_module(mod_path)
            _loaded_modules[mod_path] = getattr(mod, dict_attr, {})
        except Exception:
            _loaded_modules[mod_path] = {}
    return _loaded_modules[mod_path]


def _load_all_modules() -> None:
    global _all_loaded
    if not _all_loaded:
        for mod_path in _MODULE_MAP:
            _load_module(mod_path)
        try:
            from tests.mock_tools import MOCK_TOOLS
            _loaded_modules["tests.mock_tools"] = MOCK_TOOLS
        except ImportError:
            pass
        _all_loaded = True


class _LazyToolsDict:
    """Dict-like proxy that auto-discovers and loads tool modules on demand."""

    def __contains__(self, name: str) -> bool:
        if name in FILE_TOOLS or name in GIT_TOOLS:
            return True
        for mod_path in _MODULE_MAP:
            if name in _load_module(mod_path):
                return True
        return False

    def __getitem__(self, name: str) -> Any:
        if name in FILE_TOOLS:
            return FILE_TOOLS[name]
        if name in GIT_TOOLS:
            return GIT_TOOLS[name]
        for mod_path in _MODULE_MAP:
            mod = _load_module(mod_path)
            if name in mod:
                return mod[name]
        raise KeyError(name)

    def keys(self):
        _load_all_modules()
        seen = set()
        for d in [FILE_TOOLS, GIT_TOOLS] + list(_loaded_modules.values()):
            for k in d:
                if k not in seen:
                    seen.add(k)
                    yield k

    def values(self):
        for k in self.keys():
            yield self[k]

    def items(self):
        for k in self.keys():
            yield k, self[k]

    def get(self, name: str, default=None):
        try:
            return self[name]
        except KeyError:
            return default


AVAILABLE_TOOLS = _LazyToolsDict()

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

    # System introspection tools
    "list_agents": "system_list_agents",
    "get_agent_info": "system_get_agent_info",
    "list_tools": "system_list_tools",
    "get_tool_info": "system_get_tool_info",
    "get_skills": "system_get_skills",
    "help": "system_help",
    "get_context": "system_get_context",

    # Evolution tools
    "improvement_create_problem": "create_improvement_problem",
    "improvement_list_problems": "list_improvement_problems",
    "improvement_create_experiment": "create_improvement_experiment",
    "improvement_propose_config": "propose_config_experiment",
    "improvement_evaluate": "evaluate_improvement_experiment",
    "improvement_review_requirements": "record_requirement_review",
    "improvement_review_final": "record_final_review",
    "improvement_promote": "promote_improvement_experiment",
    "improvement_reject": "reject_improvement_experiment",
    "improvement_canary": "run_improvement_canary",
    "improvement_monitor": "monitor_promoted_improvement",

    # System platform tools
    "list_systems": "system_list_systems",
    "get_system_info": "system_get_system_info",
    "get_system_versions": "system_get_system_versions",
    "invoke_system": "system_invoke_system",
    "create_system_version": "system_create_version",
    "clone_system_version": "system_clone_version",
    "mutate_system_version": "system_apply_mutations",
    "promote_system_version": "system_promote_version",
    "reject_system_version": "system_reject_version",
    "rollback_system_stable": "system_rollback_stable",
    "build_system_bundle": "system_build_bundle",
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

        # 2. Разрешаем алиас (до обхода lazy-модулей, чтобы не грузить лишнее)
        lookup_name = TOOL_ALIASES.get(name, name)

        # 3. Ищем в системных инструментах
        tool = AVAILABLE_TOOLS.get(lookup_name)
        if tool is not None:
            tools.append(tool)
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
