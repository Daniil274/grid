/**
 * Public agent messages alternate with full execution panels. A message closes
 * the panel before it; reasoning and calls after it form the next one. Stored
 * traces and typed events rebuild the same stream without duplicate statuses.
 *
 * Run with the rest of the UI tests: `node --test tests/*.test.mjs`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();
document.addEventListener ??= () => {};
document.removeEventListener ??= () => {};

const { createActivityView, eventFromStep, isPublicCommunication, publicCommunications } = await import("../web_chat/js/ui/activity.js");
const { createMessage } = await import("../web_chat/js/ui/message.js");
const { createTranscript } = await import("../web_chat/js/ui/transcript.js");
const { createReasoningPanel } = await import("../web_chat/js/ui/reasoning.js");

let clock = 0;

/** One step frame of a live turn, as web_chat/trace.py puts it on the wire. */
const step = (id, fields = {}) => ({
  id,
  kind: "tool",
  title: id,
  status: "done",
  subtitle: "",
  detail: "",
  body: "",
  refs: [],
  at_ms: clock++,
  duration_ms: 1,
  tone: "neutral",
  policy: null,
  parent_id: null,
  ...fields,
});

/** One typed event, as web_chat/observer.py serialises it. */
const event = (id, fields = {}) => ({
  v: 1,
  id,
  kind: "message",
  visibility: "public",
  author: "",
  author_key: "",
  content: "",
  status: "done",
  at: null,
  at_ms: 0,
  turn_id: "t1",
  message_id: "m2",
  step_id: null,
  parent_id: null,
  ...fields,
});

const transcript = () => createTranscript({
  container: document.createElement("div"),
  emptyState: document.createElement("div"),
  jumpButton: document.createElement("button"),
});

const timelineKinds = (view) => view.el.find("msg__activityTimeline").childNodes.map((node) => {
  if (node.classList.contains("msg__activityMessage")) return "message";
  if (node.classList.contains("msg__activityExecution")) return "execution";
  return "unknown";
});

const stepTitles = (root) => root.findAll("step").map((row) =>
  row.find("step__messageText")?.textContent ?? row.find("step__title")?.textContent ?? "");

test("only public messages and statuses reach the stream", () => {
  const view = createActivityView();
  assert.equal(view.add(event("e1", { kind: "report", visibility: "internal", content: "think", step_id: "s1" })), false);
  assert.equal(view.add(event("e2", { kind: "status", visibility: "internal", content: "Read secrets.env", step_id: "s2" })), false);
  assert.equal(view.add({ kind: "message", visibility: "public" }), false);
  assert.equal(isPublicCommunication(null), false);
  assert.equal(view.size, 0);
  assert.equal(view.isEmpty, true);
  assert.equal(view.el.hidden, true);
});

test("narration is prose, work is the main panel, and the answer is not drawn twice", () => {
  const view = createActivityView();
  view.apply([
    event("e1", { step_id: "s2", author: "coder", content: "Reading **the** diff." }),
    event("e2", { kind: "status", step_id: "s3", content: "Ran the tests" }),
    event("e3", { author: "coder", content: "All **green**." }),
  ]);

  assert.equal(view.size, 1);
  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  assert.match(view.el.find("msg__activityText").innerHTML, /<strong>the<\/strong> diff/);
  assert.equal(view.el.find("msg__activityAuthor").textContent, "coder");
  assert.equal(view.el.findAll("reasoning").length, 1);
  assert.deepEqual(stepTitles(view.el), ["Ran the tests"]);
  assert.equal(view.el.findAll("msg__activityWork").length, 0);
  assert.equal(view.el.findAll("msg__activityText").length, 1);
  assert.equal(view.el.hidden, false);
});

test("the activity view escapes what the agent wrote", () => {
  const view = createActivityView();
  view.add(event("e1", { step_id: "s1", content: "<script>alert(1)</script>" }));
  const html = view.el.find("msg__activityText").innerHTML;
  assert.equal(html.includes("<script"), false);
  assert.match(html, /&lt;script&gt;/);
});

