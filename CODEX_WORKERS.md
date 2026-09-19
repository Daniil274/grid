# Codex → Grid workers

Grid is exposed to Codex as a small local STDIO MCP server. Codex remains the
planner and reviewer; OpenCode-backed Grid agents perform execution work using
the tools from `examples/claude-tools/tools`.

## Codex-side setup

Add this server to Codex's `config.toml` (adjust paths if the repository moves):

```toml
[mcp_servers.grid]
command = 'C:\Users\danii\grid\.venv\Scripts\python.exe'
args = [
  'C:\Users\danii\grid\grid.py',
  'serve-mcp',
  '--config', 'C:\Users\danii\grid\config.yaml',
  '--workdir', 'C:\Users\danii\grid',
  '--provider', 'opencode',
  '--max-concurrency', '4',
  '--default-timeout', '300',
  '--max-timeout', '900',
]
startup_timeout_sec = 30
tool_timeout_sec = 330
enabled_tools = [
  'grid_get_system',
  'grid_start_agent',
  'grid_get_agents',
  'grid_wait_agents',
  'grid_interrupt_agent',
]
```

`OPENCODE_API_KEY` must be present in the environment inherited by Codex. Do not
put the key in the repository.

The 330-second MCP tool timeout is intentional: `grid_wait_agents` permits a
bounded wait of up to 300 seconds, with protocol overhead. Worker runtime is
independent and can be up to `--max-timeout`.

## The hot path

Cold start requires exactly two Codex tool calls:

1. `grid_get_system()` — returns the current OpenCode model keys, exact
   `claude-tools` names/descriptions, and runtime limits. Cache this response.
2. `grid_start_agent(model, task, tools, timeout_seconds)` — validates the
   selection, creates a background task, and immediately returns `agent_id`,
   status, and deadline.

Later, Codex calls:

- `grid_wait_agents([agent_id], timeout_seconds=180)` to sleep until agent
  completion/interruption/deadline or until the wait timer fires;
- `grid_get_agents(...)` for an intentional non-blocking status snapshot;
- `grid_interrupt_agent(agent_id, reason)` when the plan changes.

No polling loop is required. Multiple independent workers can be started with
separate `grid_start_agent` calls and waited on together with `return_when =
'first'` or `'all'`.

## Why the catalog is strict

The server publishes only models whose configured provider is `opencode` and
only actual Agents SDK `FunctionTool` objects loaded from `claude-tools`. Unknown
models or tool names fail before a worker is created. Grid's legacy orchestrator
is not part of this path.

## Manual launch

For protocol debugging, run:

```powershell
.\.venv\Scripts\python.exe grid.py serve-mcp --config config.yaml --workdir .
```

STDOUT is reserved exclusively for MCP JSON-RPC. Grid and tool startup logs are
redirected to STDERR so they cannot corrupt the Codex connection.
