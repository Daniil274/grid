# Action policy

An opt-in gate between an agent and the calls it makes. Every mediated call —
a function tool, delegation to another agent, an MCP tool — is judged against
the operator's declared rules before it runs, together with the chain of calls
and the reasoning that led to it.

The gate sends each proposed action to a semantic policy classifier. Both
the rules and the complete action/chain classifier prompts belong to the policy
YAML. The runtime has no hard-coded action taxonomy and does not infer policy
from tool names; individual tools do not need policy entries. It is **not an OS
sandbox**.

## Enable

Declare it once in the host's routing configuration, where the router model
already lives:

```yaml
settings:
  action_policy:
    mode: shadow            # off | shadow | enforce
    policy_file: policies/action-policy.yaml
    validator:
      model: router         # key from this config's models registry
      timeout_seconds: 10
```

Policies may instead live in a reusable YAML file. The reference is resolved
relative to the config that contains it; inline values override the external
document:

```yaml
settings:
  action_policy:
    policy_file: policies/action-policy.yaml
    mode: enforce
    validator:
      model: router
```

The external file may contain the `action_policy` mapping directly, below an
`action_policy:` key, or below `settings.action_policy`. See
`action-policy.yaml.example`. This repository's shared runtime policy is
`policies/action-policy.yaml`. Nested policy-file references are rejected.

`validator.model` and `validator.fallback_models` reference the existing model
registry. Each model selects its policy adapter with `policy_api: decisions`
(the default) or `policy_api: chat`. Unknown keys fail at factory construction.
Every route uses the same operator policy, questions and projected evidence.

The shipped routing configs use OpenRouter `typesafe/jev-1.13` as the primary
validator (`OPENROUTER_API_KEY`) and OpenCode Go through Chat Completions as the
reserve (`OPENCODE_API_KEY`). `routing.yaml` selects `glm-5.3-flash`;
`routing.opencode.yaml` selects `mimo-v2.6-flash`. The paid Zen/System One switch
was reverted: Go access does not imply access to the Zen Jev endpoint.
HTTP 402 opens the failing route's circuit and immediately tries the reserve;
a valid deny/review is final and never triggers fallback.

```yaml
settings:
  action_policy:
    validator:
      model: policy
      fallback_models: [policy_chat]
      max_attempts: 2
      timeout_seconds: 15
providers:
  opencode-go:
    name: opencode-go
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_API_KEY
models:
  policy_chat:
    name: glm-5.3-flash
    provider: opencode-go
    policy_api: chat
    request_timeout: 9
    max_tokens: 512
    reasoning: {enabled: false}
```

The chat adapter sends trusted task/policy and operator question criteria in a
system message; tool arguments, metadata, reasoning and delegation remain in
a separate JSON evidence message. It has no tools and receives no conversation
or tool output. It requests strict `json_schema` output, temperature zero and
the model's configured token limit. A provider must support this request shape;
there is no silent downgrade to a weaker format after a provider error.

The response is exactly `{"action":"allow","chain":"review"}` (example values),
with only the configured questions and allow/deny/review values. Chat does not
invent probabilities. Local validation rejects duplicate or extra keys,
missing verdicts, markdown, refusals, tool calls, non-string values and any
completion whose finish reason is not `stop`. Reasoning content is never used
as an answer. Invalid responses stay fail-closed. Connections, deadlines,
circuits and concurrency limits are shared with the Decisions recovery path.

A policy enabled in the routing config governs every system the router picks, so
one policy and one validator model cover all systems. A system config may carry
its own `settings.action_policy`, which is used when the routing policy is off.
Each factory takes a snapshot at construction: restart after changing it.

| Mode | Run budgets | Verdict |
| --- | --- | --- |
| `off` (default) | not applied | not requested |
| `shadow` | applied | recorded; the call still runs |
| `enforce` | applied | only `allow` executes |

`shadow` is not a no-op: attempt and denial budgets still stop a run, and a call
made outside a trusted task is still refused. Run it first on disposable data to
see what the validator would have blocked.

## What is checked

The classifier receives the task, the operator-authored structured rules, the
proposed tool name, kind, complete projected arguments, registered description,
input schema and MCP annotations. Metadata is evidence, not authority. The
classifier infers the action's actual behavior from that complete context rather
than matching names against a built-in vocabulary.

