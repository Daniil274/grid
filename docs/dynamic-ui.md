# Interfaces inside an answer (dynamic UI, first slice)

An answer may carry a small working interface beside its text: a table you can
sort, a criterion you can switch, a row you pick, a button that sends that
choice to the agent. The model composes **data**, never code: no HTML, no
JavaScript, no CSS, no URL, no event-handler string reaches a browser. The
server checks the whole document against a closed catalog and the page renders
only that catalog.

This is the first vertical slice from `/project/grid-dynamic-ui-design.md`. It
is deliberately narrow and honest about what it does not do (see *Limits*).

## What the model calls

One tool, `present_ui(document)`, declared in `tools/ui_tools.py` and discovered
by the shared tool registry (`tools/function_tools.py` scans `*_tools.py` for a
`*_TOOLS` dict). `document` is a **JSON-encoded string** holding one object -
the schema stays a plain string so strict providers accept it - and never a
string of markup. The object looks like this:

```json
{
  "schema_version": 1,
  "id": "comparison",
  "revision": 1,
  "fallback_markdown": "Basic costs 10, Pro costs 25.",
  "state_schema": {
    "sort": {"type": "sort", "table": "options", "label": "Sort by"},
    "choice": {"type": "row", "table": "options", "label": "Chosen option"}
  },
  "initial_state": {"sort": "price:asc", "choice": "a"},
  "root": {
    "id": "layout", "type": "stack", "children": [
      {"id": "sortsel", "type": "select", "label": "Sort by", "bind": "sort",
       "options": [{"value": "price:asc", "label": "Price ascending"},
                   {"value": "price:desc", "label": "Price descending"}]},
      {"id": "options", "type": "table",
       "columns": [{"key": "name", "label": "Name"},
                   {"key": "price", "label": "Price", "align": "end"}],
       "rows": [{"id": "a", "name": "Basic", "price": 10},
                {"id": "b", "name": "Pro", "price": 25}],
       "row_label": "name", "sort_state": "sort", "select_state": "choice"},
      {"id": "discuss", "type": "button", "label": "Discuss", "action": "discuss", "tone": "primary"}
    ]
  },
  "actions": {
    "discuss": {"kind": "submit_to_agent", "label": "Discuss", "prompt": "Discuss the option I chose."}
  }
}
```

The tool returns a short JSON acknowledgement (`ok`, `ui_id`, `revision`,
`actions`, and a note), not the document: the block already went to the screen
and is stored with the answer.

### Catalog

| Component | Fields | Notes |
|---|---|---|
| `stack` | `title?`, `gap?` (`s`/`m`/`l`), `children` | layout only |
| `text` | `text` | plain text, escaped by the client |
| `metric` | `label`, `value`, `unit?`, `hint?` | one number |
| `table` | `title?`, `columns`, `rows`, `row_label?`, `sort?`, `sort_state?`, `select_state?` | columns `{key,label,align?}`; rows `{id,<column>: scalar}`; `sort` is a fixed display order (`{column,direction?}`) used when no `sort_state` value applies |
| `select` | `label`, `bind`, `options` | `{value,label}` choices; binds one state key |
| `button` | `label`, `action`, `tone?` (`default`/`primary`) | `action` must be a declared action |

State keys (`state_schema`) are one of:

