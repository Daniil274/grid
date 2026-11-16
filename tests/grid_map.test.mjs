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
const GRID_CSS = fileURLToPath(new URL("../web_chat/styles/grid.css", import.meta.url));

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
    chats: {
      items: [
        {
          chat_id: "c1", title: "Chat one", updated_at: "2026-10-04T10:00:00Z", active: true, awaiting_approval: false,
          task_links: [{ task_id: "bd-1", relation: "turn" }, { task_id: "bd-2", relation: "plan" }],
          task_links_total: 2,
        },
        {
          chat_id: "c2", title: "Chat two", updated_at: "2026-10-04T11:00:00Z", active: true, awaiting_approval: true,
          task_links: [{ task_id: "bd-2", relation: "turn" }],
          task_links_total: 7,
        },
        {
          chat_id: "c3", title: "Chat three", updated_at: "2026-10-03T11:00:00Z", active: false, awaiting_approval: false,
          task_links: [],
          task_links_total: 0,
        },
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

/** Every straight segment of a routed line, in order. */
const segmentsOf = (route) => {
  const points = route.points ?? [];
  const segments = [];
  for (let index = 0; index < points.length - 1; index += 1) {
    segments.push([points[index], points[index + 1]]);
  }
  return segments;
};

/** Whether a segment passes through the inside of a card (its edge is fine). */
const crosses = (a, b, box) => {
  const x0 = Math.min(a.x, b.x);
  const x1 = Math.max(a.x, b.x);
  const y0 = Math.min(a.y, b.y);
  const y1 = Math.max(a.y, b.y);
  return x1 > box.x && x0 < box.x + box.width && y1 > box.y && y0 < box.y + box.height;
};

/** Whether a point lies on the boundary of a card, not inside it. */
const onBoundary = (point, box) => {
  const inside =
    point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height;
  const edge =
    Math.abs(point.y - box.y) < 1e-9 ||
    Math.abs(point.y - (box.y + box.height)) < 1e-9 ||
    Math.abs(point.x - box.x) < 1e-9 ||
    Math.abs(point.x - (box.x + box.width)) < 1e-9;
  return inside && edge;
};

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

test("dependency and work routes do not depend on input list order", () => {
  const first = layoutMap(stressMap());
  const reversed = stressMap();
  reversed.tasks.items.forEach((task) => task.depends_on?.reverse());
  reversed.chats.items.forEach((chat) => chat.task_links?.reverse());
  assert.deepEqual(layoutMap(reversed).edges, first.edges);
  assert.deepEqual(layoutMap(reversed).links, first.links);
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
  const empty = layoutMap({ tasks: { available: true, items: [] }, chats: { items: [] }, files: { available: true, items: [] } });
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

/** Every process node's place and size by chat id: what "does not move" means. */
const processPlaces = (geometry) =>
  Object.fromEntries(geometry.nodes.processes.map((node) => [node.id, `${node.x},${node.y},${node.width},${node.height}`]));

const processCard = (root, chatId) =>
  nodesOf(root, "gridNode--process").find((node) => node.getAttribute("href") === `/?conversation=${chatId}`);

test("a chat card carries its status and the number of linked tasks, no step", () => {
  const root = el("div");
  renderMap(root, sampleMap(), { projectId: "p1" });

  const working = processCard(root, "c1");
  assert.match(textOf(working), /Active/);
  assert.match(textOf(working), /2 tasks/);
  assert.match(working.getAttribute("title"), /2 tasks/);
  assert.doesNotMatch(working.getAttribute("title"), /Active:/, "no step is said");
  assert.equal(working.findAll("gridNode__step").length, 0, "the map's chats carry no steps");

  // The server's total counts more links than the visible cards show.
  const waiting = processCard(root, "c2");
  assert.match(textOf(waiting), /Awaiting approval/);
  assert.match(textOf(waiting), /7 tasks/);

  const idle = processCard(root, "c3");
  assert.match(textOf(idle), /Idle/);
  assert.doesNotMatch(textOf(idle), /\d+ tasks/, "a chat with no link says nothing about tasks");
  assert.equal(idle.findAll("gridNode__links").length, 0);
});

test("a new link changes the words of a card, never its place or size", () => {
  const before = layoutMap(sampleMap());
  const later = sampleMap();
  const chat = later.chats.items[0];
  chat.active = false;
  chat.awaiting_approval = false;
  chat.task_links = Array.from({ length: 40 }, (_, index) => ({ task_id: `bd-${index}`, relation: "plan" }));
  chat.task_links_total = 40;
  const after = layoutMap(later);

  assert.deepEqual(processPlaces(after), processPlaces(before), "the links move nothing");
  assert.deepEqual(after.regions.processes, before.regions.processes);
  assert.deepEqual(
    after.nodes.tasks.map((node) => `${node.x},${node.y}`),
    before.nodes.tasks.map((node) => `${node.x},${node.y}`),
  );
  assert.equal(after.height, before.height, "nor does the canvas grow");

  const root = el("div");
  renderMap(root, later, { projectId: "p1" });
  assert.match(textOf(processCard(root, "c1")), /40 tasks/);
  assert.doesNotMatch(textOf(processCard(root, "c1")), /Last:/);
});

test("a process card reads the server's own split of its links", () => {
  const map = sampleMap();
  map.chats.items[0].task_link_counts = { turn: 2, plan: 1 };
  map.chats.items[1].task_link_counts = { turn: 1, plan: 0 };
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  const working = processCard(root, "c1");
  assert.match(textOf(working), /2 touched · 1 planned/, "both kinds, in the server's numbers");
  assert.match(working.getAttribute("title"), /2 touched · 1 planned/);
  const waiting = processCard(root, "c2");
  assert.match(textOf(waiting), /1 touched/, "a plan of none is not named");
  assert.doesNotMatch(textOf(waiting), /planned/);
  assert.doesNotMatch(textOf(waiting), /7 tasks/, "the split replaces the bare total");
});

test("unknown counts fall back to the total; a known empty split says nothing", () => {
  const map = sampleMap();
  // Half a split is not a split: nothing is invented about the missing half.
  map.chats.items[0].task_link_counts = { turn: 2 };
  map.chats.items[1].task_link_counts = null;
  map.chats.items.push({
    chat_id: "c4", title: "Chat four", active: false, awaiting_approval: false,
    task_links: [], task_links_total: 5, task_link_counts: { turn: 0, plan: 0 },
  });
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  assert.match(textOf(processCard(root, "c1")), /2 tasks/, "the old total when the split is unknown");
  assert.match(textOf(processCard(root, "c2")), /7 tasks/);
  const empty = processCard(root, "c4");
  assert.doesNotMatch(textOf(empty), /\d+ (touched|planned|task)/, "an empty split is not dressed up");
});

test("the map carries a key to its lines, and every line has a direction", () => {
  const root = el("div");
  renderMap(root, sampleMap(), { projectId: "p1" });
  const legend = root.find("gridMap__legend");
  assert.ok(legend, "the key is there");
  assert.deepEqual(
    legend.findAll("gridMap__legendItem").map((item) => item.dataset.relation),
    ["turn", "plan", "depends"],
  );
  assert.match(textOf(legend), /Last turn/);
  assert.match(textOf(legend), /In plan/);
  assert.match(textOf(legend), /Depends on/);
  // The key sits in the title band, where no node can be.
  const geometry = layoutMap(sampleMap());
  const top = parseFloat(legend.style.top);
  assert.ok(top >= geometry.regions.tasks.y && top < geometry.nodes.tasks[0].y, "above the first row of cards");

  const deps = root.findAll("gridMap__edge");
  assert.equal(deps.length, 2);
  for (const path of deps) {
    assert.equal(path.getAttribute("data-relation"), "depends");
    assert.match(path.getAttribute("marker-end"), /^url\(#/, "a direction, as an arrow");
    assert.match(path.querySelector("title").textContent, /depends on/);
    assert.match(path.getAttribute("d"), /^M /);
  }
  const lines = root.findAll("gridMap__link");
  for (const path of lines) {
    assert.match(path.getAttribute("marker-end"), /^url\(#/);
    assert.match(path.querySelector("title").textContent, /→|Last turn|In plan/);
  }
  const turn = lines.find((path) => path.getAttribute("data-relation") === "turn");
  const plan = lines.find((path) => path.getAttribute("data-relation") === "plan");
  assert.notEqual(turn.getAttribute("marker-end"), plan.getAttribute("marker-end"), "each kind points its own way");
});

test("links join a task to a chat only where both are on the map, once per pair", () => {
  const map = sampleMap();
  map.chats.items[0].task_links = [
    { task_id: "bd-1", relation: "plan" },
    { task_id: "bd-1", relation: "turn" }, // the same pair again: one line, the turn wins
    { task_id: "bd-2", relation: "plan" },
    { task_id: "bd-404", relation: "turn" }, // a task this map does not show
    { task_id: "bd-3", relation: "invented" }, // not a relation the server sends
    { task_id: "bd-3", relation: "constructor" }, // a name borrowed from Object is not one either
  ];
  map.chats.items[1].task_links = [{ task_id: "bd-3", relation: "turn" }];

  const geometry = layoutMap(map);
  assert.deepEqual(
    geometry.links.map((link) => `${link.from}->${link.to}:${link.relation}`).sort(),
    ["bd-1->c1:turn", "bd-2->c1:plan", "bd-3->c2:turn"],
  );
  // The dependency edges answer another question and are left as they were.
  assert.deepEqual(
    geometry.edges.map((edge) => `${edge.from}->${edge.to}`).sort(),
    ["bd-1->bd-2", "bd-1->bd-3"],
  );

  // A route meets a card on its boundary, never at its centre: it leaves the
  // task from its bottom edge and enters the chat through its top one.
  for (const link of geometry.links) {
    const task = geometry.nodes.tasks.find((node) => node.id === link.from);
    const chat = geometry.nodes.processes.find((node) => node.id === link.to);
    assert.deepEqual(link.points[0], { x: link.x1, y: link.y1 });
    assert.deepEqual(link.points.at(-1), { x: link.x2, y: link.y2 });
    assert.equal(link.y1, task.y + task.height, `${link.from} leaves its task's bottom edge`);
    assert.ok(link.x1 > task.x && link.x1 < task.x + task.width, `${link.from} leaves through a port, not a corner`);
    assert.equal(link.y2, chat.y, `${link.to} is entered through its top edge`);
    assert.ok(link.x2 > chat.x && link.x2 < chat.x + chat.width);
  }

  // A map whose chats name nothing gets no links at all.
  const quiet = sampleMap();
  for (const chat of quiet.chats.items) chat.task_links = [];
  assert.deepEqual(layoutMap(quiet).links, []);
});

test("links are drawn under the nodes, a turn and a plan as different lines", () => {
  const root = el("div");
  renderMap(root, sampleMap(), { projectId: "p1" });
  const canvas = root.find("gridMap__canvas");
  const order = canvas.childNodes.map((child) => {
    if (child.classList.contains("gridMap__edges")) return "edges";
    if (child.classList.contains("gridMap__links")) return "links";
    if (child.classList.contains("gridMap__legend")) return "legend";
    return `region:${child.dataset.region}`;
  });
  assert.deepEqual(
    order,
    ["edges", "links", "region:tasks", "region:processes", "region:files", "legend"],
    "lines under every node, the key over them but in the title band",
  );

  const lines = canvas.findAll("gridMap__link");
  assert.deepEqual(
    lines
      .map((line) => `${line.getAttribute("data-task")}->${line.getAttribute("data-chat")}:${line.getAttribute("data-relation")}`)
      .sort(),
    ["bd-1->c1:turn", "bd-2->c1:plan", "bd-2->c2:turn"],
  );
  // The two kinds must stay apart for the eye, not only in the attribute:
  // a solid thread for a turn, a dashed one for a plan.
  const css = readFileSync(GRID_CSS, "utf8");
  assert.match(css, /\.gridMap__link\[data-relation=turn\][^{]*\{[^}]*stroke:[^}]*\}/);
  assert.match(css, /\.gridMap__link\[data-relation=plan\][^{]*\{[^}]*stroke-dasharray:[^}]*\}/);
  assert.match(css, /@media \(max-width: 720px\)[^@]*\.gridMap__links \{ display: none; \}/, "the flow layout draws no lines");
});

/**
 * A project that puts the routing under stress: several rows of tasks (so the
 * gaps between them are real), chats in both process columns, and more
 * relations than any one strip carries alone.
 */
function stressMap() {
  const tasks = [];
  for (let index = 0; index < 22; index += 1) {
    tasks.push({
      id: `bd-${index}`,
      title: `Task ${index}`,
      state: index % 5 === 0 ? "blocked" : "ready",
      created_at: `2026-10-01T00:${String(index).padStart(2, "0")}:00Z`,
      depends_on: index ? [`bd-${index - 1}`] : [],
    });
  }
  tasks[7].depends_on = ["bd-0", "bd-2"];
  // A dependency inside one row: it reaches the task's bottom edge, where the
  // task's own links leave, so the two must not be given the same port.
  tasks[6].depends_on = ["bd-4", "bd-5"];
  tasks[21].depends_on = ["bd-1", "bd-9", "bd-14"];
  const chats = [];
  for (let index = 0; index < 9; index += 1) {
    chats.push({
      chat_id: `c${index}`,
      title: `Chat ${index}`,
      active: index % 2 === 0,
      awaiting_approval: false,
      task_links: tasks
        .slice(index, index + 6)
        .map((task, offset) => ({ task_id: task.id, relation: offset % 2 ? "plan" : "turn" })),
      task_links_total: 6,
      task_link_counts: { turn: 3, plan: 3 },
    });
  }
  return sampleMap({
    tasks: { available: true, reason: null, items: tasks, total: tasks.length, truncated: false },
    chats: { items: chats, total: chats.length, truncated: false },
  });
}

test("every route is orthogonal and no segment crosses a card", () => {
  const geometry = layoutMap(stressMap());
  assert.ok(geometry.nodes.tasks.length > 8, "several rows of tasks");
  assert.ok(geometry.nodes.processes.length > 4, "chats in both process columns");
  assert.ok(geometry.regions.processes.height > 2 * (92 + 14), "more than one row of chats");
  assert.ok(geometry.links.length > 15);
  const boxes = [...geometry.nodes.tasks, ...geometry.nodes.processes, ...geometry.nodes.files];
  for (const route of [...geometry.edges, ...geometry.links]) {
    assert.ok(route.points.length >= 2, `${route.from}->${route.to} is a route`);
    for (const [a, b] of segmentsOf(route)) {
      assert.ok(a.x === b.x || a.y === b.y, `${route.relation} ${route.from}->${route.to} has a diagonal segment`);
      for (const box of boxes) {
        assert.ok(!crosses(a, b, box), `${route.relation} ${route.from}->${route.to} runs through ${box.id}`);
      }
    }
    assert.match(route.d, /^M /, "the path the SVG is drawn from");
  }
});

test("a route joins its cards on their boundary, and a turn and a plan keep apart", () => {
  const geometry = layoutMap(stressMap());
  const tasks = new Map(geometry.nodes.tasks.map((node) => [node.id, node]));
  const chats = new Map(geometry.nodes.processes.map((node) => [node.id, node]));
  for (const edge of geometry.edges) {
    const from = tasks.get(edge.from);
    const to = tasks.get(edge.to);
    assert.ok(onBoundary(edge.points[0], from), `${edge.from} is left on its boundary`);
    assert.ok(onBoundary(edge.points.at(-1), to), `${edge.to} is entered on its boundary`);
    assert.equal(edge.relation, "depends", "a dependency is typed as one");
  }
  for (const link of geometry.links) {
    const task = tasks.get(link.from);
    const chat = chats.get(link.to);
    assert.ok(onBoundary(link.points[0], task), `${link.from} is left on its boundary`);
    assert.ok(onBoundary(link.points.at(-1), chat), `${link.to} is entered on its boundary`);
  }
  // The corridor between the regions: one lane for a turn, another for a plan.
  const { tasks: tasksRegion, processes: processesRegion } = geometry.regions;
  const right = tasksRegion.x + tasksRegion.width;
  const left = processesRegion.x;
  const laneOf = (link) => {
    const point = link.points.find((candidate) => candidate.x > right && candidate.x < left);
    assert.ok(point, `${link.from}->${link.to} passes through the corridor`);
    return point.x;
  };
  const turns = new Set(geometry.links.filter((link) => link.relation === "turn").map(laneOf));
  const plans = new Set(geometry.links.filter((link) => link.relation === "plan").map(laneOf));
  assert.ok(turns.size && plans.size, "both kinds are drawn");
  for (const turn of turns) {
    for (const plan of plans) assert.notEqual(turn, plan, "a turn and a plan never share a lane");
  }
});

test("no two routes meet one card at the same point of one edge", () => {
  const geometry = layoutMap(stressMap());
  const cards = [...geometry.nodes.tasks, ...geometry.nodes.processes];
  // Every place where a route touches a card, grouped by the card and its edge.
  const meeting = new Map();
  const note = (box, point) => {
    const bottom = Math.abs(point.y - (box.y + box.height)) < 1e-9;
    const top = Math.abs(point.y - box.y) < 1e-9;
    if (!bottom && !top) return;
    const key = `${box.id}:${bottom ? "bottom" : "top"}`;
    const list = meeting.get(key);
    if (list) list.push(point.x);
    else meeting.set(key, [point.x]);
  };
  for (const route of [...geometry.edges, ...geometry.links]) {
    for (const box of cards) {
      const first = route.points[0];
      const last = route.points.at(-1);
      if (onBoundary(first, box)) note(box, first);
      if (onBoundary(last, box)) note(box, last);
    }
  }
  assert.ok([...meeting.keys()].some((key) => key.endsWith(":bottom")), "routes do use the bottom edges");
  assert.ok(
    [...meeting.keys()].some((key) => key.startsWith("bd-6:")),
    "the task with a same-row dependency and links meets both kinds on its bottom edge",
  );
  for (const [key, xs] of meeting) {
    for (let left = 0; left < xs.length; left += 1) {
      for (let right = left + 1; right < xs.length; right += 1) {
        assert.ok(
          Math.abs(xs[left] - xs[right]) >= 1,
          `${key} has two routes at one point: ${xs[left]} and ${xs[right]}`,
        );
      }
    }
  }
});

test("a same-row dependency and a link out of one task leave by different ports", () => {
  // The smallest reproducer of the report: two tasks in one row, the second
  // depending on the first and a chat's last turn on the second.
  const map = sampleMap({
    tasks: {
      available: true,
      reason: null,
      items: [
        { id: "bd-0", title: "Zero", state: "ready", created_at: "2026-10-01T00:00:00Z", depends_on: [] },
        { id: "bd-1", title: "One", state: "ready", created_at: "2026-10-01T00:01:00Z", depends_on: ["bd-0"] },
      ],
      total: 2,
      truncated: false,
    },
    chats: {
      items: [{ chat_id: "c0", title: "Chat", task_links: [{ task_id: "bd-1", relation: "turn" }] }],
      total: 1,
      truncated: false,
    },
  });
  const geometry = layoutMap(map);
  const task = geometry.nodes.tasks.find((node) => node.id === "bd-1");
  const dependency = geometry.edges.find((edge) => edge.to === "bd-1");
  const link = geometry.links.find((entry) => entry.from === "bd-1");
  assert.ok(dependency && link, "both lines are drawn");
  assert.equal(task.row, geometry.nodes.tasks.find((node) => node.id === "bd-0").row, "one row");

  const dependencyPort = dependency.points.at(-1);
  const linkPort = link.points[0];
  assert.equal(dependencyPort.y, task.y + task.height, "the dependency enters by the bottom edge");
  assert.equal(linkPort.y, task.y + task.height, "the link leaves by the bottom edge");
  assert.ok(Math.abs(dependencyPort.x - linkPort.x) >= 1, "one card, two points on that edge");

  // And the two runs along the strip under the row keep a gap: they must not
  // read as one line that goes on from the dependency into the chat.
  const stripY = dependency.points[1].y;
  const dependencyRun = dependency.points.filter((point) => point.y === stripY).map((point) => point.x);
  const linkRun = link.points.filter((point) => point.y === stripY).map((point) => point.x);
  assert.ok(dependencyRun.length && linkRun.length, "both runs use the strip under the row");
  assert.ok(
    Math.max(...dependencyRun) < Math.min(...linkRun),
    "the dependency's run ends before the link's begins",
  );

  // The same answer whatever order the server listed the cards in.
  const shuffled = clone(map);
  shuffled.tasks.items.reverse();
  shuffled.chats.items.reverse();
  assert.deepEqual(layoutMap(shuffled).edges, geometry.edges);
  assert.deepEqual(layoutMap(shuffled).links, geometry.links);
});

test("the geometry of a stress map stays the same when a chat works", () => {
  const before = layoutMap(stressMap());
  const later = stressMap();
  later.chats.items[0].active = true;
  const after = layoutMap(later);
  assert.deepEqual(
    after.nodes.tasks.map((node) => `${node.id}:${node.x},${node.y}`),
    before.nodes.tasks.map((node) => `${node.id}:${node.x},${node.y}`),
  );
  assert.deepEqual(
    after.nodes.processes.map((node) => `${node.id}:${node.x},${node.y}`),
    before.nodes.processes.map((node) => `${node.id}:${node.x},${node.y}`),
  );
  assert.deepEqual(after.edges, before.edges);
  assert.deepEqual(after.links, before.links);
});

test("no step and no link is invented for a chat that has none", () => {
  const map = sampleMap();
  map.chats.items = [{ chat_id: "c9", title: "Quiet chat", active: false, awaiting_approval: false }];
  assert.deepEqual(layoutMap(map).links, []);

  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  assert.equal(root.findAll("gridMap__links").length, 0, "no links, no layer");
  const card = nodesOf(root, "gridNode--process")[0];
  assert.equal(card.findAll("gridNode__step").length, 0);
  assert.equal(card.findAll("gridNode__links").length, 0);
  assert.match(textOf(card), /Idle/);
});

test("the overview names the same working chats and counts as the cards", () => {
  const overview = el("div");
  renderOverview(overview, sampleMap(), { projectId: "p1" });
  assert.match(textOf(overview), /Chat one/);
  assert.match(textOf(overview), /2 tasks/);
  assert.match(textOf(overview), /Chat two/);
  assert.match(textOf(overview), /7 tasks/);
  // The overview names what is working, not every chat of the project, and it
  // says no step: the map's chats carry none.
  assert.doesNotMatch(textOf(overview), /Chat three/);
  assert.doesNotMatch(textOf(overview), /Active:|Last:/);
});

test("a legacy steps array on a chat is not drawn", () => {
  const map = sampleMap();
  map.chats.items[0].steps = ["read the map answer", "drew the process cards"];
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });

  const card = processCard(root, "c1");
  assert.equal(card.findAll("gridNode__step").length, 0, "the map's chats carry no steps");
  assert.doesNotMatch(textOf(card), /read the map answer/);
  assert.doesNotMatch(card.getAttribute("title"), /drew the process cards/);
  assert.doesNotMatch(textOf(overview), /read the map answer/);
});

test("the map is built from the canonical chats section", () => {
  const map = sampleMap();
  assert.equal(map.chats.items.length, 3);
  assert.equal(map.processes, undefined, "the retired key is not what the page reads");
  const geometry = layoutMap(map);
  assert.equal(geometry.nodes.processes.length, 3, "one card per chat of the answer");
  assert.equal(geometry.links.length, 3, "the links the chats name");

  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  assert.deepEqual(
    nodesOf(root, "gridNode--process").map((node) => node.dataset.chat),
    ["c1", "c2", "c3"],
    "each card is one chat of the answer",
  );
});

test("an unavailable chats section says so instead of an empty region", () => {
  const map = sampleMap();
  map.chats = {
    available: false,
    reason: "This project's chats cannot be listed on this server.",
    reason_code: "no_chats",
    items: [],
    total: 0,
    truncated: false,
    counts: { active: 0, awaiting_approval: 0 },
  };
  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  const region = root.findAll("gridMap__region").find((box) => box.dataset.region === "processes");
  assert.match(textOf(region), /No data: This project's chats cannot be listed/);
  assert.equal(nodesOf(root, "gridNode--process").length, 0);
  assert.equal(root.findAll("gridMap__link").length, 0, "no links to cards that are not drawn");

  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.match(textOf(overview), /No data: This project's chats cannot be listed/);
});

test("the overview lists the real connections as words, and none it cannot draw", () => {
  const map = sampleMap();
  map.chats.items[0].task_links = [
    ...map.chats.items[0].task_links,
    { task_id: "bd-404", relation: "turn" },
    { task_id: "bd-1", relation: "depends" },
  ];
  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  const items = nodesOf(overview, "gridOverview__connection");
  assert.deepEqual(
    items.map((item) => item.dataset.relation),
    ["turn", "plan", "turn"],
    "a stable order - by chat, then by task - and nothing the map cannot draw",
  );
  const first = items[0];
  assert.deepEqual(
    first.querySelectorAll("a").map((node) => `${node.textContent}:${node.getAttribute("href")}`),
    ["Alpha:/tasks?project=p1&task=bd-1", "Chat one:/?conversation=c1"],
    "both ends are the links the rest of the page uses",
  );
  assert.match(textOf(first), /→/);
  assert.match(textOf(first), /Last turn/);
  assert.match(textOf(items[1]), /In plan/);
  assert.doesNotMatch(textOf(overview), /bd-404/, "an unseen task is not named");
  assert.doesNotMatch(textOf(overview), /Depends on/, "a dependency is not listed among the connections");
});

test("the connection list is bounded and says how much of it is shown", () => {
  const map = sampleMap();
  const tasks = Array.from({ length: 20 }, (_, index) => ({
    id: `bd-t${index}`,
    title: `Task ${index}`,
    state: "ready",
    created_at: `2026-10-01T00:${String(index).padStart(2, "0")}:00Z`,
    depends_on: [],
  }));
  map.tasks = { available: true, items: tasks, total: 20, truncated: false };
  map.chats.items = [
    {
      chat_id: "c1", title: "Chat one", active: true, awaiting_approval: false,
      task_links: tasks.slice(0, 16).map((task) => ({ task_id: task.id, relation: "turn" })),
      task_links_total: 16,
    },
    {
      chat_id: "c2", title: "Chat two", active: true, awaiting_approval: false,
      task_links: tasks.slice(16).map((task) => ({ task_id: task.id, relation: "plan" })),
      task_links_total: 4,
    },
  ];
  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.equal(nodesOf(overview, "gridOverview__connection").length, 12);
  assert.match(textOf(overview), /Showing 12 of 20\./);
});

test("with no real connection the overview claims none", () => {
  const map = sampleMap();
  for (const chat of map.chats.items) chat.task_links = [];
  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.doesNotMatch(textOf(overview), /Connections/);
  assert.equal(nodesOf(overview, "gridOverview__connection").length, 0);
});

test("an unavailable section hides its routes but keeps recorded Connections readable", () => {
  const map = sampleMap();
  map.tasks.available = false;
  map.tasks.reason = "Tracker unavailable";
  const geometry = layoutMap(map);
  // The answer still carries the items it recorded: three tasks and the three
  // task/chat links the chats name. Availability gates the drawing, not data.
  assert.equal(geometry.nodes.tasks.length, map.tasks.items.length);
  assert.equal(geometry.nodes.tasks.length, 3);
  assert.equal(geometry.links.length, 3);
  const viewport = el("div");
  renderMap(viewport, map, { projectId: "p1" });
  assert.equal(nodesOf(viewport, "gridMap__edge").length, 0);
  assert.equal(nodesOf(viewport, "gridMap__link").length, 0);
  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  // The recorded connections stay readable as words even when nothing is drawn.
  assert.equal(nodesOf(overview, "gridOverview__connection").length, 3);
  assert.match(textOf(overview), /Tracker unavailable/);
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

test("a failed tracker read shows the server's safe diagnosis, never its stderr", () => {
  const map = sampleMap();
  map.tasks = {
    available: false,
    reason: "The project's local tracker could not be read.",
    reason_code: "bd_failed",
    diagnosis: {
      code: "docker_image_missing",
      message: "The isolation image is missing on this server; pull or build it, then reload the map.",
      exit_code: 125,
    },
    items: [],
    total: 0,
    truncated: false,
    counts: {},
  };

  const root = el("div");
  renderMap(root, map, { projectId: "p1" });
  const tasks = root.findAll("gridMap__region").find((region) => region.dataset.region === "tasks");
  assert.match(textOf(tasks), /Diagnosis: The isolation image is missing on this server/);
  assert.match(textOf(tasks), /\(exit 125\)/);
  assert.doesNotMatch(textOf(tasks), /stderr|token=|\/srv\//);

  const overview = el("div");
  renderOverview(overview, map, { projectId: "p1" });
  assert.match(textOf(overview), /Diagnosis: The isolation image is missing on this server/);
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
  map.chats.items[0].title = "<script>alert(1)</script>";
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
