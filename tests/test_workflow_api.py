"""API tests (C5): a fake factory drives the real engine over a temp journal.

Scope, the trusted-metadata cross-check, lane write_paths, deterministic gates
and safe resume are asserted end-to-end. Nothing leaves the process; no model
is called (the fake factory's tools are plain async functions).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.workflows.api import (
    WorkflowScopeError,
    WorkflowService,
    build_invoker,
    tool_effect_class,
)
from core.workflows.journal import WorkflowJournal
from core.workflows.reconcile import reconcile
from core.workflows.spec import StepResult, WorkflowSpec, pin
from utils.tool_effects import read, write


# -- fake factory -----------------------------------------------------------------


class FakeTool:
    """A tool as the factory hands it over: callable only through the wrapper."""

    def __init__(self, name, fn):
        self.name = name
        self._fn = fn
        self.calls = 0

    async def on_invoke_tool(self, ctx, args):
        self.calls += 1
        return await self._fn(ctx, json.loads(args or "{}"))


class FakeFactory:
    """The narrow factory surface the invoker relies on."""

    def __init__(self, tools, workspace):
        self._tools = dict(tools)
        self.config = SimpleNamespace(get_working_directory=lambda: str(workspace))

    def _resolve_function_tools(self, names):
        return [self._tools[n] for n in names if n in self._tools]


def spec_of(steps, **kw):
    return {"name": "wf", "goal": "g", "steps": steps, **kw}


def read_step(sid, tool, deps=(), **kw):
    return {"id": sid, "kind": "read", "tool": tool, "depends_on": list(deps), **kw}


@pytest.fixture()
def journal(tmp_path):
    j = WorkflowJournal(tmp_path / "wf.db")
    yield j
    j.close()


def make_service(journal, factory, effects, *, workspace, checks=None):
    invoker = build_invoker(
        factory,
        effect_of=lambda name: effects.get(name),
        isolation_of=lambda name: None,
        checks=checks,
    )
    return WorkflowService(
        journal=journal,
        invoke=invoker,
        owner="test",
        workspace_root=workspace,
        reconcile_on_start=False,
    )


# -- DAG, dataflow, persistence ---------------------------------------------------


async def test_dag_dataflow_and_durable_persistence(journal, tmp_path):
    async def first(ctx, args):
        return {"n": 1}

    seen: dict = {}

    async def second(ctx, args):
        seen["data"] = dict(ctx.context.workflow_data)
        upstream = ctx.context.workflow_data.get("a") or {}
        return {"m": upstream.get("n", 0) + 41}

    tool_a = FakeTool("read.a", first)
    tool_b = FakeTool("read.b", second)
    factory = FakeFactory({"read.a": tool_a, "read.b": tool_b}, tmp_path)
    service = make_service(journal, factory, {"read.a": read(), "read.b": read()}, workspace=tmp_path)

    view = await service.start(spec_of([read_step("a", "read.a"), read_step("b", "read.b", ["a"])]))

    assert view.status == "completed"
    assert [s.status for s in view.steps] == ["completed", "completed"]
    # Dataflow: the dependent tool saw the predecessor's persisted data.
    assert seen["data"]["a"] == {"n": 1}
    assert tool_a.calls == 1 and tool_b.calls == 1  # went through the wrapper once

    # Durable: a fresh journal on the same file still reports the run.
    reopened = WorkflowService(
        journal=WorkflowJournal(tmp_path / "wf.db"),
        invoke=service.engine._invoke,
        owner="test",
        reconcile_on_start=False,
    )
    assert reopened.get(view.run_id).status == "completed"


# -- ownership scope --------------------------------------------------------------


async def test_scope_refuses_another_users_or_conversations_run(journal, tmp_path):
    async def body(ctx, args):
        return {"ok": True}

    factory = FakeFactory({"read.a": FakeTool("read.a", body)}, tmp_path)
    service = make_service(journal, factory, {"read.a": read()}, workspace=tmp_path)

    view = await service.start(
        spec_of([read_step("a", "read.a")]), context_id="ctx-1", user_id="alice"
    )
    assert view.status == "completed"

    assert service.get(view.run_id, context_id="ctx-1", user_id="alice").status == "completed"
    for kwargs in (
        {"context_id": "ctx-1", "user_id": "bob"},
        {"context_id": "ctx-2", "user_id": "alice"},
        {},  # an unscoped caller never opens an owned run
    ):
        with pytest.raises(WorkflowScopeError):
            service.get(view.run_id, **kwargs)


# -- trusted metadata cross-check -------------------------------------------------


def test_tool_effect_class_maps_declarations():
    assert tool_effect_class(None) == "unknown"  # undeclared is not trusted as none
    assert tool_effect_class(read()) == "none"
    assert tool_effect_class(write("filepath")) == "workspace"


async def test_read_step_cannot_replay_an_effectful_tool(journal, tmp_path):
    async def body(ctx, args):
        return {"wrote": True}

    factory = FakeFactory({"write.a": FakeTool("write.a", body)}, tmp_path)
    service = make_service(journal, factory, {"write.a": write("filepath")}, workspace=tmp_path)

    view = await service.start(spec_of([{"id": "a", "kind": "read", "tool": "write.a"}]))

    assert view.status == "failed"
    assert view.steps[0].error_code == "POLICY_VIOLATION"


async def test_write_step_enforces_its_write_paths(journal, tmp_path):
    async def body(ctx, args):
        return {"ok": True}

    factory = FakeFactory({"write.a": FakeTool("write.a", body)}, tmp_path)
    service = make_service(journal, factory, {"write.a": write("filepath")}, workspace=tmp_path)

    ok = await service.start(
        spec_of([{"id": "a", "kind": "write", "tool": "write.a",
                  "arguments": {"filepath": "src/x.py"}, "write_paths": ["src"]}])
    )
    # The in-scope write ran (step completed); its effect stays unverified
    # (unknown, I9), so the run needs a coordinator — never a false success.
    assert ok.steps[0].status == "completed"
    assert ok.steps[0].effect_status == "unknown"
    assert ok.status == "interrupted"

    bad = await service.start(
        spec_of([{"id": "a", "kind": "write", "tool": "write.a",
                  "arguments": {"filepath": "other/x.py"}, "write_paths": ["src"]}])
    )
    assert bad.status == "failed"
    assert bad.steps[0].error_code == "POLICY_VIOLATION"


async def test_tool_outside_the_allowlist_is_refused(journal, tmp_path):
    factory = FakeFactory({}, tmp_path)  # resolves nothing
    service = make_service(journal, factory, {"read.x": read()}, workspace=tmp_path)

    view = await service.start(spec_of([read_step("a", "read.x")]))

    assert view.status == "failed"
    assert view.steps[0].error_code == "POLICY_VIOLATION"


async def test_agent_step_is_honestly_unsupported(journal, tmp_path):
    factory = FakeFactory({}, tmp_path)
    service = make_service(journal, factory, {}, workspace=tmp_path)

    view = await service.start(spec_of([{"id": "a", "kind": "agent", "task": "do it"}]))

    assert view.status == "failed"
    assert view.steps[0].error_code == "MALFORMED"


# -- deterministic gates ----------------------------------------------------------


async def test_gate_checks_depend_on_data_not_on_a_model(journal, tmp_path):
    async def body(ctx, args):
        return {"n": 1}

    factory = FakeFactory({"read.a": FakeTool("read.a", body)}, tmp_path)
    service = make_service(journal, factory, {"read.a": read()}, workspace=tmp_path)

    passed = await service.start(spec_of([
        read_step("a", "read.a"),
        {"id": "g", "kind": "gate", "check": "deps.data_present", "depends_on": ["a"]},
    ]))
    assert passed.status == "completed"

    failed = await service.start(spec_of([
        read_step("a", "read.a"),
        {"id": "g", "kind": "gate", "check": "deps.unknown-check", "depends_on": ["a"]},
    ]))
    assert failed.status == "failed"
    assert failed.steps[-1].error_code == "GATE_FAILED"


# -- crash reconciliation + safe resume --------------------------------------------


async def test_safe_resume_preserves_completed_dataflow(tmp_path):
    j = WorkflowJournal(tmp_path / "r.db")
    parsed = WorkflowSpec.model_validate(
        spec_of([read_step("a", "read.a"), read_step("b", "read.b", ["a"])])
    )
    rid = "wf-crash"
    j.create_run(run_id=rid, spec=parsed.model_dump(mode="json"), spec_sha256=pin(parsed),
                 context_id="ctx-1", user_id="alice")
    j.add_step(rid, "a")
    j.add_step(rid, "b")
    # a completed with a persisted result; b crashed mid-run.
    j.set_step_status(rid, "a", "completed", expect="pending", result_ref="ref:a")
    j.append_event(rid, "step_result", StepResult(
        step_id="a", ok=True, result_ref="ref:a", data={"n": 7}).model_dump(mode="json"))
    j.set_step_status(rid, "b", "running", expect="pending")
    j.set_run_status(rid, "running")  # no owner/lease -> stale, not live

    report = reconcile(j)
    assert rid in report.reconciled_run_ids

    calls: list[str] = []
    seen: dict = {}

    async def first(ctx, args):
        calls.append("a")
        return {"n": 999}  # must never run: a is completed

    async def second(ctx, args):
        calls.append("b")
        seen["a"] = ctx.context.workflow_data.get("a")
        upstream = ctx.context.workflow_data.get("a") or {}
        return {"m": upstream.get("n", -1) + 1}

    factory = FakeFactory({"read.a": FakeTool("read.a", first), "read.b": FakeTool("read.b", second)}, tmp_path)
    service = WorkflowService(
        journal=j,
        invoke=build_invoker(factory, effect_of=lambda n: {"read.a": read(), "read.b": read()}.get(n),
                             isolation_of=lambda n: None),
        owner="test",
        reconcile_on_start=False,
    )

    view = await service.resume(rid, context_id="ctx-1", user_id="alice", write_policy="never")

    assert view.status == "completed"
    assert calls == ["b"]  # completed 'a' was not re-executed
    assert seen["a"] == {"n": 7}  # its persisted data flowed into 'b'
    assert next(s for s in view.steps if s.step_id == "a").result_ref == "ref:a"


async def test_resume_refuses_a_foreign_run(tmp_path):
    j = WorkflowJournal(tmp_path / "r2.db")
    parsed = WorkflowSpec.model_validate(spec_of([read_step("a", "read.a")]))
    j.create_run(run_id="wf-x", spec=parsed.model_dump(mode="json"), spec_sha256=pin(parsed),
                 context_id="ctx-1", user_id="alice")
    factory = FakeFactory({}, tmp_path)
    service = WorkflowService(
        journal=j,
        invoke=build_invoker(factory, effect_of=lambda n: None, isolation_of=lambda n: None),
        owner="test",
        reconcile_on_start=False,
    )
    with pytest.raises(WorkflowScopeError):
        await service.resume("wf-x", context_id="ctx-1", user_id="bob")
