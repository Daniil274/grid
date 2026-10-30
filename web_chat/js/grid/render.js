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
import { layoutMap } from "./layout.js";

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

/** The dependency lines, under the nodes. */
function edgesLayer(geometry) {
  const svg = svgEl("svg", {
    class: "gridMap__edges",
    width: geometry.width,
    height: geometry.height,
    viewBox: `0 0 ${geometry.width} ${geometry.height}`,
    "aria-hidden": "true",
    focusable: "false",
  });
  for (const edge of geometry.edges) {
    svg.append(
      svgEl("line", {
        class: "gridMap__edge",
        x1: edge.x1,
        y1: edge.y1,
        x2: edge.x2,
        y2: edge.y2,
        "data-from": edge.from,
        "data-to": edge.to,
      }),
    );
  }
  return svg;
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
export function taskNode(node, projectId, origin) {
  const state = stateOf(node.item);
  const priority = priorityTag(node.item);
  const box = h(
    "a.gridNode.gridNode--task",
    {
      href: taskHref(projectId, node.id),
      dataset: { state: state.key, task: node.id },
      title: `${node.title} · ${state.label}${priority ? ` · ${priority}` : ""}${when(node.item?.updated_at) ? ` · ${when(node.item.updated_at)}` : ""}`,
    },
    h("span.gridNode__mark", { "aria-hidden": "true", text: state.mark }),
    h("span.gridNode__title", { text: node.title }),
    h("span.gridNode__meta", { text: state.label }),
  );
  return place(box, node, origin);
}

/** One chat of the processes region: active and awaiting approval stand out. */
export function processNode(node, origin) {
  const chat = node.item ?? {};
  const status = chat.awaiting_approval ? "Awaiting approval" : chat.active ? "Active" : "Idle";
  const box = h(
    "a.gridNode.gridNode--process",
    {
      href: chatHref(node.id),
      dataset: { active: String(Boolean(chat.active)), awaiting: String(Boolean(chat.awaiting_approval)) },
      title: `${node.title} · ${status}${when(chat.updated_at) ? ` · ${when(chat.updated_at)}` : ""}`,
    },
    h("span.gridNode__mark", { "aria-hidden": "true", text: chat.awaiting_approval ? "!" : chat.active ? "●" : "○" }),
    h("span.gridNode__title", { text: node.title }),
    h("span.gridNode__meta", { text: status }),
  );
  return place(box, node, origin);
}

/** One top-level entry of the project's space; a link only when files_url is known. */
export function fileNode(node, filesUrl, origin) {
  const entry = node.item ?? {};
  const isDir = entry.kind === "dir";
  const isFile = entry.kind === "file";
  const href = fileHref(filesUrl, entry);
  const spec = href ? "a.gridNode.gridNode--file" : "div.gridNode.gridNode--file";
  const props = {
    dataset: { kind: entry.kind || "other" },
    title: `${node.title}${isDir ? "/" : ""}${entry.size == null ? "" : ` · ${entry.size} B`}${when(entry.modified) ? ` · ${when(entry.modified)}` : ""}`,
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

const sectionOf = (map, region) => map?.[region] ?? null;

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
  bd_failed: "the server log has bd's own message",
  timeout: "the tracker did not answer in time",
  invalid_answer: "the tracker answered with something unreadable",
  tracker_too_large: "the tracker is too large to read here",
  no_chats: "the server cannot list this project's chats",
  unreadable: "the project's space cannot be read",
  not_a_folder: "the project's space is not a folder",
  files_timeout: "the files did not answer in time",
};

/** "No data: <reason>" plus a hint per code - never a fake for an unreadable section. */
const unavailableNote = (section) => {
  const hint = REASON_HINTS[text(section?.reason_code)];
  return h("p.gridMap__note.gridMap__note--off", {
    text: `No data: ${text(section?.reason) || "unavailable"}${hint ? ` (${hint})` : ""}`,
  });
};

const truncatedNote = (section) =>
  section?.truncated && Array.isArray(section.items) && section.items.length < Number(section.total ?? 0)
    ? h("p.gridMap__note.gridMap__note--foot", { text: `Showing ${section.items.length} of ${section.total}.` })
    : null;

function regionFrame(region, geometry, map, projectId) {
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
    if (region === "tasks") box.append(taskNode(node, projectId, origin));
    else if (region === "processes") box.append(processNode(node, origin));
    else box.append(fileNode(node, map?.files_url, origin));
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
 * @param {{projectId?:string, geometry?:object}} [options]
 */
export function renderMap(container, map, { projectId = "", geometry = null } = {}) {
  const placed = geometry ?? layoutMap(map);
  const canvas = h("div.gridMap__canvas", {
    style: { width: `${placed.width}px`, height: `${placed.height}px` },
    dataset: { map: projectId },
  });
  canvas.append(edgesLayer(placed));
  for (const region of ["tasks", "processes", "files"]) canvas.append(regionFrame(region, placed, map, projectId));
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

const chatLine = (chat) =>
  h(
    "li.gridOverview__item",
    {},
    h("span.gridOverview__mark", { "aria-hidden": "true", text: chat.awaiting_approval ? "!" : "●" }),
    h("a.gridOverview__link", { href: chatHref(chat.chat_id), text: text(chat.title) || text(chat.chat_id) }),
    h("span.gridOverview__meta", { text: chat.awaiting_approval ? "Awaiting approval" : "Active" }),
  );

/**
 * The compact panel beside the map: the counts, what is blocked, what is
 * working right now, the files, the sub-projects - and, where a section
 * could not be read, the reason instead of an empty pretence.
 */
export function renderOverview(container, map, { projectId = "" } = {}) {
  const tasks = Array.isArray(map?.tasks?.items) ? map.tasks.items : [];
  const processes = Array.isArray(map?.processes?.items) ? map.processes.items : [];
  const files = Array.isArray(map?.files?.items) ? map.files.items : [];
  const parts = [h("h2.gridOverview__title", { text: "Overview" })];

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
  if (map?.processes?.available === false) {
    parts.push(overviewSection("Processes", unavailableNote(map.processes)));
  } else {
    const working = processes.filter((chat) => chat.active || chat.awaiting_approval);
    parts.push(
      overviewSection(
        "Processes",
        working.length
          ? h("ul.gridOverview__list", {}, ...working.slice(0, 8).map(chatLine))
          : h("p.gridOverview__note", { text: "No chat is working right now." }),
        truncatedNote(map.processes),
      ),
    );
  }

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

