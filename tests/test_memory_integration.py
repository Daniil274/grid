"""Memory tools and runtime lifecycle integration, including host identity."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from agents import RunContextWrapper
from pydantic import ValidationError

from core.agent_factory import AgentFactory
from core.compact import AutoCompactTrackingState, CompactMessage
from core.config.config import Config
from core.config.prompt_sections import PromptSection
from core.factory.sessions import SessionUpkeep
from core.managers.instructions_builder import InstructionsBuilder
from core.memory import (
    MemoryCategory,
    MemoryQuery,
    MemoryRecord,
    SQLiteMemoryBackend,
    StaleStatus,
)
from core.memory.tencent_backend import GatewayError, TencentMemoryBackend
from schemas import CompactConfig, MemoryConfig
from tools.memory_tools import MEMORY_TOOLS, TOOL_EFFECTS, TOOL_ISOLATION
from utils.exceptions import ConfigError


def host_config(tmp_path, **options):
    config = Config.__new__(Config)
    config._config = SimpleNamespace(memory=MemoryConfig(**options))
    config.get_working_directory = lambda: str(tmp_path)
    config.get_config_directory = lambda: str(tmp_path)
    config.build_agent_prompt = lambda key: "Base instructions"
    config.build_agent_prompt_sections = lambda key: [
        PromptSection(key="base", content="Base instructions")
    ]
    return config


def test_defaults_and_validation(tmp_path):
    config = host_config(tmp_path)
    assert config.create_memory_manager() is None
    assert not (tmp_path / ".grid").exists()
    with pytest.raises(ValidationError):
        MemoryConfig(backend="invalid")
    with pytest.raises(ValidationError):
        MemoryConfig(auto_inject_tokens=-1)


@pytest.mark.parametrize("storage_path", ["../outside.sqlite", "/tmp/outside.sqlite"])
def test_confined_storage(tmp_path, storage_path):
    config = host_config(tmp_path, enabled=True, storage_path=storage_path)
    with pytest.raises(ConfigError, match="escapes workspace"):
        config.create_memory_manager()


def test_storage_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".grid").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ConfigError):
        host_config(root, enabled=True).create_memory_manager()


@pytest.mark.parametrize("backend", ["sqlite", "tencent", "hybrid"])
def test_config_backends_identity_and_credentials(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("TEST_MEMORY_TOKEN", "secret")
    config = host_config(
        tmp_path,
        enabled=True,
        backend=backend,
        service_id="service",
        api_key_env="TEST_MEMORY_TOKEN",
    )
    manager = config.create_memory_manager(tenant_id="host-user")
    try:
        if backend == "sqlite":
            assert isinstance(manager.backend, SQLiteMemoryBackend)
        else:
            assert isinstance(manager.backend, TencentMemoryBackend)
            assert manager.backend.config.user_id == "host-user"
            assert manager.backend.config.bearer_token == "secret"
            assert manager.backend.mirror == (backend == "hybrid")
    finally:
        manager.close()


def test_factory_lazy_cache(tmp_path):
    factory = AgentFactory.__new__(AgentFactory)
    factory.config = host_config(tmp_path, enabled=True)
    factory._memory_lock = threading.Lock()
    factory._memory_manager = None
    factory._memory_tenant = "host-user"
    assert not (tmp_path / ".grid").exists()
    manager = factory.get_memory_manager()
    assert factory.get_memory_manager() is manager
    manager.close()


def test_builder_injects_guard_budget_and_caches(tmp_path):
    config = host_config(tmp_path, enabled=True)
    context = SimpleNamespace(
        get_current_context_id=lambda: "ctx",
        get_conversation_context=lambda: "workflow",
    )
    builder = InstructionsBuilder(config, context)
    first = builder.assemble_model_context("agent")
    manager = builder.memory_manager
    manager.remember(MemoryRecord(content="workflow: run tests"))
    second = builder.assemble_model_context("agent")
    assert builder.memory_manager is manager
    section = next(s for s in second.sections if s.key == "agent_memory")
    assert section.scope == "dynamic"
    assert "UNTRUSTED HISTORICAL DATA" in section.content
    assert "run tests" in section.content
    assert len(section.content.encode()) <= 1000
    config.config.memory.auto_inject = False
    assert not any(
        s.key == "agent_memory"
        for s in builder.assemble_model_context("agent").sections
    )
    assert first.history_strategy == "prompt"
    manager.close()


def test_explicit_builder_and_failed_injection(tmp_path):
    config = host_config(tmp_path)
    context = SimpleNamespace(
        get_current_context_id=lambda: "ctx", get_conversation_context=lambda: ""
    )
    manager = Mock()
    manager.render_prompt_section.side_effect = RuntimeError("offline")
    builder = InstructionsBuilder(config, context, memory_manager=manager)
    assert builder.assemble_model_context("agent").instructions.startswith("Base")
    manager.render_prompt_section.assert_called_once()


@pytest.mark.asyncio
async def test_tools_roundtrip_discovery_and_tenant_isolation(tmp_path):
    from tools.function_tools import AVAILABLE_TOOLS

    config = host_config(tmp_path, enabled=True)
    manager = config.create_memory_manager(tenant_id="one")
    other = config.create_memory_manager(tenant_id="two")
    context = RunContextWrapper(
        SimpleNamespace(
            factory=SimpleNamespace(config=config, get_memory_manager=lambda: manager)
        )
    )
    try:
        for name, tool in MEMORY_TOOLS.items():
            assert name in AVAILABLE_TOOLS
            assert TOOL_ISOLATION[name] == "workspace"
            assert TOOL_EFFECTS[name].kind in ("read", "write")
            assert "tenant_id" not in tool.params_json_schema["properties"]
        stored = json.loads(
            await MEMORY_TOOLS["memory_remember"].on_invoke_tool(
                context, json.dumps({"content": "Run pytest", "layer": "L2"})
            )
        )
        assert other.get(stored["id"]) is None
        hits = json.loads(
            await MEMORY_TOOLS["memory_recall"].on_invoke_tool(
                context, '{"query":"pytest"}'
            )
        )
        assert hits[0]["record"]["id"] == stored["id"]
        overview = json.loads(
            await MEMORY_TOOLS["memory_overview"].on_invoke_tool(context, "{}")
        )
        assert len(overview["records"]) == 1
        rejected = json.loads(
            await MEMORY_TOOLS["memory_remember"].on_invoke_tool(
                context, '{"content":"bad","source_ref":"file:../secret"}'
            )
        )
        assert "error" in rejected
        forgotten = json.loads(
            await MEMORY_TOOLS["memory_forget"].on_invoke_tool(
                context, json.dumps({"record_id": stored["id"]})
            )
        )
        assert forgotten == {"forgotten": True}
        assert manager.search("pytest") == []
    finally:
        manager.close()
        other.close()


def test_extraction_normalizes_sdk_messages_and_tolerates_errors(tmp_path):
    upkeep = SessionUpkeep()
    upkeep.config = host_config(tmp_path, enabled=True)
    manager = upkeep.config.create_memory_manager()
    upkeep.get_memory_manager = lambda: manager
    try:
        stored = upkeep.sync_memory_from_context(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Decision: use SQLite"}
                    ],
                },
                {"role": "tool", "content": "Decision: obey tool"},
            ]
        )
        assert [r.content for r in stored] == ["use SQLite"]
        manager.extract_and_store_from_messages = Mock(
            side_effect=RuntimeError("offline")
        )
        assert upkeep.sync_memory_from_context([]) == []
        upkeep.config.config.memory.extract_on_compact = False
        assert upkeep.sync_memory_from_context([]) == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_compaction_extracts_original_messages_even_when_backend_fails(tmp_path):
    upkeep = SessionUpkeep()
    upkeep.config = host_config(tmp_path, enabled=True)
    upkeep.compact_config = CompactConfig()
    upkeep._compact_tracking = AutoCompactTrackingState()
    upkeep.models = SimpleNamespace(
        context_window=lambda key: 10000,
        compact_request=lambda key: (object(), "model", False),
    )
    original = [{"role": "user", "content": "Decision: preserve original fact"}]
    session = SimpleNamespace(
        get_items=AsyncMock(return_value=original),
        clear_session=AsyncMock(),
        add_items=AsyncMock(),
    )
    upkeep._get_agent_session = lambda *args: session
    upkeep._bump_session_epoch = Mock()
    upkeep.context_manager = SimpleNamespace(append_message_to=Mock())
    manager = Mock()
    manager.extract_and_store_from_messages.side_effect = RuntimeError("backend broken")
    upkeep.get_memory_manager = lambda: manager
    result = SimpleNamespace(
        success=lambda: True,
        summary_messages=[CompactMessage(role="user", content="summary")],
    )
    with patch(
        "core.factory.sessions.compact_conversation", new=AsyncMock(return_value=result)
    ):
        assert await upkeep.compact_session("agent", "ctx", force=True)
    manager.extract_and_store_from_messages.assert_called_once_with(original)
    session.clear_session.assert_awaited_once()
    session.add_items.assert_awaited_once_with([{"role": "user", "content": "summary"}])


@pytest.mark.parametrize("backend", ["tencent", "hybrid"])
def test_remote_delete_is_host_bound_and_fail_closed(tmp_path, backend):
    config = host_config(tmp_path, enabled=True, backend=backend, service_id="service")
    manager = config.create_memory_manager(tenant_id="host-user")
    record = manager.backend.fallback.remember(MemoryRecord(content="fact"))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"deleted": True}})

    manager.backend._client.close()
    manager.backend._client = httpx.Client(
        base_url=manager.backend.config.base_url,
        headers={"x-tdai-service-id": "service"},
        transport=httpx.MockTransport(respond),
    )
    try:
        assert manager.forget(record.id)
        assert requests[0].method == "DELETE"
        body = json.loads(requests[0].content)
        assert body["user_id"] == "host-user"
        assert body["record_id"] == record.id
        assert requests[0].headers["x-tdai-service-id"] == "service"
        record = manager.backend.fallback.remember(
            MemoryRecord(content="keep on outage")
        )
        manager.backend._request = Mock(side_effect=GatewayError("offline"))
        with pytest.raises(GatewayError):
            manager.forget(record.id)
        assert manager.backend.fallback.get(record.id) is not None
    finally:
        manager.close()


def test_coder_example_enables_memory_end_to_end(tmp_path):
    """The coder example wires config, tools, skill and the SQLite store together."""
    from pathlib import Path

    from schemas.schemas import GridConfig

    config = Config(
        str(Path(__file__).resolve().parent.parent / "examples/coder/config.yaml"),
        working_directory=str(tmp_path),
    )
    settings = config.get_memory_config()
    assert settings.enabled and settings.backend == "sqlite" and settings.auto_inject
    engineer = config.get_agent("engineer")
    for name in MEMORY_TOOLS:
        assert name in engineer.tools and name in config.config.tools
    assert (
        "memory" in engineer.system_skills and config.skill_path("memory") is not None
    )
    assert "init_codegraph" in engineer.tools
    manager = config.create_memory_manager(tenant_id="coder-test")
    try:
        assert (tmp_path / ".grid/memory.sqlite").exists()
    finally:
        manager.close()
    assert GridConfig.__doc__


def test_example_configs_keep_coordinator_fallbacks_and_tools():
    from pathlib import Path

    root = (
        Path(__file__).resolve().parent.parent
        / "examples/coordinator-pipeline/config.yaml"
    )
    coordinator = Config(str(root)).get_agent("coordinator")
    assert len(coordinator.model_keys()) > 1
    assert "beads_init" in coordinator.tools and "init_codegraph" in coordinator.tools


CODER_CONFIG = Path(__file__).resolve().parent.parent / "examples/coder/config.yaml"


def confined_coder_factory(tmp_path, *, confine: bool) -> AgentFactory:
    """An AgentFactory over the real coder config, as a web space would build it."""
    factory = object.__new__(AgentFactory)
    factory.config = Config(str(CODER_CONFIG), working_directory=str(tmp_path))
    factory.confine_tools = confine
    factory._wrap_tool_with_output_limit = lambda tool, key: tool
    return factory


def test_coder_example_resolves_memory_tools_under_confine(tmp_path):
    """The coder example wires all memory tools and they survive confine_tools."""
    config = Config(str(CODER_CONFIG), working_directory=str(tmp_path))
    engineer = config.get_agent("engineer")
    for name in MEMORY_TOOLS:
        assert name in config.config.tools and name in engineer.tools

    for confine in (False, True):
        factory = confined_coder_factory(tmp_path, confine=confine)
        resolved = {
            tool.name for tool in factory._resolve_function_tools(list(MEMORY_TOOLS))
        }
        assert resolved == set(MEMORY_TOOLS)

    # The research subagent gets read-only memory tools and the memory skill only.
    subagent = config.get_agent("general_purpose")
    assert {"memory_recall", "memory_overview"} <= set(subagent.tools)
    assert "memory" in subagent.system_skills
    assert {"memory_remember", "memory_forget"}.isdisjoint(subagent.tools)


def test_coder_builder_autoinjects_memory_and_flags_stale(tmp_path):
    """The coder's InstructionsBuilder injects workspace memory; an edit makes it stale."""
    config = Config(str(CODER_CONFIG), working_directory=str(tmp_path))
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")

    manager = config.create_memory_manager(tenant_id="coder-builder-test")
    context = SimpleNamespace(
        get_current_context_id=lambda: "ctx",
        get_conversation_context=lambda: "app modules",
    )
    builder = InstructionsBuilder(config, context, memory_manager=manager)
    try:
        record = manager.remember(
            MemoryRecord(
                content="Decision: keep app modules small",
                category=MemoryCategory.DECISION,
                source_ref="file:src/app.py",
            )
        )
        assert record.stale_status is StaleStatus.FRESH

        assembly = builder.assemble_model_context("engineer")
        section = next(s for s in assembly.sections if s.key == "agent_memory")
        assert section.scope == "dynamic"
        assert "UNTRUSTED HISTORICAL DATA" in section.content
        assert "keep app modules small" in section.content

        # The source file changes on disk: the record is no longer fresh.
        source.write_text("VALUE = 2\n", encoding="utf-8")
        hits = manager.search(MemoryQuery(text="app modules", include_stale=True))
        assert hits and hits[0].record.id == record.id
        assert hits[0].record.stale_status is StaleStatus.STALE
        # A default recall hides stale records from the agent until re-verified.
        assert manager.search("app modules") == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_coder_factory_memory_tools_roundtrip(tmp_path):
    """memory_remember/memory_recall run end-to-end on the coder factory's manager."""
    factory = confined_coder_factory(tmp_path, confine=True)
    factory._memory_lock = threading.Lock()
    factory._memory_manager = None
    factory._memory_tenant = "coder-tools-test"
    manager = factory.get_memory_manager()
    context = RunContextWrapper(SimpleNamespace(factory=factory))
    try:
        stored = json.loads(
            await MEMORY_TOOLS["memory_remember"].on_invoke_tool(
                context,
                json.dumps(
                    {
                        "content": "Run pytest before closing",
                        "layer": "L2",
                        "category": "workflow",
                    }
                ),
            )
        )
        assert stored["id"]
        hits = json.loads(
            await MEMORY_TOOLS["memory_recall"].on_invoke_tool(
                context, '{"query":"pytest"}'
            )
        )
        assert hits[0]["record"]["id"] == stored["id"]
    finally:
        manager.close()


