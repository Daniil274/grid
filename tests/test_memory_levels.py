"""A chat's memory comes in levels: its own, its project's and its user's. Each is
a namespace of its own; recall reads them together, a turn's memories go where
the model files them, and the memory a user had before stays theirs."""

import json
from types import SimpleNamespace

import pytest

from core.memory import MemoryLayer, MemoryRecord
from core.memory.llm_extract import extraction_prompt, parse_extraction
from tests.test_memory_extraction import REQUEST, model_factory
from web_chat.memory_api import _Target, _sync_with_model


def leveled(factory, scopes):
    factory.memory_scopes = lambda context_id: scopes.get(context_id, {})
    return factory


ATLAS = {"chat": ("ctx-a", "Мост"), "project": ("prj-atlas", "Atlas")}


def test_each_level_is_a_namespace_of_its_own_in_the_same_file(tmp_path):
    factory, _, _ = model_factory(tmp_path, "{}", embeddings=False)
    leveled(factory, {"a": ATLAS, "b": {"chat": ("ctx-b", ""), "project": ("prj-atlas", "Atlas")}})
    levels = dict(factory.memory_levels("a"))
    assert list(levels) == ["chat", "project", "user"]
    levels["chat"].remember(MemoryRecord("Для этой задачи выбран вариант Б"))
    levels["project"].remember(MemoryRecord("Atlas собирается через make dist"))
    levels["user"].remember(MemoryRecord("Отвечать кратко", layer=MemoryLayer.L3))

    other = dict(factory.memory_levels("b"))
    # The project's memory is shared by its chats; a chat's own is not.
    assert [r.content for r in other["project"].live_records()] == ["Atlas собирается через make dist"]
    assert other["chat"].live_records() == []
    assert other["user"] is levels["user"]
    # A chat without levels has the user's memory only - what every chat had before.
    assert [level for level, _ in factory.memory_levels("legacy")] == ["user"]
    assert len({str(manager.backend.db_path) for manager in levels.values()}) == 1
    for manager in levels.values():
        manager.close()
    other["chat"].close()


@pytest.mark.asyncio
async def test_the_prompt_reads_every_level_and_says_whose_each_record_is(tmp_path):
    factory, _, _ = model_factory(tmp_path, "{}")
    leveled(factory, {"a": ATLAS})
    levels = dict(factory.memory_levels("a"))
    try:
        levels["user"].remember(MemoryRecord("Отвечать кратко, по-русски", layer=MemoryLayer.L3))
        levels["user"].remember(MemoryRecord("git push в github идёт через paramiko: ssh нет"))
        levels["project"].remember(MemoryRecord("В Atlas пуш на github только после make test"))
        levels["chat"].remember(MemoryRecord("Решили пушить ветку bridge в форк, не в main"))

        section = await factory.memory_prompt_section("a", "Запушь ветку на github")

        lines = [json.loads(line) for line in section.splitlines() if line.startswith("{")]
        whose = {line["content"]: line["level"] for line in lines}
        assert whose == {
            "Отвечать кратко, по-русски": "user",
            "Решили пушить ветку bridge в форк, не в main": "chat",
            "В Atlas пуш на github только после make test": "project",
            "git push в github идёт через paramiko: ssh нет": "user",
        }
        # A chat with the user's memory only sees no levels at all, as before.
        assert '"level"' not in await factory.memory_prompt_section("legacy", "Запушь ветку на github")
    finally:
        for manager in levels.values():
            manager.close()


