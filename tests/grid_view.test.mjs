/**
 * The view switch of the map page (web_chat/js/grid/view.js) and the page that
 * owns the renderers.
 *
 * The promises kept here: a phone opens in Layers and never downloads the 3D
 * module; a desktop with WebGL2 opens in Space and imports `space.js` only at
 * that moment; a renderer that cannot start says so and steps aside for Layers;
 * one scene is built per new map answer (never per 304); and the selection is
 * the same object in Space, Layers, Timeline, the Map and the inspector.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const {
  VIEWS,
  VIEW_KEY,
  applyView,
  createViewSwitch,
  probeWebGL,
  readViewPreference,
  resolveInitialView,
  sceneIdOfSelection,
  selectionOfSceneId,
  writeViewPreference,
} = await import("../web_chat/js/grid/view.js");
const { GridPage } = await import("../web_chat/js/grid/page.js");

const el = (tag) => document.createElement(tag);
const GRID_HTML = fileURLToPath(new URL("../web_chat/grid.html", import.meta.url));
const GRID_CSS = fileURLToPath(new URL("../web_chat/styles/grid.css", import.meta.url));

/** A project as GET /api/projects/{id}/map answers for it. */
function sampleMap(overrides = {}) {
  return {
    project: { id: "p1", title: "Grid" },
    path: [{ id: "p1", title: "Grid" }],
    tasks: {
      available: true,
      items: [
        {
          id: "bd-1", title: "Alpha", state: "ready", status: "open", created_at: "2026-10-01T09:00:00Z",
          updated_at: "2026-10-02T09:00:00Z", depends_on: [],
        },
        {
          id: "bd-2", title: "Beta", state: "in_progress", status: "in_progress", created_at: "2026-10-02T09:00:00Z",
          updated_at: "2026-10-03T09:00:00Z", depends_on: ["bd-1"],
        },
      ],
      total: 2,
      counts: { ready: 1, in_progress: 1 },
    },
    processes: {
      available: true,
      items: [
        {
          chat_id: "c1",
          title: "Chat one",
          active: true,
          awaiting_approval: false,
          updated_at: "2026-10-04T10:00:00Z",
          steps: ["read the map answer"],
          activity: {
            items: [
              { id: "s1", kind: "tool", title: "Read the map", status: "done", at_ms: 1700000000000, context_id: "scout", files: ["docs/readme.md"] },
              { id: "s2", kind: "message", title: "Here is the map", status: "done", at_ms: 1700000001000, context_id: "root", files: [] },
            ],
            contexts: [
              { id: "root", parent_id: null, label: "Main agent", status: "done", at_ms: null, duration_ms: null, total: 1 },
              { id: "scout", parent_id: "root", label: "Scout", status: "done", at_ms: 1700000000000, duration_ms: 50, total: 1 },
            ],
            total: 2,
            truncated: false,
          },
          task_links: [{ task_id: "bd-1", relation: "turn" }],
        },
      ],
      total: 1,
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
    ...overrides,
  };
}

/** A chat detail as GET /api/projects/{id}/map/chats/{chat} answers it. */
function sampleDetail(chatId = "c1") {
  return {
    chat: { id: chatId, title: "Chat one", href: `/?conversation=${chatId}` },
    messages: {
      items: [
        {
          id: "m1", role: "assistant", time: "2026-10-04T10:00:01Z",
          kind: "final", preview: "Here is the map", turn_id: "turn-1", has_image: false,
        },
      ],
      total: 1,
      truncated: false,
    },
    executions: {
      items: [
        {
          id: "turn:turn-1", turn_id: "turn-1", agent: "main", status: "done",
          start_time: 1700000000, end_time: 1700000005, duration_ms: 5000, error: false,
        },
      ],
      total: 1,
      truncated: false,
    },
    operations: {
      items: [
        { id: "op-read", execution_id: "turn:turn-1", parent_id: null, kind: "tool", title: "Read the map", status: "done", at_ms: 100, duration_ms: 20, files: ["README.md"], summary: "" },
        { id: "op-agent", execution_id: "turn:turn-1", parent_id: null, kind: "agent", title: "Scout", status: "done", at_ms: 200, duration_ms: 40, files: [], summary: "" },
        { id: "op-child", execution_id: "turn:turn-1", parent_id: "op-agent", kind: "tool", title: "Child read", status: "done", at_ms: 210, duration_ms: 10, files: [], summary: "" },
      ],
      total: 3,
      truncated: false,
    },
  };
}

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem(key) {
      return map.has(key) ? map.get(key) : null;
    },
    setItem(key, value) {
      map.set(key, String(value));
    },
  };
}