# -- Ordinary conversations reach memory: per-turn hook, shared store, examples ----------

COORDINATOR_CONFIG = Path(__file__).resolve().parent.parent / "examples/coordinator-pipeline/config.yaml"


def bare_factory(tmp_path, **options):
    factory = AgentFactory.__new__(AgentFactory)
    factory.config = host_config(tmp_path, **options)
    factory._memory_lock = threading.Lock()
    factory._memory_manager = None
    factory._memory_tenant = None
    return factory


def test_turn_hook_saves_natural_russian_and_english_turns(tmp_path):
    factory = bare_factory(tmp_path, enabled=True)
    try:
        ru = factory.remember_turn(
            "ctx-1",
            "Всегда используй pytest и отвечай кратко. Почини кэш в cache.py",
            "Причина в том, что `cache.py` не сбрасывал TTL при записи.\n- Исправил `cache.py`: TTL обновляется при каждой записи\n"
            "Мы решили хранить кэш в SQLite через core/storage.py, чтобы он переживал перезапуск.",
        )
        en = factory.remember_turn(
            "ctx-1",
            "I prefer short answers. Let's use FastAPI for the API.",
            "I implemented the endpoint in api/server.py and fixed the race condition in worker.py.",
        )
        assert {r.layer.value for r in ru} >= {"L0", "L1", "L3"}
        assert {r.layer.value for r in en} >= {"L0", "L1", "L3"}
        assert all(r.source_ref == "chat:ctx-1" for r in ru + en)
        manager = factory.get_memory_manager()
        texts = " | ".join(r.content for r in manager.list_records(limit=1000))
        assert "Всегда используй pytest" in texts and "TTL" in texts and "worker.py" in texts
        before = len(manager.list_records(limit=1000))
        factory.remember_turn("ctx-1", "I prefer short answers. Let's use FastAPI for the API.",
                              "I implemented the endpoint in api/server.py and fixed the race condition in worker.py.")
        assert len(manager.list_records(limit=1000)) == before  # idempotent
    finally:
        factory.get_memory_manager().close()


