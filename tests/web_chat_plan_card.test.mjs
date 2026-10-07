/**
 * The plan card's task dialog (web_chat/js/ui/plan-card.js): the rows open a
 * task's own details - the Task tab with what the tracker recorded, the Result
 * tab with what the run reported - and the dialog follows the plan while it
 * updates. This is the beads plan of the chat, not the user's account plan
 * (that drawer lives in tests/web_chat_plan.test.mjs).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { createPlanCard } = await import("../web_chat/js/ui/plan-card.js");

const planStep = (tasks) => ({
  kind: "plan",
  plan: {
    tasks,
    total: tasks.length,
    counts: tasks.reduce((counts, task) => ({ ...counts, [task.state]: (counts[task.state] ?? 0) + 1 }), {}),
  },
});

const TASKS = () => [
  {
    id: "bd-1", key: "research", title: "Research providers", state: "closed", depends_on: [],
    description: "List **providers** and their prices.", acceptance: "A table of providers.",
    notes: "line one\nline two", close_reason: "done, with the table in the answer",
    report: {
      status: "completed", final: "Found 3 providers.", seconds: 92, model_calls: 7,
      tokens: 12345, cost_usd: 0.0432, model_key: "claude-x", tier: "high",
    },
  },
  {
    id: "bd-2", key: "compare", title: "Compare providers", state: "in_progress", depends_on: ["bd-1"],
    description: "", acceptance: "", notes: "", close_reason: null, report: null,
    waits_for: ["research"],
  },
  {
    id: "bd-3", key: "docs", title: "Read the docs", state: "waiting", depends_on: ["bd-2"],
    description: "", acceptance: "", notes: "", close_reason: null, report: null,
    waits_for: ["compare"],
  },
];

/** The row's button at *index*, then the dialog of the card that holds it. */
const openRow = (card, index) => {
  const button = card.el.findAll("planCard__task")[index].querySelector("button");
  button.click();
  return { button, dialog: card.el.querySelector("dialog") };
};

const sectionsOf = (dialog) => dialog.findAll("taskDialog__section");
const sectionTitles = (dialog) => sectionsOf(dialog).map((section) => section.find("taskDialog__sectionTitle").textContent);
const tabs = (dialog) => dialog.findAll("taskDialog__tab");
const panels = (dialog) => dialog.findAll("taskDialog__panel");

test("the plan stays compact, closed reads Closed, and every row opens the dialog", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));

  assert.equal(card.el.find("planCard__heading").textContent, "Plan · 1/3 done");
  assert.equal(card.el.find("planCard__summary").textContent, "1 in progress · 1 waiting");
  const rows = card.el.findAll("planCard__task");
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((row) => row.dataset.state), ["closed", "in_progress", "waiting"]);
  // Closed is a state, not a verdict: the mark says Closed, the title stays.
  assert.equal(rows[0].find("planCard__mark").getAttribute("aria-label"), "Closed");
  assert.equal(rows[0].find("planCard__title").textContent, "Research providers");
  assert.match(rows[2].find("planCard__meta").textContent, /waits for compare/);
  for (const row of rows) {
    const button = row.querySelector("button");
    assert.ok(button, "each task is a button");
    assert.equal(button.getAttribute("aria-haspopup"), "dialog");
  }
});

test("a row opens the task's own dialog on its Task tab", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 1);

  assert.equal(dialog.dataset.open, "true");
  assert.equal(dialog.find("taskDialog__title").textContent, "Compare providers");
  assert.equal(dialog.find("taskDialog__id").textContent, "bd-2");
  assert.match(dialog.find("taskDialog__state").textContent, /In progress/);
  const [taskTab, resultTab] = tabs(dialog);
  assert.equal(taskTab.getAttribute("aria-selected"), "true");
  assert.equal(resultTab.getAttribute("aria-selected"), "false");
  assert.equal(panels(dialog)[0].hidden, false);
  assert.equal(panels(dialog)[1].hidden, true);

  assert.deepEqual(sectionTitles(dialog), ["Description", "Acceptance criteria", "Notes", "Depends on"]);
  assert.match(sectionsOf(dialog)[0].textContent, /No description was recorded\./);
  assert.match(sectionsOf(dialog)[1].textContent, /No acceptance criteria were recorded\./);
  assert.match(sectionsOf(dialog)[2].textContent, /No notes were recorded\./);
  // The one dependency the task has is a jump to that task, not plain text.
  const dep = sectionsOf(dialog)[3].find("taskDialog__depLink");
  assert.ok(dep, "the known dependency renders as a link");
  assert.match(dep.textContent, /Research providers/);

  resultTab.click();
  assert.match(panels(dialog)[1].textContent, /No run has reported a result for this task yet\./);
});

