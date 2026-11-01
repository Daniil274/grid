/**
 * The geography of the project map (web_chat/grid.html).
 *
 * Three regions never move: goals/tasks on the left, processes on the right,
 * files under them. Inside a region a node's place is a pure function of the
 * set of items - not of their state, their priority or the order the server
 * happened to answer in:
 *
 * - tasks are ordered by `created_at`, then by id, so the same set always
 *   yields the same coordinates;
 * - a status change therefore never moves a task, and a new task (which is the
 *   newest by `created_at`) is appended after the ones already there.
 * - processes are ordered by chat id, so a chat keeps its place as it starts
 *   and stops working; its card shows the last step it took, and a new step
 *   never changes the card's size.
 *
 * Everything here is arithmetic on plain objects: no DOM, no measurement, no
 * clock. That is what tests/grid_map.test.mjs pins down.
 */

/** The virtual canvas the map is drawn on, in pixels; the page scales it. */
export const CANVAS = { width: 1240, minHeight: 760 };

/** Space around a region's nodes, between them, and its title bar. */
const PAD = 14;
const GAP = 14;
const HEADER = 38;
const MARGIN = 28;

/** The node sizes of each region. */
export const NODE_SIZE = {
  tasks: { width: 156, height: 64 },
  // Tall enough for a title, the run state, the last step and the link count.
  // The height is a constant, so a new step never moves a card.
  processes: { width: 184, height: 92 },
  files: { width: 184, height: 52 },
};

/** The three fixed anchors. The files region follows the processes one down. */
export const REGIONS = {
  tasks: { x: MARGIN, y: MARGIN, width: 740 },
  processes: { x: 796, y: MARGIN, width: 416 },
  files: { x: 796, width: 416 },
  /** The height the processes region keeps even with a few items. */
  minProcessesHeight: 300,
};

const text = (value) => String(value ?? "");

/** Tasks by when they were made, then by id: never by state. */
export function orderTasks(tasks = []) {
  return [...tasks].sort(
    (a, b) => text(a?.created_at).localeCompare(text(b?.created_at)) || text(a?.id).localeCompare(text(b?.id)),
  );
}

/** Processes by chat id: a chat keeps its place as it starts and stops working. */
export function orderProcesses(items = []) {
  return [...items].sort(
    (a, b) => text(a?.chat_id).localeCompare(text(b?.chat_id)) || text(a?.title).localeCompare(text(b?.title)),
  );
}

/** Files with the directories first, then by name. */
export function orderFiles(items = []) {
  return [...items].sort(
    (a, b) =>
      (a?.kind === "dir" ? 0 : 1) - (b?.kind === "dir" ? 0 : 1) || text(a?.name).localeCompare(text(b?.name)),
  );
}

/** How many nodes of *size* fit side by side in a region *width* wide. */
export function columnsFor(width, size) {
  return Math.max(1, Math.floor((width - PAD * 2 + GAP) / (size.width + GAP)));
}

/** The height a region needs for *count* nodes in *columns*. */
export function regionHeight(count, columns, size) {
  const rows = Math.max(1, Math.ceil(count / columns));
  return HEADER + PAD + rows * (size.height + GAP) + PAD;
}

/** Place *count* nodes found by *locate* inside a region anchored at *origin*. */
function place(count, columns, size, origin) {
  const placed = [];
  for (let index = 0; index < count; index += 1) {
    const column = index % columns;
    const row = Math.floor(index / columns);
    placed.push({
      x: origin.x + PAD + column * (size.width + GAP),
      y: origin.y + HEADER + row * (size.height + GAP),
      width: size.width,
      height: size.height,
      column,
      row,
      index,
    });
  }
  return placed;
}

/**
 * The whole map as geometry.
 *
 * @param {object} map - the answer of GET /api/projects/{id}/map
 * @returns {{width:number,height:number,regions:object,nodes:object,edges:Array,links:Array,columns:object}}
 */
