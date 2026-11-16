/** Pure office geometry and truthful metadata; real shaders are browser-tested. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createScene } from "../web_chat/js/grid/scene.js";
import { createOfficeModel, officeObjectLabel, officeState, officeToolKind, officeRelations, MAX_OFFICE_LABELS } from "../web_chat/js/grid/office_model.js";
import { createOfficeView } from "../web_chat/js/grid/office.js";
import { pickNode } from "../web_chat/js/grid/space_math.js";

function snapshot() {
  return {
    project: { id: "p", title: "Office" },
    tasks: { available: true, items: [
      { id: "a", title: "Ready", state: "ready", status: "open", priority: 1, created_at: "2026-01-01", depends_on: [] },
      { id: "b", title: "Blocked", state: "blocked", status: "blocked", priority: 0, created_at: "2026-01-02", depends_on: ["a", "missing"] },
      { id: "c", title: "Closed", state: "closed", status: "closed", created_at: "2026-01-03", close_reason: "Tests passed" },
      { id: "d", title: "Not closed", state: "ready", close_reason: "Old reason" },
    ], total: 4 },
    chats: { available: true, items: [
      { chat_id: "a", title: "Working desk", active: true, awaiting_approval: false, task_links: [{ task_id: "a", relation: "turn" }] },
      { chat_id: "b", title: "Approval desk", active: true, awaiting_approval: true, task_links: [{ task_id: "b", relation: "plan" }] },
      { chat_id: "c", title: "Idle desk", active: false },
    ], total: 3 },
    files: { available: true, items: [
      { name: "docs", path: "docs", kind: "dir" },
      { name: "report.md", path: "report.md", kind: "file" },
      { name: "image.png", path: "image.png", kind: "file" },
    ], total: 3 },
    files_url: "/files?context=p&path=project",
  };
}
function detail(agent = "main") {
  return {
    messages: { items: [{ id: "m", role: "assistant", preview: "Safe result preview", kind: "final", turn_id: "t" }] },
    executions: { items: [{ id: "e", turn_id: "t", agent, status: "running", start_time: 1700000000 }] },
    operations: { items: [
      { id: "read", execution_id: "e", kind: "tool", title: "Read docs", status: "done", files: ["docs/a.md"], summary: "PRIVATE" },
      { id: "delegate", execution_id: "e", kind: "agent", title: "Scout", status: "done" },
      { id: "child", execution_id: "e", parent_id: "delegate", kind: "tool", title: "Check", status: "error" },
    ] },
  };
}
const places = (model) => Object.fromEntries(model.nodes.map((node) => [node.id, node.position]));
const build = (map = snapshot(), options = {}) => createOfficeModel(createScene(map, options), { map, ...options });

test("empty/unavailable offices still frame a finite recognisable cutaway", () => {
  for (const map of [{}, { project: { id: "empty" }, tasks: { available: false }, files: { available: false }, chats: { items: [] } }]) {
    const model = build(map);
    assert.equal(model.zones.length, 4);
    assert.ok(model.parts.some((part) => part.role === "floor"));
    assert.ok(model.parts.some((part) => part.role === "back-wall"));
    assert.ok([...model.bounds.min, ...model.bounds.max].every(Number.isFinite));
    assert.match(model.detail.hint, /Select a desk/);
    assert.equal(model.counts.operations, 0);
    assert.equal(model.counts.reports, 0);
    if (map.files) assert.match(model.zones.find((zone) => zone.id === "zone:files").note, /unavailable/);
  }
});

test("furniture is recognisable, a worker is one chat not an invented agent", () => {
  const model = build();
  assert.equal(model.parts.filter((part) => part.role === "worker-head").length, 3);
  assert.equal(model.parts.filter((part) => part.role === "desk-top").length, 3);
  assert.equal(model.parts.filter((part) => part.role === "task-ticket").length, 4);
  assert.equal(model.parts.filter((part) => part.role === "approval-barrier").length, 1);
  assert.equal(model.parts.filter((part) => part.role === "active-screen-bar").length, 3);
  assert.ok(model.nodes.filter((node) => node.kind === "chat").every((node) => node.agent === null));
  assert.equal(model.counts.executions, 0);
  assert.equal(model.counts.operations, 0);
});

test("layout is deterministic under source order, status and selected detail changes", () => {
  const map = snapshot();
  const first = build(map);
  const reversed = structuredClone(map);
  reversed.tasks.items.reverse(); reversed.chats.items.reverse(); reversed.files.items.reverse();
  assert.deepEqual(places(build(reversed)), places(first));
  map.tasks.items[0].state = "closed";
  map.chats.items[0].active = false;
  const after = build(map, { chatDetails: { a: detail() }, selectedId: "chat:a", detailChatIds: ["a"] });
  for (const [id, position] of Object.entries(places(first))) assert.deepEqual(places(after)[id], position, id);
});

test("only the selected desk displays already-loaded real detail", () => {
  const map = snapshot();
  const scene = createScene(map, { chatDetails: { a: detail("Alpha"), b: detail("Beta") } });
  const none = createOfficeModel(scene, { map, detailChatIds: ["a", "b"] });
  assert.equal(none.counts.operations, 0);
  const a = createOfficeModel(scene, { map, selectedId: "chat:a", detailChatIds: ["a", "b"] });
  assert.equal(a.counts.operations, 3);
  assert.equal(a.counts.executions, 2, "real run plus explicit delegated execution");
  assert.equal(a.detail.runningAgent, "Alpha");
  assert.equal(a.nodes.find((node) => node.id === "chat:b").agent, null);
  assert.ok(a.nodes.filter((node) => ["message", "operation", "execution"].includes(node.kind)).every((node) => node.dimensions.provenance.chat === "a"));
  const child = createOfficeModel(scene, { map, selectedId: "operation:b:child", detailChatIds: ["a", "b"] });
  assert.equal(child.detail.chatId, "b");
  assert.equal(child.counts.operations, 3);
  assert.equal(child.nodes.find((node) => node.id === "chat:a").agent, null);
  assert.doesNotMatch(JSON.stringify(a), /PRIVATE/);
});

test("unloaded, loading, error and loaded-empty detail are different words", () => {
  const map = snapshot(); const scene = createScene(map);
  for (const [status, expression] of [["idle", /not been loaded/], ["loading", /Loading/], ["error", /could not be read/], ["ready", /no recorded tool calls/]]) {
    const model = createOfficeModel(scene, { map, selectedId: "chat:a", detailStates: { a: { status } } });
    assert.match(model.detail.hint, expression);
    assert.equal(model.counts.operations, 0);
  }
});

test("approval is waiting, idle is not busy, and all states have words and marks", () => {
  const model = build();
  assert.match(officeObjectLabel(model.nodes.find((node) => node.id === "chat:b")), /◌.*Waiting for approval/);
  assert.match(officeObjectLabel(model.nodes.find((node) => node.id === "chat:c")), /○.*No active turn/);
  for (const raw of ["blocked", "failed", "timeout", "running", "closed", "planned", "cancelled", "unfamiliar", null]) {
    const state = officeState({ kind: "task", state: raw });
    assert.ok(state.icon && state.label && state.tone);
    assert.equal(state.color.length, 3);
  }
  assert.match(officeObjectLabel(model.nodes.find((node) => node.id === "task:b")), /P0.*Blocked/);
});

test("only closed tasks with explicit close reasons become reports; files never do", () => {
  const model = build();
  assert.equal(model.reports.length, 1);
  assert.equal(model.reports[0].id, "task:c");
  assert.equal(model.reports[0].caption, "Tests passed");
  assert.match(model.reports[0].status.label, /not acceptance/);
  assert.ok(model.nodes.filter((node) => node.kind === "file").every((node) => /unconfirmed|Folder/.test(node.status.label)));
  const reportTarget = model.pickTargets.find((node) => node.pickKey === "report:task:c");
  assert.equal(reportTarget.id, "task:c", "report selection goes to the canonical task inspector");
});

test("routes only join canonical visible endpoints and preserve relation semantics", () => {
  const model = build(snapshot(), { chatDetails: { a: detail() }, selectedId: "chat:a", detailChatIds: ["a"] });
  const ids = new Set(model.nodes.map((node) => node.id));
  assert.ok(model.routes.every((route) => ids.has(route.from) && ids.has(route.to)));
  assert.ok(model.routes.some((route) => route.kind === "depends" && /waits for/.test(route.relation)));
  assert.ok(model.routes.some((route) => route.kind === "reference" && /no read\/write effect/.test(route.relation)));
  assert.ok(model.routes.some((route) => route.kind === "delegates"));
  assert.ok(officeRelations(model, "chat:a").some((word) => /touched/.test(word)));
  assert.deepEqual(officeRelations(model, "missing"), []);
});

test("a ray picks the visible desk/worker and reports by their real scene IDs", () => {
  const model = build();
  const worker = model.pickTargets.find((node) => node.pickKey === "chat:a:worker");
  const hit = pickNode(model.pickTargets, { origin: [worker.position[0], 20, worker.position[2]], direction: [0, -1, 0] }, { padding: 0 });
  assert.equal(hit.id, "chat:a");
  const report = model.reports[0];
  assert.equal(pickNode([report], { origin: [report.position[0], 20, report.position[2]], direction: [0, -1, 0] }).id, "task:c");
});

test("large bounded snapshots stay finite, complete and explicit about truncation", () => {
  const map = snapshot();
  map.tasks.items = Array.from({ length: 300 }, (_, i) => ({ id: `t${i}`, title: `Task ${i}`, state: "ready" }));
  map.chats.items = Array.from({ length: 50 }, (_, i) => ({ chat_id: `c${i}`, title: `Desk ${i}`, active: i % 2 === 0 }));
  map.files.items = Array.from({ length: 100 }, (_, i) => ({ name: `f${i}.md`, path: `f${i}.md`, kind: "file" }));
  map.tasks.total = 1200; map.tasks.truncated = true; map.chats.total = 80; map.files.total = 400;
  const model = build(map);
  assert.equal(model.counts.tasks, 300); assert.equal(model.counts.chats, 50); assert.equal(model.counts.files, 100);
  assert.match(model.summary.snapshot, /tasks 300\/1200.*chats 50\/80.*files 100\/400/);
  assert.ok(model.parts.length < 5000);
  assert.ok(model.parts.every((part) => [...part.position, ...part.size].every(Number.isFinite)));
  assert.equal(MAX_OFFICE_LABELS, 24);
});

test("tool categories never infer an effect, and invalid containers fail readably", () => {
  assert.match(officeToolKind({ item: { kind: "write" } }).label, /effect not implied/);
  assert.equal(officeToolKind({ item: { kind: "agent" } }).label, "Delegation");
  assert.throws(() => createOfficeView(null), /DOM element/);
  assert.throws(() => createOfficeView({ appendChild() {} }), /document/);
});

test("Office sources use no network, assets, innerHTML, continuous frame loop or dependency", () => {
  const model = readFileSync(new URL("../web_chat/js/grid/office_model.js", import.meta.url), "utf8");
  const renderer = readFileSync(new URL("../web_chat/js/grid/office.js", import.meta.url), "utf8");
  assert.doesNotMatch(model + renderer, /innerHTML|fetch\(|XMLHttpRequest|https?:\/\//);
  assert.match(renderer, /createRenderScheduler/);
  assert.match(renderer, /if \(transition.active\) scheduler.schedule\(\)/);
  assert.match(renderer, /scheduler.dispose\(\)/);
  assert.match(renderer, /transition.setReducedMotion/);
  assert.match(renderer, /gl.drawElementsInstanced/);
});
