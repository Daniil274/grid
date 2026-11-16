# Tracker reliability: repeats, lost answers and markers

Beads runs as a separate process against an embedded database. GRID asks for a
write and only afterwards learns what became of it. This document states what
the tracker tools guarantee, what they deliberately do **not**, and where the
honest gaps are.

Code: `core/tracker_operations.py` (the journal), `tools/beads_tools.py` (the
tools), `web_chat/tasks_api.py` and `web_chat/plan_board.py` (the page and the
plan card). The physical-database gate itself is `core/tracker_gateway.py`.

## The journal

Every write (`beads_create`, `beads_update`, `beads_close`, `beads_plan`) is an
*operation* in a SQLite journal at

```
<workspace root>/.grid/tracker-operations/journal.sqlite3
```

* **Beside the project, never inside `.beads`** (bd owns that tree) and never
  in a per-uid temporary directory. A journal that a reboot can lose is not a
  journal.
* **Named by the workspace root the gateway identifies**, i.e.
  `storage_identity(directory).beads.parent` — not the directory a caller
  happened to spell. A symlink, a `..` segment, a subdirectory of the project
  and a `.beads/redirect` alias all name one tracker, so all of them share one
  journal. A gateway refusal (relative path, missing directory) is a
  **refusal to write**, not a `realpath` guess: a journal named from a guess
  could sit beside a different tracker than the one bd opens.
* **One record is one transaction** (`BEGIN IMMEDIATE`, `PRAGMA
  synchronous=FULL`). A record a reader sees is a record that survived the
  crash. There are no half-written lines to skip and no append glued onto a
  torn predecessor — the failure modes of the earlier JSONL journal — and the
  directory entry of a newly created journal is fsynced. An unreadable or
  corrupt journal is a refusal (`tracker_operation_corrupt`), never an empty
  journal.
* A `started` record is committed **before** bd runs.

## Request ids and payload fingerprints

A caller may pass `request_id`. Repeating it answers from the journal instead
of writing twice:

| journal state | a repeat gets |
|---|---|
| `committed` | the first call's answer, with `duplicate: true`; bd is not touched |
| `failed` | the same typed refusal; bd is not touched |
| `started` / `unknown` | the marker lookup below; the write is **never** repeated |

A request id is bound to a **fingerprint of the whole payload**: kind plus every
argument that shapes the write. For `beads_create` that is `title`, `priority`,
`type` and `description`; for `beads_update` every field including `claim` and
`notes`; for `beads_plan` the keys, nodes and edges of the whole graph. The
same id with a different payload is refused with `tracker_request_conflict` —
it cannot be answered as a repeat — rather than silently returning another
request's result.

## Lost answers and markers

There is a window between bd's commit and the journal's result record. When the
process dies or bd is stopped on timeout inside it, "nothing happened" is a
guess. GRID does not guess:

* the journal records `unknown` with `committed: null`, and the tool answers a
  typed payload (`isError: true`, `code: tracker_unknown`, `committed: null`);
* the evidence that resolves it is a **marker in the tracker, written by the
  same bd transaction as the data**:
  * `beads_create` — `--external-ref grid-op:<operation-id>`;
  * `beads_update` — `--set-metadata grid_op=<operation-id>`;
  * `beads_close` — the marker rides inside `--reason` (`… [grid-op:<id>]`);
  * `beads_plan` — every node's `external_ref` is `grid-op:<id>:<key>`.
* a later call with the same request id looks the marker up. Found and matching
  what was intended → the operation is upgraded to `committed` and answered as
  reconciled. Not found (the tracker was read and holds nothing) → still
  `unknown`, never `failed`: nobody can prove a write that may have happened
  did not.

A status or assignee that merely *looks right* is not proof: another actor may
have put it there, or it may have been so before the lost answer. `beads_update`
therefore requires `metadata.grid_op == <operation-id>` **and** the expected
status/assignee, and `beads_close` requires the marker inside `close_reason` —
never `status == closed` alone. `beads_plan` adopts a plan found by its markers,
keeps the real ids of a plan that only partly reached the tracker
(`tracker_plan_partial`, `committed: null`), and reports its first wave as
unknown (`ready_unknown`) rather than inventing an empty one when `bd ready`
fails.

## Typed answers

Tracker tools answer a failure with a JSON payload, not with prose:

```json
{"isError": true, "code": "tracker_unknown", "error": "...", "committed": null, "retryable": false}
```

`isError`, `code` and `committed` are the contract; the sentence is for people.
Readers (the plan card, the tasks page) decide on the flag, not on a `❌` prefix.
The one exception kept for compatibility is that legacy plain-text tools still
say `❌`; a payload that carries `isError` is always read by the flag.

`web_chat/tasks_api.py` answers the same way: a refusal with bd's own words is
`400` with `isError: true, committed: false`; a write whose answer was lost is
`202` with `committed: null`. The claim endpoint issues a **typed receipt**
only after bd confirmed the write *and* the task read back `in_progress`:

```json
{"ok": true, "isError": false, "committed": true,
 "receipt": {"tool": "beads_update", "isError": false, "bead_id": "…",
              "status": "in_progress", "assignee": "…", "committed": true}}
```

