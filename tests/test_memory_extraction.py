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
from core.memory import (
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
    MemoryScope,
    SQLiteMemoryBackend,
)
from core.memory.distill import distill_messages
from core.memory.llm_extract import (
    Extracted,
    Extraction,
    extraction_prompt,
    parse_extraction,
)
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
                               snapshot=True)  # the heuristic, for setups without a model
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

def test_parse_extraction_keeps_knowledge_layers_only():
    answer = "Here it is:\n```json\n" + json.dumps({"memories": [
        {"layer": "L1", "category": "workflow", "content": "Пуш на GitHub из контейнера: ssh нет, работает обёртка на paramiko",
         "tags": ["git", "push", "пуш"]},
        {"layer": "L3", "category": "preference", "content": "Коммиты только от имени пользователя, без соавторов"},
        {"layer": "L3", "category": "preference",
         "content": "Требования к доступу: systems_list регистрировать в core/system_access.py"},
        {"layer": "L2", "category": "note", "content": "Задача: пуш; итог: запушено, тесты прошли"},
        {"layer": "L0", "category": "note", "content": "raw transcripts are not the model's to write"},
        {"layer": "L1", "category": "note", "content": "short"},
    ], "obsolete": ["known-1", "made-up"]}, ensure_ascii=False) + "\n```"
    result = parse_extraction(answer, ["known-1"])
    layers = [(i.record.layer.value, i.record.category.value) for i in result.memories]
    # A task's spec naming files is no profile entry: L3 reaches every prompt.
    assert layers == [("L1", "workflow"), ("L3", "preference"), ("L1", "constraint")]
    assert result.memories[1].record.scope.value == "user"
    assert result.obsolete == ["known-1"]  # only ids the model was shown
    assert parse_extraction('{"memories": []}') == Extraction([], [])
    with pytest.raises(ValueError):
        parse_extraction("I could not find anything")


def test_extraction_prompt_carries_known_memory_and_no_password():
    known = [MemoryRecord("Пуш через ssh не работает", id="k1")]
    prompt = extraction_prompt(REQUEST, ANSWER, known)
    assert "abc123" not in prompt and '"id": "k1"' in prompt and "Пуш через ssh" in prompt


def test_store_extracted_retires_only_extracted_records(tmp_path):
    manager = MemoryManager(SQLiteMemoryBackend(tmp_path))
    wrong = manager.remember(MemoryRecord("Пуш: нужен ssh-клиент в контейнере", source_ref="chat:c0", confidence=0.8))
    kept = manager.remember(MemoryRecord("Пуш делать только после ревью", source_ref="file:README.md"))
    stored = manager.store_extracted(Extraction(
        [Extracted(MemoryRecord("Пуш: ssh нет, работает обёртка на paramiko с ключом daniil.key", confidence=0.8))],
        [wrong.id, kept.id],
    ), source_ref="chat:c1", exchange=(REQUEST, ANSWER))
    assert {r.layer for r in stored} == {MemoryLayer.L1, MemoryLayer.L0}
    assert manager.get(wrong.id) is None
    assert manager.get(kept.id) is not None  # stored on purpose: not the model's to remove
    assert all("abc123" not in r.content for r in manager.list_records(limit=50))


# -- embeddings -------------------------------------------------------------------------------

#: Concepts of a toy embedding space: a text's vector counts its concepts' words.
CONCEPTS = (
    ("push", "пуш", "ssh", "paramiko", "github"),
    ("commit", "коммит", "автор", "co-authored"),
    ("документ", "documentation", "docs"),
    ("кнопк", "button", "веб-чат"),
    ("координатор", "coordinator", "систем"),
)


class FakeEmbeddings:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def create(self, model, input, encoding_format=None):
        self.calls.append(list(input))
        if self.fail:
            raise RuntimeError("embeddings down")
        def vector(text):
            text = text.casefold()
            return [0.05] + [float(sum(text.count(word) for word in words)) for words in CONCEPTS]
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=vector(t)) for i, t in enumerate(input)])


class FakeCompletions:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    async def create(self, **request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.answer))])


