/**
 * The map page (web_chat/grid.html): the geography of js/grid/layout.js, how
 * js/grid/render.js shows a project's tasks, chats and files, the refresh
 * rhythm of js/grid/live.js and the page itself over a stubbed API.
 *
 * The two things worth guarding here are promises to the reader: the map does
 * not rearrange itself (same set of tasks, same places; a status change moves
 * nothing; a new task takes the next free place), and a section the server
 * could not read says so instead of drawing a comforting blank.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { layoutMap, orderTasks, CANVAS } = await import("../web_chat/js/grid/layout.js");
const {
  renderMap,
  renderOverview,
  renderBreadcrumb,
  renderProjectPicker,
  buildLabel,
  taskHref,
  chatHref,
  fileHref,
  stateOf,
} = await import("../web_chat/js/grid/render.js");
const { createPoller } = await import("../web_chat/js/grid/live.js");
const { GridPage, projectFromSearch } = await import("../web_chat/js/grid/page.js");

const el = (tag) => document.createElement(tag);

const GRID_HTML = fileURLToPath(new URL("../web_chat/grid.html", import.meta.url));
const INDEX_HTML = fileURLToPath(new URL("../web_chat/index.html", import.meta.url));

/** A project as GET /api/projects/{id}/map answers for it. */
function sampleMap(overrides = {}) {
  return {
    project: { id: "p1", title: "Grid", parent_id: null, description: "The workspace itself", updated_at: "2026-10-30T05:00:00Z" },
    path: [{ id: "p0", title: "Studio" }, { id: "p1", title: "Grid" }],
    children: [{ id: "p2", title: "Map slice" }],
    tasks: {
      available: true,
      reason: null,
      items: [
        { id: "bd-1", title: "Alpha", status: "open", state: "ready", priority: 0, type: "feature", assignee: null, created_at: "2026-10-01T09:00:00Z", updated_at: "2026-10-02T09:00:00Z", depends_on: [], close_reason: null },
        { id: "bd-2", title: "Beta", status: "in_progress", state: "in_progress", priority: 1, type: "task", assignee: "me", created_at: "2026-10-02T09:00:00Z", updated_at: "2026-10-03T09:00:00Z", depends_on: ["bd-1"], close_reason: null },
        { id: "bd-3", title: "Gamma", status: "blocked", state: "blocked", priority: 2, type: "bug", assignee: null, created_at: "2026-10-03T09:00:00Z", updated_at: "2026-10-04T09:00:00Z", depends_on: ["bd-1", "missing"], close_reason: null },
      ],
      total: 3,
      truncated: false,
      counts: { ready: 1, in_progress: 1, blocked: 1 },
    },
    processes: {
      items: [
        { chat_id: "c1", title: "Chat one", updated_at: "2026-10-04T10:00:00Z", active: true, awaiting_approval: false },
        { chat_id: "c2", title: "Chat two", updated_at: "2026-10-04T11:00:00Z", active: true, awaiting_approval: true },
        { chat_id: "c3", title: "Chat three", updated_at: "2026-10-03T11:00:00Z", active: false, awaiting_approval: false },
      ],
      total: 3,
      truncated: false,
    },
    files: {
      available: true,
      reason: null,
      items: [
        { name: "docs", path: "docs", kind: "dir", size: 0, modified: "2026-10-04T10:00:00Z" },
        { name: "README.md", path: "README.md", kind: "file", size: 1200, modified: "2026-10-04T09:00:00Z" },
      ],
      total: 2,
      truncated: false,
    },
    files_url: "/files?context=p1&path=project",
    server: { version: "1.2.3", commit: "abcdef0", source: "git", reason_code: null, reason: null },
    ...overrides,
  };
}

const clone = (value) => JSON.parse(JSON.stringify(value));