`PlanBoard` shows ownership only from such a receipt (`plan_board.claim_receipt`),
never from a successful agent's prose or from an `init_tools` intent: the
executor may never start, and its text is not evidence that the claim ran. The
board also recognises the same receipt at the top level or under `receipt`.

## Task lifecycle: who owns a claimed task, and how the run ended

A confirmed claim is not enough to run a task: two runs of one user share the
tracker's `grid:<user>` assignee, so bd cannot tell them apart. GRID keeps that
fact in a second journal, `<workspace>/.grid/tracker-lifecycle/bindings.sqlite3`
(SQLite, one transaction per change, `synchronous=FULL`). It is beside the
operation journal, so every spelling of one project (a subdirectory, a
`.beads/redirect`) shares it.

* **The owner is a run id**, never a user: `run-…` for a turn, `exec-…` for an
  orchestrate executor, `agent-…` for an `Agent` tool sub-agent. `beads_update
  claim=true` reserves the task for that run *before* bd is called (a
  compare-and-set on the owner), so two runs racing for one task settle in the
  journal: the loser is refused with `tracker_claimed_elsewhere` and makes no
  bd write.
* **Foreign live owners are refused** by `beads_update`/`beads_close`: a task
  held by another live run cannot be changed (the tracker's own assignee check
  still applies as well). A binding whose lease ran out is **taken over in the
  journal before bd is awaited**, recording `takeover_from`; later requests
  from the stale run are refused (`tracker_not_owner`). The journal transition
  is atomic, but it is not a storage-level fence for a bd command already in
  flight: after a hard kill, an orphan command may still commit a write whose
  ownership guard passed before the takeover. Reconcile the operation markers
  and actual tracker state before resuming such a task. Action policy still
  runs before a new write.
* **How the run ended is observed, not read from the answer.** The runtime's
  `finally` (the top-level turn, `run_agent_object`, the `Agent` tool) records
  `timeout`, `attempt_limit`, `cancelled`, `error` or `not_started` as
  `interrupted`, and a normal answer as `needs_review` unless the executor's own
  close was confirmed. Only a confirmed close is `completed`; a model saying it
  is done never closes a task.
* **A preparation failure still settles.** An executor's `init_tools` run under
  the executor's own run id and share its binding list; if a required step
  claims a task and a later one fails, the early return settles it as
  `not_started` instead of leaving a claim "running" forever.
* **A live run stays live.** Each run starts a daemon `LeaseKeeper` that
  refreshes the lease of every task it holds (a third of the lease, at most
  once a minute). It stops in the run's `finally`, before the bindings are
  settled. A long model turn without tracker writes therefore does not make a
  live owner look stale.
* **UI and caller reports are the journal, not the answer.** `task_bindings` in
  an orchestrate result and the plan board read the binding by id; a missing
  binding produces no receipt, never a fabricated "claimed".

### What a hard kill does and does not do

A `SIGKILL` (or the host going down) cannot run any `finally`. The lease then
simply runs out: no heartbeat touches the binding after the process died, the
record keeps its last honest state (`claimed`/`running`) and `takeover_from`
shows who held it when the next run takes it over. GRID does **not**
automatically rewrite that record to `interrupted` - without a reconciliation
that would be a guess about whether the run did anything. The next run's
takeover, or an operator reading the checkpoints, is the recovery step.

## What is not guaranteed

* **No exactly-once.** bd's commit and the journal record are not one atomic
  unit. The guarantee is at-most-once per request id, with an honest `unknown`
  in between; a caller that wants a duplicate never gets one by repeating,
  but a write whose answer was lost stays unknown until a marker proves it.
* **One host.** Coordination is between processes on one host that share the
  project directory and the gateway's gate directory. A journal on a network
  mount, or a writer that bypasses this module (a person running `bd`
  directly), is outside it.
* **A partial plan is partial.** If bd commits some nodes and dies, the real
  ids are reported and nothing is retried; finishing the graph is a decision
  for a human or a new request id.
* **Markers are visible.** They live in `external_ref`, `metadata.grid_op` and
  `close_reason`; a tracker synced elsewhere shows them.
* **The gateway is trusted.** The journal uses its identity but never replaces
  its fail-closed checks.

## Tests

`tests/test_tracker_operations.py` (journal framing, transactions, aliases,
cross-process readers), `tests/test_beads_plan.py` (crash, partial plan,
repeat), `tests/test_beads_operations.py` (create/update/close markers),
`tests/test_tracker_lifecycle.py` (run ownership, takeover fencing, runtime
timeout/attempt-limit/cancel/not-started settlement, preparation sharing the
executor run, the lease keeper, and a real temporary `bd init` tracker whose
interrupted claim is taken over and closed once),
`tests/test_tracker_handoff.py` (typed payloads between runtime and UI),
`tests/test_plan_board.py` and `tests/test_tasks_api.py` (receipts, refusals),
plus `tests/test_tracker_gateway.py` for the physical-database identity. A
real-bd smoke creates, repeats by request id, refuses a conflicting payload,
reconciles a lost update by its marker and repeats a close — against a
temporary `bd init` workspace.

