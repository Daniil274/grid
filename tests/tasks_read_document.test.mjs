import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { installFakeDom } from "./fake_dom.mjs";
installFakeDom();
const { TasksPage } = await import("../web_chat/js/tasks/page.js");
const { api } = await import("../web_chat/js/net/api.js");
const nodes = () => Object.fromEntries(["status", "metrics", "board", "listWrap", "listRows", "search", "type", "priority", "statusFilter", "project", "viewBoard", "viewList", "newTask", "refresh"].map(key => [key, document.createElement("div")]));
const task = { id: "long", title: "Long document", status: "closed", state: "closed", priority: 1, type: "task", description: "# Heading\n\n" + "Long paragraph. ".repeat(1200), acceptance: "- **Complete**", notes: "Notes with `code`", close_reason: "Verified result", depends_on: ["parent", "unknown"], created_at: "2026-10-01", updated_at: "2026-10-09" };
const parent = { id: "parent", title: "Prerequisite", status: "open", state: "ready", depends_on: [] };
const button = (dialog, text) => dialog.querySelectorAll("button").find(node => node.textContent === text);
const field = (dialog, label) => ["input", "select", "textarea"].flatMap(tag => dialog.querySelectorAll(tag)).find(node => node.getAttribute("aria-label") === label);
const submit = dialog => dialog.querySelector("form").listeners.submit[0]({ preventDefault() {} });

test("read-first Markdown document has focus, full long text, close reason and readable dependency jumps", () => {
  const page = new TasksPage(nodes());
  page.dialog.openTask(task, [task, parent]);
  const dialog = page.dialog.el;
  assert.equal(dialog.querySelector("form").hidden, true);
  assert.equal(document.activeElement, dialog.find("taskDialog__title"));
  assert.equal(dialog.find("taskDialog__title").textContent, task.title);
  const prose = dialog.findAll("taskDialog__prose");
  assert.match(prose[0].innerHTML, /<h1>Heading<\/h1>/);
  assert.ok(prose[0].innerHTML.includes("Long paragraph. ".repeat(1199)));
  assert.match(prose[1].innerHTML, /<strong>Complete<\/strong>/);
  assert.match(prose[2].innerHTML, /<code>code<\/code>/);
  assert.match(prose[3].innerHTML, /Verified result/);
  assert.match(dialog.find("taskDialog__document").textContent, /unknown \(not in this view\)/);
  assert.match(dialog.find("taskDialog__metadata").textContent, /Created2026-10-01/);
  button(dialog, "Prerequisite").click();
  assert.equal(page.dialog.showing(), parent);
  assert.equal(document.activeElement, dialog.find("taskDialog__title"));
});

test("Edit owns dirty draft, Cancel confirms and restores text/dependencies without API mutations", async () => {
  const page = new TasksPage(nodes());
  page.tasks = [task, parent];
  let mutations = 0;
  api.taskDep = async () => { mutations++; };
  api.updateTask = async () => { mutations++; };
  page.dialog.openTask(task, page.tasks);
  const dialog = page.dialog.el;
  button(dialog, "Edit").click();
  assert.equal(dialog.querySelector("form").hidden, false);
  assert.equal(dialog.find("taskDialog__document").hidden, true);
  assert.equal(button(dialog, "Save").disabled, true);
  assert.match(dialog.querySelector("form").textContent, /nothing recorded is lost/);
  field(dialog, "Notes").value = "draft";
  dialog.querySelector("form").listeners.input[0]();
  assert.equal(button(dialog, "Save").disabled, false);
  assert.equal(dialog.find("taskDialog__dirty").textContent, "Unsaved changes");
  await button(dialog, "remove").listeners.click[0]();
  assert.equal(mutations, 0);
  globalThis.window = { confirm: () => false };
  button(dialog, "Cancel").click();
  assert.equal(dialog.querySelector("form").hidden, false);
  window.confirm = () => true;
  button(dialog, "Cancel").click();
  assert.equal(dialog.querySelector("form").hidden, true);
  button(dialog, "Edit").click();
  // The box is for the note being added; existing notes stay in the preview.
  assert.equal(field(dialog, "Notes").value, "");
  assert.equal(dialog.querySelector("form").find("taskDialog__deps").querySelectorAll("button").length, 2);
  assert.equal(mutations, 0);
});

test("Save returns to reading; read/trace submits cannot mutate; escaped Markdown stays safe", async () => {
  const page = new TasksPage(nodes());
  const current = { ...parent, description: "<script>alert(1)</script>", notes: "before" };
  page.tasks = [current];
  api.tasks = async () => ({ available: true, tasks: [current] });
  const patches = [];
  api.updateTask = async (_id, patch) => { patches.push(patch); Object.assign(current, patch); };
  page.dialog.openTask(current, page.tasks);
  const dialog = page.dialog.el;
  assert.ok(!dialog.find("taskDialog__prose").innerHTML.includes("<script>"));
  await submit(dialog);
  assert.equal(patches.length, 0);
  button(dialog, "Edit").click();
  field(dialog, "Notes").value = "after";
  await submit(dialog);
  assert.deepEqual(patches, [{ notes: "before\n\nafter" }]);
  assert.equal(dialog.querySelector("form").hidden, true);
  assert.match(dialog.findAll("taskDialog__prose")[1].innerHTML, /after/);
  page.dialog.openTask({ ...current, source: "trace" }, page.tasks);
  assert.equal(button(dialog, "Edit").hidden, true);
  button(dialog, "Edit").click();
  await submit(dialog);
  assert.equal(patches.length, 1);
});

// The fake DOM cannot see geometry, so the read-first layout contract is pinned
// on the stylesheet itself: one wide, single-scrolling document and no leftover
// counter/state-badge chrome.
test("stylesheet keeps a wide single-scroll document and drops counter shells", () => {
  const css = readFileSync(fileURLToPath(new URL("../web_chat/styles/tasks.css", import.meta.url)), "utf8");
  const rule = (selector) => {
    const match = css.match(new RegExp(`(^|\\})\\s*${selector.replace(/[.]/g, "\\.")}\\s*\\{([^}]*)\\}`, "m"));
    return match ? match[2] : "";
  };
  assert.match(rule(".taskDialog"), /width:\s*min\(960px,\s*calc\(100vw - 24px\)\)/);
  assert.match(rule(".taskDialog"), /overflow:\s*auto/);
  assert.match(rule(".taskDialog__form"), /overflow:\s*visible/);
  assert.match(rule(".taskDialog__document"), /overflow-wrap:\s*anywhere/);
  assert.match(rule(".taskDialog__prose"), /font-size:\s*15px/);
  for (const gone of [".tasksMetrics", ".tasksCard__state", ".taskDialog__metrics"]) {
    assert.equal(rule(gone), "", `${gone} rule should be gone`);
  }
});
