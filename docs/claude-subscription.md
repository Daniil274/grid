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

The CLI login is the machine operator's own credential. It is offered on the
explicit local, single-user web server (`python -m web_chat`, no `--accounts`),
where it is that one user's, and on a server with accounts **to admins only**:
the operator runs and debugs the provider there before it is opened to every
user. Every other account is refused (`web_chat.entitlements.UserCredentials`),
because the host login is not theirs to spend. The factory asks this before it
builds a Claude model or a summary client for one
(`ModelAccess.check_cli_login`), as it asks for a key of any other provider.
Agents run from the CLI/CLI-driven code paths (the command line, `grid.py`) use
the login directly.

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

The status probe is plain `claude auth status`: since Claude Code 2.1.310 the
subcommand accepts only `--json`/`--text` and rejects `--setting-sources`, so it
reads the host's settings. An `apiKeyHelper` there shows up as a non-subscription
method and is refused; generation itself still runs locked down (below). A
status call the CLI refuses (an unknown flag, say) exits 1 without JSON and is
reported as a `ClaudeCliError` with the CLI's stderr, not as a missing login.

Set `GRID_CLAUDE_BINARY` or `claude_binary` if the binary is not named `claude`.

## Locked down by default

Every launch passes the CLI the strictest documented non-interactive flags, so
the bridge is a model and not a second uncontrolled agent:

* `--tools ""` - no built-in Claude Code tools (no shell, file or web access).
* `--disallowedTools "mcp__*"` - no MCP tools at all. `--tools` covers
  built-in tools only; the [CLI reference](https://docs.claude.com/en/docs/claude-code/cli-reference)
  says so outright and names this flag for MCP ("The flag doesn't affect MCP
  tools; to deny those too, use `--disallowedTools \"mcp__*\"`"). A bare `"*"`
  removes every tool including the internal `StructuredOutput` one that
  `--json-schema` needs, so the deny is scoped to MCP. The two flags also
  complete each other: the reference notes that `--tools ""` drops the
  `EndConversation` tool only once no MCP tool remains, which this deny
  guarantees.
* `--strict-mcp-config` (with no `--mcp-config`) - no MCP servers, whatever the
  host has configured.
* `--setting-sources ""` - no user, project or local settings, so no hooks,
  plugins, `env` overrides or `apiKeyHelper` from the host run or pay. Passed
  to `-p` only; `auth status` rejects it.
* `--no-session-persistence` - a call leaves no resumable session behind.
* `--disable-slash-commands` - no skills or slash commands, so their listing
  is not part of the model's context.
* `--system-prompt-file` - Grid's system instructions and the tool
  instructions replace Claude Code's own system prompt (its identity, memory
  and skill instructions). The file is created private (mode 0600) for the one
  run and removed after it; the prompt never goes on the command line.
* `--thinking-display summarized` - over a subscription the thinking text is
  withheld unless summaries are asked for. Not listed in `--help`: it is the
  flag the Agent SDK's `thinkingDisplay` option sets.
* `--effort` - from the model's `reasoning.effort` (`minimal`/`none` become
  `low`); without one, the CLI's default.

What the CLI still adds around Grid's system prompt cannot be switched off from
the command line: an "Agent SDK" identity line, an environment section (its
working directory, the platform, the date) and a `userEmail` block with the
signed-in account's email. Grid's system prompt opens with a preamble that
says none of it describes the person in the chat, that the email must not be
written out and that the directory is not their workspace. That is an
instruction to the model, not a guarantee.

Because the CLI has no tools of its own, it cannot emit arbitrary Grid tool
calls. Grid tools and handoffs are instead offered through a **validated JSON
envelope** (`--json-schema`, see below). Native tool calls are judged by
evidence, not by suggestion: an `assistant` `tool_use` is only a proposal, and
the turn is refused (rather than run a tool twice) only when a `user` event
carries the `tool_result` whose `tool_use_id` matches it. A call the CLI
answered "No such tool available" ran nothing and is ignored. The one exception
is Claude Code's own internal `StructuredOutput` tool, which the CLI uses to
satisfy `--json-schema`: on a turn that asked for a schema its `tool_use` is
expected and accepted (its `input` is used if the `result` omits
`structured_output`). It is not exempted on a plain turn.

