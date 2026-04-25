"""
Git tools for agents.

Supports:
- Repository status
- Commit history
- File differences
- Branch management
- Adding files and creating commits
- Synchronizing with remote repositories
- Cyrillic support in author and branch names
"""

import re
import subprocess
import time
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool, RunContextWrapper
from utils.logger import Logger
from utils.path_utils import (
    display_agent_path_from_ctx,
    resolve_agent_path_from_ctx,
    sanitize_text_for_agent_from_ctx,
)

class _PrettyShim:
    def __init__(self):
        self._logger = Logger("git_tool")
    def tool_start(self, name: str, **kwargs):
        self._logger.log_tool_call(f"GIT:{name}", kwargs)
        return {"name": name, "args": kwargs}
    def tool_result(self, operation, result: str | None = None, error: str | None = None):
        if error:
            self._logger.error(f"{operation.get('name')} failed", error=error)
        else:
            self._logger.info(f"{operation.get('name')} ok", result=result)

pretty_logger = _PrettyShim()

def log_tool_start(name: str, **kwargs):
    return pretty_logger.tool_start(name, **kwargs)

def log_tool_result(name_or_operation, *, result: str | None = None, error: str | None = None):
    if isinstance(name_or_operation, dict):
        pretty_logger.tool_result(name_or_operation, result=result, error=error)
    else:
        pretty_logger.tool_result({"name": name_or_operation, "args": {}}, result=result, error=error)

# Container path for workspace (Docker forbids bind to "/"). Agent sees root as "/".
_CONTAINER_ROOT = "/workspace"

def _map_path_to_container(path: Optional[str], context: Any) -> str:
    """Map a host or agent path to the container path (agent root is "/", container uses _CONTAINER_ROOT)."""
    if not path or path == "." or path == "/":
        return _CONTAINER_ROOT

    if path.startswith(_CONTAINER_ROOT + "/") or path == _CONTAINER_ROOT:
        return path

    # If it's an absolute host path (e.g. /home/user/grid), map to container root first (avoid /workspace/home/user/grid).
    if os.path.isabs(path):
        try:
            if hasattr(context, 'context') and hasattr(context.context, 'factory'):
                host_wd = context.context.factory.config.get_working_directory()
                norm_path = os.path.normpath(path)
                norm_host_wd = os.path.normpath(host_wd)
                if norm_path.startswith(norm_host_wd):
                    rel = os.path.relpath(norm_path, norm_host_wd)
                    if rel == ".":
                        return _CONTAINER_ROOT
                    return (Path(_CONTAINER_ROOT) / rel).as_posix()
        except Exception:
            pass

    # Agent may send paths as "/file" (root is "/")
    if path.startswith("/"):
        return (_CONTAINER_ROOT + path).replace("//", "/")

    if not os.path.isabs(path):
        return (Path(_CONTAINER_ROOT) / path).as_posix()

    return _CONTAINER_ROOT

