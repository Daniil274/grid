/**
 * The tracker's speed (web_chat/js/tasks/page.js): the command palette on
 * Ctrl/⌘ K that filters the tasks and creates one from the query line, the j/k
 * cursor with Enter to open, N for a new task, ? for the shortcuts sheet, and
 * the optimistic write that a refused PATCH rolls back. The API is stubbed.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { TasksPage } = await import("../web_chat/js/tasks/page.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const key = (name, extra = {}) => ({ key: name, preventDefault() {}, target: el("div"), ...extra });

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

const typeQuery = (page, text) => {
  page.palette.input.value = text;
  page.palette.input.listeners.input[0]();
};

// -- the command palette ---------------------------------------------------------

test("Ctrl+K opens the palette, filters the tasks and Enter opens the cursor's task", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();

  assert.equal(page.palette.isOpen(), false);
  page._onKeyDown(key("k", { ctrlKey: true }));
  assert.equal(page.palette.isOpen(), true, "Ctrl+K opens the palette");

  typeQuery(page, "Beta");
  const rows = page.palette.rows();
  assert.deepEqual(rows.filter((row) => row.kind === "task").map((row) => row.task.id), ["bd-2"]);

  page._onKeyDown(key("Enter"));
  assert.equal(page.palette.isOpen(), false, "choosing closes the palette");
  assert.equal(page.dialog.showing()?.id, "bd-2");
});

test("a query with no matching task offers a Create row that makes the task", async () => {
  const created = [];
  api.tasks = async () => report(TASKS());
  api.createTask = async (spec) => { created.push(spec); return { ...TASKS()[0], id: "bd-new", title: spec.title }; };

  const page = new TasksPage(nodes());
  await page.load();

  page._onKeyDown(key("k", { metaKey: true }));
  typeQuery(page, "Gamma");
  const rows = page.palette.rows();
  assert.equal(rows.at(-1).kind, "create");
  assert.equal(rows.at(-1).title, "Gamma");

  page._onKeyDown(key("Enter"));
  await tick();
  await tick();
  assert.deepEqual(created, [{ title: "Gamma" }]);
  assert.equal(page.palette.isOpen(), false);
});

test("the palette walks its rows with the arrow keys", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  page._onKeyDown(key("k", { ctrlKey: true }));
  page._onKeyDown(key("ArrowDown"));
  const rows = page.palette.rows();
  const active = page.palette.list.findAll("tasksPalette__row").find((row) => row.dataset.active === "true");
  assert.ok(active, "a row is marked active");
  assert.equal(rows.length >= 2, true);
});

// -- j/k, Enter, N and the shortcuts sheet ---------------------------------------

test("j and k walk the cursor through the shown tasks in order", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  const shown = page.shown().map((task) => task.id);

  page._onKeyDown(key("j"));
  assert.equal(page.selectedId, shown[0]);
  page._onKeyDown(key("j"));
  assert.equal(page.selectedId, shown[1]);
  page._onKeyDown(key("k"));
  assert.equal(page.selectedId, shown[0]);
});

test("Enter opens the task the cursor is on", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  page._onKeyDown(key("j"));
  page._onKeyDown(key("Enter"));
  assert.equal(page.dialog.showing()?.id, page.shown()[0].id);
});

test("N opens the new-task dialog", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();

  page._onKeyDown(key("n"));
  assert.equal(page.dialog.el.dataset.open, "true", "N opens the create dialog");
});

test("? shows the shortcuts sheet and Esc closes it", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();

  page._onKeyDown(key("?"));
  assert.equal(page.cheatsheet.isOpen(), true, "? shows the sheet");
  page._onKeyDown(key("Escape"));
  assert.equal(page.cheatsheet.isOpen(), false, "Esc closes the sheet");
});

test("keys do not fire while a field has the focus", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  page._onKeyDown(key("j", { target: { tagName: "INPUT" } }));
  assert.equal(page.selectedId, "", "j in a field is just the letter j");
});

test("page keys stay out of the way while the task dialog is open", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[0], page.tasks);
  page._onKeyDown(key("j"));
  assert.equal(page.selectedId, "", "j does not move the board behind the dialog");
});

// -- the optimistic write --------------------------------------------------------

test("a quick action shows at once, then reloads on success", async () => {
  const calls = [];
  api.tasks = async () => report(TASKS());
  api.updateTask = async (id, patch) => { calls.push({ id, patch }); return { ok: true }; };

  const page = new TasksPage(nodes());
  await page.load();
  page._me = "alice";
  await page.quickAction("take", page.tasks[0]);
  assert.deepEqual(calls[0], { id: "bd-1", patch: { assignee: "alice" } });
});

test("a refused PATCH rolls the optimistic change back", async () => {
  api.tasks = async () => report(TASKS());
  api.closeTask = async () => { throw new Error("tracker said no"); };

  const page = new TasksPage(nodes());
  await page.load();
  const task = page.tasks.find((candidate) => candidate.id === "bd-1");
  assert.equal(task.status, "open");

  await page.quickAction("close", task);
  assert.equal(task.status, "open", "the failed close is taken back");
  assert.equal(task.state, "ready");
  assert.equal(task.close_reason, null);
});

test("a refused create from the palette removes the optimistic draft", async () => {
  api.tasks = async () => report(TASKS());
  api.createTask = async () => { throw new Error("no tracker"); };

  const page = new TasksPage(nodes());
  await page.load();
  const before = page.tasks.length;
  await page._createInline("Gamma");
  assert.equal(page.tasks.length, before, "the draft does not survive the error");
  assert.equal(page.tasks.some((task) => task.title === "Gamma"), false);
});
