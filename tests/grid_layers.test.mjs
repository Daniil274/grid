/**
 * The mobile Layers and Timeline projections (web_chat/js/grid/layers.js).
 *
 * The promises kept here: four tabs are exactly Project / Files / Chats / Work;
 * the selected chat opens its canonical Messages / Executions tree, operations
 * hang under the execution that owns them, and an `agent` operation opens the
 * child execution it delegated to; a row selects the exact scene id and keeps a
 * real Open link; the Timeline groups real moments and keeps Unknown time
 * apart; a 100-row cap is honest; and the module never uses innerHTML, WebGL or
 * the network.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const {
  CHAT_BUCKETS,
  createProjectionView,
  formatMoment,
  MAX_CHIPS,
  mergeChatOrder,
  NO_TURN_LABEL,
  orderChatsByActivity,
  ROW_LIMIT,
  SHOW_MORE_STEP,
  TABS,
  KIND_SPEC,
} = await import("../web_chat/js/grid/layers.js");
const { createScene } = await import("../web_chat/js/grid/scene.js");

const layerSource = readFileSync(fileURLToPath(new URL("../web_chat/js/grid/layers.js", import.meta.url)), "utf8");
const css = readFileSync(fileURLToPath(new URL("../web_chat/styles/grid.css", import.meta.url)), "utf8");

function sampleMap() {
  return {
    project: { id: "p1", title: "Grid" },
    files_url: "/files?path=project",
    tasks: {
      available: true,
      items: [
        { id: "bd-1", title: "Alpha", state: "ready", status: "open", created_at: "2026-10-01T09:00:00Z", depends_on: [] },
        { id: "bd-2", title: "Beta", state: "in_progress", status: "in_progress", created_at: "2026-10-03T10:00:00Z", depends_on: ["bd-1"] },
      ],
      total: 2,
    },
    processes: {
      available: true,
      items: [
        {
          chat_id: "c1", title: "Chat one", updated_at: "2026-10-03T11:00:00Z",
          active: true, awaiting_approval: false,
          task_links: [{ task_id: "bd-2", relation: "turn" }],
          // Old context shape: the canonical view must not read it as hierarchy.
          activity: {
            items: [{ id: "s1", kind: "tool", title: "Old island", files: [] }],
            contexts: [{ id: "scout", parent_id: null, label: "Scout", total: 1 }],
          },
        },
      ],
      total: 1,
    },
    files: {
      available: true,
      items: [
        { kind: "dir", path: "docs", name: "docs", modified: "2026-10-01T08:00:00Z" },
        { kind: "file", path: "docs/spec.md", name: "spec.md", modified: "2026-10-02T12:00:00Z" },
      ],
    },
  };
}

function sampleDetail() {
  return {
    chat: { id: "c1", title: "Chat one", href: "/?conversation=c1" },
    messages: {
      items: [
        { id: "m1", role: "user", time: "2026-10-03T10:59:00Z", kind: "prompt", preview: "Map it", turn_id: "turn-1" },
        { id: "m2", role: "assistant", time: "2026-10-03T11:00:00Z", kind: "final", preview: "Here is the map", turn_id: "turn-1" },
      ],
      total: 2, truncated: false,
    },
    executions: { items: [
      { id: "turn:turn-1", turn_id: "turn-1", agent: "main", status: "done", start_time: 1759498800, end_time: 1759498805, duration_ms: 5000, error: false },
    ], total: 1, truncated: false },
    operations: {
      items: [
        { id: "op-read", execution_id: "turn:turn-1", parent_id: null, kind: "tool", title: "Read the map", status: "done", at_ms: 100, duration_ms: 20, files: ["docs/spec.md"] },
        { id: "op-agent", execution_id: "turn:turn-1", parent_id: null, kind: "agent", title: "Scout", status: "done", at_ms: 200, duration_ms: 40, files: [] },
        { id: "op-child", execution_id: "turn:turn-1", parent_id: "op-agent", kind: "tool", title: "Child read", status: "done", at_ms: 210, duration_ms: 10, files: [] },
      ],
      total: 3, truncated: false,
    },
  };
}

function sceneWithDetail(map = sampleMap()) {
  return createScene(map, { projectId: "p1", chatDetails: { c1: sampleDetail() } });
}

function manyTasks(count) {
  const items = [];
  for (let index = 0; index < count; index += 1) {
    const day = String((index % 28) + 1).padStart(2, "0");
    items.push({ id: `bd-${String(index).padStart(3, "0")}`, title: `Task ${index}`, state: index % 2 ? "open" : "in_progress", created_at: `2026-09-${day}T09:00:00Z`, depends_on: [] });
  }
  return { project: { id: "p1", title: "Grid" }, tasks: { available: true, items }, processes: { items: [] }, files: { items: [] } };
}

function mount(scene, options = {}) {
  const container = document.createElement("div");
  document.body.append(container);
  const view = createProjectionView(container, { scene, ...options });
  return { container, view };
}

const rows = (container) => container.querySelectorAll(".gridProjection__row");
const rowIds = (container) => rows(container).map((row) => row.getAttribute("data-id"));
const tabs = (container) => container.querySelectorAll(".gridProjection__tab");
const tabById = (container, id) => tabs(container).find((tab) => tab.getAttribute("data-tab") === id);
const click = (node) => { assert.ok(node, "expected a clickable element"); node.click(); };

test("the four tabs are exactly Project, Files, Chats and Work", () => {
  const { container, view } = mount(sceneWithDetail());
  assert.deepEqual(TABS.map((tab) => tab.label), ["Project", "Files", "Chats", "Work"]);
  assert.deepEqual(tabs(container).map((tab) => tab.getAttribute("data-tab")), ["project", "files", "chats", "work"]);
  assert.equal(tabById(container, "chats").getAttribute("aria-selected"), "true");
  assert.equal(tabById(container, "work").getAttribute("data-count"), "2");
  assert.equal(tabById(container, "files").getAttribute("data-count"), "2");
  assert.equal(view.getState().tab, "chats");
  assert.equal(tabs(container).some((tab) => /agents|activity|context/i.test(tab.textContent)), false);
});

test("a tab click changes the projection, and Work lists the real tasks", () => {
  const { container, view } = mount(sceneWithDetail());
  click(tabById(container, "work"));
  assert.equal(view.getState().tab, "work");
  assert.deepEqual(rowIds(container).sort(), ["task:bd-1", "task:bd-2"]);
  click(tabById(container, "files"));
  assert.deepEqual(rowIds(container).sort(), ["file:docs", "file:docs/spec.md"]);
  click(tabById(container, "project"));
  assert.ok(rowIds(container).includes("project:p1"));
  assert.ok(rowIds(container).some((id) => id.startsWith("collection:p1:")));
});

test("a selected chat opens Messages, Executions and Operations with exact ownership", () => {
  const { container, view } = mount(sceneWithDetail(), { selectedId: "chat:c1" });
  const ids = rowIds(container);
  assert.equal(ids[0], "chat:c1");
  assert.ok(ids.includes("message:c1:m1"));
  assert.ok(ids.includes("message:c1:m2"));
  assert.ok(ids.includes("execution:c1:turn:turn-1"));
  assert.ok(ids.includes("operation:c1:op-read"));
  assert.ok(ids.includes("operation:c1:op-agent"));
  assert.ok(ids.includes("execution:sub:c1:op-agent"), "an agent operation opens a child execution");
  assert.equal(ids.filter((id) => id === "operation:c1:op-child").length, 1, "a child operation is drawn once, under its child execution");
  const childExec = rows(container).find((row) => row.getAttribute("data-id") === "execution:sub:c1:op-agent");
  assert.equal(childExec.getAttribute("data-parent"), "operation:c1:op-agent");
  const childOp = rows(container).find((row) => row.getAttribute("data-id") === "operation:c1:op-child");
  assert.equal(childOp.getAttribute("data-parent"), "execution:sub:c1:op-agent");
  assert.equal(view.getState().selectedId, "chat:c1");
});

test("clicking a row selects the exact canonical scene id", () => {
  const picked = [];
  const { container } = mount(sceneWithDetail(), { selectedId: "chat:c1", onSelect: (id) => picked.push(id) });
  click(rows(container).find((row) => row.getAttribute("data-id") === "operation:c1:op-child"));
  assert.deepEqual(picked, ["operation:c1:op-child"]);
  assert.match(container.textContent, /Open/);
  const opened = container.findAll("gridProjection__open").map((link) => link.getAttribute("href"));
  assert.ok(opened.includes("/?conversation=c1"), "every relevant row keeps the real chat link");
});

test("old context islands are not a tab, a row, or a hierarchy", () => {
  const { container } = mount(sceneWithDetail(), { selectedId: "chat:c1" });
  assert.equal(rows(container).some((row) => /context|activity/.test(row.getAttribute("data-kind"))), false);
  assert.equal(container.textContent.includes("Old island"), false);
  assert.equal(container.textContent.includes("Scout") && rows(container).some((row) => row.getAttribute("data-kind") === "context"), false);
});

test("the Timeline orders real moments and keeps Unknown time honest", () => {
  const { container, view } = mount(sceneWithDetail(), { mode: "timeline" });
  assert.equal(view.getState().mode, "timeline");
  assert.equal(tabs(container).length, 0, "the Timeline draws the whole scene by moment");
  const groups = container.querySelectorAll(".gridProjection__groupLabel").map((node) => node.textContent);
  assert.ok(groups.includes("Work"));
  assert.ok(groups.includes("Unknown time"), "an undated node is not given a fake moment");
  const unknown = container.querySelectorAll(".gridProjection__group").find((group) => group.textContent.includes("Unknown time"));
  assert.ok(unknown);
  assert.equal(formatMoment(null), "no time");
});

test("a big scene draws ROW_LIMIT rows; Show more adds one honest step", () => {
  const scene = createScene(manyTasks(ROW_LIMIT + 25));
  const { container, view } = mount(scene, { mode: "layers" });
  click(tabById(container, "work"));
  assert.equal(rows(container).length, ROW_LIMIT);
  const more = container.querySelectorAll(".gridProjection__action--more")[0];
  assert.ok(more, "the cap is not silent");
  assert.equal(more.tagName, "BUTTON", "Show more is a real button");
  assert.match(more.textContent, /Show more \(\+25\)/, "it says exactly what one step adds");
  const count = container.find("gridProjection__count");
  assert.equal(count.getAttribute("data-shown"), String(ROW_LIMIT));
  assert.equal(count.getAttribute("data-total"), String(ROW_LIMIT + 25));
  assert.match(count.textContent, new RegExp(`Showing ${ROW_LIMIT} of ${ROW_LIMIT + 25}`));

  click(more);
  assert.equal(rows(container).length, ROW_LIMIT + 25, "the whole scene is drawn once the step covers it");
  assert.equal(container.querySelectorAll(".gridProjection__action--more").length, 0, "nothing is left to show");
  assert.equal(
    container.find("gridProjection__count").textContent,
    `Showing ${ROW_LIMIT + 25} of ${ROW_LIMIT + 25}`,
    "the count tells the truth after the step",
  );
  assert.equal(view.getState().shown, ROW_LIMIT + 25);
  assert.equal(SHOW_MORE_STEP, 100);
});

test("the module uses text nodes only, no innerHTML, WebGL or network", () => {
  assert.doesNotMatch(layerSource, /innerHTML/);
  assert.doesNotMatch(layerSource, /fetch\(|XMLHttpRequest|WebGL|webgl/);
  assert.match(layerSource, /relatedIds/);
  const { container } = mount(sceneWithDetail(), { selectedId: "chat:c1" });
  assert.ok(container.querySelectorAll(".gridProjection__row").length > 0);
});

test("the integration styles every canonical kind and a 44px mobile target", () => {
  for (const kind of ["project", "collection", "task", "chat", "message", "execution", "operation", "file"]) {
    assert.ok(KIND_SPEC[kind], `${kind} has a word and a mark`);
  }
  assert.match(css, /gridProjection__row \{[^}]*min-height: 44px/);
  assert.match(css, /gridProjection__row\[data-depth="2"\]/);
  assert.match(css, /@media/);
});

test("update changes selection and mode without rebuilding, and destroy releases it", () => {
  const { container, view } = mount(sceneWithDetail(), { selectedId: "chat:c1" });
  const same = view.getState();
  view.update({ selectedId: "operation:c1:op-read" });
  assert.equal(view.getState().selectedId, "operation:c1:op-read");
  assert.equal(view.getState().mode, same.mode);
  assert.ok(rows(container).some((row) => row.classList.contains("gridProjection__row--selected")));
  view.destroy();
  assert.equal(container.childNodes.length, 0);
  assert.equal(container.querySelectorAll(".gridProjection").length, 0);
});

/** A map whose chats differ only in activity and in real update moments. */
function activityMap(chats) {
  return {
    project: { id: "p1", title: "Grid" },
    tasks: { available: false, items: [] },
    processes: {
      available: true,
      items: chats.map((chat) => ({
        chat_id: chat.id,
        title: chat.title ?? chat.id,
        updated_at: chat.updated_at,
        active: chat.active === true,
        awaiting_approval: chat.awaiting_approval === true,
        task_links: [],
      })),
      total: chats.length,
    },
    files: { available: true, items: [] },
  };
}