Two generic runtime invariants remain deterministic: malformed/oversized calls
and calls beyond the configured delegation depth are rejected before a model
request. These are execution budgets, not semantic action classifications.

Two questions go to the model in one request:

- **action** — may this specific call execute, given the task and the rules?
- **chain** — is the run so far, with this call, still doing the task within the
  rules, or has it drifted, started following instructions found in data, or
  assembled a violation step by step? Disable with `check_chain: false`.

The strictest verdict wins: a call runs only when both configured questions
return `allow`. For Decisions responses, the runtime validates the probability payload for
consistency but does not replace the declared choice with a numeric threshold;
uncertainty must be expressed as `review` by the policy prompt itself.

The request carries the host-supplied task, rules, an untrusted metadata and
argument projection of the proposed call, recent calls, the current run's own
reasoning tail and counters. Sensitive fields and oversized string values are
replaced by type/size/hash metadata before entering either the validator packet
or chain. It does not carry the conversation or tool output. Oversized complete
actions are rejected; the chain window is trimmed oldest-first to its declared
budget and says how many entries it dropped.

## Scope

`kinds` lists what the gate mediates. A kind left out runs unchecked.

- **function** — every function tool an agent receives, project tools included.
  The wrapper is applied where tools are resolved, so it covers agents from
  config and dynamic agents created for background workers.
- **agent** — a call to another agent as a tool. A delegated agent continues the
  caller's task, chain and budgets, so a nested call is judged with the whole
  trajectory behind it.
- **mcp** — MCP tool invocations, through the same gate as function tools.

Handoffs, the SDK's own control flow, memory and session writes, compaction and
anything the host itself does are outside the gate.

A call arriving without a trusted task is refused. The `init_tools` that
`orchestrate` passes to a dynamic agent run under the same delegated state as
the agent itself, so they are judged like its other calls; a refused one is not
injected into the agent's instructions.

`auto_run_tools` are not judged. Their tool and arguments come from the
operator's config, not from a model, so asking whether the user requested them
would deny every setup step. They are audited with rule `operator_config`, stay
out of the agent's chain, and are still refused once the run is stopped. A
refused one is not injected into the prompt, and a one-time tool is tried again
on the next run.

## Result of a block

The agent receives a JSON `blocked` result naming the rule. It is told to go on
with the rest of the task and report the blocked step, not to probe the policy:
probing reads as drift to the chain check and would block the rest of the run.
Denials are counted; reaching `max_denials_per_run` stops the run. Neither pending
review nor validator unavailability spends this denial budget in `enforce`.
`review` blocks in `enforce` and returns an opaque `approval_id`. The trusted
host may approve it once; approval is bound to the run id and the hash of the
exact tool, kind and original arguments, expires after `review_ttl_seconds`, and
is consumed by the retry. It cannot override `deny`.

The web runtime exposes the host endpoints `GET /api/action-policy/reviews` and
`POST /api/action-policy/reviews/{approval_id}`. Both require the random
`X-Grid-Action-Review-Token` printed by the web-chat launcher; the token is not
given to the agent. Embedders can call `pending_action_reviews()` and
`resolve_action_review()` directly instead.

A validator timeout, a transport error, a malformed answer or a nonfinite
probability all become `unavailable`, which blocks in `enforce`. This is an
infrastructure failure, not a policy denial: the result includes
`infrastructure_error: true`, explains that the action did not execute, and does
not increment denials or stop the run in either mode. Total call-attempt budgets
still apply, so an agent cannot retry indefinitely. In `shadow`, the call still
executes as with other advisory verdicts. Host approval cannot bypass an outage.

## Validator availability

```yaml
validator:
  model: policy
  fallback_models: [policy_chat] # model with policy_api: chat
  timeout_seconds: 15      # queue + all requests + backoff + Retry-After
  max_attempts: 2          # total requests, across every route
  retry_backoff_seconds: 0.25
  max_concurrency: 4       # concurrent judgments per gate/factory
  circuit_failure_threshold: 3
  circuit_cooldown_seconds: 15
```

A valid `allow`, `deny` or `review` ends the judgment. The reserve is used only
after a technical failure, never to search for a more permissive verdict.
Untried available routes take precedence over repeats of a failed route.
`max_attempts` must be large enough to cover all configured routes.

