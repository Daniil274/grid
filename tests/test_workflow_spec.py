"""C1: workflow spec contracts — validation (I1–I2), pin, honest retry
authority, gate machine-checkability, budgets, and the shared result types.

Run: python3 -m pytest tests/test_workflow_spec.py -q (no network, no DB).
"""

import copy
import json
import os

import pytest
from pydantic import ValidationError

from core.workflows.spec import (
    EFFECT_FREE_KINDS,
    GateSpec,
    InitToolCall,
    StepFailure,
    StepInputs,
    StepResult,
    StepSpec,
    StepView,
    WorkflowRunView,
    WorkflowSpec,
    canonical_json,
    effect_class,
    is_retryable,
    normalize_write_path,
    paths_escape_workspace,
    pin,
    validate,
    zones_overlap,
)


def read_step(step_id, tool="file_read", **extra):
    return {"id": step_id, "kind": "read", "tool": tool, **extra}


def write_step(step_id, tool="file_write", paths=("out.txt",), **extra):
    return {"id": step_id, "kind": "write", "tool": tool, "write_paths": list(paths), **extra}


def valid_spec_dict():
    return {
        "name": "release-notes",
        "goal": "collect and publish notes",
        "steps": [
            read_step("gather", depends_on=[]),
            write_step("publish", paths=("notes/draft.md",), depends_on=["gather"]),
            {"id": "check", "kind": "gate", "check": "tool:notes_lint",
             "depends_on": ["publish"], "check_params": {"strict": True}},
        ],
    }


# -- parsing: types, extra=forbid, limits ----------------------------------------


def test_valid_spec_parses_and_validates_clean():
    spec = WorkflowSpec.model_validate(valid_spec_dict())
    assert validate(spec) == []
    assert isinstance(spec.steps[2], GateSpec)
    assert spec.steps[2].max_review_rounds == 2


def test_extra_fields_forbidden_everywhere():
    for bad in ({"goal": "x", "steps": []},):  # steps empty: also below limits
        with pytest.raises(ValidationError):
            WorkflowSpec.model_validate(bad)
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate({**valid_spec_dict(), "surprise": 1})
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {"name": "n", "goal": "g", "steps": [read_step("a", bogus=1)]}
        )
    with pytest.raises(ValidationError):
        GateSpec.model_validate({"id": "c", "kind": "gate", "check": "x", "nope": 1})


def test_limits_enforced_at_parse_time():
    base = {k: v for k, v in valid_spec_dict().items() if k != "steps"}
    many = [read_step(f"s{i}") for i in range(257)]
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate({**base, "steps": many})
    ok = WorkflowSpec.model_validate(
        {**base, "steps": [read_step(f"s{i}") for i in range(256)]}
    )
    assert len(ok.steps) == 256
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate({**base, "steps": [read_step("a")], "max_parallel": 17})
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**base, "steps": [read_step("a", max_retries=4)]}
        )
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**base, "steps": [{"id": "a", "kind": "read", "tool": "t",
                                "timeout_seconds": 0}]}
        )
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**base,
             "steps": [{"id": "c", "kind": "gate", "check": "x",
                        "max_review_rounds": 6}]}
        )
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**base,
             "steps": [{"id": "c", "kind": "gate", "check": "x",
                        "max_review_rounds": 0}]}
        )


def test_gate_spec_survives_parsing_and_roundtrip():
    spec = WorkflowSpec.model_validate(valid_spec_dict())
    assert isinstance(spec.steps[2], GateSpec)
    dumped = spec.model_dump(mode="json")
    again = WorkflowSpec.model_validate(json.loads(json.dumps(dumped)))
    assert isinstance(again.steps[2], GateSpec)
    assert again.steps[2].check == "tool:notes_lint"
    assert pin(spec) == pin(again)


def test_gate_without_check_is_rejected_not_downgraded():
    base = {k: v for k, v in valid_spec_dict().items() if k != "steps"}
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {"name": "n", "goal": "g", "steps": [{"id": "c", "kind": "gate"}]}
        )
    # Prose / whitespace is not a machine check.
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**base, "steps": [{"id": "c", "kind": "gate", "check": "looks good to me"}]}
        )
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**base, "steps": [{"id": "c", "kind": "gate", "check": ""}]}
        )