test("messages and full execution panels alternate, and late updates stay in their segment", () => {
  const view = createActivityView();
  view.apply([
    event("e1", { step_id: "s1", content: "I will inspect the configuration." }),
    event("e2", { kind: "status", step_id: "s2", content: "Read config.yaml" }),
    event("e3", { kind: "status", step_id: "s3", content: "Ran tests" }),
    event("e4", { step_id: "s4", content: "The tests exposed a regression." }),
    event("e5", { kind: "status", step_id: "s5", content: "Checking the fix", status: "running" }),
  ]);

  assert.deepEqual(timelineKinds(view), ["message", "execution", "message", "execution"]);
  const executions = view.el.findAll("msg__activityExecution");
  assert.equal(executions.length, 2);
  assert.deepEqual(stepTitles(executions[0]), ["Read config.yaml", "Ran tests"]);
  assert.deepEqual(stepTitles(executions[1]), ["Checking the fix"]);
  assert.equal(view.el.findAll("msg__activityWork").length, 0);

  view.apply([event("e3-replay", { kind: "status", step_id: "s3", content: "Ran tests", status: "error" })]);
  assert.equal(executions[0].findAll("step")[1].dataset.status, "error");
  assert.equal(executions[1].find("step").dataset.status, "running");
  assert.equal(view.el.findAll("msg__activityExecution").length, 2);
});

test("each live message closes its execution panel and the next step opens another", () => {
  const view = createActivityView();
  view.start();
  view.upsert(step("s1", { title: "Read config.yaml", status: "running" }));
  const first = view.el.find("reasoning");
  assert.equal(first.dataset.state, "running");
  assert.equal(first.find("reasoning__toggle").getAttribute("aria-expanded"), "true");

  view.upsert(step("m1", { kind: "message", body: "The config is loaded." }));
  assert.equal(first.dataset.state, "idle");
  assert.equal(first.find("reasoning__toggle").getAttribute("aria-expanded"), "false");

  view.upsert(step("s2", { title: "Run tests", status: "running" }));
  const panels = view.el.findAll("reasoning");
  assert.equal(panels.length, 2);
  const second = panels[1];
  assert.equal(second.dataset.state, "running");
  assert.equal(second.find("reasoning__toggle").getAttribute("aria-expanded"), "true");
  assert.deepEqual(timelineKinds(view), ["execution", "message", "execution"]);

  // A late frame patches the first panel; it does not reopen or move the step.
  view.upsert(step("s1", { title: "Read config.yaml", status: "error" }));
  assert.equal(first.find("step").dataset.status, "error");
  assert.equal(view.el.findAll("reasoning").length, 2);

  view.finish(1200, true);
  assert.equal(second.dataset.state, "idle");
  assert.equal(second.find("reasoning__toggle").getAttribute("aria-expanded"), "false");
});

test("a message before a call survives the answer, final output and reconnect", () => {
  const first = event("e1", { step_id: "s2", author: "coder", content: "Let me check the diff." });
  const status = event("e2", { kind: "status", step_id: "s3", content: "Read app.py" });
  const final = event("e3", { author: "coder", content: "Done." });
  const stored = [first, status, final];

  const message = createMessage({ role: "assistant", content: "Done.", author: "coder", events: stored });
  assert.equal(message.activity.size, 1);
  assert.match(message.el.find("msg__activityText").innerHTML, /check the diff/);
  assert.deepEqual(stepTitles(message.el), ["Read app.py"]);

  message.setText("Done in 2s.");
  message.setWaiting(false);
  message.observeStep(step("s2", { kind: "message", body: "Let me check the diff." }));
  message.adopt({ id: "m2", versions: null, checkpoint: null, events: stored });

  assert.equal(message.el.findAll("msg__activityMessage").length, 1);
  assert.equal(message.el.findAll("reasoning").length, 1);
  assert.match(message.el.find("msg__activityText").innerHTML, /check the diff/);
  assert.equal(message.el.find("msg__activityAuthor").textContent, "coder");
});