test("a closed task shows its close reason apart from its notes, never as a success", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 0);
  tabs(dialog)[0].click(); // past the completed run: read the task itself

  assert.match(dialog.find("taskDialog__state").textContent, /Closed/);
  assert.deepEqual(sectionTitles(panels(dialog)[0]), [
    "Description", "Acceptance criteria", "Notes", "Close reason", "Depends on",
  ]);
  const [, , notes, closeReason, deps] = sectionsOf(panels(dialog)[0]);
  assert.equal(notes.find("taskDialog__notes").textContent, "line one\nline two");
  assert.equal(closeReason.find("taskDialog__notes").textContent, "done, with the table in the answer");
  // Notes and close reason are separate sections with separate texts.
  assert.notEqual(notes.textContent, closeReason.textContent);
  assert.match(deps.textContent, /No dependencies\./);
});

test("a completed run opens on Task and exposes its metrics and answer on Result", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 0);

  const [taskTab, resultTab] = tabs(dialog);
  assert.equal(taskTab.getAttribute("aria-selected"), "true");
  resultTab.click();
  assert.equal(resultTab.getAttribute("aria-selected"), "true");
  const metrics = dialog.findAll("taskDialog__metric").map((metric) => [
    metric.find("taskDialog__metricLabel").textContent,
    metric.find("taskDialog__metricValue").textContent,
  ]);
  assert.ok(metrics.some(([label, value]) => label === "Status" && value === "completed"));
  assert.ok(metrics.some(([label, value]) => label === "Duration" && value === "1m 32s"));
  assert.ok(metrics.some(([label, value]) => label === "Model calls" && value === "7"));
  assert.ok(metrics.some(([label, value]) => label === "Tokens" && /12[,.]?345/.test(value)));
  assert.ok(metrics.some(([label, value]) => label === "Cost" && value === "$0.0432"));
  assert.ok(metrics.some(([label, value]) => label === "Model" && value === "claude-x"));
  assert.ok(metrics.some(([label, value]) => label === "Tier" && value === "high"));
  assert.equal(panels(dialog)[1].find("taskDialog__prose").innerHTML, "<p>Found 3 providers.</p>");
});

test("a run that did not complete and a task closed without a run say so honestly", () => {
  const card = createPlanCard();
  card.update(planStep([
    { ...TASKS()[0], report: { status: "failed", seconds: 12, model_calls: 2 } },
    { ...TASKS()[1], state: "closed", close_reason: null },
  ]));
  const first = openRow(card, 0);
  tabs(first.dialog)[1].click();
  assert.match(
    panels(first.dialog)[1].textContent,
    /The run did not complete \(failed\); its result was not recorded\./,
  );

  const second = openRow(card, 1);
  tabs(second.dialog)[1].click();
  assert.match(
    panels(second.dialog)[1].textContent,
    /This task was closed without a run reporting a result\./,
  );
});

test("task markdown is rendered, never trusted as markup", () => {
  const card = createPlanCard();
  card.update(planStep([
    { ...TASKS()[1], description: "<script>alert(1)</script> and **bold** text" },
  ]));
  const { dialog } = openRow(card, 0);
  const prose = sectionsOf(dialog)[0].find("taskDialog__prose");
  assert.ok(prose.innerHTML.includes("<strong>bold</strong>"), "markdown renders");
  assert.ok(prose.innerHTML.includes("&lt;script&gt;"), "markup is escaped");
  assert.ok(!prose.innerHTML.includes("<script>"), "no live tag ever lands in the dialog");
});

