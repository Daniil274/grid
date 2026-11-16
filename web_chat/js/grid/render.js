/**
 * What the project map looks like (web_chat/grid.html).
 *
 * Everything is built with `h()`: every string from the server - a task title,
 * a chat title, a file name, a reason - becomes a text node, never markup.
 * The geometry comes from ./layout.js and is applied as inline pixel positions
 * on a fixed virtual canvas, which the page scales to the width it has.
 *
 * A state is never shown by colour alone: each node carries its mark and its
 * label, and the mark differs per state (○ ready, ◌ waiting, ● in progress,
 * ! blocked, ✓ closed). A section the server could not read says so and why.
 */

import { h, replace } from "../lib/dom.js";
import { chatsSection, layoutMap, mapChats } from "./layout.js";
import { parseFilesUrl } from "./explorer.js";

/** How each state reads; the same words and marks as the tracker page. */
export const STATES = {
  ready: { key: "ready", label: "Ready", mark: "○" },
  waiting: { key: "waiting", label: "Waiting", mark: "◌" },
  in_progress: { key: "in_progress", label: "In progress", mark: "●" },
  blocked: { key: "blocked", label: "Blocked", mark: "!" },
  closed: { key: "closed", label: "Closed", mark: "✓" },
};

/** The order the overview counts the states in. */
export const STATE_ORDER = ["ready", "waiting", "in_progress", "blocked", "closed"];

const UNKNOWN_STATE = { key: "unknown", label: "Unknown", mark: "·" };

/** The state of a task, known or not. */
export function stateOf(task) {
  const key = String(task?.state ?? "");
  return STATES[key] ?? { ...UNKNOWN_STATE, label: key || UNKNOWN_STATE.label };
}

const text = (value) => String(value ?? "");

/** "2026-10-30T05:51:08Z" -> "2026-10-30 05:51", for a hover title. */
export const when = (stamp) => (stamp ? text(stamp).replace("T", " ").slice(0, 16) : "");

const priorityTag = (task) => (task?.priority == null ? "" : `P${task.priority}`);

/**
 * A click on a card selects it - and the card keeps its `href`, so a modified
 * click (a new tab, a download), a middle click or a browser without this
 * module still opens the real thing. The inspector the page opens carries the
 * plain "Open" link for the same destination.
 */
function selectProps(selection, onSelect) {
  if (typeof onSelect !== "function") return {};
  return {
    on: {
      click: (event) => {
        if (event?.metaKey || event?.ctrlKey || event?.shiftKey || event?.altKey) return;
        if (event?.button) return;
        event?.preventDefault?.();
        onSelect(selection);
      },
    },
  };
}

/**
 * The files page's URL for one entry of the project's space.
 *
 * `files_url` opens that space's root (`/files?context=<chat>&path=project`),
 * and the files page names an entry by its path from the space's root: a
 * folder by `path`, a file by `file`. The entry's own path in the answer is
 * relative to the project's space, so the root's name goes in front of it.
 */
export function fileHref(filesUrl, entry) {
  const base = text(filesUrl);
  if (!base) return "";
  const [pathname, query = ""] = base.split("?");
  const params = new URLSearchParams(query);
  const root = params.get("path") || "";
  const relative = text(entry?.path) || text(entry?.name);
  if (!relative) return "";
  const full = root ? `${root}/${relative}` : relative;
  if (entry?.kind === "dir") params.set("path", full);
  else params.set("file", full);
  return `${pathname}?${params.toString()}`;
}

export const projectHref = (id) => `/map?project=${encodeURIComponent(text(id))}`;
export const taskHref = (projectId, taskId) =>
  `/tasks?project=${encodeURIComponent(text(projectId))}&task=${encodeURIComponent(text(taskId))}`;
export const chatHref = (chatId) => `/?conversation=${encodeURIComponent(text(chatId))}`;