## The page's writes: one key per action

The tracker page (`web_chat/js/tasks/page.js`, `web_chat/js/tasks/mutations.js`)
does not send a bare mutation: every write is an *action* with a key, and the
server's journal is what makes a repeat safe.

* **The key is the action's identity.** It is a UUID taken once per action - the
  operation (`create`, `update`, `close`, `dep`) plus the **exact** canonical
  payload (for an update, the task id and the patch; for a close, the id and the
  reason; for a dependency, the id, the direction and the parent). The identity
  is the payload itself, not a digest of it: two different payloads cannot fold
  onto one key, so a retry is never answered with another change's result, and
  nothing depends on a hashing API being present. Retrying the same action - a
  retry button, a second submit, a submit after the answer was lost - sends the
  same key, and `web_chat/tasks_api.py` answers the first write from its journal
  instead of writing twice.
* **A new user action gets a new key.** Once a write is confirmed (or refused
  with proof), its key is forgotten, so two deliberate identical tasks are two
  tasks. Only an action whose outcome is *unknown* keeps its key.
* **Unknown is not success.** HTTP `202` with `isError: true` and
  `committed: null` means bd did not answer: the page says so, does not report
  success, and keeps the action retryable under the same key. A refusal with
  `committed: false` (usually `400`/`404`) wrote nothing and ends the action.
  A transport failure (no HTTP status, a `5xx`, `408`, `429`) is treated as
  unknown for the same reason.
* **A reload keeps, never replays.** Unconfirmed actions are stored in
  `sessionStorage` under `grid.tasks.pending.index`, keyed by the action's
  identity, bounded to 40 records and 32000 characters. Nothing is sent on load:
  the page offers the same action again, and that repeat lands on the stored
  key. Only keys with this module's prefix are read or written, so an auth token
  kept in the same `sessionStorage` is never cleared or overwritten.
* **The key is stored before the write leaves.** If the record cannot be
  written (a full or disabled store) the page refuses the action
  (`tracker_pending_undurable`) and sends nothing: a key a reload would lose is
  a duplicate waiting to happen. With no `sessionStorage` at all the action is
  still keyed for the life of the tab, and `mutations.durable` says the reload
  guarantee does not hold.
* **A full record refuses, it never evicts.** A new action that would pass the
  40-record or 32000-character bound is refused before it is sent
  (`tracker_pending_capacity`); the unconfirmed actions already stored are never
  silently dropped, and retrying one of them stays possible. The user resolves
  or dismisses a pending action instead.
* **The shared client carries the key.** `web_chat/js/net/api.js` is used for
  every write; `request_id` rides in the create/update JSON body, and
  `closeTask(id, reason, options)` / `taskDep(id, action, parentId, options)`
  send `options.request_id` when the page passes one (and send nothing extra
  when it does not). The server also accepts the key as an `Idempotency-Key`
  header.

### HTTP contract of the write routes

`POST /api/tasks`, `PATCH /api/tasks/{id}`, `POST /api/tasks/{id}/close`,
`POST /api/tasks/{id}/claim`, `POST /api/tasks/{id}/deps` all go through
`core/tracker_service.py` - the same plain coroutines the agent tools wrap -
with the same journal, marker, ownership and project rules:

| answer | meaning |
|---|---|
| `201`/`200`, `isError: false`, `committed: true` | the journal holds the write; the body is the tracker's read-back |
| `400`, `isError: true`, `committed: false` | the tracker (or the journal) refused; nothing was written |
| `202`, `isError: true`, `committed: null` | the answer never came; the write may or may not have landed - repeat the same `request_id` |

* A `request_id` is scoped to the actor (`user_id`) in the journal: two users
  reusing one string do not replay each other's operations, and a replay never
  transfers a claim or a close from the run that performed it.
* An unknown `project_id` is `404` and writes nothing - it is never silently
  routed to the user's own tracker.
* A page claim is refused (`tracker_claimed_elsewhere`) while a live run holds
  the task; the receipt comes from the binding journal, not from prose.
* The plan board's receipt is the task journal entry, never a model's JSON
  text: an `Agent` result that *says* it claimed something is not a receipt.

Tests: `tests/tasks_reliability.test.mjs` (lost answer, unknown `202`, new key
per action, reload without replay, two payloads never sharing a key, capacity
refusal without eviction, a key that cannot be stored means no write, the real
REST client's wire bodies for close/deps), on top of `tests/test_tasks_api.py`
for the server side of the same contract.

### Final review: container fallback and recovery history

The short-lived Docker fallback mounts the canonical workspace that owns the
tracker, not a `.beads/redirect` alias. Plan graph inputs are staged beside
that actual tracker as well. External storage roots, absolute host database
paths and host-only routing/server settings are refused before starting a
temporary container rather than opening an uncoordinated second database.
Use the configured host backend for those layouts. These mount/argv checks
are covered by isolated tests; a production Docker deployment was not tested.

Releasing a temporary page claim removes its reservation, not its recovery
history: previous owners' checkpoints and the recorded takeover survive, with
a new `released` checkpoint. Review those records and actual tracker markers
when recovering after a hard process kill.
