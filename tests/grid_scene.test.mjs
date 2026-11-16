/**
 * The SceneModel (web_chat/js/grid/scene.js): the one scene the WebGL Space and
 * the mobile Layers/Timeline projections both draw.
 *
 * What is pinned here is the model's promise to a reader, not its internals: the
 * canonical Project / Files / Chats / Messages / Executions / Operations
 * hierarchy stands exactly as the answers name it; the axes mean what they say
 * (a stable geography, a hierarchy level, a real moment); nothing the answers do
 * not say is invented (no context island, no guessed message owner, no
 * timestamp, no file, no agent); a selected chat's detail is added lazily and
 * never moves the map beneath it; a slice and a selection are honest and pure;
 * and 300/50/100 items plus a chat's detail still give finite geometry.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DETAIL_Y_STEP,
  EDGE_KINDS,
  NODE_KINDS,
  SCENE_VERSION,
  SEMANTIC_Y,
  UNKNOWN_Z,
  createScene,
  expandActivity,
  nodeId,
  relatedIds,
  sceneStats,
  sliceScene,
} from "../web_chat/js/grid/scene.js";

const clone = (value) => JSON.parse(JSON.stringify(value));

/** A project as GET /api/projects/{id}/map answers for it. */
function sampleMap(overrides = {}) {
  return {
    project: { id: "p1", title: "Grid", parent_id: null, description: "The workspace", updated_at: "2026-10-30T05:00:00Z" },
    path: [{ id: "p0", title: "Studio" }],
    children: [],
    tasks: {
      available: true,
      reason: null,
      reason_code: null,
      items: [
        {
          id: "bd-1", title: "Alpha", status: "open", state: "ready", priority: 0, type: "task",
          assignee: null, created_at: "2026-10-01T09:00:00Z", updated_at: "2026-10-02T09:00:00Z",
          depends_on: [], close_reason: null,
        },
        {
          id: "bd-2", title: "Beta", status: "in_progress", state: "in_progress", priority: 1, type: "task",
          assignee: "me@example.test", created_at: "2026-10-02T09:00:00Z", updated_at: "2026-10-05T09:00:00Z",
          depends_on: ["bd-1", "missing-task"], close_reason: null,
        },
        {
          id: "bd-3", title: "Gamma", status: "blocked", state: "blocked", priority: 2, type: "bug",
          assignee: null, created_at: "2026-10-03T09:00:00Z", updated_at: "2026-10-03T10:00:00Z",
          depends_on: [], close_reason: null,
        },
        {
          // No created_at at all: the answer does not date it, and neither do we.
          id: "bd-4", title: "Undated", status: "open", state: "ready", priority: 2, type: "task",
          assignee: null, created_at: null, updated_at: null, depends_on: [], close_reason: null,
        },
      ],
      total: 4,
      truncated: false,
      counts: { ready: 2, in_progress: 1, blocked: 1 },
    },
    processes: {
      available: true,
      items: [
        {
          chat_id: "c1", title: "Chat one", updated_at: "2026-10-04T10:00:00Z", active: true, awaiting_approval: false,
          steps: ["read the map answer", "drew the cards"],
          // The old chat activity (and its context tree) is not a hierarchy any
          // more: a map that still carries it must not grow a context node.
          activity: {
            items: [{ id: "s1", kind: "tool", title: "read the map", at_ms: 0, files: ["src/app.py"], context_id: "root" }],
            total: 1,
            truncated: false,
            contexts: [
              { id: "root", parent_id: null, label: "Main agent" },
              { id: "scout", parent_id: "root", label: "scout", at_ms: 10 },
            ],
          },
          task_links: [
            { task_id: "bd-1", relation: "turn" },
            { task_id: "bd-2", relation: "plan" },
            { task_id: "missing-task", relation: "turn" },
          ],
          task_links_total: 3,
        },
        {
          chat_id: "c2", title: "Chat two", updated_at: null, active: false, awaiting_approval: true,
          steps: [], activity: { items: [], total: 0, truncated: false }, task_links: [], task_links_total: 0,
        },
      ],
      total: 2,
      truncated: false,
    },
    files: {
      available: true,
      items: [
        { name: "src", path: "src", kind: "dir", size: null, modified: "2026-10-04T08:00:00Z" },
        { name: "app.py", path: "app.py", kind: "file", size: 120, modified: "2026-10-04T09:00:00Z" },
        { name: "README.md", path: "README.md", kind: "file", size: 900, modified: "2026-10-04T09:30:00Z" },
        { name: "data.bin", path: "data.bin", kind: "file", size: 4096, modified: null },
        { name: "report", path: "report", kind: "file", size: 30, modified: null },
        { name: "latest", path: "latest", kind: "link", size: null, modified: null },
      ],
      total: 6,
      truncated: false,
    },
    files_url: "/files?context=c1&path=project",
    server: { version: "1.2.3", commit: "abcdef0" },
    ...overrides,
  };
}

