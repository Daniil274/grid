"""The beads tools hand bd what the model wrote as values, never as options."""

import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from tools import beads_tools


async def invoke(monkeypatch, tmp_path, tool, **arguments):
    """Run *tool* in *tmp_path*; the bd argument lists it asked for."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir(exist_ok=True)
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        return {"success": True, "output": "{}", "error": "", "data": {}, "exit_code": 0}

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    context = ToolContext(
        context=SimpleNamespace(factory=None, container_id=None),
        tool_name=tool.name, tool_call_id="c1", tool_arguments="{}",
    )
    result = await tool.on_invoke_tool(context, json.dumps(arguments))
    return result, calls


async def test_a_title_that_looks_like_a_flag_stays_the_title(monkeypatch, tmp_path):
    _, [args] = await invoke(
        monkeypatch, tmp_path, beads_tools.beads_create, title="--db=/elsewhere", description="-x"
    )
    assert args[0] == "create" and args[-2:] == ["--", "--db=/elsewhere"]
    assert "--description=-x" in args and "--json" in args[:-2]


async def test_update_and_close_carry_values_inside_their_flags(monkeypatch, tmp_path):
    _, [update] = await invoke(
        monkeypatch, tmp_path, beads_tools.beads_update, bead_id="p-1.2", title="-t", notes="--all"
    )
    assert update == ["update", "p-1.2", "--title=-t", "--notes=--all"]
    _, [close] = await invoke(monkeypatch, tmp_path, beads_tools.beads_close, bead_id="p-1", reason="--force")
    assert close == ["close", "p-1", "--reason=--force"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (beads_tools.beads_show, {"bead_id": "--db=/x"}),
        (beads_tools.beads_update, {"bead_id": "-h", "status": "open"}),
        (beads_tools.beads_close, {"bead_id": "--force"}),
        (beads_tools.beads_dep, {"action": "add", "child_id": "p-1", "parent_id": "--db=/x"}),
    ],
)
async def test_an_id_that_would_read_as_a_flag_never_reaches_bd(monkeypatch, tmp_path, tool, arguments):
    result, calls = await invoke(monkeypatch, tmp_path, tool, **arguments)
    assert calls == [] and "not a bead id" in result
