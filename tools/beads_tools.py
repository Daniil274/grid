"""
Beads Tools - Integration with the Beads (bd) issue tracker.

This module provides tools for Grid agents to interact with Beads,
a git-backed, dependency-aware graph issue tracker.
"""

import logging
import subprocess
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)

def _get_container_id(context: Any) -> Optional[str]:
    """Extract container_id from context."""
    if hasattr(context, 'context') and hasattr(context.context, 'container_id'):
        return context.context.container_id
    return None

def _map_path_to_container(path: Optional[str], context: Any) -> str:
    """Map a host path to a container path (/workspace)."""
    if not path or path == ".":
        return "/workspace"
    
    # If it's already a container path, return it
    if path.startswith("/workspace"):
        return path
        
    # If it's an absolute host path, try to map it
    if os.path.isabs(path):
        try:
            # We need the host working directory to calculate relative path
            raw = getattr(context, "context", None)
            factory = getattr(raw, "factory", None) if raw else None
            if factory:
                host_wd = factory.config.get_working_directory()
                # Ensure paths are normalized
                norm_path = os.path.normpath(path)
                norm_host_wd = os.path.normpath(host_wd)
                
                if norm_path.startswith(norm_host_wd):
                    rel = os.path.relpath(norm_path, norm_host_wd)
                    if rel == ".":
                        return "/workspace"
                    return (Path("/workspace") / rel).as_posix()
        except Exception:
            pass
            
    # Fallback: if it's relative, assume it's relative to /workspace
    if not os.path.isabs(path):
        return (Path("/workspace") / path).as_posix()
        
    # If path is already /workspace or inside it, return it
    if path.startswith("/workspace"):
        return path

    return "/workspace"

def _run_bd_command(args: List[str], cwd: Optional[str] = None, container_id: Optional[str] = None, context: Any = None) -> Dict[str, Any]:
    """
    Run a 'bd' command and return the result.
    Always adds --json for machine-readable output when possible.
    """
    # Use absolute path to bd if it's not in PATH
    bd_path = "/home/daniil/.local/bin/bd"
    # In container, bd is in /usr/local/bin/bd or just 'bd'
    if container_id:
        bd_path = "/usr/local/bin/bd"
        
    cmd = [bd_path] + args
    
    # Add --json if not already present and likely supported
    # Most bd commands support --json
    if "--json" not in cmd and args and args[0] in ["ready", "create", "show", "update", "list", "search", "close", "agent", "mol", "wisp", "pour"]:
        cmd.append("--json")

    try:
        logger.debug(f"Running beads command: {' '.join(cmd)}")
        
        # Prepare environment
        env = os.environ.copy()
        env["BEADS_DAEMON"] = "0"
        
        if container_id:
            # docker exec -i -w /workspace <container_id> <command>
            workdir = _map_path_to_container(cwd, context)
            docker_cmd = ["docker", "exec", "-i", "-w", workdir, "-e", "BEADS_DAEMON=0", container_id] + cmd
            
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=30
            )
        else:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                env=env,
                timeout=30
            )
        
        success = result.returncode == 0
        output = result.stdout.strip()
        error = result.stderr.strip()
        
        # Try to parse JSON if requested
        data = None
        if "--json" in cmd and output:
            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                # Not valid JSON, might be a warning + JSON or just plain text
                pass

        return {
            "success": success,
            "output": output,
            "error": error,
            "data": data,
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "error": "Command timed out", "data": None, "exit_code": -1}
    except FileNotFoundError as e:
        return {"success": False, "output": "", "error": f"Executable not found: {e}. cmd={cmd if not container_id else docker_cmd}", "data": None, "exit_code": -1}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e), "data": None, "exit_code": -1}

