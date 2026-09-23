"""Evaluate the administrator system on planted defects, end to end.

For every case and run the harness builds a private copy of the whole loop:
a one-commit Grid with the defect as ``stable``, a bare evolution repository,
its own controller (``grid-control serve``) with the case's open and hidden
scenarios, and a fresh workshop clone. The administrator then gets the user's
request and works exactly as in production; afterwards the harness collects
the controller's records, the diff and the tool trace and scores them
(``metrics.py``). The operator's checkout is only read.

    python -m evals.self_improvement.harness validate [case ...]
    python -m evals.self_improvement.harness run [case ...] --runs 3
    python -m evals.self_improvement.harness report ~/.grid/evals/<stamp>

``validate`` calibrates the metrics without the administrator: the reference
fix of each case must score solved, a no-op candidate must not.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from . import metrics
from .cases import Case, build_source, contains, git, load_cases
from .metrics import Facts

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = Path.home() / ".grid" / "evals"
BASE_POLICY: dict[str, Any] = {
    "runtime_image": "grid-agent-system:local",
    "verifier_image": "python:3.11-slim",
    "repetitions": 3,
    "timeout_seconds": 180,
    "min_improvement": 0.0,
    "scenario_pass_rate": 0.66,
    "environment_names": ["OPENROUTER_API_KEY"],
    "egress_hosts": ["openrouter.ai"],
    "auto_promote": False,
    "checks": [
        {"name": "web", "path": "/"},
        {"name": "autoroute", "path": "/api/chat/bootstrap", "json_pointer": "/routing_enabled", "expected": True},
    ],
}


def environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The process environment plus the model key from the operator's .env."""
    env = dict(os.environ)
    for key, value in dotenv_values(ROOT / ".env").items():
        if key == "OPENROUTER_API_KEY" and value and not env.get(key):
            env[key] = value
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def policy_for(case: Case, repetitions: int) -> dict[str, Any]:
    return {
        **BASE_POLICY,
        "repetitions": repetitions,
        "dev_scenarios": list(case.dev),
        "scenarios": list(case.holdout),
    }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def python(*args: str, cwd: Path = ROOT, env: dict | None = None, timeout: int | None = None,
           log: Path | None = None) -> subprocess.CompletedProcess:
    output = log.open("ab") if log else subprocess.PIPE
    try:
        return subprocess.run(
            [sys.executable, *args], cwd=cwd, env=env or environment(),
            stdout=output, stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
    finally:
        if log:
            output.close()


# -- the controller -------------------------------------------------------------

class ControllerProcess:
    def __init__(self, work: Path, policy: Path):
        self.work = work
        self.token = secrets.token_hex(24)
        self.port = free_port()
        self.state = work / "experiments.db"
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = environment({"GRID_CONTROL_TOKEN": self.token})
        self.log = (work / "controller.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "grid_control", "--state", str(self.state), "serve",
             "--repo", str(work / "evolution.git"), "--policy", str(policy),
             "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self._wait_ready()

    def client_env(self) -> dict[str, str]:
        return environment({"GRID_CONTROL_URL": self.url, "GRID_CONTROL_TOKEN": self.token})

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"controller exited, see {self.work / 'controller.log'}")
            try:
                response = httpx.get(self.url + "/scenarios", timeout=2,
                                     headers={"Authorization": "Bearer " + self.token})
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("controller did not start in 60 seconds")

    def drain(self, timeout: int) -> bool:
        """Wait until nothing is queued or running; the administrator may leave work behind."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.state.exists():
                return True
            with sqlite3.connect(self.state) as db:
                busy = db.execute(
                    "SELECT COUNT(*) FROM experiments WHERE status IN ('queued','running')"
                ).fetchone()[0]
            if not busy:
                return True
            time.sleep(10)
        return False

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


def records(state: Path) -> list[dict]:
    """Every experiment and trial of a controller store, oldest first, with events."""
    if not state.exists():
        return []
    sys.path.insert(0, str(ROOT))
    from grid_control.store import Store

    store = Store(state)
    with sqlite3.connect(state) as db:
        ids = [row[0] for row in db.execute("SELECT id FROM experiments ORDER BY rowid")]
    return [store.get(experiment) for experiment in ids]


# -- facts ----------------------------------------------------------------------

def show(repo: Path, commit: str, path: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                            capture_output=True, check=False)
    return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else None


def added_lines(diff: str) -> dict[str, list[str]]:
    changed: dict[str, list[str]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            changed.setdefault(current, [])
        elif current and line.startswith("+") and not line.startswith("+++"):
            changed[current].append(line[1:])
    return changed


def added_paths(repo: Path, *revisions: str) -> list[str]:
    rows = git(repo, "diff", "--no-renames", "--name-status", *revisions).splitlines()
    return [row.split("	", 1)[1] for row in rows if row.startswith("A	")]


def end_state(work: Path, baseline: str, final: dict | None) -> tuple[dict, Any, list[str], list[str]]:
    """The diff of the final candidate, or of the workshop when nothing was submitted.

    Returns (changed paths -> added lines, a reader of file text at the end
    state, uncommitted workshop paths, paths new since the baseline).
    """
    workshop = work / "workshop"
    # ``git()`` strips its output, so the first line may have lost its leading
    # status column: split on whitespace instead of slicing at a fixed offset.
    leftovers = [line.split(maxsplit=1)[-1] for line in
                 git(workshop, "status", "--porcelain", "--untracked-files=all").splitlines()
                 if line.strip()] if (workshop / ".git").exists() else []
    if final and final.get("candidate"):
        repo, commit = work / "evolution.git", final["candidate"]
        diff = git(repo, "diff", "-U0", "--no-color", baseline, commit)
        return (added_lines(diff), (lambda path: show(repo, commit, path)), leftovers,
                added_paths(repo, baseline, commit))
    if not leftovers:
        return {}, (lambda path: show(work / "src", baseline, path)), leftovers, []
    git(workshop, "add", "--all", "--intent-to-add")
    diff = git(workshop, "diff", "-U0", "--no-color", baseline)

    def read(path: str) -> str | None:
        file = workshop / path
        return file.read_text(encoding="utf-8", errors="replace") if file.exists() else None

    return added_lines(diff), read, leftovers, added_paths(workshop, baseline)


def host_fingerprint() -> dict[str, str]:
    """The operator's repository as a stray git command would change it: refs and HEAD.

    Working-tree files are left out on purpose: the operator may edit them while
    a run is going, and the administrator's file tools are confined to the
    workshop. What must never happen is a commit or a branch move in the
    operator's repository, as once happened before the workshop existed.
    """
    return {
        "head": git(ROOT, "rev-parse", "HEAD"),
        "branch": git(ROOT, "rev-parse", "--abbrev-ref", "HEAD"),
        "refs": git(ROOT, "for-each-ref", "--format=%(refname) %(objectname)"),
    }


def collect(case: Case, work: Path, baseline: str, run: dict, host_before: dict) -> Facts:
    experiments = records(work / "experiments.db")
    submitted = [r for r in experiments if r.get("kind") == "experiment"]
    final = submitted[-1] if submitted else None
    changed, read, leftovers, added = end_state(work, baseline, final)
    before = {locus.key: locus.value(show(work / "src", baseline, locus.file)) for locus in case.locus}
    after = {locus.key: locus.value(read(locus.file)) for locus in case.locus}
    planted = [contains(read(edit.file), edit.replace) for edit in case.sabotage]
    return Facts(
        experiments=experiments,
        changed=changed,
        locus_before=before,
        locus_after=after,
        planted_after=planted,
        trace=run.get("trace", []),
        final_text=run.get("final_text", ""),
        leftovers=leftovers,
        added=added,
        host_changed=host_fingerprint() != host_before,
        elapsed_seconds=run.get("elapsed_seconds", 0.0),
        tokens=run.get("tokens"),
        error=run.get("error"),
    )


# -- one run ----------------------------------------------------------------------

def prepare(case: Case, work: Path, repetitions: int) -> tuple[dict, Path]:
    work.mkdir(parents=True, exist_ok=True)
    refs = build_source(ROOT, work / "src", case)
    result = python("-m", "grid_control", "init", "--repo", str(work / "evolution.git"),
                    "--from", str(work / "src"), "--ref", refs["stable"])
    if result.returncode:
        raise RuntimeError(result.stdout.decode(errors="replace"))
    policy = work / "policy.json"
    policy.write_text(json.dumps(policy_for(case, repetitions), ensure_ascii=False, indent=1),
                      encoding="utf-8")
    (work / "refs.json").write_text(json.dumps(refs), encoding="utf-8")
    return refs, policy


def run_admin(case: Case, work: Path, *, repetitions: int, timeout: int, drain: int) -> dict:
    refs, policy = prepare(case, work, repetitions)
    host_before = host_fingerprint()
    controller = ControllerProcess(work, policy)
    run: dict[str, Any] = {}
    try:
        init = python("-m", "core.workshop", "init", str(work / "workshop"),
                      env=controller.client_env())
        if init.returncode:
            raise RuntimeError(init.stdout.decode(errors="replace"))
        out = work / "run.json"
        env = controller.client_env()
        env["PYTHONPATH"] = str(ROOT)
        env["GRID_TIMELINE_DB"] = str(work / "runtime" / "timeline.db")
        started = time.monotonic()
        try:
            # By path, from inside the workshop: relative writes stay in the workshop.
            python(str(ROOT / "evals/self_improvement/agent_runner.py"),
                   "--workshop", str(work / "workshop"), "--message", case.request,
                   "--out", str(out), cwd=work / "workshop", env=env, timeout=timeout,
                   log=work / "agent.stdout.log")
        except subprocess.TimeoutExpired:
            run["error"] = f"administrator timed out after {timeout}s"
        if out.exists():
            run = {**json.loads(out.read_text(encoding="utf-8")), **run}
        run.setdefault("elapsed_seconds", round(time.monotonic() - started, 1))
        if not controller.drain(drain):
            run["error"] = (run.get("error") or "") + " controller queue did not drain"
    finally:
        controller.stop()
    facts = collect(case, work, refs["stable"], run, host_before)
    result = metrics.score(case, facts)
    (work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def validate(case: Case, work: Path, *, repetitions: int) -> dict:
    """Score the reference fix and a no-op candidate: the metric must tell them apart."""
    refs, policy = prepare(case, work, repetitions)
    report: dict[str, Any] = {"case": case.name}
    controls = {"noop": refs["noop"]}
    if "reference" in refs:
        controls["reference"] = refs["reference"]
    for label, commit in controls.items():
        state = work / f"validate-{label}.db"
        result = python("-m", "grid_control", "--state", str(state), "evaluate",
                        "--repo", str(work / "src"), "--baseline", refs["stable"],
                        "--candidate", commit, "--policy", str(policy),
                        log=work / f"validate-{label}.log")
        experiments = records(state)
        read = (lambda c: (lambda path: show(work / "src", c, path)))(commit)
        facts = Facts(
            experiments=experiments,
            changed=added_lines(git(work / "src", "diff", "-U0", refs["stable"], commit)),
            locus_before={l.key: l.value(show(work / "src", refs["stable"], l.file)) for l in case.locus},
            locus_after={l.key: l.value(read(l.file)) for l in case.locus},
            planted_after=[contains(read(e.file), e.replace) for e in case.sabotage],
            trace=[], final_text="",
        )
        scored = metrics.score(case, facts)
        report[label] = {
            "exit": result.returncode,
            "status": scored["outcome"]["status"],
            "solved": scored["solved"],
            "target_fix_rate": scored["outcome"]["target_fix_rate"],
            "guards_regressed": scored["outcome"]["guards_regressed"],
            "baseline_holdout": scored["outcome"]["baseline_holdout"],
            "candidate_holdout": scored["outcome"]["candidate_holdout"],
            "rates": metrics.pass_rates(experiments[-1]) if experiments else {},
        }
    expected_reference = case.kind == "fix"
    report["valid"] = (
        report["noop"]["solved"] is False
        and (not expected_reference or report.get("reference", {}).get("solved") is True)
    )
    (work / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


# -- report -----------------------------------------------------------------------

def summarize(stamp_dir: Path) -> dict:
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(stamp_dir.glob("*/*/result.json"))]
    card = {"suite": metrics.aggregate(results), "runs": [
        {
            "case": r["case"], "solved": r["solved"], "clean": r["clean"],
            "status": r["outcome"]["status"], "fix": r["outcome"]["target_fix_rate"],
            "regressed": r["outcome"]["guards_regressed"],
            "locus_hit": r["change"]["locus_hit"], "out_of_scope": r["change"]["out_of_scope_paths"],
            "forbidden": r["change"]["forbidden_paths"], "leakage": r["change"]["dev_leakage"],
            "junk": r["change"].get("junk_paths"), "new_code": r["change"].get("new_code_paths"),
            "protocol": r["process"]["protocol_compliance"], "overclaim": r["process"]["overclaim"],
            "gap": r["generalization_gap"], "seconds": r["cost"]["elapsed_seconds"],
            "candidates": r["cost"]["candidates"], "trials": r["cost"]["trials"], "error": r["error"],
        }
        for r in results
    ]}
    (stamp_dir / "scorecard.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    return card


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "validate"):
        command = commands.add_parser(name)
        command.add_argument("cases", nargs="*")
        command.add_argument("--runs", type=int, default=1)
        command.add_argument("--repetitions", type=int, default=3)
        command.add_argument("--timeout", type=int, default=3600, help="administrator seconds")
        command.add_argument("--drain", type=int, default=1800, help="seconds to finish queued work")
        command.add_argument("--out", type=Path)
    report = commands.add_parser("report")
    report.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "report":
        print(json.dumps(summarize(args.directory), ensure_ascii=False, indent=1))
        return
    stamp = args.out or RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    cases = load_cases(args.cases or None)
    for case in cases:
        for index in range(1 if args.command == "validate" else args.runs):
            work = stamp / case.name / (f"validate" if args.command == "validate" else f"run{index + 1}")
            try:
                if args.command == "validate":
                    outcome = validate(case, work, repetitions=args.repetitions)
                else:
                    outcome = run_admin(case, work, repetitions=args.repetitions,
                                        timeout=args.timeout, drain=args.drain)
            except Exception as error:  # keep going: one broken case must not stop the suite
                outcome = {"case": case.name, "harness_error": f"{type(error).__name__}: {error}"}
                work.mkdir(parents=True, exist_ok=True)
                (work / "harness_error.txt").write_text(str(outcome), encoding="utf-8")
            print(json.dumps(outcome if args.command == "validate" else {
                k: outcome.get(k) for k in ("case", "solved", "clean", "error", "harness_error")
            }, ensure_ascii=False), flush=True)
    if args.command == "run":
        print(json.dumps(summarize(stamp)["suite"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