test("live message steps and stored events share one prose block", () => {
  const view = createActivityView();
  view.start();
  assert.equal(view.upsert(step("s2", { kind: "message", body: "Reading it now." })), true);
  assert.equal(view.observeStep(step("s2", { kind: "message", body: "Reading it now." })), true);
  view.apply([event("ev-1", { step_id: "s2", author: "coder", content: "Reading it now." })]);
  assert.equal(view.size, 1);
  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  assert.equal(view.el.find("msg__activityAuthor").textContent, "coder");
  view.finish();
});

test("narration without an author takes the agent the turn was routed to", () => {
  const message = createMessage({ role: "assistant", author: "Routing…" });
  message.observeStep(step("s2", { kind: "message", body: "On it." }));
  message.setAuthor("coder");
  assert.equal(message.el.find("msg__activityAuthor").textContent, "coder");
  assert.equal(message.el.find("msg__activityAuthor").hidden, false);
});

test("full trace prose is never replaced by a capped public event", () => {
  const full = `begin ${"x".repeat(20_100)} end`;
  const capped = full.slice(0, 20_000);
  const view = createActivityView();
  view.start();
  view.upsert(step("m1", { kind: "message", body: full }));
  view.add(event("e1", { step_id: "m1", content: capped }));
  assert.ok(view.el.find("msg__activityText").innerHTML.includes(full));
  view.finish();

  const stored = createActivityView();
  stored.apply([event("e1", { step_id: "m1", content: capped })]);
  stored.hydrate([step("m1", { kind: "message", body: full })]);
  assert.ok(stored.el.find("msg__activityText").innerHTML.includes(full));
});

test("plan updates after a message reuse the turn's one plan card", () => {
  const plan = (id, title) => step(id, {
    kind: "plan",
    title: "Plan",
    plan: { tasks: [{ id: "a", key: "", title, state: "ready", depends_on: [], turn: true }], total: 1 },
  });
  const view = createActivityView();
  view.start();
  view.upsert(plan("p1", "Research"));
  view.upsert(step("s1", { title: "Read config.yaml" }));
  view.upsert(step("m1", { kind: "message", body: "The plan is underway." }));
  view.upsert(plan("p2", "Research updated"));

  assert.equal(view.el.findAll("planCard").length, 1);
  assert.equal(view.el.find("planCard__title").textContent, "Research updated");
  view.finish();
});

test("each live segment reports its own usage, not the whole turn's", () => {
  const view = createActivityView();
  view.start();
  view.upsert(step("s1", { title: "First", tokens_in: 2, tokens_out: 1 }));
  view.upsert(step("m1", { kind: "message", body: "First stage done." }));
  view.upsert(step("s2", { title: "Second", tokens_in: 3, tokens_out: 4 }));
  view.finish(90_000, true, { tokensIn: 100, tokensOut: 100 });

  const timers = view.el.findAll("reasoning__timer").map((node) => node.textContent);
  assert.match(timers[0], /↑2.*↓1/);
  assert.match(timers[1], /↑3.*↓4/);
  assert.equal(timers.some((text) => text.includes("↑100")), false);
});

test("one-block mode puts public messages in the full reasoning timeline", () => {
  const view = createActivityView({ mode: "combined" });
  view.start();
  view.upsert(step("s1", { title: "Read config.yaml" }));
  view.upsert(step("m1", { kind: "message", title: "Narration", body: "Reading the config." }));
  view.add(event("e1", { step_id: "m1", content: "Reading the config." }));
  view.upsert(step("s2", { title: "Run tests" }));

  const panel = view.el.find("reasoning");
  assert.equal(view.el.findAll("reasoning").length, 1);
  assert.deepEqual(stepTitles(panel), ["Read config.yaml", "Reading the config.", "Run tests"]);
  assert.equal(view.el.findAll("msg__activityMessage").length, 0);
  assert.equal(view.mode, "combined");
  view.finish();
});