The CLI also states at its `system`/`init` event which tools it holds ([structured
outputs](https://docs.claude.com/en/docs/claude-code/agent-sdk/structured-outputs)
describes the `tools` list there). Anything beyond `StructuredOutput` on a
schema turn - and anything at all on a plain turn - means the lockdown did not
take (organization-managed settings can add tools the command line cannot
remove), so the turn is refused with a clear error at `init`, before the
generation goes further.

## What is supported

* **Plain text streaming.** With no tools and no output schema, partial
  `text_delta` events become `response.output_text.delta` events, so text
  appears as it is generated. The completed `assistant` block is used only when
  nothing streamed, so text is never duplicated.
* **Thinking.** Partial `thinking_delta` summaries become `reasoning_content`
  deltas, which the SDK emits as `response.reasoning_summary_text.delta` - the
  same events a ChatGPT plan's reasoning summaries arrive as, so the chat shows
  the model's thinking the same way, on text, schema and tool turns alike.
* **Errors in the CLI's words.** A `result` with `is_error` (a plan limit, an
  API error) fails the turn with the CLI's own message instead of a missing
  envelope or an empty answer.
* **A Grid tool called by name.** The model sometimes calls a Grid tool as a
  native `tool_use` before answering with the envelope. The CLI does not hold
  that tool and runs nothing ("No such tool available"), so the call is
  ignored; only a call the CLI actually answered with a matching `tool_result`
  (by `tool_use_id`) fails the turn, and a call to a tool it announced at
  `init` already failed there.
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

* **Images.** Image parts (a user's attachment, a screenshot a tool returned)
  become `[image N]` marks in the prompt and Messages API `image` blocks (base64
  PNG, JPEG, GIF, WebP, or an http(s) URL); the turn then goes to the CLI as one
  `stream-json` user message (`--input-format stream-json`). Grid's image window
  bounds how many go with one request. `vision: false` on a model turns them off.

Unsupported input is refused with an explicit `ClaudeCliError`, never dropped
silently: audio and files, an image given only by `file_id` or in another
format, and hosted tools (`WebSearchTool`, `FileSearchTool`, ...) that cannot be
expressed as a Grid function call.

## Limitations

* **Tool calls and schema answers are buffered.** On a tool turn the envelope's
  `message` streams as the CLI writes it (read from the StructuredOutput
  tool's `input_json_delta` fragments), but function calls are emitted only
  from the CLI's validated result. If the validated `message` differs from the
  streamed draft (a retried envelope), the validated text follows the draft.
  A turn whose envelope may carry a structured `final` answer, and a plain
  schema turn, stream only their thinking. The output-token bound limits
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
* **Compaction and memory extraction run through the CLI too.** Their
  one-off summary requests (`chat.completions.create`) are answered by the same
  locked-down runner (`core.claude_model.ClaudeCodeChatClient`), so a long chat
  on the subscription is compacted like one on any other provider. Other paths
  that need a keyed OpenAI-compatible client are not served by this provider.
* **Every model call is a new process.** Starting `claude -p` costs about 1.6 s
  and the `auth status` check about 0.4 s on top of the model's own time, on
  every step of a tool loop. Concurrent chats run as concurrent processes:
  they do not share a conversation, and Claude Code's own config survives them.
* **Subscription limits are the CLI's.** Grid cannot promise an allowance or
  bypass the plan's rate limits; when they are reached the CLI reports an error
  result and the turn fails. Grid does not add an API-key fallback.
* **Version drift.** The flags above target current Claude Code. A flag the
  installed CLI does not know surfaces as a clear `ClaudeCliError` with the
  CLI's stderr; `--setting-sources ""` in particular must be accepted by `-p`
  in the version in use.
* **Not verified against a live CLI in CI.** The bridge is unit-tested against
  the documented `stream-json` protocol with a fake process; running it needs a
  signed-in Claude Code CLI on the host, which the test environment does not
  have. In particular the empty `--setting-sources ""` value (load no settings)
  has **not** been exercised against a real binary in CI. Documents list only
  `user,project,local` as valid values, so an operator should verify it once
  against their CLI (a rejection shows up as a `ClaudeCliError` with the CLI's
  stderr).