/** The coordinates of every task node, by id: what "does not move" means. */
const taskPlaces = (geometry) =>
  Object.fromEntries(geometry.nodes.tasks.map((node) => [node.id, `${node.x},${node.y}`]));

const nodesOf = (root, className) => root.findAll(className);

const textOf = (root) => root.textContent;

// -- layout: the promise that the map does not rearrange itself ------------------

test("the same task set always gives the same coordinates", () => {
  const first = layoutMap(sampleMap());
  const second = layoutMap(sampleMap());
  assert.deepEqual(taskPlaces(first), taskPlaces(second));
  assert.deepEqual(first.edges, second.edges);
  assert.equal(first.width, CANVAS.width);
  assert.ok(first.height >= CANVAS.minHeight);
  // The answer's order does not matter either: the set is what decides.
  const shuffled = sampleMap();
  shuffled.tasks.items.reverse();
  assert.deepEqual(taskPlaces(layoutMap(shuffled)), taskPlaces(first));
});

test("a status change moves nothing", () => {
  const before = taskPlaces(layoutMap(sampleMap()));
  const changed = sampleMap();
  changed.tasks.items[0].state = "closed";
  changed.tasks.items[1].state = "blocked";
  const after = taskPlaces(layoutMap(changed));
  assert.deepEqual(after, before);
});

test("a new task takes the next free place and moves no existing one", () => {
  const before = layoutMap(sampleMap());
  const grown = sampleMap();
  grown.tasks.items.push({
    id: "bd-4", title: "Delta", status: "open", state: "ready", priority: 3, type: "task",
    assignee: null, created_at: "2026-10-05T09:00:00Z", updated_at: "2026-10-05T09:00:00Z", depends_on: [], close_reason: null,
  });
  grown.tasks.total = 4;
  const after = layoutMap(grown);

  for (const [id, place] of Object.entries(taskPlaces(before))) assert.equal(taskPlaces(after)[id], place);
  const added = after.nodes.tasks.find((node) => node.id === "bd-4");
  assert.equal(added.index, before.nodes.tasks.length);
  assert.equal(added.row, 0, "four tasks still fit one row");
  assert.ok(added.x > after.nodes.tasks[2].x);
});

test("edges exist only between tasks that are on the map", () => {
  const geometry = layoutMap(sampleMap());
  const pairs = geometry.edges.map((edge) => `${edge.from}->${edge.to}`).sort();
  assert.deepEqual(pairs, ["bd-1->bd-2", "bd-1->bd-3"], "a dependency on a task that is not here draws nothing");
  for (const edge of geometry.edges) {
    assert.equal(typeof edge.x1, "number");
    assert.equal(typeof edge.y2, "number");
  }
});

test("three fixed regions: tasks, processes and files", () => {
  const geometry = layoutMap(sampleMap());
  assert.deepEqual(Object.keys(geometry.regions), ["tasks", "processes", "files"]);
  const { tasks, processes, files } = geometry.regions;
  assert.ok(tasks.x < processes.x, "processes sit beside the tasks");
  assert.ok(files.y > processes.y, "files sit under the processes");
  // The regions do not move when the project has more or fewer items.
  const empty = layoutMap({ tasks: { available: true, items: [] }, processes: { items: [] }, files: { available: true, items: [] } });
  assert.deepEqual(empty.regions.tasks.x, tasks.x);
  assert.deepEqual(empty.regions.processes.y, processes.y);
});

test("orderTasks is by creation, then by id - never by state", () => {
  const ordered = orderTasks([
    { id: "b", created_at: "2026-10-02T00:00:00Z", state: "blocked" },
    { id: "a", created_at: "2026-10-01T00:00:00Z", state: "closed" },
    { id: "c", created_at: "2026-10-01T00:00:00Z", state: "ready" },
  ]);
  assert.deepEqual(ordered.map((task) => task.id), ["a", "c", "b"]);
});

// -- rendering: the states, and the honest blanks ------------------------------

