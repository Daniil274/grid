"""
Function tools for Grid agents - registry of the shared tools in tools/.

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
from .git_tools import GIT_TOOLS, TOOL_REQUIREMENTS as _GIT_REQUIREMENTS, get_git_tools

_eager_requirements: Dict[str, Any] = dict(_GIT_REQUIREMENTS)

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
_load_errors: Dict[str, str] = {}  # {mod_path: "ErrorType: message"}
_requirements: Dict[str, Any] = {}  # {tool_name: Requires}
_all_loaded = False


def _load_module(mod_path: str) -> Dict:
    if mod_path not in _loaded_modules:
        dict_attr = _MODULE_MAP[mod_path]
        try:
            mod = importlib.import_module(mod_path)
            _loaded_modules[mod_path] = getattr(mod, dict_attr, {})
            _requirements.update(getattr(mod, "TOOL_REQUIREMENTS", None) or {})
        except Exception as exc:
            _loaded_modules[mod_path] = {}
            _load_errors[mod_path] = f"{type(exc).__name__}: {exc}"
    return _loaded_modules[mod_path]


def load_errors() -> Dict[str, str]:
    """Shared tool modules that failed to import, with the reason."""
    _load_all_modules()
    return dict(_load_errors)


def tool_requirements(name: str) -> Any:
    """What shared tool *name* (or its alias) needs from the environment, or None."""
    lookup = TOOL_ALIASES.get(name, name)
    if lookup in FILE_TOOLS or lookup in GIT_TOOLS:
        return _eager_requirements.get(lookup)
    _load_all_modules()
    return _requirements.get(lookup)


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

# Alternative names that configs and models use for the same tools.
TOOL_ALIASES = {
    "read_file": "file_read",
    "read": "file_read",
    "write_file": "file_write",
    "write": "file_write",
    "list_files": "file_list",
    "search_files": "file_search",
    "edit_file_patch": "file_edit_patch",
    "replace_in_file": "file_replace",
    "delete_file": "file_delete",
    "append_to_file": "file_append",
}

def get_tools_by_names(tool_names: List[str]) -> List[Any]:
    """
    Returns a list of tools by their names.
    Supports loading from:
    1. Project tools (if project_tools_loader is initialized)
    2. Base system tools
    3. Aliases

    Args:
        tool_names: List of tool names

    Returns:
        List[Any]: List of tool functions
    """
    from core.managers.project_tools_loader import get_project_loader

    tools = []
    project_loader = get_project_loader()

    for name in tool_names:
        # 1. Check project tools (priority!)
        if project_loader and project_loader.has_tool(name):
            tool = project_loader.get_tool(name)
            if tool:
                tools.append(tool)
                continue

        # 2. Resolve alias (before iterating lazy modules, to avoid loading unnecessary ones)
        lookup_name = TOOL_ALIASES.get(name, name)

        # 3. Search in system tools
        tool = AVAILABLE_TOOLS.get(lookup_name)
        if tool is not None:
            tools.append(tool)
        else:
            from utils.logger import Logger
            Logger(__name__).warning(f"Tool '{name}' not found in project or system tools")

    return tools

def get_all_tools() -> List[Any]:
    """
    Returns all available tools.
    
    Returns:
        List[Any]: List of all tool functions
    """
    return list(AVAILABLE_TOOLS.values())

def get_file_tools_list() -> List[Any]:
    """Returns only file tools."""
    return get_file_tools()

def get_git_tools_list() -> List[Any]:
    """Returns only Git tools."""
    return get_git_tools()

def get_available_tool_names() -> List[str]:
    """
    Returns the list of names of all available tools.
    
    Returns:
        List[str]: List of tool names
    """
    return list(AVAILABLE_TOOLS.keys()) + list(TOOL_ALIASES.keys())

def get_tool_info(tool_name: str) -> Dict[str, Any]:
    """
    Returns information about a tool.
    
    Args:
        tool_name: Tool name
        
    Returns:
        Dict[str, Any]: Tool information
    """
    # Get the real name via alias if needed
    actual_name = TOOL_ALIASES.get(tool_name, tool_name)
    
    if actual_name not in AVAILABLE_TOOLS:
        return {"error": f"Tool '{tool_name}' not found"}
    
    tool_func = AVAILABLE_TOOLS[actual_name]
    
    return {
        "name": actual_name,
        "alias": tool_name if tool_name != actual_name else None,
        "description": tool_func.__doc__ or "Description not available",
        "module": tool_func.__module__,
        "type": "file" if actual_name.startswith("file_") else "git" if actual_name.startswith("git_") else "other"
    }

# ============================================================================
# TOOL STATISTICS AND MONITORING  
# ============================================================================

def get_tool_stats() -> Dict[str, Any]:
    """
    Returns tool statistics.
    
    Returns:
        Dict[str, Any]: Tool statistics
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

# Module information
__version__ = "2.0.0"
__description__ = "Enhanced Grid Agent Tools with beautiful logging"
