/**
 * A presented interface (web_chat/ui_document.py + js/ui/ui-model.js and
 * js/ui/ui-block.js) in Node: local sorting and selection, the save round
 * trip with its revision and stale handling, and the submit gate.
 *
 * The DOM is the small fake from tests/fake_dom.mjs - enough to build and
 * click, no layout and no innerHTML.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const {
  parseSort, sortRows, nextSortValue, setValue, blockState, fallbackText, requestId,
} = await import("../web_chat/js/ui/ui-model.js");
const { renderUiBlock } = await import("../web_chat/js/ui/ui-block.js");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function sampleBlock(overrides = {}) {
  return {
    id: "ui-abc",
    revision: 2,
    values: { sort: "price:asc", choice: "a" },
    state_revision: 4,
    document: {
      schema_version: 1,
      id: "comparison",
      revision: 2,
      title: "Compare options",
      fallback_markdown: "Basic 10, Pro 25",
      state_schema: {
        sort: { type: "sort", table: "options", label: "Sort by" },
        choice: { type: "row", table: "options", label: "Chosen" },
      },
      initial_state: { sort: "price:asc", choice: "a" },
      root: {
        id: "layout",
        type: "stack",
        gap: "m",
        children: [
          {
            id: "sortsel",
            type: "select",
            label: "Sort by",
            bind: "sort",
            options: [
              { value: "price:asc", label: "Price ascending" },
              { value: "price:desc", label: "Price descending" },
            ],
          },
          {
            id: "options",
            type: "table",
            columns: [{ key: "name", label: "Name" }, { key: "price", label: "Price", align: "end" }],
            rows: [
              { id: "a", name: "Basic", price: 10 },
              { id: "b", name: "Pro", price: 25 },
            ],
            row_label: "name",
            sort_state: "sort",
            select_state: "choice",
          },
          { id: "discuss", type: "button", label: "Discuss", action: "discuss", tone: "primary" },
        ],
      },
      actions: { discuss: { kind: "submit_to_agent", label: "Discuss", prompt: "Discuss it." } },
    },
    ...overrides,
  };
}

// -- the pure model --------------------------------------------------------

test("a sort value is only what the document format allows", () => {
  assert.deepEqual(parseSort("price:asc"), { column: "price", direction: "asc" });
  assert.equal(parseSort("price"), null);
  assert.equal(parseSort("price:up"), null);
  assert.equal(parseSort(null), null);
});

test("sorting is numeric where numbers are, stable, and never mutates", () => {
  const rows = [
    { id: "a", price: 10, name: "Basic" },
    { id: "b", price: 25, name: "Pro" },
    { id: "c", price: 5, name: "Lite" },
  ];
  const before = rows.map((row) => row.id);
  assert.deepEqual(sortRows(rows, "price:asc").map((row) => row.id), ["c", "a", "b"]);
  assert.deepEqual(sortRows(rows, "price:desc").map((row) => row.id), ["b", "a", "c"]);
  assert.deepEqual(rows.map((row) => row.id), before);
  // Equal cells keep their document order in both directions.
  const tied = [{ id: "x", n: 1 }, { id: "y", n: 1 }, { id: "z", n: 1 }];
  assert.deepEqual(sortRows(tied, "n:desc").map((row) => row.id), ["x", "y", "z"]);
});

test("numbers sort before text, so a mixed column never throws", () => {
  const rows = [{ id: "t", v: "text" }, { id: "n", v: 2 }, { id: "m", v: 1 }];
  assert.deepEqual(sortRows(rows, "v:asc").map((row) => row.id), ["m", "n", "t"]);
});

test("a click on a column gives the other direction of the same column", () => {
  assert.equal(nextSortValue("price:asc", "price"), "price:desc");
  assert.equal(nextSortValue("price:desc", "price"), "price:asc");
  assert.equal(nextSortValue("price:asc", "name"), "name:asc");
  assert.equal(nextSortValue(undefined, "name"), "name:asc");
});

test("values are copied, not edited in place", () => {
  const values = { sort: "a:asc" };
  const next = setValue(values, "sort", "a:desc");
  assert.deepEqual(values, { sort: "a:asc" });
  assert.deepEqual(next, { sort: "a:desc" });
});

test("a malformed block gets a safe empty state, not an exception", () => {
  assert.deepEqual(blockState(null), { values: {}, revision: 0 });
  assert.deepEqual(blockState({ values: { a: 1 }, state_revision: 3 }), { values: { a: 1 }, revision: 3 });
  assert.deepEqual(blockState({ values: null, state_revision: -1 }), { values: {}, revision: 0 });
  assert.equal(fallbackText(sampleBlock()), "Basic 10, Pro 25");
});

test("request ids are unique, so two saves are two requests", () => {
  const ids = new Set(Array.from({ length: 50 }, () => requestId("save")));
  assert.equal(ids.size, 50);
  for (const id of ids) assert.ok(id.length <= 64);
});

// -- the widget ------------------------------------------------------------

test("the widget draws the catalog and the text fallback", () => {
  const widget = renderUiBlock(sampleBlock());
  assert.equal(widget.el.querySelectorAll(".uiTable__row").length, 2);
  assert.equal(widget.el.querySelector(".uiSelect").value, "price:asc");
  assert.ok(widget.el.querySelector(".uiButton"));
  assert.equal(widget.el.querySelector(".uiBlock__fallback").textContent, "Basic 10, Pro 25");
});

test("a block without a usable document shows its fallback, not a guess", () => {
  const widget = renderUiBlock({ id: "x", document: { root: { type: "iframe" } }, values: {}, state_revision: 0 });
  assert.ok(widget.el.classList.contains("is-fallback"));
  assert.equal(widget.el.querySelector(".uiBlock__fallback").textContent, "This interface cannot be shown here.");
});

test("a save sends the values and the revision the page is looking at", async () => {
  const calls = [];
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => {
      calls.push(payload);
      return { values: payload.values, state_revision: payload.stateRevision + 1, saved: true, duplicate: false };
    },
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  await sleep(520);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].messageId, "msg-1");
  assert.equal(calls[0].uiId, "ui-abc");
  assert.deepEqual(calls[0].values, { sort: "price:desc", choice: "a" });
  assert.equal(calls[0].stateRevision, 4);
  assert.match(calls[0].requestId, /^save-/);
  assert.equal(widget.getValues().sort, "price:desc");
  // The table now shows the descending order, locally.
  assert.equal(widget.el.querySelectorAll(".uiTable__row")[0].dataset.rowId, "b");
});

test("a nil message id keeps the choice local and says so", async () => {
  const widget = renderUiBlock(sampleBlock(), { getMessageId: () => null, onSave: async () => { throw new Error("must not be called"); } });
  const status = widget.el.querySelector(".uiBlock__status");
  assert.match(status.textContent, /turn is stored/);
  assert.equal(widget.el.querySelector(".uiButton").disabled, true);
});

test("a stale save adopts the server's values instead of overwriting them", async () => {
  let first = true;
  const calls = [];
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => {
      calls.push(payload);
      if (first) {
        first = false;
        const error = new Error("changed elsewhere");
        error.payload = { code: "ui.stale", values: { sort: "price:asc", choice: "b" }, state_revision: 9 };
        throw error;
      }
      return { values: payload.values, state_revision: payload.stateRevision + 1 };
    },
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  await sleep(520);
  assert.equal(widget.el.querySelector(".uiBlock__status").dataset.tone, "warn");
  assert.equal(widget.el.querySelector(".uiSelect").value, "price:asc");
  assert.equal(widget.el.findAll("is-selected")[0].dataset.rowId, "b");
  // The next save starts from the revision the server actually holds.
  const again = widget.el.querySelector(".uiSelect");
  again.value = "price:desc";
  for (const fn of again.listeners.change ?? []) fn({ target: again });
  await sleep(520);
  assert.equal(calls.at(-1).stateRevision, 9);
});

test("a failed save is not shown as saved", async () => {
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async () => { throw new Error("boo"); },
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  await sleep(520);
  const status = widget.el.querySelector(".uiBlock__status");
  assert.equal(status.dataset.tone, "error");
  assert.match(status.textContent, /Could not save/);
});

test("a header click sorts locally and saves the new sort state", async () => {
  const calls = [];
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => {
      calls.push(payload);
      return { values: payload.values, state_revision: payload.stateRevision + 1 };
    },
  });
  const sorts = widget.el.querySelectorAll(".uiTable__sort");
  sorts[1].click(); // "Price", currently ascending
  assert.equal(widget.el.querySelectorAll(".uiTable__row")[0].dataset.rowId, "b");
  await sleep(520);
  assert.equal(calls.at(-1).values.sort, "price:desc");
});

test("clicking a row selects it and the choice is saved", async () => {
  const calls = [];
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => {
      calls.push(payload);
      return { values: payload.values, state_revision: payload.stateRevision + 1 };
    },
  });
  const rows = widget.el.querySelectorAll(".uiTable__row");
  rows[1].click();
  // The table is repainted from the new state, so ask the new rows.
  assert.equal(widget.el.findAll("is-selected")[0].dataset.rowId, "b");
  await sleep(520);
  assert.equal(calls.at(-1).values.choice, "b");
});

test("a button stays disabled until the answer has an id, then submits checked ids", async () => {
  const submitted = [];
  let messageId = null;
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => messageId,
    onSave: async (payload) => ({ values: payload.values, state_revision: payload.stateRevision + 1 }),
    onSubmit: async (payload) => { submitted.push(payload); return true; },
  });
  const button = widget.el.querySelector(".uiButton");
  assert.equal(button.disabled, true);
  button.click();
  await sleep(20);
  assert.equal(submitted.length, 0);
  messageId = "msg-1";
  widget.refresh();
  assert.equal(button.disabled, false);
  button.click();
  await sleep(20);
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0].actionId, "discuss");
  assert.equal(submitted[0].documentRevision, 2);
  assert.equal(submitted[0].stateRevision, 4);
  assert.deepEqual(submitted[0].values, { sort: "price:asc", choice: "a" });
  assert.match(submitted[0].requestId, /^submit-/);
});

test("a table's fixed sort orders its rows even without a sort state", () => {
  const block = {
    id: "ui-fixed",
    revision: 1,
    values: {},
    state_revision: 0,
    document: {
      root: {
        id: "options",
        type: "table",
        columns: [{ key: "name", label: "Name" }, { key: "price", label: "Price" }],
        rows: [{ id: "a", name: "Basic", price: 10 }, { id: "b", name: "Pro", price: 25 }],
        sort: { column: "price", direction: "desc" },
      },
    },
  };
  const widget = renderUiBlock(block);
  assert.deepEqual(widget.el.querySelectorAll(".uiTable__row").map((row) => row.dataset.rowId), ["b", "a"]);
  // A fixed sort is display only: nothing becomes a bound, saved state.
  assert.deepEqual(widget.getValues(), {});
});

test("a select shows the value it actually binds, even outside its options", () => {
  // A header click or another tab can produce a sort this select does not list.
  const widget = renderUiBlock(sampleBlock({ values: { sort: "name:asc", choice: "a" } }));
  const select = widget.el.querySelector(".uiSelect");
  assert.equal(select.value, "name:asc");
  assert.match(select.textContent, /name .*current/);
  assert.deepEqual(widget.getValues(), { sort: "name:asc", choice: "a" });
});

test("a press waits for an in-flight save and sends the saved revision", async () => {
  const calls = [];
  let release;
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: (payload) => new Promise((resolve) => {
      calls.push({ kind: "save", payload });
      release = () => resolve({ values: payload.values, state_revision: payload.stateRevision + 1 });
    }),
    onSubmit: async (payload) => {
      calls.push({ kind: "submit", payload });
      return true;
    },
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  // The debounce has not fired yet; the press itself must flush it first.
  widget.el.querySelector(".uiButton").click();
  await sleep(20);
  assert.deepEqual(calls.map((call) => call.kind), ["save"]);
  release();
  await sleep(20);
  assert.deepEqual(calls.map((call) => call.kind), ["save", "submit"]);
  assert.equal(calls[0].payload.stateRevision, 4);
  assert.equal(calls[1].payload.stateRevision, 5);
  assert.deepEqual(calls[1].payload.values, { sort: "price:desc", choice: "a" });
});

test("a sent press adopts the ack revision, stops saying Sending and can fail back", async () => {
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => ({ values: payload.values, state_revision: payload.stateRevision + 1 }),
    onSubmit: async () => true,
  });
  widget.el.querySelector(".uiButton").click();
  await sleep(20);
  assert.equal(widget.el.querySelector(".uiBlock__status").textContent, "Sending…");
  widget.acknowledge({ stateRevision: 7, sent: true });
  const status = widget.el.querySelector(".uiBlock__status");
  assert.equal(status.textContent, "Sent");
  assert.equal(status.dataset.tone, "ok");
  // A refusal of the same press lets the person try again.
  widget.acknowledge({ stateRevision: 7, failed: true });
  assert.equal(widget.el.querySelector(".uiButton").disabled, false);
  assert.match(widget.el.querySelector(".uiBlock__status").textContent, /Not sent/);
});

test("a repaint puts keyboard focus back on the same control", async () => {
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => ({ values: payload.values, state_revision: payload.stateRevision + 1 }),
  });
  const select = widget.el.querySelector(".uiSelect");
  select.focus();
  assert.equal(document.activeElement, select);
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  const next = widget.el.querySelector(".uiSelect");
  assert.notEqual(next, select);
  assert.equal(document.activeElement, next);
});

// -- input that arrives while a request is in flight -----------------------

test("a slow save of an older choice cannot overwrite a newer local one", async () => {
  const calls = [];
  let releaseFirst;
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: (payload) => new Promise((resolve) => {
      calls.push(payload);
      const done = () => resolve({ values: payload.values, state_revision: payload.stateRevision + 1 });
      if (calls.length === 1) releaseFirst = done;
      else done();
    }),
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  await sleep(450); // the debounce fired: the first save is on the wire
  assert.equal(calls.length, 1);
  // The person chooses again before the first save is answered.
  const row = widget.el.querySelectorAll(".uiTable__row").find((item) => item.dataset.rowId === "b");
  row.click();
  assert.deepEqual(widget.getValues(), { sort: "price:desc", choice: "b" });
  releaseFirst();
  await sleep(500);
  assert.equal(calls.length, 2);
  // The follow-up save carries the new choice, not the old snapshot the
  // acknowledgement brought back.
  assert.deepEqual(calls[1].values, { sort: "price:desc", choice: "b" });
  assert.deepEqual(widget.getValues(), { sort: "price:desc", choice: "b" });
  assert.equal(widget.el.querySelector(".uiSelect").value, "price:desc");
  assert.equal(widget.el.findAll("is-selected")[0].dataset.rowId, "b");
  assert.equal(widget.el.querySelector(".uiBlock__status").dataset.tone, "ok");
});

test("a stale answer keeps only what changed while that save was in flight", async () => {
  const calls = [];
  let releaseFirst;
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: (payload) => new Promise((resolve, reject) => {
      calls.push(payload);
      if (calls.length === 1) {
        releaseFirst = () => {
          const error = new Error("changed elsewhere");
          error.payload = { code: "ui.stale", values: { sort: "price:asc", choice: "a" }, state_revision: 9 };
          reject(error);
        };
        return;
      }
      resolve({ values: payload.values, state_revision: payload.stateRevision + 1 });
    }),
  });
  // The choice is what leaves first; the sort is changed while it is in flight.
  const row = widget.el.querySelectorAll(".uiTable__row").find((item) => item.dataset.rowId === "b");
  row.click();
  await sleep(450);
  assert.equal(calls.length, 1);
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  releaseFirst();
  await sleep(500);
  // The server's stale state wins for the key this tab did not touch since
  // the request left, and the newer local sort is kept.
  assert.deepEqual(widget.getValues(), { sort: "price:desc", choice: "a" });
  assert.equal(widget.el.querySelector(".uiSelect").value, "price:desc");
  assert.equal(widget.el.findAll("is-selected")[0].dataset.rowId, "a");
  // ...and the resend starts from the revision the server actually holds.
  assert.equal(calls.at(-1).stateRevision, 9);
});

test("a press waits for a save and sends the choice made while it waited", async () => {
  const calls = [];
  let releaseFirst;
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: (payload) => new Promise((resolve) => {
      calls.push({ kind: "save", payload });
      const done = () => resolve({ values: payload.values, state_revision: payload.stateRevision + 1 });
      if (calls.length === 1) releaseFirst = done;
      else done();
    }),
    onSubmit: async (payload) => {
      calls.push({ kind: "submit", payload });
      return true;
    },
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  widget.el.querySelector(".uiButton").click(); // flushes the debounce itself
  await sleep(20);
  assert.deepEqual(calls.map((call) => call.kind), ["save"]);
  // A choice lands while the press is still waiting for that save.
  const row = widget.el.querySelectorAll(".uiTable__row").find((item) => item.dataset.rowId === "b");
  row.click();
  releaseFirst();
  await sleep(50);
  assert.deepEqual(calls.map((call) => call.kind), ["save", "save", "submit"]);
  assert.deepEqual(calls[1].payload.values, { sort: "price:desc", choice: "b" });
  assert.deepEqual(calls[2].payload.values, { sort: "price:desc", choice: "b" });
  assert.equal(calls[2].payload.stateRevision, 6); // after both saves
});

test("a press with no save channel does not wait for a quiet that never comes", async () => {
  const submitted = [];
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSubmit: async (payload) => {
      submitted.push(payload);
      return true;
    },
  });
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  widget.el.querySelector(".uiButton").click();
  await sleep(50);
  assert.equal(submitted.length, 1);
  assert.deepEqual(submitted[0].values, { sort: "price:desc", choice: "a" });
});

test("a press acknowledgement does not undo a change made while it was in flight", async () => {
  let releaseSubmit;
  const widget = renderUiBlock(sampleBlock(), {
    getMessageId: () => "msg-1",
    onSave: async (payload) => ({ values: payload.values, state_revision: payload.stateRevision + 1 }),
    onSubmit: () => new Promise((resolve) => { releaseSubmit = () => resolve(true); }),
  });
  widget.el.querySelector(".uiButton").click();
  await sleep(20);
  const select = widget.el.querySelector(".uiSelect");
  select.value = "price:desc";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  releaseSubmit();
  await sleep(50);
  widget.acknowledge({ stateRevision: 6, values: { sort: "price:asc", choice: "a" }, sent: true });
  assert.deepEqual(widget.getValues(), { sort: "price:desc", choice: "a" });
  assert.equal(widget.el.querySelector(".uiSelect").value, "price:desc");
  assert.equal(widget.el.querySelector(".uiBlock__status").textContent, "Sent");
});
