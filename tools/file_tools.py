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
    Logger("tool").log_tool_call(tool_name, data)

def log_tool_result(tool_name: str, result: str | Exception = "") -> None:
    Logger("tool").info(f"TOOL_RESULT | {tool_name} | {result}")

def log_tool_error(tool_name: str, error: str | Exception) -> None:
    Logger("tool").error(f"TOOL_ERROR | {tool_name} | {error}")


def _resolve_tool_path(raw_path: str) -> tuple[str, str]:
    """Resolve a tool path and preserve a safe agent-visible representation."""
    visible_path = display_agent_path_auto(raw_path)
    resolved_path = resolve_agent_path_auto(raw_path)
    return visible_path, resolved_path


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
• Extension: {extension if extension else 'no extension'}"""

        return result

    except Exception as e:
        log_tool_error("get_file_info", str(e))
        return f"❌ Error getting info for {visible_path}: {str(e)}"

@function_tool
def list_files(directory: str = ".") -> str:
    """
    Shows a list of files in the directory.
    
    Args:
        directory: Path to the directory
        
    Returns:
        str: List of files
    """
    visible_directory = display_agent_path_auto(directory)
    log_tool_call("list_files", {"directory": visible_directory})
    try:
        visible_directory, directory = _resolve_tool_path(directory)
        path = Path(directory)
        if not path.exists():
            log_tool_error("list_files", f"Directory {visible_directory} not found")
            return f"❌ Directory {visible_directory} not found"
        
        if not path.is_dir():
            log_tool_error("list_files", f"{visible_directory} is not a directory")
            return f"❌ {visible_directory} is not a directory"
        
        files = []
        dirs = []
        for item in sorted(path.iterdir()):
            if item.is_file():
                size = item.stat().st_size
                files.append(f"📄 {item.name} ({size} bytes)")
            elif item.is_dir():
                dirs.append(f"📁 {item.name}/")
        
        total_items = len(files) + len(dirs)
        log_tool_result("list_files", f"Found {total_items} items")
        
        if total_items == 0:
            return f"📂 Directory {visible_directory} is empty"
        
        all_items = dirs + files  # Directories first
        result = f"📂 Directory {visible_directory} ({total_items} items):\n\n" + "\n".join(all_items)
        return result
        
    except Exception as e:
        log_tool_error("list_files", str(e))
        return f"❌ Error reading directory {visible_directory}: {str(e)}"

@function_tool
def write_file(filepath: str, content: str) -> str:
    """
    Writes content to a file.

    Args:
        filepath: Path to the file
        content: Content to write

    Returns:
        str: Operation result
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("write_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)

        # Create parent directories if needed
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write the file
        path.write_text(content, encoding='utf-8')

        size = path.stat().st_size
        lines_count = len(content.splitlines())

        log_tool_result("write_file", f"Written {lines_count} lines, {size} bytes")
        return f"✅ File {visible_path} successfully written ({size} bytes)"

    except Exception as e:
        log_tool_error("write_file", str(e))
        return f"❌ Error writing file {visible_path}: {str(e)}"


@function_tool
def append_to_file(filepath: str, content: str) -> str:
    """
    Appends text to the end of a file.
    
    Args:
        filepath: Path to the file
        content: Text to append
        
    Returns:
        str: Message about added bytes and position range (from start to end).
             For empty file: "Insert N bytes from 0 to N".
             For non-empty: "Insert N bytes from {old_size} to {new_size}".
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("append_to_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        old_size = path.stat().st_size if path.exists() else 0
        content_bytes = content.encode("utf-8")
        n_bytes = len(content_bytes)
        
        with path.open("ab") as f:
            f.write(content_bytes)
        
        new_size = old_size + n_bytes
        log_tool_result("append_to_file", f"Appended {n_bytes} bytes, positions {old_size}–{new_size}")
        return f"Insert {n_bytes} bytes from {old_size} to {new_size}"
        
    except Exception as e:
        log_tool_error("append_to_file", str(e))
        return f"❌ Error appending to file {visible_path}: {str(e)}"


@function_tool
def search_files(
    search_pattern: str,
    directory: str = ".",
    use_regex: bool = False,
    search_in_content: bool = False,
    file_extensions: str = "",
    max_results: int = 50,
) -> str:
    """
    Search for files and directories by name or content with regex support.
    
    Args:
        search_pattern: Search pattern (string or regex)
        directory: Directory to search (default current directory)
        use_regex: Use regex matching (default False)
        search_in_content: Search in file content (default False)
        file_extensions: Filter by comma-separated file extensions (e.g. "py,js,txt")
        max_results: Maximum number of results (default 50)
        
    Returns:
        str: Search results
    """
    start_time = time.time()
    visible_directory = display_agent_path_auto(directory)
    args = {
        "search_pattern": search_pattern,
        "directory": visible_directory,
        "use_regex": use_regex,
        "search_in_content": search_in_content,
        "file_extensions": file_extensions,
        "max_results": max_results
    }
    log_tool_call("search_files", args)

    try:
        visible_directory, directory = _resolve_tool_path(directory)
        base_path = Path(directory)
        if not base_path.exists():
            result = f"ERROR: Directory {visible_directory} not found"
            log_tool_result("search_files", result)
            return result
        
        if not base_path.is_dir():
            result = f"ERROR: {visible_directory} is not a directory"
            log_tool_result("search_files", result)
            return result
        
        # Prepare search pattern
        if use_regex:
            try:
                pattern = re.compile(search_pattern, re.IGNORECASE)
            except re.error as e:
                result = f"ERROR: Invalid regex pattern '{search_pattern}': {str(e)}"
                log_tool_result("search_files", result)
                return result
        else:
            # Simple search - convert to regex for consistency
            escaped_pattern = re.escape(search_pattern)
            pattern = re.compile(escaped_pattern, re.IGNORECASE)
        
        # Prepare extensions filter
        extensions = []
        if file_extensions:
            extensions = [ext.strip().lower() for ext in file_extensions.split(',')]
            extensions = [ext if ext.startswith('.') else f'.{ext}' for ext in extensions]
        
        results = []
        
        # Log search start
        from utils.logger import log_custom
        log_custom('debug', 'file_operation', f"Starting search in: {visible_directory}", pattern=search_pattern, use_regex=use_regex)
        
        # Recursively traverse directories
        for root, dirs, files in os.walk(base_path):
            root_path = Path(root)
            
            # Search in directory names
            for dir_name in dirs:
                if len(results) >= max_results:
                    break
                    
                if pattern.search(dir_name):
                    dir_path = root_path / dir_name
                    relative_path = dir_path.relative_to(base_path)
                    results.append(f"📁 {relative_path}/ (directory)")
            
            # Search in file names
            for file_name in files:
                if len(results) >= max_results:
                    break
                
                file_path = root_path / file_name
                file_extension = file_path.suffix.lower()
                
                # Filter by extensions
                if extensions and file_extension not in extensions:
                    continue
                
                match_found = False
                match_info = ""
                
                # Search by file name
                if pattern.search(file_name):
                    match_found = True
                    match_info = "file name"
                
                # Search in file content (text files only)
                if search_in_content and not match_found:
                    try:
                        # Check if file is text
                        if file_extension in ['.py', '.js', '.json', '.md', '.txt', '.yml', '.yaml', '.html', '.css', '.xml', '.csv']:
                            content = file_path.read_text(encoding='utf-8', errors='ignore')
                            if pattern.search(content):
                                match_found = True
                                match_info = "file content"
                    except Exception:
                        # Ignore file reading errors
                        pass
                
                if match_found:
                    relative_path = file_path.relative_to(base_path)
                    file_size = file_path.stat().st_size
                    results.append(f"📄 {relative_path} ({file_size} bytes) - found in: {match_info}")
            
            if len(results) >= max_results:
                break
        
        # Log search results
        log_custom('debug', 'file_operation', f"Search completed", found_count=len(results))
        
        # Build result
        if not results:
            result = f"Search for pattern '{search_pattern}' in {visible_directory} gave no results"
        else:
            result_header = f"Search results for pattern '{search_pattern}' in {visible_directory}:\n"
            result_header += f"Found {len(results)} result(s)"
            if len(results) >= max_results:
                result_header += f" (showing first {max_results})"
            result_header += "\n\n"
            
            result = result_header + "\n".join(results)
        
        log_tool_result("search_files", result)
        return result
        
    except Exception as e:
        log_tool_error("search_files", e)
        result = f"ERROR during search: {str(e)}"
        log_tool_result("search_files", result)
        return result

@function_tool
def edit_file_patch(filepath: str, patch_content: str) -> str:
    """
    Edits a file using a patch in unified diff format.
    
    Args:
        filepath: Path to the file to edit
        patch_content: Patch content in unified diff format
        
    Returns:
        str: Operation result
    """
    start_time = time.time()
    visible_path = display_agent_path_auto(filepath)
    args = {"filepath": visible_path, "patch_content_length": len(patch_content)}
    log_tool_call("edit_file_patch", args)

    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            result = f"ERROR: File {visible_path} not found"
            log_tool_result("edit_file_patch", result)
            return result
        
        if not path.is_file():
            result = f"ERROR: {visible_path} is not a file"
            log_tool_result("edit_file_patch", result)
            return result
        
        # Log editing information
        from utils.logger import log_custom
        original_content = path.read_text(encoding='utf-8')
        original_lines = original_content.splitlines(keepends=True)
        log_custom('debug', 'file_operation', f"Editing file: {visible_path}", 
                  original_lines=len(original_lines), patch_lines=len(patch_content.splitlines()))
        
        # Parse patch
        patch_lines = patch_content.splitlines()
        new_lines = original_lines.copy()
        
        i = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            
            # Look for patch header (starts with --- or +++)
            if line.startswith('---') or line.startswith('+++'):
                i += 1
                continue
            
            # Look for change block (starts with @@)
            if line.startswith('@@'):
                # Parse line numbers
                try:
                    # Format: @@ -old_start,old_count +new_start,new_count @@
                    parts = line.split(' ')
                    old_info = parts[1]  # -old_start,old_count
                    new_info = parts[2]  # +new_start,new_count
                    
                    old_start = int(old_info.split(',')[0][1:]) - 1  # Remove minus and subtract 1
                    new_start = int(new_info.split(',')[0][1:]) - 1  # Remove plus and subtract 1
                    
                    i += 1
                    
                    # Process hunk lines
                    old_line_num = old_start
                    new_line_num = new_start
                    
                    while i < len(patch_lines):
                        patch_line = patch_lines[i]
                        
                        if patch_line.startswith('@@'):
                            # New change block
                            break
                        elif patch_line.startswith('---') or patch_line.startswith('+++'):
                            # End of patch
                            break
                        elif patch_line.startswith(' '):
                            # Context line - keep as is
                            if old_line_num < len(new_lines):
                                new_lines[old_line_num] = patch_line[1:]  # Remove leading space
                            old_line_num += 1
                            new_line_num += 1
                        elif patch_line.startswith('-'):
                            # Deleted line
                            if old_line_num < len(new_lines):
                                del new_lines[old_line_num]
                            # Don't increment new_line_num
                        elif patch_line.startswith('+'):
                            # Added line
                            if old_line_num < len(new_lines):
                                new_lines.insert(old_line_num, patch_line[1:] + '\n')  # Remove plus and add newline
                            else:
                                new_lines.append(patch_line[1:] + '\n')
                            old_line_num += 1
                            new_line_num += 1
                        else:
                            # Empty line or comment
                            pass
                        
                        i += 1
                    
                except (ValueError, IndexError) as e:
                    result = f"ERROR: Invalid patch format at line '{line}': {str(e)}"
                    log_tool_result("edit_file_patch", result)
                    return result
            else:
                i += 1
        
        # Write updated content
        new_content = ''.join(new_lines)
        path.write_text(new_content, encoding='utf-8')
        
        # Count changes
        original_line_count = len(original_lines)
        new_line_count = len(new_lines)
        changes = new_line_count - original_line_count
        
        # Log editing result
        log_custom('debug', 'file_operation', f"File updated: {visible_path}", 
                  changes=changes, new_lines=new_line_count)
        
        result = f"✅ File {visible_path} successfully updated with patch"
        if changes != 0:
            result += f" (lines changed: {changes:+d})"
        
        log_tool_result("edit_file_patch", result)
        return result
        
    except Exception as e:
        log_tool_error("edit_file_patch", e)
        result = f"Error applying patch to file {visible_path}: {str(e)}"
        log_tool_result("edit_file_patch", result)
        return result

# ============================================================================
# FILE TOOLS DICTIONARY
# ============================================================================

FILE_TOOLS = {
    "file_read": read_file,
    "file_write": write_file,
    "file_append": append_to_file,
    "file_list": list_files,
    "file_info": get_file_info,
    "file_search": search_files,
    "file_edit_patch": edit_file_patch,
}

def get_file_tools() -> List[Any]:
    """Returns a list of all file tools."""
    return list(FILE_TOOLS.values())

def get_file_tools_by_names(tool_names: List[str]) -> List[Any]:
    """Returns a list of file tools by their names."""
    tools = []
    for name in tool_names:
        if name in FILE_TOOLS:
            tools.append(FILE_TOOLS[name])
        else:
            from utils.logger import Logger
            Logger(__name__).warning(f"File tool '{name}' not found")
    return tools 