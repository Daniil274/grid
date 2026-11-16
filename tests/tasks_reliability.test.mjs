/**
 * The tracker's reliability (web_chat/js/tasks/page.js): a dependency ring is
 * refused with the chain named, a note is added to what a task held instead of
 * replacing it, a background refresh keeps the board when it fails but shows a
 * change when it succeeds, the empty/error/unavailable states stay distinct,
 * and the card shows what it is blocked by. The API is stubbed.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { TasksPage, dependencyRing, ringReason, notesWithAddition } = await import("../web_chat/js/tasks/page.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);
const tick = () => new Promise((resolve) => setImmediate(resolve));

function nodes() {
  return {
    status: el("div"), presets: el("div"), activeFilters: el("div"), undo: el("div"), metrics: el("div"),
    board: el("div"), listWrap: el("div"), listRows: el("tbody"), search: el("input"), type: el("select"),
    priority: el("select"), statusFilter: el("select"), project: el("select"),
    viewBoard: el("button"), viewList: el("button"), newTask: el("button"), refresh: el("button"),
  };
}

const TASKS = () => [
  {
    id: "bd-1", title: "Alpha", state: "ready", status: "open", priority: 0, type: "feature",
    assignee: null, description: "", acceptance: "", notes: "first note",
    close_reason: null, depends_on: [], waits_for: [], updated_at: "2026-10-01T10:00:00",
  },
  {
    id: "bd-2", title: "Beta", state: "blocked", status: "open", priority: 2, type: "bug",
    assignee: null, description: "", acceptance: "", notes: "",
    close_reason: null, depends_on: ["bd-1"], waits_for: ["Alpha"], updated_at: "2026-10-02T10:00:00",
  },
  {
    id: "bd-3", title: "Gamma", state: "ready", status: "open", priority: 3, type: "task",
    assignee: null, description: "", acceptance: "", notes: "",
    close_reason: null, depends_on: [], waits_for: [], updated_at: "2026-10-03T10:00:00",
  },
];

const report = (tasks) => ({ available: true, reason: null, tasks, total: tasks.length });
const byLabel = (root, label) => root.querySelectorAll("select").concat(root.querySelectorAll("input"), root.querySelectorAll("textarea"))
  .find((node) => node.getAttribute("aria-label") === label);
const byText = (root, text) => root.querySelectorAll("button").find((button) => button.textContent === text);
const submit = (dialog) => dialog.querySelector("form").listeners.submit[0]({ preventDefault() {} });

// -- notes as addition, not overwrite ---------------------------------------------

test("notesWithAddition appends what is written and keeps what was recorded", () => {
  assert.equal(notesWithAddition("kept", "added"), "kept\n\nadded");
  assert.equal(notesWithAddition("", "added"), "added");
  assert.equal(notesWithAddition("kept", "  "), "kept", "blank input changes nothing");
  assert.equal(notesWithAddition(null, "added"), "added");
});

test("editing a task adds a note to what it held; the recorded notes are never lost", async () => {
  const patches = [];
  api.tasks = async () => report(TASKS());
  api.updateTask = async (id, patch) => { patches.push({ id, patch }); return { ok: true }; };

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[0], page.tasks);
  const dialog = page.dialog.el;
  // The recorded note stays in view while the box to add a note is empty.
  assert.match(dialog.findAll("taskDialog__notesExisting")[0].find("taskDialog__prose").innerHTML, /first note/);
  assert.equal(byLabel(dialog, "Notes").value, "");
  byText(dialog, "Edit").click();
  byLabel(dialog, "Notes").value = "second note";
  await submit(dialog);

  assert.equal(patches.length, 1);
  assert.equal(patches[0].id, "bd-1");
  assert.equal(patches[0].patch.notes, "first note\n\nsecond note");
  // The write carries its action key on the wire, as the server's journal reads it.
  assert.match(patches[0].patch.request_id, /^[0-9a-f-]{36}$/i);
});

// -- a dependency ring is refused, with the chain named -----------------------------

test("dependencyRing finds the chain a new edge would close", () => {
  const tasks = TASKS();
  // bd-2 already waits for bd-1: bd-1 → bd-2 would close the ring.
  assert.deepEqual(dependencyRing(tasks, "bd-1", "bd-2"), ["bd-2", "bd-1"]);
  assert.equal(dependencyRing(tasks, "bd-1", "bd-3"), null, "an independent task is safe");
  assert.deepEqual(dependencyRing(tasks, "bd-1", "bd-1"), ["bd-1"], "a self edge is a ring");
  assert.equal(dependencyRing(tasks, null, "bd-2"), null, "a task not yet created cannot close one");
});

test("a ringReason names the would-be cycle in the tasks' titles", () => {
  const reason = ringReason(TASKS(), "bd-1", "bd-2", ["bd-2", "bd-1"]);
  assert.match(reason, /cycle/);
  assert.match(reason, /Alpha → Beta → Alpha/);
});

test("adding a dependency that would close a ring is refused in the dialog, before the API", async () => {
  const deps = [];
  api.tasks = async () => report(TASKS());
  api.taskDep = async (id, action, parentId) => { deps.push({ id, action, parentId }); return { ok: true }; };
  api.updateTask = async () => ({ ok: true });

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[0], page.tasks);
  const dialog = page.dialog.el;
  byText(dialog, "Edit").click();
  byLabel(dialog, "Depends on").value = "bd-2";
  byText(dialog, "Depends on").click();

  const error = dialog.findAll("taskDialog__error")[0];
  assert.equal(error.hidden, false, "the refusal is shown in place");
  assert.match(error.textContent, /cycle/);
  // Nothing was drafted, so saving sends no dependency write.
  await submit(dialog);
  assert.deepEqual(deps, []);
});

test("a safe dependency is still drafted and saved", async () => {
  const deps = [];
  api.tasks = async () => report(TASKS());
  api.taskDep = async (id, action, parentId) => { deps.push({ id, action, parentId }); return { ok: true }; };
  api.updateTask = async () => ({ ok: true });

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[2], page.tasks); // Gamma, depends on nothing
  const dialog = page.dialog.el;
  byText(dialog, "Edit").click();
  byLabel(dialog, "Depends on").value = "bd-1";
  byText(dialog, "Depends on").click();
  assert.equal(dialog.findAll("taskDialog__error")[0].hidden, true, "a safe edge shows no error");
  await submit(dialog);
  assert.deepEqual(deps, [{ id: "bd-3", action: "add", parentId: "bd-1" }]);
});

// -- what a card is blocked by ------------------------------------------------------

test("a task that waits shows the names it is blocked by", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();

  const blocked = page.nodes.board.findAll("tasksCard__blocked");
  assert.equal(blocked.length, 1, "only the waiting task carries the row");
  assert.match(blocked[0].textContent, /blocked by/);
  assert.match(blocked[0].textContent, /Alpha/);
});

// -- live refresh: the background changes the board, safely -------------------------

test("a background refresh shows what changed elsewhere on the board", async () => {
  let tasks = TASKS();
  api.tasks = async () => report(tasks);
  const page = new TasksPage(nodes());
  await page.load();
  assert.equal(page.nodes.board.findAll("tasksCard").length, 3);

  // Another tab (or an agent) adds a task: the poll picks it up.
  tasks = [...tasks, { ...TASKS()[2], id: "bd-4", title: "Delta", updated_at: "2026-10-04T10:00:00" }];
  await page.load({ background: true });
  assert.equal(page.nodes.board.findAll("tasksCard").length, 4);
  assert.ok(page.nodes.board.textContent.includes("Delta"));
});

test("a failed background refresh keeps the last known board and offers a retry", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  assert.equal(page.nodes.board.findAll("tasksCard").length, 3);

  api.tasks = async () => { throw new TypeError("network down"); };
  await page.load({ background: true });

  // The board the user is reading is untouched; the failure is a distinct, retryable note.
  assert.equal(page.tasks.length, 3);
  assert.equal(page.nodes.board.findAll("tasksCard").length, 3);
  assert.equal(page.available, true);
  assert.match(page.nodes.status.textContent, /Couldn't refresh/);
  assert.ok(page.nodes.status.querySelectorAll("button").some((button) => button.textContent === "Retry"));
});

test("a first load that fails is an honest error state with a retry, not an empty board", async () => {
  api.tasks = async () => { throw new Error("server unreachable"); };
  const page = new TasksPage(nodes());
  await page.load();
  assert.equal(page.available, false);
  assert.equal(page.nodes.status.dataset.error, "true");
  assert.match(page.nodes.status.textContent, /server unreachable/);
  assert.ok(page.nodes.status.querySelectorAll("button").some((button) => button.textContent === "Retry"));
});

test("an unavailable tracker is distinct from an empty one, and from a failure", async () => {
  api.tasks = async () => ({ available: false, reason: "no beads database", tasks: [], total: 0 });
  const page = new TasksPage(nodes());
  await page.load();
  assert.equal(page.nodes.status.dataset.error, "false");
  assert.match(page.nodes.status.textContent, /no beads database/);
  assert.ok(page.nodes.status.querySelectorAll("button").some((button) => button.textContent === "Initialize tracker"));

  // An available but empty tracker says so plainly: nothing is broken.
  api.tasks = async () => report([]);
  await page.load();
  assert.match(page.nodes.status.textContent, /No tasks in this tracker yet/);
});

// -- one key per action: lost answers, unknown receipts, reload ---------------------

const { TrackerMutations, PENDING_PREFIX, actionScope } = await import("../web_chat/js/tasks/mutations.js");

/** The browser's sessionStorage, for the duration of one test. */
function memoryStorage() {
  const map = new Map();
  return {
    map,
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => void map.set(key, String(value)),
    removeItem: (key) => void map.delete(key),
    key: (index) => [...map.keys()][index] ?? null,
    get length() { return map.size; },
  };
}

