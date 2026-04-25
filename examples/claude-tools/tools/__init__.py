"""
Claude Tools - adaptation of OpenClaude tools for the Grid Agent System.
"""

from .bash_tool import bash_tool
from .file_tools import file_read, file_write, file_edit, file_append
from .search_tools import glob_tool, grep_tool
from .web_tools import web_fetch, web_search
from .notebook_tool import notebook_read, notebook_edit, notebook_create
from .todo_tool import todo_write, todo_list, todo_delete, todo_clear

# Tool registry for automatic registration
CLAUDE_TOOLS = {
    "bash_tool": bash_tool,
    "file_read": file_read,
    "file_write": file_write,
    "file_edit": file_edit,
    "file_append": file_append,
    "glob_tool": glob_tool,
    "grep_tool": grep_tool,
    "web_fetch": web_fetch,
    "web_search": web_search,
    "notebook_read": notebook_read,
    "notebook_edit": notebook_edit,
    "notebook_create": notebook_create,
    "todo_write": todo_write,
    "todo_list": todo_list,
    "todo_delete": todo_delete,
    "todo_clear": todo_clear,
}

__all__ = [
    # Bash
    "bash_tool",
    # File
    "file_read",
    "file_write",
    "file_edit",
    "file_append",
    # Search
    "glob_tool",
    "grep_tool",
    # Web
    "web_fetch",
    "web_search",
    # Notebook
    "notebook_read",
    "notebook_edit",
    "notebook_create",
    # Todo
    "todo_write",
    "todo_list",
    "todo_delete",
    "todo_clear",
    # Registry
    "CLAUDE_TOOLS",
]