export function layoutMap(map = {}) {
  const tasks = orderTasks(Array.isArray(map?.tasks?.items) ? map.tasks.items : []);
  const processes = orderProcesses(Array.isArray(map?.processes?.items) ? map.processes.items : []);
  const files = orderFiles(Array.isArray(map?.files?.items) ? map.files.items : []);

  const taskColumns = columnsFor(REGIONS.tasks.width, NODE_SIZE.tasks);
  const processColumns = columnsFor(REGIONS.processes.width, NODE_SIZE.processes);
  const fileColumns = columnsFor(REGIONS.files.width, NODE_SIZE.files);

  const tasksRegion = {
    region: "tasks",
    label: "Goals & tasks",
    x: REGIONS.tasks.x,
    y: REGIONS.tasks.y,
    width: REGIONS.tasks.width,
    height: Math.max(regionHeight(tasks.length, taskColumns, NODE_SIZE.tasks), CANVAS.minHeight - MARGIN * 2),
  };
  const processesRegion = {
    region: "processes",
    label: "Processes",
    x: REGIONS.processes.x,
    y: REGIONS.processes.y,
    width: REGIONS.processes.width,
    height: Math.max(regionHeight(processes.length, processColumns, NODE_SIZE.processes), REGIONS.minProcessesHeight),
  };
  const filesRegion = {
    region: "files",
    label: "Files",
    x: REGIONS.files.x,
    y: processesRegion.y + processesRegion.height + MARGIN,
    width: REGIONS.files.width,
    height: regionHeight(files.length, fileColumns, NODE_SIZE.files),
  };

  const taskCells = place(tasks.length, taskColumns, NODE_SIZE.tasks, tasksRegion);
  const taskNodes = tasks.map((task, index) => ({
    ...taskCells[index],
    id: text(task?.id),
    title: text(task?.title) || text(task?.id),
    state: text(task?.state) || "ready",
    item: task,
  }));
  const processCells = place(processes.length, processColumns, NODE_SIZE.processes, processesRegion);
  const processNodes = processes.map((chat, index) => ({
    ...processCells[index],
    id: text(chat?.chat_id),
    title: text(chat?.title) || text(chat?.chat_id),
    item: chat,
  }));
  const fileCells = place(files.length, fileColumns, NODE_SIZE.files, filesRegion);
  const fileNodes = files.map((entry, index) => ({
    ...fileCells[index],
    id: text(entry?.path) || text(entry?.name),
    title: text(entry?.name),
    item: entry,
  }));

  const tracks = { tasks: tasksRegion, processes: processesRegion, columns: taskColumns };
  const { edges, links } = routes(taskNodes, processNodes, tracks);

  return {
    width: CANVAS.width,
    height: Math.max(CANVAS.minHeight, tasksRegion.height, filesRegion.y + filesRegion.height) + MARGIN,
    regions: { tasks: tasksRegion, processes: processesRegion, files: filesRegion },
    nodes: { tasks: taskNodes, processes: processNodes, files: fileNodes },
    edges,
    // Task -> chat, kept apart from the dependencies: a different question,
    // drawn as a different line.
    links,
    corridor: corridorX(tasksRegion, processesRegion),
    columns: { tasks: taskColumns, processes: processColumns, files: fileColumns },
  };
}

// -- routing: orthogonal runs that never cross a card ------------------------
//
// A route is built from the free strips of the map: the horizontal gap between
// two rows, the vertical gap between two columns, and the corridor between the
// tasks and the processes. It leaves a card through a point on that card's own
// boundary and joins the next card the same way, so no segment ever runs
// through a card - not a task in the far column, not a chat in the second one.

/** The middle of a gap: the free line a route runs along. */
const TRACK = GAP / 2;

/** A route meets a card at least this far from a corner. */
export const PORT_MARGIN = 14;

/** Routes sharing one strip are set this far apart, so both stay visible. */
const LANES = [0, -4, 4];
const LINK_LANES = [0, -3, 3];

/** Evenly spread points along a card's horizontal edge: one per route. */
export function portsAlong(box, count) {
  const span = Math.max(0, (box?.width ?? 0) - PORT_MARGIN * 2);
  const points = [];
  for (let index = 0; index < count; index += 1) {
    points.push((box?.x ?? 0) + PORT_MARGIN + (span * (index + 1)) / (count + 1));
  }
  return points;
}

/** "M x y L x y …" for a run of points. */
export function pathOf(points = []) {
  if (!points.length) return "";
  const [first, ...rest] = points;
  return `M ${first.x} ${first.y}${rest.map((point) => ` L ${point.x} ${point.y}`).join("")}`;
}

/**
 * An orthogonal run out of its waypoints: a repeated point is dropped, and a
 * point that only continues the previous direction is folded into it, so every
 * segment left is a real turn or a real run between two of them.
 */
export function orthogonalRoute(waypoints = []) {
  const points = [];
  for (const point of waypoints) {
    if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y)) continue;
    const last = points.at(-1);
    if (!last) {
      points.push({ x: point.x, y: point.y });
      continue;
    }
    if (last.x === point.x && last.y === point.y) continue;
    const before = points.at(-2);
    if (before && ((before.x === last.x && last.x === point.x) || (before.y === last.y && last.y === point.y))) {
      last.x = point.x;
      last.y = point.y;
      continue;
    }
    points.push({ x: point.x, y: point.y });
  }
  const first = points[0] ?? { x: 0, y: 0 };
  const last = points.at(-1) ?? first;
  return { points, x1: first.x, y1: first.y, x2: last.x, y2: last.y, d: pathOf(points) };
}