const wire = (body) => JSON.parse(JSON.stringify(body));
const unknown = (error) => error?.unknown === true;

test("a create whose answer was lost keeps its key; the retry makes no second task", async () => {
  const storage = memoryStorage();
  const keys = [];
  const client = {
    createTask: async (body) => {
      keys.push(wire(body).request_id);
      if (keys.length === 1) throw new TypeError("network down");
      return { ok: true, isError: false, committed: true, id: "bd-9" };
    },
  };
  const writes = new TrackerMutations({ storage, apiClient: client });

  await assert.rejects(writes.createTask({ title: "Same" }), unknown);
  assert.equal(writes.pending().length, 1, "the unconfirmed action is kept");
  assert.equal(writes.pending()[0].payload.title, "Same");

  const answer = await writes.createTask({ title: "Same" });
  assert.equal(answer.id, "bd-9");
  assert.equal(new Set(keys).size, 1, "the retry sent the first attempt's key");
  assert.equal(writes.pending().length, 0, "a confirmed action is settled");
});

test("an HTTP 202 with committed: null is not success", async () => {
  const storage = memoryStorage();
  const client = {
    updateTask: async () => ({
      ok: false, isError: true, code: "tracker_unknown", committed: null,
      reason: "bd did not answer: the change may or may not have been applied",
    }),
  };
  const writes = new TrackerMutations({ storage, apiClient: client });

  await assert.rejects(writes.updateTask("bd-1", { title: "New" }), (error) => {
    assert.equal(error.unknown, true);
    assert.match(error.message, /may or may not/);
    return true;
  });
  assert.equal(writes.pending().length, 1, "an unknown outcome stays retryable");
});

