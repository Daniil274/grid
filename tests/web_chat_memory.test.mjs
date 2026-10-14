import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();
document.addEventListener = () => {};
globalThis.location = { hash: "", pathname: "/", search: "", assign() {} };

const { api } = await import("../web_chat/js/net/api.js");
const { MemoryDrawer, LAYER_TABS, parseTags, payloadOf } = await import("../web_chat/js/ui/memory-dialog.js");

const el = (tag = "div") => document.createElement(tag);

function nodes() {
  const names = [
    "drawer", "backdrop", "closeButton", "syncButton", "newButton", "status", "backend", "counts", "budget",
    "tabs", "stale", "search", "previewButton", "preview", "previewText", "list", "editor",
  ];
  const made = Object.fromEntries(names.map((name) => [name, el(name === "stale" || name === "search" ? "input" : "div")]));
  made.scope = el("select");
  made.scope.value = "";
  made.preview.hidden = true;
  return made;
}

const RECORDS = [
  {
    id: "r1", layer: "L1", scope: "project", category: "decision", title: "Use tabs", content: "Indent with tabs.",
    tags: ["style", "code"], source_ref: "chat:1", file_path: "src/app.py", confidence: 0.9,
    created_at: "2026-01-02T03:04:05Z", updated_at: "2026-01-02T03:04:05Z", stale_status: "stale",
  },
  {
    id: "r2", layer: "L3", scope: "user", category: "preference", title: "", content: "Prefers Russian.",
    tags: [], source_ref: null, file_path: null, confidence: 1,
    created_at: "2026-01-03T03:04:05Z", updated_at: "2026-01-03T03:04:05Z", stale_status: "fresh",
  },
];

const OVERVIEW = {
  overview: {
    counts: { L0: 0, L1: 1, L2: 0, L3: 1 }, stale_count: 1, total: 2, healthy: true, enabled: true,
    auto_inject: true, auto_inject_tokens: 1000, preview_estimated_tokens: 250,
    preview_prompt_section: "## Memory\n- Prefers Russian.",
  },
  config: { enabled_in_config: true, backend: "sqlite", auto_inject_tokens: 1000 },
  records: RECORDS,
};

function install(overrides = {}) {
  const calls = [];
  const saved = {};
  const stubs = {
    memoryOverview: async (params) => { calls.push(["overview", params]); return OVERVIEW; },
    createMemoryRecord: async (payload) => { calls.push(["create", payload]); return { id: "new", ...payload }; },
    updateMemoryRecord: async (id, payload) => { calls.push(["update", id, payload]); return { id, ...payload }; },
    deleteMemoryRecord: async (id, params) => { calls.push(["delete", id, params]); return { ok: true, id }; },
    syncMemory: async (payload) => { calls.push(["sync", payload]); return { ok: true, scanned: 4, stored: 2, records: [] }; },
    ...overrides,
  };
  for (const [name, fn] of Object.entries(stubs)) { saved[name] = api[name]; api[name] = fn; }
  return { calls, restore: () => Object.assign(api, saved) };
}

function drawerFor(contextId = "ctx") {
  const view = nodes();
  const changed = [];
  const drawer = new MemoryDrawer(view, { getContextId: () => contextId, onChanged: (data) => changed.push(data) });
  return { view, drawer, changed };
}

const buttons = (root, action) => root.querySelectorAll("button").filter((b) => b.dataset.action === action);

test("api memory methods hit /api/memory with the right verbs, paths and queries", async () => {
  const originalFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, options = {}) => {
    seen.push({ url, method: options.method ?? "GET", body: options.body });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  try {
    await api.memoryOverview({ context_id: "c 1", layer: "L2", query: "deploy", stale_only: true, scope: "", limit: 50 });
    await api.memoryOverview();
    await api.createMemoryRecord({ content: "x", layer: "L1" });
    await api.updateMemoryRecord("a/b", { title: "t" });
    await api.deleteMemoryRecord("r1", { context_id: "ctx" });
    await api.syncMemory({ context_id: "ctx" });
  } finally { globalThis.fetch = originalFetch; }

  assert.equal(seen[0].url, "/api/memory?context_id=c+1&layer=L2&query=deploy&stale_only=true&limit=50");
  assert.equal(seen[1].url, "/api/memory");
  assert.deepEqual([seen[2].method, seen[2].url, JSON.parse(seen[2].body)], ["POST", "/api/memory/records", { content: "x", layer: "L1" }]);
  assert.deepEqual([seen[3].method, seen[3].url], ["PUT", "/api/memory/records/a%2Fb"]);
  assert.deepEqual([seen[4].method, seen[4].url], ["DELETE", "/api/memory/records/r1?context_id=ctx"]);
  assert.deepEqual([seen[5].method, seen[5].url, JSON.parse(seen[5].body)], ["POST", "/api/memory/sync", { context_id: "ctx" }]);
});

