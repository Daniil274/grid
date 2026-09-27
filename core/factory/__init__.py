"""The parts AgentFactory (core.agent_factory) is made of.

- ``run_context``: what a run carries (GridRunContext for tools) and small helpers
- ``policy``: the action policy's hold on runs and tools
- ``models``: models, clients and call settings
- ``mcp``: MCP servers, started once and shared
- ``tools``: function tools, sub-agent tools, output limits
- ``auto_run``: tools the operator runs before the model is asked
- ``turns``: a conversation turn - runs, retries, stops, resumption, compaction
- ``failures``: which failures are worth another attempt
- ``journal``: what is recorded about runs
"""