test("a new user action after a success gets a new key, even with the same payload", async () => {
  const storage = memoryStorage();
  const keys = [];
  const client = {
    createTask: async (body) => {
      keys.push(wire(body).request_id);
      return { ok: true, isError: false, committed: true, id: `bd-${keys.length}` };
    },
  };
  const writes = new TrackerMutations({ storage, apiClient: client });

  await writes.createTask({ title: "Two of these" });
  await writes.createTask({ title: "Two of these" });
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1], "two deliberate creates are two actions");
});

test("a reload keeps an unconfirmed action and replays nothing by itself", async () => {
  const storage = memoryStorage();
  const first = new TrackerMutations({
    storage,
    apiClient: { createTask: async () => { throw new TypeError("connection reset"); } },
  });
  await assert.rejects(first.createTask({ title: "Pending" }), unknown);

  let calls = 0;
  const keys = [];
  const after = new TrackerMutations({
    storage,
    apiClient: {
      createTask: async (body) => {
        calls += 1;
        keys.push(wire(body).request_id);
        return { ok: true, isError: false, committed: true, id: "bd-10" };
      },
    },
  });

  // Nothing was retried on load: the page decides that, the store does not.
  assert.equal(calls, 0);
  const pending = after.pending();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].payload.title, "Pending");
  assert.equal(pending[0].action, "create");

  await after.createTask({ title: "Pending" });
  assert.equal(new Set(keys).size, 1, "the retry uses the key the reload kept");
});