/** The rows of the first drawn list, in drawing order. */
function drawnItems(container, index = 0) {
  const list = container.querySelectorAll(".gridProjection__list")[index];
  if (!list) return [];
  return list.childNodes.filter((node) => node.tagName === "LI");
}

/** Every drawn row, mapped to the explicit turn heading above it. */
function turnOfEachRow(container) {
  const places = new Map();
  let turn = null;
  for (const item of drawnItems(container)) {
    if (item.classList.contains("gridProjection__turn")) {
      turn = item.getAttribute("data-turn");
      continue;
    }
    places.set(item.getAttribute("data-id"), turn);
  }
  return places;
}

test("only a running chat is Now; idle freshness is Recent, and approval is its own bucket", () => {
  const scene = createScene(
    activityMap([
      { id: "c-active", updated_at: "2026-10-01T09:00:00Z", active: true },
      { id: "c-approval", updated_at: "2026-10-03T10:58:00Z", awaiting_approval: true },
      { id: "c-fresh", updated_at: "2026-10-03T10:59:00Z" },
      { id: "c-week", updated_at: "2026-10-01T11:00:00Z" },
      { id: "c-old", updated_at: "2026-08-01T09:00:00Z" },
      { id: "c-none" },
    ]),
    { projectId: "p1" },
  );
  const buckets = orderChatsByActivity(scene.nodes, Date.parse("2026-10-03T11:00:00Z"));
  const labels = buckets.filter((bucket) => bucket.chats.length).map((bucket) => bucket.label);
  assert.deepEqual(labels, ["Now", "Needs attention", "Recent", "Earlier", "Unknown time"]);
  assert.deepEqual(buckets[0].chats.map((chat) => chat.id), ["chat:c-active"], "Now is only what is really running");
  assert.deepEqual(
    buckets[1].chats.map((chat) => chat.id),
    ["chat:c-approval"],
    "waiting for approval is not running, and says so in its own bucket",
  );
  assert.deepEqual(
    buckets[2].chats.map((chat) => chat.id),
    ["chat:c-fresh", "chat:c-week"],
    "an idle chat updated a minute ago is Recent, never Now",
  );
  assert.deepEqual(buckets[3].chats.map((chat) => chat.id), ["chat:c-old"]);
  assert.deepEqual(CHAT_BUCKETS.map((bucket) => bucket.label), [
    "Now",
    "Needs attention",
    "Recent",
    "Earlier",
    "Unknown time",
  ]);

  const { container } = mount(scene);
  const order = rowIds(container);
  assert.deepEqual(order, [
    "chat:c-active",
    "chat:c-approval",
    "chat:c-fresh",
    "chat:c-week",
    "chat:c-old",
    "chat:c-none",
  ]);
  const groups = container.querySelectorAll(".gridProjection__groupLabel").map((node) => node.textContent);
  assert.deepEqual(groups, ["Now", "Needs attention", "Recent", "Earlier", "Unknown time"]);
  const unknown = rows(container).find((row) => row.getAttribute("data-id") === "chat:c-none");
  assert.match(unknown.textContent, /no time/, "an undated chat is never given a borrowed moment");
});

