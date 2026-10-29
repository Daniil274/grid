"""Human-readable titles/subtitles for tool steps in the reasoning timeline."""

import pytest

from web_chat.tool_summaries import (
    TOOL_SUBTITLES,
    TOOL_TITLES,
    tool_subtitle,
    tool_title,
)


def test_unknown_tool_has_no_title_or_subtitle():
    # Unknown tools must fall back to the caller's previous behaviour.
    assert tool_title("mystery_tool") is None
    assert tool_subtitle("mystery_tool", {"anything": 1}) is None


def test_mcp_tool_keeps_server_name_and_falls_back():
    assert tool_title("run", "codex") == "codex.run"
    assert tool_title("edit", "codex") == "codex.edit"
    # No local formatter exists for MCP tools, so the subtitle falls back.
    assert tool_subtitle("run", {"cmd": "ls"}) is None


def test_known_tool_title_beats_server_label():
    # Curated titles win even when the call arrives through an MCP server;
    # unknown MCP tools keep the server.name provenance form.
    assert tool_title("bash_tool", "sandbox") == "Run command"
    assert tool_title("file_read", "codex") == "Read file"


def test_empty_name_has_no_title():
    assert tool_title(None) is None
    assert tool_title("") is None
    assert tool_subtitle(None, {}) is None


@pytest.mark.parametrize("empty", [None, "", "{}", {}])
def test_argument_less_call_has_empty_not_fallback_caption(empty):
    # No arguments means no caption, rather than a literal "{}" on screen.
    assert tool_subtitle("pipeline_inspect", empty) == ""
    assert tool_subtitle("read_file", empty) == ""


def test_known_tool_with_irrelevant_arguments_falls_back():
    # A payload the formatter cannot use must not be swallowed into "".
    assert tool_subtitle("pipeline_inspect", {"unexpected": 1}) is None


@pytest.mark.parametrize(
    "name, args, title, subtitle",
    [
        ("read_file", {"filepath": "core/config.py"}, "Read file", "core/config.py"),
        # The wire sometimes uses the shorter ``path`` key.
        ("read_file", {"path": "README.md"}, "Read file", "README.md"),
        (
            "write_file",
            {"filepath": "out.txt", "content": "a\nb\nc"},
            "Write file",
            "out.txt · 3 lines",
        ),
        ("delete_file", {"filepath": "old.py"}, "Delete file", "old.py"),
        (
            "search_files",
            {"pattern": "grid", "directory": "core"},
            "Search files",
            "grid in core",
        ),
        (
            "search_content",
            {"query": "def main", "filepath": "grid.py"},
            "Search file content",
            '"def main" in grid.py',
        ),
        ("list_files", {"directory": "tests"}, "List files", "tests"),
        ("run_command", {"command": "pytest -q\nmore output"}, "Run command", "pytest -q"),
        (
            "orchestrate",
            {"task": "Investigate the failing test"},
            "Delegate to agent",
            "Investigate the failing test",
        ),
        ("git_log", {"directory": "core"}, "Git log", "in core"),
        ("crop_image", {"image_path": "shot.png", "left": 0, "top": 0, "right": 10, "bottom": 20},
         "Crop image", "shot.png [0,0,10,20]"),
        # chat runtime (main session) tools
        ("bash_tool", {"command": "pytest -q grid/tests", "working_dir": "."},
         "Run command", "pytest -q grid/tests"),
        ("file_read", {"filepath": "grid/web_chat/observer.py", "offset": 200},
         "Read file", "grid/web_chat/observer.py"),
        ("file_edit", {"filepath": "a.py", "patch_content": "+x\n+y"},
         "Edit file", "a.py · 2-line patch"),
        ("glob_tool", {"pattern": "*.py", "directory": "grid"},
         "Find files", "*.py in grid"),
        ("grep_tool", {"pattern": "def main", "directory": "grid"},
         "Search text", '"def main" in grid'),
        ("web_search", {"query": "sse streaming best practices"},
         "Web search", "sse streaming best practices"),
        ("web_fetch", {"url": "https://example.com/docs"},
         "Fetch page", "https://example.com/docs"),
        ("notebook_edit", {"filepath": "nb.ipynb", "cell_index": 3},
         "Edit notebook", "nb.ipynb · cell 3"),
        ("Agent", {"input": "Investigate the flaky test\nfull brief"},
         "Delegate to agent", "Investigate the flaky test"),
        ("WebSpider", {"input": "Compare SSE docs across browsers"},
         "Web research", "Compare SSE docs across browsers"),
        ("codegraph_explore", {"query": "AuthService loginUser", "projectPath": "/repo"},
         "Explore code graph", "AuthService loginUser"),
        ("codegraph_status", {"projectPath": "/repo"},
         "Index status", "/repo"),
    ],
)
def test_characteristic_tools(name, args, title, subtitle):
    assert tool_title(name) == title
    assert tool_subtitle(name, args) == subtitle