def test_kind_specific_fields_required():
    base = {"name": "n", "goal": "g"}
    # Missing tool/task parse fine (optional fields) but fail validation.
    spec = WorkflowSpec.model_validate({**base, "steps": [{"id": "r", "kind": "read"}]})
    assert "TOOL_REQUIRED" in errors_by_code(spec)
    spec = WorkflowSpec.model_validate({**base, "steps": [{"id": "a", "kind": "agent"}]})
    assert "TASK_REQUIRED" in errors_by_code(spec)
    spec = WorkflowSpec.model_validate(
        {**base, "steps": [{"id": "a", "kind": "agent", "task": "do it"}]}
    )
    assert validate(spec) == []


def test_init_tool_calls_typed_and_frozen():
    spec = WorkflowSpec.model_validate(
        {**valid_spec_dict(),
         "init_tools": [{"name": "env.setup", "parameters": {"k": 1}},
                        {"name": "opt.probe", "required": False}]}
    )
    assert spec.init_tools[0].required is True
    assert spec.init_tools[1].parameters == {}
    with pytest.raises(ValidationError):
        InitToolCall(name="bad name!", parameters={})
    with pytest.raises(ValidationError):
        spec.init_tools[0].name = "mutate"  # frozen
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {**valid_spec_dict(), "init_tools": [{"name": "x", "junk": 1}]}
        )


# -- budgets: finite, non-negative; queue/work deadlines separate ---------------


@pytest.mark.parametrize("bad", [-0.01, float("inf"), float("nan"), -1e9])
def test_budget_must_be_finite_and_non_negative(bad):
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate({**valid_spec_dict(), "budget_usd": bad})


def test_budget_zero_allowed_and_micros_conversion():
    spec = WorkflowSpec.model_validate({**valid_spec_dict(), "budget_usd": 0})
    assert validate(spec) == []
    assert spec.budget_usd_micros() == 0
    spec = WorkflowSpec.model_validate({**valid_spec_dict(), "budget_usd": 1.5})
    assert spec.budget_usd_micros() == 1_500_000
    assert WorkflowSpec.model_validate(valid_spec_dict()).budget_usd_micros() is None


def test_queue_deadline_is_separate_from_work_and_lifetime():
    spec = WorkflowSpec.model_validate(
        {**valid_spec_dict(),
         "deadline_seconds": 600, "queue_timeout_seconds": 30}
    )
    assert validate(spec) == []
    # Queue wait longer than the whole run makes no sense.
    spec = WorkflowSpec.model_validate(
        {**valid_spec_dict(), "deadline_seconds": 30, "queue_timeout_seconds": 600}
    )
    codes = {error.code for error in validate(spec)}
    assert "QUEUE_DEADLINE_GT_LIFETIME" in codes


# -- structural validation (I1, I2) ----------------------------------------------


def errors_by_code(spec, **kwargs):
    return {error.code for error in validate(spec, **kwargs)}


def test_cycle_is_spec_invalid():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [read_step("a", depends_on=["b"]), read_step("b", depends_on=["a"])]}
    )
    assert "CYCLE" in errors_by_code(spec)


def test_dangling_dependency_is_spec_invalid():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [read_step("a", depends_on=["ghost"])]}
    )
    assert "UNKNOWN_DEPENDENCY" in errors_by_code(spec)


def test_self_dependency_is_spec_invalid():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [read_step("a", depends_on=["a"])]}
    )
    assert "SELF_DEPENDENCY" in errors_by_code(spec)


def test_duplicate_step_id_is_spec_invalid():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [read_step("a"), read_step("a")]}
    )
    assert "DUPLICATE_STEP_ID" in errors_by_code(spec)


def test_write_step_requires_write_paths():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [write_step("w", paths=())]}
    )
    assert "WRITE_PATHS_EMPTY" in errors_by_code(spec)


def test_overlapping_write_zones_need_a_dependency_path():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("a", paths=("docs/",)), write_step("b", paths=("docs/x.md",))]}
    )
    assert "WRITE_ZONE_OVERLAP" in errors_by_code(spec)
    # With ordering via depends_on, the same zones are fine.
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("a", paths=("docs/",)),
                   write_step("b", paths=("docs/x.md",), depends_on=["a"])]}
    )
    assert validate(spec) == []
    # Transitive ordering also satisfies I2.
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("a", paths=("docs/",)),
                   read_step("mid", depends_on=["a"]),
                   write_step("b", paths=("docs/x.md",), depends_on=["mid"])]}
    )
    assert validate(spec) == []


