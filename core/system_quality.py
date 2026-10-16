"""Declarative acceptance contracts and revision-bound evidence for built systems.

Evidence lives beside, never inside, a system. No assertion executes uploaded
Python. Passing a builder-authored suite is development evidence, not an
independent certification or a claim about unseen tasks.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BYTES = 512 * 1024
MAX_SYSTEM_BYTES = 16 * 1024 * 1024


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or str(path) == ".":
        raise ValueError("Use a relative path inside the test workspace")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Assertion(StrictModel):
    kind: Literal["contains", "not_contains", "equals", "json_equals", "csv_equals", "exists", "absent"]
    path: str | None = None  # None means the final answer
    value: Any = None

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        return relative_path(value) if value is not None else value

    @model_validator(mode="after")
    def meaningful(self):
        if self.kind in {"exists", "absent", "csv_equals"} and not self.path:
            raise ValueError(f"{self.kind} requires a file path")
        if self.kind in {"contains", "not_contains", "equals"} and not isinstance(self.value, str):
            raise ValueError(f"{self.kind} requires a string value")
        if self.kind in {"contains", "not_contains"} and not self.value.strip():
            raise ValueError("An empty substring does not test behavior")
        if self.kind == "csv_equals" and (not isinstance(self.value, list) or not all(isinstance(row, dict) for row in self.value)):
            raise ValueError("csv_equals expects a list of row objects with string values")
        return self


class Scenario(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    category: Literal["normal", "ambiguous", "invalid_input", "failure", "repeat", "boundary"] = "normal"
    message: str = Field(min_length=1, max_length=12000)
    fixtures: dict[str, str] = Field(default_factory=dict, max_length=30)
    assertions: list[Assertion] = Field(min_length=1, max_length=30)
    timeout_seconds: int = Field(default=90, ge=1, le=300)
    max_tokens: int = Field(default=30000, ge=1, le=200000)
    max_tool_calls: int = Field(default=40, ge=0, le=200)

    @field_validator("fixtures")
    @classmethod
    def safe_fixtures(cls, value):
        normalized = [PurePosixPath(relative_path(path)) for path in value]
        if len(set(normalized)) != len(normalized) or any(a in b.parents for a in normalized for b in normalized if a != b):
            raise ValueError("Fixture paths overlap")
        if sum(len(text.encode()) for text in value.values()) > MAX_BYTES:
            raise ValueError("Scenario fixtures exceed 512 KiB")
        return value


class RoleContract(StrictModel):
    agent: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    inputs: str = Field(min_length=1)
    outputs: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    failure: str = Field(min_length=1)


class QualityContract(StrictModel):
    version: Literal[1] = 1
    goal: str = Field(min_length=1)
    result: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    architecture: Literal["single", "coordinator", "reviewer", "pipeline"] = "single"
    rationale: str = Field(min_length=1)
    roles: list[RoleContract] = Field(min_length=1, max_length=20)
    examples: list[str] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    failure_behavior: str = Field(min_length=1)
    scenarios: list[Scenario] = Field(min_length=1, max_length=20)
    repetitions: int = Field(default=1, ge=1, le=3)
    max_total_seconds: int = Field(default=600, ge=1, le=1800)
    max_total_tokens: int = Field(default=100000, ge=1, le=500000)
    max_total_tool_calls: int = Field(default=200, ge=1, le=1000)

    @model_validator(mode="after")
    def unique_names(self):
        if len({s.name for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("Scenario names must be unique")
        if len({r.agent for r in self.roles}) != len(self.roles):
            raise ValueError("Role agent keys must be unique")
        return self


def read_contract(root: Path) -> QualityContract:
    path = root / "quality.yaml"
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError("quality.yaml must be a regular file of at most 512 KiB")
    return QualityContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def system_files(root: Path) -> list[Path]:
    """Bounded snapshot input; refuse links and special files before reading."""
    files, total = [], 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in {"__pycache__", ".git", ".pytest_cache"} for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"System contains a symbolic link: {relative}")
        if path.is_dir():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"System contains a non-regular file: {relative}")
        total += info.st_size
        if info.st_size > MAX_BYTES or total > MAX_SYSTEM_BYTES or len(files) >= 1000:
            raise ValueError("System exceeds the evaluation snapshot size limit")
        files.append(path)
    return files


def revision(root: Path) -> str:
    digest = hashlib.sha256()
    for path in system_files(root):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if relative == "system.yaml":
            manifest = yaml.safe_load(data) or {}
            data = json.dumps({k: manifest.get(k) for k in ("key", "created_at", "origin", "description", "requires")}, sort_keys=True).encode()
        digest.update(relative.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


_FORBID = re.compile(
    r"(?:не\s+использу(?:ешь|й|йте|ют)|не\s+вызыва(?:й|йте|ешь)|запрещено\s+(?:использовать|вызывать)|"
    r"do\s+not\s+(?:use|call|invoke)|don'?t\s+(?:use|call)|must\s+not\s+(?:use|call)|never\s+(?:use|call))",
    re.IGNORECASE,
)
_TOOL_WORD = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\btools?\b|функци|инструмент|\bтул", re.IGNORECASE)
_ACCESS_HEADER = re.compile(r"^\s*#{1,6}\s*Твой\s+доступ\s+к", re.IGNORECASE | re.MULTILINE)
_SIGNATURE = re.compile(r"^\s*[-*]\s*`?[a-z][a-z0-9_]+\([a-z0-9_,\s=*:\[\]\"']*\)`?\s*[—–:-]", re.IGNORECASE | re.MULTILINE)


def prompt_problems(agent: str, prompt: str | None) -> list[str]:
    """Architectural defects of an agent's custom_prompt: it must not police or document its tools.

    A tool an agent should not use is not granted to it; the schema and docstring
    of every granted tool already reach the model, so the prompt keeps to the role.
    """
    if not prompt:
        return []
    problems = []
    forbids = _ACCESS_HEADER.search(prompt) or any(
        _FORBID.search(line) and _TOOL_WORD.search(line) for line in prompt.splitlines()
    )
    if forbids:
        problems.append(
            f"Agent '{agent}' custom_prompt forbids tool/function calls or lists tool access restrictions. "
            "Declare each tool function as 'type: function' with 'tool_package' and grant each agent "
            "only the tools it uses in 'tools:'."
        )
    if len(_SIGNATURE.findall(prompt)) >= 2:
        problems.append(
            f"Agent '{agent}' custom_prompt lists function signatures. Tool schemas and docstrings are "
            "provided to the model automatically; keep custom_prompt focused on role workflow and contracts."
        )
    return problems


def monolithic_package_message(name: str, package: str) -> str:
    return (
        f"{name}: use 'type: function' with 'tool_package: {package}' for individual @function_tool "
        "functions instead of a monolithic 'type: mcp' package in a multi-agent system"
    )


def design_review(root: Path, contract: QualityContract) -> dict:
    document = yaml.safe_load((root / "config.yaml").read_text()) or {}
    agents, tools = document.get("agents") or {}, document.get("tools") or {}
    errors, warnings = [], []
    declared = {role.agent for role in contract.roles}
    if declared != set(agents):
        errors.append("Contract roles must cover exactly the configured agents")
    if contract.architecture == "single" and len(agents) != 1:
        errors.append("The single architecture requires exactly one agent")
    if contract.architecture != "single" and len(agents) < 2:
        errors.append("A multi-agent architecture requires distinct configured agents")
    if not (root / "README.md").is_file():
        errors.append("Write README.md with examples, inputs, outputs and limitations")
    default = (document.get("settings") or {}).get("default_agent")
    reached, pending = set(), [default]
    while pending:
        key = pending.pop()
        if key in reached or key not in agents:
            continue
        reached.add(key)
        for name in agents[key].get("tools", []):
            target = tools.get(name, {}).get("target_agent")
            if target:
                pending.append(target)
    if set(agents) - reached:
        errors.append("Agents unreachable from the default entry: " + ", ".join(sorted(set(agents) - reached)))
    for key, agent in agents.items():
        errors.extend(prompt_problems(key, (agent or {}).get("custom_prompt")))
    for name, tool in tools.items():
        if len(agents) > 1 and tool.get("type") == "mcp" and tool.get("tool_package"):
            errors.append(monolithic_package_message(name, tool["tool_package"]))
        if tool.get("type") == "agent" and tool.get("context_strategy") != "minimal":
            warnings.append(f"{name}: consider minimal context and explicit artifact references")
    categories = {s.category for s in contract.scenarios}
    if "normal" not in categories or not categories.intersection({"invalid_input", "failure", "ambiguous"}):
        warnings.append("Include both normal tasks and ambiguous/invalid/failing inputs")
    if contract.repetitions == 1:
        warnings.append("One repetition does not measure run-to-run reliability")
    if all(a.path is None for s in contract.scenarios for a in s.assertions):
        warnings.append("Only final answers are checked; add artifact assertions for systems producing files")
    return {"errors": errors, "warnings": warnings}


def check_assertion(assertion: Assertion, answer: str, workspace: Path) -> dict:
    """Read bounded regular files after the sandbox stops writing."""
    try:
        text = answer
        if assertion.path:
            path = workspace / assertion.path
            if any(p.is_symlink() for p in [path, *path.parents] if p != workspace.parent):
                raise ValueError("Artifact is a symbolic link")
            if workspace.resolve() not in path.resolve().parents:
                raise ValueError("Artifact escapes the test workspace")
            exists = path.is_file()
            if assertion.kind in {"exists", "absent"}:
                ok = exists if assertion.kind == "exists" else not path.exists()
                return {"kind": assertion.kind, "path": assertion.path, "passed": ok}
            if not exists or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_BYTES:
                raise ValueError("Artifact missing, non-regular or larger than 512 KiB")
            text = path.read_text(encoding="utf-8")
        kind, expected = assertion.kind, assertion.value
        if kind == "contains":
            ok = expected in text
        elif kind == "not_contains":
            ok = expected not in text
        elif kind == "equals":
            ok = expected == text
        elif kind == "json_equals":
            ok = json.loads(text) == expected
        elif kind == "csv_equals":
            ok = list(csv.DictReader(io.StringIO(text))) == expected
        else:
            ok = False
        return {"kind": kind, "path": assertion.path, "passed": ok}
    except (OSError, ValueError) as exc:
        return {"kind": assertion.kind, "path": assertion.path, "passed": False, "error": str(exc)}


def evidence_dir(root: Path) -> Path:
    return root.parent / ".quality" / root.name


def save_evidence(root: Path, name: str, payload: dict) -> None:
    folder = evidence_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.json"
    partial = folder / f".{uuid.uuid4().hex}.tmp"
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, target)


def load_evidence(root: Path, name: str) -> dict | None:
    try:
        value = json.loads((evidence_dir(root) / f"{name}.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _tool_evidence_name(key: str) -> str:
    return "tool-" + hashlib.sha256(key.encode()).hexdigest()


def _package_key(root: Path, relative: str) -> str:
    """The evidence name of a package: its path inside the system, normalized."""
    try:
        return "package:" + (root / relative).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "package:" + relative


def _package_digest(root: Path, relative: str) -> str | None:
    """The content hash of a package that exists on disk, else None."""
    from core.tool_packages import PackageError, digest, package_dir
    try:
        folder = package_dir(root, relative)
        return digest(folder) if folder.is_dir() else None
    except (PackageError, OSError):
        return None


def _declared_packages(root: Path) -> dict[str, str]:
    """Tool key -> tool_package of the config."""
    try:
        document = yaml.safe_load((root / "config.yaml").read_text()) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return {k: v["tool_package"] for k, v in (document.get("tools") or {}).items()
            if isinstance(v, dict) and v.get("tool_package")}


def record_tool_test(root: Path, tool: str, result: dict, tested_revision: str) -> None:
    """Save a package's test result for *tool*: a tool key or a package path.

    Several ``type: function`` tools may share one package, so one test marks
    every tool that names it (and the package itself). The package's own
    content hash is stored beside the system's revision: editing the config or
    the README does not make a tested package untested; editing the package does.
    """
    declared = _declared_packages(root)
    relative = declared.get(tool)
    if relative is None:
        wanted = _package_key(root, tool)
        relative = next((r for r in declared.values() if _package_key(root, r) == wanted), None)
    keys = [tool]
    package_digest = None
    if relative is not None:
        keys += [k for k, r in declared.items() if _package_key(root, r) == _package_key(root, relative)]
        keys.append(_package_key(root, relative))
        package_digest = _package_digest(root, relative)
    payload = {"revision": tested_revision, "package_digest": package_digest, "at": time.time(), "tool": tool,
               "passed": bool(result.get("ok") and result.get("offline") and
                              (result.get("tests") or {}).get("passed", 0) > 0),
               "tests": (result.get("tests") or {}).get("passed", 0)}
    # One file per key avoids lost updates from independent package tests.
    for key in dict.fromkeys(keys):
        save_evidence(root, _tool_evidence_name(key), payload)


def _tools_tested(root: Path, current: str, declared: dict[str, str]) -> bool:
    for key, relative in declared.items():
        record = load_evidence(root, _tool_evidence_name(key)) or load_evidence(
            root, _tool_evidence_name(_package_key(root, relative)))
        if not record or not record.get("passed"):
            return False
        digest = _package_digest(root, relative)
        if record.get("revision") != current and not (digest and record.get("package_digest") == digest):
            return False
    return True


def quality_summary(root: Path) -> dict:
    result = {"state": "not_configured", "ready": False, "contract": None, "design": None,
              "evaluation": None, "tools_tested": False,
              "evidence_scope": "Development scenarios plus owner cases when configured; passing does not guarantee unseen tasks."}
    try:
        current = revision(root)
        result["revision"] = current
        document = yaml.safe_load((root / "config.yaml").read_text()) or {}
        packages = {k: v["tool_package"] for k, v in (document.get("tools") or {}).items() if v.get("tool_package")}
        result["tools_tested"] = _tools_tested(root, current, packages)
        if not (root / "quality.yaml").exists():
            return result
        contract = read_contract(root)
        result["contract"] = {k: v for k, v in contract.model_dump().items() if k != "scenarios"}
        result["design"] = design_review(root, contract)
        report = load_evidence(root, "evaluation")
        independent = acceptance_suite(root)
        result["acceptance_cases"] = len(independent.scenarios) if independent else 0
        result["evaluation"] = report
        result["state"] = "not_tested" if report is None else "stale" if (report.get("revision") != current or report.get("acceptance_revision") != acceptance_revision(root)) else "passed" if report.get("passed") else "failed"
        result["ready"] = result["state"] == "passed" and result["tools_tested"] and not result["design"]["errors"]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result.update(state="invalid", error=str(exc))
    return result


def require_quality(root: Path) -> None:
    """Opt-in gate: existing systems without a contract remain compatible."""
    if (root / "quality.yaml").exists() or load_evidence(root, "contract-required"):
        summary = quality_summary(root)
        if not summary["ready"]:
            raise ValueError("Quality checks are incomplete or stale. Run builder_evaluate and package tests for this revision.")


class AcceptanceSuite(StrictModel):
    """Owner-authored cases; stored outside builder-writable files."""
    scenarios: list[Scenario] = Field(min_length=1, max_length=20)
    repetitions: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def unique_names(self):
        if len({s.name for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("Acceptance scenario names must be unique")
        return self


def acceptance_suite(root: Path) -> AcceptanceSuite | None:
    path = evidence_dir(root) / "acceptance.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return AcceptanceSuite.model_validate(value)


def acceptance_revision(root: Path) -> str | None:
    suite = acceptance_suite(root)
    return hashlib.sha256(suite.model_dump_json().encode()).hexdigest() if suite else None


def comparison(first: dict, second: dict) -> dict:
    """Comparable evidence only; smaller token counts never conceal regressions."""
    a, b = first.get("evaluation"), second.get("evaluation")
    if not a or not b or first["state"] not in {"passed", "failed"} or second["state"] not in {"passed", "failed"}:
        raise ValueError("Both systems need current evaluation reports")
    if not a.get("suite_digest") or a["suite_digest"] != b.get("suite_digest"):
        raise ValueError("Use identical scenarios, repetitions and suite budgets before comparing")
    def passed(report):
        return {(r.get("suite", "development"), r["name"], r["repetition"]): r["passed"] for r in report["results"]}
    old, new = passed(a), passed(b)
    regressions = ["/".join(map(str, key)) for key, ok in old.items() if ok and not new.get(key)]
    return {"baseline": a["revision"], "candidate": b["revision"], "regressions": regressions,
            "candidate_passed": bool(second["ready"]),
            "tokens_delta": b["tokens"] - a["tokens"],
            "seconds_delta": round(b["seconds"] - a["seconds"], 3),
            "tool_calls_delta": b["tool_calls"] - a["tool_calls"],
            "note": "Compare multiple repeats; these measurements are development evidence, not a general quality guarantee."}
