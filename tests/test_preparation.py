"""Preparation steps: overlap only when asked, dependencies ordered, failures and Stop safe.

The steps are an agent's auto_run_tools and a caller's init_tools
(core.factory.auto_run). A batch stays a plain sequence unless a step opts in
with ``parallel``; ``depends_on`` orders what reads another step's result.

Run explicitly: ``pytest core/factory/tests/test_preparation.py``.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.agent_factory import AgentFactory
from core.factory.preparation import (
    BLOCKED,
    DONE,
    FAILED,
    SKIPPED,
    PreparationError,
    data_dependencies,
    is_concurrent,
    plan_steps,
    run_steps,
)


class Probe:
    """A tool that records when it ran, how many ran at once, and can block."""

    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.started = []
        self.finished = []
        self.cancelled = []

    def tool(self, name, result=None, delay=0.0, gate=None, fail=None):
        async def invoke(_ctx, _args):
            self.started.append(name)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                if delay:
                    await asyncio.sleep(delay)
                if gate is not None:
                    await asyncio.wait_for(gate.wait(), timeout=2.0)
                if fail is not None:
                    raise fail
                return result if result is not None else f"{name} done"
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise
            finally:
                self.in_flight -= 1
                self.finished.append(name)

        return SimpleNamespace(name=name, on_invoke_tool=invoke)


def make_factory():
    factory = object.__new__(AgentFactory)
    factory.action_gate = None
    factory._stream_observer = None
    factory.container_id = None
    factory.config = SimpleNamespace(get_working_directory=lambda: ".")
    return factory


def auto_config(*specs):
    return SimpleNamespace(auto_run_tools=list(specs))


# --------------------------------------------------------------------------
# The scheduler itself
# --------------------------------------------------------------------------


async def test_legacy_batch_is_sequential_and_keeps_its_order():
    probe = Probe()

    async def run_one(step):
        return await probe.tool(step.name, delay=0.01).on_invoke_tool(None, "{}"), DONE

    steps = plan_steps([{"name": "a"}, {"name": "b"}, {"name": "c"}])
    assert not is_concurrent(steps)
    outcomes = await run_steps(steps, run_one)

    assert [o.status for o in outcomes] == [DONE, DONE, DONE]
    assert probe.max_in_flight == 1
    assert probe.started == ["a", "b", "c"]
    assert probe.finished == ["a", "b", "c"]


async def test_independent_steps_overlap_only_when_they_opt_in():
    probe = Probe()
    both_running = asyncio.Event()

    def tool(name):
        async def invoke(_ctx, _args):
            probe.started.append(name)
            probe.in_flight += 1
            probe.max_in_flight = max(probe.max_in_flight, probe.in_flight)
            try:
                if probe.in_flight == 2:
                    both_running.set()
                # Without real overlap this would time out.
                await asyncio.wait_for(both_running.wait(), timeout=2.0)
                return f"{name} done"
            finally:
                probe.in_flight -= 1

        return SimpleNamespace(name=name, on_invoke_tool=invoke)

    tools = {name: tool(name) for name in ("a", "b")}

    async def run_one(step):
        return await tools[step.name].on_invoke_tool(None, "{}"), DONE

    steps = plan_steps([{"name": "a", "parallel": True}, {"name": "b", "parallel": True}])
    assert is_concurrent(steps)
    outcomes = await run_steps(steps, run_one)

    assert [o.status for o in outcomes] == [DONE, DONE]
    assert probe.max_in_flight == 2
    # Results keep the batch's order, not the completion order.
    assert [o.step.name for o in outcomes] == ["a", "b"]


async def test_a_step_without_opt_in_is_a_barrier():
    probe = Probe()
    order = []

    async def run_one(step):
        order.append(step.name)
        return await probe.tool(step.name, delay=0.01).on_invoke_tool(None, "{}"), DONE

    steps = plan_steps(
        [
            {"name": "a", "parallel": True},
            {"name": "b", "parallel": True},
            {"name": "c"},  # did not opt in: runs alone, after both
            {"name": "d", "parallel": True},  # must wait for c
        ]
    )
    outcomes = await run_steps(steps, run_one)

    assert [o.status for o in outcomes] == [DONE] * 4
    assert probe.max_in_flight == 2
    assert order.index("c") > order.index("a") and order.index("c") > order.index("b")
    assert order.index("d") > order.index("c")


async def test_depends_on_orders_a_step_after_the_one_it_reads():
    probe = Probe()

    async def run_one(step):
        if step.name == "b":
            assert "a" in probe.finished, "b ran before its dependency finished"
        return await probe.tool(step.name, delay=0.01).on_invoke_tool(None, "{}"), DONE

    steps = plan_steps(
        [
            {"name": "a", "parallel": True},
            {"name": "b", "parallel": True, "depends_on": ["a"]},
        ]
    )
    outcomes = await run_steps(steps, run_one)

    assert [o.status for o in outcomes] == [DONE, DONE]
    assert probe.finished == ["a", "b"]


async def test_a_failed_dependency_skips_its_dependent_but_not_independent_steps():
    ran = []

    async def run_one(step):
        ran.append(step.name)
        if step.name == "a":
            raise RuntimeError("boom")
        return f"{step.name} done", DONE

    steps = plan_steps(
        [
            {"name": "a", "parallel": True},
            {"name": "b", "parallel": True, "depends_on": ["a"]},
            {"name": "c", "parallel": True},
        ]
    )
    outcomes = await run_steps(steps, run_one)

    statuses = {o.step.name: o.status for o in outcomes}
    assert statuses == {"a": FAILED, "b": SKIPPED, "c": DONE}
    assert ran == ["a", "c"]
    skipped = next(o for o in outcomes if o.step.name == "b")
    assert "'a' failed" in skipped.detail


async def test_a_blocked_dependency_skips_its_dependent():
    async def run_one(step):
        return ("", BLOCKED) if step.name == "a" else (f"{step.name} done", DONE)

    steps = plan_steps(
        [{"name": "a", "parallel": True}, {"name": "b", "parallel": True, "depends_on": ["a"]}]
    )
    outcomes = await run_steps(steps, run_one)

    assert {o.step.name: o.status for o in outcomes} == {"a": BLOCKED, "b": SKIPPED}


async def test_an_unknown_dependency_skips_only_that_step():
    async def run_one(step):
        return f"{step.name} done", DONE

    steps = plan_steps(
        [
            {"name": "a", "parallel": True},
            {"name": "b", "parallel": True, "depends_on": ["ghost"]},
        ]
    )
    outcomes = await run_steps(steps, run_one)

    assert {o.step.name: o.status for o in outcomes} == {"a": DONE, "b": SKIPPED}
    assert "not an earlier step" in outcomes[1].detail


def test_the_graph_is_acyclic_by_construction():
    steps = plan_steps(
        [
            {"name": "a", "parallel": True, "depends_on": ["b"]},
            {"name": "b", "parallel": True},
            {"name": "b", "parallel": True, "depends_on": ["a"]},
        ]
    )
    # A name refers to the nearest earlier step with it: a's "b" is the future
    # second step, so it is a configuration error, not a cycle.
    with pytest.raises(PreparationError):
        data_dependencies(steps, 0)
    assert data_dependencies(steps, 2) == [0]


async def test_a_single_call_that_raises_does_not_stop_the_batch():
    async def run_one(step):
        if step.name == "a":
            raise RuntimeError("boom")
        return f"{step.name} done", DONE

    steps = plan_steps([{"name": "a"}, {"name": "b"}])
    outcomes = await run_steps(steps, run_one)
    assert [o.status for o in outcomes] == [FAILED, DONE]
    assert "RuntimeError: boom" in outcomes[0].detail


async def test_cancellation_cancels_in_flight_steps_and_propagates():
    probe = Probe()
    gate = asyncio.Event()
    steps = plan_steps([{"name": "slow", "parallel": True}])

    async def run_one(step):
        return await probe.tool(step.name, gate=gate).on_invoke_tool(None, "{}"), DONE

    task = asyncio.create_task(run_steps(steps, run_one))
    while "slow" not in probe.started:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert probe.cancelled == ["slow"]


# --------------------------------------------------------------------------
# The factory's preparation: auto_run_tools and init_tools
# --------------------------------------------------------------------------


async def test_auto_run_independent_every_run_tools_overlap():
    probe = Probe()
    gate = asyncio.Event()

    async def both(_ctx, _args):
        probe.in_flight += 1
        probe.max_in_flight = max(probe.max_in_flight, probe.in_flight)
        try:
            if probe.in_flight == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=2.0)
            return "ok"
        finally:
            probe.in_flight -= 1

    factory = make_factory()
    tools = [
        SimpleNamespace(name="a", on_invoke_tool=both),
        SimpleNamespace(name="b", on_invoke_tool=both),
    ]
    config = auto_config(
        {"name": "a", "every_run": True, "parallel": True},
        {"name": "b", "every_run": True, "parallel": True},
    )
    info, complete = await factory._execute_auto_run_tools(
        "agent", config, tools, ".", SimpleNamespace(stream_observer=None), every_run=True
    )

    assert complete
    assert "=== AUTO-RUN TOOL RESULT 'a' ===" in info
    assert info.index("'a'") < info.index("'b'")
    assert probe.max_in_flight == 2


async def test_auto_run_legacy_batch_stays_sequential():
    probe = Probe()
    factory = make_factory()
    tools = [probe.tool(name, delay=0.01) for name in ("a", "b")]
    config = auto_config({"name": "a", "every_run": True}, {"name": "b", "every_run": True})

    await factory._execute_auto_run_tools(
        "agent", config, tools, ".", SimpleNamespace(stream_observer=None), every_run=True
    )

    assert probe.max_in_flight == 1
    assert probe.started == ["a", "b"]


async def test_auto_run_failure_skips_a_dependent_and_records_its_timing():
    probe = Probe()
    factory = make_factory()
    tools = [
        probe.tool("a", fail=RuntimeError("no index")),
        probe.tool("b"),
        probe.tool("c"),
    ]
    config = auto_config(
        {"name": "a", "every_run": True, "parallel": True},
        {"name": "b", "every_run": True, "parallel": True, "depends_on": ["a"]},
        {"name": "c", "every_run": True, "parallel": True},
    )
    metadata = {}
    info, complete = await factory._execute_auto_run_tools(
        "agent",
        config,
        tools,
        ".",
        SimpleNamespace(stream_observer=None, metadata=metadata),
        every_run=True,
    )

    assert complete  # a failure is not a policy block: the one-time mark stays
    assert "b done" not in info and "c done" in info
    assert probe.started == ["a", "c"]
    record = metadata["preparation_timings"][0]
    assert record["mode"] == "parallel"
    assert [entry["name"] for entry in record["steps"]] == ["a", "b", "c"]
    assert [entry["status"] for entry in record["steps"]] == [FAILED, SKIPPED, DONE]
    assert "wall_ms" in record and "serial_ms" in record


async def test_auto_run_policy_block_skips_dependent_and_stays_incomplete():
    blocked = json.dumps({"status": "blocked", "rule": "run_stopped"})
    factory = make_factory()
    invoked = []

    def tool(name, result):
        async def invoke(_ctx, _args):
            invoked.append(name)
            return result

        return SimpleNamespace(name=name, on_invoke_tool=invoke)

    tools = [tool("a", blocked), tool("b", "b done")]
    config = auto_config(
        {"name": "a", "parallel": True},
        {"name": "b", "parallel": True, "depends_on": ["a"]},
    )
    info, complete = await factory._execute_auto_run_tools(
        "agent", config, tools, ".", SimpleNamespace(stream_observer=None)
    )

    assert not complete
    assert invoked == ["a"]
    assert "blocked" not in info and "b done" not in info


async def test_init_tools_overlap_and_keep_the_batch_order():
    probe = Probe()
    gate = asyncio.Event()

    async def both(_ctx, _args):
        probe.in_flight += 1
        probe.max_in_flight = max(probe.max_in_flight, probe.in_flight)
        try:
            if probe.in_flight == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=2.0)
            return "ok"
        finally:
            probe.in_flight -= 1

    factory = make_factory()
    tools = [
        SimpleNamespace(name="a", on_invoke_tool=both),
        SimpleNamespace(name="b", on_invoke_tool=both),
    ]
    info = await factory._execute_init_tools(
        [
            {"name": "a", "parallel": True},
            {"name": "b", "parallel": True},
        ],
        tools,
        SimpleNamespace(stream_observer=None),
    )

    assert info.index("=== CONTEXT [a]") < info.index("=== CONTEXT [b]")
    assert probe.max_in_flight == 2


async def test_init_tools_without_opt_in_stay_sequential():
    probe = Probe()
    factory = make_factory()
    tools = [probe.tool(name, delay=0.01) for name in ("a", "b")]

    await factory._execute_init_tools(
        [{"name": "a"}, {"name": "b"}], tools, SimpleNamespace(stream_observer=None)
    )

    assert probe.max_in_flight == 1
    assert probe.started == ["a", "b"]
