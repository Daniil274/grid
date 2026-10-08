"""The projects store: the hierarchy's invariants and its one atomic file.

These are plain store tests - no server, no conversations. The API that moves
the chats on top of it lives in tests/test_projects_api.py.
"""

import json

import pytest

from web_chat.projects import MAX_DEPTH, ProjectError, ProjectStore


def store(tmp_path):
    return ProjectStore(tmp_path / "projects.json")


def test_create_get_list_and_persistence(tmp_path):
    first = store(tmp_path)
    root = first.create("Grid")
    child = first.create("Chats", parent_id=root.id, description="the rail")
    assert root.id.startswith("prj-") and root.parent_id is None
    assert first.get(root.id).title == "Grid"
    assert first.exists(child.id) and not first.exists("prj-nope")
    assert [project.id for project in first.list()] == [root.id, child.id]

    # A fresh store reads the same file back.
    again = store(tmp_path)
    loaded = again.get(child.id)
    assert loaded.parent_id == root.id and loaded.description == "the rail"


def test_title_is_collapsed_and_a_blank_title_is_refused(tmp_path):
    projects = store(tmp_path)
    assert projects.create("  Two   words ").title == "Two words"
    with pytest.raises(ProjectError):
        projects.create("   ")
    with pytest.raises(ProjectError):
        projects.update(projects.create("A").id, title="  ")


def test_a_missing_parent_is_refused(tmp_path):
    projects = store(tmp_path)
    with pytest.raises(ProjectError):
        projects.create("Orphan", parent_id="prj-ghost")


def test_depth_is_capped_at_three(tmp_path):
    projects = store(tmp_path)
    first = projects.create("one")
    second = projects.create("two", parent_id=first.id)
    third = projects.create("three", parent_id=second.id)
    assert MAX_DEPTH == 3
    with pytest.raises(ProjectError):
        projects.create("four", parent_id=third.id)


def test_a_project_cannot_be_its_own_parent_or_descend_into_itself(tmp_path):
    projects = store(tmp_path)
    first = projects.create("one")
    second = projects.create("two", parent_id=first.id)
    third = projects.create("three", parent_id=second.id)
    with pytest.raises(ProjectError):
        projects.update(first.id, parent_id=first.id)
    with pytest.raises(ProjectError):
        projects.update(first.id, parent_id=third.id)
    # A legal move with room still works.
    moved = projects.update(third.id, parent_id=first.id)
    assert moved.parent_id == first.id


def test_moving_checks_the_whole_subtree_height(tmp_path):
    projects = store(tmp_path)
    a = projects.create("a")
    b = projects.create("b", parent_id=a.id)
    c = projects.create("c", parent_id=b.id)
    other = projects.create("other")
    # b (with child c) cannot go under other: it would be depth 3, c depth 4.
    with pytest.raises(ProjectError):
        projects.update(b.id, parent_id=other.id)
    # c alone fits under other.
    assert projects.update(c.id, parent_id=other.id).parent_id == other.id


def test_update_only_touches_the_fields_given(tmp_path):
    projects = store(tmp_path)
    project = projects.create("one", description="keep me")
    assert projects.update(project.id, archived=True).archived is True
    still = projects.get(project.id)
    assert still.description == "keep me" and still.title == "one"
    assert still.notes == ""
    notes = projects.update(project.id, notes="shared context")
    assert notes.notes == "shared context"


def test_update_unknown_project_is_a_key_error(tmp_path):
    with pytest.raises(KeyError):
        store(tmp_path).update("prj-ghost", title="x")


def test_delete_reparents_children_and_reports_them(tmp_path):
    projects = store(tmp_path)
    root = projects.create("root")
    middle = projects.create("middle", parent_id=root.id)
    leaf = projects.create("leaf", parent_id=middle.id)
    sibling = projects.create("sibling", parent_id=middle.id)

    parent_id, moved = projects.delete(middle.id)
    assert parent_id == root.id
    assert set(moved) == {leaf.id, sibling.id}
    assert projects.get(leaf.id).parent_id == root.id
    assert projects.get(sibling.id).parent_id == root.id
    assert projects.get(middle.id) is None
    with pytest.raises(KeyError):
        projects.delete(middle.id)


def test_delete_a_root_promotes_children_to_the_top(tmp_path):
    projects = store(tmp_path)
    root = projects.create("root")
    child = projects.create("child", parent_id=root.id)
    parent_id, moved = projects.delete(root.id)
    assert parent_id is None and moved == [child.id]
    assert projects.get(child.id).parent_id is None


def test_writes_are_atomic_and_the_tmp_is_gone(tmp_path):
    root = tmp_path / "nested"
    projects = ProjectStore(root / "projects.json")
    project = projects.create("one")
    path = root / "projects.json"
    assert path.exists()
    assert not (root / "projects.json.tmp").exists()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["projects"][0]["id"] == project.id


def test_a_returned_project_is_a_detached_copy(tmp_path):
    projects = store(tmp_path)
    project = projects.create("one")
    project.title = "mutated"
    assert projects.get(project.id).title == "one"
