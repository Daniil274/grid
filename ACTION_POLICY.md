# Action policy

An opt-in gate between an agent and the calls it makes. Every mediated call - a
function tool, delegation to another agent, an MCP tool - is routed before it
runs. Most calls are decided from facts, without a model: what the tool
declares it does, which paths it names, what the turn has already taken in,
and the filter the user picked in the chat. The rest goes to the operator's
policy model, which answers one question: would this call cause harm the user
did not agree to? Whether a call is *needed* for the task is the agent's
decision, not the policy's.

It is **not an OS sandbox**.

## Enable

Declare it once in the host's routing configuration, where the router model
already lives:

```yaml
settings:
  action_policy:
    mode: enforce           # off | shadow | enforce
    policy_file: policies/action-policy.yaml
    validator:
      model: policy         # key from this config's models registry
      timeout_seconds: 22
```

The external file may contain the `action_policy` mapping directly, below an
`action_policy:` key, or below `settings.action_policy`; inline values override
it. See `action-policy.yaml.example`. This repository's shared policy is
`policies/action-policy.yaml`. Nested policy-file references are rejected.

A policy enabled in the routing config governs every system the router picks.
A system config may carry its own `settings.action_policy`, used when the
routing policy is off. Each factory takes a snapshot at construction: restart
after changing it.

| Mode | Run budgets | Decision |
| --- | --- | --- |
| `off` (default) | not applied | not made |
| `shadow` | applied | recorded; the call still runs |
| `enforce` | applied | only `allow` executes |

## How a call is routed

### What tools declare

A tool module declares what each of its tools does in `TOOL_EFFECTS`, beside
its `TOOL_ISOLATION` (`utils/tool_effects.py`):

```python
from utils import tool_effects as _effects

TOOL_EFFECTS = {
    "file_read": _effects.read("filepath"),       # reads the workspace path in `filepath`
    "file_write": _effects.write("filepath"),     # changes it
    "bash_tool": _effects.run("command"),         # runs the shell command in `command`
    "web_fetch": _effects.egress(untrusted=True), # reaches out; brings outside content back
    "orchestrate": _effects.DELEGATE,             # starts an agent, whose calls are judged
}
```

| Effect | Means |
| --- | --- |
| `read` | looks at the workspace or its own stores; changes nothing |
| `write` | changes the workspace or the user's own stores |
| `exec` | runs code or commands |
| `egress` | sends a request out (fetch, search) |
| `external` | acts outside: publishes, pushes, sends, deploys, spends |
| `delegate` | starts or steers another agent |

A tool that declares nothing still works: it is `unknown`, and the usual
filters send it to the policy model. An agent called as a tool is `delegate`.
Tools that cannot declare in code - MCP tools above all - get an effect from
the operator by name: `tool_effects: {"codegraph_*": read}`. Private user
systems cannot set any of this: their tools are MCP packages, judged unless the
operator declared them, and their configs may not carry policy filters,
protected paths or tool effects.

### Filters: the user's switch

The chat shows a switch beside the message box. Each setting is a filter the
operator declares: a route per effect.

```yaml
default_filter: balanced
filters:
  balanced:
    label: Balanced
    description: Reads, workspace edits and delegation run; commands and outside actions are judged.
    read: allow
    write: allow
    exec: judge
    egress: allow
    external: judge
    unknown: judge
    protected: review
    follow_flows: true
```

Routes, from least to most strict: `allow` (runs), `judge` (the policy model
decides), `review` (waits for a person), `deny` (refused). Delegation always
runs; the agent it starts is routed call by call under the same filter.

The shipped policy offers **Read only** (no changes at all), **Strict**
(every change and command judged), **Balanced** (the default: ordinary work
runs, commands and unknown tools are judged) and **Trusted** (everything runs;
only outside actions and protected files are judged). The choice stays in the
user's browser, travels with every message, and moving the switch while the
agent works applies to that turn at once. A conversation keeps the filter it
last ran under.

### Facts that tighten a route

- **Read-only commands.** An `exec` call whose command is on
  `readonly_commands` - `git status/diff/log/show`, `ls`, `cat`, `grep`, `rg`,
  `sed -n`, `find` without `-exec`/`-delete`, and so on - is a `read`. The
  check is conservative: a redirection that writes, a substitution, an option
  the list excludes or a command that cannot be split keeps it `exec`.
- **Secrets.** A call naming a path on `secret_paths` (`.env`, keys,
  credentials) takes the filter's `protected` route, whatever its effect.
- **Authority.** Changing a path on `protected_paths` - the policy, system
  configs, CI, git internals - takes the `protected` route; reading it does not.
- **Flows** (`follow_flows`). A run that took in outside content (an
  `untrusted` tool, a file under `untrusted_paths` such as uploads) has its
  changes and commands judged; a run that read a secret has what leaves the
  machine judged. Flows belong to the whole turn, sub-agents included, and come
  only from calls that ran.

