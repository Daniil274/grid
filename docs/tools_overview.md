# Обзор инструментов

## Оглавление
1. [Введение](#введение)
2. [Function Tools](#function-tools)
3. [Agent Tools](#agent-tools)
4. [Специализированные инструменты](#специализированные-инструменты)
5. [Интеграция с AgentFactory](#интеграция-с-agentfactory)

---

## Введение

**Tools** — директория `tools/` содержит инструменты для агентов:
- **Function Tools**: Прямые Python-функции (быстрые, детерминированные)
- **Agent Tools**: Подагенты с собственными моделями/инструкциями
- **MCP Tools**: Внешние stdio-серверы (terminal, filesystem)

**Общее количество:** 17 модулей.

**Доступ:** Через `GridRunContext.factory` или `config.yaml → tools`.

---

## Function Tools

| Файл | Размер | Описание | Примеры функций |
|------|--------|----------|-----------------|
| beads_tools.py | 30KB | Управление задачами Beads (workspace) | beads_show, beads_update, list_files |
| document_tools.py | 11KB | Обработка документов (Markdown, PDF) | extract_text, generate_summary |
| emergency_tools.py | 10KB | Аварийные инструменты (shutdown, panic) | emergency_shutdown |
| file_tools.py | 25KB | Файловые операции (read/write/search) | read_file, write_file, search_files |
| function_tools.py | 11KB | Универсальные инструменты (eval, call) | safe_eval, dynamic_call |
| git_tools.py | 65KB | Git операции (clone, commit, diff) | git_clone, git_status, git_diff |
| input_tools.py | 4KB | Ввод данных (prompt, confirm) | user_input, yes_no |
| markdown_tools.py | 5KB | Markdown обработка (render, parse) | md_to_html, parse_table |
| memory_tools_v2.py | 17KB | Доступ к памяти (store/retrieve) | get_memory, store_fact |
| ocr_tools.py | 7KB | OCR распознавание текста | extract_text_from_image |
| orchestrator_tools.py | 13KB | Оркестрация подагентов | delegate_task, analyze_plan |
| screen_tools.py | 2KB | Скриншоты и захват экрана | capture_screen |
| skill_tools.py | 8KB | Выполнение навыков (skills/md) | execute_skill |
| vision_tools.py | 7KB | Компьютерное зрение | describe_image, detect_objects |
| voice_tools.py | 7KB | Голосовые операции (STT/TTS) | transcribe_audio, synthesize_speech |

---

## Agent Tools

Инструменты, использующие подагентов (тип `agent` в config.yaml):
- **orchestrator_tools.py**: Task orchestrator
- **skill_tools.py**: Skill executor
- **task_analyst.py** (упоминается в config): Анализ задач

**Конфигурация:**
```yaml
tools:
  orchestrator:
    type: agent
    prompt_addition: \"Анализируй план...\"
```

---

## Специализированные инструменты

| Категория | Инструменты | Зависимости |
|-----------|-------------|-------------|
| **Файлы/FS** | file_tools.py, git_tools.py | filesystem MCP |
| **Мультимедиа** | vision_tools.py, ocr_tools.py, voice_tools.py, screen_tools.py | OpenCV, Whisper, TTS |
| **Память** | memory_tools_v2.py | MemoryStore (SQLite) |
| **Beads** | beads_tools.py | beads API |
| **Документы** | document_tools.py, markdown_tools.py | pandas, markdown-it |
| **Система** | emergency_tools.py, input_tools.py | subprocess, asyncio |

---

## Интеграция с AgentFactory

**Доступ в инструментах:**
```python
def my_tool(context: GridRunContext):
    factory = context.factory  # AgentFactory
    memory = factory.memory_store
    # Вызов подагента
    sub_agent = await factory.create_dynamic_agent(...)
```

**Кэширование:** AgentFactory кэширует инструменты (`_tool_cache`).

**Auto-run:** `agent_config.auto_run_tools` для инициализации (beads_init).

---

*Обзор на основе структуры tools/ (17 файлов, ~300KB кода). Для детальной документации — читайте исходники.*
