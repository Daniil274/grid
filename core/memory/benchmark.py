"""Deterministic synthetic benchmark for memory retrieval approaches.

Run from grid/: ``python -m core.memory.benchmark``. Timings are local diagnostics,
not a cross-machine performance claim; workload and token estimator are fixed.
"""
from __future__ import annotations

import json
import logging
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from .manager import MemoryManager
from .models import MemoryCategory, MemoryLayer, MemoryQuery, MemoryRecord, MemoryScope
from .sqlite_backend import SQLiteMemoryBackend
from .tencent_backend import TencentGatewayConfig, TencentMemoryBackend


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {"p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[min(len(ordered)-1, int(.95 * len(ordered)))], 3)}


def run_benchmark() -> dict:
    with tempfile.TemporaryDirectory(prefix="grid-memory-bench-") as tmp:
        root = Path(tmp)
        source = root / "storage.py"
        source.write_text("ENGINE = 'sqlite'\n", encoding="utf8")
        local = SQLiteMemoryBackend(root)
        truths = [f"Project decision {i}: subsystem-{i} uses deterministic strategy-{i}"
                  for i in range(80)]
        records = []
        for i, text in enumerate(truths):
            rec = MemoryRecord(content=text, key=f"fact-{i}", category=MemoryCategory.DECISION,
                               source_ref="file:storage.py" if i == 0 else None)
            records.append(rec)
        records += [MemoryRecord(content=f"Scenario {i}: deploy service-{i} safely", layer=MemoryLayer.L2)
                    for i in range(5)]
        records += [MemoryRecord(content="Profile: prefer concise Python examples", layer=MemoryLayer.L3,
                                 scope=MemoryScope.USER)]
        write_times = []
        for record in records:
            start = time.perf_counter(); local.remember(record); write_times.append((time.perf_counter()-start)*1000)
        source.write_text("ENGINE = 'postgres'\n", encoding="utf8")
        query = "subsystem-37 deterministic strategy-37"
        search_times = []; hits = []
        for _ in range(30):
            start=time.perf_counter(); result=local.search(MemoryQuery(text=query, limit=10)); search_times.append((time.perf_counter()-start)*1000); hits=result
        ids = {h.record.id for h in hits}
        expected = records[37].id
        rank = next((i for i,h in enumerate(hits,1) if h.record.id == expected), None)
        stale = local.get(records[0].id).stale_status.value
        history = "\n".join(f"Session {s}: discussion and implementation detail " * 140 for s in range(12))
        baseline_tokens = len(history.encode("utf8"))
        manager = MemoryManager(local)
        section = manager.render_prompt_section(query, max_tokens=2500).content
        memory_tokens = len(section.encode("utf8"))
        # Deterministic mock implements v3 write/search envelope and stores tenant-bound rows.
        remote_rows: dict[str, dict] = {}
        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == "/v3/memories":
                body=json.loads(req.content); row=body["record"]; remote_rows[row["id"]]=row
                return httpx.Response(200,json={"code":0,"data":{"record":row}})
            if req.method == "POST" and req.url.path == "/v3/memories/search":
                body=json.loads(req.content); words=body["query"].casefold().split()
                found=[r for r in remote_rows.values() if all(w in r["content"].casefold() for w in words)]
                return httpx.Response(200,json={"code":0,"data":{"records":found}})
            return httpx.Response(200,json={"code":0,"data":{"records":[]}})
        mirror_local=SQLiteMemoryBackend(root, db_path="mirror.sqlite", tenant_id="bench")
        gateway=TencentMemoryBackend(TencentGatewayConfig(team_id="team",user_id="user",agent_id="agent",service_id="bench"), transport=httpx.MockTransport(handler), fallback=mirror_local, mirror=True)
        hybrid_writes=[]
        for r in records:
            start=time.perf_counter(); gateway.remember(r); hybrid_writes.append((time.perf_counter()-start)*1000)
        start=time.perf_counter(); hybrid_hits=gateway.search(MemoryQuery(text=query,limit=10)); hybrid_search=(time.perf_counter()-start)*1000
        gateway.close(); local.close()
        return {"workload":{"L1":80,"L2":5,"L3":1,"L0_history_sessions":12},
                "token_estimator":"UTF-8 bytes (same conservative estimator as memory prompt budget)",
                "baseline":{"prompt_tokens":baseline_tokens,"recovery_recall_at_10":0.0},
                "sqlite_tool_driven":{"prompt_tokens":len(query.encode()),"latency_write":_summary(write_times),"latency_search":_summary(search_times),"recall_at_10":int(expected in ids),"mrr":round(1/rank if rank else 0,3),"stale_status_after_file_change":stale},
                "sqlite_runtime_injection":{"prompt_tokens":memory_tokens,"saving_vs_history_pct":round(100*(1-memory_tokens/baseline_tokens),2)},
                "tencent_hybrid_mock":{"latency_write":_summary(hybrid_writes),"latency_search_ms":round(hybrid_search,3),"recall_at_10":int(any(h.record.id==expected for h in hybrid_hits)),"mirror":True}}


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