test("an explicit turn groups only messages and runs that name it", () => {
  const detail = {
    chat: { id: "c1", title: "Chat one", href: "/?conversation=c1" },
    messages: {
      items: [
        { id: "m1", role: "user", time: "2026-10-03T10:59:00Z", kind: "prompt", preview: "One", turn_id: "turn-1" },
        { id: "m2", role: "assistant", time: "2026-10-03T11:00:00Z", kind: "final", preview: "Two", turn_id: "turn-1" },
        { id: "m3", role: "user", time: "2026-10-03T11:01:00Z", kind: "prompt", preview: "Three", turn_id: "turn-2" },
        { id: "m4", role: "user", time: "2026-10-03T11:02:00Z", kind: "prompt", preview: "No turn" },
      ],
      total: 4, truncated: false,
    },
    executions: { items: [
      { id: "run-1", turn_id: "turn-1", agent: "main", status: "done", start_time: 1759498800 },
      { id: "run-3", turn_id: "turn-3", agent: "main", status: "done", start_time: 1759498900 },
      { id: "run-bare", agent: "main", status: "done", start_time: 1759499000 },
    ], total: 3, truncated: false },
    operations: { items: [
      { id: "op-read", execution_id: "run-1", parent_id: null, kind: "tool", title: "Read", status: "done", at_ms: 100, duration_ms: 20, files: [] },
      { id: "op-child", execution_id: "run-1", parent_id: "op-read", kind: "tool", title: "Child read", status: "done", at_ms: 110, duration_ms: 10, files: [] },
    ], total: 2, truncated: false },
  };
  const map = {
    project: { id: "p1", title: "Grid" },
    tasks: { available: false, items: [] },
    processes: { available: true, items: [{ chat_id: "c1", title: "Chat one", updated_at: "2026-10-03T11:02:00Z", active: true, task_links: [] }] },
    files: { available: true, items: [] },
  };
  const scene = createScene(map, { projectId: "p1", chatDetails: { c1: detail } });
  const { container } = mount(scene, { selectedId: "chat:c1" });

  const heads = drawnItems(container)
    .filter((item) => item.classList.contains("gridProjection__turn"))
    .map((item) => item.getAttribute("data-turn"));
  assert.deepEqual([...heads].sort(), ["", "turn-1", "turn-2", "turn-3"], "one heading per explicit turn, plus the honest no-turn block");
  assert.equal(new Set(heads).size, heads.length, "no turn heading is drawn twice");

  const places = turnOfEachRow(container);
  assert.equal(places.get("message:c1:m1"), "turn-1");
  assert.equal(places.get("message:c1:m2"), "turn-1");
  assert.equal(places.get("message:c1:m3"), "turn-2");
  assert.equal(places.get("message:c1:m4"), "", "a message without turn_id is never joined to a turn");
  assert.equal(places.get("execution:c1:run-1"), "turn-1");
  assert.equal(places.get("execution:c1:run-3"), "turn-3");
  assert.equal(places.get("execution:c1:run-bare"), "", "a run without turn_id is never joined by timing");
  assert.equal(places.get("operation:c1:op-child"), "turn-1", "the operation stays inside the causal tree of its run");
  assert.match(container.textContent, new RegExp(NO_TURN_LABEL));

  const child = rows(container).find((row) => row.getAttribute("data-id") === "operation:c1:op-child");
  assert.equal(child.getAttribute("data-parent"), "operation:c1:op-read", "a nested operation keeps its causal parent");
});