- `enum` – a value from a fixed `values` list (bound by a `select`);
- `sort` – `"column:asc"` / `"column:desc"` for a named table (bound by that
  table's `sort_state` and, optionally, one `select`);
- `row` – an id of a row of a named table (bound by that table's
  `select_state` and, optionally, one `select`).

A table and a select may hold the **same** sort/row key – that is how the table
is sorted from a control. A second select for one key, or a second table
claiming one order, is refused (`ui.schema`). Every declared state key must be
bound by some control. A select must offer the value its key starts with:
otherwise the page would show one option while the table used another, and the
first interaction would silently overwrite the real choice (`ui.schema`). A
value that becomes current later (a header click, another tab) is shown by the
select as a "(current)" option rather than hidden behind a different one.

Limits (`web_chat/ui_document.py`): 128 KB document, 300 nodes, depth 8, 100
rows, 12 columns, 50 select options, 8 actions, 12 state keys, 4000 characters
of text, 8000 of fallback. Violations are `ui.limit`.

Unknown components are `ui.unknown_component`; unknown fields, duplicate ids,
bad values, a button naming an unknown action (`ui.unknown_action`) or an
unbound state key are refused too. A table cell may be text, a number or a
boolean – never a nested object.

## Where the block lives

- **Live**: `WebStreamObserver.present_ui` validates once at the door
  (`UiSink`), emits `{"type": "ui", "ui": block}` to the running turn and keeps
  the block for the turn.
- **Stored**: at the end of the turn the blocks are written to the assistant
  message's metadata under `ui` beside the trace and events
  (`ChatSession._persist_trace`). A reload, a second tab or a reconnect reads
  them from the conversation view (`web_chat/views.py`), so a tool payload
  trimmed by compaction cannot take the interface away.
- **Client**: `web_chat/js/ui/ui-block.js` draws the catalog; the pure sorting,
  selection and request-id rules are in `web_chat/js/ui/ui-model.js`.

### State, revisions, idempotency

Each block stores `values` and a monotonic `state_revision`. Sorting and
selection happen on the page; the result is saved with a debounce (400 ms) to:

```
PUT /api/chat/conversations/{context_id}/ui/{message_id}/{ui_id}
{"values": {...}, "state_revision": 3, "request_id": "save-..."}
```

- The server re-validates the values against the **stored** document.
- `state_revision` must equal the stored one; if another tab (or a submit)
  moved it, the answer is `409` with code `ui.stale` and the current values,
  and the page adopts them instead of overwriting a newer choice.
- The check and the write are one atomic step under the conversation
  manager's lock (`ContextManager.mutate_message_metadata_by_id`), so two
  saves arriving at once on different threads cannot both pass the check: one
  saves, the other is told `ui.stale`. A choice is never silently lost while
  the answer says "Saved".
- A repeated `request_id` is the same save: the answer repeats the stored state
  with `"duplicate": true` and nothing changes.
- A failed save is shown as failed (`Not saved` / `Could not save`) – a choice
  is never displayed as saved before the server confirmed it.
- An acknowledgement is merged, not pasted: the revision is always adopted
  (the stored state did move on), but a key the person changed **while that
  request was in flight** keeps the newer local choice, and a follow-up save
  then carries it. Without this a slow save of an older value would erase the
  newer one. The same rule applies to a `ui.stale` answer: the server's values
  win for untouched keys, the newer local input for keys changed since the
  request left. A press waits for the saves it can wait for (bounded), and
  sends the values current at that moment; if saving is impossible (no message
  id yet, no save channel) the press goes ahead with the local values instead
  of waiting forever.
- A block shown during a live turn has no message id yet: its buttons stay
  disabled and it says the choice is stored with the answer; it wakes up when
  the message is stored (no reload needed).

### Buttons and the visible turn

A `submit_to_agent` button sends, over the chat websocket:

```json
{"action": "ui_action", "message_id": "...", "ui_id": "ui-...", "action_id": "discuss",
 "document_revision": 1, "state_revision": 3, "values": {...}, "request_id": "submit-..."}
```

`ChatSession._ui_action` re-checks, in order: the authenticated conversation
(the socket is already bound to it), that the message and block belong to it,
the document revision, that the action exists in the **stored** document, the
values against its state schema, and the client's state revision (a pending
save is flushed first). It then composes the sentence from the stored document
and validated values only (`ui_state.compose_submission` – the action's prompt
plus one labelled line per value) and starts an ordinary turn. The browser's
prompt, tool name or text is never used.

The user message carries `ui_origin` metadata (block id, action, document and
state revisions, the values the agent received); the view exposes it as
`origin` and the bubble shows a short "Chosen in the interface: …" line. A
repeated `request_id` repeats the acknowledgement and starts no second turn.

Every refusal of a button press is a **scoped** `ui_refused` frame carrying the
machine code, the message and the `message_id`/`ui_id` when known. The page
opened that socket for one press, so it settles that attempt (and lets the
widget retry) without sending a `done` over the conversation's own running
turn. Codes: `ui.schema`, `ui.stale`, `ui.not_found`, `ui.busy` (the
conversation is running another turn), `ui.refused` (the turn could not be
started). On success `ui_submitted.origin.state_revision` is the revision the
widget then holds, and the page clears its "Sending…" status.

## Enabling it

The module is discovered automatically, but a system still has to allow the
tool for the agent that should compose interfaces – in that system's config:

