/**
 * The tracker page (web_chat/js/tasks/page.js): its helpers, its board and
 * list views over the tasks the API answers with, and its task dialog - where
 * a task is created, edited, closed and given dependencies, every change
 * going through the tracker API (web_chat/tasks_api.py). The API is stubbed
 * here: this is the page's own behaviour, not bd's.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { TasksPage, sortTasks, matchesFilters, BOARD_ORDER } = await import("../web_chat/js/tasks/page.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);

/** Every control of the dialog/board, by its aria-label (the fake DOM has no attribute selectors). */
const byLabel = (root, label) =>
  ["input", "select", "textarea"]
    .flatMap((tag) => root.querySelectorAll(tag))
    .find((node) => node.getAttribute("aria-label") === label);

/** A button of *root* whose text is exactly *text*. */
const buttonByText = (root, text) =>
  root.querySelectorAll("button").find((button) => button.textContent.trim() === text);

/** The shell's nodes, as web_chat/tasks.html holds them. */
function nodes() {
  return {
    status: el("div"),
    metrics: el("div"),
    board: el("div"),
    listWrap: el("div"),
    listRows: el("tbody"),
    search: el("input"),
    type: el("select"),
    priority: el("select"),
    statusFilter: el("select"),
    viewBoard: el("button"),
    viewList: el("button"),
    newTask: el("button"),
    refresh: el("button"),
  };
}

const TASKS = () => [
  {
    id: "bd-1", title: "Alpha", state: "ready", status: "open", priority: 0, type: "feature",
    assignee: null, description: "", acceptance: "", notes: "",
    depends_on: [], waits_for: [], updated_at: "2026-10-01T10:00:00",
  },
  {
    id: "bd-2", title: "Beta", state: "in_progress", status: "in_progress", priority: 2, type: "bug",
    assignee: null, description: "", acceptance: "", notes: "",
    depends_on: [], waits_for: [], updated_at: "2026-10-02T10:00:00",
  },
];

const report = (tasks, extra = {}) => ({ available: true, reason: null, tasks, total: tasks.length, ...extra });

test("sortTasks puts P0 first, a task without one last, then the freshest change", () => {
  const done = sortTasks([
    { id: "a", priority: 2, updated_at: "2026-10-02T00:00:00" },
    { id: "b", priority: 0, updated_at: "2026-10-01T00:00:00" },
    { id: "c", priority: null, updated_at: "2026-10-03T00:00:00" },
    { id: "d", priority: 2, updated_at: "2026-10-09T00:00:00" },
  ]);
  assert.deepEqual(done.map((task) => task.id), ["b", "d", "a", "c"]);
});

test("matchesFilters reads text, type and priority", () => {
  const task = { title: "Fix the build", id: "bd-1", type: "bug", priority: 1, notes: "red", description: "" };
  assert.equal(matchesFilters(task), true);
  assert.equal(matchesFilters(task, { search: "build" }), true);
  assert.equal(matchesFilters(task, { search: "red" }), true);
  assert.equal(matchesFilters(task, { search: "green" }), false);
  assert.equal(matchesFilters(task, { type: "bug" }), true);
  assert.equal(matchesFilters(task, { type: "feature" }), false);
  assert.equal(matchesFilters(task, { priority: "1" }), true);
  assert.equal(matchesFilters(task, { priority: "0" }), false);
  // "0" is a real priority: an empty filter is what shows everything.
  assert.equal(matchesFilters({ ...task, priority: 0 }, { priority: "0" }), true);
});

test("load renders one board/list without duplicated metric panels", async () => {
  api.tasks = async () => report(TASKS());
  const page = new TasksPage(nodes());
  await page.load();

  assert.equal(page.tasks.length, 2);
  assert.equal(page.nodes.board.findAll("tasksColumn").length, BOARD_ORDER.length);
  assert.equal(page.nodes.board.findAll("tasksCard").length, 2);
  assert.equal(page.nodes.metrics.findAll("tasksMetric").length, 0);
  assert.equal(page.nodes.metrics.hidden, true);
  assert.equal(page.nodes.listRows.querySelectorAll("tr").length, 2);
  assert.match(page.nodes.status.textContent, /2 of 2 tasks shown/);
});

