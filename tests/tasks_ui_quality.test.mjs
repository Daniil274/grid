import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { installFakeDom } from "./fake_dom.mjs";
installFakeDom();
const { TasksPage, matchesFilters } = await import("../web_chat/js/tasks/page.js");
const { createTrackerTheme, TRACKER_THEMES } = await import("../web_chat/js/tasks/theme.js");
const { api } = await import("../web_chat/js/net/api.js");
const el = (tag = "div") => document.createElement(tag);
const nodes = () => Object.fromEntries([
  "status", "metrics", "board", "listWrap", "listRows", "search", "type", "priority", "statusFilter", "project", "viewBoard", "viewList", "newTask", "refresh",
].map(key => [key, el()]));
const task = (id, status = "open") => ({ id, title: id, status, state: status === "open" ? "ready" : status, type: "task", priority: 2, depends_on: [] });
const report = tasks => ({ available: true, tasks });

test("all explicit tracker themes persist and restore without changing chat preference", () => {
  const values = new Map([["grid.theme", "dark"]]);
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) };
  const root = el();
  const select = el("select");
  createTrackerTheme(select, root, storage);
  assert.equal(select.value, "light");
  for (const theme of TRACKER_THEMES) {
    select.value = theme;
    select.listeners.change[0]();
    assert.equal(root.dataset.trackerTheme, theme);
    const restored = el("select");
    createTrackerTheme(restored, root, storage);
    assert.equal(restored.value, theme);
  }
  assert.equal(values.get("grid.theme"), "dark");
});

test("invalid and inaccessible storage degrade to a usable light theme", () => {
  for (const storage of [{ getItem: () => "bad", setItem() {} }, { getItem() { throw Error("denied"); }, setItem() { throw Error("denied"); } }]) {
    const select = el("select"); const root = el();
    createTrackerTheme(select, root, storage);
    assert.equal(root.dataset.trackerTheme, "light");
    select.value = "vivid";
    select.listeners.change[0]();
    assert.equal(root.dataset.trackerTheme, "vivid");
  }
});

test("actual page shell wires the status select and explicit theme choices", async () => {
  const source = await readFile(new URL("../web_chat/js/tasks/page.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../web_chat/tasks.html", import.meta.url), "utf8");
  assert.match(source, /statusFilter: \$\("status-filter"\)/);
  for (const theme of TRACKER_THEMES) assert.ok(html.includes(`value="${theme}"`));
});

test("status reload passes the bd status and project mode filters locally", async () => {
  const page = new TasksPage(nodes());
  let received;
  api.tasks = async status => { received = status; return report([task("a", "blocked")]); };
  page.nodes.statusFilter.value = "blocked";
  await page.nodes.statusFilter.listeners.change[0]();
  // The listener deliberately starts a background load.
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(received, "blocked");
  assert.equal(page.shown().length, 1);
  api.projectPlan = async () => report([task("b"), task("c", "closed"), { ...task("d"), status: null, source: "trace" }]);
  page.nodes.project.value = "p";
  page.nodes.statusFilter.value = "closed";
  await page.load();
  assert.deepEqual(page.shown().map(t => t.id), ["c"]);
  assert.equal(matchesFilters({ ...task("x"), assignee: "Alex" }, { search: "alex" }), true);
});

test("late responses cannot replace a newer filter result", async () => {
  const page = new TasksPage(nodes());
  let finish;
  api.tasks = () => new Promise(resolve => { finish = resolve; });
  const first = page.load();
  assert.equal(page.nodes.board.getAttribute("aria-busy"), "true");
  api.tasks = async () => report([task("new")]);
  await page.load();
  finish(report([task("old")]));
  await first;
  assert.equal(page.tasks[0].id, "new");
  assert.equal(page.nodes.board.getAttribute("aria-busy"), "false");
});

test("failed refresh clears stale board/table/counts, then recovers", async () => {
  const page = new TasksPage(nodes());
  api.tasks = async () => report([task("a")]);
  await page.load();
  api.tasks = async () => { throw Error("Network unavailable — Refresh to retry"); };
  await page.load();
  assert.equal(page.nodes.status.dataset.error, "true");
  assert.equal(page.nodes.board.textContent, "");
  assert.equal(page.nodes.listRows.textContent, "");
  assert.equal(page.nodes.metrics.textContent, "");
  api.tasks = async () => report([]);
  await page.load();
  assert.equal(page.nodes.board.findAll("tasksColumnEmpty").length, 5);
  assert.match(page.nodes.status.textContent, /No tasks/);
});

