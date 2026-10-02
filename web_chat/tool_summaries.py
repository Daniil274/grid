"""Human-readable titles and subtitles for tool steps in the web chat trace.

The reasoning timeline is much easier to scan when a tool step reads like
"Read file — core/config.py" instead of showing the raw tool name and a
flattened JSON blob of its arguments. This module owns that translation.

Two registries drive it:

* :data:`TOOL_TITLES` maps a tool name to a short action heading.
* :data:`TOOL_SUBTITLES` maps a tool name to a callable that pulls the few
  arguments worth showing into a one-line caption.

Both are deliberately best-effort. Unknown tools (MCP servers, sub-agent
``Agent``/``Task`` calls, tool names that only exist on the wire) return
``None`` so the caller falls back to its previous behaviour. A malformed or
partial argument dict never raises - the subtitle is simply dropped.

Curated names cover two sources: the model-agent toolbox (``grid/tools``) and
the chat runtime's own tool calls (``bash_tool``, ``file_read``,
``glob_tool``, ``Agent`` ...) - the calls users actually watch in the main
timeline.

Only the *content* of ``title``/``subtitle`` is decided here; the wire format of
:class:`~web_chat.trace.Step` and the raw ``tool`` field (used for policy badge
matching) are left untouched.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from web_chat.trace import clip

#: Longest subtitle kept for a tool step; the UI renders it on one line.
_SUBTITLE_LIMIT = 120


def _val(data: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among ``keys`` (or ``None``)."""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str:
    """Collapse any value to a single whitespace-normalised line."""
    return " ".join(str(value).split())