const T1_START = Date.parse("2026-10-04T10:00:00Z") / 1000;

/**
 * One chat as GET /api/projects/{id}/map/chats/{chat_id} answers for it.
 *
 * `main` → scout → grand is a real delegation chain: every `agent` step is a
 * parent of the steps it ran, and the whole trace belongs to the run `turn:t1`.
 */
function sampleDetail(overrides = {}) {
  return {
    chat: { id: "c1", title: "Chat one", href: "/?conversation=c1" },
    messages: {
      items: [
        { id: "m1", role: "user", time: "2026-10-04T10:00:00Z", kind: "message", preview: "please draw it", has_image: false, turn_id: "t1" },
        { id: "m2", role: "assistant", time: "2026-10-04T10:00:05Z", kind: "message", preview: "the map is drawn", has_image: false, turn_id: "t1" },
        { id: "m3", role: "assistant", time: "2026-10-04T10:05:00Z", kind: "message", preview: "another turn", has_image: true, turn_id: "t2" },
      ],
      total: 3,
      truncated: false,
    },
    executions: {
      items: [
        { id: "turn:t1", turn_id: "t1", agent: "main", status: "done", start_time: T1_START, end_time: T1_START + 10, duration_ms: 10000, error: false },
        { id: "turn:t2", turn_id: "t2", agent: "unknown", status: "running", start_time: null, end_time: null, duration_ms: null, error: false },
      ],
      total: 2,
      truncated: false,
    },
    operations: {
      items: [
        { id: "op-read", execution_id: "turn:t1", parent_id: null, kind: "tool", title: "read the map", status: "done", at_ms: 0, duration_ms: 5, files: ["src/app.py", "README.md", "ghost/x.py", "/etc/passwd", ".env"], summary: "" },
        { id: "op-main-agent", execution_id: "turn:t1", parent_id: null, kind: "agent", title: "scout", status: "done", at_ms: 10, duration_ms: 100, files: [], summary: "" },
        { id: "op-scout-read", execution_id: "turn:t1", parent_id: "op-main-agent", kind: "tool", title: "scout read", status: "done", at_ms: 20, duration_ms: 5, files: ["README.md"], summary: "" },
        { id: "op-scout-agent", execution_id: "turn:t1", parent_id: "op-main-agent", kind: "agent", title: "grand", status: "done", at_ms: 30, duration_ms: 50, files: [], summary: "" },
        { id: "op-grand-read", execution_id: "turn:t1", parent_id: "op-scout-agent", kind: "tool", title: "grand read", status: "done", at_ms: 40, duration_ms: 5, files: ["src/app.py"], summary: "" },
        { id: "op-msg", execution_id: "turn:t1", parent_id: "op-read", kind: "message", title: "reported", status: "done", at_ms: 50, duration_ms: null, files: [], summary: "the map is drawn" },
        { id: "op-unknown-run", execution_id: "turn:absent", parent_id: null, kind: "tool", title: "ghost run", status: "done", at_ms: 0, duration_ms: null, files: [], summary: "" },
        { id: "op-bad-files", execution_id: "turn:t1", parent_id: null, kind: "tool", title: "touched", status: "done", at_ms: 60, duration_ms: null, files: ["ghost/x.py", "../escape"], summary: "" },
      ],
      total: 8,
      truncated: false,
    },
    ...overrides,
  };
}

const sceneOf = (map = sampleMap(), options = {}) => createScene(map, options);
const detailScene = () => createScene(sampleMap(), { chatDetails: { c1: sampleDetail() } });
const nodeById = (scene, id) => scene.nodes.find((node) => node.id === id);
const idsOf = (scene, kind) => scene.nodes.filter((node) => node.kind === kind).map((node) => node.id);
const edgeSet = (scene) => scene.edges.map((edge) => `${edge.kind}:${edge.from}->${edge.to}`);
const positionsOf = (scene) =>
  Object.fromEntries(scene.nodes.map((node) => [node.id, [...node.position]]));

/* ------------------------------------------------------------------------- */
/* The canonical hierarchy                                                    */
/* ------------------------------------------------------------------------- */

