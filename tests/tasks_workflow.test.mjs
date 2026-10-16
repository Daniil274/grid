/**
 * The tracker's working cycle (web_chat/js/tasks/page.js): the URL state
 * (view/status/q/task) that a link opens a task with and Back restores, the
 * filter presets and active-filter chips, the wide search over acceptance and
 * close reason, the card's quick actions, "take on myself", the explicit
 * Reopen and the Undo that a close offers. The API is stubbed throughout.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { TasksPage, matchesFilters, readUrlState, PRESETS } = await import("../web_chat/js/tasks/page.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);
const tick = () => new Promise((resolve) => setImmediate(resolve));

const buttonByText = (root, text) =>
  root.querySelectorAll("button").find((button) => button.textContent.trim() === text);

function nodes() {
  return {
    status: el("div"),
    presets: el("div"),
    activeFilters: el("div"),
    undo: el("div"),
    metrics: el("div"),
    board: el("div"),
    listWrap: el("div"),
    listRows: el("tbody"),
    search: el("input"),
    type: el("select"),
    priority: el("select"),
    statusFilter: el("select"),
    project: el("select"),
    viewBoard: el("button"),
    viewList: el("button"),
    newTask: el("button"),
    refresh: el("button"),
  };
}

const TASKS = () => [
  {
    id: "bd-1", title: "Alpha", state: "ready", status: "open", priority: 0, type: "feature",
    assignee: null, description: "", acceptance: "the table is written", notes: "",
    close_reason: null, depends_on: [], waits_for: [], updated_at: "2026-10-01T10:00:00",
  },
  {
    id: "bd-2", title: "Beta", state: "in_progress", status: "in_progress", priority: 2, type: "bug",
    assignee: null, description: "", acceptance: "", notes: "",
    close_reason: null, depends_on: [], waits_for: [], updated_at: "2026-10-02T10:00:00",
  },
];

const report = (tasks) => ({ available: true, reason: null, tasks, total: tasks.length });

// -- the search and the URL shape ------------------------------------------------

test("the search finds a task by its acceptance and by why it was closed", () => {
  const task = { title: "Plain", id: "bd-1", notes: "", description: "", acceptance: "Verified on staging", close_reason: "Superseded by bd-9" };
  assert.equal(matchesFilters(task, { search: "staging" }), true);
  assert.equal(matchesFilters(task, { search: "superseded" }), true);
  assert.equal(matchesFilters(task, { search: "nowhere" }), false);
});

test("readUrlState reads view/status/type/priority/q/task, defaulting to the board", () => {
  assert.deepEqual(readUrlState(""), { project: "", status: "", type: "", priority: "", q: "", view: "board", task: "", conversation: "" });
  assert.deepEqual(readUrlState("?view=list&status=blocked&type=bug&priority=0&q=fix&task=bd-3"), {
    project: "", status: "blocked", type: "bug", priority: "0", q: "fix", view: "list", task: "bd-3", conversation: "",
  });
});

test("a ?task=<id> link opens that task once the list is loaded", async () => {
  globalThis.location = { pathname: "/tasks", search: "?task=bd-2" };
  api.tasks = async () => report(TASKS());
  try {
    const page = new TasksPage(nodes());
    await page.load();
    await tick();
    assert.equal(page.dialog.showing()?.id, "bd-2");
  } finally {
    delete globalThis.location;
  }
});

test("a filter change is a history entry and Back brings the filters back", async () => {
  const handlers = {};
  const write = (url) => {
    globalThis.location = { pathname: "/tasks", search: url.includes("?") ? url.slice(url.indexOf("?")) : "" };
  };
  globalThis.window = { addEventListener: (type, fn) => { handlers[type] = fn; } };
  globalThis.history = {
    pushState: (_state, _title, url) => write(url),
    replaceState: (_state, _title, url) => write(url),
  };
  globalThis.location = { pathname: "/tasks", search: "" };
  api.tasks = async () => report(TASKS());
  try {
    const page = new TasksPage(nodes());
    await page.load();

    page.nodes.statusFilter.value = "blocked";
    page.nodes.statusFilter.listeners.change[0]();
    await tick();
    assert.equal(globalThis.location.search, "?status=blocked");

    // A Back step arrives as the previous entry and a popstate event.
    write("/tasks");
    handlers.popstate();
    await tick();
    assert.equal(page.nodes.statusFilter.value, "");
  } finally {
    delete globalThis.window;
    delete globalThis.history;
    delete globalThis.location;
  }
});

test("the P0 preset writes ?priority=0 and Back clears it again", async () => {
  const handlers = {};
  const write = (url) => {
    globalThis.location = { pathname: "/tasks", search: url.includes("?") ? url.slice(url.indexOf("?")) : "" };
  };
  globalThis.window = { addEventListener: (type, fn) => { handlers[type] = fn; } };
  globalThis.history = {
    pushState: (_state, _title, url) => write(url),
    replaceState: (_state, _title, url) => write(url),
  };
  globalThis.location = { pathname: "/tasks", search: "" };
  api.tasks = async () => report(TASKS());
  try {
    const page = new TasksPage(nodes());
    await page.load();

    const p0 = buttonByText(page.nodes.presets, "P0");
    assert.ok(p0, "the P0 preset is rendered");
    p0.listeners.click[0]();
    await tick();
    // A priority-only preset is now a real history entry, so Back returns it.
    assert.equal(globalThis.location.search, "?priority=0");
    assert.equal(page.nodes.priority.value, "0");

    write("/tasks");
    handlers.popstate();
    await tick();
    assert.equal(page.nodes.priority.value, "");
  } finally {
    delete globalThis.window;
    delete globalThis.history;
    delete globalThis.location;
  }
});

test("the view control writes ?view=list and the search writes ?q=", async () => {
  const urls = [];
  globalThis.history = { replaceState: (_s, _t, url) => urls.push(url), pushState: (_s, _t, url) => urls.push(url) };
  globalThis.location = { pathname: "/tasks", search: "" };
  api.tasks = async () => report(TASKS());
  try {
    const page = new TasksPage(nodes());
    await page.load();
    page.setView("list", { push: true });
    page.nodes.search.value = "fix";
    page.nodes.search.listeners.input[0]();
    assert.equal(urls.at(-1), "/tasks?q=fix&view=list");
  } finally {
    delete globalThis.history;
    delete globalThis.location;
  }
});

// -- presets and active-filter chips ---------------------------------------------

test("presets set the controls and active filters show as removable chips", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();

  assert.equal(page.nodes.presets.querySelectorAll("button").length, PRESETS.length);
  const p0 = buttonByText(page.nodes.presets, "P0");
  p0.listeners.click[0]();
  assert.equal(page.nodes.priority.value, "0");
  assert.deepEqual(page.shown().map((task) => task.id), ["bd-1"]);

  const blocked = buttonByText(page.nodes.presets, "Blocked");
  blocked.listeners.click[0]();
  await tick();
  assert.equal(page.nodes.statusFilter.value, "blocked");

  const chip = page.nodes.activeFilters.findAll("tasksFilterChip").find((node) => node.dataset.kind === "status");
  assert.ok(chip, "the status filter shows as a chip");
  chip.listeners.click[0]();
  await tick();
  assert.equal(page.nodes.statusFilter.value, "");
});

// -- quick actions, take on myself, reopen and undo ------------------------------

test("a card's quick actions take, close and reopen without opening the dialog", async () => {
  const closed = [];
  const updates = [];
  api.tasks = async () => report(TASKS());
  api.closeTask = async (id, reason) => { closed.push({ id, reason }); return { ok: true }; };
  api.updateTask = async (id, patch) => { updates.push({ id, patch }); return { ok: true }; };

  const page = new TasksPage(nodes());
  await page.load();

  const card = page.nodes.board.findAll("tasksCardItem")[0];
  const byAction = (action) => card.querySelectorAll("button").find((node) => node.dataset.action === action);

  // Take on myself.
  page._me = "alice";
  await byAction("take").listeners.click[0]();
  assert.deepEqual(updates, [{ id: "bd-1", patch: { assignee: "alice" } }]);

  // Close, then undo it through the bar.
  await byAction("close").listeners.click[0]();
  assert.deepEqual(closed, [{ id: "bd-1", reason: "Completed" }]);
  const undo = buttonByText(page.nodes.undo, "Undo");
  assert.ok(undo, "closing offers an undo");
  await undo.listeners.click[0]();
  assert.deepEqual(updates.at(-1), { id: "bd-1", patch: { status: "open" } });
});

test("a closed card offers Reopen, and Reopen is a real API write", async () => {
  const updates = [];
  const closedTask = { ...TASKS()[1], status: "closed", state: "closed", close_reason: "Done" };
  api.tasks = async () => report([closedTask]);
  api.updateTask = async (id, patch) => { updates.push({ id, patch }); return { ok: true }; };

  const page = new TasksPage(nodes());
  await page.load();

  const card = page.nodes.board.findAll("tasksCardItem")[0];
  const reopen = card.querySelectorAll("button").find((node) => node.dataset.action === "reopen");
  assert.ok(reopen, "a closed card offers Reopen");
  await reopen.listeners.click[0]();
  assert.deepEqual(updates, [{ id: "bd-2", patch: { status: "open" } }]);
});

test("the dialog shows an explicit Reopen for a closed task only", async () => {
  const updates = [];
  api.tasks = async () => report(TASKS());
  api.updateTask = async (id, patch) => { updates.push({ id, patch }); return { ok: true }; };

  const page = new TasksPage(nodes());
  await page.load();

  const closed = { ...TASKS()[0], status: "closed", state: "closed" };
  page.dialog.openTask(closed, [closed]);
  const reopen = buttonByText(page.dialog.el, "Reopen");
  assert.equal(reopen.hidden, false);
  await reopen.listeners.click[0]();
  assert.deepEqual(updates, [{ id: "bd-1", patch: { status: "open" } }]);

  page.dialog.openTask(page.tasks[0], page.tasks);
  assert.equal(buttonByText(page.dialog.el, "Reopen").hidden, true, "an open task has nothing to reopen");
});

test("a trace-only task exposes no quick actions", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  page.tasks = [{ ...TASKS()[0], source: "trace" }];
  page.render();
  const card = page.nodes.board.findAll("tasksCardItem")[0];
  assert.equal(card.querySelectorAll("button").filter((node) => node.dataset.action).length, 0);
});