test("every task is a node with a mark, a title and its state in words", () => {
  const root = el("div");
  renderMap(root, sampleMap(), { projectId: "p1" });
  const labels = nodesOf(root, "gridNode--task").map((node) => `${node.dataset.state}:${node.getAttribute("href")}`);
  assert.deepEqual(labels, [
    "ready:/tasks?project=p1&task=bd-1",
    "in_progress:/tasks?project=p1&task=bd-2",
    "blocked:/tasks?project=p1&task=bd-3",
  ]);
  const blocked = nodesOf(root, "gridNode--task").find((node) => node.dataset.state === "blocked");
  assert.match(textOf(blocked), /Gamma/);
  assert.match(textOf(blocked), /!/, "a state is a mark...");
  assert.match(textOf(blocked), /Blocked/, "...and a word, not only a colour");
  assert.equal(nodesOf(root, "gridNode__mark").length, 3 + 3 + 2, "every node of every region carries its mark");
});

test("a node is placed inside its region, at the spot the layout gave it", () => {
  const map = sampleMap();
  const geometry = layoutMap(map);
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });

  const regions = Object.fromEntries(root.findAll("gridMap__region").map((box) => [box.dataset.region, box]));
  const tasks = regions.tasks;
  assert.equal(tasks.style.left, `${geometry.regions.tasks.x}px`);
  assert.equal(tasks.style.width, `${geometry.regions.tasks.width}px`);
  const first = tasks.findAll("gridNode--task")[0];
  const node = geometry.nodes.tasks[0];
  assert.equal(first.style.left, `${node.x - geometry.regions.tasks.x}px`, "the region's corner comes off the canvas coordinate");
  assert.equal(first.style.top, `${node.y - geometry.regions.tasks.y}px`);

  // No node may leave its own region, whatever the project holds.
  for (const key of ["tasks", "processes", "files"]) {
    const box = geometry.regions[key];
    for (const item of geometry.nodes[key]) {
      assert.ok(item.x >= box.x && item.x + item.width <= box.x + box.width, `${key}: ${item.id} fits horizontally`);
      assert.ok(item.y >= box.y && item.y + item.height <= box.y + box.height, `${key}: ${item.id} fits vertically`);
    }
  }
});

test("chats link into the chat, files into the project's files", () => {
  const root = el("div");
  renderMap(root, sampleMap(), { projectId: "p1" });
  assert.deepEqual(
    nodesOf(root, "gridNode--process").map((node) => node.getAttribute("href")),
    ["/?conversation=c1", "/?conversation=c2", "/?conversation=c3"],
  );
  const active = nodesOf(root, "gridNode--process").filter((node) => node.dataset.active === "true");
  assert.equal(active.length, 2);
  assert.equal(nodesOf(root, "gridNode--process").filter((node) => node.dataset.awaiting === "true").length, 1);
  assert.deepEqual(
    nodesOf(root, "gridNode--file").map((node) => node.getAttribute("href")),
    ["/files?context=p1&path=project%2Fdocs", "/files?context=p1&path=project&file=project%2FREADME.md"],
  );
});

test("a result is not a link when the server names no files_url", () => {
  const root = el("div");
  renderMap(root, sampleMap({ files_url: null }), { projectId: "p1" });
  const files = nodesOf(root, "gridNode--file");
  assert.equal(files.length, 2);
  assert.deepEqual(files.map((node) => node.getAttribute("href")), [null, null]);
  assert.match(textOf(files[0]), /docs/);
});