test("the transcript mode switch reprojects messages already on screen", () => {
  const t = transcript();
  const message = t.add({
    role: "assistant",
    content: "Done.",
    author: "coder",
    events: [event("e1", { step_id: "m1", content: "I will inspect the diff." })],
  });
  message.activity.upsert(step("s1", { title: "Read config.yaml" }));

  t.setExecutionMode("combined");
  assert.equal(message.el.findAll("reasoning").length, 1);
  assert.deepEqual(stepTitles(message.el.find("reasoning")), ["I will inspect the diff.", "Read config.yaml"]);
  assert.equal(message.el.findAll("msg__activityMessage").length, 0);

  t.setExecutionMode("segmented");
  assert.deepEqual(timelineKinds(message.activity), ["message", "execution"]);
  assert.equal(message.el.findAll("msg__activityMessage").length, 1);
});

test("execution mode control lives in the assistant message actions", () => {
  let toggled = 0;
  const message = createMessage({
    role: "assistant",
    content: "Done.",
    onExecutionModeToggle: () => { toggled += 1; },
  });
  const button = message.el.find("msgAction--executionMode");
  assert.ok(button);
  assert.equal(button.getAttribute("aria-label"), "Show work as one block");
  button.click();
  assert.equal(toggled, 1);

  message.setExecutionMode("combined");
  assert.equal(button.getAttribute("aria-label"), "Show work by messages");
  assert.equal(button.getAttribute("aria-pressed"), "true");
});

test("switching modes reprojects existing steps and the live turn", () => {
  const view = createActivityView();
  view.start();
  view.upsert(step("s1", { title: "Read config.yaml" }));
  view.upsert(step("m1", { kind: "message", title: "Narration", body: "Reading the config." }));
  view.upsert(step("s2", { title: "Run tests" }));

  view.setMode("combined");
  assert.equal(view.el.findAll("reasoning").length, 1);
  assert.deepEqual(stepTitles(view.el.find("reasoning")), ["Read config.yaml", "Reading the config.", "Run tests"]);
  assert.equal(view.el.findAll("msg__activityMessage").length, 0);
  assert.equal(view.el.find("reasoning").dataset.state, "running");

  view.setMode("segmented");
  assert.deepEqual(timelineKinds(view), ["execution", "message", "execution"]);
  assert.equal(view.el.findAll("reasoning").length, 2);
  assert.deepEqual(stepTitles(view.el.findAll("msg__activityExecution")[0]), ["Read config.yaml"]);
  assert.deepEqual(stepTitles(view.el.findAll("msg__activityExecution")[1]), ["Run tests"]);
  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  view.finish();
});

test("stored trace and events rebuild one chronological stream without duplicates", () => {
  const view = createActivityView();
  const replay = [
    event("e1", { step_id: "s2", author: "coder", content: "Read the config." }),
    event("e2", { kind: "status", step_id: "s1", content: "Read config.yaml" }),
    event("e3", { kind: "status", step_id: "s3", content: "Run tests" }),
  ];
  view.apply(replay);
  view.hydrate([
    step("s1", { title: "Read config.yaml", at_ms: 0 }),
    step("s2", { kind: "message", body: "Read the config.", at_ms: 1 }),
    step("s3", { title: "Run tests", at_ms: 2 }),
  ]);

  assert.deepEqual(timelineKinds(view), ["execution", "message", "execution"]);
  assert.equal(view.el.find("msg__activityAuthor").textContent, "coder");
  const executions = view.el.findAll("msg__activityExecution");
  assert.deepEqual(stepTitles(executions[0]), ["Read config.yaml"]);
  assert.deepEqual(stepTitles(executions[1]), ["Run tests"]);
  for (const panel of view.el.findAll("reasoning")) {
    assert.equal(panel.find("reasoning__toggle").getAttribute("aria-expanded"), "false");
  }

  view.apply(replay);
  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  assert.equal(view.el.findAll("msg__activityExecution").length, 2);
  assert.equal(view.el.findAll("step").length, 2);
});

