"""The tracker's operation journal: durable, cross-process, honest about a gap."""

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core import tracker_operations as ops


def read_all(directory: Path) -> list[dict]:
    path = ops.journal_path(directory)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM operations ORDER BY rowid")]
    finally:
        connection.close()


def test_the_journal_lives_with_the_project_not_in_temp(tmp_path):
    operation = ops.begin(str(tmp_path), "plan", request_id="r1")

    path = ops.journal_path(tmp_path)
    assert path == tmp_path / ".grid" / "tracker-operations" / "journal.sqlite3"
    assert path.is_file()
    # Beside the project that owns it (tmp_path itself may sit under any
    # basetemp, /tmp included: the requirement is the project, not the prefix).
    assert tmp_path in path.parents
    # Never inside the tracker bd owns, and never in the per-uid gate.
    assert not (tmp_path / ".beads").exists()
    gate = str(tempfile.gettempdir()) + os.sep + "grid-tracker-gate"
    assert not str(path).startswith(gate)
    [started] = read_all(tmp_path)
    assert started["state"] == ops.STARTED and started["operation_id"] == operation.operation_id


def test_a_finished_operation_reads_back_with_its_answer(tmp_path):
    operation = ops.begin(str(tmp_path), "plan", request_id="r2", actor="grid:me", executor="run-1")
    ops.commit(operation, {"ids": {"a": "p-1"}}, code="ok")

    view = ops.lookup(tmp_path, request_id="r2")
    assert view["state"] == ops.COMMITTED and view["committed"] is True
    assert view["data"] == {"ids": {"a": "p-1"}}
    assert view["operation_id"] == operation.operation_id
    by_id = ops.lookup(tmp_path, operation_id=operation.operation_id)
    assert by_id["state"] == ops.COMMITTED


def test_an_unfinished_operation_stays_started_and_is_not_new_twice(tmp_path):
    first = ops.begin(str(tmp_path), "plan", request_id="r3")
    second = ops.begin(str(tmp_path), "plan", request_id="r3")

    assert first.is_new and not second.is_new
    assert second.operation_id == first.operation_id
    assert second.state == ops.STARTED


def test_fail_and_unknown_are_different_facts(tmp_path):
    failed = ops.begin(str(tmp_path), "create")
    ops.fail(failed, "bd refused the graph", code="tracker_rejected")
    unknown = ops.begin(str(tmp_path), "create")
    ops.unknown(unknown, "bd did not answer", code="tracker_unknown")

    assert ops.lookup(tmp_path, operation_id=failed.operation_id)["committed"] is False
    view = ops.lookup(tmp_path, operation_id=unknown.operation_id)
    assert view["state"] == ops.UNKNOWN and view["committed"] is None
    assert view["error"] == "bd did not answer"


def test_the_first_conclusion_is_the_one_a_repeat_hears(tmp_path):
    operation = ops.begin(str(tmp_path), "plan", request_id="r4")
    ops.commit(operation, {"ids": {"a": "p-1"}})
    ops.unknown(operation, "a second report", code="tracker_unknown")

    view = ops.lookup(tmp_path, request_id="r4")
    assert view["state"] == ops.COMMITTED and view["data"] == {"ids": {"a": "p-1"}}


def test_an_unknown_may_be_upgraded_by_proof_but_never_downgraded(tmp_path):
    proven = ops.begin(str(tmp_path), "create", request_id="u1")
    ops.unknown(proven, "the answer was lost", code="tracker_unknown")
    # A marker found later proves the write: unknown -> committed is allowed.
    ops.commit(proven, {"id": "p-1"}, code="reconciled")
    assert ops.lookup(tmp_path, request_id="u1")["state"] == ops.COMMITTED

    doubtful = ops.begin(str(tmp_path), "create", request_id="u2")
    ops.unknown(doubtful, "the answer was lost", code="tracker_unknown")
    # Nobody can prove a write that may have happened did not: no downgrade.
    ops.fail(doubtful, "someone says it failed", code="tracker_rejected")
    view = ops.lookup(tmp_path, request_id="u2")
    assert view["state"] == ops.UNKNOWN and view["committed"] is None


def test_a_first_record_that_cannot_be_read_refuses_the_write(tmp_path):
    # No half-written line is possible now: the journal is transactional. What
    # is still possible is a file that is not a readable database at all, and
    # that must never be mistaken for "this project never wrote".
    path = ops.journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"event": "started", "operation_id": "torn')

    with pytest.raises(ops.OperationError) as refused:
        ops.begin(str(tmp_path), "plan", request_id="r5")
    assert refused.value.code == "tracker_operation_corrupt"
    # Nothing was appended, so a repeat cannot create a second operation.
    assert not any(row.get("request_id") == "r5" for row in _rows_safely(path))
    assert path.read_bytes().startswith(b'{"event"')