test("an unreadable section says no data and why, in the map and in the overview", () => {
  const map = sampleMap();
  map.tasks = { available: false, reason: "bd is not installed", reason_code: "bd_unavailable", items: [], total: 0, truncated: false, counts: {} };
  map.files = { available: false, reason: "the project space is unavailable", items: [], total: 0, truncated: false };

  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  assert.match(textOf(root.findAll("gridMap__region").find((region) => region.dataset.region === "tasks")), /No data: bd is not installed/);
  // The hint is chosen by the typed code, never by parsing the sentence.
  assert.match(textOf(root.findAll("gridMap__region").find((region) => region.dataset.region === "tasks")), /no bd runtime here/);
  assert.equal(nodesOf(root, "gridNode--task").length, 0);

  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.match(textOf(overview), /No data: bd is not installed/);
  assert.match(textOf(overview), /No data: the project space is unavailable/);
  // A section without a code reads exactly as before: no hint is invented.
  assert.doesNotMatch(textOf(overview), /the project space is unavailable \(/);
  // It never invents progress: no counts chip for a section it could not read.
  assert.equal(nodesOf(overview, "gridOverview__chip").length, 0);
});

test("a truncated section says how much of it is shown", () => {
  const map = sampleMap();
  map.tasks = { ...map.tasks, total: 512, truncated: true };
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  assert.match(textOf(root), /Showing 3 of 512\./);

  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.match(textOf(overview), /Showing 3 of 512\./);
  assert.match(textOf(overview), /512 total/);
});

test("the overview names what is blocked and what is working", () => {
  const overview = el("div");
  renderOverview(overview, sampleMap(), { projectId: "p1" });
  const text = textOf(overview);
  assert.match(text, /1 ready/);
  assert.match(text, /1 blocked/);
  assert.match(text, /Gamma/, "the blocked task is named");
  assert.match(text, /Chat one/);
  assert.match(text, /Awaiting approval/);
  assert.doesNotMatch(text, /Chat three/, "an idle chat is not called working");
  assert.match(text, /2 top-level entries/);
  assert.match(text, /Map slice/, "a sub-project is a way in");
});

test("the breadcrumb links the path, the project last and unlinked", () => {
  const crumb = el("div");
  renderBreadcrumb(crumb, sampleMap(), { projectId: "p1" });
  const links = crumb.querySelectorAll("a").map((node) => `${node.textContent}:${node.getAttribute("href")}`);
  assert.deepEqual(links, ["Studio:/map?project=p0"]);
  const current = crumb.find("gridBreadcrumb__current");
  assert.equal(current.textContent, "Grid");
  assert.equal(current.getAttribute("aria-current"), "page");
});

test("a path of parents only still ends on the project itself", () => {
  // The API's path is the ancestors; the project is what the page shows.
  const map = sampleMap();
  map.path = [{ id: "p0", title: "Studio" }];
  const crumb = el("div");
  renderBreadcrumb(crumb, map, { projectId: "p1" });
  assert.deepEqual(
    crumb.querySelectorAll("a").map((node) => node.getAttribute("href")),
    ["/map?project=p0"],
  );
  assert.equal(crumb.find("gridBreadcrumb__current").textContent, "Grid");
});

test("with no project the page is a picker of projects", () => {
  const overview = el("div");
  renderProjectPicker(overview, [{ id: "p1", title: "Grid" }, { id: "p2", title: "Backend" }]);
  assert.deepEqual(
    overview.querySelectorAll("a").map((node) => `${node.textContent}:${node.getAttribute("href")}`),
    ["Grid:/map?project=p1", "Backend:/map?project=p2"],
  );
  renderProjectPicker(overview, []);
  assert.match(textOf(overview), /There are no projects yet/);
});

test("text from the server lands as text, never as markup", () => {
  const map = sampleMap();
  map.tasks.items[0].title = '<img src=x onerror="alert(1)">';
  map.processes.items[0].title = "<script>alert(1)</script>";
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.equal(root.querySelectorAll("img").length, 0);
  assert.equal(root.querySelectorAll("script").length, 0);
  assert.equal(overview.querySelectorAll("script").length, 0);
  assert.match(textOf(root), /<script>alert\(1\)<\/script>/);
});

test("helpers: a state, a task link, a query parameter", () => {
  assert.deepEqual(stateOf({ state: "closed" }), { key: "closed", label: "Closed", mark: "✓" });
  assert.equal(stateOf({ state: "weird" }).label, "weird", "an unknown state says what it is");
  assert.equal(stateOf(null).key, "unknown");
  assert.equal(taskHref("p 1", "bd/2"), "/tasks?project=p%201&task=bd%2F2");
  assert.equal(chatHref("c1"), "/?conversation=c1");
  assert.equal(fileHref("/files?context=c&path=project", { path: "a b", kind: "dir" }), "/files?context=c&path=project%2Fa+b");
  assert.equal(fileHref("/files?context=c&path=project", { path: "a.md", kind: "file" }), "/files?context=c&path=project&file=project%2Fa.md");
  assert.equal(fileHref("/files", { path: "a.md", kind: "file" }), "/files?file=a.md");
  assert.equal(fileHref("", { path: "a.md", kind: "file" }), "");
  assert.equal(projectFromSearch("?project=p1"), "p1");
  assert.equal(projectFromSearch(""), "");
});

// -- the refresh rhythm ---------------------------------------------------------

function fakeDocument(visibility = "visible") {
  const doc = {
    visibilityState: visibility,
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] ??= []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] ?? []).filter((entry) => entry !== fn);
    },
    /** The browser saying the page was hidden or shown. */
    show(state) {
      this.visibilityState = state;
      for (const fn of this.listeners.visibilitychange ?? []) fn();
    },
  };
  return doc;
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

