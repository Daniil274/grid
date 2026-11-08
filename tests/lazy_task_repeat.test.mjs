import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";
import { describeSchedule, repeatPayload, timeProblem, zoneProblem } from "../web_chat/js/lazy/repeat.js";
import { openTaskDialog } from "../web_chat/js/lazy/dialogs.js";
import { api } from "../web_chat/js/net/api.js";

// -- the payload behind each choice -----------------------------------------------------------
test("once sends no repeat at all", () => {
  assert.deepEqual(repeatPayload({ choice: "once" }), { repeat_hours: 0, repeat: null, problem: "" });
  assert.deepEqual(repeatPayload({}), { repeat_hours: 0, repeat: null, problem: "" });
});

test("an interval is hours, and a bad one comes back with a problem", () => {
  assert.deepEqual(repeatPayload({ choice: "interval", hours: "36" }), { repeat_hours: 36, repeat: null, problem: "" });
  assert.equal(repeatPayload({ choice: "interval", hours: " 36,5 " }).repeat_hours, 36.5);
  assert.match(repeatPayload({ choice: "interval", hours: "" }).problem, /hours/i);
  assert.match(repeatPayload({ choice: "interval", hours: "0.5" }).problem, /at least one hour/i);
  assert.match(repeatPayload({ choice: "interval", hours: "-4" }).problem, /at least one hour/i);
  assert.match(repeatPayload({ choice: "interval", hours: "weekly" }).problem, /number of hours/i);
  assert.equal(repeatPayload({ choice: "interval", hours: "0.5" }).repeat_hours, 0);
  // An interval is not a calendar rule: it never sends one.
  assert.equal(repeatPayload({ choice: "interval", hours: "6" }).repeat, null);
});

test("a daily rule is a wall time in the named zone, with missed slots skipped", () => {
  assert.deepEqual(repeatPayload({ choice: "daily", time: "04:00", timezone: "Europe/Moscow" }), {
    repeat_hours: 0,
    repeat: { kind: "daily", time: "04:00", timezone: "Europe/Moscow", missed: "skip" },
    problem: "",
  });
});

test("a weekly rule names its day too", () => {
  const { repeat, problem } = repeatPayload({ choice: "weekly", time: "18:30", weekday: "fri", timezone: "UTC" });
  assert.equal(problem, "");
  assert.deepEqual(repeat, { kind: "weekly", time: "18:30", weekday: "fri", timezone: "UTC", missed: "skip" });
  assert.match(repeatPayload({ choice: "weekly", weekday: "someday", timezone: "UTC" }).problem, /weekday/i);
});

test("a wall time that is not HH:MM and an unknown zone are refused, never read as UTC", () => {
  assert.equal(timeProblem("04:00"), "");
  assert.match(timeProblem("4:00"), /HH:MM/i);
  assert.match(timeProblem("24:00"), /HH:MM/i);
  assert.match(repeatPayload({ choice: "daily", time: "25:00", timezone: "UTC" }).problem, /HH:MM/i);
  assert.equal(zoneProblem("Europe/Moscow"), "");
  assert.match(zoneProblem("Mars/Base"), /unknown time zone/i);
  assert.match(zoneProblem(""), /time zone/i);
  assert.match(repeatPayload({ choice: "daily", time: "04:00", timezone: "Mars/Base" }).problem, /unknown time zone/i);
});