test("the map alone gives one project, its collection platforms and their items", () => {
  const scene = sceneOf();
  assert.equal(scene.version, SCENE_VERSION);
  assert.equal(scene.projectId, "p1");

  assert.deepEqual(idsOf(scene, "project"), ["project:p1"]);
  assert.deepEqual(idsOf(scene, "collection").sort(), [
    "collection:p1:chats",
    "collection:p1:files",
    "collection:p1:work",
  ]);
  assert.deepEqual(idsOf(scene, "task").sort(), ["task:bd-1", "task:bd-2", "task:bd-3", "task:bd-4"]);
  assert.deepEqual(idsOf(scene, "chat").sort(), ["chat:c1", "chat:c2"]);
  assert.deepEqual(idsOf(scene, "file").sort(), ["file:README.md", "file:app.py", "file:data.bin", "file:latest", "file:report", "file:src"]);
  assert.deepEqual(idsOf(scene, "message"), [], "no detail, no message");
  assert.deepEqual(idsOf(scene, "execution"), [], "no detail, no execution");
  assert.deepEqual(idsOf(scene, "operation"), [], "no detail, no operation");

  // The map's own roots are real: the project owns every collection, and each
  // collection owns exactly its branch.
  assert.ok(edgeSet(scene).includes("contains:project:p1->collection:p1:work"));
  assert.ok(edgeSet(scene).includes("contains:project:p1->collection:p1:chats"));
  assert.ok(edgeSet(scene).includes("contains:project:p1->collection:p1:files"));
  assert.ok(edgeSet(scene).includes("contains:collection:p1:work->task:bd-1"));
  assert.ok(edgeSet(scene).includes("contains:collection:p1:chats->chat:c1"));
  assert.ok(edgeSet(scene).includes("contains:collection:p1:files->file:src"));
});

test("there is no context island, no process and no activity kind left", () => {
  const scene = sceneOf();
  for (const node of scene.nodes) {
    assert.ok(NODE_KINDS.includes(node.kind), `${node.id} has a canonical kind`);
    assert.ok(!["context", "process", "activity"].includes(node.kind), `${node.id} is not a legacy kind`);
    assert.ok(!node.id.startsWith("context:"));
  }
  // The map still carries `activity.contexts`: it is ignored, not promoted.
  assert.equal(JSON.stringify(scene).includes('"context:'), false);
  for (const edge of scene.edges) {
    assert.ok(EDGE_KINDS.includes(edge.kind), `${edge.id} has a canonical edge kind`);
    assert.ok(!["delegation", "activity"].includes(edge.kind));
  }
});

test("the promised node shape: unique ids, finite geometry, honest dimensions", () => {
  const scene = detailScene();
  const ids = scene.nodes.map((node) => node.id);
  assert.equal(new Set(ids).size, ids.length, "no two nodes share an id");
  for (const node of scene.nodes) {
    assert.equal(node.id, nodeId(node.kind, node.id.slice(node.kind.length + 1)));
    assert.equal(node.position.length, 3);
    assert.equal(node.size.length, 3);
    assert.ok(node.position.every(Number.isFinite), `${node.id} has finite coordinates`);
    assert.ok(node.size.every((part) => Number.isFinite(part) && part > 0));
    assert.equal(node.dimensions.type, node.kind);
    assert.ok(node.item && typeof node.item === "object", "the safe answer object is kept");
    assert.ok(!("agent" in node), "an agent is a dimension, never a top-level invention");
    assert.ok(node.semantic.label);
  }
  const edgeIds = scene.edges.map((edge) => edge.id);
  assert.equal(new Set(edgeIds).size, edgeIds.length, "edges are deduplicated");
  for (const edge of scene.edges) {
    assert.ok(ids.includes(edge.from), `${edge.id}'s from is a real node`);
    assert.ok(ids.includes(edge.to), `${edge.id}'s to is a real node`);
  }
});

test("a chat detail hangs messages, executions and operations under its chat", () => {
  const scene = detailScene();
  assert.deepEqual(idsOf(scene, "message").sort(), ["message:c1:m1", "message:c1:m2", "message:c1:m3"]);
  assert.deepEqual(idsOf(scene, "execution").sort(), [
    "execution:c1:turn:t1",
    "execution:c1:turn:t2",
    "execution:sub:c1:op-main-agent",
    "execution:sub:c1:op-scout-agent",
  ]);
  assert.deepEqual(idsOf(scene, "operation").sort(), [
    "operation:c1:op-bad-files",
    "operation:c1:op-grand-read",
    "operation:c1:op-main-agent",
    "operation:c1:op-msg",
    "operation:c1:op-read",
    "operation:c1:op-scout-agent",
    "operation:c1:op-scout-read",
  ]);

  const edges = edgeSet(scene);
  for (const id of ["message:c1:m1", "message:c1:m2", "message:c1:m3", "execution:c1:turn:t1", "execution:c1:turn:t2"]) {
    assert.ok(edges.includes(`contains:chat:c1->${id}`), `${id} hangs under its chat`);
  }
  for (const id of ["operation:c1:op-read", "operation:c1:op-main-agent", "operation:c1:op-bad-files", "operation:c1:op-msg"]) {
    assert.ok(edges.includes(`operation:execution:c1:turn:t1->${id}`), `${id} belongs to its execution`);
  }
  // An operation of a run the detail does not hold is never invented.
  assert.equal(nodeById(scene, "operation:c1:op-unknown-run"), undefined);
  // An operation's parent is named by the trace only; op-msg is a child of op-read.
  assert.ok(edges.includes("contains-operation:operation:c1:op-read->operation:c1:op-msg"));

  // Detail nodes stand beside and above their owner, never on it.
  const chat = nodeById(scene, "chat:c1");
  const execution = nodeById(scene, "execution:c1:turn:t1");
  const message = nodeById(scene, "message:c1:m1");
  assert.ok(message.position[0] > chat.position[0] + chat.size[0] / 2);
  assert.ok(execution.position[0] < chat.position[0] - chat.size[0] / 2);
  assert.equal(message.position[1], chat.position[1] + DETAIL_Y_STEP);
  assert.equal(execution.position[1], chat.position[1] + DETAIL_Y_STEP);
});

