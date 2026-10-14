"""Reproducible end-to-end synthetic comparison; metrics are asserted, not timed."""
from core.memory.benchmark import run_benchmark


def test_benchmark_compares_memory_approaches_and_staleness():
    result = run_benchmark()
    assert result["workload"] == {"L1": 80, "L2": 5, "L3": 1, "L0_history_sessions": 12}
    assert result["baseline"]["prompt_tokens"] > result["sqlite_runtime_injection"]["prompt_tokens"]
    assert result["sqlite_tool_driven"]["recall_at_10"] == 1
    assert result["sqlite_tool_driven"]["mrr"] > 0
    assert result["sqlite_tool_driven"]["stale_status_after_file_change"] == "stale"
    assert result["tencent_hybrid_mock"]["mirror"] is True
    assert result["tencent_hybrid_mock"]["recall_at_10"] == 1
    for metric in (result["sqlite_tool_driven"]["latency_write"],
                   result["sqlite_tool_driven"]["latency_search"]):
        assert 0 <= metric["p50_ms"] <= metric["p95_ms"]
