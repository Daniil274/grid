# Claude subscription (Claude Code CLI)

Grid can run a model on a **Claude subscription** through the official
[Claude Code](https://docs.claude.com/en/docs/claude-code) CLI. Grid never
collects, stores or forwards a Claude OAuth token: the CLI keeps its own login,
and every call is made by launching the CLI as a child process.

This is the counterpart of the ChatGPT plan bridge (`web_chat.chatgpt`) but with
the opposite trust model: the ChatGPT bridge runs a sign-in and holds the access
token Grid obtained, while the Claude bridge holds **nothing** and relies on the
login the CLI already has.

## Install and sign in

1. Install the Claude Code CLI and put it on `PATH` (or set `GRID_CLAUDE_BINARY`,
   or a per-provider `claude_binary`).
2. Sign in **with the subscription**, not an API key:

   ```bash
   claude auth login          # choose the Claude subscription
   claude auth status         # JSON; "authMethod" must be "claude.ai" or "oauth_token"
   ```

If `claude auth status` reports `api_key`, `api_key_helper` or `third_party`,
Grid refuses to run the model (see *Authentication* below).

### Staying within the subscription price

A subscription login alone does **not** guarantee zero additional charges.
Disable paid **extra usage** in your Claude account before running Grid. This
bridge cannot inspect or change that billing setting. Do not configure a paid
fallback model or a paid compaction model if your requirement is subscription
usage only. Grid adds no API-key fallback of its own; reaching the subscription
limit should stop the run rather than switch providers.

The integration has not yet been tested with a real signed-in Claude Code CLI.
Treat the first live run as a smoke test, not a verified billing guarantee.

## Configuration

```yaml
providers:
  claude-code:
    name: "claude-code"
    base_url: "cli://claude"   # unused; the CLI is the endpoint
    auth: "claude"
    timeout: 300
    max_retries: 0             # the CLI retries itself
    # claude_binary: "claude"  # name or path; default: claude / GRID_CLAUDE_BINARY

models:
  claude-sonnet:
    name: "sonnet"             # a Claude Code model alias or a full model id
    provider: "claude-code"
    max_tokens: 8000
    context_window: 200000
```

`max_tokens` and `response_timeout` bound the CLI turn exactly as they bound
every other plan-backed model (`core.bounded_model.BoundedModel`): the CLI
enforces no output cap of its own, so Grid estimates one from the stream.

Then use `claude-sonnet` like any other model key (agents, routing, the web
chat). `auth: claude` providers take no API key and are not listed under a
user's stored credentials: the login belongs to the host.

### Where it may run

The CLI login is the machine operator's own credential. It is offered **only on
the explicit local, single-user web server** (`python -m web_chat`, no
`--accounts`), where it is that one user's. On a server with accounts every
other user is refused (`web_chat.entitlements.UserCredentials`), because the
host login is not theirs to spend. Agents run from the CLI/CLI-driven code paths
(the command line, `grid.py`) use it directly; a managed multi-user web
deployment should configure an OpenAI-compatible provider instead.

## Authentication: subscription only

`core.claude_cli` launches the CLI with a **credential-free environment**:
every variable that would authorize an API key, an `ANTHROPIC_BASE_URL`
gateway, Amazon Bedrock, Google Vertex/Agent Platform, Microsoft Foundry, or an
`AWS_*` / `GOOGLE_*` / `AZURE_*` credential is removed. The CLI's stored login
(under `HOME`) is what remains.

`claude auth status` is read **before every launch**, not once at startup
(`ClaudeCodeRunner.stream` calls `require_subscription`). Anything that is not a
subscription raises `ClaudeCliError` and no `claude -p` run is started; Grid does
**not** fall back to a paid API key. There is no code path that reads a Claude
token from the environment or from a file.

The status probe runs under the same host-settings lockdown as generation
(`--setting-sources ""`), so an `apiKeyHelper` or settings `env` in the host's
Claude Code config cannot run - or pay - merely to answer the check.

Set `GRID_CLAUDE_BINARY` or `claude_binary` if the binary is not named `claude`.

## Locked down by default

Every launch passes the CLI the strictest documented non-interactive flags, so
the bridge is a model and not a second uncontrolled agent:

* `--tools ""` - no built-in Claude Code tools (no shell, file or web access).
* `--strict-mcp-config` (with no `--mcp-config`) - no MCP servers, whatever the
  host has configured.
* `--setting-sources ""` - no user, project or local settings, so no hooks,
  plugins, `env` overrides or `apiKeyHelper` from the host run or pay. This is
  passed to `auth status` as well as to `-p`.
* `--no-session-persistence` - a call leaves no resumable session behind.

Because the CLI has no tools of its own, it cannot emit arbitrary Grid tool
calls. Grid tools and handoffs are instead offered through a **validated JSON
envelope** (`--json-schema`, see below), and any native tool call that still
appears is refused with an error rather than run a second time. The one
exception is Claude Code's own internal `StructuredOutput` tool, which the CLI
uses to satisfy `--json-schema`: on a turn that asked for a schema its
`tool_use` is expected and accepted (its `input` is used if the `result` omits
`structured_output`). It is not exempted on a plain turn.

## What is supported

* **Plain text streaming.** With no tools and no output schema, partial
  `text_delta` events become `response.output_text.delta` events, so text
  appears as it is generated. The completed `assistant` block is used only when
  nothing streamed, so text is never duplicated.
* **Grid tools and handoffs.** The CLI is asked for a structured envelope
  `{"message": str, "tool_calls": [{"name": <enum>, "arguments": object}]}` via
  `--json-schema`; the `name` enum holds exactly the Grid tools and handoffs the
  turn offers, and each tool's parameter schema is written into the prompt. The
  validated `result.structured_output` is translated into Responses
  `function_call` items, so tools run through Grid's normal tool loop and action
  policy. (The CLI is *not* pretended to emit arbitrary Grid tool calls
  natively.)
* **Structured output.** An agent's `output_type` becomes the CLI's
  `--json-schema` (or the envelope's `final` field when tools are present too),
  and the validated JSON is emitted as the model's text for the SDK to parse.
* **History.** Prior `function_call` and `function_call_output` items are
  rendered into the prompt, so a tool result reaches the next turn.
* **Usage.** The run's `result` event supplies token counts for spend and
  context accounting.

Unsupported input is refused with an explicit `ClaudeCliError`, never dropped
silently: images, audio and files, and hosted tools (`WebSearchTool`,
`FileSearchTool`, ...) that cannot be expressed as a Grid function call.

## Limitations

* **Tool and schema turns are buffered.** Grid waits for the CLI's structured
  result before emitting text or function calls. The output-token bound limits
  what Grid receives, not what the CLI already generated; use
  `response_timeout` to bound wall-clock execution as well.
* **Managed settings cannot be switched off from here.** Claude Code settings
  deployed by an organization (MDM / `managed-settings.json` / server-managed)
  outrank `--setting-sources` and can re-enable hooks or change the model.
  Grid cannot guarantee the CLI makes no request beyond the plan allowance;
  an operator who needs a hard guarantee must disable managed settings for the
  host account that runs the CLI.
* **One CLI turn per model call.** Each `claude -p` run is one model turn. Grid's
  outer tool loop drives multi-step tool use; the CLI runs no tools of its own.
* **No credential yet means no call.** Compaction and other paths that need a
  plain OpenAI-compatible client are not served by this provider; configure a
  separate `compact.summary_model` on an OpenAI-compatible provider.
* **Subscription limits are the CLI's.** Grid cannot promise an allowance or
  bypass the plan's rate limits; when they are reached the CLI reports an error
  result and the turn fails. Grid does not add an API-key fallback.
* **Version drift.** The flags above target current Claude Code. A flag the
  installed CLI does not know surfaces as a clear `ClaudeCliError` with the
  CLI's stderr; `--setting-sources ""` in particular must be accepted by the
  version in use.
* **Not verified against a live CLI in CI.** The bridge is unit-tested against
  the documented `stream-json` protocol with a fake process; running it needs a
  signed-in Claude Code CLI on the host, which the test environment does not
  have. In particular the empty `--setting-sources ""` value (load no settings)
  is used by both generation and `auth status` but has **not** been exercised
  against a real binary here; it is the one flag whose acceptance by the
  installed version is still unconfirmed live. Documents list only
  `user,project,local` as valid values, so an operator should verify it once
  against their CLI (a rejection shows up as a `ClaudeCliError` with the CLI's
  stderr).