/** The free strip under task row *row*. */
export const taskGapY = (region, row) =>
  region.y + HEADER + row * (NODE_SIZE.tasks.height + GAP) + NODE_SIZE.tasks.height + TRACK;

/** The free strip above task row *row*: under the region's title for the first. */
export const taskAboveY = (region, row) => (row === 0 ? region.y + HEADER - TRACK : taskGapY(region, row - 1));

/** The free vertical strip left of task column *gap* (`0 … columns`). */
export const taskGapX = (region, gap) => region.x + PAD + gap * (NODE_SIZE.tasks.width + GAP) - TRACK;

/** The free strip above process row *row*. */
export const processGapY = (region, row) =>
  region.y + HEADER + row * (NODE_SIZE.processes.height + GAP) - TRACK;

/**
 * The corridor between the two regions, one lane per relation: a turn and a
 * plan never share a line, so the eye can follow either one.
 */
export function corridorX(tasks, processes) {
  const left = tasks.x + tasks.width;
  const span = Math.max(0, (processes?.x ?? left) - left);
  return {
    left,
    span,
    turn: Math.round(left + span * 0.32),
    plan: Math.round(left + span * 0.68),
  };
}

/** The vertical gap a dependency between two tasks is routed through. */
function channelGap(from, to, columns) {
  if (from.column < to.column) return from.column + 1;
  if (to.column < from.column) return to.column + 1;
  return from.column + 1 <= columns ? from.column + 1 : from.column;
}

/** Dependencies only where they are real: both tasks are on this map. */
export function dependencySpecs(taskNodes = []) {
  const byId = new Map(taskNodes.map((node) => [node.id, node]));
  const seen = new Set();
  const specs = [];
  for (const node of taskNodes) {
    for (const dependency of node.item?.depends_on ?? []) {
      const id = text(dependency);
      if (!id || id === node.id || !byId.has(id)) continue;
      const pair = `${id}\u0000${node.id}`;
      if (seen.has(pair)) continue;
      seen.add(pair);
      specs.push({ kind: "depends", key: `depends\u0000${pair}`, from: id, to: node.id, relation: "depends" });
    }
  }
  return specs;
}

/**
 * The two kinds of work a chat has done on a task: both are real, neither is
 * a guess. A set, so a name borrowed from Object.prototype is not a relation.
 */
export const LINK_RELATIONS = new Set(["turn", "plan"]);

/**
 * Task -> chat links, only where both ends are on this map.
 *
 * The server lists, on each process, the tasks that chat worked on:
 * `{task_id, relation}`, with `relation` `turn` (the chat's last turn touched
 * the task) or `plan` (the task is in the chat's plan). A task that is not on
 * the map - a closed one, another project's, a stale id - draws nothing, and a
 * pair is drawn once: a `turn` is the stronger evidence, so it wins over a
 * `plan` for the same task and chat.
 */
export function linkSpecs(taskNodes = [], processNodes = []) {
  const tasks = new Map(taskNodes.map((node) => [node.id, node]));
  const specs = [];
  const byPair = new Map();
  for (const process of processNodes) {
    const chatId = text(process?.id);
    if (!chatId) continue;
    const listed = Array.isArray(process?.item?.task_links) ? process.item.task_links : [];
    for (const link of listed) {
      const taskId = text(link?.task_id);
      const relation = text(link?.relation);
      if (!taskId || !LINK_RELATIONS.has(relation) || !tasks.has(taskId)) continue;
      const pair = `${chatId}\u0000${taskId}`;
      const known = byPair.get(pair);
      if (known) {
        if (known.relation === "plan" && relation === "turn") known.relation = "turn";
        continue;
      }
      const spec = { kind: "link", key: `link\u0000${pair}`, from: taskId, to: chatId, relation };
      byPair.set(pair, spec);
      specs.push(spec);
    }
  }
  return specs;
}

/**
 * A spec's place in a deterministic order: by kind, then by its two ends and
 * its relation - never by the order the server happened to list things in.
 */
const specOrder = (a, b) =>
  text(a?.kind).localeCompare(text(b?.kind)) ||
  text(a?.from).localeCompare(text(b?.from)) ||
  text(a?.to).localeCompare(text(b?.to)) ||
  text(a?.relation).localeCompare(text(b?.relation)) ||
  text(a?.key).localeCompare(text(b?.key));

/**
 * The x every route leaves or enters a card at.
 *
 * The ports of one edge are spread over that edge alone, so two routes never
 * meet a card at one point. Which edge a route uses is what decides who it has
 * to share with: a task lets everything out through its bottom edge (a
 * dependency and a link alike), and takes a dependency from its own row in
 * through the same bottom edge - those must be told apart, or the two lines
 * join into one that looks like it goes on into the chat. A dependency from
 * another row comes down onto the top edge, and a chat takes its links in
 * through its top edge: separate edges, separate spreads.
 */