test("a stored rule reads back in words, and the two kinds are told apart", () => {
  assert.equal(describeSchedule(null), "Runs once");
  assert.equal(describeSchedule({ kind: "interval", seconds: 6 * 3600 }), "Every 6 h");
  assert.equal(describeSchedule({ kind: "interval", seconds: 24 * 3600 }), "Every day");
  assert.equal(describeSchedule({ kind: "interval", seconds: 7 * 24 * 3600 }), "Every 7 days");
  assert.equal(describeSchedule({ kind: "daily", time: "04:00", timezone: "Europe/Moscow" }), "Daily at 04:00 (Europe/Moscow)");
  assert.equal(describeSchedule({ kind: "weekly", time: "04:00", weekday: "mon", timezone: "UTC" }), "Weekly on Mon at 04:00 (UTC)");
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
    settings: { routine: { timezone: "Europe/Moscow" } },
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

  const inputs = () => dialog.querySelectorAll("input");
  const by = (attribute, value) => inputs().find((node) => node.getAttribute(attribute) === value);
  const title = by("placeholder", "Weekly report on the API errors");
  const hours = by("type", "number");
  const time = by("type", "time");
  const zone = by("placeholder", "Europe/Moscow");
  const choose = (value) => dialog.querySelectorAll("button").find((node) => node.dataset.value === value).click();
  const type = (node, value) => { node.value = value; for (const fn of node.listeners.input ?? []) fn(); };
  const change = (node, value) => { node.value = value; for (const fn of node.listeners.change ?? []) fn(); };
  const submit = () => dialog.find("btn--primary").click();
  const error = () => dialog.find("lazyDialog__error").textContent;
  const problem = () => dialog.find("field__problem").textContent;
  const restore = () => { api.lazyCreate = original; };
  type(title, "Weekly report");
  return { dialog, sent, choose, type, change, submit, hours, time, zone, error, problem, restore };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const payloadOf = (form) => form.sent.find((entry) => entry.label === "payload")?.value;

test("the form sends a calendar day, not 24 hours, for a daily rule", async () => {
  const form = setup();
  try {
    form.choose("daily");
    form.submit();
    await flush();
    const payload = payloadOf(form);
    assert.equal(payload.repeat_hours, 0);
    assert.deepEqual(payload.repeat, { kind: "daily", time: "04:00", timezone: "Europe/Moscow", missed: "skip" });
    assert.equal(payload.not_before, null);
  } finally {
    form.restore();
  }
});

test("the form sends the named day and wall time for a weekly rule", async () => {
  const form = setup();
  try {
    form.choose("weekly");
    form.type(form.time, "18:30");
    form.submit();
    await flush();
    const payload = payloadOf(form);
    assert.deepEqual(payload.repeat, { kind: "weekly", time: "18:30", weekday: "mon", timezone: "Europe/Moscow", missed: "skip" });
    assert.equal(payload.repeat_hours, 0);
  } finally {
    form.restore();
  }
});

test("an interval sends hours and no calendar rule", async () => {
  const form = setup();
  try {
    form.choose("interval");
    assert.equal(form.hours.hidden, false);
    form.type(form.hours, "36");
    form.submit();
    await flush();
    const payload = payloadOf(form);
    assert.equal(payload.repeat_hours, 36);
    assert.equal(payload.repeat, null);
    assert.equal(form.error(), "");
  } finally {
    form.restore();
  }
});

test("a bad interval stops the form and shows why", async () => {
  const form = setup();
  try {
    form.choose("interval");
    form.type(form.hours, "0.5");
    form.submit();
    await flush();
    assert.equal(payloadOf(form), undefined);
    assert.match(form.error(), /at least one hour/i);
    assert.match(form.problem(), /at least one hour/i);
    form.type(form.hours, "2");
    form.submit();
    await flush();
    assert.equal(payloadOf(form).repeat_hours, 2);
  } finally {
    form.restore();
  }
});

test("an unknown zone stops a calendar rule instead of being sent", async () => {
  const form = setup();
  try {
    form.choose("daily");
    form.type(form.zone, "Mars/Base");
    form.submit();
    await flush();
    assert.equal(payloadOf(form), undefined);
    assert.match(form.error(), /unknown time zone/i);
    form.type(form.zone, "Europe/Berlin");
    form.submit();
    await flush();
    assert.equal(payloadOf(form).repeat.timezone, "Europe/Berlin");
  } finally {
    form.restore();
  }
});

test("the interval field shows only for an interval, and the calendar fields only for a rule", () => {
  const form = setup();
  try {
    assert.equal(form.hours.hidden, true);
    form.choose("interval");
    assert.equal(form.hours.hidden, false);
    form.choose("weekly");
    assert.equal(form.hours.hidden, true);
    assert.equal(form.time.parentNode.hidden, false);
    form.choose("once");
    assert.equal(form.time.parentNode.parentNode.hidden, true);
  } finally {
    form.restore();
  }
});

test("an empty budget sends null; zero is sent as zero", async () => {
  const form = setup();
  try {
    const budget = form.dialog.querySelectorAll("input").find((node) => node.getAttribute("placeholder") === "No own limit");
    form.submit();
    await flush();
    assert.equal(payloadOf(form).cap, null);
    form.type(budget, "0");
    form.submit();
    await flush();
    assert.equal(payloadOf(form).cap, 0);
    form.type(budget, "12.5");
    form.submit();
    await flush();
    assert.equal(payloadOf(form).cap, 12.5);
  } finally {
    form.restore();
  }
});

test("a release picked in the form is sent as a moment, apart from the deadline", async () => {
  const form = setup();
  try {
    // The deadline picker comes first in the form; the release is the second one.
    const release = form.dialog.querySelectorAll("input").filter((node) => node.getAttribute("type") === "datetime-local")[1];
    form.change(release, "2026-11-10T09:30");
    form.submit();
    await flush();
    const payload = payloadOf(form);
    assert.equal(payload.not_before, Math.round(new Date("2026-11-10T09:30").getTime() / 1000));
    assert.equal(payload.deadline, null);
  } finally {
    form.restore();
  }
});
