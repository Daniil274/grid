/**
 * The inspector beside the map (web_chat/js/grid/inspector.js): a selected
 * card, what is really joined to it, and everything the panel says.
 *
 * The promises kept here: a click selects a card without taking the reader
 * away (the panel carries the plain "Open" link), the selection changes weight
 * and never geometry, and a chat's panel is the server's own bounded detail -
 *
 *   Chat -> Messages / Executions -> Operations, and an `agent` operation
 *   opens the child execution that owns its own steps.
 *
 * Every level is explicit: a message is the server's sanitized preview, an
 * execution says its agent/status/time and only a *boolean* error, an
 * operation says its kind and status, and no raw prompt, body, payload or
 * chain of thought ever reaches the panel. The project's space is still read
 * one folder and one file at a time, through the endpoints that already exist,
 * with an honest note for a binary file, a refusal or no way in at all.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const {
  Inspector,
  PREVIEW_LINES,
  applySelection,
  focusKeys,
  normalizeSelection,
  selectionKey,
} = await import("../web_chat/js/grid/inspector.js");
const { layoutMap } = await import("../web_chat/js/grid/layout.js");
const { createExplorer, safePath, treeUrl, textUrl } = await import("../web_chat/js/grid/explorer.js");
const { renderMap } = await import("../web_chat/js/grid/render.js");
const { GridPage } = await import("../web_chat/js/grid/page.js");

const el = (tag) => document.createElement(tag);

/** A project as GET /api/projects/{id}/map answers for it. */
function sampleMap(overrides = {}) {
  return {
    project: { id: "p1", title: "Grid" },
    path: [{ id: "p1", title: "Grid" }],
    tasks: {
      available: true,
      items: [
        {
          id: "bd-1", title: "Alpha", state: "ready", status: "open", priority: 0, type: "feature",
          assignee: null, created_at: "2026-10-01T09:00:00Z", updated_at: "2026-10-02T09:00:00Z", depends_on: [],
        },
        {
          id: "bd-2", title: "Beta", state: "in_progress", status: "in_progress", priority: 1, type: "task",
          assignee: "me", created_at: "2026-10-02T09:00:00Z", updated_at: "2026-10-03T09:00:00Z", depends_on: ["bd-1"],
        },
        {
          id: "bd-3", title: "Gamma", state: "blocked", status: "blocked", priority: 2, type: "bug",
          assignee: null, created_at: "2026-10-03T09:00:00Z", updated_at: "2026-10-04T09:00:00Z", depends_on: ["bd-1"],
        },
      ],
      total: 3,
      counts: { ready: 1, in_progress: 1, blocked: 1 },
    },
    chats: {
      available: true,
      items: [
        {
          chat_id: "c1",
          title: "Chat one",
          active: true,
          awaiting_approval: false,
          updated_at: "2026-10-04T10:00:00Z",
          task_links: [{ task_id: "bd-1", relation: "turn" }],
        },
        {
          chat_id: "c2",
          title: "Chat two",
          active: false,
          awaiting_approval: true,
          updated_at: "2026-10-04T11:00:00Z",
          task_links: [{ task_id: "bd-2", relation: "plan" }],
        },
      ],
      total: 2,
    },
    files: {
      available: true,
      items: [
        { name: "docs", path: "docs", kind: "dir" },
        { name: "README.md", path: "README.md", kind: "file" },
      ],
      total: 2,
    },
    files_url: "/files?context=p1&path=project",
    server: { version: "1.0.0", commit: "abcdef0", source: "git" },
    ...overrides,
  };
}

/**
 * One chat's detail, exactly as GET /api/projects/{id}/map/chats/{chat}
 * answers: bounded, sanitized, and joined only by explicit ids. The SECRET
 * fields are what a real answer never carries - and what the panel must never
 * print even if it does.
 */
function sampleDetail() {
  return {
    chat: { id: "c1", title: "Chat one", href: "/?conversation=c1" },
    messages: {
      items: [
        {
          id: "m1", role: "user", time: "2026-10-04T09:59:00Z", kind: "prompt",
          preview: "Read the map, please", has_image: false,
          prompt: "SECRET PROMPT", content: "SECRET CONTENT",
        },
        {
          id: "m2", role: "assistant", time: "2026-10-04T10:00:00Z", kind: "final",
          preview: "Here is the map", has_image: true, turn_id: "turn-1",
          payload: { secret: true }, output: "SECRET OUTPUT",
        },
      ],
      total: 2,
      truncated: false,
    },
    executions: {
      items: [
        {
          id: "turn:turn-1", turn_id: "turn-1", agent: "main", status: "done",
          start_time: 1759576800, end_time: 1759576805, duration_ms: 5000, error: true,
          error_text: "SECRET ERROR", prompt: "SECRET EXECUTION PROMPT",
        },
      ],
      total: 1,
      truncated: false,
    },
    operations: {
      items: [
        {
          id: "op-read", execution_id: "turn:turn-1", parent_id: null, kind: "tool",
          title: "Read the map", status: "done", at_ms: 100, duration_ms: 20,
          files: ["docs/spec.md"], summary: "SECRET TOOL SUMMARY", input: "SECRET TOOL INPUT",
        },
        {
          id: "op-agent", execution_id: "turn:turn-1", parent_id: null, kind: "agent",
          title: "Scout", status: "done", at_ms: 200, duration_ms: 40, files: [],
        },
        {
          id: "op-child", execution_id: "turn:turn-1", parent_id: "op-agent", kind: "tool",
          title: "Child read", status: "done", at_ms: 210, duration_ms: 10, files: [],
        },
        {
          id: "op-note", execution_id: "turn:turn-1", parent_id: "op-child", kind: "reasoning",
          title: "Weighing the answer", status: "done", at_ms: 220, duration_ms: null,
          files: [], summary: "SECRET CHAIN OF THOUGHT", body: "SECRET BODY", payload: { secret: true },
        },
        {
          id: "op-say", execution_id: "turn:turn-1", parent_id: null, kind: "message",
          title: "Here is the map", status: "done", at_ms: 300, duration_ms: null,
          files: [], summary: "The map is drawn.",
        },
      ],
      total: 5,
      truncated: false,
    },
  };
}

