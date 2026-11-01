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

  return {
    width: CANVAS.width,
    height: Math.max(CANVAS.minHeight, tasksRegion.height, filesRegion.y + filesRegion.height) + MARGIN,
    regions: { tasks: tasksRegion, processes: processesRegion, files: filesRegion },
    nodes: { tasks: taskNodes, processes: processNodes, files: fileNodes },
    edges: taskEdges(taskNodes),
    // Task -> chat, kept apart from the dependencies: a different question,
    // drawn as a different line.
    links: taskChatLinks(taskNodes, processNodes),
    columns: { tasks: taskColumns, processes: processColumns, files: fileColumns },
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
export function taskChatLinks(taskNodes = [], processNodes = []) {
  const tasks = new Map(taskNodes.map((node) => [node.id, node]));
  const centre = (node) => ({ x: node.x + node.width / 2, y: node.y + node.height / 2 });
  const links = [];
  const byPair = new Map();
  for (const process of processNodes) {
    const chatId = text(process?.id);
    if (!chatId) continue;
    const listed = Array.isArray(process?.item?.task_links) ? process.item.task_links : [];
    for (const link of listed) {
      const taskId = text(link?.task_id);
      const relation = text(link?.relation);
      if (!taskId || !LINK_RELATIONS.has(relation)) continue;
      const task = tasks.get(taskId);
      if (!task) continue;
      const key = `${chatId}\u0000${taskId}`;
      const known = byPair.get(key);
      if (known) {
        if (known.relation === "plan" && relation === "turn") known.relation = "turn";
        continue;
      }
      const drawn = { from: taskId, to: chatId, relation, ...pairOf(centre(task), centre(process)) };
      byPair.set(key, drawn);
      links.push(drawn);
    }
  }
  return links;
}