function fakeDocument() {
  return {
    visibilityState: "visible",
    documentElement: { dataset: {} },
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
  };
}

function fakeTimers() {
  return {
    setInterval() {
      return { cleared: false };
    },
    clearInterval(timer) {
      timer.cleared = true;
    },
  };
}

/** A stand-in for Layers/Timeline that records what it was handed. */
function stubProjection() {
  const calls = { created: 0, destroyed: 0, options: [], updates: [] };
  return {
    calls,
    create(container, options) {
      calls.created += 1;
      calls.options.push(options);
      return {
        update(patch) {
          calls.updates.push(patch);
        },
        destroy() {
          calls.destroyed += 1;
        },
        getState() {
          return {};
        },
      };
    },
  };
}

/** A stand-in for `space.js`, with a loader that records whether it ran. */
function stubSpace({ fail = false } = {}) {
  const calls = { loaded: 0, created: 0, destroyed: 0, options: [], updates: [], focus: [], resized: 0 };
  return {
    calls,
    async load() {
      calls.loaded += 1;
      return {
        createSpaceView(container, options) {
          calls.created += 1;
          calls.options.push(options);
          if (fail === "create") throw new Error("The 3D Space needs WebGL2, which this browser did not provide.");
          return {
            canvas: {},
            update(patch) {
              calls.updates.push(patch);
            },
            focus(id) {
              calls.focus.push(id);
            },
            resize() {
              calls.resized += 1;
            },
            destroy() {
              calls.destroyed += 1;
            },
            stats() {
              return {};
            },
          };
        },
      };
    },
  };
}

function stubOffice({ fail = false } = {}) {
  const space = stubSpace({ fail });
  return {
    calls: space.calls,
    async load() {
      const module = await space.load();
      return { createOfficeView: module.createSpaceView };
    },
  };
}

function pageNodes() {
  return {
    status: el("div"),
    title: el("h1"),
    breadcrumb: el("nav"),
    map: el("div"),
    space: el("div"),
    office: el("div"),
    projection: el("div"),
    view: el("select"),
    overview: el("aside"),
    refresh: el("button"),
    build: el("span"),
  };
}

function makePage(options = {}) {
  const nodes = options.nodes ?? pageNodes();
  const projection = options.projection ?? stubProjection();
  const space = options.space ?? stubSpace();
  const office = options.office ?? stubOffice();
  const doc = options.doc ?? fakeDocument();
  const storage = options.storage ?? fakeStorage();
  let mapAnswer = options.map ?? sampleMap();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc,
    timers: fakeTimers(),
    storage,
    view: options.view === undefined ? "layers" : options.view,
    loadMap: options.loadMap ?? (async () => ({ notModified: false, etag: '"a"', map: mapAnswer })),
    loadChatDetail:
      options.loadChatDetail
      ?? (async (_projectId, chatId) => ({ notModified: false, etag: '"d"', detail: sampleDetail(chatId) })),
    createProjection: projection.create,
    loadSpace: space.load,
    loadOffice: office.load,
    fetchJson: options.fetchJson ?? null,
    ...options.page,
  });
  return { page, nodes, projection, space, office, doc, storage, setMap: (next) => (mapAnswer = next) };
}

// -- the rule ----------------------------------------------------------------

test("a narrow screen opens in Layers, whatever was stored", () => {
  const resolved = resolveInitialView({ stored: "space", width: 390, webgl: true });
  assert.equal(resolved.view, "layers");
  assert.equal(resolved.source, "mobile");
  assert.match(resolved.note, /narrow screen/i);
  assert.equal(resolveInitialView({ width: 720 }).view, "layers");
  assert.equal(resolveInitialView({ width: 721, webgl: true }).view, "office");
});

