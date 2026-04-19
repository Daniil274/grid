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
