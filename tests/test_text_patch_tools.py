"""File editing tools the administrator's specialists rely on.

The patch dialects below are the ones a model actually wrote in an evaluation
run, when every one of them failed and the specialist fell back to rewriting
whole files.
"""

import asyncio
import json

import pytest

from tools.file_tools import (
    delete_file, edit_file_patch, read_file, replace_in_file, search_content, search_files,
    write_file,
)
from utils.path_utils import reset_current_factory, set_current_factory
from utils.text_patch import PatchError, apply_patch, keep_newlines, replace_once

CONFIG = (
    "agents:\r\n"
    "  engineer:\r\n"
    "    description: \"Code changes only, not for questions.\"\r\n"
    "  web_spider:\r\n"
    "    description: \"Research assistant and the default for questions.\"\r\n"
)


def call(tool, **arguments):
    return asyncio.run(tool.on_invoke_tool(None, json.dumps(arguments)))


class _Factory:
    """Just enough of an agent factory for path resolution inside tools."""

    def __init__(self, path):
        self.config = type("C", (), {"get_working_directory": lambda _self: str(path)})()
        self.container_id = None


@pytest.fixture
def workdir(tmp_path):
    set_current_factory(_Factory(tmp_path))
    yield tmp_path
    reset_current_factory()


@pytest.mark.parametrize("patch", [
    # plain unified diff with line numbers
    "--- a/c.yaml\n+++ b/c.yaml\n@@ -2,2 +2,2 @@\n   engineer:\n-    description: \"Code changes only, not for questions.\"\n+    description: \"Main assistant.\"\n",
    # git header
    "diff --git a/c.yaml b/c.yaml\n--- a/c.yaml\n+++ b/c.yaml\n@@ -3 +3 @@\n-    description: \"Code changes only, not for questions.\"\n+    description: \"Main assistant.\"",
    # bare @@ without numbers
    "@@\n-    description: \"Code changes only, not for questions.\"\n+    description: \"Main assistant.\"\n",
    # the apply_patch envelope
    "*** Begin Patch\n*** Update File: c.yaml\n@@\n   engineer:\n-    description: \"Code changes only, not for questions.\"\n+    description: \"Main assistant.\"\n*** End Patch",
    # wrong line numbers: found by content
    "@@ -40,2 +40,2 @@\n   engineer:\n-    description: \"Code changes only, not for questions.\"\n+    description: \"Main assistant.\"\n",
])
def test_patch_dialects_apply_and_keep_crlf(patch):
    result = apply_patch(CONFIG, patch)
    assert result == CONFIG.replace("Code changes only, not for questions.", "Main assistant.")
    assert result.count("\r\n") == CONFIG.count("\r\n")


def test_patch_errors_say_how_to_fix():
    with pytest.raises(PatchError, match="not found"):
        apply_patch(CONFIG, "@@\n-    description: \"Nope\"\n+    x\n")
    with pytest.raises(PatchError, match="no hunk"):
        apply_patch(CONFIG, "beta\nBETA")
    with pytest.raises(PatchError, match="occurs 2 times"):
        apply_patch("a\nx\na\nx\n", "@@\n-x\n+y\n")


def test_repeated_context_is_resolved_by_the_line_hint():
    assert apply_patch("a\nx\na\nx\n", "@@ -4 +4 @@\n-x\n+y\n") == "a\nx\na\ny\n"


def test_several_hunks_and_pure_insertion():
    text = "one\ntwo\nthree\nfour\n"
    patch = "@@\n one\n+one-and-a-half\n two\n@@\n-four\n+FOUR\n"
    assert apply_patch(text, patch) == "one\none-and-a-half\ntwo\nthree\nFOUR\n"


def test_replace_once():
    assert replace_once(CONFIG, "Research assistant and the default for questions.",
                        "Web researcher only.") .count("\r\n") == 5
    with pytest.raises(PatchError, match="not found"):
        replace_once(CONFIG, "missing", "x")
    with pytest.raises(PatchError, match="occurs 2 times"):
        replace_once(CONFIG, "description", "x")
    multi = "  engineer:\n    description: \"Code changes only, not for questions.\"\n"
    assert "Main" in replace_once(CONFIG, multi, "  engineer:\n    description: \"Main\"\n")


def test_keep_newlines():
    assert keep_newlines("a\r\nb\r\n", "a\nc\n") == "a\r\nc\r\n"
    assert keep_newlines(None, "a\n") == "a\n"
    assert keep_newlines("a\n", "a\r\n") == "a\r\n"


def test_tools_edit_in_place_without_rewriting(workdir):
    (workdir / "c.yaml").write_bytes(CONFIG.encode())
    assert "✅" in call(replace_in_file, filepath="c.yaml",
                        old_text="Code changes only, not for questions.", new_text="Main assistant.")
    assert "✅" in call(edit_file_patch, filepath="c.yaml", patch_content=(
        "@@\n-    description: \"Research assistant and the default for questions.\"\n"
        "+    description: \"Web researcher.\"\n"))
    data = (workdir / "c.yaml").read_bytes().decode()
    assert data == CONFIG.replace("Code changes only, not for questions.", "Main assistant.") \
        .replace("Research assistant and the default for questions.", "Web researcher.")


def test_write_file_keeps_the_files_line_endings(workdir):
    (workdir / "c.yaml").write_bytes(CONFIG.encode())
    call(write_file, filepath="c.yaml", content=CONFIG.replace("\r\n", "\n"))
    assert (workdir / "c.yaml").read_bytes() == CONFIG.encode()
    assert "Created" in call(write_file, filepath="new.txt", content="x\n")
    assert (workdir / "new.txt").read_bytes() == b"x\n"


def test_git_internals_are_refused(workdir):
    (workdir / ".git").mkdir()
    (workdir / ".git" / "config").write_text("[core]\n")
    for tool, arguments in (
        (write_file, {"filepath": ".git/config", "content": "[core]\nfsmonitor = evil\n"}),
        (replace_in_file, {"filepath": ".git/config", "old_text": "[core]", "new_text": "x"}),
        (edit_file_patch, {"filepath": ".git/config", "patch_content": "@@\n-[core]\n+x\n"}),
        (delete_file, {"filepath": ".git/config"}),
    ):
        assert "inside .git" in call(tool, **arguments)
    assert (workdir / ".git" / "config").read_text() == "[core]\n"
    assert "inside .git" in call(write_file, filepath=".git/hooks/pre-commit", content="x")


def test_delete_file(workdir):
    (workdir / "scratch.txt").write_text("x")
    assert "✅" in call(delete_file, filepath="scratch.txt")
    assert not (workdir / "scratch.txt").exists()
    (workdir / "d").mkdir()
    assert "directory" in call(delete_file, filepath="d")
    assert "escapes" in call(delete_file, filepath="../outside.txt").lower() or \
        "not found" in call(delete_file, filepath="../outside.txt")


def test_searches_are_relative_bounded_and_numbered(workdir):
    (workdir / ".git").mkdir()
    (workdir / ".git" / "routing.yaml").write_text("x")
    (workdir / "routing.yaml").write_text("a\nroute me\nb\n" + "route\n" * 300)
    found = call(search_files, directory=".", pattern="routing")
    assert "routing.yaml" in found and ".git" not in found and str(workdir) not in found
    lines = call(search_content, filepath="routing.yaml", query="route me")
    assert "2: route me" in lines
    assert "more" in call(search_content, filepath="routing.yaml", query="route")
    assert "File content" in call(read_file, filepath="routing.yaml")
