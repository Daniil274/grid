# Как строить систему Grid

Система — один `config.yaml` со своими агентами. Созданная тобой система живёт
в своём каталоге в хранилище созданных систем (путь даёт `builder_catalog`):

```
<store>/<key>/
  config.yaml          провайдеры, модели, инструменты, агенты
  system.yaml          манифест: имя, описание для роутера, статус — не пиши сам,
                       меняй через builder_describe
  README.md            назначение, как пользоваться, ограничения
  skills/<name>.md     протоколы, подмешиваемые в промпт агента
  tools/<package>/     пакеты инструментов (новые инструменты)
```

Относительные пути в `config.yaml` отсчитываются от этого каталога.

`builder_catalog.shared: false` означает личное хранилище пользователя.
Соблюдай `private_rules`: провайдеры копируются без изменений, модели берутся
из каталога, новые инструменты оформляются как MCP-пакеты. Импорт `project_tools`
на сервере для личных систем запрещён. Личный черновик увидит только владелец;
он может попробовать его и включить для своего роутера на `/systems`.
В ответе используй `chat_system` из результата инструмента для ссылки
`/?system=<chat_system>`: это ключ системы в чате, отдельный от ключа каталога.

## Порядок работы

1. `builder_catalog`: какие есть системы, модели, готовые инструменты и наборы.
   Прочитай (`builder_read`) конфиг ближайшей системы из каталога — это образец
   формата и проверенных провайдеров и моделей.
2. Спроектируй: какие задачи, сколько агентов (обычно один; несколько — только если
   у них разные роли и инструменты), какие инструменты готовые, каких не хватает.
3. `builder_create` с полным `config.yaml` и описанием для роутера.
4. Навыки и README — `builder_write`.
5. Для каждого недостающего инструмента — пакет инструментов с тестами
   (`builder_write` модулей, тестов, `requirements.txt`), затем
   `builder_test_tools`. Чини, пока тесты не пройдут.
6. Напиши `quality.yaml` по навыку quality: результат, контракты ролей,
   примеры использования, ограничения и сценарии с проверками результата.
7. `builder_check`: исправь конфигурационные ошибки; отсутствие программ
   означает, что запуск ещё не проверен. `healthy` проверяет только конфигурацию.
8. `builder_evaluate`: реальный запуск сценариев. Разбери провалы, исправь
   реализацию, повтори затронутые тесты пакетов и всю оценку. Максимум три цикла.
   После любого изменения старые отчёты могут устареть — смотри quality.ready.
9. Итог владельцу: проверенная ревизия, результаты, ограничения, способ пробного
   запуска. Недоступная среда — блокер, не повод сообщать об успехе.

## config.yaml

```yaml
settings:
  default_agent: main
  working_directory: .
  allow_path_override: true
  max_turns: 100
  mcp_enabled: true                  # нужно, если есть пакеты инструментов
providers:                           # скопируй ЦЕЛИКОМ из builder_catalog.providers:
  opencode:                          # без default_headers opencode отвечает 400
    name: opencode
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_API_KEY    # имя переменной, никогда не сам ключ
    default_headers:
      User-Agent: grid-agent-system/1.0
      x-opencode-session: grid-coder
    timeout: 300
    max_retries: 3
models:                              # только модели из builder_catalog.models,
  glm-flash:                         # с теми же name и provider; по умолчанию
    name: glm-5.3-flash              # flash-модели, другие — только если
    provider: opencode               # администратор попросил
tools:
  word_stats:                        # новый инструмент: пакет
    type: mcp
    tool_package: tools/word_stats
    description: Word and sentence statistics of text files in the workspace
  file_read:                         # готовый общий инструмент
    type: function
    description: Read a file
agents:
  main:
    name: Text analyst
    model: [glm-flash, deepseek-flash-latest]   # основная и запасная
    description: >-
      Что делает агент — роутер читает это, если агент routable.
    tools: [word_stats, file_read]
    system_skills: [analysis]        # skills/analysis.md
    custom_prompt: |
      Роль, границы, признак завершения. Коротко; длинные протоколы — в навыки.
```