test("the map refreshes on its interval while visible, and never while hidden", () => {
  const doc = fakeDocument("visible");
  const timers = fakeTimers();
  let loads = 0;
  const poller = createPoller({ load: () => { loads += 1; }, intervalMs: 20000, doc, timers }).start();

  assert.equal(timers.armed.length, 1);
  assert.equal(timers.armed[0].ms, 20000);
  timers.armed[0].fn();
  assert.equal(loads, 1);

  // Hidden: the timer goes away and the page asks nothing.
  doc.show("hidden");
  assert.equal(timers.live.length, 0);
  timers.armed[0].fn();
  assert.equal(loads, 1, "a tick of a hidden page does not fetch");

  // Shown again: one refresh at once, and the rhythm restarts.
  doc.show("visible");
  assert.equal(loads, 2);
  assert.equal(timers.live.length, 1);
  timers.live[0].fn();
  assert.equal(loads, 3);
});

test("a page that starts hidden waits to be seen", () => {
  const doc = fakeDocument("hidden");
  const timers = fakeTimers();
  let loads = 0;
  const poller = createPoller({ load: () => { loads += 1; }, doc, timers }).start();
  assert.equal(timers.armed.length, 0);
  assert.equal(loads, 0);
  doc.show("visible");
  assert.equal(loads, 1);
});

test("stopping the poller clears the timer and the listener", () => {
  const doc = fakeDocument("visible");
  const timers = fakeTimers();
  let loads = 0;
  const poller = createPoller({ load: () => { loads += 1; }, doc, timers }).start();
  poller.stop();
  assert.equal(timers.live.length, 0);
  assert.equal((doc.listeners.visibilitychange ?? []).length, 0);
  doc.show("visible");
  assert.equal(loads, 0);
});

// -- the page over a stubbed API ------------------------------------------------

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

