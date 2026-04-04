# Grid Documentation

## Contents

1. [Overview](#overview)
2. [Agent Factory](agent_factory.md)
3. [Configuration](config.md)
4. [Architecture](architecture.md)
5. [Tools Overview](tools_overview.md)
6. [Channels](channels.md)
7. [Emergency Shutdown](emergency_shutdown.md)
8. [Module Self-Improvement](improvement_module.md)
9. [Platform Architecture](platform/index.md)

---

## Overview

Grid is a framework for running autonomous agents on top of the OpenAI Agents SDK
with support for:

- function tools and MCP tools;
- agent orchestration;
- memory and context persistence;
- Telegram and voice integrations;
- runtime transparency and tracing;
- staged self-improvement workflows.

Core areas of the repository:

- `core/` for runtime, orchestration, registry, memory, evaluation;
- `tools/` for function tools and agent tool bindings;
- `schemas/` for typed configuration and runtime models;
- `benchmarks/` for replay and scorecard evaluation;
- `plans/` for staged roadmap documents;
- `docs/` for operational and architectural documentation.

Use [improvement_module.md](improvement_module.md) as the main entry point for
the self-improvement subsystem.
Use [platform/index.md](platform/index.md) as the main entry point for the new
self-organizing system platform layer.
