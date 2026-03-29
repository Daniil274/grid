"""Benchmark runner for Stage 1 evaluation harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Protocol

import yaml

from schemas import (
    BenchmarkAdapterResult,
    BenchmarkFixture,
    BenchmarkFixtureResult,
    BenchmarkMetricsDocument,
    BenchmarkScorecard,
)


class BenchmarkAdapter(Protocol):
    """Execution adapter protocol for benchmark fixtures."""

    async def run_fixture(self, fixture: BenchmarkFixture) -> BenchmarkAdapterResult:
        """Execute a fixture and return normalized result data."""

    async def cleanup(self) -> None:
        """Release adapter resources after the run."""


class _ToolCaptureObserver:
    """Minimal stream observer that records tool calls during a benchmark run."""

    def __init__(self) -> None:
        self.tools_used: List[str] = []
        self.text_fragments: List[str] = []

    def handle_event(self, event: Any, *, agent_key: str | None = None) -> str | None:
        try:
            from agents import RawResponsesStreamEvent, RunItemStreamEvent

            if isinstance(event, RunItemStreamEvent):
                if getattr(event, "name", "") == "tool_called":
                    item = getattr(event, "item", None)
                    raw_item = getattr(item, "raw_item", None) if item is not None else None
                    tool_name = getattr(raw_item, "name", None)
                    if tool_name:
                        self.tools_used.append(str(tool_name))
            elif isinstance(event, RawResponsesStreamEvent):
                content = getattr(event, "content", None) or getattr(event, "delta", None)
                if isinstance(content, str):
                    self.text_fragments.append(content)
                    return content
        except Exception:
            return None
        return None


class LiveGridAdapter:
    """Run fixtures against a real Grid agent via AgentFactory."""

    def __init__(
        self,
        config_path: str,
        working_directory: str | None = None,
        *,
        disable_auto_run_tools: bool = True,
    ):
        self.config_path = config_path
        self.working_directory = working_directory
        self.disable_auto_run_tools = disable_auto_run_tools
        self._config = None
        self._factory = None

    async def _ensure_factory(self) -> None:
        if self._factory is not None:
            return
        from core.config import Config
        from core.agent_factory import AgentFactory

        self._config = Config(self.config_path, self.working_directory)
        if self.disable_auto_run_tools:
            for agent_cfg in self._config.config.agents.values():
                if getattr(agent_cfg, "auto_run_tools", None):
                    agent_cfg.auto_run_tools = []
        self._factory = AgentFactory(config=self._config, working_directory=self.working_directory)

    async def run_fixture(self, fixture: BenchmarkFixture) -> BenchmarkAdapterResult:
        await self._ensure_factory()
        assert self._factory is not None
        started = time.perf_counter()
        observer = _ToolCaptureObserver()
        output = await self._factory.run_agent(
            fixture.agent,
            fixture.prompt,
            context_path=fixture.context.get("context_path"),
            user_id=fixture.context.get("user_id"),
            stream=True,
            stream_observer=observer,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        executions = self._factory.get_recent_executions(limit=1)
        tools_used: List[str] = list(dict.fromkeys(observer.tools_used))
        token_usage = None
        if executions:
            last = executions[-1]
            execution_tools = list(getattr(last, "tools_used", []) or [])
            if execution_tools:
                tools_used = list(dict.fromkeys([*tools_used, *execution_tools]))
            token_usage = getattr(last, "token_usage", None)
        return BenchmarkAdapterResult(
            output=str(output),
            tools_used=tools_used,
            latency_ms=latency_ms,
            token_usage=token_usage,
        )

    async def cleanup(self) -> None:
        if self._factory is not None:
            await self._factory.cleanup()


class BenchmarkRunner:
    """Fixture-driven benchmark runner."""

    def __init__(
        self,
        *,
        config_path: str,
        metrics_path: str,
        fixtures_dir: str,
        adapter: BenchmarkAdapter,
    ):
        self.config_path = Path(config_path)
        self.metrics_path = Path(metrics_path)
        self.fixtures_dir = Path(fixtures_dir)
        self.adapter = adapter

    def load_metrics(self) -> BenchmarkMetricsDocument:
        with self.metrics_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return BenchmarkMetricsDocument(**raw)

    def load_fixtures(self) -> List[BenchmarkFixture]:
        fixtures: List[BenchmarkFixture] = []
        for path in sorted(self.fixtures_dir.glob("*.yaml")):
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            fixtures.append(BenchmarkFixture(**raw))
        return fixtures

    def _evaluate_fixture(
        self,
        fixture: BenchmarkFixture,
        adapter_result: BenchmarkAdapterResult,
    ) -> BenchmarkFixtureResult:
        checks: Dict[str, bool] = {}
        errors: List[str] = []
        output = adapter_result.output or ""
        output_lower = output.lower()
        tools_used = adapter_result.tools_used or []

        checks["no_adapter_error"] = adapter_result.error is None
        if adapter_result.error:
            errors.append(adapter_result.error)

        checks["required_tools"] = all(tool in tools_used for tool in fixture.required_tools)
        if not checks["required_tools"]:
            missing = [tool for tool in fixture.required_tools if tool not in tools_used]
            if missing:
                errors.append(f"Missing required tools: {', '.join(missing)}")

        checks["forbidden_tools"] = all(tool not in tools_used for tool in fixture.forbidden_tools)
        if not checks["forbidden_tools"]:
            present = [tool for tool in fixture.forbidden_tools if tool in tools_used]
            if present:
                errors.append(f"Forbidden tools were used: {', '.join(present)}")

        checks["required_substrings"] = all(sub.lower() in output_lower for sub in fixture.required_substrings)
        if not checks["required_substrings"]:
            missing = [sub for sub in fixture.required_substrings if sub.lower() not in output_lower]
            if missing:
                errors.append(f"Missing required substrings: {', '.join(missing)}")

        checks["forbidden_substrings"] = all(sub.lower() not in output_lower for sub in fixture.forbidden_substrings)
        if not checks["forbidden_substrings"]:
            present = [sub for sub in fixture.forbidden_substrings if sub.lower() in output_lower]
            if present:
                errors.append(f"Forbidden substrings present: {', '.join(present)}")

        if fixture.max_latency_ms is not None:
            checks["latency_budget"] = adapter_result.latency_ms <= fixture.max_latency_ms
            if not checks["latency_budget"]:
                errors.append(
                    f"Latency {adapter_result.latency_ms}ms exceeds budget {fixture.max_latency_ms}ms"
                )
        else:
            checks["latency_budget"] = True

        passed = all(checks.values())
        return BenchmarkFixtureResult(
            fixture_id=fixture.id,
            passed=passed,
            output=output,
            tools_used=tools_used,
            latency_ms=adapter_result.latency_ms,
            token_usage=adapter_result.token_usage,
            checks=checks,
            errors=errors,
            metadata=adapter_result.metadata,
        )

    async def run(self) -> BenchmarkScorecard:
        self.load_metrics()  # validates metrics document, even if evaluation is structural for now
        fixtures = self.load_fixtures()

        results: List[BenchmarkFixtureResult] = []
        try:
            for fixture in fixtures:
                adapter_result = await self.adapter.run_fixture(fixture)
                results.append(self._evaluate_fixture(fixture, adapter_result))
        finally:
            cleanup = getattr(self.adapter, "cleanup", None)
            if cleanup is not None:
                await cleanup()

        fixture_count = len(results)
        passed_count = sum(1 for result in results if result.passed)
        aggregate_score = (passed_count / fixture_count) if fixture_count else 0.0
        average_latency_ms = (
            sum(result.latency_ms for result in results) / fixture_count if fixture_count else 0.0
        )
        tool_accuracy_count = sum(
            1
            for result in results
            if result.checks.get("required_tools", False) and result.checks.get("forbidden_tools", False)
        )

        config_snapshot = self._load_config_snapshot()
        return BenchmarkScorecard(
            run_id=f"bench-{uuid.uuid4().hex[:8]}",
            config_path=str(self.config_path),
            metrics_path=str(self.metrics_path),
            fixture_count=fixture_count,
            passed_count=passed_count,
            aggregate_score=aggregate_score,
            average_latency_ms=average_latency_ms,
            config_snapshot=config_snapshot,
            results=results,
            summary={
                "task_completion_pass_rate": aggregate_score,
                "tool_accuracy_rate": (tool_accuracy_count / fixture_count) if fixture_count else 0.0,
                "average_latency_ms": average_latency_ms,
            },
        )

    def _load_config_snapshot(self) -> Dict[str, Any]:
        with self.config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return raw

    @staticmethod
    def write_scorecard(scorecard: BenchmarkScorecard, output_path: str) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(scorecard.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def write_baseline(scorecard: BenchmarkScorecard, baseline_path: str) -> Path:
        path = Path(baseline_path)
        if path.exists():
            raise FileExistsError(f"Baseline already exists: {path}")
        return BenchmarkRunner.write_scorecard(scorecard, str(path))


async def _run_cli(args: argparse.Namespace) -> int:
    adapter = LiveGridAdapter(
        config_path=args.config,
        working_directory=args.workdir,
        disable_auto_run_tools=not args.keep_auto_run_tools,
    )
    runner = BenchmarkRunner(
        config_path=args.config,
        metrics_path=args.metrics,
        fixtures_dir=args.fixtures,
        adapter=adapter,
    )
    scorecard = await runner.run()
    output_path = BenchmarkRunner.write_scorecard(scorecard, args.output)
    print(f"Scorecard written to {output_path}")
    if args.write_baseline:
        baseline_path = BenchmarkRunner.write_baseline(scorecard, args.baseline)
        print(f"Baseline written to {baseline_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Grid benchmark fixtures")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--metrics", default="benchmarks/metrics.yaml")
    parser.add_argument("--fixtures", default="benchmarks/fixtures")
    parser.add_argument("--output", default="benchmarks/runs/latest.json")
    parser.add_argument("--baseline", default="benchmarks/baseline.json")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--keep-auto-run-tools", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
