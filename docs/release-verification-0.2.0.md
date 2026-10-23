# Проверка подготовленной версии 0.2.0

Дата: 2026-10-23. Репозиторий `/user/grid`, ветка `feat/lazy-agents`.
Baseline: `45f02e56784d427005ef9ba8c5b0f6e7522b75ca`.
Ранее завершены `521a283` (revision manifests/gates) и `91288f0` (release docs/gates).
Этот отчёт заменяет прежний статус с одним красным baseline-тестом.

## Финальный результат

После последнего изменения кода (привязка confirmation к целевому окружению и
fail-closed provenance) выполнен полный Python-прогон: **3207 collected,
3203 passed, 4 skipped, 0 failed/errors**. Все 152 модуля, включая
`tests/compact/`, распределены по 18 последовательным процессам; объединение
batch-списков точно равно списку собранных модулей, исключений тестов нет.
Четыре skips: два Docker-теста, non-root file-giveaway и optional Playwright.
Это не проверка Docker или реального браузера.

- Полный Node: `node --test tests/*.test.mjs tests/*.test.cjs` — **321 passed**, 0 failed/skipped.
- Фокусный migration: `tests/test_context_migration.py` — **29 passed**.
- Revision regression: `tests/test_revision_compatibility.py` — 37 тестов, включены в полный прогон.
- `python3 tools/release_check.py --json` — **27 checks, ok: true, failed: []**.
- `python3 -m ruff check . --select E9,F63,F7,F82` — успешно.
- `git diff --check` — успешно.
- Координатор дополнительно повторил validator, ruff, whitespace и обе новые
  UI-группы: `node --test tests/web_chat_reference.test.mjs tests/web_chat_layout.test.mjs`
  — **18 passed** (12 reference + 6 layout).
- Независимый повтор по исходному финальному дереву: migration + revisions +
  исправленный example regression — **67 passed**, одно предупреждение
  FastAPI/Starlette об устаревающей связке TestClient/httpx.

## Что проверено

- Поддерживаемые owner-scoped GET dry-run и POST с `plan_token` + `confirm: true`;
  UI «Reference-only copy…», явное согласие, открытие новой беседы и постоянная
  ссылка на исходную историю.
- Новый **пустой** контекст с новой ревизией; оригинал неизменён, legacy
  provenance остаётся `unknown`, в том числе при повторном запросе.
- Нет импорта messages/executions, approvals, pending effects/tool calls,
  credentials, sessions, queues, checkpoints, workspace или старой маршрутизации.
  История — только ссылка для человека, не автоматический контекст модели.
- Идемпотентность в том же окружении; corrupt/future/foreign manifests и
  неизвестные integrity-вердикты отвергаются. Несовместимая существующая цель
  возвращает `migration.target_incompatible`, не получает новый manifest.
- Смена источника, выбранной системы/агента или целевого окружения после
  dry-run делает подтверждение устаревшим; новый контекст не создаётся.
- Реальный двухсистемный `UserSpace`: preview не строит factory/runtime,
  каталог чата или контейнер; цель получает свежий explicit system/agent
  selection и обычный chat-space, проходит gate первого нового хода.
  Продолжение подчинено текущей action policy, подтверждение не выдаёт прав.
- Исходный regression `test_example_configs_keep_coordinator_fallbacks_and_tools`
  исправлен в **исходном примере** добавлением `beads_init` в tools координатора.
  `init_codegraph` и fallback-модели уже присутствовали. Тест не изменён и зелёный
  в полном финальном прогоне; generated systems не редактировались.
- Обнаруженные дополнительные CSS/layout-правки сохранены отдельно от
  migration-коммита и включены в Node-прогон; геометрию браузера координатор
  самостоятельно не проверял.

## Окружение и воспроизведение

Python **3.11.16**, pytest **9.1.1**, Node **22.23.3**, ruff **0.17.2**.
Прогон выполнен в `/workspace/run/snapshot`: копия отслеживаемых и новых
неигнорируемых исходников, без production `.env`, runtime DB и данных.
Перед прогоном и после него сравнивались sha256 файлов: отличий с репозиторием
не было (включая release docs/manifest). После прогона менялись только этот
verification-отчёт и уточнение текста документации, не код/тесты/конфиги.

Production `.env` недоступен обычному пользователю; он не читался и не
копировался. Test-only `sitecustomize` отключал dotenv discovery/loading.
Использовался `HOME=/home/agent`, чтобы subprocess-тесты видели установленные
user-site зависимости. В scratch-копии создан отдельный git commit для тестов,
использующих `git show/archive/rev-parse`; это не изменение git history проекта.

Ограничение изоляции тестового harness: после summary batch 11 SDK tracing
попытался отправить trace в OpenAI и получил HTTP 401; также записал ошибку
закрытого log stream. Процесс завершился с кодом 0. Это не эффект migration API,
но полный suite нельзя описывать как полностью офлайн-прогон.

Команды из scratch checkout (полный прогон разбивался по модулям для памяти):

```sh
export HOME=/home/agent
export PYTHONPATH=/workspace/run/safe:/workspace/run/snapshot:/home/agent/.local/lib/python3.11/site-packages
export TMPDIR=/workspace/run/tmp
python3 -m pytest tests --collect-only -q -o addopts='' -p no:cacheprovider
# Все собранные модули в 18 непересекающихся batch; каждый запускается так:
python3 -m pytest <batch-files> -q --tb=short --strict-markers \
  -o addopts='' -p no:cacheprovider --timeout=120
node --test tests/*.test.mjs tests/*.test.cjs
python3 tools/release_check.py --json
python3 -m ruff check . --select E9,F63,F7,F82
```

`-o addopts=''` отключает штатный stop-on-first-failure, а не скрывает сбои.
Полный протокол этой сессии: `/workspace/final-python-regression.md`, partition
`/workspace/run/batches3.json`, логи `/workspace/run/logs4/`.
Для независимой проверки используйте чистый checkout итогового commit без
боевого `.env`, новый venv и `python -m pip install -e '.[dev]'`; полный suite
в CI: `pytest -o addopts='' -ra --strict-markers --timeout=300`.

## Статус и границы

**Prepared, not activated.** Нет push/tag, package/image publication,
production restart, production DB migration или beads_sync. Зелёные локальные
проверки не означают активацию и не заменяют оставшиеся release gates:
**Python 3.12, Docker build и удалённый GitHub Actions не выполнялись**.

Строгий exact-version/config gate сохранён. Безопасный путь вперёд — новый
пустой reference-only контекст, не продолжение старого executable state и не
перенос файлов. Нет старых runtime side-by-side, in-place repin, автоматической
миграции истории, replay, exactly-once или автоматического rollback.
Идемпотентность сериализуется process-local lock: multi-worker atomic dedup и
crash-atomic transaction между pin и reference record не заявлены; hard crash
может оставить пустую pinned цель до записи ссылки. Обычный отказ записи
обрабатывается удалением новой цели, не изменением оригинала.

Digest — контроль согласованности, не подпись против владельца файлов.
Нет полной аттестации внешних MCP/provider-зависимостей, dependency lock/SBOM
или snapshot всей файловой системы. Один manifest не восстанавливает старую
систему. Порядок публикации/активации и отката — в
[release-process.md](release-process.md), пользовательский/API-контракт — в
[revisions.md](revisions.md).