test("the order is frozen while reading and applied only on demand", () => {
  const before = activityMap([
    { id: "c-old", updated_at: "2026-10-01T09:00:00Z" },
    { id: "c-new", updated_at: "2026-10-03T10:00:00Z" },
  ]);
  const after = activityMap([
    { id: "c-old", updated_at: "2026-10-05T10:00:00Z" },
    { id: "c-new", updated_at: "2026-10-02T10:00:00Z" },
  ]);
  const detail = sampleDetail();
  const first = createScene(before, { projectId: "p1", chatDetails: { "c-old": { ...detail, chat: { id: "c-old", title: "Old", href: "/?conversation=c-old" } } } });
  const second = createScene(after, { projectId: "p1", chatDetails: { "c-old": { ...detail, chat: { id: "c-old", title: "Old", href: "/?conversation=c-old" } } } });
  assert.deepEqual(rowIds(mount(first).container), ["chat:c-new", "chat:c-old"]);

  const { container, view } = mount(first, { selectedId: "chat:c-old" });
  assert.ok(rowIds(container).includes("message:c-old:m1"), "the selected chat is expanded before the update");
  view.update({ scene: second });
  assert.deepEqual(rowIds(container).slice(0, 2), ["chat:c-new", "chat:c-old"], "reading rows do not jump under the reader");
  assert.equal(view.getState().selectedId, "chat:c-old", "the selection survives the update");
  assert.ok(rowIds(container).includes("message:c-old:m1"), "the expansion survives the update");
  assert.equal(view.getState().orderPending, true);
  assert.equal(view.getState().orderMoved, 2);
  const apply = container.find("gridProjection__action--order");
  assert.ok(apply, "the fresh order is offered explicitly");
  assert.match(apply.textContent, /Apply new order/);
  assert.match(container.textContent, /Order kept while you read/);

  click(apply);
  const applied = rowIds(container);
  assert.equal(applied[0], "chat:c-old", "the explicit apply adopts the fresh order");
  assert.ok(applied.indexOf("chat:c-old") < applied.indexOf("chat:c-new"));
  assert.equal(container.find("gridProjection__action--order"), null);
  assert.equal(view.getState().orderPending, false);
});