test("a desktop with WebGL opens Office, without WebGL it opens Layers", () => {
  const withGl = resolveInitialView({ width: 1280, webgl: true });
  assert.equal(withGl.view, "office");
  assert.equal(withGl.source, "default");
  assert.match(withGl.note, /Office/);
  const noGl = resolveInitialView({ width: 1280, webgl: false });
  assert.equal(noGl.view, "layers");
  assert.equal(noGl.source, "default");
});

test("an explicit Space choice survives on a desktop that can draw it", () => {
  assert.equal(resolveInitialView({ stored: "space", width: 1280, webgl: true }).view, "space");
  assert.equal(resolveInitialView({ width: 1280, webgl: true, stored: "" }).view, "office");
});

test("reduced motion opens in Layers on a desktop too", () => {
  const resolved = resolveInitialView({ width: 1280, reducedMotion: true, webgl: true });
  assert.equal(resolved.view, "layers");
  assert.equal(resolved.source, "reduced-motion");
});

test("a stored choice is honoured, and a stored Space without WebGL2 is not", () => {
  assert.equal(resolveInitialView({ stored: "timeline", width: 1280, webgl: true }).view, "timeline");
  assert.equal(resolveInitialView({ stored: "map", width: 1280, webgl: true }).view, "map");
  const stored = resolveInitialView({ stored: "space", width: 1280, webgl: false });
  assert.equal(stored.view, "layers");
  assert.equal(stored.source, "stored");
  assert.match(stored.note, /WebGL2/);
});

test("with no viewport known the Map is the safe fallback", () => {
  assert.equal(resolveInitialView({}).view, "map");
  assert.equal(resolveInitialView({ width: 0, webgl: true }).view, "map");
});

test("the preference is kept under grid.view and only ever a known view", () => {
  const storage = fakeStorage();
  assert.equal(readViewPreference(storage), "");
  writeViewPreference(storage, "timeline");
  assert.equal(storage.getItem(VIEW_KEY), "timeline");
  assert.equal(readViewPreference(storage), "timeline");
  writeViewPreference(storage, "depth");
  assert.equal(storage.getItem(VIEW_KEY), "timeline", "an unknown value is never written");
  assert.deepEqual(VIEWS, ["office", "space", "layers", "timeline", "map"]);
});

test("the WebGL probe answers honestly without a canvas", () => {
  assert.equal(probeWebGL(document), false);
  assert.equal(probeWebGL(null), false);
});

test("the switch reports a change and snaps back to what is on screen", () => {
  const select = el("select");
  const seen = [];
  const view = createViewSwitch({ select, view: "layers", onChange: (next, meta) => seen.push({ next, meta }) });
  assert.equal(select.value, "layers");
  select.value = "timeline";
  for (const fn of select.listeners.change ?? []) fn({ target: select });
  assert.deepEqual(seen, [{ next: "timeline", meta: { explicit: true } }]);
  view.set("layers");
  assert.equal(select.value, "layers");
  assert.equal(view.view, "layers");
  view.destroy();
});

// -- the two names for one object --------------------------------------------

