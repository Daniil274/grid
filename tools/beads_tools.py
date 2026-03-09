"""
Beads Tools - Integration with the Beads (bd) issue tracker.

This module provides tools for Grid agents to interact with Beads,
a git-backed, dependency-aware graph issue tracker.
"""

import logging
import subprocess
import json
import os
import shutil
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)

def _get_isolation_image(context: Any) -> str:
    """
    Best-effort resolve Docker image to run bd in.
    Priority:
    - config.yaml isolation.image (if available via AgentFactory in context)
    - env GRID_ISOLATION_IMAGE / GRID_AGENT_IMAGE
    - default "grid-agent:latest"
    """
    try:
        factory = None
        raw = getattr(context, "context", None)
        factory = getattr(raw, "factory", None) if raw else None
        cfg = getattr(factory, "config", None) if factory else None
        isolation_cfg = getattr(getattr(cfg, "config", None), "isolation", None) if cfg else None
        if isolation_cfg:
            img = getattr(isolation_cfg, "image", None) if not isinstance(isolation_cfg, dict) else isolation_cfg.get("image")
            if img:
                return str(img)
    except Exception:
        pass
    return os.environ.get("GRID_ISOLATION_IMAGE") or os.environ.get("GRID_AGENT_IMAGE") or "grid-agent:latest"

def _get_container_id(context: Any) -> Optional[str]:
    """Extract container_id from context."""
    if hasattr(context, 'context') and hasattr(context.context, 'container_id'):
        return context.context.container_id
    return None

# Container path for workspace (Docker forbids bind to "/"). Agent sees root as "/".
_CONTAINER_ROOT = "/workspace"

def _map_path_to_container(path: Optional[str], context: Any) -> str:
    """Map a host or agent path to the container path (agent root is "/", container uses _CONTAINER_ROOT)."""
    if not path or path == "." or path == "/":
        return _CONTAINER_ROOT

    # If it's already the container path, return it
    if path.startswith(_CONTAINER_ROOT + "/") or path == _CONTAINER_ROOT:
        return path

    # Agent may send paths as "/file" (root is "/")
    if path.startswith("/"):
        return (_CONTAINER_ROOT + path).replace("//", "/")

    # If it's an absolute host path, try to map it
    if os.path.isabs(path):
        try:
            raw = getattr(context, "context", None)
            factory = getattr(raw, "factory", None) if raw else None
            if factory:
                host_wd = factory.config.get_working_directory()
                norm_path = os.path.normpath(path)
                norm_host_wd = os.path.normpath(host_wd)
                if norm_path.startswith(norm_host_wd):
                    rel = os.path.relpath(norm_path, norm_host_wd)
                    if rel == ".":
                        return _CONTAINER_ROOT
                    return (Path(_CONTAINER_ROOT) / rel).as_posix()
        except Exception:
            pass

    # Relative path: assume relative to container root
    return (Path(_CONTAINER_ROOT) / path).as_posix()


def _find_bd(container_id: Optional[str] = None) -> str:
    """
    Resolve path to bd CLI. In container use fixed path; on host use
    BEADS_BD_PATH, ~/.local/bin/bd, or bd from PATH (works on Windows when bd is in PATH).
    """
    if container_id:
        return "/usr/local/bin/bd"
    path = os.environ.get("BEADS_BD_PATH")
    if path and os.path.isfile(path):
        return path
    home_bd = Path.home() / ".local" / "bin" / "bd"
    if home_bd.exists():
        return str(home_bd)
    return shutil.which("bd") or "bd"

