# Project Configuration

## Table of Contents
1. [config.yaml Structure](#configyaml-structure)
2. [Section `settings`](#section-settings)
3. [Section `isolation`](#section-isolation)
4. [Section `telegram`](#section-telegram)
5. [Section `voice`](#section-voice)
6. [Section `providers`](#section-providers)
7. [Section `models`](#section-models)
8. [Section `checkers`](#section-checkers)
9. [Section `tools`](#section-tools)
10. [Environment Variables](#environment-variables)
11. [Configuration Examples](#configuration-examples)

---

## `config.yaml` Structure

The `config.yaml` file consists of several main sections:

| Section | Description |
|--------|----------|
| `settings` | Global system parameters. |
| `isolation` | Agent isolation settings (Docker). |
| `telegram` | Telegram bot parameters. |
| `voice` | Voice input/output configuration. |
| `providers` | API provider descriptions (lm-studio, openrouter, anthropic, etc.). |
| `models` | List of available models linked to providers. |
| `checkers` | Checker configuration (audit). |
| `tools` | Set of MCP tools and agent-tools. |

---

## Section `settings`

Global system settings.

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `default_agent` | `string` | Identifier of the default agent. | `"chat_agent"` |
| `max_history` | `int` | Maximum number of messages stored in history. | `50` |
| `max_turns` | `int` | Maximum number of turns in a session. | `100` |
| `agent_timeout` | `int` (sec) | Agent execution timeout. | `600` |
| `debug` | `bool` | Debug flag. | `false` |
| `max_tool_output_tokens` | `int` | Limit on tokens produced by tools (0 = no limit). | `12000` |
| `mcp_enabled` | `bool` | Enable MCP subsystem. | `true` |
| `working_directory` | `string` | Path to the project working directory. | `"./"` |
| `config_directory` | `string` | Path to the configuration directory. | `"./"` |
| `logs_directory` | `string` | Path to the log directory. | `~/.grid/logs` |
| `allow_path_override` | `bool` | Allow overriding the working directory path at startup. | `true` |
| `agent_logging` | section | Agent logging parameters | |
| `image_processing` | section | Image processing parameters | |

### `agent_logging` Parameters

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Enable logging. | `true` |
| `level` | `string` | Logging level (`full`, `minimal`, …). | `"full"` |
| `save_prompts` | `bool` | Save prompts. | `true` |
| `save_conversations` | `bool` | Save conversations. | `true` |
| `save_executions` | `bool` | Save execution results. | `true` |

### `image_processing` Parameters

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Enable image processing. | `true` |
| `auto_resize` | `bool` | Auto-resize images. | `true` |
| `max_width` | `int` | Maximum width (px). | `1920` |
| `max_height` | `int` | Maximum height (px). | `1080` |
| `max_file_size_mb` | `int` | Maximum file size (MB). | `10` |
| `jpeg_quality` | `int` | JPEG quality (0‑100). | `85` |

---

## Section `isolation`

Agent isolation settings.

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Enable agent isolation. | `true` |
| `type` | `string` | Isolation type (`docker`, `process`, …). | `"docker"` |
| `image` | `string` | Docker image used for isolation. | `"grid-agent:latest"` |

---

## Section `telegram`

Telegram bot configuration.

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `token_env` | `string` | Environment variable name containing the bot token. | `"TELEGRAM_BOT_TOKEN"` |
| `polling_timeout` | `int` | Polling request timeout (sec). | `30` |
| `workspace_path` | `string` | Path to the workspace directory within the project. | `"./workspace"` |
| `persist_path` | `string` | Path to the directory where bot data is saved. | `"./data"` |
| `max_message_history` | `int` | Number of recent messages stored in memory. | `15` |
| `memory_commands_enabled` | `bool` | Enable memory commands. | `true` |
| `enable_transparency` | `bool` | Show transparency (display internal steps). | `false` |
| `show_tool_calls` | `bool` | Show tool calls in messages. | `false` |
| `allowed_users` | `list|null` | List of Telegram IDs with access. `null` = all users. | `null` |
| `max_concurrent_tasks_per_user` | `int` | Maximum concurrent tasks per user. | `1` |
| `progress_update_interval` | `float` | Progress update interval (sec). | `2.0` |

---

## Section `voice`

Voice input/output configuration (STT/TTS). The `voice` section also contains nested `stt` and `tts` sections with their parameters.
Voice input/output supports two sub-modules: STT (Speech‑to‑Text) and TTS (Text‑to‑Speech).

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Enable voice input/output. | `true` |

### STT Parameters (Speech‑to‑Text)

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `model_size` | `string` | Speech‑to‑Text model size. | `"large-v3"` |
| `device` | `string` | Device (`cpu`, `cuda`). | `"cuda"` |
| `compute_type` | `string` | Compute type (`int8`, `float16`, …). | `"float16"` |

### TTS Parameters (Text-to-Speech)

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `model_path` | `string` | Path to the Text‑to‑Speech model. | `"speech-text/model.pt"` |
| `speaker` | `string` | Speaker identifier. | `"xenia"` |
| `sample_rate` | `int` | Sample rate (Hz). | `48000` |

### Common Parameters

| Parameter | Type | Description | Default Value |
|----------|------|----------|----------------------|
| `reply_with_voice` | `bool` | Send voice replies. | `true` |

---

## Section `providers`

API provider parameters are specified as nested objects.

### `lm-studio`

| Parameter | Type | Description | Example |
|----------|------|----------|--------|
| `base_url` | `string` | API URL. | `"http://192.168.3.2:1234/v1"` |
| `api_key` | `string` | API key (must provide a real key if the server requires authentication). | `"lm-studio"` |
| `timeout` | `int` | Timeout (sec). | `300` |
| `max_retries` | `int` | Number of retries on error. | `3` |

### `openrouter`

| Parameter | Type | Description | Example |
|----------|------|----------|--------|
| `base_url` | `string` | API URL. | `"https://openrouter.ai/api/v1"` |
| `api_key_env` | `string` | Environment variable name with the key. | `"OPENROUTER_API_KEY"` |
| `timeout` | `int` | Timeout (sec). | `300` |
| `max_retries` | `int` | Number of retries. | `3` |
| `streaming_enabled` | `bool` | Enable streaming output. | - |

| `reasoning` | `section` | Reasoning parameters for the model (see agent_factory) | |

### `anthropic`

| Parameter | Type | Description | Example |
|----------|------|----------|--------|
| `base_url` | `string` | API URL. | `"https://api.anthropic.com/v1/"` |
| `api_key_env` | `string` | Environment variable name with the key. | `"ANTHROPIC_API_KEY"` |
| `timeout` | `int` | Timeout (sec). | `300` |
| `max_retries` | `int` | Number of retries. | `3` |
| `streaming_enabled` | `bool` | Enable streaming output. | `true` |

---

## Section `models`

Each model is described as an object with the following fields:

| Parameter | Type | Description |
|----------|------|----------|
| `provider` | `string` | Provider identifier (see `providers` section). | - |
| `temperature` | `float` | Generation temperature parameter. | - |
| `max_tokens` | `int` | Maximum tokens in the response. | - |
| `streaming_enabled` | `bool` | Enable streaming output. |

**Example:**

```yaml
models:
  kimi-k2.5:
    provider: openrouter
    temperature: 0.7
    max_tokens: 8192
    streaming_enabled: true
```

---

## Section `checkers`

Configuration of verification modules (audit/checkers) for post-processing agent output.

**Purpose:**
- Checking responses for safety, compliance, quality.
- Automatic audit before sending to the user.
- Support for multiple checkers (default_audit and custom).

**How it works:**
1. Agent generates a response.
2. If the checker is enabled (by default), the response is sent to an audit prompt.
3. The auditor model returns a verdict (pass/fail) + explanation.
4. On fail — request for regeneration or blocking.

### default_audit

Default auditor for basic checking.

| Parameter | Type | Description | Example |
|----------|-----|----------|--------|
| `prompt` | `string` | JSON structure of the prompt for the audit model. Must return `{"verdict": "pass"|"fail", "reason": "..."}` | See example below |

**Configuration Example:**
```yaml
checkers:
  default_audit:
    prompt: |
      {
        "role": "system",
        "content": [
          {
            "type": "text",
            "text": "You are a safety auditor. Analyze the agent output:\n{output}\nReturn JSON: {\"verdict\": \"pass\"|\"fail\", \"reason\": \"...\"}.\nCheck for: harm, illegal, unsafe, biased content."
          }
        ]
      }
```

**Extensions:**
- Add custom checkers: `safety_checker: {prompt: "..."}`, `bias_checker: {...}`.
- In `settings` you can add `audit_model: "gpt-4o-mini"` to select the auditor model (if not specified — default_agent.model).

---

## Section `tools`

Set of MCP tools and agent-tools.

| Tool | Parameters | Description |
|------------|-----------|----------|
| `terminal` | MCP | Terminal command execution (using `execute_command`, `read_output`, `write_input`) |
| `filesystem` | MCP | File operations with sandbox restrictions |
| `git` | MCP | Git operations |
| `github` | MCP | GitHub API operations |
| `memory` | function | Saving/retrieving memories |
| `orchestrator` | agent | Dynamic sub-agent creation for subtasks |
| `skill_tools` | agent | Executing skills from Markdown |
| `input_tools` | function | Requesting user input |
| ... | ... | ... |

---

## Environment Variables

| Variable | Description | Required |
|----------|----------|----------|
| `OPENAI_API_KEY` | OpenAI API key | Required if using OpenAI models |
| `ANTHROPIC_API_KEY` | Anthropic API key | Required for Anthropic |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | Required for Telegram features |
| `OPENROUTER_API_KEY` | OpenRouter API key | Required for OpenRouter |

---

## Configuration Examples

### Minimal Configuration

```yaml
settings:
  default_agent: chat_agent

providers:
  openai:
    api_key_env: OPENAI_API_KEY

models:
  gpt-4o:
    provider: openai
```

### Full Configuration

(Full config.yaml file) — see the actual config.yaml.

---

*Documentation based on core/config.py and config.yaml.*
