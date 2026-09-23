import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";
import { createReasoningPanel } from "../web_chat/js/ui/reasoning.js";

installFakeDom();

let clock = 0;
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

const rows = (list) => list.childrenWith("step");
const titles = (list) => rows(list).map((row) => row.find("step__title").textContent);
const topList = (panel) => panel.el.find("reasoning__steps");
const agentList = (row) => row.find("agent__steps");

test("a sub-agent's steps nest in its block, not in the caller's timeline", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("s1", { kind: "reasoning", title: "Thinking", body: "plan" }),
    step("s2", { kind: "agent", title: "coder", body: "All tests pass." }),
    step("s3", { title: "bash_tool", parent_id: "s2" }),
    step("s4", { title: "read_file", parent_id: "s2" }),
    step("s5", { title: "git.diff" }),
  ]);

  const top = topList(panel);
  assert.deepEqual(titles(top), ["Thinking", "coder", "git.diff"]);
  const block = rows(top)[1];
  assert.equal(block.dataset.kind, "agent");
  assert.deepEqual(titles(agentList(block)), ["bash_tool", "read_file"]);
  // The collapsed block still says how much work is inside it.
  assert.match(block.find("step__subtitle").textContent, /2 steps/);
  // The headline counts what the caller did; the delegation is one step.
  assert.match(panel.el.find("reasoning__timer").textContent, /3 steps/);
});

test("the block ends with the sub-agent's report", () => {
  const panel = createReasoningPanel();
  panel.hydrate([step("s1", { kind: "agent", title: "coder", detail: "fix it", body: "Fixed." })]);

  const labels = panel.el.findAll("payload__label").map((label) => label.textContent);
  assert.deepEqual(labels, ["Task", "Report"]);
});

test("the task reads as the words the sub-agent was given", () => {
  const panel = createReasoningPanel();
  panel.hydrate([step("s1", { kind: "agent", title: "coder", detail: '{\n  "input": "Fix **the** test"\n}' })]);

  assert.match(panel.el.find("payload__body--prose").innerHTML, /Fix <strong>the<\/strong> test/);
});

test("a call turns into an agent block in place once its sub-agent starts", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { title: "first" }));
  panel.upsert(step("s2", { title: "Agent", status: "running", duration_ms: null }));
  panel.upsert(step("s3", { title: "last" }));
  panel.upsert(step("s2", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s4", { title: "bash_tool", parent_id: "s2", status: "running", duration_ms: null }));

  const top = topList(panel);
  assert.deepEqual(titles(top), ["first", "coder", "last"]);
  assert.deepEqual(titles(agentList(rows(top)[1])), ["bash_tool"]);
});

test("a running block is open and says what its sub-agent is doing", () => {
  const panel = createReasoningPanel();
  panel.start();
  panel.upsert(step("s1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s2", { title: "bash_tool", parent_id: "s1", status: "running", duration_ms: null }));

  const block = rows(topList(panel))[0];
  assert.ok(block.classList.contains("is-open"));
  assert.equal(block.find("step__subtitle").textContent, "bash_tool");
  assert.equal(panel.el.find("reasoning__headline").textContent, "coder › bash_tool");

  panel.upsert(step("s1", { kind: "agent", title: "coder", body: "done" }));
  assert.ok(!block.classList.contains("is-open"), "a finished block folds away");
  panel.finish(10);
});

test("the reader's choice wins over the block's auto-collapse", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  const block = rows(topList(panel))[0];
  block.find("step__head").click(); // the reader closes it
  block.find("step__head").click(); // and opens it again
  panel.upsert(step("s1", { kind: "agent", title: "coder" }));

  assert.ok(block.classList.contains("is-open"));
});

test("thinking streams into a step inside a block", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s2", { kind: "reasoning", title: "Thinking", parent_id: "s1", status: "running", duration_ms: null }));
  panel.appendReasoning("s2", "checking ");
  panel.appendReasoning("s2", "the tests");

  assert.equal(panel.el.find("step__prose").textContent, "checking the tests");
});

test("a retracted step inside a block is removed from that block", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s2", { kind: "reasoning", title: "Thinking", parent_id: "s1", status: "running", duration_ms: null }));
  panel.remove("s2");

  assert.deepEqual(titles(agentList(rows(topList(panel))[0])), []);
  assert.equal(panel.isEmpty, false);
});

test("traces stored before blocks existed still render flat", () => {
  const panel = createReasoningPanel();
  const legacy = [step("s1", { title: "Agent" }), step("s2", { title: "general-purpose › bash_tool" })];
  for (const entry of legacy) delete entry.parent_id;
  panel.hydrate(legacy);

  assert.deepEqual(titles(topList(panel)), ["Agent", "general-purpose › bash_tool"]);
});