test("a full record refuses a new action instead of evicting an unconfirmed one", async () => {
  const storage = memoryStorage();
  storage.setItem("grid.token", "secret-session-token");
  const sent = [];
  const client = {
    createTask: async (body) => {
      sent.push(wire(body).request_id);
      throw new TypeError("offline");
    },
  };
  const writes = new TrackerMutations({ storage, apiClient: client, limit: 2 });

  await assert.rejects(writes.createTask({ title: "One" }), unknown);
  await assert.rejects(writes.createTask({ title: "Two" }), unknown);
  assert.equal(writes.pending().length, 2);

  // A third unknown action does not fit: it is refused *before* anything is sent.
  await assert.rejects(writes.createTask({ title: "Three" }), (error) => {
    assert.equal(error.code, "tracker_pending_capacity");
    assert.match(error.message, /nothing was sent/i);
    return true;
  });
  assert.equal(sent.length, 2, "the refused action never reached the network");
  assert.deepEqual(
    writes.pending().map((entry) => entry.payload.title).sort(),
    ["One", "Two"],
    "the unconfirmed actions are all still there: nothing was evicted",
  );
  assert.equal(storage.getItem("grid.token"), "secret-session-token", "a token is never lost or overwritten");

  // A retry of an action the record already holds is still allowed, and reuses its key.
  await assert.rejects(writes.createTask({ title: "One" }), unknown);
  assert.equal(sent.length, 3);
  assert.equal(sent[2], sent[0], "the retry of a stored action reuses its key");
});

test("a record full in bytes refuses a new action and keeps what it held", async () => {
  const storage = memoryStorage();
  storage.setItem("grid.token", "secret-session-token");
  const sent = [];
  const writes = new TrackerMutations({
    storage,
    limit: 40,
    bytes: 600,
    apiClient: { createTask: async (body) => { sent.push(wire(body).request_id); throw new TypeError("offline"); } },
  });

  await assert.rejects(writes.createTask({ title: "Small" }), unknown);
  const kept = storage.getItem(`${PENDING_PREFIX}index`);
  await assert.rejects(writes.createTask({ title: "Big", description: "x".repeat(4000) }), (error) => {
    assert.equal(error.code, "tracker_pending_capacity");
    return true;
  });
  assert.equal(sent.length, 1, "the action that did not fit was not sent");
  assert.equal(storage.getItem(`${PENDING_PREFIX}index`), kept, "the stored record is untouched");
  assert.deepEqual(writes.pending().map((entry) => entry.payload.title), ["Small"]);
});

