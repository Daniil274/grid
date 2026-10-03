"""Execute development acceptance scenarios in fresh, disposable workspaces.

Models and trusted Grid function tools run in the server as in web chat;
commands and uploaded tool packages run in a resource-limited container.
The candidate gets fixtures and the request, never assertion values or reports.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import yaml

from core.system_quality import (acceptance_revision, acceptance_suite, check_assertion, design_review, read_contract,
                                 revision, save_evidence, system_files)

# Synchronous agent-tool delegation is supported. Cross-system/background
# orchestration is refused until it can share the evaluation's lifetime.
FORBIDDEN_PREFIXES = ("builder_", "grid_", "pipeline_", "control_")
_running: set[str] = set()


class EvaluationObserver:
    def __init__(self):
        self.tokens = 0
        self.tool_calls = 0
        self.responses = 0
        self.unmetered = False
        self.by_agent: dict[str, dict] = {}
        self._seen: set[tuple] = set()

    def handle_event(self, event, *, agent_key=None):
        from core.run_stream import is_output_delta
        agent = self.by_agent.setdefault(agent_key or "unknown", {"tokens": 0, "tool_calls": 0})
        data = getattr(event, "data", None)
        if getattr(event, "name", None) == "tool_called":
            self.tool_calls += 1
            agent["tool_calls"] += 1
        if getattr(data, "type", None) == "response.completed":
            response = getattr(data, "response", None)
            identifier = getattr(response, "id", None)
            key = (agent_key, identifier)
            if identifier and key in self._seen:
                return None
            if identifier:
                self._seen.add(key)
            self.responses += 1
            usage = getattr(response, "usage", None)
            if usage is None:
                self.unmetered = True
            else:
                count = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "output_tokens", 0) or 0)
                self.tokens += count
                agent["tokens"] += count
        if data is not None and is_output_delta(getattr(data, "type", None)):
            return getattr(data, "delta", None)
        return None


def validate_candidate(document: dict, root: Path) -> None:
    settings = document.get("settings") or {}
    project_tools = settings.get("project_tools") or {}
    if project_tools.get("enabled") or project_tools.get("base_tools"):
        raise ValueError("Evaluation requires shared function tools or tool_package; project_tools imports are not supported")
    if settings.get("config_directory", ".") != ".":
        raise ValueError("Evaluation requires config_directory: .")
    if settings.get("allow_path_override") is False:
        raise ValueError("Evaluation requires allow_path_override: true")
    policy = settings.get("action_policy") or {}
    if isinstance(policy, str) or any(policy.get(key) for key in ("policy_file", "system", "system_file")):
        raise ValueError("Evaluation inherits server policy; external policy files are not supported")
    from tools.function_tools import tool_isolation
    from utils.tool_isolation import CONTAINER, WORKSPACE
    for name, tool in (document.get("tools") or {}).items():
        if name.startswith(FORBIDDEN_PREFIXES) or name == "orchestrate":
            raise ValueError(f"{name}: evaluation supports local agent delegation, not cross-system/background orchestration")
        if tool.get("type") == "function" and tool_isolation(name) not in {CONTAINER, WORKSPACE}:
            raise ValueError(f"{name}: tool is not confined to the evaluation workspace/container")
        if tool.get("type") == "mcp":
            package = tool.get("tool_package")
            if not package or tool.get("server_command") or tool.get("env_vars"):
                raise ValueError("Evaluated MCP tools must use local tool_package without commands or custom environment")
            if root.resolve() not in (root / package).resolve().parents:
                raise ValueError("Tool package escapes the system snapshot")
    for agent in (document.get("agents") or {}).values():
        if agent.get("auto_run_tools"):
            raise ValueError("Evaluation requires tools to be called within the measured agent run; auto_run_tools are not supported")
        for skill in agent.get("system_skills") or []:
            if "/" in skill or "\\" in skill or skill in {".", ".."}:
                raise ValueError("Skills must be local names in skills/")


async def run_scenario(access, snapshot: Path, scenario, *, token_limit: int, call_limit: int) -> dict:
    from core.agent_factory import AgentFactory
    from core.config import Config
    from core.context import ContextManager
    from core.managers.project_tools_loader import get_project_loader, set_project_loader
    from core.tool_packages import _run, ensure_in_container, package_dir

    started = time.monotonic()
    observer = EvaluationObserver()
    result = {"name": scenario.name, "category": scenario.category, "passed": False, "assertions": []}
    name = "grid-eval-" + uuid.uuid4().hex[:16]
    factory = None
    # Parent directory is never mounted: configs, expected outputs, sessions and
    # evaluation evidence cannot be changed by candidate shell commands.
    with tempfile.TemporaryDirectory(prefix="grid-eval-") as temp:
        folder = Path(temp)
        workspace = folder / "workspace"
        workspace.mkdir(mode=0o777)
        workspace.chmod(0o777)
        for path, text in scenario.fixtures.items():
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            target.chmod(0o666)
            for parent in target.parents:
                if parent == workspace:
                    break
                parent.chmod(0o777)
        task = None
        try:
            async with asyncio.timeout(scenario.timeout_seconds):
                code, _, err = await _run([
                    "docker", "run", "-d", "--rm", "--name", name,
                    "--memory", "512m", "--pids-limit", "128", "--cpus", "1",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--init",
                    "--user", "agent", "--mount", f"type=bind,source={workspace},target=/workspace",
                    "--workdir", "/workspace", "--entrypoint", "sleep", access.image,
                    str(scenario.timeout_seconds + 120),
                ], timeout=60)
                if code:
                    raise ValueError("Cannot start evaluation sandbox: " + err[-1000:])
                manifest = yaml.safe_load((snapshot / "system.yaml").read_text()) or {}
                requires = manifest.get("requires") or []
                if requires:
                    code, _, err = await _run(["docker", "exec", name, "sh", "-c",
                                              'for program do command -v "$program" >/dev/null || exit 1; done',
                                              "check", *requires])
                    if code:
                        raise ValueError("Sandbox lacks required programs: " + ", ".join(requires))
                previous = get_project_loader()
                try:
                    config = Config(str(snapshot / "config.yaml"), str(workspace))
                finally:
                    set_project_loader(previous)
                if not access.shared:
                    config.config.providers = {k: access.base_config.config.providers[k].model_copy(deep=True)
                                               for k in config.config.providers}
                # Install pinned packages before disconnecting network. No
                # candidate agent is running yet.
                for tool in config.config.tools.values():
                    if tool.tool_package:
                        await ensure_in_container(name, package_dir(snapshot, tool.tool_package))
                code, _, err = await _run(["docker", "network", "disconnect", "-f", "bridge", name])
                if code:
                    raise ValueError("Cannot isolate sandbox network: " + err[-1000:])
                config.config.settings.logs_directory = str(folder / "logs")
                # Evaluation stays bounded even when the system asks for no
                # limit (max_turns == 0), so the cap must win over "no limit".
                budget = config.config.settings.max_turns
                config.config.settings.max_turns = min(budget, 50) if budget > 0 else 50
                factory = AgentFactory(
                    config=config, working_directory=str(workspace), container_id=name,
                    confine_tools=True, policy_config=access.catalog or access.base_config,
                    context_manager=ContextManager(max_history=100, persist_path=str(folder / "context.json")),
                    session_db_path=str(folder / "sessions.db"), logs_directory=str(folder / "logs"),
                    stream_observer=observer, model_access=access.model_access,
                )
                task = asyncio.create_task(factory.run_agent(config.get_default_agent(), scenario.message,
                                                            stream=True, stream_observer=observer,
                                                            user_id="evaluation"))
                while not task.done():
                    await asyncio.wait({task}, timeout=0.1)
                    if observer.tokens > token_limit or observer.tool_calls > call_limit:
                        task.cancel()
                        raise ValueError("Evaluation token/tool-call budget exceeded")
                answer = str(await task)
                # Grid appends a transport context marker, not part of user output.
                answer = answer.rsplit("\nContext ID: ", 1)[0].rstrip()
                await factory.cleanup()
                factory = None
                code, _, err = await _run(["docker", "stop", "--time", "1", name], timeout=15)
                if code:
                    raise ValueError("Cannot stop sandbox before inspecting artifacts: " + err[-1000:])
                result["assertions"] = [check_assertion(a, answer, workspace) for a in scenario.assertions]
                result["answer_preview"] = answer[:2000]
                metered = observer.responses > 0 and not observer.unmetered
                result["passed"] = (all(a["passed"] for a in result["assertions"]) and metered
                                    and observer.tokens <= token_limit and observer.tool_calls <= call_limit)
                if not metered:
                    result["error"] = "Provider did not report complete token usage; budget compliance is unknown"
        except TimeoutError:
            result["error"] = "Scenario deadline exceeded"
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            if task and not task.done():
                task.cancel()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            if factory:
                try:
                    await asyncio.wait_for(factory.cleanup(), timeout=10)
                except Exception:
                    pass
            try:
                await _run(["docker", "rm", "-f", name], timeout=15)
            except Exception:
                pass
    result.update(seconds=round(time.monotonic() - started, 3), tokens=observer.tokens,
                  tool_calls=observer.tool_calls, tokens_by_agent=observer.by_agent)
    return result


async def evaluate(access, key: str) -> dict:
    root = access.store.directory(key)
    lock_key = str(root.resolve())
    if lock_key in _running:
        raise ValueError("An evaluation of this system is already running")
    _running.add(lock_key)
    started = time.monotonic()
    report = None
    try:
        # Validate against the caller's store before reading or running anything.
        access.store.get(key)
        access.validate(access.store.config_text(key), key)
        before = revision(root)
        contract = read_contract(root)
        save_evidence(root, "contract-required", {"required": True})
        independent = acceptance_suite(root)
        acceptance_hash = acceptance_revision(root)
        cases = [("development", case, repeat + 1) for repeat in range(contract.repetitions) for case in contract.scenarios]
        if independent:
            cases.extend(("acceptance", case, repeat + 1) for repeat in range(independent.repetitions) for case in independent.scenarios)
        suite_digest = hashlib.sha256(json.dumps({
            "cases": [(kind, case.model_dump(), repeat) for kind, case, repeat in cases],
            "seconds": contract.max_total_seconds, "tokens": contract.max_total_tokens,
            "calls": contract.max_total_tool_calls,
        }, sort_keys=True).encode()).hexdigest()
        review = design_review(root, contract)
        if review["errors"]:
            raise ValueError("; ".join(review["errors"]))
        document = yaml.safe_load((root / "config.yaml").read_text())
        validate_candidate(document, root)
        from core.system_store import inspect_system
        health = inspect_system(lambda: access.load(key), confined=True)
        if not health["loaded"] or health["config"]:
            raise ValueError("Fix configuration before evaluation: " + health["error"] + "; ".join(health["config"]))
        report = {"id": uuid.uuid4().hex, "revision": before, "at": time.time(), "passed": False,
                  "scope": "development and owner acceptance scenarios", "results": [], "tokens": 0, "tool_calls": 0,
                  "runtime": {"image": access.image, "models": {k: v.get("name") for k, v in document.get("models", {}).items()}},
                  "expected_runs": len(cases), "suite_digest": suite_digest, "acceptance_revision": acceptance_hash,
                  "budget_note": "Limits use observed agent response usage; in-flight requests may overshoot. Policy/compaction calls and provider retries can add unreported cost. Price is not inferred from tokens."}
        with tempfile.TemporaryDirectory(prefix="grid-eval-system-") as temp:
            snapshot = Path(temp)
            for source in system_files(root):
                relative = source.relative_to(root)
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            if revision(snapshot) != before:
                raise ValueError("System changed while taking its evaluation snapshot; retry")
            # The factory only needs runtime files. Scenarios and their answers
            # are kept in host memory and outside the agent workspace.
            (snapshot / "quality.yaml").unlink()
            try:
                async with asyncio.timeout(contract.max_total_seconds):
                    for kind, scenario, repetition in cases:
                        remaining_tokens = contract.max_total_tokens - report["tokens"]
                        remaining_calls = contract.max_total_tool_calls - report["tool_calls"]
                        if remaining_tokens <= 0 or remaining_calls <= 0:
                            report["error"] = "Suite budget exhausted"
                            break
                        result = await run_scenario(access, snapshot, scenario,
                                                    token_limit=min(scenario.max_tokens, remaining_tokens),
                                                    call_limit=min(scenario.max_tool_calls, remaining_calls))
                        result["repetition"] = repetition
                        result["suite"] = kind
                        report["results"].append(result)
                        report["tokens"] += result["tokens"]
                        report["tool_calls"] += result["tool_calls"]
            except TimeoutError:
                report["error"] = "Suite deadline exceeded"
        report["passed"] = (len(report["results"]) == report["expected_runs"]
                            and all(r["passed"] for r in report["results"]) and not report.get("error"))
        if revision(root) != before or acceptance_revision(root) != acceptance_hash:
            report["passed"] = False
            report["error"] = "System changed during evaluation; results refer to the earlier revision"
        report["seconds"] = round(time.monotonic() - started, 3)
        successes = sum(r["passed"] for r in report["results"])
        report["pass_rate"] = successes / report["expected_runs"]
        report["tokens_per_success"] = report["tokens"] / successes if successes else None
        save_evidence(root, "evaluation", report)
        access.done(key, "evaluated", f"{successes}/{report['expected_runs']} passed; revision {before[:12]}")
        return report
    finally:
        _running.discard(lock_key)