test("detail items are safe projections: no raw metadata, no reasoning body", () => {
  const scene = detailScene();
  const message = nodeById(scene, "message:c1:m2");
  assert.equal(message.item.preview, "the map is drawn");
  assert.equal(message.item.role, "assistant");
  assert.equal(message.item.turn_id, "t1");
  assert.equal(message.state, "assistant");

  const operation = nodeById(scene, "operation:c1:op-msg");
  assert.equal(operation.item.summary, "the map is drawn", "a message operation keeps its own summary");
  assert.deepEqual(Object.keys(operation.item).sort(), [
    "at_ms", "duration_ms", "execution_id", "files", "id", "kind", "parent_id", "status", "summary", "title",
  ]);
  const tool = nodeById(scene, "operation:c1:op-read");
  assert.equal(tool.item.summary, "", "a non-message operation never keeps a body");
  assert.deepEqual(tool.item.files, ["src/app.py", "README.md", "ghost/x.py"], "file refs are sanitized and unique");
});

/* ------------------------------------------------------------------------- */
/* Explicit turns and explicit ownership                                      */
/* ------------------------------------------------------------------------- */

test("only two equal, explicit turn ids join a message to its execution", () => {
  const scene = detailScene();
  const joins = scene.edges.filter((edge) => edge.kind === "message-execution");
  assert.deepEqual(
    joins.map((edge) => [edge.from, edge.to]).sort(),
    [
      ["message:c1:m2", "execution:c1:turn:t1"],
      ["message:c1:m3", "execution:c1:turn:t2"],
    ],
    "each assistant message joins the run of its own explicit turn",
  );
  assert.equal(
    joins.some((edge) => edge.from === "message:c1:m1"),
    false,
    "a user message is never guessed into the run",
  );
  assert.deepEqual(relatedIds(scene, "message:c1:m2"), [
    "message:c1:m2",
    "chat:c1",
    "execution:c1:turn:t1",
  ]);
});

test("an agent operation opens a nested execution that owns its child operations", () => {
  const scene = detailScene();
  const agent = nodeById(scene, "operation:c1:op-main-agent");
  const sub = nodeById(scene, "execution:sub:c1:op-main-agent");
  assert.ok(agent && sub, "the delegation is a real operation and a real execution");
  assert.equal(agent.item.kind, "agent");
  assert.equal(sub.state, "done");

  const edges = edgeSet(scene);
  assert.ok(edges.includes("delegates:operation:c1:op-main-agent->execution:sub:c1:op-main-agent"));
  assert.ok(edges.includes("operation:execution:sub:c1:op-main-agent->operation:c1:op-scout-read"));
  assert.ok(edges.includes("contains-operation:operation:c1:op-main-agent->operation:c1:op-scout-read"));

  // The nested execution stands above the agent operation it belongs to.
  assert.ok(sub.position[0] < agent.position[0]);
  assert.equal(sub.position[1], agent.position[1] + DETAIL_Y_STEP);
  const child = nodeById(scene, "operation:c1:op-scout-read");
  assert.equal(child.position[1], sub.position[1] + DETAIL_Y_STEP);

  // A nested agent repeats: grand opens its own execution, owning its own step.
  assert.ok(edges.includes("delegates:operation:c1:op-scout-agent->execution:sub:c1:op-scout-agent"));
  assert.ok(edges.includes("operation:execution:sub:c1:op-scout-agent->operation:c1:op-grand-read"));
  assert.ok(edges.includes("contains-operation:operation:c1:op-scout-agent->operation:c1:op-grand-read"));
  assert.deepEqual(relatedIds(scene, "operation:c1:op-main-agent"), [
    "operation:c1:op-main-agent",
    "execution:c1:turn:t1",
    "execution:sub:c1:op-main-agent",
    "operation:c1:op-scout-agent",
    "operation:c1:op-scout-read",
  ]);
});

