"""Preparation steps: overlap only when asked, dependencies ordered, failures and Stop safe.

The steps are an agent's auto_run_tools and a caller's init_tools
(core.factory.auto_run). A batch stays a plain sequence unless a step opts in
with ``parallel``; ``depends_on`` orders what reads another step's result.

Run explicitly: ``pytest tests/test_preparation.py``.
"""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

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
    timings,
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


@pytest.mark.parametrize(
    "config_path",
    ["examples/coder/config.yaml", "examples/coordinator-pipeline/config.yaml"],
)
def test_shipped_agents_initialize_the_shared_tracker(config_path):
    """A new chat must not pay for a fresh Dolt database in its workspace."""
    root = Path(__file__).resolve().parent.parent
    agents = yaml.safe_load((root / config_path).read_text(encoding="utf-8"))["agents"]
    init_steps = [
        step
        for agent in agents.values()
        for step in agent.get("auto_run_tools", [])
        if step.get("name") == "beads_init"
    ]

    assert init_steps
    # beads_init's default resolves through factory.tracker_directory, just as
    # explicit beads calls do. ${working_directory} would bypass that mapping.
    assert all(step.get("parameters", {}).get("directory", ".") == "." for step in init_steps)


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
# Durations: a step's seconds is its own call, never its time in the queue
# --------------------------------------------------------------------------


async def test_a_dependent_steps_duration_excludes_the_wait_for_its_dependency():
    async def run_one(step):
        await asyncio.sleep(0.2 if step.name == "a" else 0.01)
        return f"{step.name} done", DONE

    steps = plan_steps(
        [{"name": "a", "parallel": True}, {"name": "b", "parallel": True, "depends_on": ["a"]}]
    )
    started = time.perf_counter()
    outcomes = await run_steps(steps, run_one)
    wall = time.perf_counter() - started
    a, b = outcomes

    # b waited for a's 200ms but only worked for its own 10ms.
    assert a.seconds >= 0.15
    assert b.seconds < 0.1
    assert b.waited_seconds >= 0.15
    record = timings(outcomes, wall)
    assert record["serial_ms"] == pytest.approx(
        sum(outcome.seconds for outcome in outcomes) * 1000, abs=0.2
    )
    # A chain's execution time cannot be larger than the wall time it fits in.
    assert record["serial_ms"] <= record["wall_ms"] + 20


async def test_independent_parallel_steps_report_their_own_time_not_the_batch_wall():
    delay = 0.1

    async def run_one(step):
        await asyncio.sleep(delay)
        return f"{step.name} done", DONE

    steps = plan_steps([{"name": name, "parallel": True} for name in ("a", "b", "c")])
    started = time.perf_counter()
    outcomes = await run_steps(steps, run_one)
    wall = time.perf_counter() - started
    record = timings(outcomes, wall)

    # Three overlapping 100ms calls ran in about 100ms of wall time...
    assert wall < 0.25
    # ...and each one is charged its own duration, no more.
    assert all(delay * 0.8 <= outcome.seconds <= delay * 3 for outcome in outcomes)
    # Serial time is the sum of the calls, each counted exactly once.
    assert record["serial_ms"] >= 3 * delay * 1000 * 0.9
    assert record["serial_ms"] <= 3 * delay * 1000 * 1.5 + 20


async def test_a_skipped_step_has_no_duration_and_does_not_inflate_serial_time():
    async def run_one(step):
        if step.name == "a":
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")
        await asyncio.sleep(0.02)
        return f"{step.name} done", DONE

    steps = plan_steps(
        [
            {"name": "a", "parallel": True},
            {"name": "b", "parallel": True, "depends_on": ["a"]},
            {"name": "c", "parallel": True},
        ]
    )
    started = time.perf_counter()
    outcomes = await run_steps(steps, run_one)
    wall = time.perf_counter() - started
    skipped = outcomes[1]

    assert skipped.status == SKIPPED
    assert skipped.seconds == 0.0  # never invoked, so it did not last
    record = timings(outcomes, wall)
    assert record["serial_ms"] == pytest.approx(
        (outcomes[0].seconds + outcomes[2].seconds) * 1000, abs=0.2
    )
    assert next(e for e in record["steps"] if e["name"] == "b")["seconds"] == 0.0


async def test_a_legacy_step_skipped_for_an_unknown_dependency_has_no_duration():
    async def run_one(step):
        await asyncio.sleep(0.02)
        return f"{step.name} done", DONE

    steps = plan_steps([{"name": "a"}, {"name": "b", "depends_on": ["ghost"]}])
    outcomes = await run_steps(steps, run_one)

    assert [outcome.status for outcome in outcomes] == [DONE, SKIPPED]
    assert outcomes[1].seconds == 0.0
    record = timings(outcomes, 0.02)
    assert record["serial_ms"] == pytest.approx(outcomes[0].seconds * 1000, abs=0.2)


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


