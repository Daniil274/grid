"""Cheap in-process check that a case is valid before paying for Docker runs.

Routes every scenario of a case with the real router on the sabotaged tree and
on the reference (reverted) tree. A valid fix case has targets that fail when
sabotaged and pass when reverted, and guards that pass on both. Only the
routing part of a scenario is checked here; the controller validates the rest.

    python -m evals.self_improvement.probe [case ...] [--reps 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from .cases import Case, apply_edits, export_head, load_cases

ROOT = Path(__file__).resolve().parents[2]


async def route_all(root: Path, scenarios: list[dict], reps: int) -> dict[str, float]:
    from core.config import Config
    from core.routing import AutoRouter

    rates: dict[str, float] = {}
    for scenario in scenarios:
        hits = 0
        for _ in range(reps):
            # A fresh router per message: no stickiness to the previous route.
            router = AutoRouter.from_config(
                Config(str(root / "routing.yaml")), working_directory=str(root)
            )
            route = await router.route(scenario["message"])
            ok = (not scenario.get("system") or route.system == scenario["system"]) and (
                not scenario.get("agent") or route.agent == scenario["agent"]
            )
            hits += ok
        rates[scenario["name"]] = hits / reps
    return rates


async def probe(case: Case, base: Path, reps: int) -> dict:
    scenarios = [*case.dev, *case.holdout]
    apply_edits(base, case.sabotage)
    try:
        sabotaged = await route_all(base, scenarios, reps)
    finally:
        apply_edits(base, case.sabotage, reverse=True)
    reference = await route_all(base, scenarios, reps)
    problems = []
    for s in case.targets:
        if sabotaged[s["name"]] > 0:
            problems.append(f"target {s['name']} passes when sabotaged ({sabotaged[s['name']]:.2f})")
        if reference[s["name"]] < 1:
            problems.append(f"target {s['name']} fails on reference ({reference[s['name']]:.2f})")
    for s in case.guards:
        if min(sabotaged[s["name"]], reference[s["name"]]) < 1:
            problems.append(
                f"guard {s['name']} flaky or broken ({sabotaged[s['name']]:.2f}/{reference[s['name']]:.2f})"
            )
    return {"case": case.name, "valid": not problems, "problems": problems,
            "sabotaged": sabotaged, "reference": reference}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="*")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    with tempfile.TemporaryDirectory(prefix="grid-probe-", ignore_cleanup_errors=True) as temp:
        base = Path(temp)
        export_head(ROOT, base, args.ref)
        os.chdir(base)
        try:
            for case in load_cases(args.cases or None):
                if case.kind != "fix":
                    continue
                report = await probe(case, base, args.reps)
                print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            os.chdir(ROOT)


if __name__ == "__main__":
    asyncio.run(main())
