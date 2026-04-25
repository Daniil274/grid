# Tools Overview

## Table of Contents
1. [Introduction](#introduction)
2. [Function Tools](#function-tools)
3. [Agent Tools](#agent-tools)
4. [Specialized Tools](#specialized-tools)
5. [Integration with AgentFactory](#integration-with-agentfactory)

---

## Introduction

**Tools** — the `tools/` directory contains instruments for agents:
- **Function Tools**: Direct Python functions (fast, deterministic)
- **Agent Tools**: Sub-agents with their own models/instructions
- **MCP Tools**: External stdio servers (terminal, filesystem)

**Total count:** 17 modules.

**Access:** Via `GridRunContext.factory` or `config.yaml → tools`.

---

## Function Tools

| File | Size | Description | Example Functions |
|------|--------|----------|-----------------|
| beads_tools.py | 30KB | Beads task management (workspace) | beads_show, beads_update, list_files |
| document_tools.py | 11KB | Document processing (Markdown, PDF) | extract_text, generate_summary |
| emergency_tools.py | 10KB | Emergency tools (shutdown, panic) | emergency_shutdown |
| file_tools.py | 25KB | File operations (read/write/search) | read_file, write_file, search_files |
| function_tools.py | 11KB | Universal tools (eval, call) | safe_eval, dynamic_call |
| git_tools.py | 65KB | Git operations (clone, commit, diff) | git_clone, git_status, git_diff |
| input_tools.py | 4KB | Data input (prompt, confirm) | user_input, yes_no |
| markdown_tools.py | 5KB | Markdown processing (render, parse) | md_to_html, parse_table |
| memory_tools_v2.py | 17KB | Memory access (store/retrieve) | get_memory, store_fact |
| ocr_tools.py | 7KB | OCR text recognition | extract_text_from_image |
| orchestrator_tools.py | 13KB | Sub-agent orchestration | delegate_task, analyze_plan |
| screen_tools.py | 2KB | Screenshots and screen capture | capture_screen |
| skill_tools.py | 8KB | Skill execution (skills/md) | execute_skill |
| vision_tools.py | 7KB | Computer vision | describe_image, detect_objects |
| examples/telegram_bot/voice_tools.py | 7KB | Voice operations (STT/TTS) | transcribe_audio, synthesize_speech |

---

## Agent Tools

Tools using sub-agents (type `agent` in config.yaml):
- **orchestrator_tools.py**: Task orchestrator
- **skill_tools.py**: Skill executor
- **task_analyst.py** (mentioned in config): Task analysis

**Configuration:**
```yaml
tools:
  orchestrator:
    type: agent
    prompt_addition: \"Analyze the plan...\"
```

---

## Specialized Tools

| Category | Tools | Dependencies |
|-----------|-------------|-------------|
| **Files/FS** | file_tools.py, git_tools.py | filesystem MCP |
| **Multimedia** | vision_tools.py, ocr_tools.py, screen_tools.py | OpenCV, Whisper, TTS |
| **Memory** | memory_tools_v2.py | MemoryStore (SQLite) |
| **Beads** | beads_tools.py | beads API |
| **Documents** | document_tools.py, markdown_tools.py | pandas, markdown-it |
| **System** | emergency_tools.py, input_tools.py | subprocess, asyncio |

---

## Integration with AgentFactory

**Access in tools:**
```python
def my_tool(context: GridRunContext):
    factory = context.factory  # AgentFactory
    memory = factory.memory_store
    # Sub-agent call
    sub_agent = await factory.create_dynamic_agent(...)
```

**Caching:** AgentFactory caches tools (`_tool_cache`).

**Auto-run:** `agent_config.auto_run_tools` for initialization (beads_init).

---

*Overview based on tools/ structure (17 files, ~300KB code). For detailed documentation — read the source code.*
