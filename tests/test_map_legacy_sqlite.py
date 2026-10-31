"""Legacy tracker compatibility without migration, using only synthetic data."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from web_chat import map_api
from web_chat.map_check import snapshot


@pytest.fixture
def legacy(tmp_path):
    project = tmp_path / 'prj-legacy'
    beads = project / '.beads'
    beads.mkdir(parents=True)
    (beads / 'config.yaml').write_text('issue-prefix: prj-legacy\n')
    (beads / 'metadata.json').write_text(json.dumps({'database': 'beads.db', 'jsonl_export': 'issues.jsonl'}))
    with sqlite3.connect(beads / 'beads.db') as db:
        db.executescript('''
            CREATE TABLE issues (id TEXT PRIMARY KEY, title TEXT, status TEXT,
                priority INTEGER, issue_type TEXT, created_at TEXT, notes TEXT);
            CREATE TABLE dependencies (issue_id TEXT, depends_on_id TEXT, type TEXT);
            INSERT INTO issues VALUES ('prj-legacy-a', 'First', 'open', 1, 'task', '2020-01-01', 'SECRET');
            INSERT INTO issues VALUES ('prj-legacy-b', 'Second', 'closed', 2, 'task', '2020-01-02', 'SECRET');
            INSERT INTO dependencies VALUES ('prj-legacy-a', 'prj-legacy-b', 'blocks');
        ''')
    return project


def refusal():
    return map_api._failure(map_api.ReasonCode.BD_FAILED,
                            map_api.DiagnosisReport(map_api.Diagnosis.LEGACY_SQLITE, 1))


def test_real_bd_legacy_map_read_preserves_tasks_and_original(legacy):
    """bd 1.3 refuses SQLite; the map still reads the copy without migrating."""
    before = snapshot(legacy)
    trace = map_api.ReadTrace()
    space = SimpleNamespace(chat_spaces=True, layout=SimpleNamespace(project_spaces=legacy.parent))
    tasks = asyncio.run(map_api._tasks(space, legacy.name, trace))
    assert tasks.payload['available'], tasks.payload
    assert [item['id'] for item in tasks.payload['items']] == ['prj-legacy-a', 'prj-legacy-b']
    assert tasks.payload['items'][0]['depends_on'] == ['prj-legacy-b']
    assert tasks.payload['items'][0]['state'] == 'ready'
    assert tasks.payload['counts']['closed'] == 1
    assert 'SECRET' not in json.dumps(tasks.payload)
    assert snapshot(legacy) == before
    assert trace.reason is None
    if trace.returncode == 1:
        assert 'historical SQLite workspace detected' in trace.stderr
        assert trace.diagnosis is None


def test_adapter_reads_dependencies_but_not_relations(legacy):
    database = legacy / '.beads/beads.db'
    with sqlite3.connect(database) as db:
        db.execute("UPDATE issues SET status='open' WHERE id='prj-legacy-b'")
        db.execute("INSERT INTO dependencies VALUES ('prj-legacy-b', 'prj-legacy-a', 'relates-to')")
    result = map_api._legacy_sqlite_answer(refusal(), database, None)
    from web_chat.tasks_api import _task
    statuses = {issue['id']: issue['status'] for issue in result['data']}
    assert _task(result['data'][0], statuses)['state'] == 'waiting'
    assert _task(result['data'][1], statuses)['depends_on'] == []


def test_legacy_foreign_ids_are_still_refused(legacy):
    with sqlite3.connect(legacy / '.beads/beads.db') as db:
        db.execute("UPDATE issues SET id='other-a' WHERE id='prj-legacy-a'")
    space = SimpleNamespace(chat_spaces=True, layout=SimpleNamespace(project_spaces=legacy.parent))
    tasks = asyncio.run(map_api._tasks(space, legacy.name))
    assert tasks.reason is map_api.ReasonCode.FOREIGN_TRACKER
    assert not tasks.payload['available']


@pytest.mark.parametrize('schema', [
    'CREATE TABLE issues(id TEXT, title TEXT)',
    'CREATE VIEW issues AS SELECT 1 AS id, 2 AS title, 3 AS status',
    'CREATE TABLE issues(id TEXT, title TEXT, status TEXT); CREATE TABLE dependencies(x TEXT)',
    'CREATE TABLE issues(id TEXT, title TEXT, status TEXT); CREATE VIEW dependencies AS SELECT 1 AS issue_id',
])
def test_unknown_schemas_fail_closed(tmp_path, schema):
    database = tmp_path / 'beads.db'
    with sqlite3.connect(database) as db:
        db.executescript(schema)
    answer = refusal()
    assert map_api._legacy_sqlite_answer(answer, database, None) is answer


def test_corrupt_sqlite_does_not_claim_success(tmp_path):
    database = tmp_path / 'beads.db'
    database.write_bytes(b'not SQLite')
    answer = refusal()
    assert map_api._legacy_sqlite_answer(answer, database, None) is answer


def test_adapter_is_not_a_generic_error_fallback(legacy):
    answer = map_api._failure(map_api.ReasonCode.BD_FAILED,
                             map_api.DiagnosisReport(map_api.Diagnosis.DATABASE_ERROR, 1))
    assert map_api._legacy_sqlite_answer(answer, legacy / '.beads/beads.db', None) is answer


def test_adapter_bounds_rows(legacy, monkeypatch):
    monkeypatch.setattr(map_api, 'MAX_TRACKER_COPY_ENTRIES', 1)
    result = map_api._legacy_sqlite_answer(refusal(), legacy / '.beads/beads.db', None)
    assert result['code'] is map_api.ReasonCode.TRACKER_TOO_LARGE


def test_legacy_diagnosis_is_specific_and_safe():
    result = SimpleNamespace(returncode=1, stderr='SECRET /private historical SQLite workspace detected; explicit migration is required')
    diagnosis = map_api._diagnose(result, 'docker')
    assert diagnosis.code is map_api.Diagnosis.LEGACY_SQLITE
    assert 'SECRET' not in json.dumps(diagnosis.payload())
    assert 'corrupt' not in diagnosis.payload()['message']


def test_container_refusal_uses_the_host_copy_adapter(legacy, monkeypatch):
    """A container-side refusal is resolved by the host-side adapter on the copy.

    The container is handed the tracker only as a stream; the adapter then
    reads the host copy, never the project.
    """
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout='', stderr='historical SQLite workspace detected')
    monkeypatch.setattr(map_api.subprocess, 'run', run)
    before = snapshot(legacy)
    trace = map_api.ReadTrace()
    with map_api._tracker_archive(legacy) as archive:
        answer = map_api._run_in_container('synthetic-image', legacy,
                                           legacy / '.beads/beads.db', archive,
                                           list(map_api.BD_LIST_ARGS), trace)
    assert answer['success']
    assert len(answer['data']) == 2
    assert trace.returncode == 1 and trace.reason is None
    assert snapshot(legacy) == before


def test_live_wal_is_read_from_copy_without_changing_source(legacy):
    database = legacy / '.beads/beads.db'
    with sqlite3.connect(database) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO issues(id,title,status) VALUES ('prj-legacy-c','In WAL','open')")
        writer.commit()
        before = snapshot(legacy)
        answer = map_api._read_tracker(legacy, list(map_api.BD_LIST_ARGS))
        assert answer['success'], answer
        assert len(answer['data']) == 3
        assert snapshot(legacy) == before


def test_blob_fields_are_not_sent_to_json(legacy):
    database = legacy / '.beads/beads.db'
    with sqlite3.connect(database) as db:
        db.execute("UPDATE issues SET title=x'1234' WHERE id='prj-legacy-a'")
    answer = refusal()
    assert map_api._legacy_sqlite_answer(answer, database, None) is answer