def _run_git_command(command: List[str], cwd: Optional[str] = None, container_id: Optional[str] = None, context: Any = None) -> Dict[str, Any]:
    """
    Safe execution of Git command with validation.
    
    Args:
        command: List of command arguments
        cwd: Working directory
        container_id: Container ID for command execution (optional)
        context: Context for path mapping
        
    Returns:
        Dict with result: {"success": bool, "output": str, "error": str}
    """
    try:
        # Verify command starts with git
        if not command or command[0] != "git":
            return {"success": False, "output": "", "error": "Command must start with 'git'"}
        
        # Validation of dangerous commands - only check command arguments
        dangerous_commands = ["rm", "clean", "reset --hard", "push --force", "rebase -i"]
        cmd_str = " ".join(command)
        for dangerous in dangerous_commands:
            # Check that dangerous command is a separate argument, not part of another
            if f" {dangerous} " in f" {cmd_str} " or cmd_str.endswith(f" {dangerous}") or cmd_str.startswith(f"{dangerous} "):
                return {"success": False, "output": "", "error": f"Dangerous command blocked: {dangerous}"}
        
        # Log command execution
        from utils.logger import log_custom
        log_custom('debug', 'git_command', f"Executing: {' '.join(command)}", cwd=cwd, container_id=container_id, context=context)
        
        # Execute command
        if container_id:
            # Construct docker exec command
            # docker exec -i -w <container_root> <container_id> <command>
            
            workdir = _map_path_to_container(cwd, context)

            docker_cmd = ["docker", "exec", "-i", "-w", workdir, container_id] + command
            
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=30
            )
        else:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=30
            )
        
        # Log result
        if result.returncode == 0:
            log_custom('debug', 'git_command', f"Successfully executed: {' '.join(command)}")
        else:
            log_custom('debug', 'git_command', f"Execution error: {' '.join(command)}", error=result.stderr)
        
        return {
            "success": result.returncode == 0,
            "output": sanitize_text_for_agent_from_ctx(result.stdout.strip(), context),
            "error": sanitize_text_for_agent_from_ctx(result.stderr.strip(), context),
        }
        
    except subprocess.TimeoutExpired:
        from utils.logger import log_custom
        log_custom('error', 'git_command', f"Command timeout: {' '.join(command)}")
        return {"success": False, "output": "", "error": "Command exceeded time limit"}
    except FileNotFoundError as e:
        from utils.logger import log_custom
        log_custom('error', 'git_command', f"Executable not found: {e}. command={command if not container_id else docker_cmd}")
        return {"success": False, "output": "", "error": f"Executable not found: {e}"}
    except Exception as e:
        from utils.logger import log_custom
        log_custom('error', 'git_command', f"Command execution error: {e}")
        return {"success": False, "output": "", "error": f"Command execution error: {e}"}

def _get_container_id(context: Any) -> Optional[str]:
    """Extract container_id from context."""
    if hasattr(context, 'context') and hasattr(context.context, 'container_id'):
        return context.context.container_id
    return None


def _resolve_git_directory(context: Any, directory: str) -> tuple[str, str]:
    """Return safe display path plus resolved host path for a repo directory."""
    visible_directory = display_agent_path_from_ctx(directory, context)
    resolved_directory = resolve_agent_path_from_ctx(directory, context)
    return visible_directory, resolved_directory

