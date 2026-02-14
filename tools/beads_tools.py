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

def _run_bd_command(args: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """
    Run a 'bd' command and return the result.
    Always adds --json for machine-readable output when possible.
    """
    # Use absolute path to bd if it's not in PATH
    bd_path = "/home/daniil/.local/bin/bd"
    cmd = [bd_path] + args
    
    # Add --json if not already present and likely supported
    # Most bd commands support --json
    if "--json" not in cmd and args and args[0] in ["ready", "create", "show", "update", "list", "search", "close", "agent", "mol", "wisp", "pour"]:
        cmd.append("--json")

    try:
        logger.debug(f"Running beads command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding='utf-8',
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
    except FileNotFoundError:
        return {"success": False, "output": "", "error": "'bd' CLI not found. Please install it first.", "data": None, "exit_code": -1}
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
    res = _run_bd_command(["init"], cwd=directory)
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
    res = _run_bd_command(["ready"], cwd=directory)
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
    args = ["create", title, "-p", str(priority), "-t", type]
    if description:
        args.extend(["--description", description])
    
    res = _run_bd_command(args, cwd=directory)
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
    res = _run_bd_command(["show", bead_id], cwd=directory)
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
        
    res = _run_bd_command(args, cwd=directory)
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
    res = _run_bd_command(["close", bead_id, "--reason", reason], cwd=directory)
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
    res = _run_bd_command(["sync"], cwd=directory)
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
    if action not in ["add", "remove"]:
        return "❌ Error: action must be 'add' or 'remove'"
        
    res = _run_bd_command(["dep", action, child_id, parent_id], cwd=directory)
    if not res["success"]:
        return f"❌ Error managing dependency: {res['error'] or res['output']}"
    return res["output"]

# Registry for easy integration
BEADS_TOOLS = {
    "beads_init": beads_init,
    "beads_ready": beads_ready,
    "beads_create": beads_create,
    "beads_show": beads_show,
    "beads_update": beads_update,
    "beads_close": beads_close,
    "beads_sync": beads_sync,
    "beads_dep": beads_dep,
}