test("an operation's time is its run's start plus its elapsed offset", () => {
  const scene = detailScene();
  const run = nodeById(scene, "execution:c1:turn:t1");
  assert.equal(run.time, "2026-10-04T10:00:00.000Z");
  assert.equal(run.position[2], (Date.parse(run.time) - Date.UTC(2020, 0, 1)) / 86400000);

  const read = nodeById(scene, "operation:c1:op-read");
  assert.equal(read.time, "2026-10-04T10:00:00.000Z", "at_ms 0 is the run's own start");
  const later = nodeById(scene, "operation:c1:op-msg");
  assert.equal(later.time, "2026-10-04T10:00:00.050Z");
  assert.ok(later.position[2] > read.position[2], "a later step stands later");

  const running = nodeById(scene, "execution:c1:turn:t2");
  assert.equal(running.time, null, "a run with no start has no moment");
  assert.equal(running.position[2], UNKNOWN_Z);
});

/* ------------------------------------------------------------------------- */
/* Honest edges                                                               */
/* ------------------------------------------------------------------------- */

test("a reference is only drawn to a file the answer really shows", () => {
  const scene = detailScene();
  const references = scene.edges.filter((edge) => edge.kind === "reference");
  assert.deepEqual(
    references.map((edge) => [edge.from, edge.to, edge.item.path, edge.item.match]).sort(),
    [
      ["operation:c1:op-grand-read", "file:src", "src/app.py", "src"],
      ["operation:c1:op-read", "file:README.md", "README.md", "README.md"],
      ["operation:c1:op-read", "file:src", "src/app.py", "src"],
      ["operation:c1:op-scout-read", "file:README.md", "README.md", "README.md"],
    ],
    "an exact file and a visible top-level ancestor, each once; nothing unseen",
  );
  assert.equal(references.some((edge) => String(edge.item?.path ?? "").includes("ghost")), false);
  assert.equal(references.some((edge) => String(edge.item?.path ?? "").includes("..")), false);
  const touched = nodeById(scene, "operation:c1:op-bad-files");
  assert.equal(scene.edges.some((edge) => edge.from === touched.id && edge.kind === "reference"), false);
});

test("task links become typed edges, and a missing task becomes none", () => {
  const scene = sceneOf();
  const edges = edgeSet(scene);
  assert.ok(edges.includes("turn:chat:c1->task:bd-1"));
  assert.ok(edges.includes("plan:chat:c1->task:bd-2"));
  assert.equal(edges.filter((edge) => edge.includes("missing-task")).length, 0);
  assert.ok(edges.includes("depends:task:bd-2->task:bd-1"));
  assert.equal(scene.edges.filter((edge) => edge.kind === "depends").length, 1);
  assert.deepEqual(relatedIds(scene, "task:bd-2"), ["task:bd-2", "chat:c1", "collection:p1:work", "task:bd-1"]);
});

test("unknown ids never invent a node or a link", () => {
  const detail = sampleDetail();
  detail.messages.items.push({ role: "assistant", time: "2026-10-04T10:09:00Z", preview: "no id" }, null, "nope");
  detail.executions.items.push({ turn_id: "t9", agent: "x" });
  detail.operations.items.push(
    { id: "op-no-run", execution_id: null, kind: "tool", title: "unowned", at_ms: 0, files: [] },
    { id: "op-parent-missing", execution_id: "turn:t1", parent_id: "absent-op", kind: "tool", title: "orphan", at_ms: 70, files: [] },
  );

  const scene = createScene(sampleMap(), { chatDetails: { c1: detail, absent: sampleDetail() } });
  assert.deepEqual(idsOf(scene, "message").sort(), ["message:c1:m1", "message:c1:m2", "message:c1:m3"]);
  assert.deepEqual(idsOf(scene, "execution").sort(), [
    "execution:c1:turn:t1",
    "execution:c1:turn:t2",
    "execution:sub:c1:op-main-agent",
    "execution:sub:c1:op-scout-agent",
  ]);
  const orphan = nodeById(scene, "operation:c1:op-parent-missing");
  assert.ok(orphan, "an operation of a known run is still shown");
  assert.equal(
    scene.edges.some((edge) => edge.kind === "contains-operation" && edge.to === orphan.id),
    false,
    "a parent the answer does not hold is no edge",
  );
  assert.equal(nodeById(scene, "operation:c1:op-no-run"), undefined);
  // A detail for a chat the map does not hold adds nothing at all.
  assert.equal(scene.nodes.some((node) => node.id.includes("absent")), false);
});

