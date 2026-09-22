"""Exercise the example with real background scheduling and no provider calls."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.background_agents import BackgroundAgentSupervisor
from core import pipeline_runtime as runtime_module

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "coordinator-pipeline"


def load_example(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The example is a CLI directory (its public name contains a hyphen).
sys.path.insert(0, str(EXAMPLE))
try:
    runner = load_example("run")
    offline = load_example("offline")
finally:
    sys.path.remove(str(EXAMPLE))

PipelineRuntime = runtime_module.PipelineRuntime


class ControlledFactory(offline.DemoFactory):
    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Queue()
        self.instructions = []
        self.contracts = []

    async def create_dynamic_agent(self, **kwargs):
        self.instructions.append(kwargs["instructions"])
        return await super().create_dynamic_agent(**kwargs)

    async def run_agent_object_simple(self, worker, task, *, context_id):
        contract = json.loads(task)
        self.contracts.append(contract)
        await self.started.put(worker.name)
        await self.release.wait()
        if contract["goal"] == "crash":
            raise RuntimeError("worker failed")
        return "PASS: inspected the artifact and ran the regression cases"


@pytest_asyncio.fixture
async def system(tmp_path):
    factory = ControlledFactory()
    supervisor = BackgroundAgentSupervisor(
        factory=factory,
        catalog=offline.DemoCatalog(),
        default_timeout_seconds=10,
        max_timeout_seconds=30,
        worker_instructions="Independent Grid worker protocol",
    )
    runtime = PipelineRuntime(
        supervisor,
        goal="test goal",
        workdir=tmp_path,
        state_path=tmp_path / "state.json",
        deadline_seconds=30,
        max_attempts=2,
    )
    yield runtime, factory, supervisor
    await runtime.close()
    await supervisor.shutdown()


async def start(runtime, kind="research", **overrides):
    arguments = dict(
        goal="inspect behavior",
        scope="src and tests",
        inputs="repository",
        done_criteria="evidence for the behavior",
        limits="bounded change",
        kind=kind,
        model="scripted",
        tools=["file_read"],
        timeout_seconds=10,
    )
    arguments.update(overrides)
    task = await runtime.start_task(**arguments)
    return task["task_id"]


async def wait(runtime, task_id):
    await runtime.wait_tasks([task_id], timeout_seconds=2)
    state = await runtime.snapshot()
    return state["tasks"][task_id]["attempts"][-1]


async def review(runtime, task_id, decision="accepted", **kwargs):
    return await runtime.review_task(
        task_id,
        decision=decision,
        evidence="inspected source and regression output",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_real_parallel_research_and_exclusive_changes(system):
    runtime, factory, _ = system
    first, second = await asyncio.gather(start(runtime), start(runtime))
    # Both actually start before either is allowed to finish.
    await asyncio.wait_for(factory.started.get(), 1)
    await asyncio.wait_for(factory.started.get(), 1)
    with pytest.raises(ValueError, match="Workspace busy"):
        await start(runtime, "change", tools=["file_write"])
    with pytest.raises(ValueError, match="research profile"):
        await start(runtime, tools=["bash_tool"])
    factory.release.set()
    await wait(runtime, first)
    await wait(runtime, second)
    assert factory.instructions == ["Independent Grid worker protocol"] * 2
    await review(runtime, first)
    await review(runtime, second)
    await runtime.finish_run("Both independent findings checked")


@pytest.mark.asyncio
async def test_unverified_change_cannot_finish_or_be_discarded(system):
    runtime, factory, _ = system
    factory.release.set()
    change = await start(runtime, "change")
    await wait(runtime, change)
    with pytest.raises(ValueError, match="accepted verification"):
        await review(runtime, change)
    with pytest.raises(ValueError, match="Unresolved task"):
        await runtime.finish_run("unsupported success")
    with pytest.raises(ValueError, match="cannot be discarded"):
        await review(runtime, change, "discarded")
    with pytest.raises(ValueError, match="Resolve the existing change"):
        await start(runtime, "change")
    check = await start(runtime, "verification", verification_for=change)
    await wait(runtime, check)
    await review(runtime, check)
    await review(runtime, change, verification_task_id=check)
    state = await runtime.finish_run("Verified change")
    assert state["status"] == "completed"
    assert json.loads(runtime.state_path.read_text(encoding="utf-8")) == state


@pytest.mark.asyncio
async def test_correction_keeps_history_and_rejects_old_verification(system):
    runtime, factory, _ = system
    factory.release.set()
    change = await start(runtime, "change")
    await wait(runtime, change)
    old = await start(runtime, "verification", verification_for=change)
    await wait(runtime, old)
    await review(runtime, old)
    await review(runtime, change, "rejected")
    await runtime.retry_task(change, "A missed edge case requires correction")
    await wait(runtime, change)
    with pytest.raises(ValueError, match="this attempt"):
        await review(runtime, change, verification_task_id=old)
    check = await start(runtime, "verification", verification_for=change)
    await wait(runtime, check)
    await review(runtime, check)
    await review(runtime, change, verification_task_id=check)
    state = await runtime.finish_run("Corrected and reverified")
    attempts = state["tasks"][change]["attempts"]
    assert [a["review"]["decision"] for a in attempts] == ["rejected", "accepted"]
    correction = next(c for c in factory.contracts if "correction" in c)
    assert correction["correction"]["previous_attempt"]["number"] == 1


@pytest.mark.asyncio
async def test_retry_limits_and_failed_worker_cannot_be_accepted(system):
    runtime, factory, _ = system
    factory.release.set()
    task = await start(runtime, goal="crash")
    attempt = await wait(runtime, task)
    assert attempt["worker"]["status"] == "failed"
    with pytest.raises(ValueError, match="Only completed"):
        await review(runtime, task)
    await review(runtime, task, "rejected")
    with pytest.raises(ValueError, match="specific feedback"):
        await runtime.retry_task(task, "")
    await runtime.retry_task(task, "Retry a transient failure")
    await wait(runtime, task)
    await review(runtime, task, "rejected")
    with pytest.raises(ValueError, match="attempt budget"):
        await runtime.retry_task(task, "Again")


@pytest.mark.asyncio
async def test_worker_timeout_and_immediate_interrupt_are_persisted(system):
    runtime, _, _ = system
    slow = await start(runtime, timeout_seconds=1)
    attempt = await wait(runtime, slow)
    assert attempt["worker"]["status"] == "timed_out"
    cancelled = await start(runtime)
    await runtime.interrupt_task(cancelled, "No longer needed")
    state = await runtime.close("blocked", "Need user input")
    assert state["status"] == "blocked"
    assert (
        state["tasks"][cancelled]["attempts"][-1]["worker"]["status"] == "interrupted"
    )
    assert json.loads(runtime.state_path.read_text(encoding="utf-8")) == state


@pytest.mark.asyncio
async def test_completion_is_saved_without_coordinator_polling(system):
    runtime, factory, supervisor = system
    task = await start(runtime)
    factory.release.set()
    agent_id = runtime.state["tasks"][task]["attempts"][-1]["worker"]["agent_id"]
    await supervisor.wait_agents([agent_id], timeout_seconds=1)
    await asyncio.gather(*list(runtime._watchers))
    saved = json.loads(runtime.state_path.read_text(encoding="utf-8"))
    assert saved["tasks"][task]["attempts"][-1]["worker"]["status"] == "completed"
    assert saved["tasks"][task]["attempts"][-1]["review"] is None


@pytest.mark.asyncio
async def test_wait_ignores_already_completed_tasks(system):
    runtime, factory, _ = system
    factory.release.set()
    done = await start(runtime)
    await wait(runtime, done)
    factory.release.clear()
    pending = await start(runtime)
    waiter = asyncio.create_task(runtime.wait_tasks([done, pending], 2))
    await asyncio.sleep(0.02)
    assert not waiter.done()
    factory.release.set()
    state = await waiter
    assert state["tasks"][pending]["attempts"][-1]["worker"]["status"] == "completed"


@pytest.mark.asyncio
async def test_overall_deadline_bounds_coordinator_inference(system):
    runtime, _, _ = system
    runtime._deadline = asyncio.get_running_loop().time() + 0.05

    class NeverReturning(offline.DemoFactory):
        async def run_agent_object_simple(self, *args, **kwargs):
            await asyncio.Event().wait()

    state = await runner.run_coordinator(NeverReturning(), runtime, "scripted")
    assert state["status"] == "timed_out"


@pytest.mark.asyncio
async def test_early_coordinator_answer_is_not_success(system):
    runtime, _, _ = system

    class EarlyAnswer(offline.DemoFactory):
        async def run_agent_object_simple(self, *args, **kwargs):
            return "I think it is done"

    state = await runner.run_coordinator(EarlyAnswer(), runtime, "scripted")
    assert state["status"] == "blocked"
    assert "without finish_run" in state["summary"]


@pytest.mark.asyncio
async def test_launch_budget_and_terminal_run_prevent_more_work(system):
    runtime, factory, _ = system
    runtime.state["limits"]["max_launches"] = 1
    factory.release.set()
    task = await start(runtime)
    await wait(runtime, task)
    with pytest.raises(ValueError, match="launch budget"):
        await start(runtime)
    await review(runtime, task)
    await runtime.finish_run("done")
    with pytest.raises(ValueError, match="no more actions"):
        await start(runtime)


@pytest.mark.asyncio
async def test_existing_state_file_is_never_overwritten(system, tmp_path):
    runtime, _, supervisor = system
    original = runtime.state_path.read_bytes()
    with pytest.raises(FileExistsError):
        PipelineRuntime(
            supervisor, goal="new", workdir=tmp_path, state_path=runtime.state_path
        )
    assert runtime.state_path.read_bytes() == original


@pytest.mark.asyncio
async def test_sdk_tools_call_runtime_and_return_recoverable_errors(system):
    from agents import RunContextWrapper

    runtime, _, _ = system
    tools = {tool.name: tool for tool in runner.coordinator_tools(runtime)}
    ctx = RunContextWrapper(context=None)
    error = await tools["pipeline_review"].on_invoke_tool(
        ctx,
        json.dumps(
            {
                "task_id": "missing",
                "decision": "accepted",
                "evidence": "claim",
                "verification_task_id": None,
            }
        ),
    )
    assert "Unknown task" in error
    result = await tools["pipeline_finish"].on_invoke_tool(
        ctx, '{"summary": "A simple answer"}'
    )
    assert json.loads(result)["status"] == "completed"


@pytest.mark.asyncio
async def test_offline_demo_reproduces_failure_then_correction(tmp_path):
    state, path = await offline.run_demo(tmp_path)
    assert state["status"] == "completed"
    assert state["launches"] == 4
    checks = [t for t in state["tasks"].values() if t["kind"] == "verification"]
    assert [t["attempts"][-1]["review"]["decision"] for t in checks] == [
        "rejected",
        "accepted",
    ]
    assert "dict.fromkeys" in (path.parent / "events.py").read_text()


@pytest.mark.asyncio
async def test_real_factory_and_sdk_tools_complete_a_run_without_network(
    tmp_path, monkeypatch
):
    from agents import RunContextWrapper
    from core.agent_catalog import AgentCatalog
    from core.agent_factory import AgentFactory
    from core.config import Config
    from core.managers.project_tools_loader import (
        get_project_loader,
        set_project_loader,
    )
    from utils.path_utils import set_current_factory, reset_current_factory

    previous_loader = get_project_loader()
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    (tmp_path / "artifact.md").write_text("verified workspace marker", encoding="utf-8")
    config = Config(str(EXAMPLE / "config.yaml.example"), str(tmp_path))
    catalog = AgentCatalog(
        config,
        tools_directory=str(EXAMPLE.parent / "coder" / "tools"),
        provider_key="compatible",
    )
    factory = AgentFactory(
        config=config, working_directory=str(tmp_path), tracing_level=None
    )
    supervisor = BackgroundAgentSupervisor(
        factory=factory, catalog=catalog, worker_instructions="test worker"
    )
    runtime = PipelineRuntime(
        supervisor,
        goal="Inspect artifact.md",
        workdir=tmp_path,
        state_path=tmp_path / "state.json",
        deadline_seconds=30,
    )

    async def scripted_inference(agent, task, *, context_id):
        tools = {tool.name: tool for tool in agent.tools}
        ctx = RunContextWrapper(context=SimpleNamespace(factory=factory))

        async def invoke(name, **arguments):
            return await tools[name].on_invoke_tool(ctx, json.dumps(arguments))

        if agent.name != "pipeline-coordinator":
            set_current_factory(factory)
            try:
                return await invoke(
                    "file_read", filepath="artifact.md", offset=0, limit_lines=None
                )
            finally:
                reset_current_factory()
        started = json.loads(
            await invoke(
                "pipeline_task",
                goal="Read artifact",
                scope="artifact.md",
                inputs="",
                done_criteria="Report its marker",
                limits="Read only",
                kind="research",
                model="default",
                tools=["file_read"],
                timeout_seconds=10,
                verification_for=None,
            )
        )
        task_id = started["task_id"]
        state = json.loads(
            await invoke("pipeline_wait", task_ids=[task_id], timeout_seconds=5)
        )
        result = state["tasks"][task_id]["attempts"][-1]["worker"]["result"]
        assert "verified workspace marker" in result
        await invoke(
            "pipeline_review",
            task_id=task_id,
            decision="accepted",
            evidence=result,
            verification_task_id=None,
        )
        return await invoke(
            "pipeline_finish", summary="Read and verified the requested artifact"
        )

    monkeypatch.setattr(factory, "run_agent_object_simple", scripted_inference)
    try:
        state = await runner.run_coordinator(factory, runtime, "default")
        assert state["status"] == "completed"
        assert state["launches"] == 1
    finally:
        await runtime.close()
        await supervisor.shutdown()
        set_project_loader(previous_loader)


@pytest.mark.asyncio
async def test_coordinator_agent_of_the_system_drives_a_run(tmp_path):
    """The router's entry point: the system's agent opens and closes a run itself."""
    from agents import RunContextWrapper

    from core.agent_factory import AgentFactory, GridRunContext
    from core.config.config import Config

    factory = AgentFactory(
        config=Config(str(EXAMPLE / "config.yaml"), str(tmp_path)),
        working_directory=str(tmp_path),
        tracing_level=None,
    )
    try:
        agent = await factory.create_agent("pipeline_coordinator")
        tools = {tool.name: tool for tool in agent.tools}
        assert {"pipeline_start", "pipeline_finish", "file_read"} <= set(tools)

        ctx = RunContextWrapper(
            context=GridRunContext(
                factory=factory, context_id="ctx-test", agent_id="pipeline_coordinator"
            )
        )

        early = json.loads(await tools["pipeline_inspect"].on_invoke_tool(ctx, "{}"))
        assert "pipeline_start" in early["error"]

        opened = json.loads(
            await tools["pipeline_start"].on_invoke_tool(
                ctx, json.dumps({"goal": "Split the parser", "deadline_seconds": 60})
            )
        )
        assert Path(opened["workdir"]) == tmp_path.resolve()
        assert opened["system"]["models"], "workers need a model catalog"
        assert set(opened["system"]["task_tools"]) == {
            "research",
            "change",
            "verification",
        }

        busy = json.loads(
            await tools["pipeline_start"].on_invoke_tool(ctx, json.dumps({"goal": "other"}))
        )
        assert "already open" in busy["error"]

        closed = json.loads(
            await tools["pipeline_finish"].on_invoke_tool(
                ctx, json.dumps({"summary": "Answered without delegation"})
            )
        )
        assert closed["status"] == "completed"
        assert Path(opened["state_path"]).exists()
    finally:
        await factory.cleanup()