test("a selection and a scene id name each other, including a folder's real path", () => {
  const map = sampleMap();
  assert.equal(sceneIdOfSelection({ kind: "task", id: "bd-1" }), "task:bd-1");
  assert.equal(sceneIdOfSelection({ kind: "process", id: "c1" }), "chat:c1");
  assert.equal(sceneIdOfSelection({ kind: "chat", id: "c1" }), "chat:c1");
  assert.equal(sceneIdOfSelection({ kind: "message", chatId: "c1", id: "m1" }), "message:c1:m1");
  assert.equal(sceneIdOfSelection({ kind: "execution", chatId: "c1", id: "turn:1" }), "execution:c1:turn:1");
  assert.equal(sceneIdOfSelection({ kind: "execution", chatId: "c1", id: "op-agent", sub: true }), "execution:sub:c1:op-agent");
  assert.equal(sceneIdOfSelection({ kind: "operation", chatId: "c1", id: "op-read" }), "operation:c1:op-read");
  assert.equal(sceneIdOfSelection({ kind: "dir", path: "project/docs" }, { map }), "file:docs");
  assert.equal(sceneIdOfSelection({ kind: "file", path: "project/README.md" }, { map }), "file:README.md");
  assert.equal(sceneIdOfSelection({ kind: "file", path: "elsewhere/x.md" }, { map }), "file:elsewhere/x.md");

  assert.deepEqual(selectionOfSceneId("task:bd-1"), { kind: "task", id: "bd-1" });
  assert.deepEqual(selectionOfSceneId("chat:c1"), { kind: "chat", id: "c1", sceneId: "chat:c1" });
  assert.deepEqual(selectionOfSceneId("message:c1:m1"), { kind: "message", chatId: "c1", id: "m1", messageId: "m1", sceneId: "message:c1:m1" });
  assert.deepEqual(selectionOfSceneId("execution:c1:turn:1"), { kind: "execution", chatId: "c1", id: "turn:1", executionId: "turn:1", sub: false, sceneId: "execution:c1:turn:1" });
  assert.deepEqual(selectionOfSceneId("execution:sub:c1:op-agent"), { kind: "execution", chatId: "c1", id: "op-agent", executionId: "op-agent", sub: true, sceneId: "execution:sub:c1:op-agent" });
  assert.deepEqual(selectionOfSceneId("operation:c1:op-read"), { kind: "operation", chatId: "c1", id: "op-read", operationId: "op-read", sceneId: "operation:c1:op-read" });
  assert.deepEqual(selectionOfSceneId("file:docs", { map }), { kind: "file", path: "project/docs", sceneId: "file:docs" });
  const scene = { nodes: [{ id: "file:docs", item: { kind: "dir", path: "docs" } }] };
  assert.deepEqual(selectionOfSceneId("file:docs", { scene, map }), { kind: "dir", path: "project/docs", sceneId: "file:docs" });
  assert.equal(selectionOfSceneId("file:", { map }), null);
  assert.equal(selectionOfSceneId("nonsense", { map }), null);
});

test("old context and activity scene ids are ignored: there is no context entity", () => {
  assert.equal(selectionOfSceneId("context:c1:scout", { map: sampleMap() }), null);
  assert.equal(selectionOfSceneId("activity:c1:0", { map: sampleMap() }), null);
});

test("the document carries the view, and an unknown one becomes the Map", () => {
  const doc = fakeDocument();
  applyView(doc, "timeline");
  assert.equal(doc.documentElement.dataset.view, "timeline");
  applyView(doc, "nope");
  assert.equal(doc.documentElement.dataset.view, "map");
});

// -- the page and its renderers ----------------------------------------------

test("the page builds one scene per new map answer and hands it to Layers", async () => {
  const { page, projection } = makePage();
  await page.load();
  assert.equal(projection.calls.created, 1);
  assert.ok(page.scene?.nodes.length >= 5, "two tasks, one chat and two files");
  assert.equal(projection.calls.options[0].scene, page.scene);
  assert.equal(projection.calls.options[0].mode, "layers");
  assert.equal(page.view, "layers");
});

test("a 304 rebuilds no scene, asks for no file and keeps the selection", async () => {
  let answer = { notModified: false, etag: '"a"', map: sampleMap() };
  const asked = [];
  const { page, projection } = makePage({
    loadMap: async () => answer,
    fetchJson: async (url) => {
      asked.push(url);
      return { ok: true, status: 200, json: { entries: [], path: "project/docs" } };
    },
  });
  await page.load();
  const scene = page.scene;
  await page.select({ kind: "dir", path: "project/docs" });
  assert.equal(asked.length, 1, "the folder is read once, lazily");
  const updates = projection.calls.updates.length;
  answer = { notModified: true, etag: '"a"' };
  await page.load();
  assert.equal(page.scene, scene, "a 304 speaks for the same scene");
  assert.deepEqual(page.inspector.selection, { kind: "dir", path: "project/docs" });
  assert.equal(asked.length, 1, "a refresh re-reads nothing");
  assert.equal(projection.calls.updates.length, updates, "a 304 pushes nothing into the renderer");
  assert.equal(projection.calls.created, 1);
});

test("a click in the Layers view selects in the inspector and back", async () => {
  const { page, projection } = makePage();
  await page.load();
  page.pickSceneId("task:bd-1");
  assert.deepEqual(page.inspector.selection, { kind: "task", id: "bd-1" });
  assert.equal(projection.calls.updates.at(-1).selectedId, "task:bd-1");
  assert.match(page.nodes.overview.textContent, /Open task/);

  page.pickSceneId(null);
  assert.equal(page.inspector.selection, null);
  assert.equal(projection.calls.updates.at(-1).selectedId, null);
});