test("mergeChatOrder keeps known chats put and appends a new one honestly", () => {
  const frozen = [
    { id: "now", ids: ["chat:a", "chat:b"] },
    { id: "recent", ids: ["chat:c"] },
    { id: "earlier", ids: [] },
    { id: "unknown", ids: [] },
  ];
  const fresh = [
    { id: "now", ids: ["chat:b", "chat:a"] },
    { id: "recent", ids: ["chat:d", "chat:c"] },
    { id: "earlier", ids: [] },
    { id: "unknown", ids: [] },
  ];
  const merged = mergeChatOrder(frozen, fresh);
  assert.deepEqual(merged.buckets[0].ids, ["chat:a", "chat:b"]);
  assert.deepEqual(merged.buckets[1].ids, ["chat:c", "chat:d"], "known chats keep the frozen order; a new one is appended");
  assert.deepEqual(merged.added, ["chat:d"], "a new chat appears at its own fresh bucket");
  assert.equal(merged.moved, 3, "every known chat whose place changes is counted");
  assert.equal(merged.changed, true);

  const gone = mergeChatOrder(merged.buckets, [
    { id: "now", ids: ["chat:a", "chat:b"] },
    { id: "recent", ids: ["chat:c", "chat:d"] },
    { id: "earlier", ids: [] },
    { id: "unknown", ids: [] },
  ]);
  assert.equal(gone.changed, false, "the same order is not a pending update");
  assert.deepEqual(gone.added, []);
});