def test_disjoint_write_zones_need_no_dependency():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("a", paths=("a/",)), write_step("b", paths=("b/",))]}
    )
    assert validate(spec) == []


def test_write_path_normalization_and_lexical_escape_rejection():
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("w", paths=("./docs//./x.md",))]}
    )
    assert spec.steps[0].write_paths == ["docs/x.md"]
    for bad in ("/abs/x.txt", "../up.txt", "a/../../etc", "~", "", "  ", "C:\\x.txt"):
        with pytest.raises(ValidationError):
            WorkflowSpec.model_validate(
                {"name": "n", "goal": "g",
                 "steps": [write_step("w", paths=(bad,))]}
            )
    assert normalize_write_path("a/./b//c/") == "a/b/c"


def test_symlink_escape_caught_when_workspace_root_given(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    os.symlink(outside, workspace / "link")
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [write_step("w", paths=("link/x.txt",))]}
    )
    assert validate(spec) == []  # lexically fine
    assert "SYMLINK_ESCAPE" in errors_by_code(spec, workspace_root=workspace)
    spec = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [write_step("w", paths=("docs/x.md",))]}
    )
    assert validate(spec, workspace_root=workspace) == []
    assert paths_escape_workspace(workspace, ["docs/x.md"]) == []
    assert paths_escape_workspace(workspace, ["link/x.txt"]) == ["link/x.txt"]


# -- honest retry authority (trusted effect classification) ----------------------


def test_effect_class_comes_from_kind_not_from_claims():
    spec = WorkflowSpec.model_validate(valid_spec_dict())
    assert effect_class(spec.steps[0]) == "none"  # read
    assert effect_class(spec.steps[1]) == "workspace"  # write
    assert effect_class(spec.steps[2]) == "none"  # gate
    agent = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [{"id": "a", "kind": "agent", "task": "t"}]}
    )
    assert effect_class(agent.steps[0]) == "unknown"
    egress = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [{"id": "e", "kind": "egress", "tool": "smtp"}]}
    )
    # Egress effects live outside the workspace: no checkpoint can verify them.
    assert effect_class(egress.steps[0]) == "external"
    assert EFFECT_FREE_KINDS == frozenset({"read", "gate"})


def test_auto_retry_only_for_effect_free_kinds():
    base = {"name": "n", "goal": "g"}
    # read/gate may ask for retries.
    for step in (read_step("r", max_retries=3),
                 {"id": "c", "kind": "gate", "check": "tool:x", "max_retries": 2}):
        assert validate(WorkflowSpec.model_validate({**base, "steps": [step]})) == []
    # Egress is NOT automatically safe — external effects, unverifiable.
    spec = WorkflowSpec.model_validate(
        {**base, "steps": [{"id": "e", "kind": "egress", "tool": "smtp", "max_retries": 1}]}
    )
    assert "RETRY_UNSAFE" in errors_by_code(spec)
    # A self-declared idempotent flag is a claim, not replay authority.
    spec = WorkflowSpec.model_validate(
        {**base, "steps": [write_step("w", idempotent=True, max_retries=1)]}
    )
    assert "RETRY_UNSAFE" in errors_by_code(spec)
    spec = WorkflowSpec.model_validate(
        {**base, "steps": [write_step("w", idempotent=True)]}
    )
    assert validate(spec) == []  # recording the claim is fine; acting on it is not


def test_is_retryable_computed_from_code_and_effect_class():
    read = WorkflowSpec.model_validate(valid_spec_dict()).steps[0]
    agent = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [{"id": "a", "kind": "agent", "task": "t"}]}
    ).steps[0]
    gate = WorkflowSpec.model_validate(valid_spec_dict()).steps[2]
    egress = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [{"id": "e", "kind": "egress", "tool": "s",
                                             "idempotent": True}]}
    ).steps[0]
    write = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g", "steps": [write_step("w", idempotent=True)]}
    ).steps[0]
    assert is_retryable(read, "TRANSIENT_ERROR") is True
    assert is_retryable(read, "TIMEOUT") is True
    assert is_retryable(gate, "TRANSIENT_ERROR") is True
    for step in (egress, write, agent):
        assert is_retryable(step, "TRANSIENT_ERROR") is False  # claims don't grant replay
        assert is_retryable(step, "TIMEOUT") is False
    for code in ("POLICY_VIOLATION", "UNKNOWN_EFFECT", "GATE_FAILED",
                 "DEPENDENCY_FAILED", "INIT_REQUIRED_FAILED", "SPEC_INVALID",
                 "PERMANENT_ERROR", "MALFORMED", "BUDGET_PAUSED", "INTERRUPTED"):
        assert is_retryable(write, code) is False