test("selecting a chat reads its detail exactly once and builds the canonical tree", async () => {
  const calls = [];
  const { page, projection, nodes } = makePage({
    loadChatDetail: async (projectId, chatId) => {
      calls.push([projectId, chatId]);
      return { notModified: false, etag: '"d"', detail: sampleDetail(chatId) };
    },
  });
  await page.load();
  assert.equal(calls.length, 0, "the base map reads no chat detail");
  assert.equal(page.scene.nodes.filter((node) => node.kind === "message").length, 0);

  page.pickSceneId("chat:c1");
  await page.viewReady;
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, [["p1", "c1"]], "one bounded detail read for the selected chat");
  const scene = projection.calls.updates.at(-1).scene;
  assert.ok(scene.nodes.some((node) => node.kind === "message"));
  assert.ok(scene.nodes.some((node) => node.kind === "execution"));
  assert.ok(scene.nodes.some((node) => node.kind === "operation"));
  assert.ok(scene.nodes.some((node) => node.id === "execution:sub:c1:op-agent"), "the agent operation opens a child execution");
  assert.match(nodes.overview.textContent, /Messages/);
  assert.match(nodes.overview.textContent, /Executions/);
  assert.match(nodes.overview.textContent, /Operations/);
  assert.match(nodes.overview.textContent, /Child execution/);

  // Switching views asks for nothing new: the detail is in the scene.
  await page.setView("timeline");
  await page.setView("layers");
  assert.equal(calls.length, 1, "a view switch never reloads the detail");
});

test("an operation selection reads its owning chat and keeps the exact node selected", async () => {
  let calls = 0;
  const { page, projection } = makePage({
    loadMap: async () => ({ notModified: false, etag: '"a"', map: sampleMap() }),
    loadChatDetail: async (_projectId, chatId) => {
      calls += 1;
      return { notModified: false, etag: '"d"', detail: sampleDetail(chatId) };
    },
  });
  await page.load();
  page.pickSceneId("operation:c1:op-child");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls, 1);
  assert.equal(page.inspector.selection.kind, "operation");
  assert.equal(page.inspector.selection.chatId, "c1");
  assert.equal(page.inspector.selection.id, "op-child");
  assert.equal(page.sceneId(), "operation:c1:op-child");
  assert.equal(projection.calls.updates.at(-1).selectedId, "operation:c1:op-child");
});

test("a new map 200 clears and refetches the selected detail; a 304 does neither", async () => {
  let answer = { notModified: false, etag: '"a"', map: sampleMap() };
  let calls = 0;
  const { page } = makePage({
    loadMap: async () => answer,
    loadChatDetail: async (_projectId, chatId) => {
      calls += 1;
      return { notModified: false, etag: '"d"', detail: sampleDetail(chatId) };
    },
  });
  await page.load();
  page.pickSceneId("chat:c1");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls, 1);

  answer = { notModified: true, etag: '"a"' };
  await page.load();
  assert.equal(calls, 1, "a 304 asks for no detail and does not rerender");
  assert.ok(page.chatDetails.has("c1"), "the detail stays cached across a 304");

  answer = { notModified: false, etag: '"b"', map: sampleMap() };
  await page.load();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls, 2, "a new map answer is a new revision and refetches the selected chat");
  assert.ok(page.chatDetails.has("c1"));
});

test("the Space module is imported only when Space is really chosen", async () => {
  const space = stubSpace();
  const { page, projection } = makePage({ space });
  await page.load();
  assert.equal(space.calls.loaded, 0, "Layers never downloads the 3D module");

  await page.setView("space");
  assert.equal(space.calls.loaded, 1);
  assert.equal(space.calls.created, 1);
  assert.equal(page.spaceView !== null, true);

  await page.setView("layers");
  assert.equal(space.calls.destroyed, 1, "leaving Space releases the renderer");
  assert.equal(page.spaceView, null);
  assert.equal(projection.calls.created, 2, "Layers is built again on return");
  assert.equal(projection.calls.destroyed, 1, "the Layers renderer was released while Space ran");
});

