"""Run an autonomous Grid coordinator, or a deterministic offline demonstration."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Optional
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pipeline_runtime import PipelineRuntime, READ_TOOLS, TOOL_PROFILES
from tools.pipeline_tools import PIPELINE_TOOLS, attach_runtime


def coordinator_tools(runtime: Optional[PipelineRuntime]) -> list:
    """Bind this run to the shared pipeline tools.

    The same tools serve the coordinator agent of a Grid system; here the host
    opens the run itself, so ``pipeline_start`` is not offered to the model.
    """
    if runtime is not None:
        attach_runtime(runtime)
    return [tool for name, tool in PIPELINE_TOOLS.items() if name != "pipeline_start"]


async def run_coordinator(factory, runtime: PipelineRuntime, model: str) -> dict:
    """The outer deadline also bounds coordinator inference, not just workers."""

    async def execute() -> None:
        agent = await factory.create_dynamic_agent(
            name="pipeline-coordinator",
            instructions=(HERE / "skills" / "coordinator.md").read_text(
                encoding="utf-8"
            ),
            model_key=model,
            tool_names=sorted(READ_TOOLS),
        )
        actual = {tool.name for tool in agent.tools}
        if actual != READ_TOOLS:
            raise ValueError(
                f"Coordinator read tools did not resolve exactly: {actual}"
            )
        agent.tools.extend(coordinator_tools(runtime))
        output = await factory.run_agent_object_simple(
            agent,
            json.dumps(
                {
                    "goal": runtime.state["goal"],
                    "workdir": runtime.state["workdir"],
                    "system": runtime.system(),
                },
                ensure_ascii=False,
            ),
            context_id=f"ctx-{uuid.uuid4().hex[:12]}",
        )
        if runtime.state["status"] == "running":
            await runtime.close(
                "blocked",
                "Coordinator returned without finish_run or block_run. " + str(output),
            )

    try:
        await asyncio.wait_for(execute(), timeout=runtime.remaining_seconds)
    except asyncio.TimeoutError:
        await runtime.close("timed_out", "Overall run deadline exceeded")
    except asyncio.CancelledError:
        await runtime.close("interrupted", "Coordinator interrupted")
        raise
    except Exception as exc:
        await runtime.close("failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await runtime.close()
    return await runtime.snapshot()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("goal", nargs="?", help="The user's task")
    parser.add_argument("--config", type=Path, default=HERE / "config.yaml.example")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--model", help="Coordinator model key; defaults to the coordinator config"
    )
    parser.add_argument("--state-dir", type=Path, default=HERE / "runs")
    parser.add_argument(
        "--deadline", type=int, default=900, help="Whole run deadline in seconds"
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-launches", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config, tools and schemas without API calls",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a scripted defect/correction example without API calls",
    )
    args = parser.parse_args(argv)
    if any(
        value < 1
        for value in (
            args.deadline,
            args.max_attempts,
            args.max_launches,
            args.concurrency,
        )
    ):
        parser.error(
            "deadline, max-attempts, max-launches and concurrency must be positive"
        )
    if not args.goal and not (args.check or args.demo):
        parser.error("supply a goal, --check or --demo")
    return args


async def main(args: argparse.Namespace) -> int:
    if args.demo:
        from offline import run_demo

        state, path = await run_demo(args.state_dir.resolve())
    else:
        from core.agent_catalog import AgentCatalog
        from core.config import Config

        workdir = args.workdir.resolve()
        if not workdir.is_dir():
            raise ValueError(f"Working directory does not exist: {workdir}")
        config = Config(str(args.config.resolve()), str(workdir))
        if Path(config.get_working_directory()).resolve() != workdir:
            raise ValueError(
                "Config ignores --workdir; enable settings.allow_path_override"
            )
        model = args.model or config.get_agent("coordinator").model
        provider = config.get_model(model).provider
        catalog = AgentCatalog(
            config,
            tools_directory=str(ROOT / "examples" / "coder" / "tools"),
            provider_key=provider,
        )
        catalog.require_model(model)
        catalog.require_tools(sorted(set().union(*TOOL_PROFILES.values())))
        if args.check:
            names = [tool.name for tool in coordinator_tools(None)]
            print(
                json.dumps(
                    {
                        "models": catalog.model_keys(),
                        "tools": catalog.tool_names(),
                        "coordinator_tools": names,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        from core.agent_factory import AgentFactory
        from core.background_agents import BackgroundAgentSupervisor

        factory = AgentFactory(
            config=config, working_directory=str(workdir), tracing_level=None
        )
        supervisor = BackgroundAgentSupervisor(
            factory=factory,
            catalog=catalog,
            max_concurrency=args.concurrency,
            default_timeout_seconds=min(300, args.deadline),
            max_timeout_seconds=args.deadline,
            worker_instructions=(HERE / "skills" / "worker.md").read_text(
                encoding="utf-8"
            ),
        )
        path = args.state_dir.resolve() / f"{uuid.uuid4().hex}.json"
        try:
            runtime = PipelineRuntime(
                supervisor,
                goal=args.goal,
                workdir=workdir,
                state_path=path,
                deadline_seconds=args.deadline,
                max_attempts=args.max_attempts,
                max_launches=args.max_launches,
            )
            print(f"Run state: {path}", flush=True)
            state = await run_coordinator(factory, runtime, model)
        finally:
            await supervisor.shutdown()
    print(
        json.dumps(
            {
                "status": state["status"],
                "summary": state["summary"],
                "state_path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if state["status"] == "completed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