test("previous and next walk the plan, with the ends honestly disabled", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 1);

  const [prev, next] = dialog.querySelectorAll("button").filter((button) =>
    ["Previous", "Next"].includes(button.textContent));
  assert.equal(dialog.find("taskDialog__count").textContent, "2 of 3");
  assert.equal(prev.disabled, false);
  assert.equal(next.disabled, false);

  next.click();
  assert.equal(dialog.find("taskDialog__title").textContent, "Read the docs");
  assert.equal(dialog.find("taskDialog__count").textContent, "3 of 3");
  assert.equal(next.disabled, true);

  prev.click();
  prev.click();
  assert.equal(dialog.find("taskDialog__title").textContent, "Research providers");
  assert.equal(dialog.find("taskDialog__count").textContent, "1 of 3");
  assert.equal(prev.disabled, true);
});

test("a dependency jumps to the task it names", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 2); // Read the docs depends on Compare

  sectionsOf(dialog)
    .find((section) => section.find("taskDialog__sectionTitle").textContent === "Depends on")
    .find("taskDialog__depLink")
    .click();
  assert.equal(dialog.find("taskDialog__title").textContent, "Compare providers");
  assert.equal(dialog.find("taskDialog__count").textContent, "2 of 3");
});

test("a plan update while the dialog is open keeps the task, the tab, the scroll and the focus", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 1);
  const [, resultTab] = tabs(dialog);
  resultTab.click();
  dialog.find("taskDialog__body").scrollTop = 77;
  resultTab.focus();

  card.update(planStep([
    TASKS()[0],
    {
      ...TASKS()[1], state: "closed", close_reason: "compared", notes: "added later",
      report: { status: "completed", final: "Cheapest: prov-x.", seconds: 5 },
    },
    TASKS()[2],
  ]));

  assert.equal(dialog.dataset.open, "true");
  assert.equal(dialog.find("taskDialog__title").textContent, "Compare providers");
  assert.equal(resultTab.getAttribute("aria-selected"), "true", "the chosen tab survives the update");
  assert.equal(panels(dialog)[0].hidden, true);
  assert.equal(dialog.find("taskDialog__body").scrollTop, 77, "the reader's place survives the update");
  assert.equal(resultTab.focused, true, "focus stays inside the dialog across the re-render");
  assert.equal(panels(dialog)[1].find("taskDialog__prose").innerHTML, "<p>Cheapest: prov-x.</p>");

  // What the update brought is there once the reader turns to the task tab.
  tabs(dialog)[0].click();
  assert.match(sectionsOf(dialog).find((section) =>
    section.find("taskDialog__sectionTitle").textContent === "Close reason").textContent, /compared/);
});

test("a plan update that drops the open task closes the dialog, focus back on the row that opened it", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const opener = openRow(card, 0); // bd-1 opened the dialog
  const dialog = card.el.querySelector("dialog");
  const [, next] = dialog.querySelectorAll("button").filter((button) =>
    ["Previous", "Next"].includes(button.textContent));
  next.click(); // onto bd-2, still opened by bd-1's row

  card.update(planStep([TASKS()[0], TASKS()[2]])); // bd-2 is gone

  assert.equal(dialog.dataset.open, "false");
  assert.equal(card.el.findAll("planCard__row")[0], document.activeElement, "focus returns to the rebuilt opener row");
});

test("closing - the button and the browser's Escape alike - returns focus to the row", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { button, dialog } = openRow(card, 1);

  // The browser's own Escape path: the dialog fires close.
  for (const fn of dialog.listeners.close ?? []) fn();
  assert.equal(dialog.dataset.open, "false");
  assert.equal(button.focused, true);

  // The visible close button takes the same road.
  const again = openRow(card, 1);
  again.dialog.findAll("taskDialog__close").forEach((closeButton) => closeButton.click());
  assert.equal(again.dialog.dataset.open, "false");
  assert.equal(again.button.focused, true);
});

test("arrow keys move between the tabs, keeping the keyboard on the chosen one", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 1);
  const [taskTab, resultTab] = tabs(dialog);

  for (const fn of taskTab.listeners.keydown ?? []) fn({ key: "ArrowRight", preventDefault() {} });
  assert.equal(resultTab.getAttribute("aria-selected"), "true");
  assert.equal(resultTab.focused, true, "the switch itself lands the focus");

  for (const fn of resultTab.listeners.keydown ?? []) fn({ key: "ArrowLeft", preventDefault() {} });
  assert.equal(taskTab.getAttribute("aria-selected"), "true");
  assert.equal(taskTab.focused, true);
});

