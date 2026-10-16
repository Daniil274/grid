"""The memory API: overview, filters, search, writes, staleness, sync and isolation."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.memory import MemoryConfig, MemoryManager
from schemas import MemoryConfig as MemorySettings
from schemas.schemas import ContextMessage
from web_chat.identity import User
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool

SAME_SITE = {"Origin": "http://testserver"}


class FakeContexts:
    def __init__(self):
        self.chats = {}

    def add(self, context_id, messages, **metadata):
        self.chats[context_id] = {"messages": messages, "metadata": metadata}

    def conversation_view(self, context_id):
        chat = self.chats.get(context_id)
        return None if chat is None else {"id": context_id, "messages": list(chat["messages"]), "metadata": chat["metadata"]}

    def get_context_metadata(self, context_id):
        chat = self.chats.get(context_id)
        return dict(chat["metadata"]) if chat else {}


class FakeConfig:
    def __init__(self, root, settings):
        self.root, self.settings = root, settings

    def get_memory_config(self):
        return self.settings

    def get_working_directory(self):
        return str(self.root)


class FakeFactory:
    def __init__(self, config, tenant):
        self.config, self._memory_tenant, self._manager = config, tenant, None

    def get_memory_manager(self):
        if not self.config.settings.enabled:
            return None
        return self.get_workspace_memory_manager()

    def get_workspace_memory_manager(self):
        if self._manager is None:
            root = Path(self.config.root)
            self._manager = MemoryManager.from_config(MemoryConfig(
                workspace_root=root, db_path=root / self.config.settings.storage_path, tenant_id=self._memory_tenant))
        return self._manager


class FakeRegistry:
    def __init__(self, root, tenant, settings):
        self.cfg = FakeConfig(root, settings)
        self.fac = FakeFactory(self.cfg, tenant)

    def keys(self):
        return ["main"]

    def default_key(self):
        return "main"

    def config(self, key):
        return self.cfg

    def factory(self, key):
        return self.fac


class FakeSpace:
    idle = True

    def __init__(self, root, user_id, enabled=False):
        self.user_id = user_id
        self.workspace_path = root
        self.registry = FakeRegistry(root, user_id, MemorySettings(enabled=enabled))
        self.contexts = FakeContexts()

    async def close(self):
        return None

    def schedule_warmup(self):
        return None

    def context_manager(self):
        return self.contexts

    async def registry_for(self, context_id):
        return self.registry


@pytest.fixture
def spaces(tmp_path):
    made = {}

    def build(user_id):
        root = tmp_path / user_id
        root.mkdir(exist_ok=True)
        made[user_id] = FakeSpace(root, user_id, enabled=user_id == "enabled")
        return made[user_id]

    build.made = made
    return build


@pytest.fixture
def client(spaces):
    async def identify(connection):
        name = connection.headers.get("X-Test-User", "alice")
        return User(id=name, username=name, role="user")

    deployment = SimpleNamespace(voice_source=lambda: ("voice.yaml", None), voice_config_dict=dict)
    server = WebChatServer(deployment, SpacePool(spaces), identify=identify, warm_user=None)
    with TestClient(server.app, headers=SAME_SITE) as test_client:
        yield test_client


def add(client, content, **fields):
    response = client.post("/api/memory/records", json={"content": content, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def test_overview_of_an_empty_memory_works_while_disabled_in_config(client, spaces):
    data = client.get("/api/memory").json()

    assert data["records"] == [] and data["count"] == 0
    assert data["config"]["enabled_in_config"] is False
    assert data["config"]["backend"] == "sqlite"
    assert data["overview"]["counts"] == {"L0": 0, "L1": 0, "L2": 0, "L3": 0}
    assert data["overview"]["healthy"] is True
    assert data["overview"]["auto_inject_tokens"] == 1000
    assert (spaces.made["alice"].workspace_path / ".grid/memory.sqlite").exists()


def test_enabled_in_config_uses_the_systems_own_memory(client, spaces):
    head = {"X-Test-User": "enabled"}
    add_headers = client.post("/api/memory/records", json={"content": "Shared fact"}, headers=head)
    data = client.get("/api/memory", headers=head).json()

    assert add_headers.status_code == 201
    assert data["config"]["enabled_in_config"] is True
    manager = spaces.made["enabled"].registry.fac.get_memory_manager()
    assert [r.content for r in manager.list_records()] == ["Shared fact"]


def test_create_returns_the_record_and_overview_counts_layers(client):
    fact = add(client, "Use tabs", title="Style", tags=["code"], category="preference", confidence=0.9)
    scenario = add(client, "Release flow", layer="L2", category="workflow")
    profile = add(client, "Prefers Russian", layer="L3", scope="user", category="preference")

    assert fact["layer"] == "L1" and fact["tags"] == ["code"] and fact["title"] == "Style"
    assert fact["confidence"] == 0.9 and fact["stale_status"] == "unverified"
    data = client.get("/api/memory").json()
    assert data["overview"]["counts"] == {"L0": 0, "L1": 1, "L2": 1, "L3": 1}
    assert data["overview"]["total"] == 3
    assert data["overview"]["total_estimated_tokens"] > 0
    assert "Prefers Russian" in data["overview"]["preview_prompt_section"]
    assert {r["id"] for r in data["records"]} == {fact["id"], scenario["id"], profile["id"]}


def test_filters_by_layer_scope_category_and_l0(client):
    add(client, "fact one", category="bug")
    add(client, "scenario one", layer="L2", category="workflow")
    add(client, "profile one", layer="L3", scope="user", category="preference")
    add(client, "raw transcript", layer="L0")

    def contents(**params):
        return {r["content"] for r in client.get("/api/memory", params=params).json()["records"]}

    assert contents(layer="L1") == {"fact one"}
    assert contents(layer="L2") == {"scenario one"}
    assert contents(layer="L3") == {"profile one"}
    assert contents(layer="L0") == {"raw transcript"}
    assert contents(scope="user") == {"profile one"}
    assert contents(category="workflow") == {"scenario one"}
    assert contents(layer="L1", category="workflow") == set()
    assert len(contents()) == 4
    assert len(contents(limit=2)) == 2


def test_records_are_newest_first_without_a_query(client):
    for name in ("first", "second", "third"):
        add(client, name)

    assert [r["content"] for r in client.get("/api/memory").json()["records"]] == ["third", "second", "first"]


def test_hybrid_search_finds_relevant_records(client):
    add(client, "The deploy pipeline runs on Friday")
    add(client, "Prefer small pull requests")

    found = client.get("/api/memory", params={"query": "deploy pipeline"}).json()["records"]

    assert found and found[0]["content"].startswith("The deploy pipeline")
    assert all("pull requests" not in r["content"] for r in found)


def test_update_edits_in_place_and_moves_between_layers(client):
    record = add(client, "Old text", tags=["a"])

    edited = client.put(f"/api/memory/records/{record['id']}", json={"content": "New text", "title": "T", "tags": ["b", "c"]})
    moved = client.patch(f"/api/memory/records/{record['id']}", json={"layer": "L2", "category": "workflow"})

    assert edited.status_code == 200
    assert edited.json()["id"] == record["id"] and edited.json()["content"] == "New text"
    assert edited.json()["tags"] == ["b", "c"] and edited.json()["title"] == "T"
    assert moved.json()["layer"] == "L2" and moved.json()["content"] == "New text"
    data = client.get("/api/memory").json()
    assert data["overview"]["counts"]["L1"] == 0 and data["overview"]["counts"]["L2"] == 1
    assert len(data["records"]) == 1


def test_update_validation_and_unknown_record(client):
    record = add(client, "Fact")

    assert client.put("/api/memory/records/missing", json={"content": "x"}).status_code == 404
    assert client.put(f"/api/memory/records/{record['id']}", json={"layer": "L9"}).status_code == 422
    assert client.put(f"/api/memory/records/{record['id']}", json={"content": " "}).status_code == 400
    assert client.post("/api/memory/records", json={"content": ""}).status_code == 422


def test_delete_forgets_the_record(client):
    record = add(client, "Temporary")

    assert client.delete(f"/api/memory/records/{record['id']}").json() == {"ok": True, "id": record["id"]}
    assert client.delete(f"/api/memory/records/{record['id']}").status_code == 404
    assert client.get("/api/memory").json()["records"] == []


def test_a_file_changed_after_the_record_was_made_is_stale(client, spaces):
    client.get("/api/memory")
    root = spaces.made["alice"].workspace_path
    (root / "notes.txt").write_text("version one")
    record = add(client, "Notes say one", file_path="notes.txt")
    assert record["stale_status"] == "fresh" and record["file_path"] == "notes.txt"

    (root / "notes.txt").write_text("version two!")
    os.utime(root / "notes.txt", ns=(1, 1))

    data = client.get("/api/memory").json()
    assert data["records"][0]["stale_status"] == "stale"
    assert data["overview"]["stale_count"] == 1
    assert [r["id"] for r in client.get("/api/memory", params={"stale_only": True}).json()["records"]] == [record["id"]]
    assert client.get("/api/memory", params={"include_stale": False}).json()["records"] == []
    # Editing the record takes a new snapshot of the file.
    fixed = client.put(f"/api/memory/records/{record['id']}", json={"content": "Notes say two"}).json()
    assert fixed["stale_status"] == "fresh"


def test_a_file_path_outside_the_workspace_is_refused(client):
    response = client.post("/api/memory/records", json={"content": "x", "file_path": "../../etc/passwd"})

    assert response.status_code == 400


def test_sync_distils_marked_statements_of_the_conversation(client, spaces):
    client.get("/api/memory")
    chat = spaces.made["alice"].contexts
    now = "2026-01-01T00:00:00Z"
    chat.add("chat-1", [
        ContextMessage(role="user", content="decision: use SQLite for storage", timestamp=now),
        ContextMessage(role="assistant", content="Sure.\n- bug: the cache never expires", timestamp=now),
        ContextMessage(role="user", content="just chatting", timestamp=now),
    ])

    result = client.post("/api/memory/sync", json={"context_id": "chat-1"}).json()

    facts = [r for r in result["records"] if r["layer"] != "L0"]
    assert result["scanned"] == 3 and {r["category"] for r in facts} == {"decision", "bug"}
    assert not any(r["layer"] == "L0" for r in result["records"])  # no raw copies by default
    assert all(r["source_ref"] == "chat:chat-1" for r in result["records"])
    count = client.get("/api/memory", params={"context_id": "chat-1"}).json()["count"]
    assert count == result["stored"]
    again = client.post("/api/memory/sync", json={"context_id": "chat-1"}).json()
    assert again["stored"] == result["stored"] and client.get("/api/memory").json()["count"] == count


def test_unknown_conversation_is_a_404(client):
    assert client.get("/api/memory", params={"context_id": "nope"}).status_code == 404
    assert client.post("/api/memory/sync", json={"context_id": "nope"}).status_code == 404
    assert client.post("/api/memory/records", json={"content": "x", "context_id": "nope"}).status_code == 404


def test_one_user_never_sees_or_changes_anothers_memory(client, spaces):
    alice, bob = {"X-Test-User": "alice"}, {"X-Test-User": "bob"}
    secret = client.post("/api/memory/records", json={"content": "alice secret"}, headers=alice).json()

    assert client.get("/api/memory", headers=bob).json()["records"] == []
    assert client.put(f"/api/memory/records/{secret['id']}", json={"content": "pwned"}, headers=bob).status_code == 404
    assert client.delete(f"/api/memory/records/{secret['id']}", headers=bob).status_code == 404
    assert [r["content"] for r in client.get("/api/memory", headers=alice).json()["records"]] == ["alice secret"]
    spaces.made["alice"].contexts.add("alice-chat", [])
    assert client.get("/api/memory", params={"context_id": "alice-chat"}, headers=bob).status_code == 404
    assert client.post("/api/memory/sync", json={"context_id": "alice-chat"}, headers=bob).status_code == 404


def test_memory_routes_need_the_same_site_guard(client):
    assert client.post("/api/memory/records", json={"content": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_chat_page_carries_the_memory_button_and_drawer(client):
    page = client.get("/")

    assert page.status_code == 200
    html = page.text
    for marker in ('id="open-memory"', 'id="memory"', 'id="memory-sync"', 'id="memory-new"',
                   'id="memory-tabs"', 'id="memory-search"', 'id="memory-stale"', 'id="memory-scope"',
                   'id="memory-preview-toggle"', 'id="memory-list"', 'id="memory-editor"', "styles/memory.css"):
        assert marker in html, marker


def test_memory_ui_assets_are_served(client):
    script = client.get("/static/js/ui/memory-dialog.js")
    styles = client.get("/static/styles/memory.css")
    api_client = client.get("/static/js/net/api.js")

    assert script.status_code == 200 and "class MemoryDrawer" in script.text
    assert styles.status_code == 200 and ".memCard" in styles.text
    assert api_client.status_code == 200
    for name in ("memoryOverview", "createMemoryRecord", "updateMemoryRecord", "deleteMemoryRecord", "syncMemory"):
        assert name in api_client.text


RU_CHAT = [
    ("user", "Привет! Всегда используй pytest для тестов и отвечай кратко на русском. Нужно починить кэш в cache.py"),
    ("assistant", "Разобрался. Причина в том, что `cache.py` не сбрасывал TTL при записи.\n\n## Итог\n"
                  "- Исправил `cache.py`: TTL теперь обновляется при каждой записи\n"
                  "- Добавил тест tests/test_cache.py\n\nМы решили хранить кэш в SQLite через core/storage.py, "
                  "а не в памяти процесса, чтобы он переживал перезапуск сервера."),
]
EN_CHAT = [
    ("user", "Let's use FastAPI for the API. I prefer concise answers and you must never touch legacy/db.py."),
    ("assistant", "Done. I implemented the endpoint in api/server.py and fixed the race condition in worker.py.\n"
                  "Summary:\n- Endpoint lives in api/server.py\n- Root cause: missing lock in worker.py\n"
                  "The handler validates every request before it is queued for the worker pool."),
]


def fill(spaces, chat_id, dialogue):
    now = "2026-01-01T00:00:00Z"
    spaces.made["alice"].contexts.add(
        chat_id, [ContextMessage(role=role, content=text, timestamp=now) for role, text in dialogue])


def test_sync_distils_natural_russian_and_english_without_prefixes(client, spaces):
    client.get("/api/memory")
    fill(spaces, "ru", RU_CHAT)
    fill(spaces, "en", EN_CHAT)

    ru = client.post("/api/memory/sync", json={"context_id": "ru"}).json()
    en = client.post("/api/memory/sync", json={"context_id": "en"}).json()

    assert ru["stored"] >= 4 and en["stored"] >= 4
    by_layer = {r["layer"] for r in ru["records"]} | {r["layer"] for r in en["records"]}
    assert by_layer == {"L1", "L2", "L3"}
    shown = client.get("/api/memory", params={"limit": 1000}).json()
    texts = " | ".join(r["content"] for r in shown["records"])
    assert "Всегда используй pytest" in texts and "prefer concise answers" in texts
    assert "TTL" in texts and "worker.py" in texts and "SQLite" in texts
    prefs = [r for r in shown["records"] if r["layer"] == "L3"]
    assert prefs and all(r["scope"] == "user" for r in prefs)
    assert shown["overview"]["counts"]["L0"] == 0  # no raw copies of exchanges by default


def test_agent_and_panel_share_one_database_and_namespace(client, spaces):
    """A fact stored through the factory (as memory_remember does) is in GET /api/memory,
    and a record written through the API is visible to the factory's manager."""
    client.get("/api/memory")
    space = spaces.made["alice"]
    factory = space.registry.fac
    manager = factory.get_workspace_memory_manager()
    from core.memory import MemoryRecord
    stored = manager.remember(MemoryRecord("Agent says: deploy via make release", category="workflow"))

    records = client.get("/api/memory").json()["records"]
    assert [r["id"] for r in records] == [stored.id]
    created = add(client, "panel note")
    assert manager.get(created["id"]) is not None
    assert (space.workspace_path / ".grid" / "memory.sqlite").exists()
    assert space.registry.fac.get_workspace_memory_manager() is manager