/** `fetchJson` over a table keyed by "endpoint|path". */
function fakeApi(routes = {}) {
  const calls = [];
  const json = async (url) => {
    calls.push(url);
    const parsed = new URL(url, "http://grid.test");
    const key = `${parsed.pathname}|${parsed.searchParams.get("path") || ""}`;
    const route = routes[key];
    if (!route) return { ok: false, status: 404, json: { detail: "No such file." } };
    if (typeof route === "function") return await route(parsed);
    // A route may answer with an answer ({ok, status, json}) or with a payload.
    if (typeof route === "object" && "ok" in route && "status" in route) return route;
    return { ok: true, status: 200, json: route };
  };
  return { calls, json };
}

/** The inspector over one map, with an API that records what was asked. */
function inspectorFor(map, routes = {}) {
  const api = fakeApi(routes);
  const seen = { updates: 0, picks: [], closed: 0 };
  const inspector = new Inspector({
    projectId: "p1",
    fetchJson: api.json,
    onUpdate: () => {
      seen.updates += 1;
    },
    onSelect: (selection) => seen.picks.push(selection),
    onClose: () => {
      seen.closed += 1;
    },
  });
  inspector.setMap(map, layoutMap(map));
  return { inspector, api, seen };
}

/** An inspector holding one already-read chat detail (a lazy read settled). */
function inspectorWithDetail(map = sampleMap(), detail = sampleDetail(), routes = {}) {
  const made = inspectorFor(map, routes);
  made.inspector.setChatDetail("c1", { status: "ready", detail });
  return made;
}

const folder = (path, entries) => ({ path, entries });
const entry = (path, kind = "file", size = 10) => ({
  name: path.split("/").pop(),
  path,
  kind,
  size,
  mtime: 1700000000,
});

function fakeDocument(visibility = "visible") {
  return {
    visibilityState: visibility,
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] ??= []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] ?? []).filter((item) => item !== fn);
    },
    fire(type, event) {
      for (const fn of this.listeners[type] ?? []) fn(event);
    },
    show(state) {
      this.visibilityState = state;
      this.fire("visibilitychange");
    },
  };
}

function fakeTimers() {
  const armed = [];
  return {
    armed,
    get live() {
      return armed.filter((timer) => !timer.cleared);
    },
    setInterval(fn, ms) {
      const timer = { fn, ms, cleared: false };
      armed.push(timer);
      return timer;
    },
    clearInterval(timer) {
      timer.cleared = true;
    },
  };
}

function pageNodes() {
  return {
    status: el("div"),
    title: el("h1"),
    breadcrumb: el("nav"),
    map: el("div"),
    overview: el("aside"),
    refresh: el("button"),
    build: el("span"),
  };
}

/** Fire a card's own click listener with an event the test can read. */
function clickNode(node, extra = {}) {
  const event = { target: node, preventDefault: () => { event.prevented = true; }, ...extra };
  for (const fn of node.listeners?.click ?? []) fn(event);
  return event;
}

const textOf = (root) => root.textContent;
const rowsOf = (panel) => panel.findAll("gridInspector__treeRow");
const rowByKind = (panel, kind) => rowsOf(panel).filter((row) => row.dataset.kind === kind);
const toggleByLabel = (panel, label) =>
  panel.findAll("gridInspector__treeToggle").find((node) => textOf(node).includes(label)) ?? null;
const childrenOfClass = (node, className) =>
  node.childNodes.filter((child) => child.classList?.contains?.(className));
/** The rows a list holds directly: a nested step is not a sibling of its parent. */
const directRows = (node) =>
  node.childNodes.filter((child) => child.classList?.contains?.("gridInspector__treeRow"));
/** The one child list inside `node`, or null. */
const childListOf = (node) => childrenOfClass(node, "gridInspector__treeList--child")[0] ?? null;
/** The rows a run's own Operations body holds directly. */
const runSteps = (run) => {
  const body = run.find("gridInspector__treeBody");
  const list = body ? body.childNodes.find((child) => child.classList?.contains?.("gridInspector__treeList")) : null;
  return list ? directRows(list) : [];
};

// -- selection -----------------------------------------------------------------

test("a click selects a card instead of following it", () => {
  const picked = [];
  const root = el("div");
  renderMap(root, sampleMap(), { projectId: "p1", onSelect: (selection) => picked.push(selection) });

  const task = root.findAll("gridNode--task").find((node) => node.dataset.task === "bd-2");
  const event = clickNode(task);
  assert.deepEqual(picked.at(-1), { kind: "task", id: "bd-2" });
  assert.equal(event.prevented, true, "the panel, not the browser, answers the click");
  assert.equal(task.getAttribute("href"), "/tasks?project=p1&task=bd-2", "the link is still there");

  clickNode(root.findAll("gridNode--process")[0]);
  assert.deepEqual(picked.at(-1), { kind: "process", id: "c1" });

  const dir = root.findAll("gridNode--file").find((node) => node.dataset.kind === "dir");
  clickNode(dir);
  assert.deepEqual(picked.at(-1), { kind: "dir", path: "project/docs" }, "a folder names its real path");

  const file = root.findAll("gridNode--file").find((node) => node.dataset.kind === "file");
  clickNode(file);
  assert.deepEqual(picked.at(-1), { kind: "file", path: "project/README.md" });

  // A new tab is still a new tab: a modified click is left to the browser.
  const before = picked.length;
  const modified = clickNode(task, { metaKey: true });
  assert.equal(picked.length, before);
  assert.notEqual(modified.prevented, true);
});