@function_tool
def git_status(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Shows the status of the Git repository.
    
    Args:
        directory: Path to the repository
        
    Returns:
        str: Repository status
    """
    visible_directory = display_agent_path_from_ctx(directory, context)
    operation = pretty_logger.tool_start("GitStatus", directory=visible_directory)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory

        if not container_id:
            path = Path(resolved_directory)
            if not path.exists():
                pretty_logger.tool_result(operation, error=f"Directory {visible_directory} not found")
                return f"❌ Directory {visible_directory} not found"
        
        cmd_result = _run_git_command(["git", "status", "--porcelain"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            error_text = cmd_result.get("error", "")
            error_text_lower = error_text.lower()
            if "not a git repository" in error_text_lower:
                pretty_logger.tool_result(operation, error=error_text)
                return f"❌ Not a Git repository: {error_text}"
            pretty_logger.tool_result(operation, error=error_text)
            return f"❌ Git error: {error_text}"
        
        
        # Format output
        if not cmd_result["output"]:
            pretty_logger.tool_result(operation, result="Repository is clean")
            return "✅ Working directory is clean - no changes"
        else:
            lines = cmd_result["output"].split('\n')
            status_map = {
                'M': 'modified',
                'A': 'added',
                'D': 'deleted',
                'R': 'renamed',
                'C': 'copied',
                '??': 'untracked'
            }
            
            formatted_lines = []
            for line in lines:
                if len(line) < 3:
                    continue
                status_code = line[:2].strip()
                filename_start = 2
                while filename_start < len(line) and line[filename_start] == ' ':
                    filename_start += 1
                filename = line[filename_start:].strip()
                status_text = status_map.get(status_code, status_code)
                formatted_lines.append(f"  📝 {status_text}: {filename}")
            
            changes_count = len(formatted_lines)
            pretty_logger.tool_result(operation, result=f"Found {changes_count} changes")
            result = f"📋 Git repository status in {visible_directory} ({changes_count} changes):\n\n" + "\n".join(formatted_lines)
        
        return result
        
    except Exception as e:
        pretty_logger.tool_result(operation, error=str(e))
        return f"❌ Error getting Git status: {str(e)}"

@function_tool
def git_log(context: RunContextWrapper, directory: str = ".", max_commits: int = 10) -> str:
    """
    Shows the commit history.
    
    Args:
        directory: Path to the repository
        max_commits: Maximum number of commits to display
        
    Returns:
        str: Commit history
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "max_commits": max_commits}
    operation = log_tool_start("git_log", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, error=result)
                return result
        
        # Limit the number of commits for safety
        max_commits = min(max_commits, 50)
        
        cmd_result = _run_git_command([
            "git", "log", 
            f"--max-count={max_commits}",
            "--pretty=format:%h|%an|%ad|%s",
            "--date=short"
        ], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ERROR: {cmd_result['error']}"
            log_tool_result(operation, error=result)
            return result
        
        if not cmd_result["output"]:
            result = "Commit history is empty"
        else:
            lines = cmd_result["output"].split('\n')
            formatted_lines = [f"Commit history in {visible_directory}:\n"]
            
            for line in lines:
                parts = line.split('|')
                if len(parts) == 4:
                    hash_short, author, date, message = parts
                    formatted_lines.append(f"  {hash_short} - {author} ({date}): {message}")
            
            result = "\n".join(formatted_lines)
        
        duration = time.time() - start_time
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR getting history: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_diff(context: RunContextWrapper, directory: str = ".", filename: str = "") -> str:
    """
    Shows differences in files.
    
    Args:
        directory: Path to the repository
        filename: Specific file name (optional)
        
    Returns:
        str: File differences
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    visible_filename = display_agent_path_from_ctx(filename, context) if filename else ""
    args = {"directory": visible_directory, "filename": visible_filename}
    operation = log_tool_start("git_diff", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, error=result)
                return result
        
        # Build command
        command = ["git", "diff"]
        if filename:
            # Validate filename
            if not re.match(r'^[a-zA-Z0-9._/-]+$', filename):
                result = f"ERROR: Invalid filename: {visible_filename}"
                log_tool_result(operation, error=result)
                return result
            command.append(filename)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ERROR: {cmd_result['error']}"
            log_tool_result(operation, error=result)
            return result
        
        if not cmd_result["output"]:
            result = "No changes to display"
        else:
            # Show full output  
            result = f"Differences in {visible_directory}" + (f" for file {visible_filename}" if filename else "") + ":\n\n" + cmd_result["output"]
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR getting differences: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_branch_list(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Shows the list of branches.
    
    Args:
        directory: Path to the repository
        
    Returns:
        str: List of branches
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory}
    operation = log_tool_start("git_branch_list", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, result=result)
                return result
        
        cmd_result = _run_git_command(["git", "branch", "-a"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ERROR: {cmd_result['error']}"
            log_tool_result(operation, result=result)
            return result
        
        if not cmd_result["output"]:
            result = "No branches to display"
        else:
            lines = cmd_result["output"].split('\n')
            formatted_lines = [f"Branches in repository {visible_directory}:\n"]
            
            for line in lines:
                line = line.strip()
                if line:
                    if line.startswith('*'):
                        formatted_lines.append(f"  ➤ {line[1:].strip()} (current)")
                    else:
                        formatted_lines.append(f"    {line}")
            
            result = "\n".join(formatted_lines)
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR getting branch list: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_add_file(context: RunContextWrapper, directory: str, filename: str) -> str:
    """
    Adds a file to the Git index.
    
    Args:
        directory: Path to the repository
        filename: Name of the file to add
        
    Returns:
        str: Operation result
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    visible_filename = display_agent_path_from_ctx(filename, context)
    args = {"directory": visible_directory, "filename": visible_filename}
    operation = log_tool_start("git_add_file", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, result=result)
                return result
            
            # Check that the file exists (locally)
            file_path = path / filename
            if not file_path.exists():
                result = f"ERROR: File {visible_filename} not found"
                log_tool_result(operation, result=result)
                return result
        
        # Validate filename
        if not re.match(r'^[a-zA-Z0-9._/-]+$', filename):
            result = f"ERROR: Invalid filename: {visible_filename}"
            log_tool_result(operation, result=result)
            return result
        
        cmd_result = _run_git_command(["git", "add", filename], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ File {visible_filename} successfully added to index"
        else:
            result = f"ERROR adding file: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR adding file: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_commit(context: RunContextWrapper, directory: str, message: str, author_name: str = "", author_email: str = "") -> str:
    """
    Creates a commit with the specified message.
    
    Args:
        directory: Path to the repository
        message: Commit message
        author_name: Author name (optional)
        author_email: Author email (optional)
        
    Returns:
        str: Operation result
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "message": message, "author_name": author_name, "author_email": author_email}
    operation = log_tool_start("git_commit", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, result=result)
                return result
        
        # Validate commit message
        if not message or len(message.strip()) < 3:
            result = "ERROR: Commit message must be at least 3 characters"
            log_tool_result(operation, result=result)
            return result
        
        if len(message) > 500:
            result = "ERROR: Commit message is too long (max 500 characters)"
            log_tool_result(operation, result=result)
            return result
        
        # Build command
        command = ["git", "commit", "-m", message.strip()]
        
        # Add author if specified
        if author_name and author_email:
            # Validate author name - supports Cyrillic, Latin, digits, spaces, hyphens and dots
            if not re.match(r'^[\w\sа-яёА-ЯЁ\-\.]+$', author_name):
                result = "ERROR: Invalid author name (supports letters, digits, spaces, hyphens and dots)"
                log_tool_result(operation, result=result)
                return result
            
            # More flexible email validation - supports local addresses
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+(\.[a-zA-Z]{2,})?$', author_email):
                result = "ERROR: Invalid author email (format: user@domain.com or user@local)"
                log_tool_result(operation, result=result)
                return result
            
            command.extend(["--author", f"{author_name} <{author_email}>"])
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Commit successfully created: {message}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ERROR creating commit: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR during operation: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_checkout_branch(context: RunContextWrapper, directory: str, branch_name: str, create_new: bool = False) -> str:
    """
    Switches to a branch or creates a new one.
    
    Args:
        directory: Path to the repository
        branch_name: Branch name
        create_new: Create a new branch if it doesn't exist
        
    Returns:
        str: Operation result
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "branch_name": branch_name, "create_new": create_new}
    operation = log_tool_start("git_checkout_branch", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, result=result)
                return result
        
        # Validate branch name - supports Cyrillic and basic characters
        if not re.match(r'^[\wа-яёА-ЯЁ._/-]+$', branch_name):
            result = f"ERROR: Invalid branch name: {branch_name} (supports letters, digits, dots, hyphens, underscores and slashes)"
            log_tool_result(operation, result=result)
            return result
        
        # Build command
        command = ["git", "checkout"]
        if create_new:
            command.append("-b")
        command.append(branch_name)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            action = "created and switched" if create_new else "switched to"
            result = f"✅ Branch {branch_name} {action}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"Error switching to branch: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"Error during operation: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_pull(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Fetches and merges changes from a remote repository.
    
    Args:
        directory: Path to the repository
        
    Returns:
        str: Operation result
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory}
    operation = log_tool_start("git_pull", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ERROR: {visible_directory} is not a Git repository"
                log_tool_result(operation, result=result)
                return result
        
        cmd_result = _run_git_command(["git", "pull"], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = "✅ Changes successfully fetched from remote repository"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ERROR fetching changes: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR performing operation: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_remote_info(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Shows information about remote repositories.
    
    Args:
        directory: Path to the repository
        
    Returns:
        str: Information about remote repositories
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory}
    operation = log_tool_start("git_remote_info", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
        
        cmd_result = _run_git_command(["git", "remote", "-v"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, result=result)
            return result
        
        if not cmd_result["output"]:
            result = "Удаленные репозитории не настроены"
        else:
            result = f"Удаленные репозитории для {visible_directory}:\n\n{cmd_result['output']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_init(context: RunContextWrapper, directory: str = ".", bare: bool = False) -> str:
    """
    Инициализирует новый Git репозиторий.
    
    Args:
        directory: Путь к директории для инициализации
        bare: Создать bare репозиторий (без рабочей директории)
        
    Returns:
        str: Результат инициализации
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "bare": bare}
    operation = log_tool_start("git_init", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists():
                result = f"ОШИБКА: Директория {visible_directory} не существует"
                log_tool_result(operation, error=result)
                return result
            
            # Проверяем, что это не уже Git репозиторий
            if (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} уже является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Формируем команду
        command = ["git", "init"]
        if bare:
            command.append("--bare")
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            repo_type = "bare" if bare else "обычный"
            result = f"✅ Git репозиторий ({repo_type}) успешно инициализирован в {visible_directory}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при инициализации: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_config(context: RunContextWrapper, directory: str = ".", name: str = "", email: str = "", global_config: bool = False) -> str:
    """
    Настраивает Git конфигурацию (имя пользователя и email).
    
    Args:
        directory: Путь к репозиторию
        name: Имя пользователя
        email: Email пользователя
        global_config: Применить глобально (--global)
        
    Returns:
        str: Результат настройки
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "name": name, "email": email, "global_config": global_config}
    operation = log_tool_start("git_config", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(resolved_directory)
            if not global_config and (not path.exists() or not (path / ".git").exists()):
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path) if not global_config else None
        else:
            if not global_config:
                cwd = resolved_directory
        
        results = []
        
        # Настраиваем имя пользователя
        if name:
            command = ["git", "config"]
            if global_config:
                command.append("--global")
            command.extend(["user.name", name])
            
            cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
            if cmd_result["success"]:
                results.append(f"✅ Имя пользователя установлено: {name}")
            else:
                results.append(f"❌ Ошибка установки имени: {cmd_result['error']}")
        
        # Настраиваем email
        if email:
            command = ["git", "config"]
            if global_config:
                command.append("--global")
            command.extend(["user.email", email])
            
            cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
            if cmd_result["success"]:
                results.append(f"✅ Email установлен: {email}")
            else:
                results.append(f"❌ Ошибка установки email: {cmd_result['error']}")
        
        if not name and not email:
            # Показываем текущую конфигурацию
            command = ["git", "config"]
            if global_config:
                command.append("--global")
            command.extend(["--list"])
            
            cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
            if cmd_result["success"]:
                result = f"Текущая конфигурация Git:\n{cmd_result['output']}"
            else:
                result = f"ОШИБКА при получении конфигурации: {cmd_result['error']}"
        else:
            result = "\n".join(results)
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_add_all(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Добавляет все измененные файлы в индекс Git.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory}
    operation = log_tool_start("git_add_all", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        cmd_result = _run_git_command(["git", "add", "."], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = "✅ Все измененные файлы добавлены в индекс"
        else:
            result = f"ОШИБКА при добавлении файлов: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_push(context: RunContextWrapper, directory: str = ".", remote: str = "origin", branch: str = "") -> str:
    """
    Отправляет изменения в удаленный репозиторий.
    
    Args:
        directory: Путь к репозиторию
        remote: Имя удаленного репозитория
        branch: Имя ветки (если не указана, используется текущая)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "remote": remote, "branch": branch}
    operation = log_tool_start("git_push", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Формируем команду
        command = ["git", "push", remote]
        if branch:
            command.append(branch)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Изменения успешно отправлены в {remote}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при отправке изменений: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_remote_add(context: RunContextWrapper, directory: str, name: str, url: str) -> str:
    """
    Добавляет удаленный репозиторий.
    
    Args:
        directory: Путь к репозиторию
        name: Имя удаленного репозитория
        url: URL удаленного репозитория
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "name": name, "url": url}
    operation = log_tool_start("git_remote_add", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Валидация URL
        if not url.startswith(('http://', 'https://', 'git://', 'ssh://', 'git@')):
            result = f"ОШИБКА: Недопустимый URL репозитория: {url}"
            log_tool_result(operation, error=result)
            return result
        
        cmd_result = _run_git_command(["git", "remote", "add", name, url], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Удаленный репозиторий '{name}' добавлен: {url}"
        else:
            result = f"ОШИБКА при добавлении удаленного репозитория: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_remote_remove(context: RunContextWrapper, directory: str, name: str) -> str:
    """
    Удаляет удаленный репозиторий.
    
    Args:
        directory: Путь к репозиторию
        name: Имя удаленного репозитория
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "name": name}
    operation = log_tool_start("git_remote_remove", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        cmd_result = _run_git_command(["git", "remote", "remove", name], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Удаленный репозиторий '{name}' удален"
        else:
            result = f"ОШИБКА при удалении удаленного репозитория: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_merge(context: RunContextWrapper, directory: str, branch_name: str, message: str = "") -> str:
    """
    Сливает указанную ветку в текущую.
    
    Args:
        directory: Путь к репозиторию
        branch_name: Имя ветки для слияния
        message: Сообщение коммита слияния (опционально)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "branch_name": branch_name, "message": message}
    operation = log_tool_start("git_merge", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Формируем команду
        command = ["git", "merge"]
        if message:
            command.extend(["-m", message])
        command.append(branch_name)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Ветка '{branch_name}' успешно слита"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при слиянии ветки: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_reset(context: RunContextWrapper, directory: str, mode: str = "soft", commit_hash: str = "HEAD~1") -> str:
    """
    Сбрасывает состояние репозитория к указанному коммиту.
    
    Args:
        directory: Путь к репозиторию
        mode: Режим сброса (soft, mixed, hard)
        commit_hash: Хеш коммита для сброса (по умолчанию HEAD~1)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "mode": mode, "commit_hash": commit_hash}
    operation = log_tool_start("git_reset", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Валидация режима
        valid_modes = ["soft", "mixed", "hard"]
        if mode not in valid_modes:
            result = f"ОШИБКА: Недопустимый режим сброса '{mode}'. Допустимые: {', '.join(valid_modes)}"
            log_tool_result(operation, error=result)
            return result
        
        # Проверяем, что это не hard reset (опасная операция)
        if mode == "hard":
            result = "⚠️  ВНИМАНИЕ: Hard reset может привести к потере данных. Операция заблокирована для безопасности."
            log_tool_result(operation, error=result)
            return result
        
        cmd_result = _run_git_command(["git", "reset", f"--{mode}", commit_hash], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Репозиторий сброшен к коммиту {commit_hash} (режим: {mode})"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при сбросе: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_stash(context: RunContextWrapper, directory: str = ".", action: str = "save", message: str = "") -> str:
    """
    Управляет stash (временным сохранением изменений).
    
    Args:
        directory: Путь к репозиторию
        action: Действие (save, list, pop, apply, drop)
        message: Сообщение для stash (только для save)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "action": action, "message": message}
    operation = log_tool_start("git_stash", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Формируем команду
        command = ["git", "stash"]
        
        if action == "save":
            if message:
                command.extend(["save", message])
            else:
                command.append("save")
        elif action == "list":
            command.append("list")
        elif action == "pop":
            command.append("pop")
        elif action == "apply":
            command.append("apply")
        elif action == "drop":
            command.append("drop")
        else:
            result = f"ОШИБКА: Недопустимое действие '{action}'. Допустимые: save, list, pop, apply, drop"
            log_tool_result(operation, error=result)
            return result
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            if action == "list":
                if cmd_result["output"]:
                    result = f"Список stash:\n{cmd_result['output']}"
                else:
                    result = "Список stash пуст"
            else:
                result = f"✅ Stash операция '{action}' выполнена успешно"
                if cmd_result["output"]:
                    result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при выполнении stash: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_tag(context: RunContextWrapper, directory: str, tag_name: str, message: str = "", commit_hash: str = "HEAD") -> str:
    """
    Создает тег в репозитории.
    
    Args:
        directory: Путь к репозиторию
        tag_name: Имя тега
        message: Сообщение тега (опционально)
        commit_hash: Хеш коммита для тегирования (по умолчанию HEAD)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "tag_name": tag_name, "message": message, "commit_hash": commit_hash}
    operation = log_tool_start("git_tag", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        # Валидация имени тега
        if not re.match(r'^[\wа-яёА-ЯЁ._/-]+$', tag_name):
            result = f"ОШИБКА: Недопустимое имя тега: {tag_name}"
            log_tool_result(operation, error=result)
            return result
        
        # Формируем команду
        command = ["git", "tag"]
        if message:
            command.extend(["-a", tag_name, "-m", message, commit_hash])
        else:
            command.extend([tag_name, commit_hash])
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Тег '{tag_name}' создан для коммита {commit_hash}"
            if message:
                result += f" с сообщением: {message}"
        else:
            result = f"ОШИБКА при создании тега: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_tag_list(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Показывает список тегов в репозитории.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Список тегов
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory}
    operation = log_tool_start("git_tag_list", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        cmd_result = _run_git_command(["git", "tag", "-l"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, error=result)
            return result
        
        if not cmd_result["output"]:
            result = "Теги не найдены"
        else:
            tags = cmd_result["output"].split('\n')
            formatted_tags = [f"Теги в репозитории {visible_directory}:\n"]
            for tag in tags:
                if tag.strip():
                    formatted_tags.append(f"  🏷️  {tag.strip()}")
            result = "\n".join(formatted_tags)
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_clone(context: RunContextWrapper, directory: str, repository_url: str, branch: str = "") -> str:
    """
    Клонирует удаленный репозиторий.
    
    Args:
        directory: Директория для клонирования
        repository_url: URL репозитория
        branch: Ветка для клонирования (опционально)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "repository_url": repository_url, "branch": branch}
    operation = log_tool_start("git_clone", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(resolved_directory)
            if path.exists():
                result = f"ОШИБКА: Директория {visible_directory} уже существует"
                log_tool_result(operation, error=result)
                return result
            cwd = None # clone uses current dir, but here we specify target dir in command
        else:
             # In container, directory is relative to container root or absolute
             pass
        
        # Валидация URL
        if not repository_url.startswith(('http://', 'https://', 'git://', 'ssh://', 'git@')):
            result = f"ОШИБКА: Недопустимый URL репозитория: {repository_url}"
            log_tool_result(operation, error=result)
            return result
        
        # Формируем команду
        command = ["git", "clone"]
        if branch:
            command.extend(["-b", branch])
        command.extend([repository_url, resolved_directory])
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Репозиторий успешно клонирован в {visible_directory}"
            if branch:
                result += f" (ветка: {branch})"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при клонировании: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_fetch(context: RunContextWrapper, directory: str = ".", remote: str = "origin") -> str:
    """
    Получает изменения из удаленного репозитория без слияния.
    
    Args:
        directory: Путь к репозиторию
        remote: Имя удаленного репозитория
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_directory = display_agent_path_from_ctx(directory, context)
    args = {"directory": visible_directory, "remote": remote}
    operation = log_tool_start("git_fetch", **args)
    
    try:
        visible_directory, resolved_directory = _resolve_git_directory(context, directory)
        container_id = _get_container_id(context)
        cwd = resolved_directory
        if not container_id:
            path = Path(resolved_directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {visible_directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
        
        cmd_result = _run_git_command(["git", "fetch", remote], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Изменения получены из {remote}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при получении изменений: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

# ============================================================================
# СЛОВАРЬ GIT ИНСТРУМЕНТОВ
# ============================================================================

GIT_TOOLS = {
    # Основные операции
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "git_branch_list": git_branch_list,
    "git_add_file": git_add_file,
    "git_add_all": git_add_all,
    "git_commit": git_commit,
    "git_checkout_branch": git_checkout_branch,
    
    # Инициализация и настройка
    "git_init": git_init,
    "git_config": git_config,
    "git_clone": git_clone,
    
    # Удаленные репозитории
    "git_remote_info": git_remote_info,
    "git_remote_add": git_remote_add,
    "git_remote_remove": git_remote_remove,
    "git_fetch": git_fetch,
    "git_pull": git_pull,
    "git_push": git_push,
    
    # Управление ветками и слияние
    "git_merge": git_merge,
    "git_reset": git_reset,
    "git_stash": git_stash,
    
    # Теги
    "git_tag": git_tag,
    "git_tag_list": git_tag_list,
}

def get_git_tools() -> List[Any]:
    """Возвращает список всех Git инструментов."""
    return list(GIT_TOOLS.values())

def get_git_tools_by_names(tool_names: List[str]) -> List[Any]:
    """Возвращает список Git инструментов по их именам."""
    tools = []
    for name in tool_names:
        if name in GIT_TOOLS:
            tools.append(GIT_TOOLS[name])
        else:
            from utils.logger import Logger
            Logger(__name__).warning(f"Git инструмент '{name}' не найден")
    return tools 