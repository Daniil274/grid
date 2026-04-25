# Agent Factory

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Classes](#classes)
   - [StreamObserver](#streamobserver-protocol)
   - [ConsoleStreamObserver](#consolestreamobserver)
   - [AutoRunToolContext](#autoruntoolcontext)
   - [GridRunContext](#gridruncontext)
   - [AgentFactory](#agentfactory)
4. [AgentFactory Methods](#agentfactory-methods)
5. [Usage Examples](#usage-examples)
6. [Integrations](#integrations)

---

## Overview

**Agent Factory** is the central component of the Grid system, responsible for creating, configuring, and managing agents based on the OpenAI Agents SDK.

**Main Responsibilities:**
- Creating agents from configuration (`config.yaml`) or dynamically
- Caching agents for reuse
- Managing dialog context and memory sessions
- Integrating with MCP servers (Model Context Protocol)
- Tracing and logging agent execution
- Supporting real-time streaming events
- Integration with Telegram for progress notifications

**File:** `core/agent_factory.py` (2572 lines)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        AgentFactory                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐    │
│  │Config       │  │ContextManager│  │MemoryStore (SQLite) │    │
│  │(config.yaml)│  │(dialog history)│ │(long/short term memory)│ │
│  └─────────────┘  └──────────────┘  └─────────────────────┘    │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐    │
│  │SkillManager │  │ContainerMgr  │  │MCP Server Manager   │    │
│  │(skills/md)  │  │(Docker iso)  │  │(stdio MCP servers)  │    │
│  └─────────────┘  └──────────────┘  └─────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │           Agent Cache & Session Management              │   │
│  │  _agent_cache: Dict[str, Agent]                         │   │
│  │  _agent_sessions: Dict[tuple, SQLiteSession]            │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌────────────────────────────────────────┐
        │   OpenAI Agents SDK                    │
        │   ┌─────────────────────────────────┐  │
        │   │ Agent (name, instructions,      │  │
        │   │        model, tools, mcp)       │  │
        │   └─────────────────────────────────┘  │
        └────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │Function Tools│ │Agent Tools   │ │MCP Servers   │
    │(filesystem,  │ │(orchestrator,│ │(terminal,    │
    │ terminal,    │ │ task_analyst)│ │ filesystem,  │
    │ memory, etc) │ │              │ │ git, etc)    │
    └──────────────┘ └──────────────┘ └──────────────┘
```

---

## Classes

### StreamObserver (Protocol)

Protocol for components that handle agent streaming events.

```python
class StreamObserver(Protocol):
    """Protocol for components that render streaming events."""

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        """Render a streaming event. Return text fragments to append to buffers if any."""
```

**Purpose:** Defines the interface for observers of streaming events (tool calls, output, agent handoffs).

**Returns:** Text fragments to add to the response buffer (optional).

---

### ConsoleStreamObserver

Implementation of `StreamObserver` for outputting events to the console.

**Main Events:**
- `tool_called` — tool invocation (icon 🔧)
- `tool_output` — tool result (icon ✅)
- `handoff_requested` — request to hand off to another agent (icon 🔀)
- `handoff_occured` — handoff event (icon 🔁)
- `mcp_list_tools` — list of MCP tools (icon 🧩)
- `RawResponsesStreamEvent` — model text response

**Usage Example:**
```python
observer = ConsoleStreamObserver(output_writer=print)
# Passed to AgentFactory via stream_observer parameter
factory = AgentFactory(stream_observer=observer)
```

---

### AutoRunToolContext

Helper class for emulating SDK's `ToolContext` when auto-running tools.

```python
class AutoRunToolContext:
    """Mock context that mimics SDK's ToolContext for direct tool invocation."""
    def __init__(self, context: Any, tool_name: str = ""):
        self.context = context
        self.tool_name = tool_name
```

**Purpose:** Allows running tools directly (without a full Runner) for agent initialization (e.g., `beads_init`, `beads_ready`).

---

### GridRunContext

Execution context passed to the OpenAI Agents SDK Runner.

```python
@dataclass
class GridRunContext:
    """
    Runtime context object passed into Agents SDK Runner.
    """
    factory: "AgentFactory"
    context_id: Optional[str] = None
    session: Optional[Any] = None  # SQLiteSession for local agent history
    user_id: Optional[str] = None  # User identifier for workspace isolation
    agent_id: Optional[str] = None  # Agent identifier for isolation
    metadata: Optional[dict] = None  # Additional metadata from context manager
    container_id: Optional[str] = None  # Docker container ID for isolation
```

**Fields:**
| Field | Type | Description |
|------|-----|----------|
| `factory` | AgentFactory | Reference to the factory for tool access |
| `context_id` | str | Dialog context identifier |
| `session` | SQLiteSession | Memory session for the agent |
| `user_id` | str | User ID for workspace isolation |
| `max_turns` | int | Maximum number of turns in session (default from config) |
| `agent_id` | str | Agent ID for isolation |
| `metadata` | dict | Additional metadata from ContextManager |
| `container_id` | str | Docker container ID for isolation |

### AgentFactory

Main agent factory class.

#### Constructor

```python
def __init__(
    self,
    config: Optional[Config] = None,
    working_directory: Optional[str] = None,
    *,
    tracing_level: Optional[str] = "INFO",
    stream_observer: Optional[StreamObserver] = None,
    broadcaster: Optional[Any] = None,
    unified_memory: Optional[Any] = None,
    memory_store: Optional[Any] = None,
    container_id: Optional[str] = None,
    enable_mcp: Optional[bool] = True,
):
```

**Parameters:**
| Parameter | Type | Description |
|----------|-----|----------|
| `config` | Config | Configuration (created by default if None) |
| `working_directory` | str | Path to working directory |
| `tracing_level` | str | Tracing level ("INFO", "DEBUG", etc.) |
| `stream_observer` | StreamObserver | Streaming event observer |
| `broadcaster` | LiveTransparencyBroadcaster | For progress notifications in Telegram |
| `memory_store` | MemoryStore | SQLite memory (new approach) |
| `container_id` | str | Docker container ID for isolation |

**Initialized Components:**
1. **Config** — loading configuration from `config.yaml`
2. **ContextManager** — dialog history management
3. **MemoryStore** — SQLite database for long-term/short-term memory
4. **SkillManager** — skill management
5. **ContainerManager** — Docker isolation management
6. **Caches:**
   - `_agent_cache: Dict[str, Agent]` — agent cache
   - `_tool_cache: Dict[str, List[Any]]` — tool cache
   - `_mcp_servers: Dict[str, Any]` — MCP server cache
   - `_agent_sessions: Dict[tuple, SQLiteSession]` — sessions per (agent, context) pair

---

## AgentFactory Methods

### create_agent

Create or get a cached agent from configuration.

```python
async def create_agent(
    self, 
    agent_key: str, 
    context_path: Optional[str] = None,
    force_reload: bool = False
) -> Agent:
```

**Parameters:**
| Parameter | Type | Description |
|----------|-----|----------|
| `agent_key` | str | Agent key from `config.yaml` |
| `context_path` | str | Path to context file (optional) |
| `force_reload` | bool | Force recreation (bypass cache) |

**Returns:** `Agent` — OpenAI Agents SDK Agent instance.

**Creation Process:**
1. Check cache (if not `force_reload`)
2. Load agent config and model
3. Validate provider API key
4. Create OpenAI client (with proxy support)
5. Model selection:
   - `OpenAIResponsesModel` — for reasoning models with Responses API
   - `VisionChatCompletionsModel` — standard Chat Completions
6. Tool collection (function + agent tools)
7. Create MCP servers (if specified in config)
8. Build instructions with context
9. Create `Agent` SDK
10. Save to cache

**Example:**
```python
factory = AgentFactory()
agent = await factory.create_agent("chat_agent")
```

---

### create_dynamic_agent

Create a dynamic agent "on the fly" (without writing to `config.yaml`).

```python
async def create_dynamic_agent(
    self,
    *,
    name: str,
    instructions: str,
    model_key: Optional[str] = None,
    tool_names: Optional[List[str]] = None,
    mcp_tool_names: Optional[List[str]] = None,
) -> Agent:
```

**Parameters:**
| Parameter | Type | Description |
|----------|-----|----------|
| `name` | str | Agent name |
| `instructions` | str | Agent instructions (prompt) |
| `model_key` | str | Model key (from `config.yaml`) |
| `tool_names` | List[str] | List of function/agent tools |

**Returns:** `Agent` — OpenAI Agents SDK Agent instance.

**Example:**
```python
agent = await factory.create_dynamic_agent(
    name="debug_helper",
    instructions="You help debug Python code.",
    model_key="gpt-4o",
    tool_names=["file_read", "git_diff"],
)
```

---

### run_agent

Run agent with input.

```python
async def run_agent(
    self,
    agent_key: str,
    input_text: str,
    context_id: Optional[str] = None,
    user_id: str = "default",
    session_id: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> str:
```

**Parameters:**
| Parameter | Type | Description |
|----------|-----|----------|
| `agent_key` | str | Agent key |
| `input_text` | str | Input text |
| `context_id` | str | Context for history continuation |
| `max_turns` | int | Maximum turns |

**Returns:** `str` — agent response text.

---

### emit_progress

Send a progress notification to Telegram (if broadcaster is configured).

```python
async def emit_progress(
    self,
    event_type: str,
    agent_name: str,
    content: str = "",
    details: Optional[dict] = None,
) -> None:
```

**Parameters:**
| Parameter | Type | Description |
|----------|-----|----------|
| `event_type` | str | Event type (tool_call, agent_start, etc.) |
| `agent_name` | str | Agent name |
| `content` | str | Event content |
| `details` | dict | Event details |

---

### get_context_manager

Get the ContextManager instance.

```python
def get_context_manager(self) -> ContextManager:
    ...
```

---

### get_memory_store

Get the MemoryStore instance.

```python
def get_memory_store(self) -> Optional[MemoryStore]:
    ...
```

---

## Usage Examples

### Basic Usage

```python
from core.agent_factory import AgentFactory

async def main():
    factory = AgentFactory()
    
    # Run agent
    response = await factory.run_agent(
        agent_key="chat_agent",
        input_text="What files are in the current directory?",
    )
    print(response)
```

### With Streaming Observer

```python
from core.agent_factory import AgentFactory, ConsoleStreamObserver

observer = ConsoleStreamObserver(output_writer=print)
factory = AgentFactory(stream_observer=observer)

response = await factory.run_agent("chat_agent", "Analyze the project structure.")
```

### With Telegram Notifications

```python
from examples.telegram_bot.live_transparency import LiveTransparencyBroadcaster

broadcaster = LiveTransparencyBroadcaster(channel="telegram_progress")
factory = AgentFactory(broadcaster=broadcaster)

response = await factory.run_agent("git_agent", "Show git log.")
```

---

## Integrations

- **Telegram**: Progress notifications through LiveTransparencyBroadcaster
- **MCP Servers**: Standardized tools (terminal, filesystem, git, github)
- **Docker**: Workspace isolation via ContainerManager
- **MemoryStore**: SQLite-based long/short-term memory
- **SkillManager**: Skills from Markdown files
- **OpenAI Agents SDK**: Core runtime

---

*Documentation based on core/agent_factory.py (2572 lines).*