test("the selected card and its relatives are the only ink left", () => {
  const map = sampleMap();
  const geometry = layoutMap(map);
  const root = el("div");
  const canvas = renderMap(root, map, { projectId: "p1", geometry });

  const keys = focusKeys({ kind: "task", id: "bd-1" }, geometry, map);
  assert.equal(keys.focus, "task:bd-1");
  assert.deepEqual([...keys.related].sort(), ["process:c1", "task:bd-2", "task:bd-3"]);
  assert.deepEqual([...keys.edges].sort(), ["bd-1>bd-2", "bd-1>bd-3"]);
  assert.deepEqual([...keys.links], ["bd-1>c1"]);

  applySelection(canvas, keys);
  const byKey = Object.fromEntries(root.findAll("gridNode").map((node) => [node.dataset.focus, node]));
  assert.ok(byKey["task:bd-1"].classList.contains("is-selected"));
  assert.ok(byKey["task:bd-2"].classList.contains("is-related"));
  assert.ok(byKey["process:c1"].classList.contains("is-related"));
  assert.ok(byKey["file:project/docs"].classList.contains("is-dimmed"));
  assert.ok(!byKey["file:project/docs"].classList.contains("is-related"));

  const edges = canvas.querySelectorAll(".gridMap__edge");
  assert.ok(edges.find((path) => path.getAttribute("data-to") === "bd-2").classList.contains("is-lit"));
  assert.ok(edges.find((path) => path.getAttribute("data-to") === "bd-3").classList.contains("is-lit"));

  // Nothing selected: every card keeps its normal weight.
  applySelection(canvas, null);
  for (const node of root.findAll("gridNode")) {
    assert.ok(!node.classList.contains("is-selected"));
    assert.ok(!node.classList.contains("is-related"));
    assert.ok(!node.classList.contains("is-dimmed"));
  }
  for (const path of canvas.querySelectorAll(".gridMap__edge")) {
    assert.ok(!path.classList.contains("is-lit"));
    assert.ok(!path.classList.contains("is-dimmed"));
  }
});

test("a chat lights the tasks that really link to it; a file lights its folder", () => {
  const map = sampleMap();
  const geometry = layoutMap(map);

  const fromChat = focusKeys({ kind: "process", id: "c1" }, geometry, map);
  assert.equal(fromChat.focus, "process:c1");
  assert.deepEqual([...fromChat.related].sort(), ["task:bd-1"], "only the turn link, nothing guessed");
  assert.deepEqual([...fromChat.links], ["bd-1>c1"]);

  // A message, execution or operation lights the chat that owns it.
  for (const selection of [
    { kind: "message", chatId: "c1", id: "m1" },
    { kind: "execution", chatId: "c1", id: "turn:turn-1" },
    { kind: "operation", chatId: "c1", id: "op-read" },
  ]) {
    const keys = focusKeys(selection, geometry, map);
    assert.equal(keys.focus, "process:c1", `${selection.kind} points at its chat`);
    assert.deepEqual([...keys.related].sort(), ["task:bd-1"]);
  }

  const fromFile = focusKeys({ kind: "file", path: "project/docs/readme.md" }, geometry, map);
  assert.deepEqual([...fromFile.related], ["file:project/docs"], "a file lights every folder above it");

  const idle = focusKeys({ kind: "process", id: "c2" }, geometry, map);
  assert.deepEqual([...idle.related], ["task:bd-2"], "a chat with no file keeps only its task");
  assert.equal(focusKeys(null, geometry, map), null);
});

test("a selection is normalized to the canonical names, and nonsense is refused", () => {
  assert.deepEqual(normalizeSelection({ kind: "process", id: "c1" }), { kind: "chat", id: "c1" });
  assert.deepEqual(normalizeSelection({ kind: "chat", id: "c1" }), { kind: "chat", id: "c1" });
  assert.deepEqual(normalizeSelection({ kind: "message", chatId: "c1", id: "m1" }), { kind: "message", chatId: "c1", id: "m1" });
  assert.deepEqual(normalizeSelection({ kind: "execution", chat: "c1", executionId: "r1", sub: true }), {
    kind: "execution", chatId: "c1", id: "r1", sub: true,
  });
  assert.deepEqual(normalizeSelection({ kind: "file", path: "project/a.md" }), { kind: "file", path: "project/a.md" });
  assert.equal(normalizeSelection({ kind: "message", id: "m1" }), null, "a message without its chat names nothing");
  assert.equal(normalizeSelection({ kind: "operation", chatId: "c1" }), null);
  assert.equal(normalizeSelection({ kind: "file", path: "/etc/passwd" }), null);
  assert.equal(normalizeSelection({ kind: "context", id: "scout" }), null, "a legacy context is not a canonical node");
  assert.equal(normalizeSelection(null), null);

  assert.equal(selectionKey({ kind: "chat", id: "c1" }), "process:c1");
  assert.equal(selectionKey({ kind: "operation", chatId: "c1", id: "op-read" }), "operation:c1:op-read");
  assert.equal(selectionKey({ kind: "execution", chatId: "c1", id: "turn-1", sub: true }), "execution:sub:c1:turn-1");
  assert.equal(selectionKey({ kind: "message", chatId: "c1", id: "m1" }), "message:c1:m1");
});

test("Escape lets the selection go, and the close button too", async () => {
  const doc = fakeDocument();
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc,
    timers: fakeTimers(),
    loadMap: async () => ({ notModified: false, etag: '"a"', map: sampleMap() }),
  });
  await page.load();

  clickNode(nodes.map.findAll("gridNode--task")[0]);
  assert.deepEqual(page.inspector.selection, { kind: "task", id: "bd-1" });
  assert.match(textOf(nodes.overview), /Open task/);
  assert.ok(nodes.map.findAll("gridNode").find((node) => node.dataset.focus === "task:bd-1").classList.contains("is-selected"));

  doc.fire("keydown", { key: "Escape" });
  assert.equal(page.inspector.selection, null);
  assert.doesNotMatch(textOf(nodes.overview), /Open task/);
  assert.ok(!nodes.map.findAll("gridNode").some((node) => node.classList.contains("is-selected")));

  clickNode(nodes.map.findAll("gridNode--task")[0]);
  const panel = nodes.overview.find("gridInspector");
  panel.find("gridInspector__close").click();
  assert.equal(page.inspector.selection, null);
});