test("view controls expose pressed state and trace tasks disable mutations", async () => {
  const page = new TasksPage(nodes());
  api.tasks = async () => report([task("a")]);
  await page.load();
  page.setView("list");
  assert.equal(page.nodes.viewList.getAttribute("aria-pressed"), "true");
  assert.equal(page.nodes.board.hidden, true);
  assert.equal(page.nodes.listWrap.hidden, false);
  page.dialog.openTask({ ...task("trace"), source: "trace" }, page.tasks);
  const buttons = page.dialog.el.querySelectorAll("button");
  assert.equal(buttons.find(b => b.textContent === "Close task").hidden, true);
  assert.equal(buttons.find(b => b.textContent === "Depends on").disabled, true);
  assert.equal(page.dialog.el.find("taskDialog__title").getAttribute("id"), "task-dialog-title");
  page.dialog.openTask(page.tasks[0], page.tasks);
  assert.equal(buttons.find(b => b.textContent === "Depends on").disabled, false);
});

test("closed task edits preserve status; reopening requires an explicit status choice", async () => {
  const page = new TasksPage(nodes());
  const closed = { ...task("done", "closed"), notes: "original" };
  const updates = [];
  const originalTasks = api.tasks;
  const originalUpdate = api.updateTask;
  api.tasks = async () => report([closed]);
  api.updateTask = async (id, patch) => { updates.push({ id, patch }); };
  try {
    const dialog = page.dialog.el;
    const status = dialog.querySelectorAll("select").find(input => input.getAttribute("aria-label") === "Status");
    const edit = () => dialog.querySelectorAll("button").find(button => button.textContent === "Edit").click();
    const notes = dialog.querySelectorAll("textarea").find(input => input.getAttribute("aria-label") === "Notes");
    const submit = () => dialog.querySelector("form").listeners.submit[0]({ preventDefault() {} });
    page.dialog.openTask(closed, [closed]);
    assert.equal(status.value, "closed");
    assert.ok(status.querySelectorAll("option").some(option => option.getAttribute("value") === "closed"));
    edit();
    notes.value = "changed";
    await submit();
    // A note is added to what the task already held, never swapped for it.
    assert.deepEqual(updates, [{ id: "done", patch: { notes: "original\n\nchanged" } }]);

    page.dialog.openTask(closed, [closed]);
    edit();
    await submit();
    assert.equal(updates.length, 1, "unchanged closed task sends no PATCH");

    page.dialog.openTask(closed, [closed]);
    edit();
    status.value = "open";
    await submit();
    assert.deepEqual(updates[1], { id: "done", patch: { status: "open" } });

    page.dialog.openCreate([closed]);
    assert.equal(status.value, "open");
    assert.ok(!status.querySelectorAll("option").some(option => option.getAttribute("value") === "closed"));
    page.dialog.openTask(task("active"), [closed]);
    assert.ok(!status.querySelectorAll("option").some(option => option.getAttribute("value") === "closed"));
  } finally {
    api.tasks = originalTasks;
    api.updateTask = originalUpdate;
  }
});

test("late failures do not hide a newer success; unavailable disables create until recovery", async () => {
  const page = new TasksPage(nodes());
  let reject;
  api.tasks = () => new Promise((_resolve, fail) => { reject = fail; });
  const old = page.load();
  api.tasks = async () => report([task("fresh")]);
  await page.load();
  reject(Error("old error"));
  await old;
  assert.equal(page.available, true);
  assert.equal(page.tasks[0].id, "fresh");
  api.tasks = async () => ({ available: false, reason: "bd unavailable", tasks: [] });
  await page.load();
  assert.equal(page.nodes.board.textContent, "");
  assert.equal(page.nodes.listRows.textContent, "");
  assert.equal(page.nodes.newTask.disabled, true);
  assert.match(page.nodes.status.textContent, /Initialize tracker/);
  api.tasks = async () => report([]);
  await page.load();
  assert.equal(page.nodes.newTask.disabled, false);
});
