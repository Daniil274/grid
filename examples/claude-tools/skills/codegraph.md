# CodeGraph

CodeGraph строит семантический граф **текущей рабочей директории агента**. Индекс лежит в `.codegraph/` **внутри workspace**, не в корне monorepo.

## Инициализация

При старте агента автоматически выполняется `codegraph init -i` в working directory (идемпотентно).

Если CodeGraph вернул структуру чужого проекта (например, `core/`, `examples/` вместо файлов workspace) — значит MCP смотрел не туда. Перезапусти сессию после init или проверь, что в конфиге у `codegraph` стоит `add_working_directory: true` и `--path` в `server_command`.

## Когда использовать CodeGraph вместо grep/glob

| Задача | Инструмент |
|--------|------------|
| Найти символ по имени (класс, функция, метод) | `codegraph_search` |
| Понять, как устроена подсистема / «как работает X?» | `codegraph_context` (через subagent `Agent`) |
| Кто вызывает символ | `codegraph_callers` |
| Что вызывает символ | `codegraph_callees` |
| Что сломается при изменении | `codegraph_impact` |
| Детали одного символа | `codegraph_node` |
| Структура проекта из индекса | `codegraph_files` |
| Статус индекса | `codegraph_status` |
| Точный текст / regex по файлам | `grep_tool` |
| Поиск файлов по шаблону | `glob_tool` |

**Предпочитай CodeGraph** для архитектурных вопросов и навигации по связям. **Предпочитай grep/glob** для точного текстового поиска и когда `.codegraph/` отсутствует.

## Главная сессия vs subagent

`codegraph_context` и explore-вызовы возвращают много исходного кода — **не вызывай их напрямую в главной сессии** для широкого исследования.

Для вопросов «как работает X?», «где реализовано Y?», «объясни систему Z» — делегируй subagent `Agent` с инструкцией:

> В проекте есть CodeGraph (`.codegraph/`). Используй `codegraph_context` и `codegraph_search` как основные инструменты исследования. Не перечитывай файлы, которые уже вернул CodeGraph. К grep/glob/read переходи только если CodeGraph не дал нужного или нужны детали из «Additional relevant files».

В главной сессии напрямую допустимы **лёгкие** вызовы: `codegraph_search`, `codegraph_callers`, `codegraph_callees`, `codegraph_impact`, `codegraph_node`, `codegraph_status`.

## Пакетирование

Независимые `codegraph_search` / `codegraph_callers` / `codegraph_callees` для разных символов — одним ответом параллельно.