def _extract_factory(context: RunContextWrapper) -> Optional[Any]:
    """Best-effort factory extraction from RunContextWrapper."""
    # Preferred path for Agents SDK function tools:
    # context -> RunContextWrapper, context.context -> GridRunContext
    raw = getattr(context, "context", None)
    if raw is not None:
        factory = getattr(raw, "factory", None)
        if factory is not None:
            return factory

    # Backward-compat fallback for legacy wrappers.
    legacy_factory = getattr(context, "factory", None)
    if legacy_factory is not None:
        return legacy_factory
    return None


def _resolve_directory(context: RunContextWrapper, directory: str) -> str:
    """Resolve directory for bd commands with robust context fallbacks."""
    if directory != ".":
        return os.path.abspath(directory)

    try:
        factory = _extract_factory(context)
        if factory is None or not hasattr(factory, "config"):
            return os.path.abspath(".")

        base_wd = os.path.abspath(factory.config.get_working_directory())
        base_path = Path(base_wd)

        # If .beads already exists in the configured cwd, use it.
        if (base_path / ".beads").exists():
            return base_wd

        # User-scoped fallback: workspace/user_{id}
        raw = getattr(context, "context", None)
        user_id = getattr(raw, "user_id", None) if raw is not None else None
        if user_id:
            user_workspace = base_path / f"user_{user_id}"
            if (user_workspace / ".beads").exists() or user_workspace.exists():
                return str(user_workspace)

        return base_wd
    except Exception as e:
        logger.warning(f"Failed to resolve beads working directory: {e}")
        return os.path.abspath(".")

