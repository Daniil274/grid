# Action policy

An opt-in gate between an agent and the calls it makes. Every mediated call —
a function tool, delegation to another agent, an MCP tool — is judged against
the operator's declared rules before it runs, together with the chain of calls
and the reasoning that led to it.

The gate sends each proposed action to a semantic Decisions classifier. Both
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

`validator.model` is an ordinary key of the `models` registry, like
`routing.model`; the validator reuses that provider's base URL, credentials,
proxy and timeout. It expects a Decisions-compatible model (`/alpha/decisions`),
not a chat model. A missing or unknown key fails at factory construction: there
is no fallback model.

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
return `allow`. The runtime validates the probability payload for consistency
but does not replace the classifier's declared choice with a numeric threshold;
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

A call arriving without a trusted task is refused: `init_tools` executed while a
dynamic agent is being created run before any task exists, so under an enabled
policy they return a `blocked` result instead of executing. `auto_run_tools` of
a normal run carry the run's task and are checked like any other call.

## Result of a block

The agent receives a JSON `blocked` result naming the rule, so it can revise and
try again. Denials are counted; reaching `max_denials_per_run` stops the run.
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
probability all become `unavailable`, which blocks in `enforce`.

A failed judgment is retried once within `validator.timeout_seconds`. Set
`request_timeout` on the validator's model entry to bound a single request: a
stalled one is then dropped and retried instead of consuming the whole budget.
Keep it under half of `timeout_seconds` so the retry fits. Give the validator
its own model entry when the same model also routes, so routing keeps its
provider timeout.

## Audit

The `grid.action_policy` logger emits one JSON `ACTION_POLICY` line per decision:
run id, tool, kind, action hash, policy version, mode, decision, rule and the
per-question verdicts. Arguments, tool output, file contents and provider error
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
for. Every call costs one Decisions request; `shadow` costs the same as
`enforce`.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/test_action_policy.py
```

The suite covers the gate contract, the chain window, nested mediated calls,
fail-closed behavior, the Decisions request shape and the factory wiring on the
real `examples/coder` tool set. Decisions are mocked; no provider key or network
request is needed. The tests make no claim about live model accuracy.