def _rows_safely(path: Path) -> list[dict]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM operations")]
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return []


def test_a_request_id_reused_for_a_different_payload_is_a_conflict(tmp_path):
    ops.begin(str(tmp_path), "create", request_id="same", detail={"title": "Fix the build"})

    with pytest.raises(ops.OperationError) as refused:
        ops.begin(str(tmp_path), "create", request_id="same", detail={"title": "Delete the build"})
    assert refused.value.code == "tracker_request_conflict"

    # The same payload is still a repeat, not a conflict.
    repeat = ops.begin(str(tmp_path), "create", request_id="same", detail={"title": "Fix the build"})
    assert not repeat.is_new


def test_two_spellings_of_one_project_share_one_journal(tmp_path):
    real = tmp_path / "project"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    first = ops.begin(str(real), "plan", request_id="alias")
    second = ops.begin(str(alias), "plan", request_id="alias")

    assert not second.is_new and second.operation_id == first.operation_id
    assert ops.journal_path(alias) == ops.journal_path(real)
    assert len(read_all(real)) == 1


def test_a_subdirectory_and_a_redirect_share_the_workspace_journal(tmp_path):
    project = tmp_path / "project"
    (project / ".beads").mkdir(parents=True)
    (project / ".beads" / "config.yaml").write_text("", encoding="utf-8")
    sub = project / "src"
    sub.mkdir()

    # bd run in a subdirectory opens the same tracker: one journal.
    assert ops.journal_path(sub) == ops.journal_path(project)
    started = ops.begin(str(sub), "create", request_id="sub")
    assert started.is_new and started.directory == str(project)
    assert ops.lookup(project, request_id="sub") is not None

    # A .beads/redirect aliases another directory onto the same tracker.
    other = tmp_path / "other"
    (other / ".beads").mkdir(parents=True)
    (other / ".beads" / "redirect").write_text(str(project / ".beads"), encoding="utf-8")
    assert ops.journal_path(other) == ops.journal_path(project)
    assert ops.lookup(other, request_id="sub") is not None


@pytest.mark.parametrize("value", ["has space", "x" * 200, "-leading"])
def test_a_request_id_that_cannot_be_a_key_is_refused(value):
    with pytest.raises(ops.OperationError) as refused:
        ops.check_request_id(value)
    assert refused.value.code == "tracker_request_id"


def test_no_request_id_is_allowed_and_still_makes_an_operation(tmp_path):
    assert ops.check_request_id("") is None and ops.check_request_id(None) is None
    operation = ops.begin(str(tmp_path), "create")
    assert operation.operation_id and operation.request_id is None


