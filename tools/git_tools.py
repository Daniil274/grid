"""
Git tools for agents: read-only commit history (git_log).
"""

import subprocess
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool, RunContextWrapper
from utils.logger import Logger
from utils.tool_requirements import Requires
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
def git_log(context: RunContextWrapper, directory: str = ".", max_commits: int = 10) -> str:
    """
    Shows the commit history.
    
    Args:
        directory: Path to the repository
        max_commits: Maximum number of commits to display
        
    Returns:
        str: Commit history
    """
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
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ERROR getting history: {str(e)}"
        log_tool_result(operation, error=result)
        return result


TOOL_REQUIREMENTS = {
    "git_log": Requires(programs=("git",), hint="Install Git and put it on PATH"),
}

GIT_TOOLS = {
    "git_log": git_log,
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