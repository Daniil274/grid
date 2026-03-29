"""Stage 2 observer that mines Grid logs for improvement problems."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.config import Config
from core.improvement_registry import ImprovementRegistry
from schemas.improvement import ImprovementProblem


CORRECTION_RE = re.compile(
    r"\b(нет|не то|неверно|попробуй|ошибка|неправильно|исправь|wrong|incorrect|retry)\b",
    re.IGNORECASE,
)
FAILURE_RE = re.compile(
    r"(не могу|не получается|cannot|can't|unable|failed|ошибка|не удалось)",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(
    r"(timed out while waiting for response|tool execution timed out|agent execution timed out|max_turns exceeded|execution timed out|превышен лимит)",
    re.IGNORECASE,
)
TOOL_ERROR_RE = re.compile(r"(tool|instrument).*(error|failed)|ошибк.*инструмент", re.IGNORECASE)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value))
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _safe_json_load(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


@dataclass
class EvidenceRef:
    file: str
    line: Optional[int] = None
    context_id: Optional[str] = None
    detail: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"file": self.file}
        if self.line is not None:
            payload["line"] = self.line
        if self.context_id:
            payload["context_id"] = self.context_id
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class CandidateProblem:
    signal_type: str
    dedup_key: str
    title: str
    summary: str
    priority: str
    related_paths: List[str]
    evidence: List[EvidenceRef]
    requirements: List[str]
    success_criteria: List[str]
    contexts: List[str]
    metadata: Dict[str, Any]


@dataclass
class ObserverResult:
    scanned_contexts: int
    created: List[ImprovementProblem]
    candidates: List[CandidateProblem]


class LogObserver:
    """Parse saved Grid logs and create structured improvement problems."""

    def __init__(
        self,
        *,
        config: Config,
        registry: Optional[ImprovementRegistry] = None,
        logs_directory: Optional[str] = None,
        min_occurrences: int = 3,
    ) -> None:
        self.config = config
        self.registry = registry or ImprovementRegistry(config=config)
        configured_logs_dir = logs_directory or config.get("settings.logs_directory", "logs")
        self.logs_directory = Path(config.get_absolute_path(configured_logs_dir))
        self.min_occurrences = max(1, min_occurrences)

    def observe(self, since: Optional[datetime] = None) -> ObserverResult:
        candidates = self._collect_candidates(since=since)
        created: List[ImprovementProblem] = []
        for candidate in candidates:
            if len(set(candidate.contexts)) < self.min_occurrences:
                continue
            if self._is_duplicate(candidate):
                continue
            problem = self.registry.create_problem(
                title=candidate.title,
                summary=candidate.summary,
                requirements=candidate.requirements,
                success_criteria=candidate.success_criteria,
                priority=candidate.priority,
                source="observer",
                related_paths=candidate.related_paths,
                metadata={
                    **candidate.metadata,
                    "signal_type": candidate.signal_type,
                    "dedup_key": candidate.dedup_key,
                    "log_refs": [ref.as_dict() for ref in candidate.evidence],
                    "affected_path": candidate.metadata.get("affected_path", ""),
                    "context_count": len(set(candidate.contexts)),
                },
            )
            created.append(problem)
        scanned_contexts = self._count_contexts(since=since)
        return ObserverResult(scanned_contexts=scanned_contexts, created=created, candidates=candidates)

    def _count_contexts(self, since: Optional[datetime]) -> int:
        payload = _safe_json_load(self.logs_directory / "context.json") or {}
        contexts = payload.get("contexts", {})
        total = 0
        for context in contexts.values():
            updated_at = _parse_timestamp(context.get("updated_at"))
            if since and updated_at and updated_at < since:
                continue
            total += 1
        return total

    def _collect_candidates(self, since: Optional[datetime]) -> List[CandidateProblem]:
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for item in self._detect_user_corrections(since=since):
            grouped[item["dedup_key"]].append(item)
        for item in self._detect_task_failures(since=since):
            grouped[item["dedup_key"]].append(item)
        for item in self._detect_timeouts(since=since):
            grouped[item["dedup_key"]].append(item)

        candidates: List[CandidateProblem] = []
        for instances in grouped.values():
            template = instances[0]
            evidence = [entry["evidence"] for entry in instances]
            contexts = [entry["context_id"] for entry in instances if entry.get("context_id")]
            signal_type = template["signal_type"]
            count = len(set(contexts))
            signal_subtype = template.get("signal_subtype")
            title_suffix = {
                "user_correction": "Repeated user corrections",
                "task_failure": "Repeated incomplete task handling",
                "model_timeout": "Repeated timeout failures",
            }.get(signal_type, "Observed reliability issue")
            if signal_type == "task_failure" and signal_subtype:
                title_suffix = {
                    "provider_error": "Repeated provider failures",
                    "quota_error": "Repeated quota and budget failures",
                    "tool_schema_error": "Repeated tool schema failures",
                    "tool_missing": "Repeated missing tool failures",
                    "routing_error": "Repeated routing and model selection failures",
                    "context_overflow": "Repeated context overflow failures",
                    "connectivity_error": "Repeated connectivity failures",
                    "agent_creation_error": "Repeated agent creation failures",
                }.get(signal_subtype, title_suffix)
            summary = f"{template['summary']} Seen in {count} distinct context(s)."
            candidates.append(
                CandidateProblem(
                    signal_type=signal_type,
                    dedup_key=template["dedup_key"],
                    title=title_suffix,
                    summary=summary,
                    priority=template["priority"],
                    related_paths=template["related_paths"],
                    evidence=evidence,
                    requirements=template["requirements"],
                    success_criteria=template["success_criteria"],
                    contexts=contexts,
                    metadata={
                        "affected_path": template.get("affected_path", ""),
                        "agent_name": template.get("agent_name"),
                        "signal_subtype": signal_subtype,
                    },
                )
            )
        return sorted(candidates, key=lambda item: (len(set(item.contexts)), item.signal_type), reverse=True)

    def _detect_user_corrections(self, since: Optional[datetime]) -> Iterable[Dict[str, Any]]:
        payload = _safe_json_load(self.logs_directory / "context.json") or {}
        contexts = payload.get("contexts", {})
        for context_id, context in contexts.items():
            updated_at = _parse_timestamp(context.get("updated_at"))
            if since and updated_at and updated_at < since:
                continue
            history = context.get("conversation_history", []) or []
            agent_name = self._infer_agent_name(context)
            for index, message in enumerate(history):
                if message.get("role") != "user":
                    continue
                text = str(message.get("content") or "")
                if not CORRECTION_RE.search(text):
                    continue
                if index == 0:
                    continue
                prev_message = history[index - 1]
                if prev_message.get("role") != "assistant":
                    continue
                yield {
                    "signal_type": "user_correction",
                    "dedup_key": f"user_correction:{agent_name}",
                    "summary": f"Users repeatedly corrected responses from agent '{agent_name}'.",
                    "priority": "medium",
                    "related_paths": ["config.yaml"],
                    "affected_path": "config.yaml",
                    "requirements": [
                        "Adjust prompts or routing so the agent needs fewer manual corrections.",
                    ],
                    "success_criteria": [
                        "Correction-style follow-up messages drop on the affected benchmark or log slice.",
                    ],
                    "context_id": context_id,
                    "agent_name": agent_name,
                    "evidence": EvidenceRef(
                        file=str((self.logs_directory / "context.json").as_posix()),
                        context_id=context_id,
                        detail=text[:160],
                    ),
                }

    def _detect_task_failures(self, since: Optional[datetime]) -> Iterable[Dict[str, Any]]:
        payload = _safe_json_load(self.logs_directory / "context.json") or {}
        contexts = payload.get("contexts", {})
        for context_id, context in contexts.items():
            updated_at = _parse_timestamp(context.get("updated_at"))
            if since and updated_at and updated_at < since:
                continue
            executions = context.get("execution_history", []) or []
            agent_name = self._infer_agent_name(context)
            for execution in executions:
                end_time = _parse_timestamp(execution.get("end_time"))
                if since and end_time and end_time < since:
                    continue
                error = str(execution.get("error") or "")
                output = str(execution.get("output") or "")
                if not error and not FAILURE_RE.search(output):
                    continue
                signal_text = error or output
                if TIMEOUT_RE.search(signal_text):
                    continue
                classification = self._classify_task_failure(signal_text)
                yield {
                    "signal_type": "task_failure",
                    "signal_subtype": classification["signal_subtype"],
                    "dedup_key": f"task_failure:{classification['signal_subtype']}:{agent_name}",
                    "summary": classification["summary_template"].format(agent=agent_name),
                    "priority": classification["priority"],
                    "related_paths": classification["related_paths"],
                    "affected_path": classification["affected_path"],
                    "requirements": classification["requirements"],
                    "success_criteria": classification["success_criteria"],
                    "context_id": context_id,
                    "agent_name": agent_name,
                    "evidence": EvidenceRef(
                        file=str((self.logs_directory / "context.json").as_posix()),
                        context_id=context_id,
                        detail=signal_text[:160],
                    ),
                }

    @staticmethod
    def _classify_task_failure(signal_text: str) -> Dict[str, Any]:
        text = signal_text.lower()

        if "invalid json input for tool" in text:
            return {
                "signal_subtype": "tool_schema_error",
                "summary_template": "Agent '{agent}' repeatedly emits malformed tool arguments.",
                "priority": "high",
                "related_paths": ["config.yaml", "tools/"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Reduce malformed tool-call generation for the affected agent.",
                ],
                "success_criteria": [
                    "The same workflow stops failing with invalid tool JSON.",
                ],
            }
        if "tool " in text and " not found in agent" in text:
            return {
                "signal_subtype": "tool_missing",
                "summary_template": "Agent '{agent}' is routed without tools it expects to use.",
                "priority": "high",
                "related_paths": ["config.yaml"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Align tool allowlists and prompts so agents stop requesting missing tools.",
                ],
                "success_criteria": [
                    "Missing-tool failures disappear for the affected agent.",
                ],
            }
        if "credit balance is too low" in text or "quota" in text:
            return {
                "signal_subtype": "quota_error",
                "summary_template": "Agent '{agent}' frequently fails because provider budget or quota is exhausted.",
                "priority": "high",
                "related_paths": ["config.yaml"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Route affected tasks to providers or models that respect current budget limits.",
                ],
                "success_criteria": [
                    "Quota-related failures disappear from the same workflow slice.",
                ],
            }
        if "unknown model" in text or "model_not_supported" in text or "api key not found for provider" in text:
            return {
                "signal_subtype": "routing_error",
                "summary_template": "Agent '{agent}' frequently fails because model or provider routing is invalid.",
                "priority": "high",
                "related_paths": ["config.yaml"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Fix model/provider routing so the affected agent resolves to supported backends.",
                ],
                "success_criteria": [
                    "Routing-related provider and model selection errors disappear.",
                ],
            }
        if "maximum context length" in text or "tokens_limit_reached" in text or "request body too large" in text:
            return {
                "signal_subtype": "context_overflow",
                "summary_template": "Agent '{agent}' frequently exceeds model context or payload limits.",
                "priority": "high",
                "related_paths": ["config.yaml"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Reduce prompt, history, or tool payload size for the affected workflow.",
                ],
                "success_criteria": [
                    "Context and payload limit failures stop appearing for the same task class.",
                ],
            }
        if (
            "connection error" in text
            or "service is not available in your region" in text
            or "provider_unavailable" in text
        ):
            return {
                "signal_subtype": "provider_error",
                "summary_template": "Agent '{agent}' frequently fails because the upstream provider is unavailable.",
                "priority": "medium",
                "related_paths": ["config.yaml"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Add fallback routing or provider selection for temporary upstream failures.",
                ],
                "success_criteria": [
                    "Provider-availability failures drop for the affected workflow.",
                ],
            }
        if "failed to create agent" in text or ("agent '" in text and " not found" in text):
            return {
                "signal_subtype": "agent_creation_error",
                "summary_template": "Agent '{agent}' frequently fails during agent creation or startup.",
                "priority": "medium",
                "related_paths": ["config.yaml"],
                "affected_path": "config.yaml",
                "requirements": [
                    "Ensure referenced agents and their dependencies can be created successfully.",
                ],
                "success_criteria": [
                    "Agent creation failures stop recurring for the same route.",
                ],
            }
        return {
            "signal_subtype": "provider_error",
            "summary_template": "Agent '{agent}' regularly ends tasks with an explicit failure response.",
            "priority": "medium",
            "related_paths": ["config.yaml"],
            "affected_path": "config.yaml",
            "requirements": [
                "Improve task completion rate for the affected agent.",
            ],
            "success_criteria": [
                "The same class of task no longer ends with explicit failure language.",
            ],
        }

    def _detect_timeouts(self, since: Optional[datetime]) -> Iterable[Dict[str, Any]]:
        context_hits: Counter[str] = Counter()
        context_evidence: Dict[str, List[EvidenceRef]] = defaultdict(list)
        affected_agents: Dict[str, str] = {}
        log_path = self.logs_directory / "grid.log"
        if not log_path.exists():
            return []
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                for line_no, raw_line in enumerate(fh, start=1):
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = _parse_timestamp(payload.get("timestamp"))
                    if since and timestamp and timestamp < since:
                        continue
                    message = str(payload.get("message") or "")
                    message_lower = message.lower()
                    if "invalid json" in message_lower:
                        continue
                    if not TIMEOUT_RE.search(message):
                        continue
                    context_id = self._extract_context_id(message) or f"log-line-{line_no}"
                    agent_name = self._extract_agent_name(message, payload.get("logger"))
                    affected_agents[context_id] = agent_name
                    context_hits[context_id] += 1
                    context_evidence[context_id].append(
                        EvidenceRef(
                            file=str(log_path.as_posix()),
                            line=line_no,
                            context_id=context_id if context_id.startswith("ctx-") else None,
                            detail=message[:160],
                        )
                    )
        except OSError:
            return []

        results: List[Dict[str, Any]] = []
        for context_id, count in context_hits.items():
            agent_name = affected_agents.get(context_id, "unknown")
            results.append(
                {
                    "signal_type": "model_timeout",
                    "dedup_key": f"model_timeout:{agent_name}",
                    "summary": f"Agent '{agent_name}' hit timeout or max-turn limits during execution.",
                    "priority": "high",
                    "related_paths": ["config.yaml"],
                    "affected_path": "config.yaml",
                    "requirements": [
                        "Reduce timeout frequency by improving routing, prompts, or execution limits.",
                    ],
                    "success_criteria": [
                        "Timeout-related log events disappear for the affected workflow.",
                    ],
                    "context_id": context_id,
                    "agent_name": agent_name,
                    "evidence": context_evidence[context_id][0],
                    "metadata": {"event_count": count},
                }
            )
        return results

    def _is_duplicate(self, candidate: CandidateProblem) -> bool:
        open_statuses = {
            "observed",
            "requirements_draft",
            "requirements_approved",
            "experiment_active",
        }
        for problem in self.registry.list_problems():
            if problem.status.value not in open_statuses:
                continue
            if problem.source != "observer":
                continue
            if problem.metadata.get("dedup_key") == candidate.dedup_key:
                return True
        return False

    @staticmethod
    def _infer_agent_name(context: Dict[str, Any]) -> str:
        metadata = context.get("metadata", {}) or {}
        invocation = metadata.get("last_invocation", {}) or {}
        if invocation.get("agent"):
            return str(invocation["agent"])
        executions = context.get("execution_history", []) or []
        if executions and executions[0].get("agent_name"):
            return str(executions[0]["agent_name"])
        return "unknown"

    @staticmethod
    def _extract_context_id(message: str) -> Optional[str]:
        match = re.search(r"(ctx-[a-zA-Z0-9_-]+)", message)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_agent_name(message: str, logger_name: Optional[str] = None) -> str:
        if logger_name == "grid.mcp_manager":
            tool_match = re.search(r"MCP tool ([^:\s]+)", message, re.IGNORECASE)
            if tool_match:
                return f"tool:{tool_match.group(1)}"
        tagged = re.search(r"agent=([a-zA-Z0-9_:-]+)", message)
        if tagged:
            return tagged.group(1)
        quoted = re.search(r"agent ['\"]([^'\"]+)['\"]", message, re.IGNORECASE)
        if quoted:
            return quoted.group(1)
        bare = re.search(r"for ([a-zA-Z0-9_:-]+)", message)
        if bare:
            return bare.group(1)
        return "unknown"


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"Invalid --since value: {value}")
    return parsed


def run_observer_command(
    *,
    config_path: str = "config.yaml",
    workdir: Optional[str] = None,
    since: Optional[str] = None,
    min_occurrences: int = 3,
) -> int:
    config = Config(config_path, workdir)
    observer = LogObserver(config=config, min_occurrences=min_occurrences)
    result = observer.observe(since=_parse_since(since))
    print(f"Scanned contexts: {result.scanned_contexts}")
    print(f"Candidates found: {len(result.candidates)}")
    if result.created:
        print("Created problems:")
        for problem in result.created:
            print(problem.id)
    else:
        print("Created problems: none")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe logs and create improvement problems")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--min-occurrences", type=int, default=3)
    args = parser.parse_args()
    return run_observer_command(
        config_path=args.config,
        workdir=args.workdir,
        since=args.since,
        min_occurrences=args.min_occurrences,
    )


if __name__ == "__main__":
    raise SystemExit(main())