test("a turn reads user message, then executions, then the assistant reply", () => {
  const { container, view } = mount(sceneWithDetail(), { selectedId: "chat:c1" });
  const ids = rowIds(container);
  const asked = ids.indexOf("message:c1:m1");
  const ran = ids.indexOf("execution:c1:turn:turn-1");
  const answered = ids.indexOf("message:c1:m2");
  assert.ok(asked >= 0 && ran >= 0 && answered >= 0);
  assert.ok(asked < ran, "the explicit user message opens its turn");
  assert.ok(ran < answered, "the assistant reply comes after the run it answers");
  // The overview is newest-first here, and the inside of a turn is still a
  // story: a reply above its question would tell it backwards.
  assert.equal(view.getState().order, "newest");
});

test("operations inside one execution always read forward, even in newest-first", () => {
  const { container, view } = mount(sceneWithDetail(), { selectedId: "chat:c1" });
  const ids = rowIds(container);
  assert.ok(
    ids.indexOf("operation:c1:op-read") < ids.indexOf("operation:c1:op-agent"),
    "the earlier step is drawn above the later one",
  );
  assert.equal(view.getState().order, "newest", "the overview above is newest-first");
  assert.ok(ids.indexOf("operation:c1:op-agent") < ids.indexOf("operation:c1:op-child"));
});

