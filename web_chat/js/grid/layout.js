/**
 * The geography of the project map (web_chat/grid.html).
 *
 * Three regions never move: goals/tasks on the left, processes on the right,
 * results under them. Inside a region a node's place is a pure function of the
 * set of items - not of their state, their priority or the order the server
 * happened to answer in:
 *
 * - tasks are ordered by `created_at`, then by id, so the same set always
 *   yields the same coordinates;
 * - a status change therefore never moves a task, and a new task (which is the
 *   newest by `created_at`) is appended after the ones already there.
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
  processes: { width: 184, height: 56 },
  results: { width: 184, height: 52 },
};

/** The three fixed anchors. The results region follows the processes one down. */
export const REGIONS = {
  tasks: { x: MARGIN, y: MARGIN, width: 740 },
  processes: { x: 796, y: MARGIN, width: 416 },
  results: { x: 796, width: 416 },
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

/** Results with the directories first, then by name. */
export function orderResults(items = []) {
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
 * @returns {{width:number,height:number,regions:object,nodes:object,edges:Array,columns:object}}
 */
export function layoutMap(map = {}) {
  const tasks = orderTasks(Array.isArray(map?.tasks?.items) ? map.tasks.items : []);
  const processes = orderProcesses(Array.isArray(map?.processes?.items) ? map.processes.items : []);
  const results = orderResults(Array.isArray(map?.results?.items) ? map.results.items : []);

  const taskColumns = columnsFor(REGIONS.tasks.width, NODE_SIZE.tasks);
  const processColumns = columnsFor(REGIONS.processes.width, NODE_SIZE.processes);
  const resultColumns = columnsFor(REGIONS.results.width, NODE_SIZE.results);

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
  const resultsRegion = {
    region: "results",
    label: "Results",
    x: REGIONS.results.x,
    y: processesRegion.y + processesRegion.height + MARGIN,
    width: REGIONS.results.width,
    height: regionHeight(results.length, resultColumns, NODE_SIZE.results),
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
  const resultCells = place(results.length, resultColumns, NODE_SIZE.results, resultsRegion);
  const resultNodes = results.map((entry, index) => ({
    ...resultCells[index],
    id: text(entry?.path) || text(entry?.name),
    title: text(entry?.name),
    item: entry,
  }));

  return {
    width: CANVAS.width,
    height: Math.max(CANVAS.minHeight, tasksRegion.height, resultsRegion.y + resultsRegion.height) + MARGIN,
    regions: { tasks: tasksRegion, processes: processesRegion, results: resultsRegion },
    nodes: { tasks: taskNodes, processes: processNodes, results: resultNodes },
    edges: taskEdges(taskNodes),
    columns: { tasks: taskColumns, processes: processColumns, results: resultColumns },
  };
}

/** Edges only where a dependency is real: both tasks are on this map. */
export function taskEdges(taskNodes = []) {
  const centre = (node) => ({ x: node.x + node.width / 2, y: node.y + node.height / 2 });
  const byId = new Map(taskNodes.map((node) => [node.id, node]));
  const edges = [];
  for (const node of taskNodes) {
    const seen = new Set();
    for (const dependency of node.item?.depends_on ?? []) {
      const id = text(dependency);
      if (!id || id === node.id || seen.has(id)) continue;
      const from = byId.get(id);
      if (!from) continue;
      seen.add(id);
      edges.push({ from: id, to: node.id, ...pairOf(centre(from), centre(node)) });
    }
  }
  return edges;
}

const pairOf = (from, to) => ({ x1: from.x, y1: from.y, x2: to.x, y2: to.y });
