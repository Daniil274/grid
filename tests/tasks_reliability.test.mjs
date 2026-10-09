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

  assert.deepEqual(patches, [{ id: "bd-1", patch: { notes: "first note\n\nsecond note" } }]);
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

  api.tasks = async () => { throw new Error("network down"); };
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