test("a filter change re-renders without another request", async () => {
  let calls = 0;
  api.tasks = async () => {
    calls += 1;
    return report(TASKS());
  };
  const page = new TasksPage(nodes());
  await page.load();
  assert.equal(calls, 1);

  page.nodes.search.addEventListener("input", () => {});
  page.nodes.search.value = "Beta";
  page.render();
  assert.equal(page.nodes.board.findAll("tasksCard").length, 1);
  assert.match(page.nodes.board.findAll("tasksCard")[0].textContent, /Beta/);
  assert.equal(calls, 1);
});

test("a tracker that is unavailable offers to start one, then reloads", async () => {
  let inits = 0;
  let loads = 0;
  api.tasks = async () => {
    loads += 1;
    return loads === 1
      ? { available: false, reason: "The tracker is unavailable: bd is not installed", tasks: [], total: 0 }
      : report(TASKS());
  };
  api.initTracker = async () => {
    inits += 1;
    return { ok: true };
  };

  const page = new TasksPage(nodes());
  await page.load();
  assert.equal(page.available, false);
  assert.match(page.nodes.status.textContent, /bd is not installed/);
  const init = buttonByText(page.nodes.status, "Initialize tracker");
  assert.ok(init, "the page offers a way to start a tracker");

  await init.listeners.click[0]();
  assert.equal(inits, 1);
  assert.equal(page.available, true);
  assert.equal(page.nodes.board.findAll("tasksCard").length, 2);
});

test("the dialog creates a task with the fields and the dependencies picked", async () => {
  const created = [];
  api.tasks = async () => report(TASKS());
  api.createTask = async (task) => {
    created.push(task);
    return { id: "bd-new", ...task };
  };

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openCreate(page.tasks);

  byLabel(page.dialog.el, "Title").value = "New one";
  byLabel(page.dialog.el, "Type").value = "bug";
  byLabel(page.dialog.el, "Priority").value = "1";
  const [form] = page.dialog.el.querySelectorAll("form");
  await form.listeners.submit[0]({ preventDefault() {} });

  assert.equal(created.length, 1);
  assert.equal(created[0].title, "New one");
  assert.equal(created[0].type, "bug");
  assert.equal(created[0].priority, 1);
  assert.deepEqual(created[0].depends_on, []);
});

test("saving an edited task patches only the fields that changed", async () => {
  const patches = [];
  api.tasks = async () => report(TASKS());
  api.updateTask = async (id, patch) => {
    patches.push({ id, patch });
    return { ok: true };
  };

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[0], page.tasks);
  buttonByText(page.dialog.el, "Edit").click();
  byLabel(page.dialog.el, "Title").value = "Alpha v2";
  const [form] = page.dialog.el.querySelectorAll("form");
  await form.listeners.submit[0]({ preventDefault() {} });

  assert.deepEqual(patches, [{ id: "bd-1", patch: { title: "Alpha v2" } }]);
});

test("closing a task records the reason through the API", async () => {
  const closed = [];
  api.tasks = async () => report(TASKS());
  api.closeTask = async (id, reason) => {
    closed.push({ id, reason });
    return { ok: true };
  };

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[0], page.tasks);
  byLabel(page.dialog.el, "Close reason").value = "table written";
  await buttonByText(page.dialog.el, "Close task").listeners.click[0]();

  assert.deepEqual(closed, [{ id: "bd-1", reason: "table written" }]);
});

test("dependency changes are drafts until Save calls the existing API", async () => {
  const deps = [];
  api.tasks = async () => report(TASKS());
  api.taskDep = async (id, action, parentId) => {
    deps.push({ id, action, parentId });
    return { ok: true };
  };

  const page = new TasksPage(nodes());
  await page.load();
  page.dialog.openTask(page.tasks[0], page.tasks);
  buttonByText(page.dialog.el, "Edit").click();
  byLabel(page.dialog.el, "Depends on").value = "bd-2";
  await buttonByText(page.dialog.el, "Depends on").listeners.click[0]();

  assert.deepEqual(deps, []);
  await page.dialog.el.querySelector("form").listeners.submit[0]({ preventDefault() {} });
  assert.deepEqual(deps, [{ id: "bd-1", action: "add", parentId: "bd-2" }]);
});