test("the page shows a map, then revalidates it with the ETag it got", async () => {
  const calls = [];
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async (id, { etag }) => {
      calls.push({ id, etag });
      if (calls.length === 1) return { notModified: false, etag: '"abc"', map: sampleMap() };
      return { notModified: true, etag };
    },
    listProjects: async () => [],
  });

  await page.load();
  assert.equal(calls[0].id, "p1");
  assert.equal(calls[0].etag, "");
  assert.equal(nodes.title.textContent, "Grid");
  assert.equal(nodes.map.findAll("gridNode__title").length, 3 + 3 + 2);
  assert.match(textOf(nodes.overview), /Overview/);
  assert.match(nodes.status.textContent, /Updated \d\d:\d\d:\d\d · refreshes every 20 s/);

  await page.load();
  assert.equal(calls[1].etag, '"abc"', "a refresh carries the previous answer's ETag");
  assert.equal(nodes.map.findAll("gridNode__title").length, 3 + 3 + 2, "a 304 keeps what is on screen");
  assert.match(nodes.status.textContent, /Unchanged/);

  await page.load({ force: true });
  assert.equal(calls[2].etag, "", "a manual refresh asks for the map itself");
});

test("the header names the short commit the server runs", async () => {
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => ({ notModified: false, etag: "", map: sampleMap() }),
    listProjects: async () => [],
  });

  await page.load();

  assert.equal(nodes.build.textContent, "build abcdef0");
  assert.match(nodes.build.title, /commit abcdef0/);
  assert.equal(nodes.build.dataset.state, "known");
  // Nothing but the short commit: no full hash, no checkout path.
  assert.doesNotMatch(nodes.build.textContent, /[0-9a-f]{8,}/);
});

test("an unknown build says so with the server's own reason", async () => {
  const nodes = pageNodes();
  const unknown = sampleMap({
    server: {
      version: "1.2.3", commit: null, source: "unknown",
      reason_code: "build_commit_unknown",
      reason: "This server does not know which commit of the code it runs.",
    },
  });
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => ({ notModified: false, etag: "", map: unknown }),
    listProjects: async () => [],
  });

  await page.load();
  assert.equal(nodes.build.textContent, "build unknown");
  assert.match(nodes.build.title, /does not know which commit/);
  assert.equal(nodes.build.dataset.state, "unknown");
});

test("buildLabel never shows a long value as a commit", () => {
  const full = buildLabel({ commit: "abcdef0" });
  assert.equal(full.text, "build abcdef0");
  assert.equal(buildLabel({ commit: null, reason: "no .git here" }).text, "build unknown");
  assert.match(buildLabel({ commit: null, reason: "no .git here" }).title, /no \.git here/);
});

test("a manual Refresh button asks again", async () => {
  let loads = 0;
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => {
      loads += 1;
      return { notModified: false, etag: "", map: sampleMap() };
    },
    listProjects: async () => [],
  });
  await page.load();
  nodes.refresh.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(loads, 2);
});

test("a missing project is reported, not drawn as an empty map", async () => {
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "gone",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => {
      const error = new Error("Project not found");
      error.status = 404;
      throw error;
    },
    listProjects: async () => [],
  });
  await page.load();
  assert.equal(nodes.map.hidden, true);
  assert.match(textOf(nodes.overview), /No data: Project not found/);
  assert.equal(nodes.status.textContent, "Project not found");
  assert.equal(nodes.status.dataset.tone, "error");
});

test("with no ?project the page offers the projects it can open", async () => {
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "",
    doc: fakeDocument(),
    timers: fakeTimers(),
    loadMap: async () => assert.fail("the picker asks for no map"),
    listProjects: async () => [{ id: "p1", title: "Grid" }],
  });
  await page.load();
  assert.equal(nodes.map.hidden, true);
  assert.deepEqual(nodes.overview.querySelectorAll("a").map((node) => node.getAttribute("href")), ["/map?project=p1"]);
  assert.match(nodes.status.textContent, /choose one/);
});
test("a refreshed map is cleared on authorization and missing-project errors", async () => {
  for (const status of [401, 403, 404]) {
    const nodes = pageNodes();
    let loads = 0;
    const page = new GridPage(nodes, {
      projectId: "p1", doc: fakeDocument(), timers: fakeTimers(),
      loadMap: async () => {
        loads += 1;
        if (loads === 1) return { notModified: false, etag: '"old"', map: sampleMap() };
        const error = new Error(`HTTP ${status}`);
        error.status = status;
        throw error;
      },
    });
    await page.load();
    assert.equal(nodes.map.hidden, false);
    await page.load();
    assert.equal(nodes.map.hidden, true, `${status} hides the old map`);
    assert.equal(nodes.map.findAll("gridNode__title").length, 0, "old nodes are removed, not just hidden");
    assert.equal(textOf(nodes.breadcrumb), "", "old project path is cleared too");
    assert.match(textOf(nodes.overview), new RegExp(`No data: HTTP ${status}`));
    assert.equal(page.etag, "", "a subsequent request must not validate the old snapshot");
  }
});

