/**
 * Compact agent activity: what the agent said between its actions, and a short
 * status of what it did, read from the typed public events
 * (web_chat/js/ui/activity.js) and
 * drawn with the answer (./message.js).
 *
 * What is asserted is what a reader sees: a sentence said before a tool call
 * must still be on screen after the answer, after the final output and after a
 * reconnect; the compact status names the latest step without repeating every
 * one; and nothing internal is ever drawn.
 *
 * Run with the rest of the UI tests: `node --test tests/*.test.mjs`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();
// The transcript wires page-level listeners the fake DOM does not model.
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

const transcript = () =>
  createTranscript({
    container: document.createElement("div"),
    emptyState: document.createElement("div"),
    jumpButton: document.createElement("button"),
  });

test("only public messages and statuses reach the activity view", () => {
  const view = createActivityView();

  // Thinking, a sub-agent's answer and a tool's output are all reports: internal.
  assert.equal(view.add(event("e1", { kind: "report", visibility: "internal", content: "let me think", step_id: "s1" })), false);
  assert.equal(view.add(event("e2", { kind: "status", visibility: "internal", content: "Read secrets.env", step_id: "s2" })), false);
  // A damaged entry is not a communication either.
  assert.equal(view.add({ kind: "message", visibility: "public" }), false);
  assert.equal(isPublicCommunication(null), false);

  assert.equal(view.size, 0);
  assert.equal(view.statusCount, 0);
  assert.equal(view.el.hidden, true);
});

test("narration is drawn as prose under its author, and the answer is not drawn twice", () => {
  const view = createActivityView();
  view.apply([
    event("e1", { step_id: "s2", author: "coder", content: "Reading **the** diff." }),
    event("e2", { kind: "status", step_id: "s3", content: "Ran the tests" }),
    // The answer itself: no step, and already the message body.
    event("e3", { author: "coder", content: "All **green**." }),
  ]);

  assert.equal(view.size, 1);
  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  assert.match(view.el.find("msg__activityText").innerHTML, /<strong>the<\/strong> diff/);
  assert.equal(view.el.find("msg__activityAuthor").textContent, "coder");
  assert.equal(view.statusText, "Ran the tests");
  assert.equal(view.el.hidden, false);
  // The answer is the body of the message, never a second prose block.
  assert.equal(view.el.findAll("msg__activityText").length, 1);
});

test("the activity view escapes what the agent wrote", () => {
  const view = createActivityView();
  view.add(event("e1", { step_id: "s1", content: "<script>alert(1)</script>" }));
  const html = view.el.find("msg__activityText").innerHTML;
  assert.equal(html.includes("<script"), false);
  assert.match(html, /&lt;script&gt;/);
});

test("a message said before a tool call survives the answer, the final output and a reconnect", () => {
  const first = event("e1", { step_id: "s2", author: "coder", content: "Let me check the diff." });
  const status = event("e2", { kind: "status", step_id: "s3", content: "Read app.py" });
  const final = event("e3", { author: "coder", content: "Done." });
  const stored = [first, status, final];

  const message = createMessage({ role: "assistant", content: "Done.", author: "coder", events: stored });
  assert.equal(message.activity.size, 1);
  assert.match(message.el.find("msg__activityText").innerHTML, /check the diff/);

  // The final output replaces the streamed draft; it must not take the narration with it.
  message.setText("Done in 2s.");
  assert.match(message.el.find("msg__activityText").innerHTML, /check the diff/);
  message.setWaiting(false);

  // The same turn arriving again as live step frames adds nothing new.
  message.observeStep(step("s2", { kind: "message", body: "Let me check the diff." }));
  // A reconcile after the turn replaces ids and events; the block is the same one.
  message.adopt({ id: "m2", versions: null, checkpoint: null, events: stored });
  assert.equal(message.el.findAll("msg__activityMessage").length, 1);
  assert.match(message.el.find("msg__activityText").innerHTML, /check the diff/);
  assert.equal(message.el.find("msg__activityAuthor").textContent, "coder");
});

test("live steps and stored events share one block per thing the agent said", () => {
  const view = createActivityView();
  assert.equal(view.observeStep(step("s2", { kind: "message", body: "Reading it now." })), true);
  // The same step is re-sent when it completes: still one block.
  assert.equal(view.observeStep(step("s2", { kind: "message", body: "Reading it now.", status: "done" })), true);
  // Routing learnt the agent's name: the stored twin names the same block.
  view.apply([event("ev-1", { step_id: "s2", author: "coder", content: "Reading it now." })]);

  assert.equal(view.size, 1);
  assert.equal(view.el.findAll("msg__activityMessage").length, 1);
  assert.equal(view.el.find("msg__activityAuthor").textContent, "coder");
});

test("narration without an author takes the agent the turn was routed to", () => {
  const message = createMessage({ role: "assistant", author: "Routing…" });
  message.observeStep(step("s2", { kind: "message", body: "On it." }));
  // Routing named the agent after the narration started.
  message.setAuthor("coder");
  assert.equal(message.el.find("msg__activityAuthor").textContent, "coder");
  assert.equal(message.el.find("msg__activityAuthor").hidden, false);
});

test("a reload's statuses match the live ones and are drawn once", () => {
  const live = createActivityView();
  live.observeStep(step("s1", { title: "Read config.yaml" }));
  live.observeStep(step("s2", { title: "Ran tests", status: "error" }));
  const settled = [live.statusText, live.statusCount];
  assert.deepEqual(settled, ["Ran tests", 2]);

  // History replays the same work as typed status events (the server fills the
  // statuses its stored turn never kept in from the trace).
  const replayed = createActivityView();
  replayed.apply([
    event("e1", { kind: "status", step_id: "s1", content: "Read config.yaml" }),
    event("e2", { kind: "status", step_id: "s2", content: "Ran tests", status: "error" }),
  ]);
  assert.deepEqual([replayed.statusText, replayed.statusCount], settled);
  assert.equal(replayed.el.find("msg__activityStatus").dataset.status, "error");

  // The turn then reconnects and its step frames arrive again: same rows.
  replayed.observeStep(step("s1", { title: "Read config.yaml" }));
  replayed.observeStep(step("s2", { title: "Ran tests", status: "error" }));
  assert.deepEqual([replayed.statusText, replayed.statusCount], settled);
});

test("the compact status keeps the latest step and counts the work", () => {
  const view = createActivityView();
  view.observeStep(step("s1", { title: "Read config.yaml" }));
  view.observeStep(step("s2", { title: "Ran tests", subtitle: "pytest -q" }));

  assert.equal(view.statusText, "Ran tests · pytest -q");
  assert.equal(view.statusCount, 2);
  assert.equal(view.el.find("msg__activityCount").textContent, "2 steps");

  // The step settles with an error: the same row, now saying so.
  view.observeStep(step("s2", { title: "Ran tests", subtitle: "pytest -q", status: "error" }));
  assert.equal(view.statusCount, 2);
  assert.equal(view.el.find("msg__activityStatus").dataset.status, "error");
});

test("one step has no count beside it", () => {
  const view = createActivityView();
  view.observeStep(step("s1", { title: "Read config.yaml" }));
  assert.equal(view.el.find("msg__activityCount").textContent, "");
});

test("what a sub-agent does stays an internal report, never a message of the turn", () => {
  assert.equal(eventFromStep(step("s1", { kind: "message", body: "I will fix it.", parent_id: "a1" })), null);
  assert.equal(eventFromStep(step("s2", { kind: "tool", title: "bash", parent_id: "a1" })), null);
  assert.equal(eventFromStep(step("s3", { kind: "reasoning", body: "hmm" })), null);
  assert.equal(eventFromStep(step("s4", { kind: "plan", title: "Plan" })).kind, "status");
  assert.equal(eventFromStep(step("s5", { kind: "error", title: "Bad tool", parent_id: "a1" })), null);
});

test("a public message is not a second row in the reasoning panel", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { kind: "message", body: "Let me look.", title: "Message" }));
  assert.equal(panel.isEmpty, true);
  // A sub-agent's message is part of its report and stays inside its block.
  panel.upsert(step("a1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s2", { kind: "message", body: "Checking.", title: "Message", parent_id: "a1" }));
  assert.equal(panel.isEmpty, false);
});

test("a stored conversation draws its activity from the message events", () => {
  const t = transcript();
  t.render([
    {
      id: "m2",
      role: "assistant",
      content: "Done.",
      timestamp: 1,
      events: [
        event("e1", { step_id: "s2", author: "coder", content: "Let me check the diff." }),
        event("e2", { kind: "status", step_id: "s3", content: "Ran the tests" }),
        event("e3", { author: "coder", content: "Done." }),
      ],
    },
  ]);

  assert.match(t.lastAnswer().el.find("msg__activityText").innerHTML, /check the diff/);
  assert.equal(t.lastAnswer().el.find("msg__activityLabel").textContent, "Ran the tests");

  // A reconnect re-reads the same chat: adopt keeps the panel and the blocks.
  assert.equal(t.adopt([{ id: "m2", role: "assistant", content: "Done.", events: [] }]), true);
  // The same events applied twice do not double the narration.
  t.lastAnswer().activity.apply([event("e1", { step_id: "s2", author: "coder", content: "Let me check the diff." })]);
  assert.equal(t.lastAnswer().el.findAll("msg__activityMessage").length, 1);
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

test("a real child's explicit commentary is drawn once with its author, run and parent", () => {
  const view = createActivityView();
  // What web_chat/observer.py publishes for a nested agent's commentary: a
  // public message with the child's own author and the parent block it ran in.
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

  // Replay is the same stored event under the same run and step id: no duplicate.
  view.apply([{ ...live }]);
  assert.equal(view.size, 1);

  // A grandchild's commentary has its own author and parent but the same run.
  const grandchild = event("child-2", {
    step_id: "s-child-2",
    author: "coder",
    author_key: "coder",
    parent_id: "call-2",
    turn_id: "run-1",
    content: "I am the nested coder.",
  });
  view.add(grandchild);
  const authors = view.el.findAll("msg__activityAuthor").map((node) => node.textContent);
  assert.deepEqual(authors, ["researcher", "coder"]);
  assert.equal(view.size, 2);
});

test("a child's internal output, report and thinking are never drawn", () => {
  const view = createActivityView();
  // The same child block, but these are internal work products of the run.
  for (const internal of [
    { id: "r1", kind: "report", visibility: "internal", parent_id: "call-1", content: "private thinking" },
    { id: "r2", kind: "report", visibility: "internal", parent_id: "call-1", content: "sub-agent answer" },
    { id: "r3", kind: "status", visibility: "internal", parent_id: "call-1", content: "Ran a tool" },
  ]) {
    assert.equal(view.add(event(internal.id, internal)), false);
  }
  // A nested trace row stays internal even when it is shaped like narration.
  assert.equal(view.observeStep(step("s-nested", { kind: "message", body: "child prose", parent_id: "call-1" })), false);
  assert.equal(view.size, 0);
  assert.equal(view.statusCount, 0);
});