// -- the task panel ------------------------------------------------------------

test("the task panel names the safe fields and opens the task", async () => {
  const { inspector } = inspectorFor(sampleMap());
  await inspector.select({ kind: "task", id: "bd-2" });
  const panel = inspector.element();
  assert.equal(panel.dataset.kind, "task");
  assert.match(textOf(panel), /Beta/);
  assert.match(textOf(panel), /In progress/);
  assert.match(textOf(panel), /P1/);
  assert.match(textOf(panel), /me/);
  const open = panel.findAll("gridInspector__open")[0];
  assert.equal(open.getAttribute("href"), "/tasks?project=p1&task=bd-2");
  assert.equal(open.tagName, "A", "a real link, not a button that pretends");

  // A task that left the map is said to have left, not drawn as an empty box.
  const gone = inspectorFor(sampleMap()).inspector;
  await gone.select({ kind: "task", id: "bd-9" });
  assert.match(textOf(gone.element()), /no longer on the map/);
});

test("the project and its groups say what they are, without inventing entities", async () => {
  const { inspector } = inspectorFor(sampleMap());
  await inspector.select({ kind: "project", id: "p1" });
  const project = inspector.element();
  assert.equal(project.dataset.kind, "project");
  assert.match(textOf(project), /Grid/);
  assert.match(textOf(project), /2 chats/);
  assert.match(textOf(project), /3 tasks/);

  await inspector.select({ kind: "collection", id: "collection:p1:files" });
  const group = inspector.element();
  assert.equal(group.dataset.kind, "collection");
  assert.match(textOf(group), /files/);
  assert.match(textOf(group), /not an entity itself/);
});

// -- the chat panel ------------------------------------------------------------

test("the chat panel is the canonical tree, and never a raw prompt or a thought", async () => {
  const { inspector, api } = inspectorWithDetail();
  await inspector.select({ kind: "process", id: "c1" });
  const panel = inspector.element();
  assert.equal(panel.dataset.kind, "chat");
  assert.deepEqual(api.calls, [], "the page owns the lazy read; the panel never fetches");

  const labels = panel.findAll("gridInspector__treeLabel").map((node) => textOf(node));
  assert.deepEqual(labels.slice(0, 2), ["Messages", "Executions"], "the two real levels of a chat");
  assert.match(textOf(toggleByLabel(panel, "Messages")), /Messages2/);
  assert.match(textOf(toggleByLabel(panel, "Executions")), /Executions1/);

  const messages = rowByKind(panel, "message");
  assert.deepEqual(messages.map((row) => row.dataset.id), ["m1", "m2"]);
  assert.match(textOf(messages[0]), /Read the map, please/, "the server's sanitized preview");
  assert.match(textOf(messages[0]), /user/);
  assert.doesNotMatch(textOf(messages[0]), /image/, "no attachment, no word about one");
  assert.match(textOf(messages[1]), /assistant/);
  assert.match(textOf(messages[1]), /image/, "an attachment is said, not shown");

  const executions = rowByKind(panel, "execution");
  assert.deepEqual(executions.map((row) => row.dataset.id), ["turn:turn-1"]);
  assert.match(textOf(executions[0]), /Turn turn-1/);
  assert.match(textOf(executions[0]), /agent main/);
  assert.match(textOf(executions[0]), /Done/);
  assert.match(textOf(executions[0]), /error/, "the boolean is said");

  const operations = rowByKind(panel, "operation");
  assert.deepEqual(operations.map((row) => row.dataset.id).sort(), ["op-agent", "op-child", "op-note", "op-read", "op-say"]);
  assert.match(textOf(operations.find((row) => row.dataset.id === "op-say")), /The map is drawn\./, "only a message narrates itself");

  const words = textOf(panel);
  for (const secret of ["SECRET", "secret: true", "chain of thought"]) {
    assert.doesNotMatch(words, new RegExp(secret, "i"), `no raw payload survives: ${secret}`);
  }
  assert.doesNotMatch(words, /SECRET PROMPT|SECRET OUTPUT|SECRET TOOL SUMMARY|SECRET EXECUTION PROMPT/);

  const open = panel.findAll("gridInspector__open")[0];
  assert.equal(open.getAttribute("href"), "/?conversation=c1", "Open chat is the real chat link");
  assert.equal(panel.querySelectorAll("script").length, 0);
});

test("an execution owns exactly the operations that name it", async () => {
  const detail = sampleDetail();
  detail.executions.items.push({
    id: "turn:turn-2", turn_id: "turn-2", agent: "helper", status: "running",
    start_time: 1759576900, end_time: null, duration_ms: null, error: false,
  });
  detail.executions.total = 2;
  detail.operations.items.push(
    { id: "op-second", execution_id: "turn:turn-2", parent_id: null, kind: "tool", title: "Another run", status: "running", at_ms: 10, duration_ms: null, files: [] },
    { id: "op-orphan", execution_id: "turn:gone", parent_id: null, kind: "tool", title: "No run here", status: "done", at_ms: 10, duration_ms: null, files: [] },
  );
  detail.operations.total = 7;
  const { inspector } = inspectorWithDetail(sampleMap(), detail);
  await inspector.select({ kind: "execution", chatId: "c1", id: "turn:turn-2" });

  const panel = inspector.element();
  const blocks = panel.findAll("gridInspector__execution");
  assert.equal(blocks.length, 2, "one block per run, and no block for an unknown run");
  const second = blocks.find((block) => textOf(block).includes("Turn turn-2"));
  assert.match(textOf(second), /agent helper/);
  assert.match(textOf(second), /Running/);
  assert.match(textOf(second), /Another run/);
  assert.doesNotMatch(textOf(second), /Read the map/, "a run never borrows another run's steps");
  assert.doesNotMatch(textOf(panel), /No run here/, "an operation with no run of its own is not drawn");

  // The selection is the exact run, not the whole chat.
  const selected = rowsOf(panel).filter((row) => row.dataset.selected === "true");
  assert.deepEqual(selected.map((row) => `${row.dataset.kind}:${row.dataset.id}`), ["execution:turn:turn-2"]);
});