def test_subtitle_from_json_string_arguments():
    # The observer receives arguments as a JSON string, not a dict.
    assert tool_subtitle("read_file", '{"filepath": "a/b.py"}') == "a/b.py"


def test_beads_subtitles_use_ids_and_titles():
    assert tool_subtitle("beads_show", {"bead_id": "bd-1"}) == "bd-1"
    assert tool_subtitle("beads_update", {"bead_id": "bd-1", "status": "closed"}) == (
        "bd-1 → closed"
    )
    assert tool_subtitle(
        "beads_create", {"title": "Fix bug", "type": "task", "priority": 1}
    ) == "Fix bug (task, P1)"
    assert tool_subtitle("beads_dep", {"action": "add", "child_id": "a", "parent_id": "b"}) == (
        "add: a ← b"
    )


def test_long_subtitle_is_clipped():
    long_task = "word " * 100
    clipped = tool_subtitle("orchestrate", {"task": long_task})
    assert clipped is not None
    assert len(clipped) <= 120
    assert clipped.endswith("…")


@pytest.mark.parametrize(
    "name, args",
    [
        ("read_file", "not json"),
        ("read_file", ""),
        ("read_file", None),
        ("read_file", {}),
        ("read_file", 123),
        ("read_file", ["path"]),
        ("write_file", "{bad json"),
        ("write_file", "[1, 2, 3]"),
        ("beads_create", {"priority": 1}),  # missing title
        ("beads_update", {"status": "closed"}),  # missing id
        ("pipeline_wait", {"task_ids": []}),
        ("search_files", {"pattern": ""}),
    ],
)
def test_malformed_or_incomplete_arguments_never_raise(name, args):
    # Any junk must degrade to None (caller falls back), never raise.
    result = tool_subtitle(name, args)
    assert result is None or isinstance(result, str)


def test_every_formatter_is_registered_with_a_title():
    for name in TOOL_SUBTITLES:
        assert name in TOOL_TITLES, f"{name} has a subtitle but no title"


def test_unknown_arguments_shape_is_safe_for_every_formatter():
    for name, formatter in TOOL_SUBTITLES.items():
        # Neither {} nor a hostile payload may blow up.
        result = tool_subtitle(name, {})
        assert result is None or isinstance(result, str)
        tool_subtitle(name, {"filepath": None, "task": [], "content": {}, "pattern": 5})


# -- workspace memory --------------------------------------------------------


def test_memory_subtitles_describe_the_operation_arguments():
    assert tool_title("memory_recall") == "Recall memory"
    assert tool_title("memory_overview") == "Memory overview"
    assert tool_title("memory_remember") == "Remember"
    assert tool_title("memory_forget") == "Forget memory"

    assert tool_subtitle("memory_recall", {"query": "auth tokens"}) == '"auth tokens"'
    assert tool_subtitle("memory_overview", {"limit": 25}) == "up to 25 records"
    assert tool_subtitle(
        "memory_remember",
        {"content": "Use SQLite for memory", "layer": "L1", "category": "decision"},
    ) == 'L1 decision: "Use SQLite for memory"'
    assert tool_subtitle("memory_forget", {"record_id": "abc123"}) == "abc123"


def test_memory_subtitles_stay_honest_when_arguments_are_partial():
    # A search without a query has nothing to show; the caller falls back.
    assert tool_subtitle("memory_recall", {"limit": 5}) is None
    assert tool_subtitle("memory_forget", {"bead_id": "bd-1"}) is None
    assert tool_subtitle("memory_overview", {}) == ""
    # A save without content still says where it goes.
    assert tool_subtitle("memory_remember", {"layer": "L3", "category": "preference"}) == "L3 preference"
    # Multi-line content collapses to its first line, and long text is clipped.
    assert tool_subtitle("memory_remember", {"content": "line one\nline two"}) == '"line one"'
    clipped = tool_subtitle("memory_remember", {"content": "word " * 100})
    # The quoted snippet is shortened inside the quotes, so the caption keeps
    # both its delimiters and the 120-character budget.
    assert clipped is not None and len(clipped) <= 120
    assert clipped.startswith('"') and clipped.endswith('"') and "…" in clipped
