# Module Self-Improvement

## Purpose

The self-improvement module turns Grid into a controlled improvement system.
It does not let agents "rewrite themselves directly" in the live runtime.
Instead, it manages a staged loop:

`observe -> define problem -> approve requirements -> create experiment -> evaluate -> review -> promote or reject`

This gives the system three important properties:

- every change is explicit and traceable;
- every change can be measured against a baseline;
- human review can stay in the loop where risk is high.

## Current Status

Implemented today:

- benchmark harness and baseline scorecards;
- log observer that creates structured improvement problems;
- JSON-backed improvement registry;
- human requirement and final-review gates;
- bounded config experiments with `config_diff`;
- heuristic config proposer;
- benchmark evaluation for experiments;
- CLI flow for approve, propose, evaluate;
- auto-evaluation after proposal through config.

Planned next:

- canary rollout and rollback;
- isolated code experiments in worktrees;
- background scheduler and fleet-aware execution.

The implementation roadmap is tracked in:

- [improvement-loop-roadmap.md](/d:/Work/repo/agents_portable/plans/improvement-loop-roadmap.md)
- [improvement-loop-stage1.md](/d:/Work/repo/agents_portable/plans/improvement-loop-stage1.md)
- [improvement-loop-stage2.md](/d:/Work/repo/agents_portable/plans/improvement-loop-stage2.md)
- [improvement-loop-stage3.md](/d:/Work/repo/agents_portable/plans/improvement-loop-stage3.md)
- [improvement-loop-stage4.md](/d:/Work/repo/agents_portable/plans/improvement-loop-stage4.md)
- [improvement-loop-stage5.md](/d:/Work/repo/agents_portable/plans/improvement-loop-stage5.md)
- [improvement-loop-stage6.md](/d:/Work/repo/agents_portable/plans/improvement-loop-stage6.md)

## Architecture

The module is split into a few focused components.

### Registry and Schemas

- [core/improvement_registry.py](/d:/Work/repo/agents_portable/core/improvement_registry.py)
- [schemas/improvement.py](/d:/Work/repo/agents_portable/schemas/improvement.py)

These files define the system of record for:

- `ImprovementProblem`
- `ImprovementExperiment`
- `ImprovementReview`
- `ImprovementConfigDiff`

The registry is persisted as JSON in:

- [data/improvement_registry.json](/d:/Work/repo/agents_portable/data/improvement_registry.json)

This registry is the source of truth for status, reviews, evaluation summaries,
baseline scorecards, candidate scorecards, and promotion history.

### Observer

- [core/log_observer.py](/d:/Work/repo/agents_portable/core/log_observer.py)

The observer scans log artifacts and converts repeated failures into structured
problems. It currently detects signals such as:

- `provider_error`
- `routing_error`
- `tool_missing`
- `tool_schema_error`
- `context_overflow`
- `model_timeout`
- `user_correction`

The observer deduplicates already-open issues to avoid filling the registry
with repeated copies of the same failure pattern.

### Proposer

- [core/config_proposer.py](/d:/Work/repo/agents_portable/core/config_proposer.py)

The proposer builds a bounded `config_diff` from an observed problem.
It only works inside the configured allowlist and only for supported low-risk
cases. Today it can propose changes for:

- provider failures;
- routing failures;
- context overflow;
- coordinator tool-schema instability.

The proposer can also auto-run evaluation after experiment creation when
enabled in config.

### Evaluator

- [core/evaluator.py](/d:/Work/repo/agents_portable/core/evaluator.py)
- [benchmarks/run.py](/d:/Work/repo/agents_portable/benchmarks/run.py)

The evaluator compares an experiment candidate against a baseline benchmark.
For config experiments it:

1. loads the baseline scorecard;
2. creates a temporary config copy;
3. applies the candidate `config_diff`;
4. runs the benchmark suite;
5. stores baseline and candidate scorecards in the registry;
6. marks the experiment as passed or rejected based on thresholds.

### Tool Surface

- [tools/evolution_tools.py](/d:/Work/repo/agents_portable/tools/evolution_tools.py)
- [tools/function_tools.py](/d:/Work/repo/agents_portable/tools/function_tools.py)

These tools expose the module to agents in a bounded way:

- `create_improvement_problem`
- `list_improvement_problems`
- `create_improvement_experiment`
- `propose_config_experiment`
- `evaluate_improvement_experiment`
- `record_requirement_review`
- `record_final_review`
- `promote_improvement_experiment`
- `reject_improvement_experiment`

### CLI Surface

- [grid.py](/d:/Work/repo/agents_portable/grid.py)

The CLI is the simplest operational interface for now:

- `python -m grid observe`
- `python -m grid approve --problem-id ...`
- `python -m grid approve --experiment-id ...`
- `python -m grid propose --problem-id ...`
- `python -m grid evaluate --experiment-id ...`