def test_the_journal_is_read_by_another_process(tmp_path):
    operation = ops.begin(str(tmp_path), "plan", request_id="r6", actor="grid:me")
    ops.commit(operation, {"ids": {"a": "p-1"}})

    code = (
        "import json,sys;"
        "from core import tracker_operations as ops;"
        "print(json.dumps(ops.lookup(sys.argv[1], request_id='r6')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    view = json.loads(out.stdout)
    assert view["state"] == ops.COMMITTED and view["data"] == {"ids": {"a": "p-1"}}


def _begin_in_child(directory: str, request_id: str, start, results) -> None:
    """One process of the race: begin the same request, report whether it was new."""
    from core import tracker_operations as ops  # noqa: F401 - re-imported per process

    start.wait(30)
    operation = ops.begin(directory, "plan", request_id=request_id)
    results.put((operation.is_new, operation.operation_id))


def test_two_processes_beginning_one_request_make_one_operation(tmp_path):
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    children = [
        context.Process(target=_begin_in_child, args=(str(tmp_path), "shared", start, results))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    start.set()
    answers = [results.get(timeout=60) for _ in children]
    for child in children:
        child.join(30)

    assert sorted(new for new, _ in answers) == [False, True]
    assert len({operation_id for _, operation_id in answers}) == 1
    assert len([row for row in read_all(tmp_path) if row.get("state") == ops.STARTED]) == 1


def _append_many(directory: str, tag: str, count: int, start) -> None:
    from core import tracker_operations as ops

    start.wait(30)
    for index in range(count):
        operation = ops.begin(directory, f"{tag}-{index}")
        ops.commit(operation, {"index": index})


def test_concurrent_appends_from_two_processes_all_survive(tmp_path):
    context = multiprocessing.get_context("fork")
    start = context.Event()
    children = [
        context.Process(target=_append_many, args=(str(tmp_path), tag, 25, start))
        for tag in ("a", "b")
    ]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(60)

    rows = read_all(tmp_path)
    assert len(rows) == 50
    assert len({row["operation_id"] for row in rows}) == 50
    assert all(row["state"] == ops.COMMITTED for row in rows)


def _conclude_in_child(directory: str, request_id: str, state: str, start, results) -> None:
    """A second process concludes the operation the first one began."""
    from core import tracker_operations as ops

    start.wait(30)
    view = ops.lookup(directory, request_id=request_id)
    operation = ops.Operation(
        operation_id=view["operation_id"],
        request_id=request_id,
        kind=view["kind"],
        directory=view["directory"],
        tracker=view["tracker"],
        state=view["state"],
        record=view,
        is_new=False,
    )
    if state == "committed":
        ops.commit(operation, {"thief": False})
    else:
        ops.unknown(operation, "lost")
    results.put(ops.lookup(directory, request_id=request_id)["state"])


def test_a_conclusion_from_another_process_wins_once(tmp_path):
    operation = ops.begin(str(tmp_path), "plan", request_id="cross")
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    children = [
        context.Process(target=_conclude_in_child, args=(str(tmp_path), "cross", state, start, results))
        for state in ("committed", "unknown")
    ]
    for child in children:
        child.start()
    start.set()
    answers = [results.get(timeout=60) for _ in children]
    for child in children:
        child.join(30)

    # Exactly one first conclusion, and nobody can prove a write that may
    # have happened did not: the "unknown" child may read the state before the
    # other one's commit upgrades it, but it never undoes it. The answer order
    # is the queue's, not the children's, so both values are allowed.
    assert set(answers) <= {ops.COMMITTED, ops.UNKNOWN}
    assert ops.COMMITTED in answers  # the commit won, whoever read first
    assert ops.lookup(tmp_path, operation_id=operation.operation_id)["state"] == ops.COMMITTED


def test_the_marker_names_the_operation_and_its_key():
    assert ops.marker("abc") == "grid-op:abc"
    assert ops.marker("abc", "api") == "grid-op:abc:api"
    assert ops.marker_prefix("abc") == "grid-op:abc"


# -- the request id belongs to an actor ---------------------------------------

def test_two_actors_may_use_one_request_id_without_sharing_an_operation(tmp_path):
    alice = ops.begin(str(tmp_path), "create", request_id="retry-1", actor="grid:alice",
                      detail={"title": "A"})
    bob = ops.begin(str(tmp_path), "create", request_id="retry-1", actor="grid:bob",
                    detail={"title": "B"})

    assert alice.is_new and bob.is_new
    assert alice.operation_id != bob.operation_id
    assert ops.lookup(tmp_path, request_id="retry-1", actor="grid:alice")["detail"] == {"title": "A"}
    assert ops.lookup(tmp_path, request_id="retry-1", actor="grid:bob")["detail"] == {"title": "B"}
    # The journal read as an administrator still sees both.
    assert len([row for row in read_all(tmp_path) if row["request_id"] == "retry-1"]) == 2


def test_one_actor_repeating_a_key_is_still_a_repeat_from_another_run(tmp_path):
    first = ops.begin(str(tmp_path), "create", request_id="once", actor="grid:alice",
                      executor="run-a", detail={"title": "A"})
    second = ops.begin(str(tmp_path), "create", request_id="once", actor="grid:alice",
                       executor="run-b", detail={"title": "A"})

    assert not second.is_new and second.operation_id == first.operation_id
    assert second.record["executor"] == "run-a"  # the write stays the first run's


def test_a_key_reused_within_another_actor_is_not_a_conflict(tmp_path):
    ops.begin(str(tmp_path), "create", request_id="k", actor="grid:alice", detail={"title": "A"})
    # A different payload under the same key by another actor is that actor's
    # own request, not a conflict with somebody else's.
    other = ops.begin(str(tmp_path), "create", request_id="k", actor="grid:bob", detail={"title": "B"})
    assert other.is_new


def test_an_old_global_request_index_is_migrated_to_the_actor_namespace(tmp_path):
    first = ops.begin(str(tmp_path), "create", request_id="k", actor="grid:alice", detail={"t": 1})
    path = ops.journal_path(tmp_path)

    # Rewind the schema to the one an older journal has: one global key.
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX operations_request_actor")
        connection.execute(
            "CREATE UNIQUE INDEX operations_request ON operations(request_id) WHERE request_id IS NOT NULL"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError):
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "INSERT INTO operations (operation_id, request_id, fingerprint, kind, tracker, "
                "directory, actor, executor, detail, state, created_at, updated_at) "
                "VALUES ('x', 'k', 'f', 'create', 't', ?, 'grid:bob', NULL, '{}', 'started', "
                "'now', 'now')",
                (str(tmp_path),),
            )
            connection.commit()
        finally:
            connection.close()

    # The next write migrates it: another actor's key is no longer taken.
    ops._migrated.discard(str(path))
    other = ops.begin(str(tmp_path), "create", request_id="k", actor="grid:bob", detail={"t": 2})
    assert other.is_new and other.operation_id != first.operation_id
    assert ops.lookup(tmp_path, request_id="k", actor="grid:alice")["detail"] == {"t": 1}