LEVELED_ANSWER = json.dumps({"memories": [
    {"layer": "L1", "level": "project", "category": "workflow",
     "content": "Atlas: релиз собирается командой make dist, артефакты в dist/atlas-*.tar.gz"},
    {"layer": "L1", "level": "chat", "category": "decision",
     "content": "В этом чате мост переносим в форк bridge, ветку main не трогаем до ревью"},
    {"layer": "L3", "level": "project", "category": "preference",
     "content": "Отвечать кратко и по-русски, без вступлений"},
    ]}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_turns_memories_go_to_the_level_the_model_names(tmp_path):
    factory, completions, _ = model_factory(tmp_path, LEVELED_ANSWER)
    leveled(factory, {"a": ATLAS})
    levels = dict(factory.memory_levels("a"))
    try:
        stored = await factory.remember_turn_async("a", REQUEST, "Готово.", "engineer")
        assert len(stored) == 3
        assert [r.content[:5] for r in levels["project"].live_records()] == ["Atlas"]
        assert [r.content[:9] for r in levels["chat"].live_records()] == ["В этом ча"]
        # A preference is the user's, whatever level it was given.
        assert [r.layer for r in levels["user"].live_records()] == [MemoryLayer.L3]
        prompt = completions.requests[0]["messages"][-1]["content"]
        assert '"project": the project "Atlas"' in prompt and '"chat": this chat' in prompt
    finally:
        for manager in levels.values():
            manager.close()


@pytest.mark.asyncio
async def test_without_a_project_a_projects_memory_is_the_users(tmp_path):
    factory, _, _ = model_factory(tmp_path, LEVELED_ANSWER)
    leveled(factory, {"solo": {"chat": ("ctx-solo", "")}})
    levels = dict(factory.memory_levels("solo"))
    try:
        await factory.remember_turn_async("solo", REQUEST, "Готово.", "engineer")
        assert {r.content[:5] for r in levels["user"].live_records()} == {"Atlas", "Отвеч"}
        assert len(levels["chat"].live_records()) == 1
    finally:
        for manager in levels.values():
            manager.close()


def test_the_model_is_told_the_levels_only_where_a_chat_has_them():
    plain = extraction_prompt("q", "a")
    assert '"level"' not in plain
    record = MemoryRecord("Atlas собирается через make dist", id="r1")
    leveled_prompt = extraction_prompt("q", "a", [record], levels={"project": 'the project "Atlas"', "user": "the user"},
                                       known_levels={"r1": "project"})
    assert '- "project": the project "Atlas"' in leveled_prompt
    assert '"level": "project"' in leveled_prompt
    parsed = parse_extraction(LEVELED_ANSWER, levels=["project", "user"])
    assert [item.level for item in parsed.memories] == ["project", None, "user"]


@pytest.mark.asyncio
async def test_the_agent_recalls_and_remembers_at_its_chats_levels(tmp_path):
    from agents import RunContextWrapper

    from tools.memory_tools import MEMORY_TOOLS

    factory, _, _ = model_factory(tmp_path, "{}")
    leveled(factory, {"a": ATLAS})
    levels = dict(factory.memory_levels("a"))
    try:
        context = RunContextWrapper(SimpleNamespace(factory=factory, context_id="a"))
        remember = MEMORY_TOOLS["memory_remember"].on_invoke_tool
        saved = json.loads(await remember(context, json.dumps({"content": "Atlas: пуш на github через paramiko"})))
        assert saved["level"] == "project"
        saved = json.loads(await remember(context, json.dumps({"content": "Тут пуш только в форк", "level": "chat"})))
        assert saved["level"] == "chat"
        refused = json.loads(await remember(context, json.dumps({"content": "x" * 20, "level": "team"})))
        assert "error" in refused

        hits = json.loads(await MEMORY_TOOLS["memory_recall"].on_invoke_tool(context, '{"query": "как запушить"}'))
        assert {hit["level"] for hit in hits} == {"project", "chat"}
        forgotten = json.loads(await MEMORY_TOOLS["memory_forget"].on_invoke_tool(
            context, json.dumps({"record_id": saved["id"]})))
        assert forgotten == {"forgotten": True} and levels["chat"].live_records() == []
    finally:
        for manager in levels.values():
            manager.close()


@pytest.mark.asyncio
async def test_the_panels_sync_files_each_memory_at_its_level(tmp_path):
    factory, _, _ = model_factory(tmp_path, LEVELED_ANSWER)
    leveled(factory, {"a": ATLAS})
    levels = factory.memory_levels("a", force=True)
    try:
        target = _Target(levels[-1][1], factory.config.get_memory_config(), True, factory, "engineer",
                         levels=levels, titles=factory.memory_level_titles("a"))
        messages = [{"role": "user", "content": REQUEST}, {"role": "assistant", "content": "Готово."}]
        stored = await _sync_with_model(target, messages, "chat:a")
        assert len(stored) == 3
        assert len(dict(levels)["project"].live_records()) == 1
    finally:
        for _, manager in levels:
            manager.close()
