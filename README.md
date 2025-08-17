## Grid Agent System

An orchestration system for AI agents focused on engineering tasks, with clear architecture, strict logging, security, and an OpenAI-compatible API.

### Purpose
- **Orchestration**: hierarchical coordination of specialized agents (files, Git, task analysis) with efficient context management.
- **Tools**: a unified tool layer (filesystem, Git, MCP) with optimization for small open-source models.
- **API**: OpenAI-compatible endpoints for integration with external clients and tools.
- **Observability**: a unified logger, tool call tracing, and agent session persistence.
- **Efficiency**: smart delegation and context optimization for resource-constrained models.

## Capabilities
- **Agents**: configurable profiles (model, tools, prompt). Support for subagents as tools (`call_*`).
- **Tools**: file read/write/search, Git operations, MCP integration.
- **Context**: context propagation between agents and tools, memory sessions (`SQLiteSession`).
- **Security**: security-aware factory, middleware for authentication, rate limiting, and basic guardrails.
- **API**: Chat Completions/Completions (OpenAI-compatible), agents, and system routes.
- **Logging**: unified structured logs, tool call journal, metric collection.

## Architecture
- `core/`
  - `config.py`: loads and validates `config.yaml`, manages paths, providers, models, and agents.
  - `agent_factory.py`: creates/caches agents, assembles tools, runs via `Runner.run`, unified logging, sessions.
  - `security_agent_factory.py`: factory extension with security guardrails for selected agent types.
  - `context.py`: manages dialogue and execution context.
- `tools/`
  - `file_tools.py`: filesystem operations.
  - `git_tools.py`: Git wrappers with validation and logging.
  - `function_tools.py`: integrator and registry of available tools, aliases, statistics.
  - `mcp.py`: MCP integration (if enabled).
- `api/`
  - `main.py`: FastAPI app, middleware, error handlers, routes.
  - `routers/`: OpenAI-compatible endpoints, agent CRUD, system endpoints.
- `utils/`: unified logger, metrics, formatters, exceptions.
- `schemas.py`: Pydantic schemas for configuration and execution.

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

### API (FastAPI)
- Helper launcher:
```
python start_api.py
```
- Or directly via uvicorn:
```
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```
Docs: `http://localhost:8000/docs`
Health check: `http://localhost:8000/health`

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

## API (OpenAI-compatible)
Main routes (prefix `/v1`):
- `POST /chat/completions` — OpenAI Chat Completions compatible. Supports `stream`.
- `POST /completions` — legacy format, converted to internal Chat Completions and back.
- `GET /agents` etc. — list and details of agents.
- `GET /system/health` — health check.

Format and context converter is located at `api/utils/openai_converter.py` (routing in `api/routers/openai_compatible.py`).

## Logging and Observability
- Unified logger (`utils/unified_logger.py`) for agent runs, tool calls, and results.
- Structured logs, writing to `logs/`, agent sessions stored in `logs/agent_sessions.db`.
- Deduplication of Responses API warnings (reduces log noise).
- On API startup, the context is cleared and saved context files are removed.

## Security
- Security-aware factory (`core/security_agent_factory.py`) applies guardrails to specified agents.
- Middleware: authentication, request security, rate limiting.
- Git commands run with parameter validation and timeouts; filesystem operations verify path existence/type.

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