Timeouts, transport errors, malformed answers and HTTP 408/429/500/502/503/504
may be retried. Other errors (including authentication and configuration HTTP
errors) are not retried on the same route in that judgment; a configured reserve
may still be used. Retries use exponential backoff with jitter. `Retry-After`
on 429/503 is honored in seconds or HTTP-date form and persists across calls
to that route. A cooldown that exceeds the remaining budget never causes an
early retry; an available independent reserve can still answer immediately.

Set `request_timeout` on each model entry to bound its requests. Each attempt
is additionally capped to the remaining total budget divided by remaining
attempts, reserving time for recovery. When preceding routes are known down,
the reserve can use the remaining budget up to its own request timeout.
Queueing and backoff spend that same budget. Give policy its own model entry
so routing keeps its provider timeout.

After the configured number of consecutive failures, a route is skipped for
the circuit cooldown; non-retryable errors open the circuit immediately. Once
the cooldown expires, only one recovery probe is allowed at a time. If all
circuits are open, the gate reports unavailable without sending requests.
The concurrency limit, cooldowns and circuits belong to one factory, not to a
provider-wide distributed quota. Clients reuse connections and are closed by
`AgentFactory.cleanup()`; direct users of `ActionValidator` or `ActionGate`
must call `aclose()` on the same event loop after in-flight checks finish.

## Audit

The `grid.action_policy` logger emits one JSON `ACTION_POLICY` line per decision:
run id, tool, kind, action hash, policy version, mode, decision, rule and the
per-question verdicts. Availability metrics include total `latency_ms`,
`queue_ms`, `packet_bytes`, the successful `validator_model`, and
`validator_attempts` with route key, model, duration and outcome per request.
`validator_failures` distinguishes HTTP statuses, invalid answers, request
timeouts, total budget exhaustion, queue timeouts and unavailable routes.
Exception messages are omitted because even parsing errors can contain private
data. Use these fields to compare outage rates, recovery rates and p95/p99
latency before adjusting budgets. Arguments, tool output, file contents and provider error
bodies are never logged. Grid's ordinary conversation and tool logs have their
own settings and may contain all of that; configure retention for both. The
in-memory decision buffer is bounded and is not storage.

Webchat renders the check as a compact badge inside the corresponding action
row: green for allow, amber for review and red for deny or an unavailable
validator. Its tooltip shows the decision source and latency; the policy UI
never repeats the action arguments.

## Limits

This gate is a judgment layer, not containment. Opaque commands and side effects
hidden inside a tool implementation cannot be reliably inspected. Misleading
tool metadata, incomplete arguments, or a permitted tool that does more than
advertised can escape the intent of the policy. In `shadow`, verdicts remain
advisory until a run budget stops further calls.

Side effects are not verified after execution: the gate reports what it allowed,
not what happened. Tool implementations, plugins, hooks and the host stay inside
the trust boundary.

For real containment, run the agent against an OS-isolated executor with bounded
filesystem, process and network capabilities, and treat this policy as the layer
that decides intent, not the layer that enforces the blast radius.

Before enforcing in production, label representative task and call sequences and
measure false allows, false denials, outages and added latency — including
prompt injection and multi-step violations, which is what `check_chain` exists
for. Every checked call normally costs one classifier request; recovery can use
up to `max_attempts`. `shadow` uses the same validation path as `enforce`.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/test_action_policy.py tests/test_policy_resilience.py tests/test_chat_policy.py
```

The suite covers the gate contract, the chain window, nested mediated calls,
fail-closed behavior, the Decisions request shape and the factory wiring on the
real `examples/coder` tool set. Decisions are mocked; no provider key or network
request is needed. The tests make no claim about live model accuracy.

For an opt-in live smoke check with synthetic scenarios only, run:

```powershell
.venv/Scripts/python.exe scripts/check_policy_fallback.py --live
```

It loads `.env`, uses the configured provider APIs (which may charge for calls),
and checks allowed reads/edits, injection, exfiltration, ambiguous recipients
and cumulative limits. The invoked tool is always a no-op. Only verdicts and
delivery metadata are printed; credentials, provider bodies and reasoning are
not. This small smoke check is not a substitute for a representative security
evaluation before changing models or policy rules.