test("opening loads the overview and shows backend, counts, budget and the layer tabs", async () => {
  const stub = install();
  try {
    const { view, drawer, changed } = drawerFor();
    await drawer.open();

    assert.ok(view.drawer.classList.contains("is-open"));
    assert.deepEqual(stub.calls[0], ["overview", { context_id: "ctx", layer: undefined, scope: undefined, query: undefined, stale_only: undefined, limit: 200 }]);
    assert.match(view.backend.textContent, /sqlite/);
    assert.match(view.backend.textContent, /active/);
    for (const layer of ["L3", "L2", "L1", "L0"]) assert.match(view.counts.textContent, new RegExp(layer));
    assert.match(view.counts.textContent, /STALE 1/);
    assert.match(view.budget.textContent, /250 \/ 1000 tokens/);
    assert.equal(view.previewText.textContent, "## Memory\n- Prefers Russian.");
    assert.equal(view.tabs.querySelectorAll("button").length, LAYER_TABS.length);
    assert.equal(changed.length, 1);
    assert.equal(changed[0].overview.stale_count, 1);
  } finally { stub.restore(); }
});

test("cards show layer, category, FRESH/STALE, source, tags and edit/delete actions", async () => {
  const stub = install();
  try {
    const { view, drawer } = drawerFor();
    await drawer.load();

    const cards = view.list.querySelectorAll(".memCard");
    assert.equal(cards.length, 2);
    const [stale, fresh] = cards;
    assert.equal(stale.dataset.stale, "stale");
    assert.match(stale.textContent, /L1/);
    assert.match(stale.textContent, /decision/);
    assert.match(stale.textContent, /STALE/);
    assert.match(stale.textContent, /src\/app\.py/);
    assert.match(stale.textContent, /#style/);
    assert.match(stale.textContent, /Use tabs/);
    assert.match(fresh.textContent, /FRESH/);
    assert.match(fresh.textContent, /Prefers Russian/);
    assert.equal(buttons(stale, "edit").length, 1);
    assert.equal(buttons(stale, "delete").length, 1);
  } finally { stub.restore(); }
});

test("tabs, scope, stale filter and search reload with the matching params", async () => {
  const stub = install();
  try {
    const { view, drawer } = drawerFor();
    await drawer.load();

    view.tabs.querySelectorAll("button").find((b) => b.dataset.layer === "L2").click();
    await drawer.load();
    await drawer.setScope("user");
    await drawer.setStaleOnly(true);
    await drawer.setQuery("  deploy  ");

    const last = stub.calls.at(-1)[1];
    assert.deepEqual(
      { layer: last.layer, scope: last.scope, stale_only: last.stale_only, query: last.query },
      { layer: "L2", scope: "user", stale_only: true, query: "deploy" },
    );
    assert.ok(view.tabs.querySelectorAll("button").find((b) => b.dataset.layer === "L2").classList.contains("is-active"));
  } finally { stub.restore(); }
});

test("a slow older request never overwrites a newer one", async () => {
  let release;
  const stub = install({
    memoryOverview: (params) =>
      params.query === "old"
        ? new Promise((resolve) => { release = () => resolve({ ...OVERVIEW, records: [RECORDS[0]] }); })
        : Promise.resolve({ ...OVERVIEW, records: [RECORDS[1]] }),
  });
  try {
    const { view, drawer } = drawerFor();
    const slow = drawer.setQuery("old");
    await drawer.setQuery("new");
    release();
    await slow;
    assert.equal(view.list.querySelectorAll(".memCard").length, 1);
    assert.match(view.list.textContent, /Prefers Russian/);
  } finally { stub.restore(); }
});

test("an empty memory and a failed load say so", async () => {
  const stub = install({ memoryOverview: async () => ({ ...OVERVIEW, records: [] }) });
  try {
    const { view, drawer } = drawerFor();
    await drawer.load();
    assert.match(view.list.textContent, /Memory is empty/);
    await drawer.setQuery("zzz");
    assert.match(view.list.textContent, /Nothing matches/);
  } finally { stub.restore(); }
  const failing = install({ memoryOverview: async () => { throw new Error("Memory is unavailable"); } });
  try {
    const { view, drawer } = drawerFor();
    await drawer.load();
    assert.equal(view.status.textContent, "Memory is unavailable");
    assert.equal(view.status.dataset.tone, "error");
  } finally { failing.restore(); }
});

test("a new record is created from the form with parsed tags and the chat's context", async () => {
  const stub = install();
  try {
    const { view, drawer } = drawerFor("chat-7");
    await drawer.load();
    view.newButton.click?.();
    drawer.startNew();

    assert.equal(drawer.draft.layer, "L1");
    Object.assign(drawer.draft, { layer: "L2", category: "workflow", title: " Release ", content: " Tag then push ", tagsText: "ci, release,, ci", file_path: " ", confidence: "2" });
    await drawer.save();

    const create = stub.calls.find((call) => call[0] === "create");
    assert.deepEqual(create[1], {
      layer: "L2", scope: "project", category: "workflow", title: "Release", content: "Tag then push",
      file_path: "", source_ref: "", tags: ["ci", "release"], confidence: 1, context_id: "chat-7",
    });
    assert.equal(drawer.draft, null);
    assert.equal(stub.calls.at(-1)[0], "overview"); // reloaded
  } finally { stub.restore(); }
});

test("an empty text is refused before any request", async () => {
  const stub = install();
  try {
    const { view, drawer } = drawerFor();
    drawer.startNew();
    assert.equal(await drawer.save(), null);
    assert.match(view.status.textContent, /Write what to remember/);
    assert.equal(stub.calls.filter((c) => c[0] === "create").length, 0);
  } finally { stub.restore(); }
});

test("editing fills the form from the record and saves through update", async () => {
  const stub = install();
  try {
    const { view, drawer } = drawerFor();
    await drawer.load();
    buttons(view.list.querySelectorAll(".memCard")[0], "edit")[0].click();

    assert.equal(drawer.selectedId, "r1");
    assert.equal(drawer.draft.tagsText, "style, code");
    assert.equal(drawer.draft.file_path, "src/app.py");
    assert.match(view.editor.textContent, /Edit record/);
    drawer.draft.title = "Use spaces";
    await drawer.save();

    const update = stub.calls.find((call) => call[0] === "update");
    assert.equal(update[1], "r1");
    assert.equal(update[2].title, "Use spaces");
    assert.deepEqual(update[2].tags, ["style", "code"]);
    assert.equal(update[2].file_path, "src/app.py");
  } finally { stub.restore(); }
});

test("deleting asks first, then removes the record in the chat's context", async () => {
  const stub = install();
  const originalConfirm = globalThis.confirm;
  try {
    const { view, drawer } = drawerFor("ctx");
    await drawer.load();
    const card = () => view.list.querySelectorAll(".memCard")[0];

    globalThis.confirm = () => false;
    buttons(card(), "delete")[0].click();
    await Promise.resolve();
    assert.equal(stub.calls.filter((c) => c[0] === "delete").length, 0);

    globalThis.confirm = () => true;
    assert.equal(await drawer.remove(RECORDS[0]), true);
    assert.deepEqual(stub.calls.find((c) => c[0] === "delete"), ["delete", "r1", { context_id: "ctx" }]);
    assert.equal(view.status.textContent, "Deleted");
  } finally { stub.restore(); globalThis.confirm = originalConfirm; }
});

test("extract from chat syncs the open chat and reports what was stored", async () => {
  const stub = install();
  try {
    const { view, drawer } = drawerFor("ctx");
    const result = await drawer.sync();

    assert.equal(result.stored, 2);
    assert.deepEqual(stub.calls.find((c) => c[0] === "sync"), ["sync", { context_id: "ctx" }]);
    assert.match(view.status.textContent, /Stored 2 new records from 4 messages/);
    assert.equal(view.syncButton.disabled, false);
  } finally { stub.restore(); }

  const none = install();
  try {
    const { view, drawer } = drawerFor(null);
    assert.equal(await drawer.sync(), null);
    assert.match(view.status.textContent, /Open a chat first/);
    assert.equal(none.calls.length, 0);
  } finally { none.restore(); }
});

test("the prompt preview toggles open and closed", () => {
  const { view, drawer } = drawerFor();
  drawer.togglePreview();
  assert.equal(view.preview.hidden, false);
  assert.equal(view.previewButton.getAttribute("aria-expanded"), "true");
  drawer.togglePreview();
  assert.equal(view.preview.hidden, true);
});

test("parseTags and payloadOf normalise a draft", () => {
  assert.deepEqual(parseTags("a, b\nc,,a"), ["a", "b", "c"]);
  const body = payloadOf({ layer: "L3", scope: "user", category: "note", title: " t ", content: " c ", file_path: "", source_ref: " s ", tagsText: "x", confidence: "abc" });
  assert.equal(body.confidence, 1);
  assert.equal(body.source_ref, "s");
  assert.equal("context_id" in body, false);
});

test("index.html carries the memory button, the drawer and every node main.js binds", () => {
  const html = readFileSync(new URL("../web_chat/index.html", import.meta.url), "utf8");
  const main = readFileSync(new URL("../web_chat/js/main.js", import.meta.url), "utf8");
  assert.match(html, /id="open-memory"/);
  assert.match(html, /id="memory"[^>]*role="dialog"/);
  assert.match(html, /styles\/memory\.css/);
  const bound = [...main.matchAll(/\$\("#(memory[\w-]*|open-memory)"\)/g)].map((m) => m[1]);
  assert.ok(bound.length >= 17);
  for (const id of new Set(bound)) assert.match(html, new RegExp(`id="${id}"`), `#${id} is missing from index.html`);
});