test("a Space that cannot start says so and falls back to Layers", async () => {
  const space = stubSpace({ fail: "create" });
  const { page, projection, nodes } = makePage({ space });
  await page.load();
  await page.setView("space");
  assert.equal(page.view, "layers");
  assert.equal(page.spaceView, null);
  assert.equal(projection.calls.created, 2, "Layers is built again after the failed Space");
  assert.match(nodes.status.textContent, /Showing Layers instead/);
  assert.match(nodes.status.textContent, /WebGL2/);
  assert.equal(nodes.status.dataset.tone, "error");
});

test("a mobile page ignores a stored Space and imports nothing", async () => {
  const space = stubSpace();
  const storage = fakeStorage({ [VIEW_KEY]: "space" });
  const { page, nodes } = makePage({
    space,
    storage,
    view: null,
    page: { width: 390, webgl: true, reducedMotion: false },
  });
  await page.load();
  assert.equal(page.view, "layers");
  assert.equal(space.calls.loaded, 0, "a phone asks for no 3D module");
  assert.equal(nodes.space.hidden, true);
  assert.equal(nodes.projection.hidden, false);
  // The control shows what is really on screen, not what was stored.
  assert.equal(nodes.view.value, "layers");
});

test("a desktop page with no stored choice opens Office without downloading Space", async () => {
  const space = stubSpace();
  const { page, office, nodes, storage } = makePage({
    space,
    storage: fakeStorage(),
    view: null,
    page: { width: 1280, webgl: true, reducedMotion: false },
  });
  await page.load();
  await page.syncView();
  assert.equal(page.view, "office");
  assert.equal(space.calls.loaded, 0, "Office asks for no Space module");
  assert.equal(nodes.office.hidden, false);
  assert.ok(office.calls.created >= 1, "Office is what the reader sees");
  // An explicit 3D choice is still remembered, so the next visit keeps it.
  await page.chooseView("space");
  assert.equal(storage.getItem(VIEW_KEY), "space");
  assert.equal(space.calls.loaded, 1);
});

test("an explicit choice is remembered only when the view answered", async () => {
  const space = stubSpace({ fail: "create" });
  const storage = fakeStorage();
  const { page } = makePage({ space, storage });
  await page.load();
  await page.chooseView("space");
  assert.equal(storage.getItem(VIEW_KEY), null, "a failed Space is not remembered");
  await page.chooseView("timeline");
  assert.equal(storage.getItem(VIEW_KEY), "timeline");
  assert.equal(page.view, "timeline");
});

test("Layers and Timeline are the same renderer with a different mode", async () => {
  const { page, projection } = makePage();
  await page.load();
  await page.setView("timeline");
  assert.equal(projection.calls.created, 1, "no second renderer");
  assert.equal(projection.calls.updates.at(-1).mode, "timeline");
  await page.setView("layers");
  assert.equal(projection.calls.updates.at(-1).mode, "layers");
});

test("Escape clears the selection in the inspector and in the active view", async () => {
  const { page, doc, projection } = makePage();
  await page.load();
  page.pickSceneId("task:bd-2");
  doc.fire("keydown", { key: "Escape" });
  assert.equal(page.inspector.selection, null);
  assert.equal(projection.calls.updates.at(-1).selectedId, null);
  assert.doesNotMatch(page.nodes.overview.textContent, /Open task/);
});

test("the Map view keeps the existing DOM map and creates no 3D renderer", async () => {
  const space = stubSpace();
  const { page, nodes, projection } = makePage({ space, view: "map" });
  await page.load();
  assert.equal(page.view, "map");
  assert.equal(nodes.map.hidden, false);
  assert.equal(nodes.space.hidden, true);
  assert.equal(nodes.projection.hidden, true);
  assert.equal(projection.calls.created, 0);
  assert.equal(space.calls.loaded, 0);
  assert.ok(nodes.map.findAll("gridNode").length > 0, "the fallback map is drawn");
  assert.equal(page.canvas !== null, true);
});

test("the page's status and refresh still work while a projection is shown", async () => {
  const calls = [];
  const { page, nodes } = makePage({
    loadMap: async (id, { etag }) => {
      calls.push(etag);
      return { notModified: calls.length > 1, etag: '"a"', map: sampleMap() };
    },
  });
  await page.load();
  assert.match(nodes.status.textContent, /Updated/);
  await page.load({ force: true });
  assert.match(nodes.status.textContent, /Unchanged/);
  assert.deepEqual(calls, ["", ""], "a manual refresh asks for the map itself");
});

