# System Tools Architecture: Introspection & Meta-Control

## Problem Statement

Необходимо добавить системные инструменты, которые позволят агентам:
1. Получать список других агентов
2. Изучать инструкции и возможности других агентов
3. Просматривать доступные инструменты с описаниями
4. Просматривать скиллы агентов
5. Получать справку по работе системы

## Design Principles

### 1. Separation of Concerns
```
┌─────────────────────────────────────────────────────────────────┐
│                      GRID AGENT SYSTEM                          │
├─────────────────────────────────────────────────────────────────┤
│  User Tools           │  System Tools            │  Meta Tools  │
│  (file, git, etc.)    │  (introspection)         │  (orchestrate)│
├───────────────────────┼──────────────────────────┼──────────────┤
│  Работа с внешним     │  Самоанализ системы      │  Управление  │
│  миром                │  и мета-информация       │  агентами    │
└───────────────────────┴──────────────────────────┴──────────────┘
```

### 2. Information Flow
```
Agent A                     System Tools                    Config/Registry
   │                            │                               │
   │  system_list_agents()      │                               │
   │ ────────────────────────>  │  config.agents                │
   │                            │ ───────────────────────────>  │
   │  <─────────────────────────│  <────────────────────────────│
   │  [agent1, agent2, ...]     │                               │
   │                            │                               │
   │  system_get_agent_info()   │                               │
   │ ────────────────────────>  │  config.get_agent(key)        │
   │                            │ ───────────────────────────>  │
   │  <─────────────────────────│  <────────────────────────────│
   │  {name, tools, prompt...}  │                               │
```

## Proposed Architecture

### Module Structure
```
tools/
├── system_tools.py          # NEW: System introspection tools
├── function_tools.py        # Registry (add SYSTEM_TOOLS)
├── orchestrator_tools.py    # Meta-orchestration (existing)
└── ...
```

### System Tools API

#### 1. `system_list_agents` - List All Agents
```python
@function_tool
async def system_list_agents(
    context: RunContextWrapper,
    include_dynamic: bool = False,      # Include dynamically created agents
    filter_by_capability: str = None,   # Filter by tool capability
) -> str:
    """
    Returns list of all configured agents with their descriptions.

    Returns JSON:
    {
        "agents": [
            {
                "key": "chat_agent",
                "name": "Chat Agent",
                "description": "Main chat interface",
                "model": "qwen-3.5-397b",
                "tools_count": 5,
                "mcp_enabled": true
            },
            ...
        ],
        "total": 3,
        "dynamic_agents": 2  # if include_dynamic=True
    }
    """
```

#### 2. `system_get_agent_info` - Detailed Agent Information
```python
@function_tool
async def system_get_agent_info(
    context: RunContextWrapper,
    agent_key: str,                     # Agent identifier
    include_prompt: bool = True,        # Include system prompt
    include_tools_details: bool = True, # Include tool descriptions
) -> str:
    """
    Returns detailed information about a specific agent.

    Returns JSON:
    {
        "key": "chat_agent",
        "name": "Chat Agent",
        "description": "Main chat interface",
        "model": {
            "key": "qwen-3.5-397b",
            "name": "qwen/qwen3.5-397b-a17b",
            "provider": "openrouter",
            "temperature": 0.7
        },
        "tools": [
            {"name": "file_read", "description": "Read file contents"},
            {"name": "orchestrate", "description": "Create dynamic agent"}
        ],
        "system_prompt": "You are a helpful assistant...",  # if include_prompt
        "auto_run_tools": [{"name": "skill_list", "parameters": {}}],
        "mcp_enabled": true
    }
    """
```

#### 3. `system_list_tools` - List All Tools
```python
@function_tool
async def system_list_tools(
    context: RunContextWrapper,
    category: str = None,               # Filter: file, git, memory, system, etc.
    include_mcp: bool = True,           # Include MCP tools
    include_project: bool = True,       # Include project tools
) -> str:
    """
    Returns list of all available tools in the system.

    Returns JSON:
    {
        "tools": {
            "file": [
                {"name": "file_read", "description": "...", "type": "function"},
                ...
            ],
            "git": [...],
            "memory": [...],
            "orchestration": [...],
            "system": [...]
        },
        "total": 45,
        "by_type": {"function": 40, "mcp": 3, "agent": 2}
    }
    """
```

#### 4. `system_get_tool_info` - Detailed Tool Information
```python
@function_tool
async def system_get_tool_info(
    context: RunContextWrapper,
    tool_name: str,
    include_schema: bool = True,        # Include parameter schema
) -> str:
    """
    Returns detailed information about a specific tool.

    Returns JSON:
    {
        "name": "file_read",
        "description": "Read file contents from the filesystem",
        "type": "function",
        "parameters": {
            "path": {"type": "string", "required": true, "description": "..."},
            "encoding": {"type": "string", "required": false, "default": "utf-8"}
        },
        "returns": "File contents as string",
        "examples": ["file_read(path='/etc/hosts')"],
        "category": "file",
        "aliases": ["read_file"]
    }
    """
```

#### 5. `system_get_skills` - Agent Skills
```python
@function_tool
async def system_get_skills(
    context: RunContextWrapper,
    scope: str = "current",             # "current", "user", "system"
) -> str:
    """
    Returns available skills/knowledge bases.

    Returns JSON:
    {
        "skills": [
            {
                "name": "python-best-practices",
                "description": "Python coding guidelines",
                "tags": ["python", "coding"],
                "size": "2.3 KB"
            },
            ...
        ],
        "total": 5
    }
    """
```