/* ------------------------------------------------------------------------- */
/* Stability of the axes                                                      */
/* ------------------------------------------------------------------------- */

test("loading a chat's detail moves none of the map's own nodes", () => {
  const before = positionsOf(sceneOf());
  const after = positionsOf(detailScene());
  for (const [id, position] of Object.entries(before)) {
    assert.deepEqual(after[id], position, `${id} did not move`);
  }
});

test("a task keeps its place when its state, steps and updated_at change", () => {
  const before = positionsOf(sceneOf());
  const changed = sampleMap();
  const task = changed.tasks.items.find((item) => item.id === "bd-2");
  task.state = "closed";
  task.status = "closed";
  task.updated_at = "2026-10-20T00:00:00Z";
  const chat = changed.processes.items.find((item) => item.chat_id === "c1");
  chat.steps = ["a", "b", "c", "d"];
  chat.updated_at = "2026-10-21T00:00:00Z";
  const after = positionsOf(sceneOf(changed));
  for (const id of idsOf(sceneOf(), "task")) {
    assert.deepEqual(after[id], before[id], `${id} did not move`);
  }
});

test("a new task takes the next free slot and moves nothing older", () => {
  const before = positionsOf(sceneOf());
  const appended = sampleMap();
  appended.tasks.items.push({
    id: "bd-5", title: "Delta", status: "open", state: "ready", priority: 1, type: "task",
    assignee: null, created_at: "2026-10-06T09:00:00Z", updated_at: "2026-10-06T09:00:00Z",
    depends_on: [], close_reason: null,
  });
  const scene = sceneOf(appended);
  const after = positionsOf(scene);
  for (const [id, position] of Object.entries(before)) {
    assert.deepEqual(after[id], position, `${id} did not move`);
  }
  const newest = nodeById(scene, "task:bd-5");
  assert.equal(newest.position[1], SEMANTIC_Y.intention);
  assert.ok(newest.position[0] > nodeById(scene, "task:bd-3").position[0], "the newest task is the last in its street");
});

test("Y is the hierarchy level and Z is the real moment the answer names", () => {
  const scene = detailScene();
  const expected = { project: 200, collection: SEMANTIC_Y.structure, task: SEMANTIC_Y.intention, chat: SEMANTIC_Y.work, file: SEMANTIC_Y.artifact };
  for (const node of scene.nodes) {
    if (node.kind in expected) assert.equal(node.position[1], expected[node.kind]);
    assert.ok(["structure", "intention", "work", "artifact"].includes(node.semantic.level));
  }
  assert.equal(nodeById(scene, "project:p1").semantic.level, "structure");
  assert.equal(nodeById(scene, "task:bd-1").semantic.level, "intention");
  assert.equal(nodeById(scene, "operation:c1:op-read").semantic.level, "work");

  assert.equal(nodeById(scene, "task:bd-1").time, "2026-10-01T09:00:00.000Z");
  assert.equal(nodeById(scene, "chat:c1").time, "2026-10-04T10:00:00.000Z");
  assert.equal(nodeById(scene, "file:app.py").time, "2026-10-04T09:00:00.000Z");
  for (const id of ["task:bd-4", "chat:c2", "file:data.bin"]) {
    assert.equal(nodeById(scene, id).time, null, `${id} has no time`);
    assert.equal(nodeById(scene, id).position[2], UNKNOWN_Z);
  }
  assert.ok(!scene.nodes.some((node) => Number.isNaN(node.position[2])));
});

test("the same answers always yield the same scene", () => {
  const once = createScene(sampleMap(), { chatDetails: { c1: sampleDetail() } });
  const twice = createScene(clone(sampleMap()), { chatDetails: { c1: clone(sampleDetail()) } });
  assert.deepEqual(once, twice);
  assert.equal(JSON.stringify(once), JSON.stringify(twice));
});

/* ------------------------------------------------------------------------- */
/* Slices, selection and the legacy expansion                                 */
/* ------------------------------------------------------------------------- */

