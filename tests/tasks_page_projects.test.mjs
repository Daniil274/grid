/**
 * The project view of the tracker page (web_chat/js/tasks/page.js): the Project
 * filter, the aggregated plan the projects API answers with, the chat chips that
 * link a task back to the chat whose turns planned it, the read-only state of a
 * trace-only task, and the /tasks?project=<id> URL. The API is stubbed.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { TasksPage, chatChip } = await import("../web_chat/js/tasks/page.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);

const buttonByText = (root, text) =>
  root.querySelectorAll("button").find((button) => button.textContent.trim() === text);

/** The page shell as web_chat/tasks.html holds it (with the project filter). */
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
    project: el("select"),
    viewBoard: el("button"),
    viewList: el("button"),
    newTask: el("button"),
    refresh: el("button"),
  };
}

const trackerReport = (tasks) => ({ available: true, reason: null, tasks, total: tasks.length });

const TRACKER_TASK = {
  id: "bd-1", title: "Tracked", state: "open", status: "open", priority: 1, type: "task",
  assignee: null, description: "", acceptance: "", notes: "", depends_on: [], waits_for: [],
  updated_at: "2026-10-01T10:00:00",
};

const projectPlan = () => ({
  tasks: [
    { id: "bd-1", title: "Alpha", state: "ready", status: "open", priority: 0, type: "feature", source: "bd", chat_ids: ["c1"] },
    { id: "trace-2", title: "From a plan", state: "waiting", status: null, type: "task", source: "trace", chat_ids: ["c2"] },
  ],
  chats: { c1: { title: "Chat one" }, c2: "Chat two" },
  counts: { ready: 1, waiting: 1 },
  total: 2,
});

test("chatChip links back to the chat that planned the task", () => {
  const chip = chatChip("c1", { title: "Chat one" });
  assert.equal(chip.getAttribute("href"), "/?conversation=c1");
  assert.match(chip.textContent, /Chat one/);
});

test("choosing a project loads its aggregated plan and writes the URL", async () => {
  const urls = [];
  globalThis.history = { replaceState: (_state, _title, url) => urls.push(url) };
  api.tasks = async () => { throw new Error("the tracker must not be asked in project mode"); };
  api.projectPlan = async (id) => {
    assert.equal(id, "p1");
    return projectPlan();
  };

  const page = new TasksPage(nodes());
  page.nodes.project.value = "p1";
  await page.load();

  assert.equal(page.project, "p1");
  assert.equal(page.tasks.length, 2);
  assert.equal(page.nodes.board.findAll("tasksCard").length, 2);
  // The source reader and a chip back to each chat.
  const chips = page.nodes.board.findAll("tasksChip");
  assert.deepEqual(chips.map((chip) => chip.getAttribute("href")), ["/?conversation=c1", "/?conversation=c2"]);
  assert.deepEqual(urls, ["/tasks?project=p1"]);
});

test("clearing the project returns to the tracker and resets the URL", async () => {
  const urls = [];
  globalThis.history = { replaceState: (_state, _title, url) => urls.push(url) };
  api.projectPlan = async () => projectPlan();
  api.tasks = async () => trackerReport([TRACKER_TASK]);

  const page = new TasksPage(nodes());
  page.nodes.project.value = "p1";
  await page.load();
  page.nodes.project.value = "";
  await page.load();

  assert.equal(page.project, null);
  assert.deepEqual(page.chats, {});
  assert.equal(page.tasks.length, 1);
  assert.deepEqual(urls, ["/tasks?project=p1", "/tasks"]);
});

test("a trace-only task is read-only in the dialog; a tracker task is editable", async () => {
  api.projectPlan = async () => projectPlan();
  const page = new TasksPage(nodes());
  page.nodes.project.value = "p1";
  await page.load();

  page.dialog.openTask(page.tasks.find((task) => task.source === "trace"), page.tasks);
  const save = buttonByText(page.dialog.el, "Save");
  assert.equal(save.hidden, true, "a trace-only task cannot be saved from here");
  const chip = page.dialog.el.querySelectorAll("a").find((node) => node.classList.contains("tasksChip"));
  assert.equal(chip.getAttribute("href"), "/?conversation=c2");

  page.dialog.openTask(page.tasks.find((task) => task.source === "bd"), page.tasks);
  assert.equal(save.hidden, false, "a task the tracker holds is editable");
});

test("loadProjects fills the filter and honours ?project=<id>", async () => {
  globalThis.location = { search: "?project=p2" };
  api.projects = async () => ({ projects: [{ id: "p1", title: "Grid" }, { id: "p2", title: "Backend" }] });
  try {
    const page = new TasksPage(nodes());
    await page.loadProjects();
    assert.equal(page.nodes.project.value, "p2");
  } finally {
    delete globalThis.location;
  }
});