function assignPorts(taskNodes, processNodes, specs) {
  const tasks = new Map(taskNodes.map((node) => [node.id, node]));
  const chats = new Map(processNodes.map((node) => [node.id, node]));
  // node id -> edge -> the routes that meet that edge, and on which side.
  const edges = new Map();
  const add = (id, edge, spec, side) => {
    let byEdge = edges.get(id);
    if (!byEdge) {
      byEdge = new Map();
      edges.set(id, byEdge);
    }
    const list = byEdge.get(edge);
    if (list) list.push({ spec, side });
    else byEdge.set(edge, [{ spec, side }]);
  };
  for (const spec of specs) {
    const from = tasks.get(spec.from);
    if (!from) continue;
    // Every route out of a task leaves by its bottom edge.
    add(spec.from, "bottom", spec, "from");
    const dependency = tasks.get(spec.to);
    if (dependency) {
      add(spec.to, from.row === dependency.row ? "bottom" : "top", spec, "to");
    } else if (chats.has(spec.to)) {
      add(spec.to, "top", spec, "to");
    }
  }
  for (const [id, byEdge] of edges) {
    const node = tasks.get(id) ?? chats.get(id);
    if (!node) continue;
    for (const list of byEdge.values()) {
      const ordered = [...list].sort((a, b) => specOrder(a.spec, b.spec));
      portsAlong(node, ordered.length).forEach((x, index) => {
        const entry = ordered[index];
        entry.spec[entry.side === "from" ? "fromX" : "toX"] = x;
      });
    }
  }
}

/** A dependency: out of the dependency's bottom edge into the dependent's top. */
function routeDependency(spec, tasks, tracks) {
  const from = tasks.get(spec.from);
  const to = tasks.get(spec.to);
  const sameRow = from.row === to.row;
  const drop = taskGapY(tracks.tasks, from.row);
  const approach = sameRow ? drop : taskAboveY(tracks.tasks, to.row);
  const end = sameRow ? to.y + to.height : to.y;
  return orthogonalRoute([
    { x: spec.fromX, y: from.y + from.height },
    { x: spec.fromX, y: drop },
    { x: spec.channel, y: drop },
    { x: spec.channel, y: approach },
    { x: spec.toX, y: approach },
    { x: spec.toX, y: end },
  ]);
}

/**
 * A work link: out of the task's bottom edge, along the gap under its row, up
 * the corridor between the regions, and into the chat's top edge through the
 * free strip above the chat's row.
 */
function routeLink(spec, tasks, chats, tracks) {
  const task = tasks.get(spec.from);
  const chat = chats.get(spec.to);
  const lane = tracks.corridor[spec.relation] + spec.lane;
  const drop = taskGapY(tracks.tasks, task.row);
  const approach = processGapY(tracks.processes, chat.row);
  return orthogonalRoute([
    { x: spec.fromX, y: task.y + task.height },
    { x: spec.fromX, y: drop },
    { x: lane, y: drop },
    { x: lane, y: approach },
    { x: spec.toX, y: approach },
    { x: spec.toX, y: chat.y },
  ]);
}

/** Both kinds of relation, routed; dependencies first, so the ports stay put. */
function routes(taskNodes, processNodes, tracks) {
  const tasks = new Map(taskNodes.map((node) => [node.id, node]));
  const chats = new Map(processNodes.map((node) => [node.id, node]));
  const edges = dependencySpecs(taskNodes);
  const links = linkSpecs(taskNodes, processNodes);
  assignPorts(taskNodes, processNodes, [...edges, ...links]);

  // One strip may carry several routes: each takes the next lane of it.
  const channels = new Map();
  const orderedEdges = [...edges].sort(specOrder);
  for (const edge of orderedEdges) {
    const from = tasks.get(edge.from);
    const to = tasks.get(edge.to);
    const gap = channelGap(from, to, tracks.columns);
    const used = channels.get(gap) ?? 0;
    channels.set(gap, used + 1);
    edge.channel = taskGapX(tracks.tasks, gap) + LANES[used % LANES.length];
  }
  const corridor = corridorX(tracks.tasks, tracks.processes);
  const lanes = new Map();
  const orderedLinks = [...links].sort(specOrder);
  for (const link of orderedLinks) {
    const used = lanes.get(link.relation) ?? 0;
    lanes.set(link.relation, used + 1);
    link.lane = LINK_LANES[used % LINK_LANES.length];
  }
  const guide = { ...tracks, corridor };
  return {
    edges: orderedEdges.map((edge) => ({
      from: edge.from,
      to: edge.to,
      relation: edge.relation,
      ...routeDependency(edge, tasks, guide),
    })),
    links: orderedLinks.map((link) => ({
      from: link.from,
      to: link.to,
      relation: link.relation,
      ...routeLink(link, tasks, chats, guide),
    })),
  };
}