test("a slice filters by level, by an inclusive window and by title, without mutating", () => {
  const scene = detailScene();
  const frozen = clone(scene);

  const tasksOnly = sliceScene(scene, { levels: ["intention"] });
  assert.deepEqual(tasksOnly.nodes.map((node) => node.id).sort(), ["task:bd-1", "task:bd-2", "task:bd-3", "task:bd-4"]);
  assert.deepEqual(edgeSet(tasksOnly), ["depends:task:bd-2->task:bd-1"]);

  const chatOnly = sliceScene(scene, { levels: ["work"] });
  assert.ok(chatOnly.nodes.every((node) => node.semantic.level === "work"));
  assert.ok(chatOnly.nodes.some((node) => node.kind === "operation"));

  const exactly = sliceScene(scene, { from: "2026-10-02T09:00:00Z", to: "2026-10-02T09:00:00Z" });
  assert.deepEqual(exactly.nodes.map((node) => node.id), ["task:bd-2"], "from and to are inclusive");

  const undated = sliceScene(scene, { from: "2020-01-01T00:00:00Z" });
  assert.ok(!undated.nodes.some((node) => node.time === null), "an unknown time never claims a window");
  assert.ok(!undated.nodes.some((node) => node.id === "task:bd-4"));

  const found = sliceScene(scene, { query: "  beta " });
  assert.deepEqual(found.nodes.map((node) => node.id), ["task:bd-2"]);
  assert.equal(found.edges.length, 0, "its only neighbour is filtered out");

  const steps = sliceScene(scene, { query: "scout" });
  assert.ok(steps.nodes.some((node) => node.id === "operation:c1:op-scout-read"));
  const kept = new Set(steps.nodes.map((node) => node.id));
  assert.ok(steps.edges.every((edge) => kept.has(edge.from) && kept.has(edge.to)));

  const empty = sliceScene(scene, { levels: ["intention"], query: "nothing matches this" });
  assert.deepEqual(empty.nodes, []);
  assert.deepEqual(empty.bounds, { min: [0, 0, 0], max: [0, 0, 0] });
  assert.deepEqual(empty.timeRange, { from: null, to: null });

  assert.deepEqual(scene, frozen, "the original scene is untouched");
});

test("relatedIds walks every canonical relation, including a detail not loaded", () => {
  const map = sceneOf();
  assert.deepEqual(relatedIds(map, "chat:c1"), ["chat:c1", "collection:p1:chats", "task:bd-1", "task:bd-2"]);
  assert.deepEqual(relatedIds(map, "task:bd-3"), ["task:bd-3", "collection:p1:work"]);
  assert.deepEqual(relatedIds(map, "collection:p1:files").slice(0, 2), ["collection:p1:files", "file:README.md"]);
  assert.deepEqual(relatedIds(map, "nope:1"), []);

  const scene = detailScene();
  const chat = relatedIds(scene, "chat:c1");
  for (const id of ["message:c1:m1", "execution:c1:turn:t1", "task:bd-1"]) assert.ok(chat.includes(id));
  assert.deepEqual(relatedIds(scene, "execution:c1:turn:t1"), [
    "execution:c1:turn:t1",
    "chat:c1",
    "message:c1:m2",
    "operation:c1:op-bad-files",
    "operation:c1:op-main-agent",
    "operation:c1:op-msg",
    "operation:c1:op-read",
  ]);
  assert.deepEqual(relatedIds(scene, "chat:c1"), relatedIds(scene, "chat:c1"));
});

test("expandActivity is a no-op: a chat's steps come only from its loaded detail", () => {
  const scene = sceneOf();
  assert.equal(expandActivity(scene, "c1"), scene, "the same scene object, with nothing invented");
  assert.equal(expandActivity(null, "c1").nodes.length, 0);
  assert.deepEqual(expandActivity(scene, "c1").nodes.map((node) => node.id), scene.nodes.map((node) => node.id));
});

/* ------------------------------------------------------------------------- */
/* Empty, broken and large answers                                            */
/* ------------------------------------------------------------------------- */

test("an empty or broken answer still gives a scene without NaN", () => {
  for (const answer of [{}, { tasks: null, processes: {}, files: [] }, null]) {
    const scene = createScene(answer);
    assert.deepEqual(scene.nodes, []);
    assert.deepEqual(scene.edges, []);
    assert.deepEqual(scene.bounds, { min: [0, 0, 0], max: [0, 0, 0] });
    assert.deepEqual(scene.timeRange, { from: null, to: null });
    assert.deepEqual(sliceScene(scene, { levels: ["work"] }).nodes, []);
    assert.deepEqual(relatedIds(scene, "task:any"), []);
    const stats = sceneStats(scene);
    assert.equal(stats.nodes, 0);
    assert.equal(stats.edges, 0);
  }
  // A project id is the whole hierarchy's root, even when no branch has items.
  const bare = createScene({ project: { id: "p9" } });
  assert.deepEqual(bare.nodes.map((node) => node.id).sort(), [
    "collection:p9:chats",
    "collection:p9:files",
    "project:p9",
  ]);
  for (const node of bare.nodes) assert.ok(node.position.every(Number.isFinite));
});

