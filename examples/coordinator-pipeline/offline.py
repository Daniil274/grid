"""Deterministic demo: real lifecycle and files, scripted agents, no model calls."""

from __future__ import annotations

import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import uuid

from core.background_agents import BackgroundAgentSupervisor
from core.pipeline_runtime import PipelineRuntime, TOOL_PROFILES


class DemoCatalog:
    def require_model(self, model):
        if model != "scripted":
            raise ValueError("The offline demo only has the scripted model")

    def require_tools(self, tools):
        if set(tools) - set().union(*TOOL_PROFILES.values()):
            raise ValueError("Unknown demo tools")
        return list(dict.fromkeys(tools))

    def system_info(self, *, constraints):
        return {
            "models": [{"key": "scripted"}],
            "tools": [
                {"name": name} for name in sorted(set().union(*TOOL_PROFILES.values()))
            ],
            "constraints": constraints,
        }


class DemoFactory:
    async def create_dynamic_agent(self, *, name, instructions, model_key, tool_names):
        return SimpleNamespace(
            name=name, tools=[SimpleNamespace(name=t) for t in tool_names]
        )

    async def run_agent_object_simple(self, worker, task, *, context_id):
        contract = json.loads(task)
        path = Path(contract["workdir"]) / "events.py"
        if contract["kind"] == "change":
            fixed = bool(contract.get("correction"))
            implementation = "list(dict.fromkeys(events))" if fixed else "list(events)"
            path.write_text(
                f"def process(events):\n    return {implementation}\n", encoding="utf-8"
            )
            return f"Updated {path}. Independent regression check still required."
        if contract["kind"] == "verification":
            process = runpy.run_path(str(path))["process"]
            cases = [([], []), (["a"], ["a"]), (["a", "b", "a"], ["a", "b"])]
            failures = [
                f"{values!r}: got {process(values)!r}, expected {expected!r}"
                for values, expected in cases
                if process(values) != expected
            ]
            return (
                ("FAIL: " + "; ".join(failures))
                if failures
                else "PASS: all 3 regression cases"
            )
        return f"Inspected {path}: repeated event IDs must be ignored, order preserved."

    async def cleanup(self):
        pass


async def run_demo(state_dir: Path) -> tuple[dict, Path]:
    folder = state_dir / f"offline-{uuid.uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=False)
    state_path = folder / "state.json"
    supervisor = BackgroundAgentSupervisor(
        factory=DemoFactory(),
        catalog=DemoCatalog(),
        default_timeout_seconds=10,
        max_timeout_seconds=30,
        worker_instructions="Execute the scripted offline demonstration.",
    )
    runtime = PipelineRuntime(
        supervisor,
        goal="Ignore duplicate events while preserving order",
        workdir=folder,
        state_path=state_path,
        deadline_seconds=60,
    )

    async def start(kind, target=None):
        task = await runtime.start_task(
            goal="Ignore duplicate events while preserving order",
            scope="events.py",
            inputs="Event identifiers are strings",
            done_criteria="Empty, unique and repeated event sequences pass",
            limits="Only change events.py",
            kind=kind,
            model="scripted",
            tools=["file_write"] if kind == "change" else ["file_read", "bash_tool"],
            verification_for=target,
        )
        await runtime.wait_tasks([task["task_id"]], 5)
        return task["task_id"]

    try:
        change = await start("change")
        first_check = await start("verification", change)
        first_result = runtime.state["tasks"][first_check]["attempts"][-1]["worker"][
            "result"
        ]
        if not first_result.startswith("FAIL:"):
            raise AssertionError("The demo must first reproduce a failed verification")
        await runtime.review_task(
            first_check, decision="rejected", evidence=first_result
        )
        await runtime.review_task(change, decision="rejected", evidence=first_result)
        await runtime.retry_task(
            change, "Deduplicate event IDs and preserve first-seen order"
        )
        await runtime.wait_tasks([change], 5)
        final_check = await start("verification", change)
        final_result = runtime.state["tasks"][final_check]["attempts"][-1]["worker"][
            "result"
        ]
        if not final_result.startswith("PASS:"):
            raise AssertionError("The correction must pass the regression cases")
        await runtime.review_task(
            final_check, decision="accepted", evidence=final_result
        )
        await runtime.review_task(
            change,
            decision="accepted",
            evidence=final_result,
            verification_task_id=final_check,
        )
        state = await runtime.finish_run(
            "Offline demo: first verification failed; the correction passed all 3 cases. "
            "Two change attempts and both verification results are saved. No API calls."
        )
        return state, state_path
    finally:
        await runtime.close()
        await supervisor.shutdown()
