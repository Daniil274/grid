"""Tool tests (C5): discovery/opt-in and an end-to-end run through the tools.

The workflow tools are ordinary Grid tools: discovered by the existing
auto-discovery, declared with their effects/isolation, and reachable only when
a system names them. The end-to-end case drives a fake factory — the tools go
through the real ``WorkflowService``/engine and a temp journal, no model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext
from agents.usage import Usage

from tools.function_tools import _discover_tool_modules, resolve_tool, tool_effect, tool_isolation
from tools.workflow_tools import (
    TOOL_EFFECTS,
    TOOL_ISOLATION,
    WORKFLOW_TOOLS,
    workflow_cancel,
    workflow_run,
    workflow_status,
)
from utils.tool_effects import DELEGATE, read
from utils.tool_isolation import WORKSPACE


# -- discovery / opt-in -----------------------------------------------------------


def test_workflow_module_is_discovered_and_declares_its_effects():
    assert "tools.workflow_tools" in _discover_tool_modules()
    assert set(WORKFLOW_TOOLS) == {"workflow_run", "workflow_status", "workflow_resume", "workflow_cancel"}
    # Reachable by name only (the existing allowlist / opt-in path).
    assert resolve_tool("workflow_run", None) is not None
    assert resolve_tool("workflow_status", None) is not None
    assert tool_effect("workflow_run") == DELEGATE
    assert tool_effect("workflow_status").kind == "read"
    assert tool_isolation("workflow_status") == WORKSPACE
    assert TOOL_EFFECTS["workflow_run"] is DELEGATE
    assert TOOL_ISOLATION["workflow_run"] == WORKSPACE


# -- fake factory + context -------------------------------------------------------


class FakeTool:
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    async def on_invoke_tool(self, ctx, args):
        return await self._fn(ctx, json.loads(args or "{}"))


class FakeFactory:
    def __init__(self, tools, workspace, logs):
        self._tools = dict(tools)
        self._logs = logs
        self.config = SimpleNamespace(get_working_directory=lambda: str(workspace))

    def _resolve_function_tools(self, names):
        return [self._tools[n] for n in names if n in self._tools]

    def _logs_directory_path(self):
        return self._logs

    def get_active_context_id(self):
        return "ctx-live"


def _context(factory, *, context_id="ctx-live", user_id="alice"):
    raw = SimpleNamespace(factory=factory, context_id=context_id, user_id=user_id, stream_observer=None)
    return ToolContext(context=raw, usage=Usage(), tool_name="workflow_run",
                       tool_call_id="call-1", tool_arguments="")


def _factory(tmp_path, tools):
    return FakeFactory(tools, tmp_path, tmp_path)


def _wire(monkeypatch, effects):
    monkeypatch.setattr("tools.tool_effect", lambda name, loader=None: effects.get(name))
    monkeypatch.setattr("tools.tool_isolation", lambda name, loader=None: None)


async def _call(tool, context, **args):
    return json.loads(await tool.on_invoke_tool(context, json.dumps(args)))


# -- end-to-end -------------------------------------------------------------------


async def test_workflow_run_end_to_end_with_dataflow(tmp_path, monkeypatch):
    seen: dict = {}

    async def first(ctx, args):
        return {"n": 2}

    async def second(ctx, args):
        upstream = ctx.context.workflow_data.get("a") or {}
        seen["upstream"] = upstream
        return {"m": upstream.get("n", 0) + 1}

    factory = _factory(tmp_path, {"read.a": FakeTool("read.a", first), "read.b": FakeTool("read.b", second)})
    _wire(monkeypatch, {"read.a": read(), "read.b": read()})
    context = _context(factory)

    spec = {
        "name": "demo", "goal": "g",
        "steps": [
            {"id": "a", "kind": "read", "tool": "read.a"},
            {"id": "b", "kind": "read", "tool": "read.b", "depends_on": ["a"]},
            {"id": "g", "kind": "gate", "check": "deps.data_present", "depends_on": ["b"]},
        ],
    }
    out = await _call(workflow_run, context, spec_json=json.dumps(spec))

    assert out["status"] == "completed"
    assert out["cost_status"] == "unknown"  # truthful: no spend tally wired here
    assert seen["upstream"] == {"n": 2}
    assert [s["status"] for s in out["steps"]] == ["completed", "completed", "completed"]

    # A status call sees the persisted run.
    status = await _call(workflow_status, context, run_id=out["run_id"])
    assert status["run_id"] == out["run_id"]
    assert status["status"] == "completed"


async def test_workflow_run_reports_spec_invalid_without_starting(tmp_path, monkeypatch):
    factory = _factory(tmp_path, {})
    _wire(monkeypatch, {})
    context = _context(factory)

    spec = {"name": "bad", "goal": "g",
            "steps": [{"id": "w", "kind": "write", "tool": "file_write", "arguments": {"filepath": "x"}}]}
    out = await _call(workflow_run, context, spec_json=json.dumps(spec))

    assert out["status"] == "SPEC_INVALID"
    assert out["errors"], "the problems are reported"
    assert all(e["code"] == "WRITE_PATHS_EMPTY" for e in out["errors"])


async def test_workflow_run_refuses_a_tool_outside_the_allowlist(tmp_path, monkeypatch):
    factory = _factory(tmp_path, {})  # resolves nothing
    _wire(monkeypatch, {"read.x": read()})
    context = _context(factory)

    spec = {"name": "x", "goal": "g",
            "steps": [{"id": "a", "kind": "read", "tool": "read.x"}]}
    out = await _call(workflow_run, context, spec_json=json.dumps(spec))

    assert out["status"] == "failed"
    assert out["steps"][0]["error_code"] == "POLICY_VIOLATION"


async def test_workflow_status_refuses_another_user(tmp_path, monkeypatch):
    async def body(ctx, args):
        return {"ok": True}

    factory = _factory(tmp_path, {"read.a": FakeTool("read.a", body)})
    _wire(monkeypatch, {"read.a": read()})
    alice = _context(factory, user_id="alice")
    bob = _context(factory, user_id="bob")

    spec = {"name": "x", "goal": "g", "steps": [{"id": "a", "kind": "read", "tool": "read.a"}]}
    out = await _call(workflow_run, alice, spec_json=json.dumps(spec))

    assert out["status"] == "completed"
    denied = await _call(workflow_status, bob, run_id=out["run_id"])
    assert denied["status"] == "FORBIDDEN"


async def test_workflow_status_missing_run_is_not_found(tmp_path, monkeypatch):
    factory = _factory(tmp_path, {})
    _wire(monkeypatch, {})
    out = await _call(workflow_status, _context(factory), run_id="wf-missing")
    assert out["status"] == "NOT_FOUND"


async def test_workflow_cancel_a_queued_run(tmp_path, monkeypatch):
    # A run that exists but never executed is cancelled through the journal.
    from core.workflows.journal import WorkflowJournal
    from core.workflows.spec import WorkflowSpec, pin

    factory = _factory(tmp_path, {})
    _wire(monkeypatch, {})
    context = _context(factory)

    spec = WorkflowSpec.model_validate(
        {"name": "q", "goal": "g", "steps": [{"id": "a", "kind": "read", "tool": "read.a"}]})
    db = tmp_path / "workflow_runs.db"
    journal = WorkflowJournal(db)
    journal.create_run(run_id="wf-queued", spec=spec.model_dump(mode="json"),
                       spec_sha256=pin(spec), context_id="ctx-live", user_id="alice")
    journal.close()

    out = await _call(workflow_cancel, context, run_id="wf-queued", reason="stop")
    assert out["status"] in ("emergency_stopped", "cancelling")
