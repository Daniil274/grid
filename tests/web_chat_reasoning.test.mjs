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

test("a running sub-agent's block stays out of the way until the reader opens it", () => {
  const panel = createReasoningPanel();
  panel.start();
  panel.upsert(step("s1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  panel.upsert(step("s2", { title: "bash_tool", parent_id: "s1", status: "running", duration_ms: null }));

  const block = rows(topList(panel))[0];
  assert.ok(!block.classList.contains("is-open"), "a working sub-agent does not push its block open");
  assert.equal(block.find("step__subtitle").textContent, "bash_tool");
  assert.equal(panel.el.find("reasoning__headline").textContent, "coder › bash_tool");

  // The reader can still open it by hand, and it stays open across updates.
  block.find("step__head").click();
  assert.ok(block.classList.contains("is-open"));
  panel.upsert(step("s1", { kind: "agent", title: "coder", body: "done" }));
  assert.ok(block.classList.contains("is-open"));
  panel.finish(10);
});

test("the reader's opened sub-agent block stays open when the turn ends", () => {
  const panel = createReasoningPanel();
  panel.start();
  panel.upsert(step("s1", { kind: "agent", title: "coder", status: "running", duration_ms: null }));
  const block = rows(topList(panel))[0];
  block.find("step__head").click(); // the reader opens it
  block.find("step__head").click(); // and closes it again
  block.find("step__head").click(); // and opens it once more
  panel.upsert(step("s1", { kind: "agent", title: "coder" }));
  panel.finish(10);

  assert.ok(block.classList.contains("is-open"), "the reader's last choice stands");
});

test("a parallel batch opens while its calls run and folds away once done", () => {
  const panel = createReasoningPanel();
  panel.start();
  panel.upsert(step("p1", { kind: "agent", title: "Parallel calls", tool: "parallel", status: "running", duration_ms: null }));
  panel.upsert(step("p2", { title: "read_file", parent_id: "p1", status: "running", duration_ms: null }));
  panel.upsert(step("p3", { kind: "agent", title: "Agent", parent_id: "p1", status: "running", duration_ms: null }));
  panel.upsert(step("p4", { title: "bash_tool", parent_id: "p3", status: "running", duration_ms: null }));

  const batch = rows(topList(panel))[0];
  assert.ok(batch.classList.contains("is-open"), "a running batch opens to show its calls");
  assert.deepEqual(titles(agentList(batch)), ["read_file", "Agent"]);
  // A sub-agent nested in the batch does not push its own block open.
  assert.ok(!rows(agentList(batch))[1].classList.contains("is-open"));

  panel.upsert(step("p1", { kind: "agent", title: "Parallel calls", tool: "parallel", body: '{"results": []}' }));
  assert.ok(!batch.classList.contains("is-open"), "a finished batch folds away");
  panel.finish(10);
});

test("a parallel batch shows its calls, not the JSON task and report", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("p1", {
      kind: "agent", title: "Parallel calls", tool: "parallel", subtitle: "2 calls: read_file ×2",
      detail: '{"calls": [{"tool": "read_file"}, {"tool": "read_file"}]}',
      body: '{"results": [{"output": "text"}], "wall_seconds": 1.0}',
    }),
    step("p2", { title: "read_file", parent_id: "p1" }),
    step("p3", { title: "read_file", parent_id: "p1" }),
  ]);
  const batch = rows(topList(panel))[0];
  assert.ok(!batch.classList.contains("is-open"), "a stored batch renders collapsed");
  batch.find("step__head").click();

  assert.deepEqual(titles(agentList(batch)), ["read_file", "read_file"]);
  assert.deepEqual(panel.el.findAll("payload__label").map((label) => label.textContent), [], "no Task or Report payloads");
  assert.match(batch.find("step__subtitle").textContent, /2 calls: read_file ×2/);
  assert.ok(!batch.find("step__subtitle").textContent.includes('{"calls"'));
});