def _as_dict(args: Any) -> dict[str, Any]:
    """Tool arguments arrive as a dict or a JSON string; normalise to a dict."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _joined(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(item) for item in value if item not in (None, ""))
    return _text(value)


def _line_count(content: Any) -> int:
    text = str(content)
    return text.count("\n") + 1 if text else 0


def _lines_label(count: int) -> str:
    return f"{count} line" if count == 1 else f"{count} lines"


# --------------------------------------------------------------------------
# Subtitle formatters (name -> callable(args: dict) -> str | None)
# --------------------------------------------------------------------------
def _read_file(a: dict) -> Optional[str]:
    path = _val(a, "filepath", "path", "file")
    return f"{_text(path)}" if path else None


def _write_file(a: dict) -> Optional[str]:
    path = _val(a, "filepath", "path", "file")
    if not path:
        return None
    content = _val(a, "content", "text", "data")
    if content is None:
        return _text(path)
    return f"{_text(path)} · {_lines_label(_line_count(content))}"


def _edit_file(a: dict) -> Optional[str]:
    path = _val(a, "filepath", "path", "file")
    if not path:
        return None
    patch = _val(a, "patch_content", "patch")
    if patch is None:
        return _text(path)
    return f"{_text(path)} · {_line_count(patch)}-line patch"


def _replace_in_file(a: dict) -> Optional[str]:
    path = _val(a, "filepath", "path", "file")
    if not path:
        return None
    old = _val(a, "old_text", "old")
    if old is None:
        return _text(path)
    return f'{_text(path)} · "{clip(_text(old), 40)}"'


def _list_files(a: dict) -> Optional[str]:
    directory = _val(a, "directory", "path", "dir")
    return _text(directory) if directory else None


def _search_files(a: dict) -> Optional[str]:
    pattern = _val(a, "pattern", "query", "q")
    directory = _val(a, "directory", "path", "dir")
    if pattern and directory:
        return f"{_text(pattern)} in {_text(directory)}"
    if pattern:
        return _text(pattern)
    return None


def _search_content(a: dict) -> Optional[str]:
    query = _val(a, "query", "q", "pattern")
    path = _val(a, "filepath", "path", "file")
    if query and path:
        return f'"{_text(query)}" in {_text(path)}'
    if query:
        return f'"{_text(query)}"'
    return None


def _git_log(a: dict) -> Optional[str]:
    directory = _val(a, "directory", "path")
    if directory:
        return f"in {_text(directory)}"
    return None


def _orchestrate(a: dict) -> Optional[str]:
    task = _val(a, "task", "goal", "prompt")
    return _text(task) if task else None


def _pipeline_task(a: dict) -> Optional[str]:
    goal = _val(a, "goal", "task")
    kind = _val(a, "kind")
    if goal and kind:
        return f"{_text(kind)}: {_text(goal)}"
    if goal:
        return _text(goal)
    return None


def _pipeline_wait(a: dict) -> Optional[str]:
    ids = _val(a, "task_ids", "tasks")
    if ids:
        return _joined(ids) or None
    return None


def _pipeline_review(a: dict) -> Optional[str]:
    task_id = _val(a, "task_id", "id")
    decision = _val(a, "decision", "verdict")
    if task_id and decision:
        return f"{_text(task_id)}: {_text(decision)}"
    if task_id:
        return _text(task_id)
    return None


def _task_feedback(a: dict) -> Optional[str]:
    task_id = _val(a, "task_id", "id")
    note = _val(a, "feedback", "reason", "message")
    if task_id and note:
        return f"{_text(task_id)} · {_text(note)}"
    if task_id:
        return _text(task_id)
    return None


def _pipeline_finish(a: dict) -> Optional[str]:
    summary = _val(a, "summary", "reason", "message")
    return _text(summary) if summary else None


def _system_file(a: dict) -> Optional[str]:
    system = _val(a, "system")
    path = _val(a, "path", "file", "config_path", "tool")
    if system and path:
        return f"{_text(system)}/{_text(path)}"
    return _text(system) if system else (_text(path) if path else None)


def _builder_create(a: dict) -> Optional[str]:
    key = _val(a, "key", "system")
    name = _val(a, "name")
    if key and name:
        return f"{_text(key)} ({_text(name)})"
    return _text(key) if key else None


def _builder_fork(a: dict) -> Optional[str]:
    system = _val(a, "system")
    key = _val(a, "key")
    if system and key:
        return f"{_text(system)} → {_text(key)}"
    return _text(system) if system else None


def _builder_compare(a: dict) -> Optional[str]:
    baseline = _val(a, "baseline")
    candidate = _val(a, "candidate")
    if baseline and candidate:
        return f"{_text(baseline)} vs {_text(candidate)}"
    return _text(baseline or candidate) if (baseline or candidate) else None


def _beads_create(a: dict) -> Optional[str]:
    title = _val(a, "title")
    if not title:
        return None
    bits = [_text(title)]
    kind = _val(a, "type")
    priority = _val(a, "priority")
    if kind is not None and priority is not None:
        bits.append(f"({_text(kind)}, P{_text(priority)})")
    elif kind is not None:
        bits.append(f"({_text(kind)})")
    return " ".join(bits)


def _beads_update(a: dict) -> Optional[str]:
    bead_id = _val(a, "bead_id", "id")
    if not bead_id:
        return None
    status = _val(a, "status")
    if status:
        return f"{_text(bead_id)} → {_text(status)}"
    return _text(bead_id)


def _beads_close(a: dict) -> Optional[str]:
    bead_id = _val(a, "bead_id", "id")
    if not bead_id:
        return None
    reason = _val(a, "reason")
    if reason:
        return f"{_text(bead_id)} · {_text(reason)}"
    return _text(bead_id)


def _beads_dep(a: dict) -> Optional[str]:
    action = _val(a, "action")
    child = _val(a, "child_id")
    parent = _val(a, "parent_id")
    if child and parent:
        prefix = f"{_text(action)}: " if action else ""
        return f"{prefix}{_text(child)} ← {_text(parent)}"
    return _text(action) if action else None


def _beads_list(a: dict) -> Optional[str]:
    status = _val(a, "status")
    if status:
        return f"status: {_text(status)}"
    if a.get("all"):
        return "all tasks"
    return None


def _beads_log_append(a: dict) -> Optional[str]:
    channel = _val(a, "channel")
    if not channel:
        return None
    author = _val(a, "author")
    return f"{_text(author)} → {_text(channel)}" if author else _text(channel)


def _control_submit(a: dict) -> Optional[str]:
    task_id = _val(a, "task_id", "id")
    message = _val(a, "message", "summary")
    if task_id and message:
        return f"{_text(task_id)} · {_text(message)}"
    if message:
        return _text(message)
    return _text(task_id) if task_id else None


def _control_trial(a: dict) -> Optional[str]:
    reps = _val(a, "repetitions")
    if reps is not None:
        return f"{_text(reps)} repetitions"
    return None


def _control_submit_task(a: dict) -> Optional[str]:
    return _text(_val(a, "task_id", "bead_id", "experiment_id", "id") or "") or None


def _no_subtitle(a: dict) -> Optional[str]:
    """Tools that take no meaningful arguments (empty caption is expected)."""
    return None


def _control_revert(a: dict) -> Optional[str]:
    paths = _val(a, "paths", "path")
    if paths is None:
        return None
    return _joined(paths) or None


def _crop_image(a: dict) -> Optional[str]:
    left = a.get("left")
    top = a.get("top")
    right = a.get("right")
    bottom = a.get("bottom")
    box = ""
    if None not in (left, top, right, bottom):
        box = f" [{_text(left)},{_text(top)},{_text(right)},{_text(bottom)}]"
    target = _val(a, "image_path", "path", "image")
    if target:
        return f"{_text(target)}{box}"
    return f"image{box}" if box else None


def _first_line(a: dict, *keys: str) -> Optional[str]:
    value = _val(a, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text.splitlines()[0].strip() if text else None


def _run_command(a: dict) -> Optional[str]:
    return _first_line(a, "command", "cmd", "script", "shell_command")


def _target_object(a: dict) -> Optional[str]:
    target = _val(a, "target", "object", "object_name", "screen", "region")
    return _text(target) if target else None


def _grep_text(a: dict) -> Optional[str]:
    pattern = _val(a, "pattern", "query", "q")
    directory = _val(a, "directory", "dir")
    if pattern and directory:
        return f'"{_text(pattern)}" in {_text(directory)}'
    if pattern:
        return f'"{_text(pattern)}"'
    return None


def _web_search(a: dict) -> Optional[str]:
    return _first_line(a, "query", "q")


def _web_fetch(a: dict) -> Optional[str]:
    return _first_line(a, "url", "link")


def _notebook_cell(a: dict) -> Optional[str]:
    path = _val(a, "filepath", "path", "file")
    cell = _val(a, "cell_index", "cell")
    if path and cell is not None:
        return f"{_text(path)} · cell {_text(cell)}"
    return _read_file(a)


def _delegate_brief(a: dict) -> Optional[str]:
    return _first_line(a, "input", "task", "message", "prompt", "goal")


def _codegraph_query(a: dict) -> Optional[str]:
    query = _val(a, "query", "symbol", "symbols", "name")
    if query is not None:
        if isinstance(query, (list, tuple)):
            return _joined(query) or None
        return _text(query)
    project = _val(a, "projectPath", "path", "project")
    return _text(project) if project else None


def _todo_list(a: dict) -> Optional[str]:
    todos = _val(a, "todos", "items")
    if isinstance(todos, (list, tuple)) and todos:
        return f"{len(todos)} tasks"
    return None


#: Tool name -> short action heading shown as the step title.
TOOL_TITLES: dict[str, str] = {
    # chat runtime (main session) tools - the calls users watch in the timeline
    "bash_tool": "Run command",
    "file_read": "Read file",
    "file_write": "Write file",
    "file_edit": "Edit file",
    "file_append": "Append to file",
    "glob_tool": "Find files",
    "grep_tool": "Search text",
    "web_search": "Web search",
    "web_fetch": "Fetch page",
    "notebook_read": "Read notebook",
    "notebook_edit": "Edit notebook",
    "notebook_create": "Create notebook",
    "Agent": "Delegate to agent",
    "WebSpider": "Web research",
    "codegraph_explore": "Explore code graph",
    "codegraph_search": "Search symbols",
    "codegraph_context": "Code context",
    "codegraph_callers": "Find callers",
    "codegraph_callees": "Find callees",
    "codegraph_impact": "Impact analysis",
    "codegraph_node": "Symbol details",
    "codegraph_files": "Indexed files",
    "codegraph_status": "Index status",
    "todo_write": "Update todo list",
    # files
    "read_file": "Read file",
    "write_file": "Write file",
    "append_file": "Append to file",
    "edit_file_patch": "Edit file",
    "replace_in_file": "Replace in file",
    "delete_file": "Delete file",
    "list_files": "List files",
    "search_files": "Search files",
    "search_content": "Search file content",
    # git
    "git_log": "Git log",
    # orchestration / pipeline
    "orchestrate": "Delegate to agent",
    "pipeline_start": "Start pipeline",
    "pipeline_task": "Queue pipeline task",
    "pipeline_wait": "Wait for workers",
    "pipeline_inspect": "Inspect pipeline",
    "pipeline_review": "Review worker output",
    "pipeline_retry": "Retry worker task",
    "pipeline_interrupt": "Interrupt worker",
    "pipeline_finish": "Finish pipeline",
    "pipeline_block": "Block pipeline",
    # system builder
    "builder_catalog": "Browse builder catalog",
    "builder_tool_set": "Show tool set",
    "builder_read": "Read system file",
    "builder_check": "Check system",
    "builder_create": "Create system draft",
    "builder_write": "Write system file",
    "builder_delete": "Delete system file",
    "builder_describe": "Describe system",
    "builder_test_tools": "Test system tool",
    "builder_evaluate": "Evaluate system",
    "builder_fork": "Fork system",
    "builder_compare": "Compare systems",
    # grid systems
    "grid_systems_catalog": "List systems",
    "grid_check_system": "Check system config",
    # beads
    "beads_init": "Initialize beads",
    "beads_ready": "List ready beads",
    "beads_create": "Create bead",
    "beads_show": "Show bead",
    "beads_update": "Update bead",
    "beads_close": "Close bead",
    "beads_sync": "Sync beads",
    "beads_dep": "Link bead dependency",
    "beads_list": "List beads",
    "beads_log_append": "Append to bead log",
    "beads_log_read": "Read bead log",
    # control
    "control_begin": "Begin experiment",
    "control_tasks": "List review tasks",
    "control_take_task": "Take review task",
    "control_submit": "Submit experiment",
    "control_trial": "Run trial",
    "control_diff": "Review experiment diff",
    "control_revert": "Revert experiment change",
    "control_scenarios": "List scenarios",
    "control_status": "Check experiment status",
    # vision
    "crop_image": "Crop image",
    # common shell / screen tools (robustness; not in the local registry)
    "run_command": "Run command",
    "bash": "Run command",
    "shell": "Run command",
    "screenshot": "Take screenshot",
    "perceive": "Perceive screen",
}

#: Tool name -> caption builder from the tool's arguments.
TOOL_SUBTITLES: dict[str, Callable[[dict[str, Any]], Optional[str]]] = {
    # files
    "read_file": _read_file,
    "write_file": _write_file,
    "append_file": _write_file,
    "edit_file_patch": _edit_file,
    "replace_in_file": _replace_in_file,
    "delete_file": _read_file,
    "list_files": _list_files,
    "search_files": _search_files,
    "search_content": _search_content,
    # git
    "git_log": _git_log,
    "pipeline_inspect": _no_subtitle,
    # orchestration / pipeline
    "orchestrate": _orchestrate,
    "pipeline_start": _orchestrate,
    "pipeline_task": _pipeline_task,
    "pipeline_wait": _pipeline_wait,
    "pipeline_review": _pipeline_review,
    "pipeline_retry": _task_feedback,
    "pipeline_interrupt": _task_feedback,
    "pipeline_finish": _pipeline_finish,
    "pipeline_block": _pipeline_finish,
    # system builder
    "builder_catalog": _no_subtitle,
    "builder_tool_set": _system_file,
    "builder_read": _system_file,
    "builder_check": _system_file,
    "builder_create": _builder_create,
    "builder_write": _system_file,
    "builder_delete": _system_file,
    "builder_describe": _system_file,
    "builder_test_tools": _system_file,
    "builder_evaluate": _system_file,
    "builder_fork": _builder_fork,
    "builder_compare": _builder_compare,
    # grid systems
    "grid_systems_catalog": _no_subtitle,
    "grid_check_system": _system_file,
    # beads
    "beads_init": _list_files,
    "beads_ready": _list_files,
    "beads_create": _beads_create,
    "beads_show": _control_submit_task,
    "beads_update": _beads_update,
    "beads_close": _beads_close,
    "beads_sync": _list_files,
    "beads_dep": _beads_dep,
    "beads_list": _beads_list,
    "beads_log_append": _beads_log_append,
    "beads_log_read": _beads_log_append,
    # control
    "control_begin": _no_subtitle,
    "control_tasks": _no_subtitle,
    "control_scenarios": _no_subtitle,
    "control_take_task": _control_submit_task,
    "control_submit": _control_submit,
    "control_trial": _control_trial,
    "control_diff": _read_file,
    "control_revert": _control_revert,
    "control_status": _control_submit_task,
    # vision
    "crop_image": _crop_image,
    # common shell / screen tools
    "run_command": _run_command,
    "bash": _run_command,
    "shell": _run_command,
    "screenshot": _target_object,
    "perceive": _target_object,
    # chat runtime (main session) tools
    "bash_tool": _run_command,
    "file_read": _read_file,
    "file_write": _write_file,
    "file_edit": _edit_file,
    "file_append": _write_file,
    "glob_tool": _search_files,
    "grep_tool": _grep_text,
    "web_search": _web_search,
    "web_fetch": _web_fetch,
    "notebook_read": _read_file,
    "notebook_edit": _notebook_cell,
    "notebook_create": _read_file,
    "Agent": _delegate_brief,
    "WebSpider": _delegate_brief,
    "codegraph_explore": _codegraph_query,
    "codegraph_search": _codegraph_query,
    "codegraph_context": _codegraph_query,
    "codegraph_callers": _codegraph_query,
    "codegraph_callees": _codegraph_query,
    "codegraph_impact": _codegraph_query,
    "codegraph_node": _codegraph_query,
    "codegraph_files": _codegraph_query,
    "codegraph_status": _codegraph_query,
    "todo_write": _todo_list,
}


def tool_title(name: Any, server_label: Optional[str] = None) -> Optional[str]:
    """Human heading for a tool, or ``None`` to keep the caller's fallback.

    Curated titles win even when the call arrives through an MCP server (the
    names are ours); unknown MCP tools keep the ``server.name`` provenance.
    """
    if not name:
        return None
    known = TOOL_TITLES.get(str(name))
    if known:
        return known
    if server_label:
        return f"{server_label}.{name}"
    return None


def tool_subtitle(name: Any, args: Any) -> Optional[str]:
    """One-line caption from a tool's arguments, or ``None`` for a fallback.

    ``args`` may be a dict, a JSON string or nothing; any error yields ``None``
    rather than propagating into the trace.
    """
    if not name:
        return None
    formatter = TOOL_SUBTITLES.get(str(name))
    if formatter is None:
        return None
    # A genuinely argument-less call gets an empty caption (nothing to show);
    # any malformed or irrelevant payload still falls back to flattened JSON.
    if args in (None, "", "{}", {}):
        return ""
    try:
        data = _as_dict(args)
        text = formatter(data)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return None
    if not text:
        return None
    return clip(text, _SUBTITLE_LIMIT)
