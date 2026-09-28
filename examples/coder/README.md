# Engineering (coder)

The default system of the routing catalog (`routing.yaml`, key `engineering`):
conversation, explanations and software work in a project directory.

- `engineer` — the main agent and the default: answers, reads and changes code,
  runs commands, delegates.
- `general_purpose` — a subagent the engineer hands bounded search and
  investigation to; not routed to directly.
- `web_spider` — web research, for requests that need the internet (news,
  prices, online documentation).

## Tools

| Tool | What it does | Where it acts with accounts (`TOOL_ISOLATION`) |
|---|---|---|
| `bash_tool` | shell commands in the working directory | the user's container |
| `file_read`, `file_write`, `file_edit`, `file_append` | files of the working directory | the server, inside the user's workspace |
| `glob_tool`, `grep_tool` | find files and search their contents (ripgrep) | the server, inside the workspace |
| `notebook_read`, `notebook_edit`, `notebook_create` | Jupyter notebooks | the server, inside the workspace |
| `web_fetch`, `web_search` | fetch a public page; search through SearXNG | the server, public internet only |
| `beads_*` | the beads issue tracker (`bd`) | the user's container |
| `codegraph` (MCP) | a semantic code graph of the working directory | the user's container |

Tools live in `tools/`, skills the agents load in `skills/`.

## Requirements

- `rg` (ripgrep) on `PATH` for `grep_tool`.
- SearXNG for `web_search`: `docker compose -f searxng/docker-compose.yml up -d`
  from the repository root, or point `SEARXNG_URL` at a running instance
  (default `http://localhost:8080`).
- `codegraph` on `PATH` for the CodeGraph MCP server; it indexes the agent's
  working directory (`.codegraph/`).
- `bd` on `PATH` for the beads tools.

A server with accounts runs `bash_tool`, `codegraph` and beads in each user's
container, so the host needs only Docker and the sandbox image
(`docker build -t grid-agent:latest docker/agent`), which has all of them.

## Run

Through the catalog, as the web chat does by default:

```bash
grid-web-chat --routing routing.yaml --path /path/to/project
```

This system alone:

```bash
grid-web-chat --config examples/coder/config.yaml --path /path/to/project
agent-chat --config examples/coder/config.yaml --path /path/to/project
```

Providers and models are in `config.yaml` (`OPENCODE_API_KEY` and
`OPENROUTER_API_KEY` in `.env`), as are the context compaction limits.