def model_factory(tmp_path, answer, *, embeddings=True, fail_embeddings=False, **options):
    if embeddings:
        options.setdefault("embeddings", {"provider": "openrouter", "query_instruction": ""})
    factory = AgentFactory.__new__(AgentFactory)
    factory.config = host_config(tmp_path, enabled=True, **options)
    factory.config.get_provider = lambda key: SimpleNamespace(base_url="http://x", timeout=5, max_retries=0)
    factory._memory_lock = threading.Lock()
    factory._memory_manager = None
    factory._memory_tenant = None
    completions = FakeCompletions(answer)
    vectors = FakeEmbeddings(fail_embeddings)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions), embeddings=vectors)
    factory.models = SimpleNamespace(
        compact_request=Mock(return_value=(client, "m", None)),
        model_request=Mock(return_value=(client, "x", None)),
        make_client=Mock(return_value=client),
        access=SimpleNamespace(api_key=lambda config, provider: "key"),
    )
    factory.context_manager = SimpleNamespace(conversation_view=lambda cid: {"messages": [
        SimpleNamespace(role="user", get_text_content=lambda: "Как запушить ветку на github?"),
    ]})
    return factory, completions, vectors


def seed(manager):
    push = manager.remember(MemoryRecord("В контейнере нет ssh: git push в github идёт через paramiko",
                                         tags=("push", "пуш")))
    docs = manager.remember(MemoryRecord("Документация GRID собирается из docs/ через mkdocs"))
    profile = manager.remember(MemoryRecord("Отвечать кратко, по-русски", layer=MemoryLayer.L3, scope=MemoryScope.USER))
    return push, docs, profile


@pytest.mark.asyncio
async def test_the_prompt_gets_the_profile_and_only_what_is_close_in_meaning(tmp_path):
    factory, _, vectors = model_factory(tmp_path, "{}")
    manager = factory.get_memory_manager()
    try:
        push, docs, profile = seed(manager)
        section = await factory.memory_prompt_section("c", "Запушь ветку feature на github")
        assert push.content in section and profile.content in section and docs.content not in section
        # Nothing close: the profile alone, not "the newest few".
        section = await factory.memory_prompt_section("c", "Почини кнопку в веб-чате, она не нажимается вообще")
        assert profile.content in section and push.content not in section and docs.content not in section
        # Records are embedded once; later searches embed only the query.
        indexed = sum(len(batch) for batch in vectors.calls)
        await factory.memory_prompt_section("c", "Запушь ветку feature на github")
        assert sum(len(batch) for batch in vectors.calls) == indexed + 1
        # An edited record is embedded again.
        manager.update_record(docs.id, content="Документация собирается через sphinx")
        await factory.memory_prompt_section("c", "Документация")
        assert ["Документация собирается через sphinx"] in vectors.calls
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_a_short_request_is_searched_with_the_previous_ones(tmp_path):
    factory, _, _ = model_factory(tmp_path, "{}")
    manager = factory.get_memory_manager()
    try:
        push, _, _ = seed(manager)
        assert "Как запушить ветку на github?" in factory.memory_query("c", "Работай")
        assert push.content in await factory.memory_prompt_section("c", "Работай")
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_without_embeddings_only_literal_matches_are_injected(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "down").mkdir()
    for factory, _, _ in (model_factory(tmp_path / "off", "{}", embeddings=False),
                          model_factory(tmp_path / "down", "{}", fail_embeddings=True)):
        manager = factory.get_memory_manager()
        try:
            push, docs, profile = seed(manager)
            section = await factory.memory_prompt_section("c", "paramiko")
            assert push.content in section and docs.content not in section and profile.content in section
            section = await factory.memory_prompt_section("c", "Почини кнопку отправки в веб-чате, она не нажимается")
            assert push.content not in section and docs.content not in section
        finally:
            manager.close()


# -- the turn hook --------------------------------------------------------------------------