A stricter route is never loosened: a write refused by Read only stays refused
on a protected path.

## What the policy model sees

Only calls routed to `judge`. The packet carries:

- `trusted_task`: the conversation - the user's messages, and between them the
  assistant replies the user saw, each marked as authorizing nothing by
  itself. A short answer such as "yes, go ahead" means what the reply before it
  proposed.
- `trusted_policy`: the rules and the user's filter (name, label, description).
- `untrusted_action`: tool, kind, projected arguments, registered metadata.
- `host_facts`: the declared effect, why the call was judged, and what the turn
  took in so far - established by the host, not by an agent.
- `untrusted_history`: the run's recent executed calls and reasoning tail.
- `untrusted_delegation`: what callers asked their sub-agents, outermost first.

Sensitive fields and oversized values are replaced by type/size/hash metadata.
Tool output is never sent. The model answers `allow`, `deny` or `review` for a
single question, `action`. Each model selects its adapter with
`policy_api: decisions` (default) or `policy_api: chat`; the chat adapter puts
the trusted part in its system message and the rest in a separate evidence
message, and accepts exactly `{"action": "allow"}`-shaped JSON.

## Held calls

A call routed to `review`, or judged `review`, waits for a person.

- **In the chat.** While someone watches the turn (the web chat), the call
  waits there: its row shows *Allow*, *Allow for this turn* and *Decline*, and
  the gate continues the same call once answered - no retry by the agent.
  *Allow for this turn* lets that tool through for the rest of the turn.
  Nobody answering within `review_ttl_seconds` refuses it (`review_timeout`);
  Stop refuses what waits. An unavailable validator asks too, instead of
  failing the call.
- **Who answers.** With `approvals: user`, the user answers in the chat of the
  conversation the call came from, and only there. With
  `approvals: operator`, only the host review API (`GET/POST
  /api/action-policy/reviews`, admins on a server with accounts, the launcher's
  token otherwise) answers; the user sees that the call waits.
- **Without anyone watching** (CLI, background runs) the call is refused with
  an `approval_id`; an approval lets the retry of that exact call through once.

## Result of a block

The agent receives a JSON `blocked` result naming the rule - `policy_filter`
(the user's filter does not allow it), `policy_deny`, `declined`,
`review_timeout`, `policy_review`, `policy_unavailable`, or a budget rule - and
is told to go on with the rest of the task and name the blocked step, never to
reach the same target another way. Denials are counted; reaching
`max_denials_per_run` stops the run. A held call nobody answered and an
unavailable validator spend no denials.

## Validator availability

```yaml
validator:
  model: [policy, policy_reserve]  # tried in order, the next only when one is unavailable
  timeout_seconds: 15              # queue + all requests + backoff + Retry-After
  max_attempts: 2
  retry_backoff_seconds: 0.25
  max_concurrency: 4
  circuit_failure_threshold: 3
  circuit_cooldown_seconds: 15
```

A valid verdict ends the judgment; a reserve is used only after a technical
failure, never to look for a more permissive answer. Timeouts, transport
errors, malformed answers and HTTP 408/429/500/502/503/504 may be retried;
`Retry-After` is honored. After repeated failures a route's circuit opens for
the cooldown. Set `request_timeout` on each model entry to bound its requests.

## Audit

The `grid.action_policy` logger emits one JSON `ACTION_POLICY` line per
decision: run id, tool, kind, action hash, policy version, mode, decision,
rule, and how it was reached - `effect`, `route`, `filter`, `reasons`, and
`source` (`filter`, `validator`, `user`, `user_grant`, `host_approval`,
`no_answer`), plus validator latency and failures for judged calls. Arguments,
tool output, file contents and provider error bodies are never logged. The web
chat shows each decision as a badge on its action row.

`scripts/replay_policy.py` replays the calls stored in chat traces through the
routing under any filter and prints where they would go - run it before
changing filters, protected paths or declarations.

## Limits

This gate is a judgment layer, not containment. A tool that does more than it
declares, or a command whose script does what its words do not say, can escape
the intent of the policy. Side effects are not verified after execution.
Tool implementations, plugins, hooks and the host stay inside the trust
boundary. For real containment run agents in an OS-isolated executor with
bounded filesystem, process and network access, and treat this policy as the
layer that decides intent.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/test_action_policy.py tests/test_policy_filters.py tests/test_policy_resilience.py tests/test_chat_policy.py tests/test_web_policy_switch.py
```

Decisions are mocked; no provider key or network request is needed. The tests
make no claim about live model accuracy. For an opt-in live smoke check with
synthetic scenarios (billable API calls):

```powershell
.venv/Scripts/python.exe scripts/check_policy_fallback.py --live
```
