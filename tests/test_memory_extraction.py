"""Turns reach memory as self-contained facts, without credentials.

Reproduces the chats that showed the problem: a push done with a key and its
password was stored as a raw transcript with the password in clear and a
context-free "I fixed only the branch you pushed" fact, while the next chat got
nothing useful about how pushing works.
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.agent_factory import AgentFactory
from core.memory import MemoryLayer, MemoryManager, MemoryRecord, SQLiteMemoryBackend
from core.memory.distill import distill_messages
from core.memory.llm_extract import Extracted, extraction_prompt, parse_extraction
from core.memory.redact import REDACTED, redact_secrets
from tests.test_memory_integration import host_config
from web_chat.memory_api import _sync_with_model, _Target

REQUEST = ("Чекай изменения в grid/ коммит и пуш. ключ: daniil.key, пароль: abc123. "
           "Убедись что все коммиты в истории были от моего имени без соавторства.")
ANSWER = ("Изменения закоммичены и запушены в `feat/claude-subscription-bridge`.\n\n"
          "**Пуш.** Ключ подошёл как RSA с паролем `abc123`. В контейнере не было `ssh`, поэтому я "
          "подключался через временную обёртку на paramiko.\n\n"
          "Я исправил только ту ветку, которую вы пушили.")


# -- redaction ------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kept", [
    ("ключ: daniil.key, пароль: abc123. Дальше", "ключ: daniil.key, пароль: [secret]. Дальше"),
    ("с паролем `abc123`.", "с паролем `[secret]`."),
    ("password is hunter22 ok", "password is [secret] ok"),
    ("OPENAI_API_KEY=sk-abcdefghijklmnop123", "OPENAI_API_KEY=[secret]"),
    ("https://user:pw@github.com/x", "https://[secret]@github.com/x"),
    ("auto_inject_tokens: 1000 и 500 токенов", "auto_inject_tokens: 1000 и 500 токенов"),
    ("пароль не нужен", "пароль не нужен"),
])
def test_redaction_masks_values_and_keeps_the_sentence(text, kept):
    assert redact_secrets(text) == kept
    assert redact_secrets(kept) == kept


def test_no_record_keeps_the_password_and_the_scenario_survives(tmp_path):
    records = distill_messages([{"role": "user", "content": REQUEST}, {"role": "assistant", "content": ANSWER}],
                               snapshot=True)
    assert records and all("abc123" not in r.content for r in records)
    assert any(r.layer == MemoryLayer.L0 and REDACTED in r.content for r in records)
    # A password in the request used to cost the whole L2 scenario.
    assert any(r.layer == MemoryLayer.L2 for r in records)
    # A bare "I fixed ..." sentence without anything concrete is no "bug" fact.
    assert not any("только ту ветку" in r.content for r in records if r.layer == MemoryLayer.L1)


def test_every_write_is_redacted(tmp_path):
    manager = MemoryManager(SQLiteMemoryBackend(tmp_path))
    stored = manager.remember(MemoryRecord("Деплой: token = ghp_abcdefghijklmnop1234", title="pwd: x1"))
    assert "ghp_" not in stored.content and stored.title == f"pwd: {REDACTED}"
    assert "ghp_" not in manager.get(stored.id).content


# -- the model's answer -----------------------------------------------------------------------

def test_parse_extraction_is_strict_about_items_and_lenient_about_wrapping():
    answer = "Here it is:\n```json\n" + json.dumps({"memories": [
        {"layer": "L1", "category": "workflow", "content": "Пуш на GitHub из контейнера: ssh нет, работает обёртка на paramiko",
         "tags": ["git", "push"], "supersedes": "known-1"},
        {"layer": "L1", "category": "preference", "content": "Пользователь хочет коммиты только от своего имени"},
        {"layer": "L0", "category": "note", "content": "raw transcripts are not the model's to write"},
        {"layer": "L9", "category": "note", "content": "an unknown layer is dropped"},
        {"layer": "L1", "category": "note", "content": "short"},
        {"layer": "L2", "category": "note", "content": "Задача: пуш; итог: запушено", "supersedes": "unknown"},
    ]}, ensure_ascii=False) + "\n```"
    items = parse_extraction(answer, ["known-1"])
    assert [i.record.layer for i in items] == [MemoryLayer.L1, MemoryLayer.L3, MemoryLayer.L2]
    assert items[0].supersedes == "known-1" and items[2].supersedes is None
    assert items[1].record.scope.value == "user"
    assert parse_extraction('{"memories": []}') == []
    with pytest.raises(ValueError):
        parse_extraction("I could not find anything")


def test_extraction_prompt_carries_known_memory_and_no_password():
    known = [MemoryRecord("Пуш через ssh не работает", id="k1")]
    prompt = extraction_prompt(REQUEST, ANSWER, known)
    assert "abc123" not in prompt and '"id": "k1"' in prompt and "Пуш через ssh" in prompt


def test_store_extracted_supersedes_within_a_layer_only(tmp_path):
    manager = MemoryManager(SQLiteMemoryBackend(tmp_path))
    old = manager.remember(MemoryRecord("Пуш: нужен ssh-клиент в контейнере"))
    scenario = manager.remember(MemoryRecord("Задача: пуш", layer=MemoryLayer.L2))
    stored = manager.store_extracted([
        Extracted(MemoryRecord("Пуш: ssh нет, работает обёртка на paramiko с ключом daniil.key"), old.id),
        Extracted(MemoryRecord("Пуш упал, затем прошёл через paramiko", layer=MemoryLayer.L1), scenario.id),
    ], source_ref="chat:c1", exchange=(REQUEST, ANSWER))
    assert {r.layer for r in stored} == {MemoryLayer.L1, MemoryLayer.L0}
    assert all(r.source_ref == "chat:c1" for r in stored)
    assert manager.get(old.id).superseded_by is not None
    assert manager.get(scenario.id).superseded_by is None  # another layer: kept
    current = [r.content for r in manager.list_records(limit=50)]
    assert "Пуш: нужен ssh-клиент в контейнере" not in current
    assert all("abc123" not in text for text in current)


# -- the turn hook --------------------------------------------------------------------------

class FakeCompletions:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    async def create(self, **request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.answer))])


def model_factory(tmp_path, answer, **options):
    factory = AgentFactory.__new__(AgentFactory)
    factory.config = host_config(tmp_path, enabled=True, **options)
    factory._memory_lock = threading.Lock()
    factory._memory_manager = None
    factory._memory_tenant = None
    completions = FakeCompletions(answer)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    factory.models = SimpleNamespace(compact_request=Mock(return_value=(client, "m", None)),
                                     model_request=Mock(return_value=(client, "x", None)))
    return factory, completions


MODEL_ANSWER = json.dumps({"memories": [
    {"layer": "L1", "category": "workflow",
     "content": "В контейнере агента нет ssh: git push в github.com/Daniil274/grid идёт через обёртку на paramiko с ключом daniil.key",
     "tags": ["git", "push", "paramiko"]},
    {"layer": "L2", "category": "note",
     "content": "Задача: закоммитить и запушить grid. Итог: ветка feat/claude-subscription-bridge запушена, история переписана на автора Daniil274"},
]}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_turn_memory_is_written_by_the_model(tmp_path):
    factory, completions = model_factory(tmp_path, MODEL_ANSWER)
    try:
        stored = await factory.remember_turn_async("ctx-1", REQUEST, ANSWER, "engineer")
        assert {r.layer for r in stored} == {MemoryLayer.L0, MemoryLayer.L1, MemoryLayer.L2}
        assert factory.models.compact_request.call_args.args == ("engineer",)
        sent = json.dumps(completions.requests[0]["messages"], ensure_ascii=False)
        assert "abc123" not in sent
        manager = factory.get_memory_manager()
        assert all("abc123" not in r.content for r in manager.list_records(limit=50))
        # The next chat about pushing gets the know-how in its prompt.
        section = manager.render_prompt_section("Запуш grid-next", max_tokens=1000).content
        assert "paramiko" in section
        # The model sees what is known: it is asked not to repeat it.
        await factory.remember_turn_async("ctx-2", "Запуш grid-next", "Готово, запушено через paramiko.", "engineer")
        assert "paramiko" in completions.requests[1]["messages"][-1]["content"].split("<user_request>")[0]
    finally:
        factory.get_memory_manager().close()


@pytest.mark.asyncio
async def test_extract_model_setting_picks_the_model(tmp_path):
    factory, _ = model_factory(tmp_path, MODEL_ANSWER, extract_model="cheap")
    try:
        await factory.remember_turn_async("c", REQUEST, ANSWER, "engineer")
        factory.models.model_request.assert_called_once_with("cheap")
        factory.models.compact_request.assert_not_called()
    finally:
        factory.get_memory_manager().close()


@pytest.mark.parametrize("answer", [RuntimeError("model down"), "not json at all"])
@pytest.mark.asyncio
async def test_a_failing_model_falls_back_to_the_heuristic(tmp_path, answer):
    factory, _ = model_factory(tmp_path, answer)
    try:
        stored = await factory.remember_turn_async("c", "Всегда отвечай кратко и по делу, пожалуйста",
                                                   "Хорошо, буду отвечать кратко и по делу.", "engineer")
        assert any(r.layer == MemoryLayer.L3 for r in stored)
    finally:
        factory.get_memory_manager().close()


@pytest.mark.asyncio
async def test_model_extraction_can_be_switched_off_and_never_raises(tmp_path):
    factory, completions = model_factory(tmp_path, MODEL_ANSWER, extract_with_model=False)
    try:
        await factory.remember_turn_async("c", "Всегда пиши тесты на pytest", "Хорошо, буду писать тесты.", "e")
        assert completions.requests == []
    finally:
        factory.get_memory_manager().close()
    muted, completions = model_factory(tmp_path, MODEL_ANSWER, extract_on_turn=False)
    assert await muted.remember_turn_async("c", REQUEST, ANSWER) == [] and completions.requests == []
    broken, _ = model_factory(tmp_path, MODEL_ANSWER)
    broken.get_memory_manager = Mock(side_effect=RuntimeError("down"))
    assert await broken.remember_turn_async("c", REQUEST, ANSWER) == []


@pytest.mark.asyncio
async def test_remember_turn_later_runs_in_the_background(tmp_path):
    factory, _ = model_factory(tmp_path, MODEL_ANSWER)
    try:
        task = factory.remember_turn_later("c", REQUEST, ANSWER, "engineer")
        assert task in factory._memory_tasks
        assert await task
        assert task not in factory._memory_tasks
    finally:
        factory.get_memory_manager().close()


def test_run_turn_does_not_wait_for_memory():
    import inspect

    from core.factory.turns import TurnRunner

    source = inspect.getsource(TurnRunner._run_turn)
    assert "self.remember_turn_later(" in source
    assert "to_thread(self.remember_turn" not in source


@pytest.mark.asyncio
async def test_panel_sync_reads_each_exchange_with_the_model(tmp_path):
    factory, completions = model_factory(tmp_path, MODEL_ANSWER)
    manager = factory.get_memory_manager()
    try:
        target = _Target(manager, factory.config.get_memory_config(), True, factory, "engineer")
        messages = [{"role": "user", "content": REQUEST}, {"role": "assistant", "content": ANSWER},
                    {"role": "user", "content": "Запуш grid-next"}, {"role": "assistant", "content": "Готово."}]
        stored = await _sync_with_model(target, messages, "chat:c")
        assert len(completions.requests) == 2 and stored
        assert {r.layer for r in stored} >= {MemoryLayer.L0, MemoryLayer.L1}
        off = _Target(manager, factory.config.get_memory_config(), False, factory, "engineer")
        assert await _sync_with_model(off, messages, "chat:c") is None
    finally:
        manager.close()