test("an old task without any recorded field shows every empty state, not an error", () => {
  const card = createPlanCard();
  card.update(planStep([{ id: "old-1", title: "Old task", state: "ready" }]));
  const { dialog } = openRow(card, 0);

  assert.equal(dialog.find("taskDialog__title").textContent, "Old task");
  assert.equal(dialog.find("taskDialog__id").textContent, "old-1");
  assert.deepEqual(sectionTitles(dialog), ["Description", "Acceptance criteria", "Notes", "Depends on"]);
  for (const section of sectionsOf(dialog)) {
    assert.ok(section.find("taskDialog__missing"), "every empty section says what is missing");
  }
  tabs(dialog)[1].click();
  assert.match(panels(dialog)[1].textContent, /No run has reported a result for this task yet\./);
});


test("each card has unique accessible dialog and panel IDs", () => {
  const cards = [createPlanCard(), createPlanCard()];
  const allIds = [];
  for (const card of cards) {
    card.update(planStep(TASKS()));
    const { dialog } = openRow(card, 1);
    const titleId = dialog.find("taskDialog__title").getAttribute("id");
    assert.equal(dialog.getAttribute("aria-labelledby"), titleId);
    allIds.push(titleId);
    tabs(dialog).forEach((tab, index) => {
      const panel = panels(dialog)[index];
      assert.equal(tab.getAttribute("aria-controls"), panel.getAttribute("id"));
      assert.equal(panel.getAttribute("aria-labelledby"), tab.getAttribute("id"));
      allIds.push(tab.getAttribute("id"), panel.getAttribute("id"));
    });
  }
  assert.equal(new Set(allIds).size, allIds.length);
});

test("real orchestrate token objects show input and output without NaN", () => {
  const card = createPlanCard();
  const task = { ...TASKS()[0], report: { status: "completed", final: "Result", tokens: { input: 1200, output: 300 } } };
  card.update(planStep([task]));
  const { dialog } = openRow(card, 0);
  const metrics = panels(dialog)[1].findAll("taskDialog__metric");
  assert.deepEqual(metrics.map((row) => row.find("taskDialog__metricLabel").textContent), ["Status", "Input tokens", "Output tokens"]);
  assert.ok(!panels(dialog)[1].textContent.includes("NaN"));
});

test("old completed reports without text do not falsely say the run failed", () => {
  const card = createPlanCard();
  card.update(planStep([{ ...TASKS()[1], report: { status: "completed" } }]));
  const { dialog } = openRow(card, 0);
  assert.match(panels(dialog)[1].textContent, /The run completed, but no result text was recorded/);
  assert.ok(!panels(dialog)[1].textContent.includes("did not complete"));
});

test("failed reports never render a partial answer as a successful result", () => {
  const card = createPlanCard();
  card.update(planStep([{ ...TASKS()[1], report: { status: "timeout", final: "PARTIAL ANSWER" } }]));
  const { dialog } = openRow(card, 0);
  assert.match(panels(dialog)[1].textContent, /did not complete \(timeout\)/);
  assert.equal(panels(dialog)[1].find("taskDialog__prose"), null);
});

test("identical live updates preserve the rendered text nodes and close-button focus", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 1);
  const panel = panels(dialog)[0];
  const close = dialog.find("taskDialog__close");
  close.focus();
  card.update(planStep(TASKS()));
  assert.equal(panels(dialog)[0], panel);
  assert.equal(document.activeElement, close);
});

test("removed opener falls back to the plan header, and empty plans dismiss the dialog", () => {
  const card = createPlanCard();
  card.update(planStep(TASKS()));
  const { dialog } = openRow(card, 0);
  card.update(planStep([TASKS()[1]]));
  assert.equal(dialog.dataset.open, "false");
  assert.equal(document.activeElement, card.el.find("planCard__head"));
  openRow(card, 0);
  card.update(planStep([]));
  assert.equal(dialog.dataset.open, "false");
  assert.equal(card.el.hidden, true);
});