## Lifecycle

### 1. Observe

The system scans logs and creates an `ImprovementProblem`.

Example:

```powershell
python -m grid observe --config config.yaml --min-occurrences 3
```

### 2. Review Requirements

On the current configuration, problem requirements are human-gated.
This means the system does not open an experiment until a person confirms that:

- the problem framing is correct;
- the success criteria make sense;
- the change is worth attempting.

Example:

```powershell
python -m grid approve --config config.yaml --problem-id problem-12345678 --reviewer admin --summary "Approve requirements"
```

### 3. Propose Experiment

The proposer creates an `ImprovementExperiment` with a measurable diff.

Example:

```powershell
python -m grid propose --config config.yaml --problem-id problem-12345678
```

When `improvement.auto_evaluate_proposed_experiments` is enabled, this step
also runs benchmark evaluation automatically.

### 4. Evaluate

If evaluation is not triggered automatically, it can be run directly:

```powershell
python -m grid evaluate --config config.yaml --experiment-id experiment-12345678
```

Evaluation writes:

- `baseline_scorecard`
- `candidate_scorecard`
- `evaluation_summary`

to the experiment record in the registry.

### 5. Final Review

Final review remains human-gated by default.
This is the last checkpoint before permanent application.

### 6. Promote or Reject

Promotion applies the approved diff to the main config only after:

- benchmark checks pass;
- required human review exists;
- experiment status is promotion-ready.

Rejected experiments remain in the registry for learning and audit.

## Configuration

Main settings live under `improvement:` in:

- [config.yaml](/d:/Work/repo/agents_portable/config.yaml)

Important keys:

- `enabled`
- `registry_path`
- `plans_directory`
- `require_human_requirements_review`
- `require_human_final_review`
- `auto_promote_safe_changes`
- `auto_evaluate_proposed_experiments`
- `require_benchmark_before_promotion`
- `promotion_threshold`
- `rollback_threshold`
- `canary_window_minutes`
- `allowed_change_types`
- `allowed_paths`
- `allowed_config_keys`
- `max_open_experiments`

### Safety Model

The module is intentionally restrictive.

Current safety boundaries:

- only configured change types are allowed;
- only configured paths are allowed;
- only configured config keys may be changed automatically;
- human review cannot be forged through agent tool calls;
- promotion can be blocked until benchmark evaluation exists;
- failed evaluation rejects the experiment instead of applying it.

## Benchmarking

Benchmark inputs and outputs live in:

- [benchmarks/fixtures](/d:/Work/repo/agents_portable/benchmarks/fixtures)
- [benchmarks/metrics.yaml](/d:/Work/repo/agents_portable/benchmarks/metrics.yaml)
- [benchmarks/baseline.json](/d:/Work/repo/agents_portable/benchmarks/baseline.json)
- [benchmarks/runs](/d:/Work/repo/agents_portable/benchmarks/runs)

This layer exists so the module optimizes measurable outcomes, not "intuition".

Today the benchmark harness is mostly focused on system-level smoke and routing
checks. As the improvement loop grows, this suite should expand with:

- regression fixtures for observed failures;
- adversarial fixtures for unsafe changes;
- replay fixtures for real user scenarios.

## State Machine

Problem states and experiment states are defined in:

- [schemas/improvement.py](/d:/Work/repo/agents_portable/schemas/improvement.py)

At a high level:

- problems move from `observed` to `requirements_approved` to `experiment_active` to `solved`;
- experiments move from `in_review` to `promotion_pending` to `promoted`, or to `rejected`.

The evaluator also introduces evaluation-related states used internally for
the staged loop.

## Human In The Loop

The module is designed so human involvement can be reduced over time without
removing safety.

Current mode:

- human requirement review is enabled;
- human final review is enabled;
- promotion is therefore effectively supervised.

Future mode:

- low-risk, benchmark-clean config changes may be auto-promoted;
- risky changes may still require explicit final review;
- code-level changes should remain more heavily gated than config changes.

## Operational Notes

- The observer is safe to run repeatedly; it deduplicates open issues.
- The registry should be treated as an operational artifact and backed up.
- Benchmark evaluation may start MCP/tool infrastructure and therefore takes
  longer than pure unit tests.
- If Docker isolation is configured but the Docker SDK is missing, the system
  currently degrades by disabling isolation instead of hard-failing.

## Known Gaps

Not implemented yet:

- canary rollout state with live traffic sampling;
- automatic rollback after post-promotion regression;
- isolated worktree-based code experiments;
- fleet scheduler for multi-device model routing;
- budget-aware autonomous background loop.

Those items are already planned in the stage documents and should be treated
as the next expansion of the module, not as missing documentation.