test("the latest stored status wins without changing its stream position", () => {
  const view = createActivityView();
  const replay = [
    event("running", { kind: "status", step_id: "s1", content: "Run tests", status: "running" }),
    event("error", { kind: "status", step_id: "s1", content: "Run tests", status: "error" }),
  ];
  view.apply(replay);
  view.hydrate([]);
  assert.equal(view.el.findAll("step").length, 1);
  assert.equal(view.el.find("step").dataset.status, "error");
});

test("stored execution segments retain lazy payload loading", async () => {
  const view = createActivityView();
  const loaded = [];
  view.hydrate([step("s1", { title: "Read app.py", lazy: true })], {
    loadStep: async (id) => {
      loaded.push(id);
      return { body: "file contents", lazy: false };
    },
  });

  const panel = view.el.find("reasoning");
  panel.find("reasoning__toggle").click();
  panel.find("step__head").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(loaded, ["s1"]);
});

test("what a sub-agent does silently stays out of the public stream", () => {
  assert.equal(eventFromStep(step("s1", { kind: "message", body: "I will fix it.", parent_id: "a1" })), null);
  assert.equal(eventFromStep(step("s2", { kind: "tool", title: "bash", parent_id: "a1" })), null);
  assert.equal(eventFromStep(step("s3", { kind: "reasoning", body: "hmm" })), null);
  assert.equal(eventFromStep(step("s4", { kind: "plan", title: "Plan" })).kind, "status");
  assert.equal(eventFromStep(step("s5", { kind: "error", title: "Bad tool", parent_id: "a1" })), null);
});

test("a public message is not a second row in the main execution panel", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { kind: "message", body: "Let me look.", title: "Message" }));
  assert.equal(panel.isEmpty, true);
  panel.upsert(step("a1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s2", { kind: "message", body: "Checking.", title: "Message", parent_id: "a1" }));
  assert.equal(panel.isEmpty, false);
});

test("a stored conversation renders its message and execution stream together", () => {
  const t = transcript();
  t.render([{
    id: "m2",
    role: "assistant",
    content: "Done.",
    timestamp: 1,
    events: [
      event("e1", { step_id: "s2", author: "coder", content: "Let me check the diff." }),
      event("e2", { kind: "status", step_id: "s3", content: "Ran the tests" }),
      event("e3", { author: "coder", content: "Done." }),
    ],
    trace: [
      step("s2", { kind: "message", body: "Let me check the diff.", at_ms: 0 }),
      step("s3", { title: "Ran the tests", at_ms: 1 }),
    ],
  }]);

  assert.match(t.lastAnswer().el.find("msg__activityText").innerHTML, /check the diff/);
  assert.deepEqual(stepTitles(t.lastAnswer().el), ["Ran the tests"]);
  assert.deepEqual(timelineKinds(t.lastAnswer().activity), ["message", "execution"]);

  assert.equal(t.adopt([{ id: "m2", role: "assistant", content: "Done.", events: [] }]), true);
  t.lastAnswer().activity.apply([event("e1", { step_id: "s2", author: "coder", content: "Let me check the diff." })]);
  assert.equal(t.lastAnswer().el.findAll("msg__activityMessage").length, 1);
  assert.equal(t.lastAnswer().el.findAll("reasoning").length, 1);
});

test("the public list keeps order and drops duplicates by step", () => {
  const list = publicCommunications([
    event("e1", { step_id: "s1", content: "first" }),
    event("e2", { kind: "status", step_id: "s2", content: "second" }),
    event("e3", { step_id: "s1", content: "first" }),
    event("e4", { kind: "report", visibility: "internal", content: "hidden" }),
  ]);
  assert.deepEqual(list.map((item) => item.content), ["first", "second"]);
});

test("a child's explicit public commentary keeps its author and is drawn once", () => {
  const view = createActivityView();
  const live = event("child-1", {
    step_id: "s-child-1",
    author: "researcher",
    author_key: "researcher",
    parent_id: "call-1",
    turn_id: "run-1",
    content: "Checking the **router** now.",
  });
  assert.equal(view.add(live), true);
  assert.equal(view.size, 1);
  assert.equal(view.el.find("msg__activityAuthor").textContent, "researcher");
  assert.match(view.el.find("msg__activityText").innerHTML, /<strong>router<\/strong>/);
  view.apply([{ ...live }]);
  assert.equal(view.size, 1);
});

