## Grid Agent System

## Оркестратор (динамический мета-агент)

В проект добавлен базовый механизм “динамических агентов” и мета‑инструмент `orchestrate`, который умеет:
- создавать **временных под‑агентов** на лету (без записи в `config.yaml`);
- “раздавать” им набор инструментов (по ключам `tools:` из конфигурации)

![Агенты orchestration pipeline](assets/agents.png)
### Как включить

1) Добавьте инструмент `orchestrate` в `tools:` конфигурации (тип `function`):

```yaml
tools:
  orchestrate:
    type: "function"
    name: "orchestrate"
```

2) Дайте этот tool агенту (например, вашему `orchestrator`/`coordinator`):

```yaml
agents:
  orchestrator:
    # ...
    tools: ["orchestrate", "...другие..."]
```

3) Вызовите агента и попросите его использовать `orchestrate(goal=...)`.

Возвращаемый результат — JSON, в котором есть `final` (итог).

An orchestration system for AI agents focused on engineering tasks.

### Purpose
- **Orchestration**: hierarchical coordination of specialized agents (files, Git, task analysis) with efficient context management.
- **Tools**: a unified tool layer (filesystem, Git, MCP) with optimization for small open-source models.
- **Observability**: a unified logger, tool call tracing, and agent session persistence.
- **Efficiency**: smart delegation and context optimization for resource-constrained models.

## Capabilities
- **Agents**: configurable profiles (model, tools, prompt). Support for subagents as tools (`call_*`).
- **Tools**: file read/write/search, Git operations, MCP integration.
- **Multimodal**: full support for images in messages (base64, URLs, file paths) via OpenAI Vision API format.
- **Context**: context propagation between agents and tools, memory sessions (`SQLiteSession`).
- **Logging**: unified structured logs, tool call journal, metric collection.

## Installation
1) Clone and environment
```
git clone <repo-url>
cd agents
python -m venv .venv
# Windows PowerShell
. .venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate
```
2) Dependencies
```
pip install -r requirements.txt           # core and CLI
pip install -r requirements-api.txt       # API dependencies (FastAPI/uvicorn, etc.)
```
3) Configuration
```
copy config.yaml.example config.yaml       # Windows
# or
cp config.yaml.example config.yaml         # Linux/macOS
```
Fill in `config.yaml` for your environment (see the “Configuration” section).

4) Environment variables
Create a `.env` (following `.env.example`, if present) and set API keys, or use environment variables corresponding to `providers.*.api_key_env`.

### Docker Installation
You can also run the system in a Docker container:

1) Build the image:
```bash
docker build -t agent-system .
```

2) Run the container with your current directory mounted (agent sees it as root "/"):
```bash
# Windows PowerShell
docker run -it --rm -v "${PWD}:/workspace" -w /workspace --env-file .env agent-system

# Linux/macOS
docker run -it --rm -v "$(pwd):/workspace" -w /workspace --env-file .env agent-system
```

Note for Windows/pytest: add the current folder to `PYTHONPATH` for the session:
```
$env:PYTHONPATH = "."
```

## Running
### CLI (local agent)
- Chat mode:
```
python agent_chat.py
```
- Single request:
```
python agent_chat.py --message "List files in current directory"
```
- Explicit agent:
```
python agent_chat.py --agent file_agent --message "Read config.yaml"
```
- **With images** (vision agents):
```
python agent_chat.py --agent vision_analyzer --message "Describe & image.jpg"
```
Multiple images:
```
python agent_chat.py -a vision_analyzer -m "Compare & img1.png & img2.jpg"
```
See [docs/CLI_IMAGES.md](docs/CLI_IMAGES.md) for full CLI image support guide.

### Telegram Bot
The system includes a Telegram bot interface with full file handling support:

```bash
# Set your bot token in .env
TELEGRAM_BOT_TOKEN=your_token_here

# Run the Telegram server
python telegram_server.py
```

**Features:**
- 💬 Natural language interaction with agents
- 📤 Send files (documents, images, audio, video) to the bot
- 📥 Receive files from agents using `/sendfile` command
- 💾 Per-user workspace isolation
- 🧠 Integrated memory system
- 🔄 Real-time agent status updates

**File Support:**
- Documents (up to 20 MB)
- Images/Photos (automatic format handling)
- Audio/Voice messages
- Video (up to 50 MB)

**Documentation:**
- [Telegram Files Guide](docs/TELEGRAM_FILES.md) - Complete file handling documentation
- [Quick Start](docs/TELEGRAM_FILES_QUICKSTART.md) - Testing and troubleshooting guide
## Configuration
Configuration is defined in `config.yaml` and validated via Pydantic (`schemas.py`).

