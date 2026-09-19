# Codex worker policy

Codex is the planner, dispatcher, and reviewer. Delegate substantial execution
work to Grid workers when the `grid-codex-workers` MCP server is available.

## Launch contract

1. Call `grid_get_system` once per task/session and cache its model keys, tool
   names, and constraints.
2. Call `grid_start_agent` with one returned model key, a self-contained task,
   only the required returned tools, and an explicit deadline when useful.

Those are the only two actions required to launch a worker. Do not use Grid's
legacy orchestration or pipeline commands for Codex delegation.

After launch, continue planning, reviewing, or dispatching independent work.
Do not poll. At a meaningful checkpoint, call `grid_wait_agents`; it wakes on
completion, worker deadline, interruption, or its own timer. Use
`grid_get_agents` only for an intentional non-blocking snapshot and
`grid_interrupt_agent` when the plan changes.

Codex owns decomposition and final verification. A worker's report is evidence,
not proof: inspect relevant changes and run proportionate checks before declaring
the user's task complete. Give a follow-up worker a focused correction task when
verification fails.