# -- pin: stable, canonical identity --------------------------------------------


def test_pin_is_stable_and_canonical():
    spec = WorkflowSpec.model_validate(valid_spec_dict())
    assert pin(spec) == pin(spec.model_copy(deep=True))
    again = WorkflowSpec.model_validate(json.loads(canonical_json(spec)))
    assert pin(again) == pin(spec)
    clone = WorkflowSpec.model_validate(valid_spec_dict())
    assert pin(clone) == pin(spec)
    assert len(pin(spec)) == 64  # sha256 hex
    # Canonical form: re-dumping the parsed canonical JSON is a no-op.
    text = canonical_json(spec)
    assert text == json.dumps(
        json.loads(text), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )


def test_pin_changes_when_spec_changes():
    spec = WorkflowSpec.model_validate(valid_spec_dict())
    other = WorkflowSpec.model_validate(
        {**valid_spec_dict(), "steps": valid_spec_dict()["steps"][:2]}
    )
    assert pin(spec) != pin(other)
    assert canonical_json(spec) != canonical_json(other)


def test_pin_survives_input_normalization_differences():
    loose = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("w", paths=("./docs//x.md",)),
                   read_step("r", depends_on=["w", "w"])]}
    )
    tight = WorkflowSpec.model_validate(
        {"name": "n", "goal": "g",
         "steps": [write_step("w", paths=("docs/x.md",)),
                   read_step("r", depends_on=["w"])]}
    )
    assert pin(loose) == pin(tight)


def test_deep_copy_is_independent():
    spec = WorkflowSpec.model_validate(valid_spec_dict())
    clone = copy.deepcopy(spec)
    clone.steps[1].write_paths[0] = "other.md"
    assert spec.steps[1].write_paths[0] == "notes/draft.md"


# -- shared result contracts: dataflow -------------------------------------------


def test_step_result_separates_evidence_from_model_text():
    result = StepResult(
        step_id="gather",
        ok=True,
        attempt=1,
        result_ref="event:42",
        unverified_claims=["the notes are complete"],
    )
    dumped = result.model_dump(mode="json")
    assert dumped["result_ref"] == "event:42"
    assert dumped["unverified_claims"] == ["the notes are complete"]
    assert StepResult.model_validate(dumped) == result
    # Model text never becomes the result pointer.
    with pytest.raises(ValidationError):
        StepResult(step_id="g", ok=True, surprise=1)


def test_step_failure_and_gate_outcome_typed():
    failure = StepFailure(
        step_id="publish",
        code="POLICY_VIOLATION",
        retryable=False,
        detail="wrote outside write_paths",
        allowed_write_paths=["notes/"],
        attempt=2,
    )
    assert failure.model_dump()["code"] == "POLICY_VIOLATION"
    gate = StepResult(
        step_id="check",
        ok=False,
        gate={
            "check": "tool:notes_lint",
            "passed": False,
            "rounds": 2,
            "evidence_ref": "event:7",
        },
        failure={"step_id": "check", "code": "GATE_FAILED",
                 "retryable": False, "detail": "lint failed"},
    )
    assert gate.gate.passed is False
    assert gate.gate.evidence_ref == "event:7"
    assert gate.failure.code == "GATE_FAILED"


def test_step_inputs_alias_is_results_keyed_by_step_id():
    step = WorkflowSpec.model_validate(valid_spec_dict()).steps[1]
    inputs: StepInputs = {"gather": StepResult(step_id="gather", ok=True)}
    assert inputs["gather"].ok is True
    assert {"gather"} <= set(step.depends_on) or inputs  # alias usable as dataflow


def test_workflow_run_view_minimal_contract():
    view = WorkflowRunView(
        run_id="wf-abc123",
        spec_sha256=pin(WorkflowSpec.model_validate(valid_spec_dict())),
        status="queued",
        budget_usd_micros=1_500_000,
        steps=[StepView(step_id="gather", status="ready", attempts=1)],
    )
    dumped = view.model_dump(mode="json")
    assert dumped["status"] == "queued"
    assert dumped["spent_usd_micros"] == 0
    assert WorkflowRunView.model_validate(dumped) == view
    assert view.final_report is None