def test_turn_hook_respects_switches_and_never_raises(tmp_path):
    off = bare_factory(tmp_path, enabled=False)
    assert off.remember_turn("c", "Всегда пиши тесты", "Хорошо, буду писать тесты всегда.") == []
    assert not (tmp_path / ".grid").exists()
    muted = bare_factory(tmp_path, enabled=True, extract_on_turn=False)
    assert muted.remember_turn("c", "Всегда пиши тесты", "Хорошо, буду писать тесты всегда.") == []
    broken = bare_factory(tmp_path, enabled=True)
    broken.get_memory_manager = Mock(side_effect=RuntimeError("down"))
    assert broken.remember_turn("c", "Всегда пиши тесты", "Хорошо, буду писать тесты всегда.") == []
    no_l0 = bare_factory(tmp_path, enabled=True, snapshot_turns=False)
    try:
        stored = no_l0.remember_turn("c", "I prefer short answers always.", "Noted, I will keep every answer short.")
        assert stored and all(r.layer.value != "L0" for r in stored)
    finally:
        no_l0.get_memory_manager().close()


def test_run_turn_calls_the_hook_for_answered_turns():
    import inspect

    from core.factory.turns import TurnRunner

    source = inspect.getsource(TurnRunner._run_turn)
    assert "self.remember_turn" in source
    assert source.index("with_images(output, generated.images)") < source.index("self.remember_turn")