def _run_bd_via_docker_run(args: List[str], cwd: str, context: Any) -> Dict[str, Any]:
    """
    Run bd inside a short-lived container (no Docker SDK required).
    Mounts the provided cwd as container root.
    """
    image = _get_isolation_image(context)
    host_cwd = os.path.abspath(cwd) if cwd else os.path.abspath(".")
    # Docker on Windows generally accepts forward slashes in volume specs.
    host_cwd_vol = host_cwd.replace("\\", "/")

    bd_path = "/usr/local/bin/bd"
    cmd = ["docker", "run", "--rm", "-i", "-e", "BEADS_DAEMON=0", "-v", f"{host_cwd_vol}:{_CONTAINER_ROOT}", "-w", _CONTAINER_ROOT, image, bd_path] + args

    # Add --json if not already present and likely supported
    if "--json" not in cmd and args and args[0] in ["ready", "create", "show", "update", "list", "search", "close", "agent", "mol", "wisp", "pour"]:
        cmd.append("--json")

    try:
        logger.debug(f"Running beads via docker: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

        success = result.returncode == 0
        output = result.stdout.strip()
        error = result.stderr.strip()

        # Friendly error when isolation image isn't built locally.
        if not success:
            err_low = (error or "").lower()
            if ("unable to find image" in err_low) or ("no such image" in err_low) or ("pull access denied" in err_low):
                build_hint = (
                    f"Docker image '{image}' not found locally.\n"
                    f"Build it in repo root:\n"
                    f"  docker build -t {image} -f Dockerfile .\n"
                    f"Or set GRID_ISOLATION_IMAGE to an existing image name."
                )
                return {
                    "success": False,
                    "output": output,
                    "error": build_hint + (f"\n\nDocker error: {error}" if error else ""),
                    "data": None,
                    "exit_code": result.returncode,
                }

        data = None
        if "--json" in cmd and output:
            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                pass

        return {
            "success": success,
            "output": output,
            "error": error,
            "data": data,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "error": "Command timed out", "data": None, "exit_code": -1}
    except FileNotFoundError as e:
        return {"success": False, "output": "", "error": f"Docker executable not found: {e}", "data": None, "exit_code": -1}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e), "data": None, "exit_code": -1}


def _run_bd_command(args: List[str], cwd: Optional[str] = None, container_id: Optional[str] = None, context: Any = None) -> Dict[str, Any]:
    """
    Run a 'bd' command and return the result.
    Always adds --json for machine-readable output when possible.
    """
    bd_path = _find_bd(container_id)
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
            # docker exec -i -w <container_root> <container_id> <command>
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
        # If we're not already in a managed container, fall back to running bd in Docker.
        # This avoids requiring bd on the Windows host.
        if not container_id and os.environ.get("BEADS_DISABLE_DOCKER_FALLBACK", "0") not in ("1", "true", "yes"):
            try:
                effective_cwd = cwd or os.path.abspath(".")
                return _run_bd_via_docker_run(args, cwd=effective_cwd, context=context)
            except Exception:
                pass
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


# ============================================================================
# Shared append-only logs (for games / chat-like streams)
# ============================================================================

_LOG_ROOT_DIRNAME = "shared_log"
_LOG_CURSOR_FILENAME = "cursors.json"


def _safe_filename(name: str) -> str:
    """Conservatively map an arbitrary name to a safe filename stem."""
    if not name:
        return "default"
    out = []
    for ch in str(name):
        if ch.isalnum() or ch in ("-", "_", ".", "@"):
            out.append(ch)
        else:
            out.append("_")
    stem = "".join(out).strip("._")
    return stem or "default"


def _log_root(directory: str) -> Path:
    """Return path to the shared log root inside .beads/."""
    base = Path(directory)
    beads_dir = base / ".beads"
    beads_dir.mkdir(parents=True, exist_ok=True)
    root = beads_dir / _LOG_ROOT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _log_path(directory: str, channel: str) -> Path:
    """Path to the append-only JSONL log file for a channel."""
    root = _log_root(directory)
    return root / f"{_safe_filename(channel)}.jsonl"


def _cursor_path(directory: str) -> Path:
    """Path to the cursor storage JSON file."""
    root = _log_root(directory)
    return root / _LOG_CURSOR_FILENAME


def _load_cursors(path: Path) -> Dict[str, Dict[str, int]]:
    """Load cursor map: {channel: {reader: byte_offset}}."""
    try:
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            # normalize to int offsets
            out: Dict[str, Dict[str, int]] = {}
            for ch, readers in data.items():
                if not isinstance(readers, dict):
                    continue
                out[str(ch)] = {}
                for r, off in readers.items():
                    try:
                        out[str(ch)][str(r)] = int(off)
                    except Exception:
                        continue
            return out
    except Exception:
        pass
    return {}


def _save_cursors(path: Path, cursors: Dict[str, Dict[str, int]]) -> None:
    path.write_text(json.dumps(cursors, ensure_ascii=False, indent=2), encoding="utf-8")


