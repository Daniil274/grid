"""The parts AgentFactory (core.agent_factory) is made of.

Objects the factory holds - each needs only what it is given:

- ``models.ModelProvider`` (``factory.models``): model keys, clients, SDK
  models, call settings, context windows
- ``mcp.McpServers`` (``factory.mcp``): MCP servers, started once and shared
- ``journal.RunJournal`` (``factory.journal``): the pending-run record and
  runtime events of the turns
- ``failures``: which failures of a model call are worth another attempt
- ``run_context``: what a run carries - GridRunContext for tools - and helpers

Behaviour mixed into AgentFactory - it works on the factory's shared state
(its caches, sessions, context manager and policy gate), each class says
which part of it:

- ``turns.TurnRunner``: a conversation turn - runs, retries, stops,
  resumption, steering
- ``sessions.SessionUpkeep``: the agents' sessions - how full, compaction, forks
- ``tools.ToolAssembly``: function tools, sub-agent tools, output limits
- ``auto_run.AutoRunTools``: tools the operator runs before the model is asked
- ``policy.PolicyWiring``: the action policy's hold on runs and tools
"""
