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
from utils.text_patch import PatchError, apply_patch, keep_newlines, replace_once


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


MAX_MATCHES = 200
_SKIPPED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _refuse_git_internals(visible_path: str, resolved: str) -> str | None:
    """Git's own files are never changed by file tools: an edited .git/config or
    hook runs commands the next time git does. Returns the refusal, if any."""
    if ".git" in Path(resolved).parts:
        return f"❌ {visible_path} is inside .git: git internals cannot be changed with file tools"
    return None


def _read_existing(path: Path) -> str | None:
    """Current text with its own line endings, or None for a missing file."""
    if not path.is_file():
        return None
    with open(path, encoding="utf-8", newline="") as file:
        return file.read()


def _write(path: Path, text: str) -> None:
    """Write text exactly as given: no newline translation on Windows."""
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(text)


@function_tool
def edit_file_patch(filepath: str, patch_content: str) -> str:
    """Apply a unified diff to a file.

    Each changed block starts with an ``@@`` line (line numbers are optional)
    followed by lines prefixed with ' ' (unchanged context), '-' (remove) or
    '+' (add). A block is found by its context and removed lines, so copy them
    exactly from the file. The file keeps its line endings. To replace one
    exact fragment, file_replace is simpler.

    Args:
        filepath: Path to the file
        patch_content: The unified diff

    Returns:
        str: Result message
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("edit_file_patch", {"filepath": visible_path, "patch_length": len(patch_content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        refused = _refuse_git_internals(visible_path, filepath)
        if refused:
            return refused
        path = Path(filepath)
        original = _read_existing(path)
        if original is None:
            return f"❌ File {visible_path} not found"
        updated = apply_patch(original, patch_content)
        _write(path, updated)
        delta = updated.count("\n") - original.count("\n")
        log_tool_result("edit_file_patch", f"Patch applied ({delta:+d} lines)")
        return f"✅ Applied patch to {visible_path} ({delta:+d} lines)"
    except PatchError as e:
        log_tool_error("edit_file_patch", str(e))
        return f"❌ Patch not applied to {visible_path}: {e}"
    except Exception as e:
        log_tool_error("edit_file_patch", str(e))
        return f"❌ Error applying patch to {visible_path}: {str(e)}"


@function_tool
def replace_in_file(filepath: str, old_text: str, new_text: str) -> str:
    """Replace one exact fragment of a file with new text.

    old_text must occur exactly once: copy it from the file with its
    indentation, and add neighbouring lines if it is not unique. The rest of the
    file, its formatting and its line endings stay untouched. Prefer this to
    rewriting a file for a focused change.

    Args:
        filepath: Path to the file
        old_text: The exact text to replace (whole or partial lines)
        new_text: The text to put in its place
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("replace_in_file", {"filepath": visible_path, "old_length": len(old_text),
                                      "new_length": len(new_text)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        refused = _refuse_git_internals(visible_path, filepath)
        if refused:
            return refused
        path = Path(filepath)
        original = _read_existing(path)
        if original is None:
            return f"❌ File {visible_path} not found"
        _write(path, replace_once(original, old_text, new_text))
        log_tool_result("replace_in_file", "Replaced")
        return f"✅ Replaced text in {visible_path}"
    except PatchError as e:
        log_tool_error("replace_in_file", str(e))
        return f"❌ Nothing replaced in {visible_path}: {e}"
    except Exception as e:
        log_tool_error("replace_in_file", str(e))
        return f"❌ Error editing {visible_path}: {str(e)}"


@function_tool
def delete_file(filepath: str) -> str:
    """Delete one file of the working directory (not a directory).

    Use it for files created by mistake, such as scratch or test files.

    Args:
        filepath: Path to the file
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("delete_file", {"filepath": visible_path})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        refused = _refuse_git_internals(visible_path, filepath)
        if refused:
            return refused
        path = Path(filepath)
        if not path.exists():
            return f"❌ File {visible_path} not found"
        if not path.is_file():
            return f"❌ {visible_path} is a directory: only files can be deleted"
        path.unlink()
        log_tool_result("delete_file", "Deleted")
        return f"✅ Deleted {visible_path}"
    except Exception as e:
        log_tool_error("delete_file", str(e))
        return f"❌ Error deleting {visible_path}: {str(e)}"


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
    """Create a file or replace its whole content.

    An existing file keeps its line endings. To change part of a file use
    file_replace or file_edit_patch rather than rewriting it.
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("write_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        refused = _refuse_git_internals(visible_path, filepath)
        if refused:
            return refused
        path = Path(filepath)
        existing = _read_existing(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, keep_newlines(existing, content))
        log_tool_result("write_file", f"Wrote {len(content)} chars")
        verb = "Replaced" if existing is not None else "Created"
        return f"✅ {verb} file {visible_path}"
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
        refused = _refuse_git_internals(visible_path, filepath)
        if refused:
            return refused
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
    """Find files whose name matches a regex below a directory (skips .git and caches)."""
    visible_dir = display_agent_path_auto(directory)
    log_tool_call("search_files", {"directory": visible_dir, "pattern": pattern})
    try:
        visible_dir, directory = _resolve_tool_path(directory)
        path = Path(directory)
        if not path.exists() or not path.is_dir():
            return f"❌ Directory {visible_dir} not found"

        regex = re.compile(pattern, re.IGNORECASE)
        matches: List[str] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS)
            for file in sorted(files):
                if regex.search(file):
                    # Relative to the searched directory: host paths stay private.
                    matches.append((Path(root) / file).relative_to(path).as_posix())

        if not matches:
            return f"🔍 No files matching '{pattern}' in {visible_dir}"

        log_tool_result("search_files", f"Found {len(matches)} matches")
        shown = matches[:MAX_MATCHES]
        more = (f"\n... and {len(matches) - len(shown)} more: narrow the pattern"
                if len(matches) > len(shown) else "")
        return f"🔍 Matching files in {visible_dir}:\n" + "\n".join(shown) + more
    except Exception as e:
        log_tool_error("search_files", str(e))
        return f"❌ Error searching in {visible_dir}: {str(e)}"


@function_tool
def search_content(filepath: str, query: str) -> str:
    """Find the lines of one file that contain a text (case-insensitive), with line numbers."""
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("search_content", {"filepath": visible_path, "query": query})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists() or not path.is_file():
            return f"❌ File {visible_path} not found"

        content = path.read_text(encoding='utf-8')
        needle = query.lower()
        matches = [
            f"{number}: {line}"
            for number, line in enumerate(content.splitlines(), start=1)
            if needle in line.lower()
        ]
        if not matches:
            return f"🔍 No matches for '{query}' in {visible_path}"

        log_tool_result("search_content", f"Found {len(matches)} matching lines")
        shown = matches[:MAX_MATCHES]
        more = (f"\n... and {len(matches) - len(shown)} more: use a more specific query"
                if len(matches) > len(shown) else "")
        return f"🔍 Matching lines in {visible_path}:\n" + "\n".join(shown) + more
    except UnicodeDecodeError:
        return f"❌ {visible_path} is not a text file"
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
    "file_replace": replace_in_file,
    "file_delete": delete_file,
}


def get_file_tools() -> List[Any]:
    """Returns all file tool functions."""
    return list(FILE_TOOLS.values())