test("stopping the page releases the renderers", async () => {
  const space = stubSpace();
  const { page } = makePage({ space, view: "space" });
  await page.load();
  await page.setView("space");
  assert.equal(space.calls.created, 1);
  page.stop();
  assert.equal(space.calls.destroyed, 1);
  assert.equal(page.spaceView, null);
});

test("coming back after a pagehide rebuilds the active renderer", async () => {
  const { page, projection } = makePage();
  await page.load();
  page.stop();
  assert.equal(projection.calls.destroyed, 1);
  assert.equal(page.projection, null);
  await page.start();
  assert.equal(page.projection !== null, true);
  assert.equal(projection.calls.created, 2, "a stopped page is brought back, not left blank");
});

test("grid.html carries four view containers, the switch and no outside resource", () => {
  const html = readFileSync(GRID_HTML, "utf8");
  assert.match(html, /id="grid-map"/);
  assert.match(html, /id="grid-space"[^>]*hidden/);
  assert.match(html, /id="grid-projection"[^>]*hidden/);
  assert.match(html, /id="grid-office"[^>]*hidden/);
  assert.match(html, /<select id="grid-view"/);
  assert.match(html, /<option value="space">/);
  assert.match(html, /<option value="office"[^>]*selected[^>]*>/);
  assert.match(html, /<option value="layers">/);
  assert.match(html, /<option value="timeline">/);
  assert.match(html, /<option value="map">/);
  assert.match(html, /Readable Layers/, "the shell explains the accessible alternative");
  assert.doesNotMatch(html, /id="grid-depth"/);
  assert.doesNotMatch(html, /https?:\/\//);
});

test("the 3D module is only ever a dynamic import", () => {
  const pageSource = readFileSync(fileURLToPath(new URL("../web_chat/js/grid/page.js", import.meta.url)), "utf8");
  const mainSource = readFileSync(fileURLToPath(new URL("../web_chat/js/grid/main.js", import.meta.url)), "utf8");
  for (const [name, source] of [["page.js", pageSource], ["main.js", mainSource]]) {
    assert.doesNotMatch(source, /from\s+["']\.\/space(\.js)?["']/, `${name} imports no Space module statically`);
    assert.doesNotMatch(source, /from\s+["'][^"']*space_math(\.js)?["']/, `${name} imports no math module statically`);
  }
  assert.match(pageSource, /import\("\.\/space\.js"\)/, "the page imports Space when, and only when, it is chosen");
});

test("the tree depth is real CSS, and the 3D viewport keeps its height", () => {
  const css = readFileSync(GRID_CSS, "utf8");
  assert.match(css, /data-depth/);
  assert.match(css, /gridSpace__viewport/);
  assert.match(css, /min-height: 600px/);
  assert.match(css, /gridProjection__row/);
});

test("scene ids name the canonical entities both ways", () => {
  const map = sampleMap();
  for (const id of [
    "chat:c1",
    "message:c1:m1",
    "execution:c1:turn:1",
    "execution:sub:c1:op-agent",
    "operation:c1:op-read",
    "task:bd-1",
  ]) {
    const selection = selectionOfSceneId(id, { map });
    assert.ok(selection, `${id} names a selection`);
    assert.equal(sceneIdOfSelection(selection, { map }), id, `${id} roundtrips`);
  }
  assert.equal(selectionOfSceneId("chat:", { map }), null);
  assert.equal(selectionOfSceneId("message:c1", { map }), null);
  assert.equal(selectionOfSceneId("operation:c1", { map }), null);
  // An id that already carries the exact node wins over re-parsing.
  assert.equal(sceneIdOfSelection({ kind: "operation", chatId: "c1", id: "other", sceneId: "operation:c1:exact" }), "operation:c1:exact");
});

test("a canonical chat node clicked in Layers reaches the inspector and every view", async () => {
  const { page, projection } = makePage();
  await page.load();
  page.pickSceneId("chat:c1");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(page.inspector.selection.kind, "chat");
  assert.equal(page.inspector.selection.id, "c1");
  assert.equal(projection.calls.updates.at(-1).selectedId, "chat:c1");
  assert.equal(page.scene.nodes.some((node) => node.kind === "context"), false);
  assert.equal(page.scene.nodes.some((node) => node.kind === "activity"), false);
  assert.match(page.nodes.overview.textContent, /Open chat/);
});

// -- Office integration -----------------------------------------------------
test("Office is lazy, passes real detail only after a desk selection, and shares inspector", async () => {
  let reads = 0;
  const { page, office, space, nodes } = makePage({
    loadChatDetail: async () => { reads += 1; return { detail: sampleDetail() }; },
  });
  await page.load();
  assert.equal(office.calls.loaded, 0);
  await page.chooseView("office");
  assert.equal(office.calls.created, 1);
  assert.equal(space.calls.loaded, 0);
  assert.equal(nodes.office.hidden, false);
  assert.equal(nodes.projection.hidden, true);
  assert.equal(reads, 0);
  const options = office.calls.options[0];
  assert.equal(options.scene, page.scene);
  options.onSelect("chat:c1");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(reads, 1);
  assert.equal(page.inspector.selection.id, "c1");
  assert.equal(office.calls.focus.length, 0, "plain selection does not fly the camera");
  const update = office.calls.updates.at(-1);
  assert.equal(update.selectedId, "chat:c1");
  assert.deepEqual(update.detailChatIds, ["c1"]);
  assert.ok(update.scene.nodes.some((node) => node.kind === "operation"));
  assert.match(nodes.overview.textContent, /Executions/);
  await page.setView("layers");
  assert.equal(office.calls.destroyed, 1);
  assert.equal(page.sceneId(), "chat:c1");
  await page.setView("office");
  assert.equal(office.calls.created, 2);
  assert.equal(reads, 1);
  page.stop();
  assert.equal(office.calls.destroyed, 2);
});

test("failed Office and lost context show Layers without losing selection or storing failure", async () => {
  const office = stubOffice({ fail: "create" });
  const { page, storage } = makePage({ office });
  await page.load();
  page.pickSceneId("task:bd-1");
  await page.chooseView("office");
  assert.equal(page.view, "layers");
  assert.equal(page.sceneId(), "task:bd-1");
  assert.equal(storage.getItem(VIEW_KEY), null);
  assert.match(page.nodes.status.textContent, /WebGL2/);

  const good = makePage({ view: "office" });
  await good.page.load();
  await good.page.viewReady;
  good.office.calls.options[0].onUnavailable("Context lost");
  await good.page.viewReady;
  assert.equal(good.page.view, "layers");
  assert.match(good.nodes.status.textContent, /Context lost/);
});

test("mobile/reduced-motion stored Office never downloads a 3D module", async () => {
  for (const settings of [{ width: 390, reducedMotion: false }, { width: 1280, reducedMotion: true }]) {
    const { page, office, space } = makePage({
      view: null,
      storage: fakeStorage({ [VIEW_KEY]: "office" }),
      page: { ...settings, webgl: true },
    });
    await page.load();
    assert.equal(page.view, "layers");
    assert.equal(office.calls.loaded, 0);
    assert.equal(space.calls.loaded, 0);
  }
});

test("Office imports resolving after switch or stop never resurrect the renderer", async () => {
  for (const leave of ["switch", "stop"]) {
    let resolve;
    const office = stubOffice();
    const deferred = new Promise((done) => { resolve = done; });
    const { page } = makePage({ office, page: { loadOffice: () => deferred } });
    await page.load();
    const pending = page.setView("office");
    if (leave === "switch") await page.setView("layers");
    else page.stop();
    resolve(await office.load());
    await pending;
    assert.equal(office.calls.created, 0);
    assert.equal(page.officeView, null);
  }
});

test("Office detail loading/error is explicit and no extra API is called", async () => {
  let reject;
  const pending = new Promise((_resolve, fail) => { reject = fail; });
  const { page, office } = makePage({ view: "office", loadChatDetail: () => pending });
  await page.load();
  await page.viewReady;
  office.calls.options[0].onSelect("chat:c1");
  assert.equal(office.calls.updates.at(-1).detailStates.c1.status, "loading");
  reject(new Error("Detail offline"));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(office.calls.updates.at(-1).detailStates.c1.status, "error");
  assert.equal(office.calls.updates.at(-1).detailChatIds.length, 0);
});