@function_tool
async def beads_init(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Initialize beads (bd) in the given directory. Idempotent: safe to call if already initialized.
    Creates .beads/ and SQLite DB so beads_ready, beads_create, etc. work in this directory.

    Args:
        directory: Path to the project directory (default: ".")
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    res = _run_bd_command(["init"], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        # "already a beads database" or similar is often success for idempotent init
        if "already" in (res.get("error") or "").lower() or "already" in (res.get("output") or "").lower():
            return "✅ Beads already initialized in this directory."
        return f"❌ Error: {res['error'] or res['output']}"
    return res["output"] or "✅ Beads initialized."


@function_tool
async def beads_ready(context: RunContextWrapper, directory: str = ".") -> str:
    """
    List beads tasks that are ready to be worked on (no open blockers).
    
    Args:
        directory: Path to the project directory (default: ".")
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    res = _run_bd_command(["ready"], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_create(
    context: RunContextWrapper, 
    title: str, 
    priority: int = 1, 
    type: str = "task",
    description: str = "",
    directory: str = "."
) -> str:
    """
    Create a new bead (task, epic, message, etc.).
    
    Args:
        title: Short title of the bead
        priority: Priority (0=P0, 1=P1, 2=P2, 3=P3)
        type: Type of bead (task, epic, message, etc.)
        description: Detailed description
        directory: Path to the project directory
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    args = ["create", title, "-p", str(priority), "-t", type]
    if description:
        args.extend(["--description", description])
    
    res = _run_bd_command(args, cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error creating bead: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_show(context: RunContextWrapper, bead_id: str, directory: str = ".") -> str:
    """
    Show detailed information about a specific bead.
    
    Args:
        bead_id: The ID of the bead (e.g., "bd-a1b2")
        directory: Path to the project directory
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    res = _run_bd_command(["show", bead_id], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error showing bead {bead_id}: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_update(
    context: RunContextWrapper,
    bead_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    claim: bool = False,
    directory: str = "."
) -> str:
    """
    Update an existing bead.
    
    Args:
        bead_id: The ID of the bead
        status: New status (todo, in_progress, blocked, closed)
        title: New title
        description: New description
        notes: Add notes to the bead
        claim: If True, atomically sets assignee to current user and status to in_progress
        directory: Path to the project directory
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    args = ["update", bead_id]
    if claim:
        args.append("--claim")
    if status:
        args.extend(["--status", status])
    if title:
        args.extend(["--title", title])
    if description:
        args.extend(["--description", description])
    if notes:
        args.extend(["--notes", notes])
        
    res = _run_bd_command(args, cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error updating bead {bead_id}: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_close(
    context: RunContextWrapper,
    bead_id: str,
    reason: str = "Completed",
    directory: str = "."
) -> str:
    """
    Close a bead.
    
    Args:
        bead_id: The ID of the bead
        reason: Reason for closing
        directory: Path to the project directory
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    res = _run_bd_command(["close", bead_id, "--reason", reason], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error closing bead {bead_id}: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_sync(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Sync beads with the remote repository (pull, export, commit, push).
    Should be called at the end of a session or after significant changes.
    
    Args:
        directory: Path to the project directory
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    res = _run_bd_command(["sync"], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error syncing beads: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_dep(
    context: RunContextWrapper,
    action: str,
    child_id: str,
    parent_id: str,
    directory: str = "."
) -> str:
    """
    Manage dependencies between beads.
    
    Args:
        action: "add" or "remove"
        child_id: The ID of the child/blocked bead
        parent_id: The ID of the parent/blocker bead
        directory: Path to the project directory
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)
    
    if action not in ["add", "remove"]:
        return "❌ Error: action must be 'add' or 'remove'"
        
    res = _run_bd_command(["dep", action, child_id, parent_id], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error managing dependency: {res['error'] or res['output']}"
    return res["output"]

@function_tool
async def beads_list(
    context: RunContextWrapper,
    all: bool = False,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 15,
    directory: str = ".",
) -> str:
    """
    List beads tasks with optional filters and simple pagination.

    Important:
    - By default returns at most 15 items to keep tool output bounded.
    - Use `page=2` (and so on) to fetch subsequent pages.
    
    Args:
        all: If True, show all issues including closed ones.
        status: Filter by status (open, in_progress, blocked, deferred, closed).
        page: 1-based page number.
        page_size: Items per page (clamped to 1..15).
        directory: Path to the project directory (default: ".")
    """
    directory = _resolve_directory(context, directory)
    container_id = _get_container_id(context)

    # Safety clamps to avoid huge tool outputs.
    try:
        page = int(page)
    except Exception:
        page = 1
    if page < 1:
        page = 1

    try:
        page_size = int(page_size)
    except Exception:
        page_size = 15
    if page_size < 1:
        page_size = 1
    if page_size > 15:
        page_size = 15

    # Fetch full list (JSON) then paginate locally to preserve total count and navigation hints.
    # Sorting newest-first tends to be the most useful for agents.
    args = ["list", "--limit", "0", "--sort", "updated", "--reverse"]
    if all:
        args.append("--all")
    if status:
        args.extend(["--status", status])
    
    res = _run_bd_command(args, cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return f"❌ Error listing beads: {res['error'] or res['output']}"

    issues = res.get("data")
    if issues is None:
        # Fallback: try to parse JSON from stdout (e.g., if warnings preceded JSON).
        try:
            issues = json.loads(res.get("output") or "[]")
        except Exception:
            issues = []

    if not isinstance(issues, list):
        return f"❌ Error listing beads: unexpected output format"

    total = len(issues)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    start = (page - 1) * page_size
    end = start + page_size
    items = issues[start:end] if start < total else []

    payload = {
        "page": page,
        "page_size": page_size,
        "returned": len(items),
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1 and total_pages > 0,
        "has_next": total_pages > 0 and page < total_pages,
        "prev_page": (page - 1) if page > 1 else None,
        "next_page": (page + 1) if total_pages > 0 and page < total_pages else None,
        "items": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

# Registry for easy integration
BEADS_TOOLS = {
    "beads_init": beads_init,
    "beads_ready": beads_ready,
    "beads_list": beads_list,
    "beads_create": beads_create,
    "beads_show": beads_show,
    "beads_update": beads_update,
    "beads_close": beads_close,
    "beads_sync": beads_sync,
    "beads_dep": beads_dep,
}
