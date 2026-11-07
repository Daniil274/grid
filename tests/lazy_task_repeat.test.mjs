import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";
import { repeatChoice, repeatHours } from "../web_chat/js/lazy/repeat.js";
import { openTaskDialog } from "../web_chat/js/lazy/dialogs.js";
import { api } from "../web_chat/js/net/api.js";

// -- the interval behind each choice ---------------------------------------------------------
test("the once, daily and weekly choices are the intervals the service takes", () => {
  assert.deepEqual(repeatHours("once"), { hours: 0, problem: "" });
  assert.deepEqual(repeatHours("daily"), { hours: 24, problem: "" });
  assert.deepEqual(repeatHours("weekly"), { hours: 168, problem: "" });
});

test("an own interval is hours, and a bad one comes back with a problem", () => {
  assert.deepEqual(repeatHours("custom", "36"), { hours: 36, problem: "" });
  assert.deepEqual(repeatHours("custom", " 36,5 "), { hours: 36.5, problem: "" });
  assert.match(repeatHours("custom", "").problem, /hours/i);
  assert.match(repeatHours("custom", "0.5").problem, /at least one hour/i);
  assert.match(repeatHours("custom", "-4").problem, /at least one hour/i);
  assert.match(repeatHours("custom", "weekly").problem, /number of hours/i);
  assert.equal(repeatHours("custom", "0.5").hours, null);
});

test("a stored interval comes back as the choice an edit form would show", () => {
  assert.equal(repeatChoice(null).choice, "once");
  assert.equal(repeatChoice(24 * 3600).choice, "daily");
  assert.equal(repeatChoice(7 * 24 * 3600).choice, "weekly");
  // A custom interval is not rounded onto a preset.
  assert.deepEqual(repeatChoice(36 * 3600), { choice: "custom", custom: "36" });
});

// -- the creation form -----------------------------------------------------------------------
/**
 * The task dialog in the fake DOM: a <dialog> the test can open and close, and
 * the two accessors the segmented control and the fields need.
 */
function setup() {
  installFakeDom();
  const Element = document.createElement("div").constructor;
  Object.defineProperty(Element.prototype, "parentElement", { configurable: true, get() { return this.parentNode; } });
  Object.defineProperty(Element.prototype, "children", {
    configurable: true,
    get() { return this.childNodes.filter((node) => node instanceof Element); },
  });
  const dialog = document.createElement("dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; };
  document.getElementById = () => dialog;

  const state = {
    now: 1_800_000_000, loadedAt: 1_800_000_000,
    agents: [], account: { key: "claude-code", label: "Claude" }, pools: [],
    sizes: Object.fromEntries(["S", "M", "L"].map((size) => [size, {
      scout: { tokens: 1000 }, work: { tokens: 2000 }, verify: { tokens: 1000 }, extra: { tokens: 500 },
    }])),
  };
  const sent = [];
  const act = async (promise, label) => {
    const value = await promise;
    sent.push({ label, value });
    return value;
  };
  const original = api.lazyCreate;
  api.lazyCreate = async (payload) => { sent.push({ label: "payload", value: payload }); return { tasks: [] }; };

  openTaskDialog(state, act);

  const input = (predicate) => dialog.querySelectorAll("input").find(predicate);
  const title = input((node) => node.getAttribute("placeholder") === "Weekly report on the API errors");
  const custom = input((node) => node.getAttribute("type") === "number");
  const choose = (value) => dialog.querySelectorAll("button").find((node) => node.dataset.value === value).click();
  const type = (node, value) => { node.value = value; for (const fn of node.listeners.input ?? []) fn(); };
  const submit = () => dialog.find("btn--primary").click();
  const error = () => dialog.find("lazyDialog__error").textContent;
  const problem = () => dialog.find("field__problem").textContent;
  const restore = () => { api.lazyCreate = original; };
  type(title, "Weekly report");
  return { dialog, sent, choose, type, submit, custom, error, problem, restore };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test("the form sends a day for the daily preset", async () => {
  const form = setup();
  try {
    form.choose("daily");
    form.submit();
    await flush();
    const payload = form.sent.find((entry) => entry.label === "payload").value;
    assert.equal(payload.repeat_hours, 24);
  } finally {
    form.restore();
  }
});

test("the form sends a week for the weekly preset", async () => {
  const form = setup();
  try {
    form.choose("weekly");
    form.submit();
    await flush();
    const payload = form.sent.find((entry) => entry.label === "payload").value;
    assert.equal(payload.repeat_hours, 168);
  } finally {
    form.restore();
  }
});

test("the form sends an own interval in hours", async () => {
  const form = setup();
  try {
    form.choose("custom");
    assert.equal(form.custom.hidden, false);
    form.type(form.custom, "36");
    form.submit();
    await flush();
    const payload = form.sent.find((entry) => entry.label === "payload").value;
    assert.equal(payload.repeat_hours, 36);
    assert.equal(form.error(), "");
  } finally {
    form.restore();
  }
});

test("a bad own interval stops the form and shows why", async () => {
  const form = setup();
  try {
    form.choose("custom");
    form.type(form.custom, "0.5");
    form.submit();
    await flush();
    assert.equal(form.sent.some((entry) => entry.label === "payload"), false);
    assert.match(form.error(), /at least one hour/i);
    assert.match(form.problem(), /at least one hour/i);
    // The task is not created until the interval is usable.
    form.type(form.custom, "2");
    form.submit();
    await flush();
    assert.equal(form.sent.find((entry) => entry.label === "payload").value.repeat_hours, 2);
  } finally {
    form.restore();
  }
});

test("the hours field shows only for an own interval and its problem goes away", async () => {
  const form = setup();
  try {
    assert.equal(form.custom.hidden, true);
    form.choose("custom");
    assert.equal(form.custom.hidden, false);
    form.type(form.custom, "0.5");
    assert.match(form.problem(), /at least one hour/i);
    form.choose("daily");
    assert.equal(form.custom.hidden, true);
    assert.equal(form.problem(), "");
    form.submit();
    await flush();
    assert.equal(form.sent.find((entry) => entry.label === "payload").value.repeat_hours, 24);
  } finally {
    form.restore();
  }
});