### General settings (`settings`)
- `default_agent`: default agent.
- `max_history`: context history size.
- `max_turns`: turn limit for agents (applies to subagents-tools as well).
- `agent_timeout`: agent execution timeout (sec).
- `working_directory`: process working directory (set on configuration start).
- `config_directory`: configuration directory.
- `allow_path_override`: allow changing the working directory from code.
- `mcp_enabled`: enable MCP globally.
- `agent_logging`: agent logging parameters.

### Providers (`providers`)
- Base URL, API key (via environment variable), timeouts, and retries.

### Models (`models`)
- Model identifier, provider, temperature, token limits, etc.
- `use_responses_api`: a flag to use the Responses API for reasoning models (specified in the model config, not hard-coded in code). If a provider does not support it, the system will automatically and quietly (with deduplicated warnings) fall back to Chat Completions [[memory:5609856]].

Example model:
```yaml
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
    temperature: 0.7
    max_tokens: 4000
    use_responses_api: false
```

### Agents (`agents`)
- Name, model, tools, base/custom prompt, description.
- Tool types: `function` (direct tools), `agent` (invokes a subagent via `call_*`), `mcp` (tools from an MCP server).

Example agent:
```yaml
agents:
  file_agent:
    name: "File Agent"
    model: "gpt-4"
    tools: ["file_read", "file_write", "file_list"]
    base_prompt: "with_files"
    description: "File operations specialist"
```

### Tools (`tools`)
- `function`: connected from `tools/file_tools.py`, `tools/git_tools.py`, and the registry in `tools/function_tools.py`.
- `agent`: creates a `call_<agent_key>` tool to invoke a subagent with context passing. Supports context sharing parameters (`context_strategy`, `context_depth`, `include_tool_history`).
- `mcp`: third-party MCP server tools (enabled when `mcp_enabled`).

Specifics of agent-tools:
- Accept input in the `input` field. For compatibility, aliases `task`, `message`, `prompt` are supported and are automatically normalized to `input`.
- Subagent execution inherits `max_turns` from `settings.max_turns` and uses its own `SQLiteSession`.


## Conversation Context IDs
The system assigns a lightweight context identifier to every agent turn. This allows
humans, tools, and downstream agents to resume a previous conversation explicitly.

- **Automatic creation** – Every call to `AgentFactory.run_agent` without an override
  starts a fresh context session and appends a line `Контекст ID: ctx-xxxxxx` to the
  final answer.
- **Manual reuse** – Pass `context_id=...` when invoking `run_agent` (or include the
  same string in API/CLI requests) to continue the exact conversation state.
- **Storage** – Conversation history, executions, and metadata are kept per-context
  inside `core/context.py` and persisted (when enabled) with the ID as the key.
- **CLI helpers** – `agent_chat.py` shows the current ID after each response, starts
  a fresh context for every prompt by default, lists known contexts via the `contexts`
  command, and lets you reuse a session with `use <id>` (or by embedding `ctx-…`
  in the message)
This mechanism prevents accidental cross-talk between independent requests while
keeping it trivial to stitch conversations back together when needed.

## Security
- Security-aware factory (`core/security_agent_factory.py`) applies guardrails to specified agents.
- Middleware: authentication, request security, rate limiting.
- Git commands run with parameter validation and timeouts; filesystem operations verify path existence/type.

## Multimodal Support (Images)

The system supports sending images to vision-capable agents from multiple sources:
- **Base64-encoded data** - Embedded in messages
- **URLs** - HTTP/HTTPS image links
- **Local files** - File paths on local filesystem
- **MCP tools** - Images from MCP server responses

### Quick Example

```python
from utils.multimodal_converter import MultimodalConverter

# Create message with image
message = MultimodalConverter.create_multimodal_message(
    role="user",
    text="Опиши это изображение",
    image_sources=["path/to/image.jpg"]
)

# Run vision agent
result = await factory.run_agent("vision_analyzer", str(message))
```


**Full Documentation**: See [docs/MULTIMODAL_GUIDE.md](docs/MULTIMODAL_GUIDE.md) for complete guide, examples, and API reference.

**Examples**: Run `python examples/vision_example.py` for working examples.

## Testing
```
# In Windows before running tests in PowerShell:
$env:PYTHONPATH = "."
pytest -q
```
Coverage:
```
pytest --cov=. --cov-report=html
```

## Troubleshooting
- `ModuleNotFoundError: No module named 'api'` when running `pytest` in Windows — set `PYTHONPATH`:
  - For the session: `$env:PYTHONPATH = "."`
- `Max turns exceeded` — increase `settings.max_turns` or simplify the task.
- Tool schema error: use `input` (or `task`/`message`/`prompt`, which are normalized automatically).
- Responses API warnings — provider does not support it; set `use_responses_api: false` for the model or use a compatible provider.

## License
MIT. See the `LICENSE` file.
