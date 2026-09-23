"""Run the administrator system on one request, headless, and record its tool trace.

Started by the harness as a separate process whose working directory is the
workshop, so a path that escapes the workshop lands nowhere near the operator's
checkout. The trace includes the specialists' tool calls: the run's observer is
shared with sub-agents.

    python -m evals.self_improvement.agent_runner --workshop W --message M --out run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

OUTPUT_LIMIT = 4000


def make_recorder(log_path: Path):
    from agents import RawResponsesStreamEvent, RunItemStreamEvent

    from core.agent_factory import ConsoleStreamObserver, tool_event_info

    log = log_path.open("a", encoding="utf-8")

    def write(message: str = "", end: str = "\n", flush: bool = False) -> None:
        log.write(str(message) + end)
        if flush:
            log.flush()

    class Recorder(ConsoleStreamObserver):
        def __init__(self) -> None:
            super().__init__(output_writer=write, render_text_deltas=False)
            self.started = time.monotonic()
            self.events: list[dict[str, Any]] = []
            # agent -> {"input": n, "output": n}, from each model response's usage.
            self.tokens: dict[str, dict[str, int]] = {}

        def _count_tokens(self, event: Any, agent_key: Optional[str]) -> None:
            if not isinstance(event, RawResponsesStreamEvent):
                return
            data = getattr(event, "data", None)
            if getattr(data, "type", None) != "response.completed":
                return
            usage = getattr(getattr(data, "response", None), "usage", None)
            if usage is None:
                return
            total = self.tokens.setdefault(agent_key or "?", {"input": 0, "output": 0})
            total["input"] += int(getattr(usage, "input_tokens", 0) or 0)
            total["output"] += int(getattr(usage, "output_tokens", 0) or 0)

        def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
            self._count_tokens(event, agent_key)
            if isinstance(event, RunItemStreamEvent) and event.name in {"tool_called", "tool_output"}:
                info = tool_event_info(event.item)
                output = info.get("output")
                self.events.append({
                    "t": round(time.monotonic() - self.started, 1),
                    "agent": agent_key,
                    "phase": "called" if event.name == "tool_called" else "output",
                    "tool": info.get("tool_name"),
                    "call_id": info.get("call_id"),
                    "arguments": _clip(info.get("arguments")),
                    "output": _clip(output),
                })
            return super().handle_event(event, agent_key=agent_key)

    return Recorder(), log


def _clip(value: Any) -> Any:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + f"...[{len(text)} chars]"


async def run(workshop: Path, message: str, out: Path) -> None:
    from core.agent_factory import AgentFactory
    from core.config import Config

    config = Config(str(workshop / "examples/system-admin/config.yaml"), str(workshop))
    agent_key = config.get_default_agent()
    names = {key: agent.name for key, agent in config.config.agents.items()}
    recorder, log = make_recorder(out.with_suffix(".log"))
    factory = AgentFactory(
        config=config,
        working_directory=config.get_working_directory(),
        stream_observer=recorder,
    )
    started = time.monotonic()
    result: dict[str, Any] = {"agent": agent_key, "message": message}
    try:
        answer = await factory.run_agent(
            agent_key, message, stream=True, stream_observer=recorder, user_id="eval"
        )
        result["final_text"] = str(answer)
    except BaseException as error:  # noqa: BLE001 - the harness must always get a record
        result["error"] = f"{type(error).__name__}: {error}"
        result["final_text"] = ""
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 1)
        by_name = {name: key for key, name in names.items()}
        for event in recorder.events:
            event["agent"] = by_name.get(event["agent"], event["agent"])
        result["trace"] = recorder.events
        by_agent = {by_name.get(k, k): v for k, v in recorder.tokens.items()}
        result["tokens_by_agent"] = by_agent
        result["tokens"] = sum(v["input"] + v["output"] for v in by_agent.values()) or None
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        log.close()
        try:
            await factory.cleanup()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workshop", type=Path, required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.workshop.resolve(), args.message, args.out.resolve()))


if __name__ == "__main__":
    main()