test("a failed parallel batch shows its error, still without the JSON task and report", () => {
  const panel = createReasoningPanel();
  panel.start();
  panel.upsert(step("p1", { kind: "agent", title: "Parallel calls", tool: "parallel", status: "running", duration_ms: null }));
  panel.upsert(step("p2", { title: "read_file", parent_id: "p1", status: "running", duration_ms: null }));
  panel.upsert(step("p1", {
    kind: "agent", title: "Parallel calls", tool: "parallel", status: "error", tone: "error",
    subtitle: "2 calls: read_file ×2", detail: '{"calls": [{"tool": "read_file"}, {"tool": "read_file"}]}',
    body: "ValueError: Calls 0 and 1 both write 'notes.md': run them in sequence.",
  }));
  panel.finish(10);

  const batch = rows(topList(panel))[0];
  // What broke is readable even before the batch is opened.
  assert.match(batch.find("step__subtitle").textContent, /ValueError: Calls 0 and 1 both write 'notes\.md'/);
  batch.find("step__head").click();
  assert.deepEqual(
    panel.el.findAll("payload__label").map((label) => label.textContent),
    ["Error"],
    "the batch's error shows; its task and report do not",
  );
  assert.match(batch.find("payload__body").textContent, /run them in sequence/);
  assert.ok(!batch.textContent.includes('{"calls"'), "the batch's JSON envelope never shows");
});

test("a tool row nested in a running batch never opens on its own", () => {
  const panel = createReasoningPanel();
  panel.start();
  panel.upsert(step("p1", { kind: "agent", title: "Parallel calls", tool: "parallel", status: "running", duration_ms: null }));
  panel.upsert(step("p2", { title: "read_file", parent_id: "p1", status: "running", duration_ms: null }));
  panel.upsert(step("p2", { title: "read_file", parent_id: "p1", detail: '{"path": "notes.md"}', body: "content" }));

  const batch = rows(topList(panel))[0];
  const inner = rows(agentList(batch))[0];
  assert.ok(batch.classList.contains("is-open"), "the batch itself opens while it runs");
  assert.ok(!inner.classList.contains("is-open"), "a tool row inside the batch stays closed");
  inner.find("step__head").click(); // the reader can still open it by hand
  assert.ok(inner.classList.contains("is-open"), "manual opening works");
  panel.finish(10);
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

test("a call held for the user offers Allow and Decline until the answer lands", () => {
  const answers = [];
  document.dispatchEvent = (event) => answers.push(event.detail);
  const panel = createReasoningPanel();
  const held = { decision: "review", label: "Waiting for you", title: "file_write · secret: .env", approval_id: "a1", approvals: "user" };
  panel.upsert(step("s1", { title: "file_write", status: "running", duration_ms: null, policy: held }));

  const approval = topList(panel).find("step__approval");
  assert.equal(approval.hidden, false);
  const buttons = approval.querySelectorAll("button");
  assert.deepEqual(buttons.map((button) => button.textContent), ["Allow", "Allow for this turn", "Decline"]);
  buttons[1].click();
  assert.deepEqual(answers, [{ approvalId: "a1", approve: true, remember: true }]);
  assert.ok(buttons.every((button) => button.disabled));

  // The server's next event for the call replaces the badge: the buttons go.
  panel.upsert(step("s1", { title: "file_write", policy: { decision: "allow", label: "Allowed by you", title: "" } }));
  assert.equal(approval.hidden, true);
  assert.equal(approval.querySelectorAll("button").length, 0);
});

test("a call held for the operator shows no buttons to the user", () => {
  const panel = createReasoningPanel();
  const held = { decision: "review", label: "Waiting for the operator", title: "", approval_id: "a2", approvals: "operator" };
  panel.upsert(step("s1", { title: "deploy", status: "running", duration_ms: null, policy: held }));
  const approval = topList(panel).find("step__approval");
  assert.equal(approval.hidden, false);
  assert.equal(approval.querySelectorAll("button").length, 0);
});

const planStep = (tasks, done) => step("p1", {
  kind: "plan",
  title: `Plan · ${done}/${tasks.length} done`,
  plan: {
    tasks,
    total: tasks.length,
    counts: tasks.reduce((counts, task) => ({ ...counts, [task.state]: (counts[task.state] ?? 0) + 1 }), {}),
  },
});

test("the plan shows as its own card, kept up to date, outside the timeline", () => {
  const panel = createReasoningPanel();
  panel.upsert(step("s1", { title: "beads_plan" }));
  panel.upsert(planStep([
    { id: "p-1", title: "Research", state: "ready", waits_for: [] },
    { id: "p-2", title: "Compare", state: "waiting", waits_for: ["research"] },
  ], 0));
  panel.upsert(planStep([
    { id: "p-1", title: "Research", state: "closed", waits_for: [] },
    { id: "p-2", title: "Compare", state: "in_progress", waits_for: [] },
  ], 1));

  assert.deepEqual(titles(topList(panel)), ["beads_plan"]);
  const card = panel.planEl;
  assert.equal(card.hidden, false);
  assert.equal(card.find("planCard__heading").textContent, "Plan · 1/2 done");
  assert.equal(card.find("planCard__summary").textContent, "1 in progress");
  const tasks = card.findAll("planCard__task");
  assert.deepEqual(tasks.map((task) => task.dataset.state), ["closed", "in_progress"]);
});

test("a stored plan is restored from the trace with the rest of the turn", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("s1", { title: "beads_plan" }),
    planStep([{ id: "p-1", title: "Research", state: "waiting", waits_for: ["setup"] }], 0),
  ]);
  assert.deepEqual(titles(topList(panel)), ["beads_plan"]);
  assert.match(panel.planEl.find("planCard__meta").textContent, /waits for setup/);
});