@function_tool
async def beads_log_append(
    context: RunContextWrapper,
    channel: str,
    author: str,
    text: str,
    directory: str = ".",
) -> str:
    """
    Append a single entry to a shared, append-only JSONL log (inside .beads/shared_log/).
    Intended for chat-like streams (e.g., game public log) to avoid rewriting previous text.

    Args:
        channel: Logical channel name (e.g., "GAME", "MAFIA", "public")
        author: Author identifier (e.g., "GM", "Agent_A")
        text: Message body (one entry)
        directory: Working directory (defaults to auto-resolved ".")
    """
    directory = _resolve_directory(context, directory)
    path = _log_path(directory, channel)

    entry = {
        "ts": time.time(),
        "author": str(author),
        "text": str(text),
    }

    try:
        # Ensure parent exists
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)
            f.write("\n")
            f.flush()
        return json.dumps(
            {"success": True, "channel": channel, "bytes_appended": len(line) + 1},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@function_tool
async def beads_log_read(
    context: RunContextWrapper,
    channel: str,
    reader: str,
    max_bytes: int = 6000,
    from_start: bool = False,
    reset_to_end: bool = False,
    directory: str = ".",
) -> str:
    """
    Read ONLY new log data for (channel, reader) since the last read.
    Tracks per-reader cursor (byte offset) in .beads/shared_log/cursors.json.

    Args:
        channel: Logical channel name
        reader: Reader identifier (e.g., "GM", "Agent_A")
        max_bytes: Maximum bytes to return (soft cap; helps keep outputs bounded)
        from_start: If True, ignore stored cursor and read from beginning
        reset_to_end: If True, set cursor to end of file and return empty (useful to "mark as read")
        directory: Working directory (defaults to auto-resolved ".")
    """
    directory = _resolve_directory(context, directory)
    log_path = _log_path(directory, channel)
    cur_path = _cursor_path(directory)

    # Clamp max_bytes to keep tool output safe.
    try:
        max_bytes = int(max_bytes)
    except Exception:
        max_bytes = 6000
    if max_bytes < 256:
        max_bytes = 256
    if max_bytes > 20000:
        max_bytes = 20000

    if not log_path.exists():
        return json.dumps(
            {
                "success": True,
                "channel": channel,
                "reader": reader,
                "new_data": "",
                "truncated": False,
                "cursor_before": 0,
                "cursor_after": 0,
                "log_exists": False,
            },
            ensure_ascii=False,
        )

    cursors = _load_cursors(cur_path)
    ch_key = str(channel)
    rd_key = str(reader)
    stored = cursors.get(ch_key, {}).get(rd_key, 0)

    cursor_before = 0 if from_start else int(stored or 0)

    try:
        file_size = log_path.stat().st_size
    except Exception:
        file_size = None

    if reset_to_end:
        end_pos = file_size if isinstance(file_size, int) else 0
        cursors.setdefault(ch_key, {})[rd_key] = int(end_pos)
        try:
            _save_cursors(cur_path, cursors)
        except Exception:
            pass
        return json.dumps(
            {
                "success": True,
                "channel": channel,
                "reader": reader,
                "new_data": "",
                "truncated": False,
                "cursor_before": cursor_before,
                "cursor_after": int(end_pos),
                "reset_to_end": True,
            },
            ensure_ascii=False,
        )

    try:
        with log_path.open("rb") as f:
            # Guard against cursor beyond EOF (e.g., log rotated/cleared)
            if isinstance(file_size, int) and cursor_before > file_size:
                cursor_before = 0
            f.seek(cursor_before)
            data = f.read(max_bytes + 1)  # +1 to detect truncation
            truncated = len(data) > max_bytes
            if truncated:
                data = data[:max_bytes]
            cursor_after = cursor_before + len(data)

        try:
            new_text = data.decode("utf-8", errors="replace")
        except Exception:
            new_text = ""

        # Persist cursor.
        cursors.setdefault(ch_key, {})[rd_key] = int(cursor_after)
        try:
            _save_cursors(cur_path, cursors)
        except Exception:
            pass

        return json.dumps(
            {
                "success": True,
                "channel": channel,
                "reader": reader,
                "new_data": new_text,
                "truncated": bool(truncated),
                "cursor_before": int(cursor_before),
                "cursor_after": int(cursor_after),
                "log_exists": True,
                "log_size": int(file_size) if isinstance(file_size, int) else None,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


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
    # Shared append-only log helpers (useful for games / chat streams)
    "beads_log_append": beads_log_append,
    "beads_log_read": beads_log_read,
}