test("an agent operation opens the child execution that owns its own steps", async () => {
  const { inspector } = inspectorWithDetail();
  await inspector.select({ kind: "chat", id: "c1" });
  const panel = inspector.element();

  const rootRun = panel.findAll("gridInspector__execution")[0];
  const runOps = runSteps(rootRun);
  assert.deepEqual(runOps.map((row) => row.dataset.id), ["op-read", "op-agent", "op-say"], "the run's own steps, in trace order");

  const agentRow = runOps.find((row) => row.dataset.id === "op-agent");
  assert.match(textOf(agentRow), /Scout/);
  const child = agentRow.findAll("gridInspector__childExecution");
  assert.equal(child.length, 1, "the delegation is a level of its own");
  assert.equal(child[0].dataset.agent, "op-agent");
  assert.match(textOf(child[0]), /Child execution · Scout/);

  // The child operation hangs under that child execution, exactly once.
  const childOperation = directRows(childListOf(child[0]));
  assert.deepEqual(childOperation.map((row) => row.dataset.id), ["op-child"], "a step of the child run is not also a root step");
  assert.equal(rowsOf(panel).filter((row) => row.dataset.id === "op-child").length, 1);

  // A deeper step stays under the operation that produced it, in the same run.
  const nested = directRows(childListOf(childOperation[0]));
  assert.deepEqual(nested.map((row) => row.dataset.id), ["op-note"]);

  // Root operations remain root operations.
  assert.equal(runOps.some((row) => row.dataset.id === "op-child"), false, "a child step is never repeated at the top");
});

test("selecting a message, a run or an operation marks exactly that row", async () => {
  const { inspector } = inspectorWithDetail();

  for (const [selection, key] of [
    [{ kind: "message", chatId: "c1", id: "m2" }, "m2"],
    [{ kind: "execution", chatId: "c1", id: "turn:turn-1" }, "turn:turn-1"],
    [{ kind: "operation", chatId: "c1", id: "op-child" }, "op-child"],
  ]) {
    await inspector.select(selection);
    const panel = inspector.element();
    assert.equal(panel.dataset.kind, selection.kind, "the panel says what was selected");
    assert.ok(panel.findAll("gridInspector__tree").length === 1, "and shows the whole chat the node belongs to");
    const selected = rowsOf(panel).filter((row) => row.dataset.selected === "true");
    assert.deepEqual(selected.map((row) => row.dataset.id), [key], `${selection.kind} marks one row`);
    assert.equal(selected[0].getAttribute("aria-selected"), "true");
  }
});

test("a step that is clicked asks for that exact node", async () => {
  const { inspector, seen } = inspectorWithDetail();
  await inspector.select({ kind: "chat", id: "c1" });
  const panel = inspector.element();
  const child = rowByKind(panel, "operation").find((row) => row.dataset.id === "op-child");
  clickNode(child);
  assert.deepEqual(seen.picks.at(-1), { kind: "operation", chatId: "c1", id: "op-child" });

  const message = rowByKind(panel, "message")[0];
  clickNode(message);
  assert.deepEqual(seen.picks.at(-1), { kind: "message", chatId: "c1", id: "m1" });

  // A keyboard reader gets the same exact node.
  const run = rowByKind(panel, "execution")[0];
  for (const fn of run.listeners.keydown ?? []) fn({ key: "Enter", preventDefault() {} });
  assert.deepEqual(seen.picks.at(-1), { kind: "execution", chatId: "c1", id: "turn:turn-1" });
});

test("a file chip on a step opens that path, and the refs are bounded", async () => {
  const detail = sampleDetail();
  detail.operations.items[0].files = [
    "docs/spec.md", "docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md", "docs/e.md",
  ];
  const { inspector, seen } = inspectorWithDetail(sampleMap(), detail);
  await inspector.select({ kind: "chat", id: "c1" });
  const panel = inspector.element();

  const chips = panel.findAll("gridFileRef");
  assert.equal(chips.length, 4, "an operation carries a few references, not a dump");
  assert.equal(chips[0].dataset.path, "project/docs/spec.md");
  assert.match(textOf(chips[0]), /docs\/spec\.md/);
  assert.equal(chips.some((chip) => chip.dataset.path === "project/docs/e.md"), false, "the tail is not drawn at all");

  chips[0].click();
  assert.deepEqual(seen.picks.at(-1), { kind: "file", path: "project/docs/spec.md" });
});

test("a chat with no detail yet is not invented", async () => {
  const { inspector, api } = inspectorFor(sampleMap());
  await inspector.select({ kind: "process", id: "c1" });
  const panel = inspector.element();
  assert.match(textOf(panel), /Select the chat to read its messages and executions\./);
  assert.equal(panel.findAll("gridInspector__tree").length, 0);
  assert.equal(panel.findAll("gridInspector__treeRow").length, 0);
  assert.equal(panel.findAll("gridInspector__open")[0].getAttribute("href"), "/?conversation=c1");
  assert.deepEqual(api.calls, [], "selecting a chat never fetches by itself");

  inspector.setChatDetail("c1", { status: "loading" });
  assert.match(textOf(inspector.element()), /Loading the chat…/);

  inspector.setChatDetail("c1", { status: "error", error: "The chat detail could not be loaded." });
  assert.match(textOf(inspector.element()), /could not be loaded/);
  assert.equal(inspector.element().findAll("gridInspector__treeRow").length, 0, "a failed read draws no tree");
});

test("a chat whose detail has no runs or steps says so", async () => {
  const detail = {
    chat: { id: "c2", title: "Chat two", href: "/?conversation=c2" },
    messages: { items: [], total: 0, truncated: false },
    executions: { items: [], total: 0, truncated: false },
    operations: { items: [], total: 0, truncated: false },
  };
  const { inspector } = inspectorFor(sampleMap());
  inspector.setChatDetail("c2", { status: "ready", detail });
  await inspector.select({ kind: "chat", id: "c2" });
  const panel = inspector.element();
  assert.match(textOf(panel), /no safe message preview/);
  assert.match(textOf(panel), /no executions/);
  assert.equal(rowsOf(panel).length, 0, "nothing is made up");
});

