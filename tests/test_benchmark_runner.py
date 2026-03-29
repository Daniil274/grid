import json

import pytest

from benchmarks.run import BenchmarkRunner
from schemas import BenchmarkAdapterResult


class FakeAdapter:
    def __init__(self, results):
        self.results = results

    async def run_fixture(self, fixture):
        return self.results[fixture.id]


def test_benchmark_runner_loads_metrics_and_fixtures():
    runner = BenchmarkRunner(
        config_path="config.yaml",
        metrics_path="benchmarks/metrics.yaml",
        fixtures_dir="benchmarks/fixtures",
        adapter=FakeAdapter({}),
    )

    metrics = runner.load_metrics()
    fixtures = runner.load_fixtures()

    assert metrics.metrics
    assert len(fixtures) >= 5


@pytest.mark.asyncio
async def test_benchmark_runner_produces_scorecard(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("settings:\n  default_agent: chat_agent\n", encoding="utf-8")

    adapter = FakeAdapter(
        {
            "agent-info": BenchmarkAdapterResult(
                output="code_agent uses kimi-k2.5-opencode",
                tools_used=["system_get_agent_info"],
                latency_ms=100,
            ),
            "get-context": BenchmarkAdapterResult(
                output="Current context is available.",
                tools_used=["system_get_context"],
                latency_ms=120,
            ),
            "list-agents": BenchmarkAdapterResult(
                output="chat agent and code agent are available",
                tools_used=["system_list_agents"],
                latency_ms=110,
            ),
            "list-tools": BenchmarkAdapterResult(
                output="категории инструментов доступны",
                tools_used=["system_list_tools"],
                latency_ms=105,
            ),
            "system-help": BenchmarkAdapterResult(
                output="This tool explains the system architecture.",
                tools_used=["system_help"],
                latency_ms=130,
            ),
        }
    )

    runner = BenchmarkRunner(
        config_path=str(config_path),
        metrics_path="benchmarks/metrics.yaml",
        fixtures_dir="benchmarks/fixtures",
        adapter=adapter,
    )

    scorecard = await runner.run()

    assert scorecard.fixture_count >= 5
    assert scorecard.passed_count == scorecard.fixture_count
    assert scorecard.aggregate_score == 1.0
    assert scorecard.summary["tool_accuracy_rate"] == 1.0


def test_baseline_write_is_immutable(tmp_path):
    from schemas import BenchmarkScorecard

    scorecard = BenchmarkScorecard(
        run_id="bench-test",
        config_path="config.yaml",
        metrics_path="benchmarks/metrics.yaml",
    )
    baseline_path = tmp_path / "baseline.json"

    BenchmarkRunner.write_baseline(scorecard, str(baseline_path))

    with pytest.raises(FileExistsError):
        BenchmarkRunner.write_baseline(scorecard, str(baseline_path))


def test_scorecard_write_creates_json(tmp_path):
    from schemas import BenchmarkScorecard

    scorecard = BenchmarkScorecard(
        run_id="bench-test",
        config_path="config.yaml",
        metrics_path="benchmarks/metrics.yaml",
    )
    output_path = tmp_path / "scorecard.json"

    BenchmarkRunner.write_scorecard(scorecard, str(output_path))

    raw = json.loads(output_path.read_text(encoding="utf-8"))
    assert raw["run_id"] == "bench-test"