MODEL_ANSWER = json.dumps({"memories": [
    {"layer": "L1", "category": "workflow",
     "content": "В контейнере агента нет ssh: git push в github.com/Daniil274/grid идёт через обёртку на paramiko с ключом daniil.key",
     "tags": ["git", "push", "пуш", "paramiko"]},
    {"layer": "L3", "category": "preference",
     "content": "Все коммиты - от Daniil274, без Co-authored-by; перед force-push спрашивать"},
]}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_turn_memory_is_written_by_the_model_and_embedded(tmp_path):
    factory, completions, _ = model_factory(tmp_path, MODEL_ANSWER)
    try:
        stored = await factory.remember_turn_async("ctx-1", REQUEST, ANSWER, "engineer")
        assert {r.layer for r in stored} == {MemoryLayer.L1, MemoryLayer.L3}  # no L0 copy by default
        assert factory.models.compact_request.call_args.args == ("engineer",)
        sent = json.dumps(completions.requests[0]["messages"], ensure_ascii=False)
        assert "abc123" not in sent
        manager = factory.get_memory_manager()
        assert manager.unembedded("qwen/qwen3-embedding-8b") == []  # embedded right away
        # The next chat about pushing gets the know-how by meaning.
        section = await factory.memory_prompt_section("ctx-2", "Запушь grid-next на github")
        assert "paramiko" in section
        # The model sees what this chat already stored: it is asked not to repeat it.
        await factory.remember_turn_async("ctx-1", "Запушь ещё раз", "Готово, запушено через paramiko.", "engineer")
        assert "paramiko" in completions.requests[1]["messages"][-1]["content"].split("<user_request>")[0]
    finally:
        factory.get_memory_manager().close()


@pytest.mark.asyncio
async def test_extract_model_setting_picks_the_model(tmp_path):
    factory, _, _ = model_factory(tmp_path, MODEL_ANSWER, extract_model="cheap")
    try:
        await factory.remember_turn_async("c", REQUEST, ANSWER, "engineer")
        factory.models.model_request.assert_called_once_with("cheap")
        factory.models.compact_request.assert_not_called()
    finally:
        factory.get_memory_manager().close()


@pytest.mark.parametrize("answer", [RuntimeError("model down"), "not json at all"])
@pytest.mark.asyncio
async def test_a_failing_model_stores_nothing_rather_than_fragments(tmp_path, answer):
    factory, _, _ = model_factory(tmp_path, answer)
    try:
        assert await factory.remember_turn_async("c", REQUEST, ANSWER, "engineer") == []
        assert factory.get_memory_manager().list_records(limit=10) == []
    finally:
        factory.get_memory_manager().close()


@pytest.mark.asyncio
async def test_model_extraction_can_be_switched_off_and_never_raises(tmp_path):
    factory, completions, _ = model_factory(tmp_path, MODEL_ANSWER, extract_with_model=False)
    try:
        stored = await factory.remember_turn_async("c", "Всегда отвечай кратко и по делу, пожалуйста",
                                                   "Хорошо, буду отвечать кратко и по делу.", "e")
        assert completions.requests == [] and any(r.layer == MemoryLayer.L3 for r in stored)  # the heuristic
    finally:
        factory.get_memory_manager().close()
    muted, completions, _ = model_factory(tmp_path, MODEL_ANSWER, extract_on_turn=False)
    assert await muted.remember_turn_async("c", REQUEST, ANSWER) == [] and completions.requests == []
    broken, _, _ = model_factory(tmp_path, MODEL_ANSWER)
    broken.get_memory_manager = Mock(side_effect=RuntimeError("down"))
    assert await broken.remember_turn_async("c", REQUEST, ANSWER) == []
    assert await broken.memory_prompt_section("c", REQUEST) is None


@pytest.mark.asyncio
async def test_remember_turn_later_runs_in_the_background(tmp_path):
    factory, _, _ = model_factory(tmp_path, MODEL_ANSWER)
    try:
        task = factory.remember_turn_later("c", REQUEST, ANSWER, "engineer")
        assert task in factory._memory_tasks
        assert await task
        assert task not in factory._memory_tasks
    finally:
        factory.get_memory_manager().close()


def test_run_turn_picks_memory_per_request_and_does_not_wait_for_extraction():
    import inspect

    from core.factory.turns import TurnRunner

    source = inspect.getsource(TurnRunner._run_turn)
    assert "await self.memory_prompt_section(active_context_id, message)" in source
    assert "self.remember_turn_later(" in source
    assert "to_thread(self.remember_turn" not in source


@pytest.mark.asyncio
async def test_panel_sync_reads_each_exchange_with_the_model(tmp_path):
    factory, completions, _ = model_factory(tmp_path, MODEL_ANSWER)
    manager = factory.get_memory_manager()
    try:
        target = _Target(manager, factory.config.get_memory_config(), True, factory, "engineer")
        messages = [{"role": "user", "content": REQUEST}, {"role": "assistant", "content": ANSWER},
                    {"role": "user", "content": "Запуш grid-next"}, {"role": "assistant", "content": "Готово."}]
        stored = await _sync_with_model(target, messages, "chat:c")
        assert len(completions.requests) == 2 and stored
        off = _Target(manager, factory.config.get_memory_config(), False, factory, "engineer")
        assert await _sync_with_model(off, messages, "chat:c") is None
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_memory_recall_tool_searches_by_meaning(tmp_path):
    from agents import RunContextWrapper

    from tools.memory_tools import MEMORY_TOOLS

    factory, _, _ = model_factory(tmp_path, "{}")
    manager = factory.get_memory_manager()
    try:
        push, _, _ = seed(manager)
        context = RunContextWrapper(SimpleNamespace(factory=factory))
        hits = json.loads(await MEMORY_TOOLS["memory_recall"].on_invoke_tool(context, '{"query": "как запушить"}'))
        assert hits[0]["record"]["id"] == push.id and hits[0]["score"] > hits[-1]["score"]
    finally:
        manager.close()