test("a truncated detail says how much of it is shown", async () => {
  const detail = sampleDetail();
  detail.messages.total = 30;
  detail.messages.truncated = true;
  detail.executions.truncated = true;
  detail.executions.total = 4;
  detail.operations.total = 140;
  detail.operations.truncated = true;
  const { inspector } = inspectorWithDetail(sampleMap(), detail);
  await inspector.select({ kind: "chat", id: "c1" });
  const words = textOf(inspector.element());
  assert.match(words, /Showing 2 of 30 messages/);
  assert.match(words, /1 of 4 executions/);
  assert.match(words, /5 of 140 operations/);
  assert.match(textOf(toggleByLabel(inspector.element(), "Messages")), /30/, "the group count is the server's total");
});

test("a group and a run fold with a real button, and the fold survives a re-render", async () => {
  const { inspector, seen } = inspectorWithDetail();
  await inspector.select({ kind: "chat", id: "c1" });

  let panel = inspector.element();
  const messagesToggle = toggleByLabel(panel, "Messages");
  assert.equal(messagesToggle.tagName, "BUTTON");
  assert.equal(messagesToggle.getAttribute("aria-expanded"), "true");
  messagesToggle.click();
  assert.equal(seen.updates > 0, true, "the panel is rebuilt by its own update callback");

  panel = inspector.element();
  const folded = toggleByLabel(panel, "Messages");
  assert.equal(folded.getAttribute("aria-expanded"), "false");
  const body = panel.findAll("gridInspector__treeBody").find((node) => node.getAttribute("id") === folded.getAttribute("aria-controls"));
  assert.equal(body.getAttribute("hidden"), "", "the folded body is really hidden");
  assert.equal(folded.findAll("gridInspector__caret")[0].textContent, "▸");

  // The same selection keeps the fold; another selection opens it again.
  await inspector.select({ kind: "chat", id: "c1" });
  assert.equal(toggleByLabel(inspector.element(), "Messages").getAttribute("aria-expanded"), "false");
  await inspector.select({ kind: "task", id: "bd-1" });
  await inspector.select({ kind: "chat", id: "c1" });
  assert.equal(toggleByLabel(inspector.element(), "Messages").getAttribute("aria-expanded"), "true");

  // A run's own toggle folds just its steps.
  panel = inspector.element();
  const runToggle = toggleByLabel(panel, "Operations");
  assert.equal(runToggle.getAttribute("aria-expanded"), "true");
  runToggle.click();
  assert.equal(runToggle.getAttribute("aria-expanded"), "false");
});

test("a legacy activity island on a chat is not drawn", async () => {
  const map = sampleMap();
  map.chats.items[1].activity = {
    items: [{ id: "s1", kind: "agent", title: "Old island", files: [] }],
    contexts: [{ id: "scout", parent_id: null, label: "Scout", total: 1 }],
    total: 1,
  };
  const { inspector } = inspectorFor(map);
  inspector.setChatDetail("c2", {
    status: "ready",
    detail: {
      chat: { id: "c2", title: "Chat two", href: "/?conversation=c2" },
      messages: { items: [], total: 0, truncated: false },
      executions: { items: [], total: 0, truncated: false },
      operations: { items: [], total: 0, truncated: false },
    },
  });
  await inspector.select({ kind: "chat", id: "c2" });
  const words = textOf(inspector.element());
  assert.doesNotMatch(words, /Old island/);
  assert.doesNotMatch(words, /Scout/);
  assert.equal(inspector.element().findAll("gridActivity__item").length, 0);
});

test("a new map keeps the read detail, and an empty chat id clears every detail", () => {
  const { inspector } = inspectorWithDetail();
  assert.ok(inspector.chatDetails.has("c1"));
  inspector.setMap(sampleMap(), layoutMap(sampleMap()));
  assert.ok(inspector.chatDetails.has("c1"), "a refresh does not throw a read detail away");
  inspector.setChatDetail("", null);
  assert.equal(inspector.chatDetails.size, 0);
  assert.equal(inspector.chatDetailStates.size, 0);
});

// -- the files panel -----------------------------------------------------------

test("a folder is read when it is opened, and only then", async () => {
  const map = sampleMap();
  const routes = {
    "/api/workspace/tree|project/docs": folder("project/docs", [
      entry("project/docs/a.md"),
      entry("project/docs/nested", "dir", null),
    ]),
  };
  const { inspector, api } = inspectorFor(map, routes);
  assert.deepEqual(api.calls, [], "nothing is read before a card is selected");

  await inspector.select({ kind: "dir", path: "project/docs" });
  assert.equal(api.calls.length, 1);
  const asked = new URL(api.calls[0], "http://grid.test");
  assert.equal(asked.pathname, "/api/workspace/tree");
  assert.equal(asked.searchParams.get("context"), "p1");
  assert.equal(asked.searchParams.get("path"), "project/docs");

  const panel = inspector.element();
  assert.match(textOf(panel), /Entries/);
  assert.match(textOf(panel), /a\.md/);
  assert.match(textOf(panel), /nested/);
  assert.match(textOf(panel), /Folder/);
  assert.equal(panel.findAll("gridInspector__entryButton").length, 2);
  assert.equal(panel.findAll("gridInspector__open")[0].getAttribute("href"), "/files?context=p1&path=project%2Fdocs");

  // Opening the same folder again answers from what was already read.
  await inspector.select({ kind: "dir", path: "project/docs" });
  assert.equal(api.calls.length, 1, "one folder is asked for once");

  assert.equal(treeUrl(map.files_url, "project/docs"), "/api/workspace/tree?context=p1&path=project%2Fdocs");
  assert.equal(textUrl(map.files_url, "project/a.md"), "/api/workspace/text?context=p1&path=project%2Fa.md");
});