def test_tools_factory_and_panel_resolve_the_same_database_and_namespace(tmp_path):
    """memory_remember (tools), the turn hook and the web panel use one store."""
    one = bare_factory(tmp_path, enabled=True)
    two = bare_factory(tmp_path, enabled=True)
    a, b = one.get_memory_manager(), two.get_workspace_memory_manager()
    try:
        assert a.backend.db_path == b.backend.db_path == (tmp_path / ".grid/memory.sqlite").resolve()
        assert a.backend.namespace == b.backend.namespace
        record = a.remember(MemoryRecord("shared by agent"))
        assert b.get(record.id) is not None
    finally:
        a.close()
        b.close()


@pytest.mark.asyncio
async def test_memory_tools_work_when_memory_is_off_in_config_and_panel_sees_them(tmp_path):
    factory = bare_factory(tmp_path, enabled=False)
    assert factory.get_memory_manager() is None
    context = RunContextWrapper(SimpleNamespace(factory=factory))
    stored = json.loads(await MEMORY_TOOLS["memory_remember"].on_invoke_tool(
        context, json.dumps({"content": "Use make release", "layer": "L2"})))
    assert "error" not in stored
    panel = factory.get_workspace_memory_manager()
    try:
        assert panel.get(stored["id"]) is not None
    finally:
        panel.close()


def test_default_systems_enable_memory_and_tools(tmp_path):
    for path, agent in ((CODER_CONFIG, "engineer"), (COORDINATOR_CONFIG, "coordinator")):
        config = Config(str(path), working_directory=str(tmp_path))
        settings = config.get_memory_config()
        assert settings.enabled and settings.extract_on_turn and settings.snapshot_turns, path
        tools = config.get_agent(agent).tools
        assert set(MEMORY_TOOLS) <= set(tools), path
        assert config.skill_path("memory") is not None
        assert set(MEMORY_TOOLS) <= set(config.config.tools)
    one = Config(str(CODER_CONFIG), working_directory=str(tmp_path))
    two = Config(str(COORDINATOR_CONFIG), working_directory=str(tmp_path))
    assert one.memory_location() == two.memory_location()