#### 6. `system_help` - System Documentation
```python
@function_tool
async def system_help(
    context: RunContextWrapper,
    topic: str = "overview",            # overview, agents, tools, memory, etc.
) -> str:
    """
    Returns system documentation and help.

    Topics:
    - overview: General system architecture
    - agents: How agents work, how to orchestrate
    - tools: Tool categories and usage
    - memory: Memory system (short-term, long-term, skills)
    - orchestration: Dynamic agent creation
    - mcp: Model Context Protocol integration

    Returns formatted markdown documentation.
    """
```

#### 7. `system_get_context` - Current Execution Context
```python
@function_tool
async def system_get_context(
    context: RunContextWrapper,
    include_history: bool = False,
) -> str:
    """
    Returns information about current execution context.

    Returns JSON:
    {
        "context_id": "ctx-abc12345",
        "current_agent": "chat_agent",
        "pipeline_id": "pipeline-xyz789",  # if in orchestrated pipeline
        "depth": 2,                        # orchestration depth
        "parent_agent": "orchestrator",    # if called from orchestrate
        "working_directory": "/path/to/work",
        "session_id": "session-123"
    }
    """
```

## Implementation Strategy

### Phase 1: Core Introspection
1. Create `tools/system_tools.py`
2. Implement `system_list_agents`, `system_list_tools`, `system_help`
3. Register in `function_tools.py` as `SYSTEM_TOOLS`
4. Add to config.yaml tools section

### Phase 2: Detailed Information
1. Implement `system_get_agent_info`, `system_get_tool_info`
2. Add parameter schema extraction from @function_tool
3. Add caching for expensive operations

### Phase 3: Context & Skills
1. Implement `system_get_skills`, `system_get_context`
2. Integration with SkillManager
3. Integration with PipelineRegistry

### Phase 4: Advanced Features
1. `system_search` - Search across agents, tools, skills
2. `system_capabilities` - What can this system do?
3. `system_recommend_agent` - Suggest best agent for task

## Access Control Considerations

### Option A: Open Access (Recommended for v1)
All agents can see all system information. Simple, promotes collaboration.

### Option B: Role-Based Access
```yaml
tools:
  system_get_agent_info:
    type: function
    access_control:
      allow_self: true          # Agent can always see itself
      allow_others: ["orchestrator", "supervisor"]
```

### Option C: Information Hiding
Some agents marked as "private" - their prompts hidden from others.

## Config Integration

```yaml
# config.yaml additions

tools:
  # System introspection tools
  system_list_agents:
    type: function
    description: "List all configured agents"
  system_get_agent_info:
    type: function
    description: "Get detailed agent information"
  system_list_tools:
    type: function
    description: "List all available tools"
  system_get_tool_info:
    type: function
    description: "Get detailed tool information"
  system_get_skills:
    type: function
    description: "List available skills"
  system_help:
    type: function
    description: "System documentation and help"
  system_get_context:
    type: function
    description: "Current execution context"

agents:
  chat_agent:
    tools:
      - file_read
      - orchestrate
      - system_list_agents    # NEW
      - system_help           # NEW
```

## Usage Examples

### Example 1: Agent discovers other agents
```
User: "What other agents are available?"

Agent uses: system_list_agents()
→ Returns list of agents

Agent: "I found 3 agents:
1. code_agent - Specialist for programming tasks
2. file_agent - File operations expert
3. research_agent - Web research and analysis"
```

### Example 2: Agent studies another agent before delegation
```
User: "Help me refactor this code"

Agent uses: system_get_agent_info(agent_key="code_agent", include_tools_details=true)
→ Returns code_agent capabilities

Agent: "I see code_agent has access to terminal, git, and can run tests.
Let me delegate this to code_agent via orchestrate()..."

Agent uses: orchestrate(task="...", model_key="code_agent's model")
```

### Example 3: Agent explains system capabilities
```
User: "What can you do?"

Agent uses: system_help(topic="overview")
Agent uses: system_list_tools(category=None)

Agent: "This system has the following capabilities:
- File operations (read, write, search)
- Git version control
- Memory (save/recall information)
- Dynamic agent creation
- Vision (analyze images)
..."
```

## Benefits

1. **Self-Discovery**: Agents can learn about the system without hardcoding
2. **Dynamic Adaptation**: New agents/tools automatically discoverable
3. **Better Orchestration**: Informed decisions about delegation
4. **Debugging**: Easy to understand current system state
5. **Documentation**: Built-in help always up to date

## Technical Notes

### Getting Factory from Context
```python
def _get_factory_from_context(ctx: RunContextWrapper) -> Any:
    raw = getattr(ctx, "context", None)
    return getattr(raw, "factory", None)
```

### Getting Config
```python
factory = _get_factory_from_context(context)
if factory:
    config = factory.config  # Config instance
    agents = config.config.agents  # Dict[str, AgentConfig]
    tools = config.config.tools    # Dict[str, ToolConfig]
```

### Tool Schema Extraction
```python
from agents import FunctionTool

def get_tool_schema(tool_func):
    if hasattr(tool_func, '__wrapped__'):
        # Get original function signature
        import inspect
        sig = inspect.signature(tool_func.__wrapped__)
        # Extract parameters...
```