test("the key is stored before the write leaves; an unstorable key means no write", async () => {
  const storage = memoryStorage();
  storage.setItem("grid.token", "secret-session-token");
  const sent = [];
  const failing = { ...storage, setItem: () => { throw new Error("QuotaExceededError"); } };
  const writes = new TrackerMutations({
    storage: failing,
    apiClient: { createTask: async (body) => { sent.push(wire(body)); return { ok: true, isError: false, committed: true }; } },
  });

  await assert.rejects(writes.createTask({ title: "Check" }), (error) => {
    assert.equal(error.code, "tracker_pending_undurable");
    return true;
  });
  assert.deepEqual(sent, [], "a key a reload would lose is not used for a write");
  assert.equal(writes.durable, false);
  assert.equal(storage.getItem("grid.token"), "secret-session-token");
});

test("the action identity is the exact payload, so two payloads cannot share a key", async () => {
  const one = actionScope("create", { title: "Alpha" });
  const other = actionScope("create", { title: "Alphb" });
  const same = actionScope("create", { title: "Alpha" });
  assert.equal(one.canonical, '{"title":"Alpha"}');
  assert.equal(one.scope, 'create:{"title":"Alpha"}', "the identity is the payload itself");
  assert.equal(one.scope, same.scope, "the same payload is the same action");
  assert.notEqual(one.scope, other.scope, "one changed character is another action");

  // Key order does not change the action; a different value does.
  assert.equal(
    actionScope("update", { id: "bd-1", patch: { a: 1, b: 2 } }).scope,
    actionScope("update", { id: "bd-1", patch: { b: 2, a: 1 } }).scope,
  );

  // Two actions that only differ in the payload are two keys on the wire, even
  // with the same title text at the start.
  const storage = memoryStorage();
  const keys = [];
  const writes = new TrackerMutations({
    storage,
    apiClient: { createTask: async (body) => { keys.push(wire(body).request_id); return { ok: true, isError: false, committed: true }; } },
  });
  await writes.createTask({ title: "Same", priority: 1 });
  await writes.createTask({ title: "Same", priority: 2 });
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1], "a different payload never inherits another action's key");
});

test("a stored record is reused only by the exact payload it belongs to", async () => {
  const storage = memoryStorage();
  const recorded = actionScope("create", { title: "Someone else" });
  storage.setItem(`${PENDING_PREFIX}index`, JSON.stringify({
    [recorded.scope]: { key: "11111111-1111-4111-8111-111111111111", action: "create", at: 1 },
  }));
  const sent = [];
  const writes = new TrackerMutations({
    storage,
    apiClient: { createTask: async (body) => { sent.push(wire(body)); return { ok: true, isError: false, committed: true }; } },
  });

  await writes.createTask({ title: "Mine" });
  assert.equal(sent.length, 1);
  assert.notEqual(sent[0].request_id, "11111111-1111-4111-8111-111111111111",
    "another payload's key is never reused");

  // The record that was there is untouched and keeps its own key.
  const pending = writes.pending();
  assert.deepEqual(pending.map((entry) => entry.payload.title), ["Someone else"]);
  assert.equal(pending[0].key, "11111111-1111-4111-8111-111111111111");
});

test("close and dependency writes reach the shared client with their optional key", async () => {
  const storage = memoryStorage();
  const calls = [];
  const client = {
    closeTask: async (id, reason, options) => {
      calls.push({ kind: "close", id, reason, options });
      if (calls.length === 1) throw new TypeError("connection reset");
      return { ok: true, isError: false, committed: true };
    },
    taskDep: async (id, action, parentId, options) => {
      calls.push({ kind: "dep", id, action, parentId, options });
      return { ok: true, isError: false, committed: true };
    },
  };
  const writes = new TrackerMutations({ storage, apiClient: client });

  await assert.rejects(writes.closeTask("bd-1", "Done"), unknown);
  await writes.closeTask("bd-1", "Done");
  assert.equal(calls[0].options.request_id, calls[1].options.request_id, "the close retry reuses its key");
  assert.equal(calls[1].reason, "Done");

  await writes.taskDep("bd-1", "add", "bd-2");
  assert.equal(calls[2].action, "add");
  assert.ok(calls[2].options.request_id, "the dependency carries a key through the client");
  assert.notEqual(calls[2].options.request_id, calls[0].options.request_id);
});