test("a child's public commentary does not also remain inside its execution report", () => {
  const commentary = event("child-1", {
    step_id: "m-child",
    parent_id: "a1",
    author: "researcher",
    content: "Checking the router.",
    at_ms: 1,
  });
  const view = createActivityView();
  view.apply([commentary]);
  view.hydrate([
    step("a1", { kind: "agent", title: "researcher", at_ms: 0 }),
    step("m-child", { kind: "message", title: "Message", body: "Checking the router.", parent_id: "a1", at_ms: 1 }),
  ]);

  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  assert.deepEqual(stepTitles(view.el), ["researcher"]);
  assert.equal(view.el.findAll("step").length, 1);
});

test("a child's internal output, report and thinking are never public", () => {
  const view = createActivityView();
  for (const internal of [
    { id: "r1", kind: "report", visibility: "internal", parent_id: "call-1", content: "private thinking" },
    { id: "r2", kind: "report", visibility: "internal", parent_id: "call-1", content: "sub-agent answer" },
    { id: "r3", kind: "status", visibility: "internal", parent_id: "call-1", content: "Ran a tool" },
  ]) assert.equal(view.add(event(internal.id, internal)), false);
  assert.equal(view.observeStep(step("s-nested", { kind: "message", body: "child prose", parent_id: "call-1" })), false);
  assert.equal(view.size, 0);
  assert.equal(view.isEmpty, true);
});

test("a reloaded trace of unfinished work never invents a segment time", () => {
  const work = () => [
    step("a1", { kind: "agent", title: "coder", status: "running", at_ms: 20_000, duration_ms: null }),
    step("s2", { title: "read_file", parent_id: "a1", status: "running", at_ms: 21_000, duration_ms: null }),
  ];

  const view = createActivityView();
  view.hydrate(work(), {});
  const headline = view.el.find("reasoning__headline").textContent;
  assert.doesNotMatch(headline, /Worked for/);
  assert.match(headline, /1 step/);

  // Reading the same stored trace again (a second reload) changes nothing.
  view.hydrate(work(), {});
  assert.equal(view.el.find("reasoning__headline").textContent, headline);
});

test("a stored message shows the turn duration it recorded", () => {
  const view = createActivityView();
  view.hydrate([step("s1", { at_ms: 0, duration_ms: 10_000 })], { durationMs: 90_000 });

  assert.equal(view.el.find("reasoning__headline").textContent, "Worked for 1m 30s");
});

test("a live message before any work leaves no empty segment behind", () => {
  const view = createActivityView();
  view.start(80_000);
  view.upsert(step("m1", { kind: "message", body: "Stage two.", at_ms: 80_000 }));
  view.upsert(step("a1", { kind: "agent", title: "coder", status: "done", at_ms: 85_000, duration_ms: 5_000 }));

  const panels = view.el.findAll("reasoning");
  assert.equal(panels.length, 1, "the empty segment is not drawn");
  view.finish(90_000);
  // The segment began at the message (80 s) and the turn ended at 90 s.
  assert.equal(panels[0].find("reasoning__headline").textContent, "Worked for 10s");
});

test("live and reloaded history agree on a narration-first segment", () => {
  const realNow = Date.now;
  let now = 0;
  Date.now = () => now;
  try {
    const live = createActivityView();
    live.start(0);
    now = 60_000;
    live.upsert(step("m1", { kind: "message", body: "On it.", at_ms: 60_000 }));
    live.upsert(step("a1", { kind: "agent", title: "coder", status: "done", at_ms: 60_000, duration_ms: 60_000 }));
    now = 120_000;
    live.finish(120_000);
    const liveHeadline = live.el.find("reasoning__headline").textContent;
    assert.equal(liveHeadline, "Worked for 1m");

    const stored = createActivityView();
    stored.hydrate([
      step("m1", { kind: "message", body: "On it.", at_ms: 60_000, duration_ms: 0 }),
      step("a1", { kind: "agent", title: "coder", status: "done", at_ms: 60_000, duration_ms: 60_000 }),
    ], { durationMs: 120_000 });
    assert.equal(stored.el.find("reasoning__headline").textContent, liveHeadline);
  } finally {
    Date.now = realNow;
  }
});