```yaml
tools:
  present_ui:
    type: "function"
    description: "Show a small working interface inside the answer."

agents:
  assistant:
    tools:
      - present_ui
```

Only enable it for agents where comparison/selection is useful; a tool catalog
is prompt budget. The tool reads nothing, writes nothing and declares workspace
isolation (`tools/ui_tools.py`), so it is not asked for approval and is allowed
in confined spaces.

`config.yaml` is generated from the system builder; add the declaration to the
system's source configuration rather than editing a generated deployment.
`examples/coder/config.yaml` (the engineering system) already declares
`present_ui` under `tools:`; no agent lists it under its own `tools:` yet, so
the tool is declared for the system but not reachable by an agent. Add it to
that agent's list, as above, when the interface should become callable.

## Fallback and plain text

- Every document must carry `fallback_markdown`. The stored view and the block
  itself keep it; the widget offers it under "Text version", and a client that
  cannot draw the block can show that text.
- A block whose document is missing or has an unknown root shows its fallback
  instead of a guess.
- Export and plain-text clients need no special support: the answer's text is
  still the answer.

## Limits of this slice (explicit)

- **No token-level streaming of the interface.** Text streams first; the block
  appears once, after validation. There are no upsert/remove revision streams
  for a growing tree.
- **No arbitrary expressions, formulas, charts, sliders, checklists, tabs or
  forms.** The catalog is the six components above; arithmetic in a cell is not
  evaluated.
- **No `data_refs`** to tool results. The model composes the snapshot; the
  server does not resolve opaque references yet.
- **No CSP change.** The block is DOM built with `h()`/text nodes; the markdown
  renderer stays escape-first. No HTML from a model is ever parsed.
- **No cross-conversation blocks.** Membership is the authenticated
  conversation the socket/REST call already acts in.
- Exact rendered sizes are not measured for very large tables; the limits bound
  the work (100 rows × 12 columns).

## Tests

- `python3 tests/test_dynamic_ui.py` – 26 stdlib `unittest` cases for the
  document validator (catalog, fields, limits, bindings, values, labels) and
  the stored state (save, duplicate, stale, missing block, composed prompt,
  origin, request-id claims). No pytest or heavy package imports required.
- `node --test tests/dynamic_ui.test.mjs` – 26 cases for the pure model
  (sorting, numeric/stable order, toggle, values copy, request ids) and the
  widget in the fake DOM (render, local sort/selection, debounced save,
  stale adoption, failed save status, submit gate, fixed sort, current-value
  select option, save-before-submit, ack revision/status, focus retention,
  an older answer never overwriting newer local input, a press carrying a
  choice made while it waited, and no spin when nothing can be saved).
- `.map-venv/bin/python -m pytest -q tests/test_dynamic_ui_state.py` – the real
  `ContextManager` (two concurrent saves: exactly one wins) and the websocket
  command loop (stale / busy / missing-block presses are scoped `ui_refused`
  frames and send no `done`).
- `.map-venv/bin/python -m pytest -q tests/test_web_views.py
  tests/test_web_ui_routes.py tests/test_web_chat_session.py` – storage/view
  and chat-transport regressions.

The `.map-venv` interpreter in this checkout has `agents`, `fastapi` and
`pytest`, so the server-side suites run. A live deployment (real browser,
two-tab session, running websocket turn) is still not exercised here: the
engineering system declares `present_ui`, but no agent is wired to it yet.

## Files

- `web_chat/ui_document.py` – catalog, validator, canonical form, value labels.
- `web_chat/ui_state.py` – `UiSink`, block storage, revision/idempotency, submit
  composition, origin, request-id claims.
- `tools/ui_tools.py` – `present_ui`.
- `web_chat/observer.py` – validation at the door, live `ui` frame, turn blocks.
- `web_chat/session.py` – `ui_action` handler, persistence of blocks/origin.
- `web_chat/server.py` – `PUT .../ui/{message_id}/{ui_id}`.
- `web_chat/schemas.py` – `UiStateRequest`.
- `web_chat/views.py` – `ui` and `origin` in the conversation view.
- `web_chat/js/ui/ui-model.js`, `web_chat/js/ui/ui-block.js` – the widget.
- `web_chat/js/ui/message.js`, `transcript.js`, `chat.js`,
  `net/chat-socket.js`, `net/api.js`, `main.js`, `styles/chat.css` – wiring.