test("the real REST client puts request_id in the close and dependency bodies", async () => {
  // The `api` this file stubs above is replaced by stubs; a fresh module instance
  // is the real client.
  const { api: realApi } = await import("../web_chat/js/net/api.js?wire");
  const seen = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    seen.push({ url, body: JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => ({ ok: true, isError: false, committed: true }) };
  };
  try {
    await realApi.closeTask("bd-1", "Done", { request_id: "key-close" });
    await realApi.taskDep("bd-1", "add", "bd-2", { request_id: "key-dep" });
    // Without a key the shared methods send exactly what they sent before.
    await realApi.closeTask("bd-1", "Done");
    await realApi.taskDep("bd-1", "remove", "bd-2");
  } finally {
    globalThis.fetch = previous;
  }

  assert.deepEqual(seen[0], { url: "/api/tasks/bd-1/close", body: { reason: "Done", request_id: "key-close" } });
  assert.deepEqual(seen[1], { url: "/api/tasks/bd-1/deps", body: { action: "add", parent_id: "bd-2", request_id: "key-dep" } });
  assert.deepEqual(seen[2].body, { reason: "Done" }, "no key, no extra field");
  assert.deepEqual(seen[3].body, { action: "remove", parent_id: "bd-2" });
});

test("a definite refusal ends the action, so a corrected retry is a new one", async () => {
  const storage = memoryStorage();
  const keys = [];
  const client = {
    updateTask: async (_id, patch) => {
      keys.push(wire(patch).request_id);
      if (keys.length === 1) {
        const error = new Error("Nothing to update.");
        error.status = 400;
        error.payload = { ok: false, isError: true, committed: false, reason: "Nothing to update." };
        throw error;
      }
      return { ok: true, isError: false, committed: true };
    },
  };
  const writes = new TrackerMutations({ storage, apiClient: client });

  await assert.rejects(writes.updateTask("bd-1", { title: "A" }), (error) => error.unknown !== true);
  assert.equal(writes.pending().length, 0, "nothing was written, nothing is pending");

  await writes.updateTask("bd-1", { title: "A" });
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1], "a refusal is not an unknown outcome");
});

test("the page's create carries a key and its retry after a lost answer reuses it", async () => {
  const storage = memoryStorage();
  globalThis.sessionStorage = storage;
  const bodies = [];
  api.tasks = async () => report(TASKS());
  api.createTask = async (spec) => {
    bodies.push(wire(spec));
    if (bodies.length === 1) throw new TypeError("network down");
    return { ok: true, isError: false, committed: true, ...TASKS()[0], id: "bd-9", title: spec.title };
  };

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openCreate(page.tasks);
  const dialog = page.dialog.el;
  byLabel(dialog, "Title").value = "Same title";
  await submit(dialog);
  await submit(dialog);

  assert.equal(bodies.length, 2);
  assert.ok(bodies[0].request_id, "the create carries a request id");
  assert.equal(bodies[1].request_id, bodies[0].request_id, "a retry of the same action reuses the key");
  assert.equal(page.tasks.filter((task) => task.title === "Same title").length, 0, "the reload owns the list");

  // A different payload is a different action: it gets its own key.
  page.dialog.openCreate(page.tasks);
  byLabel(page.dialog.el, "Title").value = "Another title";
  await submit(page.dialog.el);
  assert.equal(bodies.length, 3);
  assert.notEqual(bodies[2].request_id, bodies[0].request_id);
});