test("a folder's entries are ways in, and its breadcrumbs are ways back", async () => {
  const routes = {
    "/api/workspace/tree|project/docs": folder("project/docs", [entry("project/docs/nested", "dir", null)]),
    "/api/workspace/tree|project": folder("project", [entry("project/docs", "dir", null)]),
  };
  const { inspector, seen } = inspectorFor(sampleMap(), routes);
  await inspector.select({ kind: "dir", path: "project/docs" });

  const panel = inspector.element();
  panel.find("gridInspector__entryButton").click();
  assert.deepEqual(seen.picks.at(-1), { kind: "dir", path: "project/docs/nested" });

  const crumbs = panel.findAll("gridInspector__crumbButton");
  assert.deepEqual(crumbs.map((node) => textOf(node)), ["project"]);
  crumbs[0].click();
  assert.deepEqual(seen.picks.at(-1), { kind: "dir", path: "project" });
});

test("a file shows numbered lines and says how many it loaded", async () => {
  const lines = Array.from({ length: 20 }, (_, index) => `line ${index + 1}`);
  const routes = {
    "/api/workspace/tree|project": folder("project", [entry("project/README.md")]),
    "/api/workspace/text|project/README.md": {
      path: "project/README.md", size: 240, mtime: "1700000000", truncated: false, binary: false,
      text: `${lines.join("\n")}\n`,
    },
  };
  const { inspector, api } = inspectorFor(sampleMap(), routes);
  await inspector.select({ kind: "file", path: "project/README.md" });

  const panel = inspector.element();
  const shown = panel.findAll("gridPreview__line");
  assert.equal(shown.length, PREVIEW_LINES);
  assert.equal(textOf(shown[0]), "1line 1", "a line carries its number and its text");
  assert.equal(textOf(shown[11]), "12line 12");
  assert.match(textOf(panel), /Showing 12 of 20 lines · open the file for the rest/);
  assert.match(textOf(panel), /Pathproject\/README\.md/);
  assert.match(textOf(panel), /240 B/);
  assert.equal(panel.findAll("gridInspector__open")[0].getAttribute("href"), "/files?context=p1&file=project%2FREADME.md");

  // The listing of the folder the file is in was read, not the whole tree.
  assert.deepEqual(
    api.calls.map((url) => new URL(url, "http://grid.test").searchParams.get("path")).sort(),
    ["project", "project/README.md"],
  );
});

test("text from a file lands as text, never as markup", async () => {
  const routes = {
    "/api/workspace/tree|project": folder("project", [entry("project/x.md")]),
    "/api/workspace/text|project/x.md": {
      path: "project/x.md", size: 30, truncated: false, binary: false,
      text: '<script>alert(1)</script>\n<img src=x onerror="alert(2)">',
    },
  };
  const { inspector } = inspectorFor(sampleMap(), routes);
  await inspector.select({ kind: "file", path: "project/x.md" });
  const panel = inspector.element();
  assert.equal(panel.querySelectorAll("script").length, 0);
  assert.equal(panel.querySelectorAll("img").length, 0);
  assert.match(textOf(panel), /<script>alert\(1\)<\/script>/);
});

test("a binary file says so instead of pretending to be empty", async () => {
  const routes = {
    "/api/workspace/tree|project": folder("project", [entry("project/blob.bin", "file", 2048)]),
    "/api/workspace/text|project/blob.bin": {
      path: "project/blob.bin", size: 2048, mtime: "1700000000", truncated: false, binary: true, text: null,
    },
  };
  const { inspector } = inspectorFor(sampleMap(), routes);
  await inspector.select({ kind: "file", path: "project/blob.bin" });
  const panel = inspector.element();
  assert.equal(panel.findAll("gridPreview").length, 0, "no invented preview");
  assert.match(textOf(panel), /not a text file: no preview/);
  assert.match(textOf(panel), /2\.0 KB/);
});

test("a refusal of the files endpoint is told honestly", async () => {
  for (const [status, words] of [
    [415, /cannot be shown as text/],
    [404, /not there/],
    [403, /may not read that/],
    [413, /too large/],
    [500, /status 500/],
  ]) {
    const routes = {
      "/api/workspace/tree|project/docs": folder("project/docs", [entry("project/docs/a.bin")]),
      "/api/workspace/text|project/docs/a.bin": { ok: false, status, json: { detail: "refused" } },
    };
    const { inspector } = inspectorFor(sampleMap(), routes);
    await inspector.select({ kind: "file", path: "project/docs/a.bin" });
    const panel = inspector.element();
    assert.match(textOf(panel), words, `status ${status} is named for what it is`);
    assert.equal(panel.findAll("gridPreview").length, 0);
  }
});

test("a folder that cannot be read says why, and keeps its way in", async () => {
  const routes = { "/api/workspace/tree|project/docs": { ok: false, status: 404, json: null } };
  const { inspector } = inspectorFor(sampleMap(), routes);
  await inspector.select({ kind: "dir", path: "project/docs" });
  const panel = inspector.element();
  assert.match(textOf(panel), /not there/);
  assert.equal(panel.findAll("gridInspector__open").length, 1, "the files page is still one click away");
});

test("an absolute path or a .. is refused before any request", async () => {
  const { inspector, api } = inspectorFor(sampleMap(), {});
  for (const path of ["/etc/passwd", "project/../secret", "..", "project\\docs", "file:///etc/passwd", "C:/windows"]) {
    await inspector.select({ kind: "file", path });
    assert.equal(inspector.selection, null, `${path} is not a path in this space`);
    assert.equal(inspector.element(), null);
  }
  assert.deepEqual(api.calls, []);

  assert.equal(safePath("project/docs/a.md"), "project/docs/a.md");
  assert.equal(safePath(""), "");
  assert.equal(safePath("a//b"), "a/b");
});

test("with no files_url the panel says the space is not open", async () => {
  const { inspector, api } = inspectorFor(sampleMap({ files_url: null }), {});
  await inspector.select({ kind: "dir", path: "project/docs" });
  const panel = inspector.element();
  assert.match(textOf(panel), /not open to you/);
  assert.equal(panel.findAll("gridInspector__open").length, 0, "no link to a page that would refuse");
  assert.equal(panel.findAll("gridInspector__entryButton").length, 0);
  assert.deepEqual(api.calls, []);
});