async def test_a_containers_working_directory_is_the_mounted_workspace():
    """`${working_directory}` names the workspace, not the filesystem root.

    An isolated agent's shell starts in /workspace; telling init_codegraph to
    index "/" makes it read the whole container instead, and workspace_overview
    lists the container root rather than the files the agent was given.
    """
    seen = []

    async def invoke(_ctx, args):
        seen.append(json.loads(args)["directory"])
        return "ok"

    factory = make_factory()
    factory.container_id = "grid-test-container"
    factory._initialized_agents = set()
    config = auto_config(
        {"name": "init_codegraph", "every_run": True, "parameters": {"directory": "${working_directory}"}}
    )
    agent = SimpleNamespace(tools=[SimpleNamespace(name="init_codegraph", on_invoke_tool=invoke)])

    await factory._auto_run_preamble(
        "engineer",
        config,
        agent,
        SimpleNamespace(stream_observer=None, user_id="u1", metadata={}),
        "привет",
    )

    assert seen == ["/workspace"]


# --------------------------------------------------------------------------
# The typed result of required init_tools
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, status",
    [
        (RuntimeError("tracker is down"), FAILED),
        (json.dumps({"success": False, "error": "no beads database found"}), FAILED),
        ("❌ Error showing bead x-1: no issue found", FAILED),
        (json.dumps({"status": "blocked", "rule": "run_stopped"}), BLOCKED),
    ],
)
async def test_an_init_tool_that_did_not_succeed_is_not_a_successful_preparation(result, status):
    """A raise, a missing tool or a failed answer all make the batch fail."""
    invoke = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    factory = make_factory()
    tool = SimpleNamespace(name="beads_show", on_invoke_tool=invoke)

    info = await factory._execute_init_tools(
        [{"name": "beads_show"}], [tool], SimpleNamespace(stream_observer=None)
    )

    assert info.ok is False
    assert str(info) == ""  # nothing that failed is injected as context
    (failure,) = info.failures
    assert failure.name == "beads_show" and failure.status == status
    assert "beads_show" in info.failure_report("init_tools")


async def test_a_missing_init_tool_makes_the_preparation_not_ok():
    factory = make_factory()
    info = await factory._execute_init_tools(
        [{"name": "ghost"}], [], SimpleNamespace(stream_observer=None)
    )

    assert not info.ok
    assert "not available" in info.failure_report()


async def test_a_successful_init_tool_keeps_its_text_and_is_ok():
    async def invoke(_ctx, _args):
        return "3 open issues"

    factory = make_factory()
    info = await factory._execute_init_tools(
        [{"name": "beads_list"}],
        [SimpleNamespace(name="beads_list", on_invoke_tool=invoke)],
        SimpleNamespace(stream_observer=None),
    )

    assert info.ok and "3 open issues" in info


async def test_a_containers_init_tools_use_the_mounted_workspace():
    """init_tools see /workspace, not the container's filesystem root."""
    seen = []

    async def invoke(_ctx, args):
        seen.append(json.loads(args)["directory"])
        return "indexed"

    factory = make_factory()
    factory.container_id = "grid-test-container"

    await factory._execute_init_tools(
        [{"name": "init_codegraph", "parameters": {"directory": "${working_directory}"}}],
        [SimpleNamespace(name="init_codegraph", on_invoke_tool=invoke)],
        SimpleNamespace(stream_observer=None),
    )

    assert seen == ["/workspace"]


async def test_an_auto_run_tool_that_reports_failure_is_not_injected():
    factory = make_factory()
    config = auto_config({"name": "beads_init"})
    tool = SimpleNamespace(
        name="beads_init",
        on_invoke_tool=AsyncMock(return_value="❌ Error: no beads database found"),
    )

    info, complete = await factory._execute_auto_run_tools(
        "agent", config, [tool], ".", SimpleNamespace(stream_observer=None)
    )

    assert info == ""
    # A failure is not a policy block: the one-time mark stays, as before.
    assert complete


async def test_a_raising_init_tool_keeps_its_error_for_the_caller():
    async def invoke(_ctx, _args):
        raise RuntimeError("tracker is down")

    factory = make_factory()
    info = await factory._execute_init_tools(
        [{"name": "beads_show"}],
        [SimpleNamespace(name="beads_show", on_invoke_tool=invoke)],
        SimpleNamespace(stream_observer=None),
    )

    assert not info.ok
    (failure,) = info.failures
    assert failure.status == FAILED
    assert "tracker is down" in failure.detail
    assert "tracker is down" in info.failure_report()
