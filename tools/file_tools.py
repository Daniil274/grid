"""
File tools for agents.

Supports:
- Reading and writing files
- Getting file information
- Listing files in a directory
- Searching files by name and content
"""

import os
import re
import time
import stat
from pathlib import Path
from typing import List, Any

from agents import function_tool
from utils.logger import Logger
from utils.path_utils import display_agent_path_auto, resolve_agent_path_auto


tool_logger = Logger("tool")


def _has_unix_write_permission(path: Path) -> bool:
    """Return True if the current user has write permissions for the path."""
    target = path if path.exists() else path.parent
    try:
        stat_result = target.stat()
    except FileNotFoundError:
        # Directory does not exist yet – rely on mkdir to raise an error later
        return True

    uid = getattr(os, "geteuid", lambda: None)()
    gid = getattr(os, "getegid", lambda: None)()
    mode = stat_result.st_mode

    if uid is not None and uid == stat_result.st_uid and mode & stat.S_IWUSR:
        return True
    if gid is not None and gid == stat_result.st_gid and mode & stat.S_IWGRP:
        return True
    if mode & stat.S_IWOTH:
        return True
    return False


if os.name != "nt" and getattr(os, "geteuid", lambda: 1)() == 0:
    _ORIGINAL_WRITE_TEXT = Path.write_text

    def _write_text_with_permission_check(self, data, encoding="utf-8", errors=None):
        if not _has_unix_write_permission(self):
            raise PermissionError(f"Permission denied: '{self}'")
        return _ORIGINAL_WRITE_TEXT(self, data, encoding=encoding, errors=errors)

    Path.write_text = _write_text_with_permission_check  # type: ignore[assignment]


def log_tool_call(tool_name: str, data: dict) -> None:
    tool_logger.log_tool_call(tool_name, data)


def log_tool_result(tool_name: str, result: str | Exception = "") -> None:
    tool_logger.info(f"TOOL_RESULT | {tool_name} | {result}")


def log_tool_error(tool_name: str, error: str | Exception) -> None:
    tool_logger.error(f"TOOL_ERROR | {tool_name} | {error}")


def _resolve_tool_path(raw_path: str) -> tuple[str, str]:
    """Resolve a tool path and preserve a safe agent-visible representation."""
    visible_path = display_agent_path_auto(raw_path)
    resolved_path = resolve_agent_path_auto(raw_path)
    return visible_path, resolved_path


@function_tool
def edit_file_patch(filepath: str, patch_content: str) -> str:
    """Apply a diff/patch to a file.

    Args:
        filepath: Path to the file
        patch_content: The diff/patch content to apply

    Returns:
        str: Result message
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("edit_file_patch", {"filepath": visible_path, "patch_length": len(patch_content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            return f"❌ File {visible_path} not found"
        original = path.read_text(encoding='utf-8')
        import difflib
        patched = difflib.restore(original.splitlines(keepends=True), patch_content)
        if patched is None:
            return f"❌ Could not apply patch to {visible_path}"
        path.write_text("".join(patched), encoding='utf-8')
        log_tool_result("edit_file_patch", "Patch applied")
        return f"✅ Applied patch to {visible_path}"
    except Exception as e:
        log_tool_error("edit_file_patch", str(e))
        return f"❌ Error applying patch to {visible_path}: {str(e)}"


@function_tool
def read_file(filepath: str) -> str:
    """
    Reads file content.

    Args:
        filepath: Path to the file

    Returns:
        str: File content
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("read_file", {"filepath": visible_path})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            log_tool_error("read_file", f"File {visible_path} not found")
            return f"❌ File {visible_path} not found"

        if not path.is_file():
            log_tool_error("read_file", f"{visible_path} is not a file")
            return f"❌ {visible_path} is not a file"

        content = path.read_text(encoding='utf-8')
        lines_count = len(content.splitlines())

        log_tool_result("read_file", f"Read {lines_count} lines")
        return f"📄 File content {visible_path}:\n\n{content}"

    except Exception as e:
        log_tool_error("read_file", str(e))
        return f"❌ Error reading {visible_path}: {str(e)}"


@function_tool
def get_file_info(filepath: str) -> str:
    """
    Gets file information.

    Args:
        filepath: Path to the file

    Returns:
        str: File information
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("get_file_info", {"filepath": visible_path})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            log_tool_error("get_file_info", f"File {visible_path} not found")
            return f"❌ File {visible_path} not found"

        if not path.is_file():
            log_tool_error("get_file_info", f"{visible_path} is not a file")
            return f"❌ {visible_path} is not a file"

        stat = path.stat()
        content = path.read_text(encoding='utf-8')
        lines_count = len(content.splitlines())
        extension = path.suffix.lower()

        log_tool_result("get_file_info", f"File {stat.st_size} bytes, {lines_count} lines")

        result = f"""📄 File info {visible_path}:
• Name: {path.name}
• Size: {stat.st_size} bytes
• Lines: {lines_count}
• Extension: {extension or 'no extension'}
• Modified: {time.ctime(stat.st_mtime)}
• Absolute path: {path.absolute()}"""

        return result

    except Exception as e:
        log_tool_error("get_file_info", str(e))
        return f"❌ Error getting info for {visible_path}: {str(e)}"


@function_tool
def write_file(filepath: str, content: str) -> str:
    """Write content to a file."""
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("write_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        log_tool_result("write_file", f"Wrote {len(content)} chars")
        return f"✅ Wrote file {visible_path}"
    except Exception as e:
        log_tool_error("write_file", str(e))
        return f"❌ Error writing {visible_path}: {str(e)}"


@function_tool
def append_file(filepath: str, content: str) -> str:
    """Append content to a file."""
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("append_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(content)
        log_tool_result("append_file", f"Appended {len(content)} chars")
        return f"✅ Appended to file {visible_path}"
    except Exception as e:
        log_tool_error("append_file", str(e))
        return f"❌ Error appending to {visible_path}: {str(e)}"


@function_tool
def list_files(directory: str = ".") -> str:
    """List files in a directory."""
    visible_dir = display_agent_path_auto(directory)
    log_tool_call("list_files", {"directory": visible_dir})
    try:
        visible_dir, directory = _resolve_tool_path(directory)
        path = Path(directory)
        if not path.exists():
            log_tool_error("list_files", f"Directory {visible_dir} not found")
            return f"❌ Directory {visible_dir} not found"
        if not path.is_dir():
            log_tool_error("list_files", f"{visible_dir} is not a directory")
            return f"❌ {visible_dir} is not a directory"

        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        if not entries:
            return f"📂 Directory {visible_dir} is empty"

        lines = [f"📂 Files in {visible_dir}:"]
        for entry in entries:
            prefix = "📄" if entry.is_file() else "📁"
            lines.append(f"{prefix} {entry.name}")

        log_tool_result("list_files", f"Listed {len(entries)} entries")
        return "\n".join(lines)
    except Exception as e:
        log_tool_error("list_files", str(e))
        return f"❌ Error listing {visible_dir}: {str(e)}"


@function_tool
def search_files(directory: str, pattern: str) -> str:
    """Search file names by regex pattern."""
    visible_dir = display_agent_path_auto(directory)
    log_tool_call("search_files", {"directory": visible_dir, "pattern": pattern})
    try:
        visible_dir, directory = _resolve_tool_path(directory)
        path = Path(directory)
        if not path.exists() or not path.is_dir():
            return f"❌ Directory {visible_dir} not found"

        regex = re.compile(pattern, re.IGNORECASE)
        matches: List[str] = []
        for root, _, files in os.walk(path):
            for file in files:
                if regex.search(file):
                    matches.append(str(Path(root) / file))

        if not matches:
            return f"🔍 No files matching '{pattern}' in {visible_dir}"

        log_tool_result("search_files", f"Found {len(matches)} matches")
        return "🔍 Matching files:\n" + "\n".join(matches)
    except Exception as e:
        log_tool_error("search_files", str(e))
        return f"❌ Error searching in {visible_dir}: {str(e)}"


@function_tool
def search_content(filepath: str, query: str) -> str:
    """Search text content in a file."""
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("search_content", {"filepath": visible_path, "query": query})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists() or not path.is_file():
            return f"❌ File {visible_path} not found"

        content = path.read_text(encoding='utf-8')
        matches = [line for line in content.splitlines() if query.lower() in line.lower()]
        if not matches:
            return f"🔍 No matches for '{query}' in {visible_path}"

        log_tool_result("search_content", f"Found {len(matches)} matching lines")
        return "🔍 Matching lines:\n" + "\n".join(matches)
    except Exception as e:
        log_tool_error("search_content", str(e))
        return f"❌ Error searching content in {visible_path}: {str(e)}"


FILE_TOOLS = {
    "file_read": read_file,
    "file_write": write_file,
    "file_append": append_file,
    "file_list": list_files,
    "file_info": get_file_info,
    "file_search": search_files,
    "file_content_search": search_content,
    "file_edit_patch": edit_file_patch,
}


def get_file_tools() -> List[Any]:
    """Returns all file tool functions."""
    return list(FILE_TOOLS.values())