test("300 tasks, 50 chats, 100 files and one chat's detail stay finite and counted", () => {
  const map = sampleMap();
  const day = 24 * 60 * 60 * 1000;
  const start = Date.parse("2026-01-01T00:00:00Z");

  map.tasks.items = Array.from({ length: 300 }, (_, index) => ({
    id: `bd-${String(index).padStart(3, "0")}`,
    title: `Task ${index}`,
    status: index % 7 === 0 ? "closed" : "open",
    state: index % 3 === 0 ? "in_progress" : "ready",
    priority: index % 4,
    type: "task",
    assignee: index % 5 === 0 ? "me@example.test" : null,
    created_at: new Date(start + index * (day / 4)).toISOString(),
    updated_at: new Date(start + index * day).toISOString(),
    depends_on: index > 0 && index % 10 === 0 ? [`bd-${String(index - 1).padStart(3, "0")}`] : [],
    close_reason: null,
  }));
  map.processes.items = Array.from({ length: 50 }, (_, index) => ({
    chat_id: `c${String(index).padStart(2, "0")}`,
    title: `Chat ${index}`,
    updated_at: new Date(start + index * day).toISOString(),
    active: index % 2 === 0,
    awaiting_approval: index % 5 === 0,
    steps: [],
    activity: { items: [], total: 0, truncated: false },
    task_links: [{ task_id: "bd-000", relation: "turn" }],
    task_links_total: 1,
  }));
  map.files.items = Array.from({ length: 100 }, (_, index) => ({
    name: `file-${index}.${index % 3 === 0 ? "md" : index % 3 === 1 ? "bin" : "log"}`,
    path: `file-${index}.${index % 3 === 0 ? "md" : index % 3 === 1 ? "bin" : "log"}`,
    kind: index % 17 === 0 ? "dir" : "file",
    size: index,
    modified: new Date(start + index * day).toISOString(),
  }));

  const room = map.processes.items[0].chat_id;
  const detail = {
    chat: { id: room, title: "Chat 0", href: `/?conversation=${room}` },
    messages: {
      items: Array.from({ length: 10 }, (_, index) => ({
        id: `m${index}`, role: index % 2 ? "assistant" : "user",
        time: new Date(start + index * 1000).toISOString(), kind: "message",
        preview: `message ${index}`, has_image: false, turn_id: "t0",
      })),
      total: 10, truncated: false,
    },
    executions: {
      items: [{
        id: "turn:t0", turn_id: "t0", agent: "main", status: "done",
        start_time: start / 1000, end_time: start / 1000 + 20, duration_ms: 20000, error: false,
      }],
      total: 1, truncated: false,
    },
    operations: {
      items: Array.from({ length: 20 }, (_, index) => ({
        id: `op${index}`, execution_id: "turn:t0", parent_id: index % 5 === 0 && index > 0 ? `op${index - 1}` : null,
        kind: index === 4 ? "agent" : "tool", title: `step ${index}`, status: "done",
        at_ms: index * 10, duration_ms: 5, files: index === 0 ? ["file-0.md"] : [], summary: "",
      })),
      total: 20, truncated: false,
    },
  };

  const scene = createScene(map, { chatDetails: { [room]: detail } });
  const stats = sceneStats(scene);

  assert.equal(stats.kinds.project, 1);
  assert.equal(stats.kinds.collection, 3);
  assert.equal(stats.kinds.task, 300);
  assert.equal(stats.kinds.chat, 50);
  assert.equal(stats.kinds.file, 100);
  assert.equal(stats.kinds.message, 10);
  assert.equal(stats.kinds.operation, 20);
  assert.equal(stats.kinds.execution, 1 + 1, "the run and the agent step's nested execution");
  assert.equal(stats.edgeKinds.depends, 29, "every tenth task depends on the one before it");
  assert.equal(stats.edgeKinds.turn, 50);
  assert.ok(stats.edgeKinds.contains >= 3 + 300 + 50 + 100);

  for (const node of scene.nodes) {
    assert.ok(node.position.every(Number.isFinite), `${node.id} is finite`);
  }
  for (const axis of [0, 1, 2]) {
    assert.ok(Number.isFinite(scene.bounds.min[axis]));
    assert.ok(Number.isFinite(scene.bounds.max[axis]));
    assert.ok(scene.bounds.max[axis] >= scene.bounds.min[axis]);
  }
  assert.equal(scene.timeRange.from, start);
  assert.ok(scene.timeRange.to > scene.timeRange.from);

  const window = sliceScene(scene, { from: "2026-01-10T00:00:00Z", to: "2026-01-20T00:00:00Z" });
  assert.ok(window.nodes.length > 0 && window.nodes.length < scene.nodes.length);
  assert.ok(window.nodes.every((node) => node.time !== null));
  assert.ok(window.bounds.min.every(Number.isFinite));
});