- Каждый инструмент, которым пользуется агент, объявлен в `tools`, иначе конфиг не загрузится.
- `type: function` — готовый инструмент: общий (`builder_catalog.shared_tools`) или из
  готового набора (`builder_catalog.tool_sets`: подключи через
  `settings.project_tools: {enabled: true, tools_directory: <путь из tool_sets>}`
  и объяви нужные; `builder_tool_set` покажет, где каждый действует). На сервере с
  аккаунтами работают только инструменты, которые действуют в контейнере или рабочей
  папке пользователя — остальные будут скрыты, `builder_check` это покажет.
- `type: agent` — вызов другого агента системы: `target_agent`, `description`,
  `context_strategy: minimal`.
- `type: mcp` с `tool_package` — новый инструмент (ниже). Агенту с таким инструментом
  нужен `mcp_enabled: true` (в settings или у агента).

## Описания решают маршрутизацию

Роутер выбирает систему по описанию из манифеста (параметр `description` у
`builder_create`), агента — по `description` routable-агентов. Называй конкретные
задачи и входы и явно — для чего система НЕ нужна, если рядом похожая. Один вход —
один routable-агент, остальным `routable: false`.

## Пакет инструментов

Новый инструмент — обычная функция Python в каталоге `tools/<package>/`. Код
выполняется не на сервере, а в контейнере пользователя; рабочая папка при вызове —
рабочая папка пользователя, пути относительные.

```python
# tools/word_stats/stats.py
import re
from grid_tool import tool


@tool(read_only=True)
def word_stats(path: str, top: int = 10) -> dict:
    """Word statistics of a text file: words, sentences, the most frequent words.

    Args:
        path: the file, relative to the workspace
        top: how many frequent words to list
    """
    text = open(path, encoding="utf-8").read()
    words = re.findall(r"\w+", text.lower())
    counts = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    frequent = sorted(counts.items(), key=lambda item: -item[1])[:top]
    return {"words": len(words), "sentences": len(re.findall(r"[.!?]+", text)), "frequent": frequent}
```

```python
# tools/word_stats/test_stats.py
import os
import tempfile

from stats import word_stats


def test_counts_words_and_sentences():
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "a.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("One two two. Three!")
    result = word_stats(path, top=1)
    assert result["words"] == 4 and result["sentences"] == 2
    assert result["frequent"] == [("two", 2)]
```

Правила:
- `@tool` из `grid_tool`; флаги: `read_only=True` (ничего не меняет),
  `destructive=True` (удаляет или перезаписывает), `open_world=True` (ходит в
  интернет). Их читает политика действий — ставь честно.
- Docstring обязателен: первая часть — что делает и когда звать, `Args:` —
  параметры. Типы параметров: `str`, `int`, `float`, `bool`, `list`, `dict`,
  `Optional[...]`, `Literal[...]`. Без `*args`/`**kwargs`.
- Возвращай строку или JSON-совместимое значение. Ошибку сообщай исключением
  с понятным текстом — агент увидит его как ошибку вызова.
- Модули с `_` в начале — общий код, не инструменты. Импорт между модулями пакета
  — по имени модуля (`from stats import word_stats`).
- `requirements.txt` — только пакеты PyPI с одной версией: `name==1.2.3`. Системные
  программы (apt: pandoc, libreoffice, …) так не ставятся: если без них никак,
  скажи администратору и перечисли их в `requires` через builder_describe.
- Тесты: `test_*.py` с функциями `test_*` и `assert`. Они идут в одноразовом
  контейнере БЕЗ сети: данные создавай во временной папке, сеть в тестах не трогай.
- В контейнере: Python 3.11, git, curl, ripgrep, node. Больше ничего не предполагай.
- Никаких ключей и паролей в коде: нужен секрет — `api_key_env`-подобная переменная
  окружения, и скажи администратору, какую задать.

## Готовность

`builder_check` → `healthy: true` и `quality.ready: true`. У каждого пакета
должен быть хотя бы один прошедший тест в изолированном контейнере без сети.
`builder_evaluate` вызывает модели и расходует токены. Отчёт показывает результаты
конкретных сценариев; независимую пользовательскую приёмку он не заменяет.
Действующую версию меняй через `builder_fork`, сохраняя исходную для сравнения.
Ограничения первой версии оценщика: общие инструменты и локальные tool_package,
синхронное делегирование type: agent; без project_tools, произвольных MCP-команд
и фоновой/межсистемной оркестрации. Не обходи отказ оценщика другими инструментами.
