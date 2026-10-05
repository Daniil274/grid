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
  panel.el.find("step__head").click();

  const labels = panel.el.findAll("payload__label").map((label) => label.textContent);
  assert.deepEqual(labels, ["Task", "Report"]);
});

test("the task reads as the words the sub-agent was given", () => {
  const panel = createReasoningPanel();
  panel.hydrate([step("s1", { kind: "agent", title: "coder", detail: '{\n  "input": "Fix **the** test"\n}' })]);
  panel.el.find("step__head").click();

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

test("a step shows the tokens its own model responses spent", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("s1", { kind: "reasoning", title: "Thinking", tokens_in: 1200, tokens_out: 300 }),
    step("s2", { title: "read_file", tokens_in: 100, tokens_out: 0 }),
  ]);

  const [thinking, read] = rows(topList(panel));
  assert.equal(thinking.find("step__usage").textContent, "↑1.2k ↓300");
  // An output-only step hides the arrow it has nothing for.
  assert.equal(read.find("step__usage").textContent, "↑100");
});

test("a step that reported no tokens shows no usage", () => {
  const panel = createReasoningPanel();
  panel.hydrate([step("s1", { title: "read_file" })]);

  assert.equal(rows(topList(panel))[0].find("step__usage").textContent, "");
});

test("the headline totals the tokens the turn spent", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("s1", { kind: "reasoning", title: "Thinking", tokens_in: 1000, tokens_out: 200 }),
    step("s2", { kind: "message", title: "Answer", tokens_in: 500, tokens_out: 100 }),
  ]);

  assert.match(panel.el.find("reasoning__timer").textContent, /↑1\.5k ↓300/);
});

test("the server's own turn total is used when the steps didn't report one", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { kind: "reasoning", title: "Thinking", status: "running", duration_ms: null }));
  panel.finish(900, true, { tokensIn: 4200, tokensOut: 800 });

  assert.match(panel.el.find("reasoning__timer").textContent, /↑4\.2k ↓800/);
});

test("a closed step builds its thinking and payloads only when opened", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("s1", { kind: "reasoning", title: "Thinking", body: "**long** plan" }),
    step("s2", { title: "read_file", detail: '{"path": "a.md"}', body: "file text" }),
  ]);
  const [thinking, call] = rows(topList(panel));
  // Stored chats keep hundreds of these closed: nothing heavy is rendered yet.
  assert.equal(thinking.find("step__prose").innerHTML, "");
  assert.equal(panel.el.findAll("payload__label").length, 0);
  assert.ok(call.classList.contains("has-detail"));

  thinking.find("step__head").click();
  call.find("step__head").click();
  assert.match(thinking.find("step__prose").innerHTML, /<strong>long<\/strong> plan/);
  assert.deepEqual(panel.el.findAll("payload__label").map((label) => label.textContent), ["Input", "Result"]);
});

test("typed tool payloads render JSON safely and preserve empty typed results", () => {
  const panel = createReasoningPanel();
  panel.hydrate([step("s1", {
    input_payload: { version: 1, parts: [{ kind: "json", data: { ok: true, n: 0 } }], raw_text: '{"ok":true,"n":0}' },
    result_payload: { version: 1, parts: [], raw_text: "" },
  })]);
  const row = rows(topList(panel))[0];
  row.find("step__head").click();
  assert.deepEqual(panel.el.findAll("payload__label").map((label) => label.textContent), ["Input", "Result"]);
  assert.match(row.find("payload__typed").textContent, /"ok": true/);
  assert.equal(row.find("payload__typed").innerHTML.includes("<script>"), false);
});

test("typed markdown is rendered through the markdown renderer and truncation is visible", () => {
  const panel = createReasoningPanel();
  panel.hydrate([step("s1", { result_payload: { version: 1, parts: [{ kind: "markdown", data: "**safe** <script>bad</script>" }], raw_text: "raw", truncated: true, original_size: 40000 } })]);
  const row = rows(topList(panel))[0];
  row.find("step__head").click();
  assert.match(row.find("payload__body--prose").innerHTML, /<strong>safe<\/strong>/);
  assert.equal(row.find("payload__notice").textContent, "Truncated · original 40000 bytes");
});