test("a lazy step fetches its payloads the first time it is opened", async () => {
  const panel = createReasoningPanel();
  const asked = [];
  const loadStep = async (id) => {
    asked.push(id);
    return step(id, { title: "read_file", detail: '{"path": "a.md"}', body: "file text" });
  };
  panel.hydrate([step("s1", { title: "read_file", detail: "", body: "", lazy: true })], { loadStep });
  const [call] = rows(topList(panel));
  assert.ok(call.classList.contains("has-detail"));
  assert.deepEqual(asked, []); // nothing fetched while the step is closed

  call.find("step__head").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(asked, ["s1"]);
  assert.deepEqual(panel.el.findAll("payload__label").map((label) => label.textContent), ["Input", "Result"]);

  call.find("step__head").click();
  call.find("step__head").click();
  assert.deepEqual(asked, ["s1"]); // fetched once
});

test("a stored answer's plan starts folded, and a saved plan never doubles it", () => {
  const panel = createReasoningPanel();
  const task = (id, turn) => ({ id, key: "", title: id, state: "ready", depends_on: [], turn });
  panel.hydrate([{ id: "p", kind: "plan", plan: { tasks: [task("a", true), task("b", false)], total: 2 } }]);
  assert.equal(panel.hasPlan, true);
  assert.ok(panel.planEl.classList.contains("is-collapsed"));

  panel.setPlanCollapsed(false);
  assert.equal(panel.planEl.classList.contains("is-collapsed"), false);
  // The answer's own plan step stays: the saved plan is not drawn over it.
  panel.applyStoredPlan({ tasks: [task("z", false)], total: 1 });
  assert.equal(panel.planEl.findAll("planCard__task").length, 1);
  assert.equal(panel.planEl.find("planCard__title").textContent, "a");
});

test("an answer without a plan step shows the saved plan open, as the whole plan", () => {
  const panel = createReasoningPanel();
  panel.hydrate([]);
  panel.applyStoredPlan({ tasks: [{ id: "a", title: "a", state: "ready", turn: true }, { id: "b", title: "b", state: "ready" }], total: 2 });
  assert.equal(panel.planEl.classList.contains("is-collapsed"), false);
  assert.equal(panel.planEl.findAll("planCard__task").length, 2);
});

test("a tracker id in a step's refs deep-links into the tracker", () => {
  const panel = createReasoningPanel();
  panel.hydrate([
    step("s1", {
      title: "beads_show",
      refs: [
        { kind: "task", label: "workspace-xiy", detail: "workspace-xiy" },
        { kind: "file", label: "a.py", detail: "/x/a.py" },
      ],
    }),
  ]);

  const chips = topList(panel).findAll("refChip");
  assert.equal(chips[0].getAttribute("href"), "/tasks?task=workspace-xiy");
  assert.match(chips[0].getAttribute("title"), /Open workspace-xiy in the tracker/);
  // A file ref is not a link: only a url or a tracker id carries an href.
  assert.equal(chips[1].tagName, "SPAN");
  assert.equal(chips[1].getAttribute("href"), null);
});
