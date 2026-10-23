# Проверка подготовленной версии 0.2.0

Дата проверки: 2026-10-23. Репозиторий `/user/grid`, ветка `feat/lazy-agents`.
Baseline: `45f02e56784d427005ef9ba8c5b0f6e7522b75ca`. Исходное рабочее дерево было чистым: отдельного baseline-коммита с чужими изменениями нет.

## Выполнено

- `python tools/release_check.py --json`: 27 проверок, `ok: true`.
- `ruff check . --select E9,F63,F7,F82`: успешно.
- `git diff --check`: успешно; workflow YAML разобран PyYAML.
- `node --test tests/*.test.mjs tests/*.test.cjs`: 303 passed.
- Финальный целевой Python-прогон после последних исправлений: 166 passed:

```sh
python -m pytest -o addopts='' -q \
  tests/test_revision_compatibility.py tests/test_web_restart.py \
  tests/test_web_chat_session.py tests/test_interruption.py \
  tests/test_web_policy_switch.py tests/test_context.py tests/test_branches.py
```

Включает 37 revision-тестов: legacy/new, future/malformed versions, digest/config/skill/tool-source drift, секреты, JSON/encrypted persistence, продолжение и fork, отказ без execution/journal, web preflight до quota/warm-up, machine-readable ошибки. Дополнительно исправлено: recovery выполняется после web gate; fork не переподписывает повреждённый manifest; крупные source-файлы хешируются потоково, а не по размеру.

Полная Python-регрессия **до последних небольших исправлений** выполнена исполнителем на копии дерева четырьмя группами файлов и `tests/compact`: **3171 passed, 4 skipped, 1 failed**. Один процесс всего suite был завершён по лимиту памяти (cgroup около 805 MB). Это не утверждение о полностью зелёном финальном suite.

Единственный сбой: `tests/test_memory_integration.py::test_example_configs_keep_coordinator_fallbacks_and_tools`. Независимо повторён на pristine baseline (`git archive 45f02e5`): в примере coordinator отсутствуют ожидаемые `beads_init`/`init_codegraph`. Пример не менялся в этой задаче. CI остаётся строгим: этот существующий сбой не скрыт и блокирует зелёный полный прогон/публикацию до отдельного исправления.

Тесты выполнялись Python 3.11 из `/tmp/v311` на копиях `/tmp/gridrun3` без production `.env`/runtime DB. Локальная `.venv` репозитория сломана (интерпретатор-ссылка отсутствует); production `.env` не читался и не копировался. Python 3.12, Docker build и удалённый GitHub Actions здесь не выполнялись.

## Независимая проверка

Используйте отдельный clone/checkout итогового commit, не production working tree и не его `.env`. Создайте новый venv, установите `python -m pip install -e '.[dev]'`, затем выполните команды выше, `python tools/release_check.py --json`, `ruff check . --select E9,F63,F7,F82` и полный `python -m pytest -o addopts='' -ra --strict-markers --timeout=300`. При ограниченной памяти запускайте группы файлов отдельными процессами; не исключайте известный baseline-сбой из отчёта.

Сценарии и порядок update/rollback описаны в [release-process.md](release-process.md), контракт для пользователей и агентов — в [revisions.md](revisions.md).

## Состояние и ограничения

**Prepared, not activated.** Нет push, tag, публикации image/package, production restart или миграции DB. Для активации владелец должен отдельно проверить изменения/backup и выполнить контролируемое обновление. До устранения существующего красного regression-теста критерии публикации не выполнены.

Совместимость намеренно строгая: равенство Grid version, Python minor и config digest. Нет старых runtime side-by-side, продолжения на сохранённом бинарнике, миграции/перепривязки legacy, планировщика спящих агентов или автоматического rollback. Новый пустой диалог закрепляется первым turn; загруженный без manifest остаётся legacy даже пустым. Смена системы внутри pinned диалога может быть запрещена. Новые ревизии не подменяют manifest старых диалогов: история читается, несовместимое продолжение отклоняется.

Digest — контроль согласованности, **не криптографическая подпись против злоумышленника с правом записи**. Версии runtime/tool contracts требуют дисциплины выпуска: нет аттестации удалённых MCP-серверов, полного dependency lock/SBOM или доказательства неизменности внешних провайдеров. Локальные явно подключённые skill/tool источники учитываются; это не snapshot всей файловой системы. Сохраняется ограниченная проекция defaults, не полный исполняемый конфиг. Нельзя восстановить старую систему из одного manifest. Текущие права проверяются существующей action policy; manifest не выдаёт разрешений. Внешние операции не переигрываются и exactly-once не обещается.