test("mergeChatOrder counts a bucket change even when the flat index stays", () => {
  const frozen = [
    { id: "now", ids: [] },
    { id: "attention", ids: [] },
    { id: "recent", ids: ["chat:b"] },
    { id: "earlier", ids: [] },
    { id: "unknown", ids: [] },
  ];
  const fresh = [
    { id: "now", ids: ["chat:b"] },
    { id: "attention", ids: [] },
    { id: "recent", ids: [] },
    { id: "earlier", ids: [] },
    { id: "unknown", ids: [] },
  ];
  const merged = mergeChatOrder(frozen, fresh);
  assert.deepEqual(merged.buckets[2].ids, ["chat:b"], "the chat stays under the heading it is being read under");
  assert.deepEqual(merged.buckets[0].ids, [], "and is not moved under the reader");
  assert.equal(merged.moved, 1, "a chat that changed heading is a real move");
  assert.equal(merged.changed, true, "so the explicit notice is offered");

  const { container, view } = mount(createScene(activityMap([{ id: "b", updated_at: "2026-10-03T10:00:00Z" }]), { projectId: "p1" }));
  view.update({
    scene: createScene(activityMap([{ id: "b", updated_at: "2026-10-03T10:00:00Z", active: true }]), { projectId: "p1" }),
    now: Date.parse("2026-10-03T11:00:00Z"),
  });
  const notice = container.find("gridProjection__notice");
  assert.ok(notice, "a chat that started running is announced, not silently moved");
  assert.match(notice.textContent, /Order kept while you read/);
});

test("a render keeps the reader's scroll and focus, and never takes focus", () => {
  const first = sceneWithDetail();
  const { container, view } = mount(first, { selectedId: "chat:c1" });
  container.scrollTop = 240;
  const toggle = container.find("gridProjection__toggle");
  assert.ok(toggle, "a chat row has a fold control");
  const control = toggle.getAttribute("data-for");
  toggle.focus();
  assert.equal(document.activeElement, toggle);

  view.update({ selectedId: "chat:c1" });
  assert.equal(container.scrollTop, 240, "the frame keeps its place");
  const refocused = document.activeElement;
  assert.notEqual(refocused, toggle, "the old node was replaced");
  assert.equal(refocused.getAttribute("data-for"), control, "focus is restored to the same control");
  assert.ok(container.contains(refocused), "and it is the node the reader now sees");

  // Selecting a row repaints its classes; the reader's focus stays on it.
  const row = rows(container).find((element) => element.getAttribute("data-id") === "operation:c1:op-read");
  row.focus();
  view.update({ selectedId: "operation:c1:op-read" });
  assert.equal(document.activeElement.getAttribute("data-id"), "operation:c1:op-read");
  assert.ok(document.activeElement.classList.contains("gridProjection__row--selected"));

  // Focus the reader put elsewhere is not stolen by a render.
  const outside = document.createElement("button");
  document.body.append(outside);
  outside.focus();
  view.update({ selectedId: "operation:c1:op-agent" });
  assert.equal(document.activeElement, outside, "a fresh answer never takes the keyboard");
  outside.remove();
});
