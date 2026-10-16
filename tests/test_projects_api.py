"""The projects API: continues that carry context, and the projects' plan.

The routes mount on the real :class:`WebChatServer` over a dummy space that
lends a real ``ContextManager`` and a real ``ProjectStore`` (see the projects
section of ``test_web_chat_session``); the tracker is mocked, so no bd runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

SAME_SITE = {"Origin": "http://testserver"}

#: A plan snapshot as a turn's trace leaves it: one open task.
PLAN = {
    "tasks": [
        {"id": "grid-a1", "key": "a1", "title": "Do it", "status": "open", "depends_on": []},
    ],
    "counts": {"ready": 1, "waiting": 0, "in_progress": 0, "blocked": 0, "closed": 0},
    "total": 1,
}

#: A bd issue as ``list --json`` prints it.
ISSUE = {
    "id": "grid-a1",
    "title": "Do it",
    "status": "closed",
    "description": "",
    "acceptance_criteria": "",
    "notes": "",
    "priority": 2,
    "issue_type": "task",
    "dependencies": [],
    "close_reason": "done",
}


def _plan_message(text: str = "planned") -> dict:
    return {"metadata": {"trace": [{"kind": "plan", "plan": PLAN}]}}


@pytest.fixture
def space(tmp_path):
    """A dummy space with a real ContextManager and a real ProjectStore."""
    from core.context import ContextManager

    from tests.test_web_ui_routes import _DummySpace
    from web_chat.projects import ProjectStore

    dummy = _DummySpace()
    manager = ContextManager(persist_path=str(tmp_path / "conversations.json"))
    dummy._context_manager = manager
    dummy.conversations_path = tmp_path / "conversations.json"
    store = ProjectStore(tmp_path / "projects.json")
    dummy.project_store = store
    dummy.project_exists = store.exists

    def update(context_id, **updates):
        manager.update_context_metadata(context_id, {k: v for k, v in updates.items() if v is not None})

    def by_project():
        grouped = {}
        for view in manager.conversation_views():
            project_id = (view.get("metadata") or {}).get("project_id") or None
            grouped.setdefault(project_id, []).append(view["id"])
        return grouped

    dummy.update_conversation_metadata = update
    dummy.conversations_by_project = by_project
    return dummy


@pytest.fixture
def client(space):
    from fastapi.testclient import TestClient

    from tests.test_web_ui_routes import _server

    return TestClient(_server(space).app, headers=SAME_SITE)


def _messages(space, context_id):
    return space.context_manager().conversation_view(context_id)["messages"]


def _seeds(space, context_id):
    return [m for m in _messages(space, context_id) if (m.metadata or {}).get("type") == "context_seed"]


# -- the CRUD routes ---------------------------------------------------------------


def test_projects_are_created_listed_patched_and_deleted(client):
    project = client.post("/api/projects", json={"title": "Grid", "description": "the app"}).json()
    assert project["title"] == "Grid" and project["id"].startswith("prj-")
    child = client.post("/api/projects", json={"title": "Chats", "parent_id": project["id"]}).json()

    assert client.get("/api/projects").json()["projects"][0]["id"] == project["id"]
    patched = client.patch(f"/api/projects/{project['id']}", json={"notes": "shared notes"})
    assert patched.status_code == 200 and patched.json()["notes"] == "shared notes"
    assert client.patch("/api/projects/prj-ghost", json={"title": "x"}).status_code == 404

    deleted = client.delete(f"/api/projects/{project['id']}").json()
    assert deleted["ok"] is True and deleted["moved_projects"] == 1
    assert [item["id"] for item in client.get("/api/projects").json()["projects"]] == [child["id"]]
    assert client.delete("/api/projects/prj-ghost").status_code == 404


def test_a_chat_moves_to_a_project_and_is_detached(client):
    first = client.post("/api/projects", json={"title": "one"}).json()
    second = client.post("/api/projects", json={"title": "two"}).json()
    context_id = client.post("/api/chat/conversations", json={"project_id": first["id"]}).json()["id"]

    moved = client.put(f"/api/chat/conversations/{context_id}/project", json={"project_id": second["id"]})
    assert moved.status_code == 200 and moved.json()["project_id"] == second["id"]
    assert client.put(f"/api/chat/conversations/{context_id}/project", json={"project_id": None}).json()["project_id"] is None
    assert client.put("/api/chat/conversations/ctx-ghost/project", json={"project_id": first["id"]}).status_code == 404


# -- seeding ----------------------------------------------------------------------


def test_create_conversation_seeds_a_project_with_notes(client, space):
    project = client.post("/api/projects", json={"title": "Grid"}).json()
    client.patch(f"/api/projects/{project['id']}", json={"notes": "Always run the tests."})

    seeded_id = client.post("/api/chat/conversations", json={"project_id": project["id"]}).json()["id"]
    seeds = _seeds(space, seeded_id)
    assert len(seeds) == 1 and seeds[0].role == "user"
    assert "Always run the tests." in seeds[0].content
    assert seeds[0].metadata["source_id"] == f"project:{project['id']}"

    # A project without notes, and no project at all, seed nothing.
    quiet = client.post("/api/projects", json={"title": "quiet"}).json()
    assert _seeds(space, client.post("/api/chat/conversations", json={"project_id": quiet["id"]}).json()["id"]) == []
    assert _seeds(space, client.post("/api/chat/conversations", json={}).json()["id"]) == []


def test_continue_inherits_the_project_and_seeds_the_plan(client, space):
    project = client.post("/api/projects", json={"title": "Grid"}).json()
    source = client.post("/api/chat/conversations", json={"project_id": project["id"]}).json()["id"]
    manager = space.context_manager()
    manager.update_context_metadata(source, {"title": "Planning"})
    manager.append_message_to(source, "assistant", "Here is the plan.", **_plan_message())

    continued = client.post(f"/api/chat/conversations/{source}/continue", json={"strategy": "plan"})
    assert continued.status_code == 201
    body = continued.json()
    assert body["project_id"] == project["id"] and body["continued_from"] == source
    metadata = manager.get_context_metadata(body["id"])
    assert metadata["project_id"] == project["id"] and metadata["continued_from"] == source

    seeds = _seeds(space, body["id"])
    assert len(seeds) == 1 and seeds[0].role == "user"
    assert "Do it" in seeds[0].content and "Planning" in seeds[0].content
    assert "Here is the plan." in seeds[0].content
    assert seeds[0].metadata["type"] == "context_seed" and seeds[0].metadata["source_id"] == source


def test_continue_tail_seeds_the_recent_messages(client, space):
    source = client.post("/api/chat/conversations", json={}).json()["id"]
    manager = space.context_manager()
    manager.update_context_metadata(source, {"title": "Chat"})
    manager.append_message_to(source, "user", "first question")
    manager.append_message_to(source, "assistant", "first answer")
    manager.append_message_to(source, "user", "second question")

    new_id = client.post(f"/api/chat/conversations/{source}/continue", json={"strategy": "tail"}).json()["id"]
    assert new_id != source
    # No project on the source: the continuation inherits none.
    assert space.context_manager().get_context_metadata(new_id).get("project_id") is None
    seed = _seeds(space, new_id)[0].content
    assert "second question" in seed and "first answer" in seed


def test_continue_defaults_to_the_plan_strategy(client, space):
    source = client.post("/api/chat/conversations", json={}).json()["id"]
    space.context_manager().append_message_to(source, "user", "hi", {"type": "user_input"})
    body = client.post(f"/api/chat/conversations/{source}/continue", json={}).json()
    assert body["strategy"] == "plan"


def test_continue_on_an_unknown_chat_is_404(client):
    assert client.post("/api/chat/conversations/ctx-ghost/continue", json={}).status_code == 404


# -- the plan ---------------------------------------------------------------------


def test_the_projects_plan_merges_the_chats_tasks_and_prioritises_bd(client, space):
    project = client.post("/api/projects", json={"title": "Grid"}).json()
    first = client.post("/api/chat/conversations", json={"project_id": project["id"]}).json()["id"]
    second = client.post("/api/chat/conversations", json={"project_id": project["id"]}).json()["id"]
    manager = space.context_manager()
    manager.update_context_metadata(first, {"title": "One"})
    manager.update_context_metadata(second, {"title": "Two"})
    manager.append_message_to(first, "assistant", "planned", **_plan_message())
    manager.append_message_to(second, "assistant", "planned again", **_plan_message())

    with patch("web_chat.projects_api._bd_for", new=AsyncMock(return_value={"success": True, "data": [ISSUE]})):
        plan = client.get(f"/api/projects/{project['id']}/plan").json()

    assert plan["chats"] == {first: "One", second: "Two"}
    assert plan["total"] == 1
    task = plan["tasks"][0]
    # bd's status and fields win over the trace's open task.
    assert task["id"] == "grid-a1" and task["source"] == "bd" and task["state"] == "closed"
    assert sorted(task["chat_ids"]) == sorted([first, second])
    assert plan["counts"]["closed"] == 1


def test_the_projects_plan_falls_back_to_trace_when_bd_is_missing(client, space):
    project = client.post("/api/projects", json={"title": "Grid"}).json()
    chat = client.post("/api/chat/conversations", json={"project_id": project["id"]}).json()["id"]
    space.context_manager().append_message_to(chat, "assistant", "planned", **_plan_message())

    async def unavailable(_space, _args):
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="The tracker is unavailable")

    with patch("web_chat.projects_api._bd_for", new=unavailable):
        plan = client.get(f"/api/projects/{project['id']}/plan").json()

    task = plan["tasks"][0]
    assert task["source"] == "trace" and task["state"] == "ready" and task["chat_ids"] == [chat]


def test_the_projects_plan_of_an_unknown_project_is_404(client):
    assert client.get("/api/projects/prj-ghost/plan").status_code == 404


def test_a_conversations_plan_is_its_newest_snapshot(client, space):
    context_id = client.post("/api/chat/conversations", json={}).json()["id"]
    manager = space.context_manager()
    assert client.get(f"/api/chat/conversations/{context_id}/plan").json()["total"] == 0
    manager.append_message_to(context_id, "assistant", "planned", **_plan_message())

    plan = client.get(f"/api/chat/conversations/{context_id}/plan").json()
    assert plan["total"] == 1 and plan["tasks"][0]["id"] == "grid-a1" and plan["counts"]["ready"] == 1
    assert client.get("/api/chat/conversations/ctx-ghost/plan").status_code == 404