test("a browser with no fetch is not a crash: the explorer says it did not answer", async () => {
  const explorer = createExplorer({ filesUrl: "/files?context=p1&path=project", fetchJson: async () => { throw new Error("offline"); } });
  const listing = await explorer.tree("project");
  assert.equal(listing.ok, false);
  assert.equal(listing.status, 0);
  assert.match(listing.error, /did not answer/);
  const refused = createExplorer({ filesUrl: "/files?context=p1&path=project", fetchJson: null });
  const result = await refused.tree("project");
  assert.equal(result.ok, false, "a space with a root and no reader answers honestly");
});

// -- the page ------------------------------------------------------------------

test("a new answer keeps the inspector and re-reads nothing", async () => {
  const map = sampleMap();
  const routes = { "/api/workspace/tree|project/docs": folder("project/docs", [entry("project/docs/a.md")]) };
  const api = fakeApi(routes);
  const nodes = pageNodes();
  let loads = 0;
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    fetchJson: api.json,
    loadMap: async (id, { etag } = {}) => {
      loads += 1;
      if (loads === 2) return { notModified: true, etag };
      return { notModified: false, etag: `"v${loads}"`, map };
    },
  });
  await page.load();

  const dir = nodes.map.findAll("gridNode--file").find((node) => node.dataset.kind === "dir");
  dir.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.match(textOf(nodes.overview), /Entries/);
  assert.equal(api.calls.length, 1);
  assert.equal(new URL(api.calls[0], "http://grid.test").searchParams.get("path"), "project/docs");

  // A 304 redraws nothing; a changed answer redraws the map but re-reads no
  // folder: the panel keeps what it already has.
  await page.load();
  await page.load({ force: true });
  assert.equal(api.calls.length, 1, "a refresh never lists the space again");
  assert.match(textOf(nodes.overview), /Entries/, "the open folder survives the refresh");
  assert.equal(page.inspector.selection.kind, "dir");
  for (const url of api.calls) {
    assert.doesNotMatch(url, /[?&]path=project(&|$)/, "the whole space is never listed on a refresh");
  }
});

test("the panel stands in the overview, above its counts", async () => {
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => ({ notModified: false, etag: '"a"', map: sampleMap() }),
  });
  await page.load();
  assert.match(textOf(nodes.overview), /Overview/);
  assert.equal(nodes.overview.find("gridInspector"), null, "nothing selected, no panel");

  await page.select({ kind: "task", id: "bd-1" });
  const panel = nodes.overview.find("gridInspector");
  assert.ok(panel, "the panel is part of the overview");
  assert.match(textOf(panel), /Open task/);
  assert.equal(textOf(nodes.overview.find("gridOverview__title")), "Overview", "the overview keeps its own heading");
  assert.match(textOf(nodes.overview), /Connections|Tasks/, "and its counts are still there");
});

test("the page reads a selected chat's detail once, and never sends it in the map", async () => {
  const nodes = pageNodes();
  const asks = [];
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async (id, { etag, signal } = {}) => {
      asks.push({ etag, signal });
      return etag ? { notModified: true, etag } : { notModified: false, etag: '"a"', map: sampleMap() };
    },
    loadChatDetail: async (_project, chatId) => {
      assert.equal(chatId, "c1");
      return { notModified: false, etag: '"d"', detail: sampleDetail() };
    },
  });
  await page.load();
  assert.equal(page.chatDetails.size, 0, "the base map carries no transcript");

  await page.select({ kind: "chat", id: "c1" });
  await page.ensureSelectedDetail();
  assert.equal(page.chatDetails.size, 1);
  assert.match(textOf(nodes.overview), /Read the map, please/, "the real safe message reaches the panel");

  await page.select({ kind: "operation", chatId: "c1", id: "op-child" });
  await page.ensureSelectedDetail();
  assert.equal(page.chatDetails.size, 1, "the chat is read once, not per selected node");
});

test("the page does not navigate on a card click, and keeps the link", async () => {
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => ({ notModified: false, etag: '"a"', map: sampleMap() }),
    loadChatDetail: async () => ({ notModified: false, etag: '"d"', detail: sampleDetail() }),
  });
  await page.load();
  const chat = nodes.map.findAll("gridNode--process")[0];
  const event = clickNode(chat);
  assert.equal(event.prevented, true);
  assert.deepEqual(page.inspector.selection, { kind: "chat", id: "c1" }, "the canonical name, not the legacy one");
  assert.match(textOf(nodes.overview), /Open chat/);
  assert.equal(chat.getAttribute("href"), "/?conversation=c1");
});

// -- the source ----------------------------------------------------------------

test("the inspector builds with text nodes only", () => {
  for (const name of ["inspector.js", "explorer.js", "view.js"]) {
    const source = readFileSync(fileURLToPath(new URL(`../web_chat/js/grid/${name}`, import.meta.url)), "utf8");
    assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML|document\.write|eval\(/, `${name} puts no data in markup`);
  }
  const render = readFileSync(fileURLToPath(new URL("../web_chat/js/grid/render.js", import.meta.url)), "utf8");
  assert.doesNotMatch(render, /innerHTML|insertAdjacentHTML|document\.write/);
});

test("the inspector has no legacy context or activity hierarchy left", () => {
  const source = readFileSync(fileURLToPath(new URL("../web_chat/js/grid/inspector.js", import.meta.url)), "utf8");
  for (const gone of ["activityItems", "activityContexts", "contextId", "gridActivity", "contexts"]) {
    assert.equal(source.includes(gone), false, `no legacy ${gone} in the canonical panel`);
  }
  const api = readFileSync(fileURLToPath(new URL("../web_chat/js/grid/api.js", import.meta.url)), "utf8");
  assert.match(api, /map\/chats\//, "the detail contract is the one endpoint the page reads");
});