test("a transient refresh failure keeps old data but marks it stale", async () => {
  const nodes = pageNodes();
  let loads = 0;
  const page = new GridPage(nodes, {
    projectId: "p1", doc: fakeDocument(), timers: fakeTimers(),
    loadMap: async () => {
      loads += 1;
      if (loads === 1) return { notModified: false, etag: '"old"', map: sampleMap() };
      throw new Error("network down");
    },
  });
  await page.load();
  await page.load();
  assert.equal(nodes.map.hidden, false);
  assert.match(nodes.status.textContent, /stale data/i);
  assert.match(textOf(nodes.overview), /Stale data · last successful update/);
});

test("stale time is the last success and a 304 clears the stale warning", async () => {
  const nodes = pageNodes();
  let loads = 0;
  const page = new GridPage(nodes, {
    projectId: "p1", doc: fakeDocument(), timers: fakeTimers(),
    loadMap: async () => {
      loads += 1;
      if (loads === 1) return { etag: '"old"', map: sampleMap() };
      if (loads === 2) throw new Error("offline");
      return { notModified: true };
    },
  });
  await page.load();
  page.checkedAt = "01:02:03";
  await page.load();
  assert.equal(page.staleAt, "01:02:03");
  assert.match(nodes.status.textContent, /stale data from 01:02:03/);
  await page.load();
  assert.equal(page.staleAt, "");
  assert.doesNotMatch(textOf(nodes.overview), /Stale data/);
  assert.match(nodes.status.textContent, /Unchanged/);
});

test("starting the page loads once and then keeps the rhythm", async () => {
  let loads = 0;
  const timers = fakeTimers();
  const nodes = pageNodes();
  const page = new GridPage(nodes, {
    projectId: "p1",
    doc: fakeDocument(),
    timers,
    loadMap: async () => {
      loads += 1;
      return { notModified: false, etag: "", map: sampleMap() };
    },
    listProjects: async () => [],
  });
  await page.start();
  assert.equal(loads, 1);
  assert.equal(timers.live.length, 1);
  timers.live[0].fn();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(loads, 2);
  page.stop();
  assert.equal(timers.live.length, 0);
});

// -- the shell ------------------------------------------------------------------

test("grid.html asks for nothing from outside the server", () => {
  const html = readFileSync(GRID_HTML, "utf8");
  assert.doesNotMatch(html, /https?:\/\//i, "no CDN, no external stylesheet or font");
  assert.doesNotMatch(html, /(src|href)\s*=\s*["']\/\//i, "no protocol-relative URL either");
  assert.match(html, /\/static\/js\/grid\/main\.js/);
  assert.match(html, /\/static\/styles\/grid\.css/);
  assert.match(html, /<html lang="en">/);
});

test("the chat's rail has a Map link, and it follows the active project", () => {
  const html = readFileSync(INDEX_HTML, "utf8");
  assert.match(html, /id="open-grid" href="\/map"/);
  const main = readFileSync(fileURLToPath(new URL("../web_chat/js/main.js", import.meta.url)), "utf8");
  assert.match(main, /gridLink\.href = activeProject \? `\/map\?project=/);
  assert.match(main, /store\.watch\(\["activeProject"\], syncGridLink\)/);
});