test("live and reloaded history agree on every narrated segment", () => {
  const realNow = Date.now;
  let now = 0;
  Date.now = () => now;
  try {
    const live = createActivityView();
    live.start(0);
    live.upsert(step("s1", { title: "first", at_ms: 0, duration_ms: 10_000 }));
    now = 10_000;
    live.upsert(step("m1", { kind: "message", body: "Middle.", at_ms: 10_000 }));
    now = 30_000;
    live.upsert(step("s2", { title: "second", at_ms: 30_000, duration_ms: 20_000 }));
    now = 120_000;
    live.finish(120_000);
    const liveHeadlines = live.el.findAll("reasoning__headline").map((node) => node.textContent);
    assert.deepEqual(liveHeadlines, ["Worked for 10s", "Worked for 1m 50s"]);

    const stored = createActivityView();
    stored.hydrate([
      step("s1", { title: "first", at_ms: 0, duration_ms: 10_000 }),
      step("m1", { kind: "message", body: "Middle.", at_ms: 10_000, duration_ms: 0 }),
      step("s2", { title: "second", at_ms: 30_000, duration_ms: 20_000 }),
    ], { durationMs: 120_000 });
    const storedHeadlines = stored.el.findAll("reasoning__headline").map((node) => node.textContent);
    assert.deepEqual(storedHeadlines, liveHeadlines);
  } finally {
    Date.now = realNow;
  }
});

test("overlapping calls are timed by their segment, not by a sum", () => {
  const view = createActivityView();
  view.hydrate([
    step("t1", { title: "first", at_ms: 0, duration_ms: 30_000 }),
    step("t2", { title: "second", at_ms: 10_000, duration_ms: 30_000 }),
    step("m1", { kind: "message", body: "Done.", at_ms: 40_000, duration_ms: 0 }),
  ], { durationMs: 40_000 });

  // Both calls ran from 0 to 40 s: a sum would claim 60 s.
  assert.equal(view.el.find("reasoning__headline").textContent, "Worked for 40s");
});

test("a step that began after the page's elapsed shows no false zero before attached", () => {
  const view = createActivityView();
  view.start(10_000);
  view.upsert(step("a1", { kind: "agent", title: "coder", status: "running", at_ms: 40_000, duration_ms: null }));
  assert.equal(view.el.find("step__foldTime").textContent, "");

  view.setElapsed(90_000);
  assert.equal(view.el.find("step__foldTime").textContent, "50s");
  view.finish(95_000);
});

test("a legacy message without a real at_ms never zeroes the segment before it", () => {
  const view = createActivityView();
  view.hydrate([
    step("t", { title: "read_file", at_ms: 5_000, duration_ms: 1_000 }),
    step("m", { kind: "message", body: "Done.", at_ms: 0, duration_ms: 0 }),
  ], { durationMs: null });

  // No trustworthy total and no trustworthy boundary: the time stays unknown.
  assert.equal(view.el.find("reasoning__headline").textContent, "1 step");
  assert.equal(view.el.find("reasoning__timer").textContent, "");
});

test("a stopped segment keeps the interval it ran and its failure", () => {
  const view = createActivityView();
  view.hydrate([
    step("t", { title: "bash_tool", status: "error", tone: "error", at_ms: 0, duration_ms: 1_000 }),
    step("m", { kind: "message", body: "Stopped.", at_ms: 1_000, duration_ms: 0 }),
  ], { durationMs: 2_000 });

  assert.equal(view.el.find("reasoning__headline").textContent, "Worked for 1.0s");
  const row = view.el.find("step");
  assert.equal(row.dataset.status, "error");
  assert.match(row.find("step__foldLabel").textContent, /^Failed after 1\.0s/);
});
