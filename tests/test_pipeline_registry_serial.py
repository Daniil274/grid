import asyncio
import uuid

import pytest

from core.tracing.pipeline_registry import PipelineRegistry


@pytest.mark.asyncio
async def test_run_serialized_step_executes_nested_subtrees_depth_first():
    registry = PipelineRegistry()
    context_id = f"ctx-{uuid.uuid4().hex[:8]}"
    pipeline_id = await registry.get_or_create_pipeline(
        orchestrator_name="test-orchestrator",
        context_id=context_id,
        user_id="test-user",
    )

    order: list[str] = []

    async def first_step():
        order.append("first:start")

        async def child_step():
            order.append("first:child")
            return "child-result"

        child_result = await registry.run_serialized_step(
            pipeline_id=pipeline_id,
            agent_name="child-step",
            step_coro_factory=child_step,
        )
        assert child_result == "child-result"

        order.append("first:end")
        return "first-result"

    async def second_step():
        order.append("second")
        return "second-result"

    first_task = asyncio.create_task(
        registry.run_serialized_step(
            pipeline_id=pipeline_id,
            agent_name="first-step",
            step_coro_factory=first_step,
        )
    )
    await asyncio.sleep(0)
    second_task = asyncio.create_task(
        registry.run_serialized_step(
            pipeline_id=pipeline_id,
            agent_name="second-step",
            step_coro_factory=second_step,
        )
    )

    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result == "first-result"
    assert second_result == "second-result"
    assert order == ["first:start", "first:child", "first:end", "second"]


@pytest.mark.asyncio
async def test_get_or_create_pipeline_reuses_running_pipeline_for_context():
    registry = PipelineRegistry()
    context_id = f"ctx-{uuid.uuid4().hex[:8]}"

    pipeline_id = await registry.get_or_create_pipeline(
        orchestrator_name="first",
        context_id=context_id,
        user_id="test-user",
    )
    reused_pipeline_id = await registry.get_or_create_pipeline(
        orchestrator_name="second",
        context_id=context_id,
        user_id="test-user",
    )

    assert reused_pipeline_id == pipeline_id


async def _pipeline(registry: PipelineRegistry) -> str:
    return await registry.get_or_create_pipeline(
        orchestrator_name="test-orchestrator",
        context_id=f"ctx-{uuid.uuid4().hex[:8]}",
        user_id="test-user",
    )


@pytest.mark.asyncio
async def test_cancelling_a_step_stops_its_work_and_frees_the_pipeline():
    # A turn that times out or is stopped cancels its step: the work under it
    # (a subagent of orchestrate) must stop too, not go on holding the pipeline.
    registry = PipelineRegistry()
    pipeline_id = await _pipeline(registry)
    started, finished = asyncio.Event(), asyncio.Event()

    async def long_work():
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            finished.set()

    step = asyncio.create_task(
        registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="subagent", step_coro_factory=long_work)
    )
    await started.wait()
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step

    assert finished.is_set()

    async def next_turn():
        return "next"

    assert await asyncio.wait_for(
        registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="next", step_coro_factory=next_turn), 1
    ) == "next"


@pytest.mark.asyncio
async def test_a_step_cancelled_while_waiting_leaves_the_pipeline_free():
    registry = PipelineRegistry()
    pipeline_id = await _pipeline(registry)
    release = asyncio.Event()

    async def holder():
        await release.wait()

    async def waiter():
        return "never"

    holding = asyncio.create_task(
        registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="holder", step_coro_factory=holder)
    )
    await asyncio.sleep(0.01)
    waiting = asyncio.create_task(
        registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="waiter", step_coro_factory=waiter)
    )
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await holding

    async def after():
        return "after"

    assert await asyncio.wait_for(
        registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="after", step_coro_factory=after), 1
    ) == "after"


@pytest.mark.asyncio
async def test_a_nested_step_that_outlives_its_holder_does_not_keep_the_pipeline():
    # A step of the holder's subtree that runs on after the holder ended - a
    # detached task, a run never awaited - must not hold the pipeline locked.
    registry = PipelineRegistry()
    pipeline_id = await _pipeline(registry)
    nested_started = asyncio.Event()
    stuck: list[asyncio.Task] = []

    async def never_ends():
        nested_started.set()
        await asyncio.Event().wait()

    async def holder():
        stuck.append(
            asyncio.create_task(
                registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="nested", step_coro_factory=never_ends)
            )
        )
        await nested_started.wait()
        return "done"

    assert await registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="holder", step_coro_factory=holder) == "done"

    async def next_turn():
        return "next"

    try:
        assert await asyncio.wait_for(
            registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="next", step_coro_factory=next_turn), 1
        ) == "next"
    finally:
        stuck[0].cancel()
        await asyncio.gather(stuck[0], return_exceptions=True)