function svgEl(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

/** A title inside an SVG element: what a line means, in words. */
function svgTitle(words) {
  const node = svgEl("title");
  node.textContent = words;
  return node;
}

/**
 * One arrowhead per relation, drawn in that relation's own stroke: a
 * restrained mark at the end a route points at, so a line has a direction.
 */
function arrowDefs(markers) {
  const defs = svgEl("defs");
  for (const [id, relation] of Object.entries(markers)) {
    const marker = svgEl("marker", {
      id,
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      markerUnits: "strokeWidth",
      orient: "auto-start-reverse",
    });
    marker.append(svgEl("path", { class: "gridMap__arrow", "data-relation": relation, d: "M 1 1 L 9 5 L 1 9" }));
    defs.append(marker);
  }
  return defs;
}

/** A routed line: its path, the two ends it joins, and what it means. */
function routePath(className, route, attributes, words) {
  const path = svgEl("path", { class: className, d: route.d, ...attributes });
  path.append(svgTitle(words));
  return path;
}

const nameOf = (nodes, id) => nodes.find((node) => node.id === id)?.title ?? id;

/** The dependency routes, under the nodes: one task stands on another. */
function edgesLayer(geometry) {
  const svg = svgEl("svg", {
    class: "gridMap__edges",
    width: geometry.width,
    height: geometry.height,
    viewBox: `0 0 ${geometry.width} ${geometry.height}`,
    "aria-hidden": "true",
    focusable: "false",
  });
  svg.append(arrowDefs({ gridMapArrowDepends: "depends" }));
  for (const edge of geometry.edges) {
    svg.append(
      routePath(
        "gridMap__edge",
        edge,
        {
          "data-from": edge.from,
          "data-to": edge.to,
          "data-relation": edge.relation,
          "marker-end": "url(#gridMapArrowDepends)",
        },
        `${nameOf(geometry.nodes.tasks, edge.to)} depends on ${nameOf(geometry.nodes.tasks, edge.from)}`,
      ),
    );
  }
  return svg;
}

/** The task -> chat routes, under the nodes too, each kind in its own stroke. */
function linksLayer(geometry) {
  if (!geometry.links?.length) return null;
  const svg = svgEl("svg", {
    class: "gridMap__links",
    width: geometry.width,
    height: geometry.height,
    viewBox: `0 0 ${geometry.width} ${geometry.height}`,
    "aria-hidden": "true",
    focusable: "false",
  });
  svg.append(arrowDefs({ gridMapArrowTurn: "turn", gridMapArrowPlan: "plan" }));
  for (const link of geometry.links) {
    svg.append(
      routePath(
        "gridMap__link",
        link,
        {
          "data-relation": link.relation,
          "data-task": link.from,
          "data-chat": link.to,
          "marker-end": link.relation === "turn" ? "url(#gridMapArrowTurn)" : "url(#gridMapArrowPlan)",
        },
        `${nameOf(geometry.nodes.tasks, link.from)} → ${nameOf(geometry.nodes.processes, link.to)} · ${relationLabel(link.relation)}`,
      ),
    );
  }
  return svg;
}

/** The lines the map draws, each with the words it stands for. */
export const LINE_KEY = [
  {
    relation: "turn",
    label: "Last turn",
    hint: "A solid line from a task to the chat whose last turn touched it",
  },
  {
    relation: "plan",
    label: "In plan",
    hint: "A dashed line from a task to a chat that has it in its plan",
  },
  {
    relation: "depends",
    label: "Depends on",
    hint: "An arrow from a task to the one that depends on it",
  },
];

function legendSample(relation) {
  const svg = svgEl("svg", {
    class: "gridMap__legendSample",
    width: 26,
    height: 8,
    viewBox: "0 0 26 8",
    "aria-hidden": "true",
    focusable: "false",
  });
  svg.append(svgEl("path", { class: "gridMap__legendLine", "data-relation": relation, d: "M 1 4 L 25 4" }));
  return svg;
}

/**
 * The key to the lines, at rest in the tasks region's title band: it names
 * each stroke in words and moves no node.
 */
export function legendKey(geometry) {
  return h(
    "div.gridMap__legend",
    {
      "aria-label": "Line key",
      style: {
        left: `${geometry.regions.tasks.x + geometry.regions.tasks.width - 356}px`,
        top: `${geometry.regions.tasks.y + 5}px`,
      },
    },
    ...LINE_KEY.map((row) =>
      h(
        "span.gridMap__legendItem",
        { dataset: { relation: row.relation }, title: row.hint },
        legendSample(row.relation),
        h("span.gridMap__legendLabel", { text: row.label }),
      ),
    ),
  );
}

/**
 * Put a node where the layout says, inside a region whose own corner is
 * *origin*: the layout speaks canvas coordinates, the node is a child of its
 * region, so the region's corner comes off.
 */
function place(box, node, origin = { x: 0, y: 0 }) {
  box.style.left = `${node.x - origin.x}px`;
  box.style.top = `${node.y - origin.y}px`;
  box.style.width = `${node.width}px`;
  box.style.height = `${node.height}px`;
  return box;
}

/** One task of the goals/tasks region: mark, title, state label. */
export function taskNode(node, projectId, origin, onSelect = null) {
  const state = stateOf(node.item);
  const priority = priorityTag(node.item);
  const box = h(
    "a.gridNode.gridNode--task",
    {
      href: taskHref(projectId, node.id),
      dataset: { state: state.key, task: node.id, focus: `task:${node.id}` },
      title: `${node.title} · ${state.label}${priority ? ` · ${priority}` : ""}${when(node.item?.updated_at) ? ` · ${when(node.item.updated_at)}` : ""}`,
      ...selectProps({ kind: "task", id: node.id }, onSelect),
    },
    h("span.gridNode__mark", { "aria-hidden": "true", text: state.mark }),
    h("span.gridNode__title", { text: node.title }),
    h("span.gridNode__meta", { text: state.label }),
  );
  return place(box, node, origin);
}

/**
 * How many tasks the chat is linked to: the server's own total, which counts
 * every link it knows, or the distinct tasks it listed when no total came.
 */
export function linkedTaskCount(chat) {
  const listed = Array.isArray(chat?.task_links) ? chat.task_links : [];
  const ids = new Set();
  for (const link of listed) {
    const id = text(link?.task_id).trim();
    if (id) ids.add(id);
  }
  const total = Number(chat?.task_links_total);
  return Number.isFinite(total) && total >= ids.size ? Math.floor(total) : ids.size;
}

/** "3 tasks", "1 task", or "" for none: a number is never shown bare. */
export const linkedTasksLabel = (count) => (count ? `${count} ${count === 1 ? "task" : "tasks"}` : "");

/** The words of each relation, in the map's key and in the overview. */
const RELATION_LABELS = { turn: "Last turn", plan: "In plan", depends: "Depends on" };
export function relationLabel(relation) {
  return RELATION_LABELS[text(relation)] ?? "";
}

/**
 * The server's own split of a chat's links: `turn` is how many tasks the
 * chat's last turn touched, `plan` how many are in its plan. A split it did
 * not send - or did not send as two counts - is unknown, and unknown is not
 * zero: nothing is invented about it.
 */
export function linkCounts(chat) {
  const counts = chat?.task_link_counts;
  if (!counts || typeof counts !== "object") return null;
  const turn = Number(counts.turn);
  const plan = Number(counts.plan);
  if (!Number.isFinite(turn) || !Number.isFinite(plan) || turn < 0 || plan < 0) return null;
  return { turn: Math.floor(turn), plan: Math.floor(plan) };
}

/**
 * "2 touched · 1 planned" when the server split the links, the old total when
 * it did not, "" when the chat touches nothing at all.
 */
export function linkedTasksLabelFor(chat) {
  const counts = linkCounts(chat);
  if (!counts) return linkedTasksLabel(linkedTaskCount(chat));
  const parts = [];
  if (counts.turn) parts.push(`${counts.turn} touched`);
  if (counts.plan) parts.push(`${counts.plan} planned`);
  return parts.join(" · ");
}

/**
 * One chat of the processes region: active and awaiting approval stand out,
 * and the card carries how many tasks it is linked to. A chat's steps are no
 * longer part of the map answer - they belong to its loaded detail - so none
 * is drawn, not even when an older answer still carries a `steps` array.
 */
export function processNode(node, origin, onSelect = null) {
  const chat = node.item ?? {};
  const status = chat.awaiting_approval ? "Awaiting approval" : chat.active ? "Active" : "Idle";
  const linked = linkedTasksLabelFor(chat);
  const box = h(
    "a.gridNode.gridNode--process",
    {
      href: chatHref(node.id),
      dataset: {
        active: String(Boolean(chat.active)),
        awaiting: String(Boolean(chat.awaiting_approval)),
        chat: node.id,
        focus: `process:${node.id}`,
      },
      title: `${node.title} · ${status}${linked ? ` · ${linked}` : ""}${when(chat.updated_at) ? ` · ${when(chat.updated_at)}` : ""}`,
      ...selectProps({ kind: "process", id: node.id }, onSelect),
    },
    h("span.gridNode__mark", { "aria-hidden": "true", text: chat.awaiting_approval ? "!" : chat.active ? "●" : "○" }),
    h("span.gridNode__title", { text: node.title }),
    h("span.gridNode__meta", { text: status }),
    linked ? h("span.gridNode__tail", {}, h("span.gridNode__links", { text: linked })) : null,
  );
  return place(box, node, origin);
}

/** One top-level entry of the project's space; a link only when files_url is known. */
export function fileNode(node, filesUrl, origin, onSelect = null) {
  const entry = node.item ?? {};
  const isDir = entry.kind === "dir";
  const isFile = entry.kind === "file";
  const href = fileHref(filesUrl, entry);
  // The path the entry really names in the space: "project/docs", not "docs".
  const root = parseFilesUrl(filesUrl)?.root ?? "";
  const path = root ? `${root}/${text(entry.path)}` : text(entry.path);
  const spec = href ? "a.gridNode.gridNode--file" : "div.gridNode.gridNode--file";
  const props = {
    dataset: { kind: entry.kind || "other", file: path, focus: path ? `file:${path}` : "" },
    title: `${node.title}${isDir ? "/" : ""}${entry.size == null ? "" : ` · ${entry.size} B`}${when(entry.modified) ? ` · ${when(entry.modified)}` : ""}`,
    ...selectProps({ kind: isDir ? "dir" : "file", path }, onSelect),
  };
  if (href) props.href = href;
  const box = h(
    spec,
    props,
    h("span.gridNode__mark", { "aria-hidden": "true", text: isDir ? "▸" : "·" }),
    h("span.gridNode__title", { text: node.title }),
    h("span.gridNode__meta", { text: isDir ? "Folder" : isFile ? "File" : entry.kind === "link" ? "Symbolic link" : "Other" }),
  );
  return place(box, node, origin);
}

const sectionOf = (map, region) => (region === "processes" ? chatsSection(map) : map?.[region] ?? null);

/**
 * What an operator can do about each cause, keyed by the server's typed
 * `reason_code`. The sentence in `reason` is the server's own and is shown
 * as it comes; this is only the extra line, and an unknown code adds none.
 */
export const REASON_HINTS = {
  no_space: "the project has no space of its own here",
  no_tracker: "it has no tracker of its own yet",
  foreign_tracker: "the tracker found belongs to another project",
  bd_unavailable: "no bd runtime here; the operator can run the map check",
  bd_failed: "the server names the cause when it can recognize it",
  timeout: "the tracker did not answer in time",
  invalid_answer: "the tracker answered with something unreadable",
  tracker_too_large: "the tracker is too large to read here",
  no_chats: "the server cannot list this project's chats",
  unreadable: "the project's space cannot be read",
  not_a_folder: "the project's space is not a folder",
  files_timeout: "the files did not answer in time",
};

/**
 * The build the server runs, as the map header shows it: a short commit, or
 * the server's own reason why it cannot name one. `server` is the map answer's
 * `server` object - never a full hash, never a path.
 */
export function buildLabel(server) {
  const commit = text(server?.commit);
  if (commit) {
    const source = text(server?.source);
    const version = text(server?.version);
    const details = [version && `version ${version}`, source && source !== "unknown" && `from ${source}`]
      .filter(Boolean).join(", ");
    return {
      text: `build ${commit}`,
      title: `This server runs commit ${commit}${details ? ` (${details})` : ""}`,
      known: true,
    };
  }
  const reason = text(server?.reason) || "The server did not name the commit it runs.";
  return { text: "build unknown", title: `No commit: ${reason}`, known: false };
}

/**
 * The server's safe cause of a failed read, as one sentence: a typed code and
 * its allowlisted message, with a bare exit status. The server never sends
 * bd's stderr, a path or a command here, so nothing else is shown.
 */
const diagnosisNote = (section) => {
  const diagnosis = section?.diagnosis;
  const message = text(diagnosis?.message);
  if (!message) return "";
  const exit = Number.isInteger(diagnosis?.exit_code) ? ` (exit ${diagnosis.exit_code})` : "";
  return ` Diagnosis: ${message}${exit}`;
};

/** "No data: <reason>" plus a hint per code - never a fake for an unreadable section. */
const unavailableNote = (section) => {
  const hint = REASON_HINTS[text(section?.reason_code)];
  const diagnosis = diagnosisNote(section);
  const base = `No data: ${text(section?.reason) || "unavailable"}${hint ? ` (${hint})` : ""}`;
  return h("p.gridMap__note.gridMap__note--off", {
    text: diagnosis ? `${base}.${diagnosis}` : base,
  });
};

const truncatedNote = (section) =>
  section?.truncated && Array.isArray(section.items) && section.items.length < Number(section.total ?? 0)
    ? h("p.gridMap__note.gridMap__note--foot", { text: `Showing ${section.items.length} of ${section.total}.` })
    : null;

function regionFrame(region, geometry, map, projectId, onSelect) {
  const spec = geometry.regions[region];
  const section = sectionOf(map, region);
  const box = h(
    "section.gridMap__region",
    {
      dataset: { region },
      "aria-label": spec.label,
      style: {
        left: `${spec.x}px`,
        top: `${spec.y}px`,
        width: `${spec.width}px`,
        height: `${spec.height}px`,
      },
    },
    h("h2.gridMap__regionTitle", { text: spec.label }),
  );
  if (section?.available === false) {
    box.append(unavailableNote(section));
    return box;
  }
  const nodes = geometry.nodes[region] ?? [];
  if (!nodes.length) {
    box.append(h("p.gridMap__note", { text: "Nothing here yet." }));
    return box;
  }
  for (const node of nodes) {
    const origin = { x: spec.x, y: spec.y };
    if (region === "tasks") box.append(taskNode(node, projectId, origin, onSelect));
    else if (region === "processes") box.append(processNode(node, origin, onSelect));
    else box.append(fileNode(node, map?.files_url, origin, onSelect));
  }
  const note = truncatedNote(section);
  if (note) box.append(note);
  return box;
}

/**
 * Scale the fixed canvas into the width the viewport has: down to 62%, and
 * below that the viewport scrolls, because a smaller map is unreadable.
 *
 * A transform does not change the box an element occupies, so the canvas sits
 * on a *plane* that is resized to what the canvas really paints - otherwise
 * the viewport would reserve 1240px and scroll to nowhere.
 */
export function fitMap(viewport, plane, geometry) {
  const MIN_SCALE = 0.62;
  const canvas = plane.firstElementChild ?? plane.childNodes[0];
  const apply = () => {
    const available = Number(viewport.clientWidth ?? 0);
    if (!available || !canvas?.style) return;
    const scale = Math.max(MIN_SCALE, Math.min(1, available / geometry.width));
    plane.style.width = `${Math.ceil(geometry.width * scale)}px`;
    plane.style.height = `${Math.ceil(geometry.height * scale)}px`;
    canvas.style.transform = scale < 0.999 ? `scale(${scale})` : "";
    viewport.style.height = plane.style.height;
  };
  apply();
  if (typeof ResizeObserver !== "function") return;
  viewport.__gridFitObserver?.disconnect?.();
  const observer = new ResizeObserver(apply);
  observer.observe(viewport);
  viewport.__gridFitObserver = observer;
}

/**
 * The map itself: three fixed regions, dependency edges for tasks that really
 * depend on each other, and one node per item.
 *
 * @param {HTMLElement} container - the viewport the canvas goes into
 * @param {object} map - GET /api/projects/{id}/map
 * @param {{projectId?:string, geometry?:object, onSelect?:Function}} [options]
 *   `onSelect(selection)` makes a card selectable: the click is taken and the
 *   page decides what the inspector shows; without it a card is only a link.
 */
export function renderMap(container, map, { projectId = "", geometry = null, onSelect = null } = {}) {
  const placed = geometry ?? layoutMap(map);
  const canvas = h("div.gridMap__canvas", {
    style: { width: `${placed.width}px`, height: `${placed.height}px` },
    dataset: { map: projectId },
  });
  // A recorded relation may remain readable in Connections while a section is
  // unavailable, but a route to a card that is not rendered must not be drawn.
  const drawTasks = map?.tasks?.available !== false;
  const drawProcesses = chatsSection(map)?.available !== false;
  const edgeCount = drawTasks ? placed.edges.length : 0;
  const linkCount = drawTasks && drawProcesses ? placed.links.length : 0;
  canvas.append(edgesLayer({ ...placed, edges: drawTasks ? placed.edges : [] }));
  const links = linkCount ? linksLayer(placed) : null;
  if (links) canvas.append(links);
  for (const region of ["tasks", "processes", "files"]) {
    canvas.append(regionFrame(region, placed, map, projectId, onSelect));
  }
  // The key to the lines rests in the tasks region's title band: it names the
  // strokes and moves no node.
  if (edgeCount || linkCount) canvas.append(legendKey(placed));
  const plane = h("div.gridMap__plane", {}, canvas);
  replace(container, plane);
  fitMap(container, plane, placed);
  return canvas;
}

/** The project's path, each ancestor a link, the project itself the page. */
export function renderBreadcrumb(container, map, { projectId = "" } = {}) {
  const trail = Array.isArray(map?.path) ? map.path.map((item) => ({ ...item })) : [];
  const title = text(map?.project?.title) || text(projectId);
  if (!trail.length && !title) {
    replace(container);
    return null;
  }
  // The answer's path holds the parents; the project itself is the page.
  if (text(trail.at(-1)?.id) !== text(projectId)) trail.push({ id: projectId, title });
  const list = h("ol.gridBreadcrumb", { "aria-label": "Project path" });
  trail.forEach((item, index) => {
    const last = index === trail.length - 1;
    list.append(
      h(
        "li.gridBreadcrumb__item",
        {},
        last
          ? h("span.gridBreadcrumb__current", { text: text(item?.title) || text(item?.id), "aria-current": "page" })
          : h("a.gridBreadcrumb__link", { href: projectHref(item?.id), text: text(item?.title) || text(item?.id) }),
      ),
    );
  });
  replace(container, list);
  return list;
}

/** The overview's counts: one chip per state that has tasks, plus the total. */
export function stateCounts(tasks = []) {
  const counts = {};
  for (const task of tasks) {
    const key = stateOf(task).key;
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

function overviewSection(title, ...children) {
  return h("section.gridOverview__section", {}, h("h3.gridOverview__heading", { text: title }), ...children);
}

const taskLine = (task, projectId) =>
  h(
    "li.gridOverview__item",
    {},
    h("span.gridOverview__mark", { "aria-hidden": "true", text: stateOf(task).mark }),
    h("a.gridOverview__link", { href: taskHref(projectId, task.id), text: text(task.title) || text(task.id) }),
    h("span.gridOverview__meta", { text: task.id }),
  );

/** One working chat: where it goes and what it is linked to. */
function chatLine(chat) {
  const linked = linkedTasksLabelFor(chat);
  return h(
    "li.gridOverview__item",
    {},
    h("span.gridOverview__mark", { "aria-hidden": "true", text: chat.awaiting_approval ? "!" : "●" }),
    h(
      "div.gridOverview__body",
      {},
      h("a.gridOverview__link", { href: chatHref(chat.chat_id), text: text(chat.title) || text(chat.chat_id) }),
      linked ? h("span.gridOverview__detail", { text: linked }) : null,
    ),
    h("span.gridOverview__meta", { text: chat.awaiting_approval ? "Awaiting approval" : "Active" }),
  );
}

/** How many connections the overview names before it says "showing X of Y". */
export const CONNECTIONS_LIMIT = 12;

/**
 * The task -> chat relations the map really draws, in a stable order: a chat
 * keeps its place (by id) and so does a task (by creation), so the list never
 * reshuffles itself between two answers about the same project.
 */
export function visibleConnections(geometry) {
  const tasks = new Map(geometry.nodes.tasks.map((node) => [node.id, node]));
  const chats = new Map(geometry.nodes.processes.map((node) => [node.id, node]));
  const chatOrder = new Map(geometry.nodes.processes.map((node, index) => [node.id, index]));
  const taskOrder = new Map(geometry.nodes.tasks.map((node, index) => [node.id, index]));
  return (geometry.links ?? [])
    .filter((link) => tasks.has(link.from) && chats.has(link.to))
    .map((link) => ({ relation: link.relation, task: tasks.get(link.from), chat: chats.get(link.to) }))
    .sort((a, b) => chatOrder.get(a.chat.id) - chatOrder.get(b.chat.id) || taskOrder.get(a.task.id) - taskOrder.get(b.task.id));
}

/** "Task → Chat · Last turn": both ends are links, the relation is a word. */
const connectionLine = ({ task, chat, relation }, projectId) =>
  h(
    "li.gridOverview__item.gridOverview__connection",
    { dataset: { relation } },
    h(
      "span.gridOverview__connectionPair",
      {},
      h("a.gridOverview__link", { href: taskHref(projectId, task.id), text: task.title || task.id }),
      h("span.gridOverview__arrow", { "aria-hidden": "true", text: "→" }),
      h("a.gridOverview__link", { href: chatHref(chat.id), text: chat.title || chat.id }),
    ),
    h("span.gridOverview__meta", { text: relationLabel(relation) }),
  );

/**
 * The same relations the lines draw, as words: the alternative to the map
 * where the lines are hidden - on a phone - and a key to them where they are
 * not. Dependencies are a different question and are not listed here.
 */
function connectionsSection(map, projectId) {
  const connections = visibleConnections(layoutMap(map));
  if (!connections.length) return null;
  const shown = connections.slice(0, CONNECTIONS_LIMIT);
  return overviewSection(
    "Connections",
    h(
      "ul.gridOverview__list.gridOverview__connections",
      { "aria-label": "Tasks and the chats that worked on them" },
      ...shown.map((connection) => connectionLine(connection, projectId)),
    ),
    connections.length > shown.length
      ? h("p.gridOverview__note", { text: `Showing ${shown.length} of ${connections.length}.` })
      : null,
  );
}

/**
 * The compact panel beside the map: the counts, what is blocked, what is
 * working right now, the files, the sub-projects - and, where a section
 * could not be read, the reason instead of an empty pretence.
 */
export function renderOverview(container, map, { projectId = "", inspector = null } = {}) {
  const tasks = Array.isArray(map?.tasks?.items) ? map.tasks.items : [];
  const processes = mapChats(map);
  const files = Array.isArray(map?.files?.items) ? map.files.items : [];
  const parts = [h("h2.gridOverview__title", { text: "Overview" })];
  // The inspector stands at the top of the panel, where the reader is already
  // looking; it is built by js/grid/inspector.js and is absent until a card is
  // selected.
  if (inspector) parts.push(inspector);

  if (text(map?.project?.description)) {
    parts.push(h("p.gridOverview__description", { text: text(map.project.description) }));
  }

  // Tasks: the counts, then what stands still.
  if (map?.tasks?.available === false) {
    parts.push(overviewSection("Tasks", unavailableNote(map.tasks)));
  } else {
    const counts = map?.tasks?.counts ?? stateCounts(tasks);
    const chips = STATE_ORDER.filter((key) => counts[key]).map((key) =>
      h("li.gridOverview__chip", { dataset: { state: key } }, h("span.gridOverview__mark", { "aria-hidden": "true", text: STATES[key].mark }), `${counts[key]} ${STATES[key].label.toLowerCase()}`),
    );
    const total = Number(map?.tasks?.total ?? tasks.length);
    parts.push(
      overviewSection(
        "Tasks",
        h("ul.gridOverview__chips", { "aria-label": "Tasks by state" }, ...chips, h("li.gridOverview__chip.gridOverview__chip--total", {}, `${total} total`)),
        truncatedNote(map.tasks),
      ),
    );
    const blocked = tasks.filter((task) => stateOf(task).key === "blocked");
    if (blocked.length) {
      parts.push(overviewSection("Blocked", h("ul.gridOverview__list", {}, ...blocked.slice(0, 8).map((task) => taskLine(task, projectId)))));
    }
  }

  // Processes: the chats of the project, the working ones named.
  const chats = chatsSection(map);
  if (chats?.available === false) {
    parts.push(overviewSection("Processes", unavailableNote(chats)));
  } else {
    const working = processes.filter((chat) => chat.active || chat.awaiting_approval);
    parts.push(
      overviewSection(
        "Processes",
        working.length
          ? h("ul.gridOverview__list", {}, ...working.slice(0, 8).map(chatLine))
          : h("p.gridOverview__note", { text: "No chat is working right now." }),
        truncatedNote(chats),
      ),
    );
  }

  // Connections: the relations the lines draw, said in words - the same list
  // that a phone has instead of the lines.
  const connections = connectionsSection(map, projectId);
  if (connections) parts.push(connections);

  // Files: how many top-level entries, and the way into the space.
  if (map?.files?.available === false) {
    parts.push(overviewSection("Files", unavailableNote(map.files)));
  } else {
    const counts = map?.files?.counts ?? {};
    const dirs = counts.dir ?? files.filter((entry) => entry.kind === "dir").length;
    const total = Number(map?.files?.total ?? files.length);
    parts.push(
      overviewSection(
        "Files",
        h("p.gridOverview__note", { text: `${total} top-level ${total === 1 ? "entry" : "entries"} · ${dirs} ${dirs === 1 ? "folder" : "folders"} · files are not verified artifacts` }),
        map?.files_url ? h("a.gridOverview__link.gridOverview__link--button", { href: text(map.files_url), text: "Open the files" }) : h("p.gridOverview__note", { text: "The files of this project are not open to you." }),
        truncatedNote(map.files),
      ),
    );
  }

  const children = Array.isArray(map?.children) ? map.children : [];
  if (children.length) {
    parts.push(
      overviewSection(
        "Sub-projects",
        h(
          "ul.gridOverview__list",
          {},
          ...children.map((child) =>
            h("li.gridOverview__item", {}, h("a.gridOverview__link", { href: projectHref(child.id), text: text(child.title) || text(child.id) })),
          ),
        ),
      ),
    );
  }

  replace(container, ...parts);
  return container;
}

/** The picker shown by /map with no ?project: every project is a way in. */
export function renderProjectPicker(container, projects = []) {
  const items = Array.isArray(projects) ? projects : [];
  if (!items.length) {
    replace(container, h("p.gridOverview__note", { text: "There are no projects yet. A project is made in the chat's rail." }));
    return container;
  }
  const list = h("ul.gridPicker__list", { "aria-label": "Projects" });
  for (const project of items) {
    list.append(
      h("li.gridPicker__item", {}, h("a.gridPicker__link", { href: projectHref(project.id), text: text(project.title) || text(project.id) })),
    );
  }
  replace(container, h("h2.gridOverview__title", { text: "Choose a project" }), list);
  return container;
}

