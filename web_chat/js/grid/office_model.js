/**
 * A deterministic, renderer-independent cutaway office for one SceneModel.
 *
 * The SceneModel remains canonical. This module only gives its real nodes a
 * recognisable office geography: tickets on a back wall, chat workstations,
 * file shelving and a counter of closure reports. A closed task with a real
 * close_reason is a report *about closure*, never proof of acceptance; a file
 * is never promoted to a result. Detail furniture belongs only to the selected
 * chat and contains only messages/executions/operations already in the scene.
 *
 * No DOM, WebGL, clock, random numbers or network live here. All geometry is
 * axis-aligned so the WebGL renderer can draw every piece in one instanced
 * draw call and pick logical objects with the existing ray/AABB maths.
 */

export const OFFICE_MODEL_VERSION = 1;
export const MAX_OFFICE_LABELS = 24;
export const OFFICE_DPR_CAP = 1.75;

export const OFFICE_COLORS = Object.freeze({
  background: [0.035, 0.047, 0.064],
  floor: [0.18, 0.21, 0.245],
  floorLine: [0.30, 0.38, 0.43],
  wall: [0.20, 0.245, 0.285],
  wallEdge: [0.32, 0.40, 0.45],
  board: [0.10, 0.145, 0.17],
  desk: [0.47, 0.35, 0.23],
  deskEdge: [0.60, 0.46, 0.30],
  metal: [0.35, 0.42, 0.47],
  screen: [0.025, 0.055, 0.070],
  paper: [0.67, 0.72, 0.72],
  paperDark: [0.40, 0.47, 0.50],
  folder: [0.43, 0.48, 0.35],
  person: [0.35, 0.55, 0.72],
  skin: [0.80, 0.63, 0.47],
  chair: [0.24, 0.30, 0.35],
  neutral: [0.40, 0.48, 0.54],
  active: [0.12, 0.78, 0.88],
  waiting: [0.95, 0.61, 0.18],
  blocked: [0.95, 0.38, 0.19],
  failed: [0.95, 0.25, 0.31],
  done: [0.32, 0.72, 0.46],
  cancelled: [0.38, 0.41, 0.44],
  task: [0.62, 0.71, 0.77],
  file: [0.35, 0.55, 0.61],
  binary: [0.50, 0.43, 0.62],
  unknown: [0.43, 0.46, 0.49],
  report: [0.52, 0.57, 0.38],
  tool: [0.28, 0.57, 0.68],
  message: [0.49, 0.62, 0.68],
});

const text = (value) => String(value ?? "");
const arrayOf = (value) => (Array.isArray(value) ? value : []);
const finite = (value, fallback = 0) => Number.isFinite(value) ? value : fallback;
const clamp = (value, min, max) => Math.max(min, Math.min(max, finite(value, min)));
const known = (value) => text(value).trim() || null;
const cmp = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const vec = (value, fallback = [0, 0, 0]) => [
  finite(value?.[0], fallback[0]),
  finite(value?.[1], fallback[1]),
  finite(value?.[2], fallback[2]),
];

/** The public state language: icon + exact words, with colour only a duplicate. */
export function officeState(node) {
  const kind = text(node?.kind);
  const raw = text(node?.state ?? node?.dimensions?.status).toLowerCase();
  if (kind === "chat") {
    if (raw === "awaiting_approval") return { icon: "◌", label: "Waiting for approval", tone: "waiting", color: OFFICE_COLORS.waiting };
    if (raw === "in_progress" || raw === "running") return { icon: "●", label: "In progress", tone: "active", color: OFFICE_COLORS.active };
    if (raw === "idle") return { icon: "○", label: "No active turn", tone: "idle", color: OFFICE_COLORS.neutral };
  }
  if (kind === "file") {
    if (raw === "dir") return { icon: "▰", label: "Folder on disk", tone: "file", color: OFFICE_COLORS.file };
    if (raw === "link") return { icon: "↗", label: "Symbolic link", tone: "file", color: OFFICE_COLORS.active };
    return { icon: "▤", label: "File on disk · result unconfirmed", tone: "file", color: OFFICE_COLORS.file };
  }
  if (["blocked"].includes(raw)) return { icon: "!", label: "Blocked", tone: "blocked", color: OFFICE_COLORS.blocked };
  if (["error", "failed", "failure"].includes(raw)) return { icon: "△", label: "Failed", tone: "failed", color: OFFICE_COLORS.failed };
  if (raw === "timeout") return { icon: "△", label: "Timed out", tone: "failed", color: OFFICE_COLORS.failed };
  if (["awaiting_approval", "waiting", "pending", "review"].includes(raw)) {
    const label = raw === "awaiting_approval" ? "Waiting for approval" : raw === "review" ? "In review" : "Waiting";
    return { icon: "◌", label, tone: "waiting", color: OFFICE_COLORS.waiting };
  }
  if (["in_progress", "running"].includes(raw)) return { icon: "●", label: raw === "running" ? "Running" : "In progress", tone: "active", color: OFFICE_COLORS.active };
  if (["closed", "done", "completed", "accepted"].includes(raw)) {
    return { icon: "✓", label: raw === "closed" ? "Closed" : raw === "accepted" ? "Accepted" : "Done", tone: "done", color: OFFICE_COLORS.done };
  }
  if (["cancelled", "canceled", "stopped", "skipped"].includes(raw)) {
    return { icon: "—", label: raw === "skipped" ? "Skipped" : raw === "stopped" ? "Stopped" : "Cancelled", tone: "cancelled", color: OFFICE_COLORS.cancelled };
  }
  if (["ready", "open"].includes(raw)) return { icon: "○", label: raw === "ready" ? "Ready" : "Open", tone: "neutral", color: OFFICE_COLORS.neutral };
  if (raw === "planned") return { icon: "○", label: "Planned · not started", tone: "neutral", color: OFFICE_COLORS.neutral };
  if (kind === "project" || kind === "collection") return { icon: "⌂", label: kind === "project" ? "Project" : "Office zone", tone: "neutral", color: OFFICE_COLORS.neutral };
  if (kind === "message") return { icon: "▤", label: `${raw || "other"} message`, tone: "neutral", color: OFFICE_COLORS.message };
  if (known(raw)) return { icon: "?", label: text(node.state).replaceAll("_", " "), tone: "unknown", color: OFFICE_COLORS.unknown };
  return { icon: "?", label: "State unknown", tone: "unknown", color: OFFICE_COLORS.unknown };
}

/** Tool categories come from the recorded operation kind, never its guessed effect. */
export function officeToolKind(operation) {
  const kind = known(operation?.item?.kind ?? operation?.state) ?? "step";
  if (kind === "agent") return { kind, icon: "↗", label: "Delegation", color: OFFICE_COLORS.waiting };
  if (kind === "message") return { kind, icon: "▤", label: "Message", color: OFFICE_COLORS.message };
  if (kind === "tool") return { kind, icon: "▣", label: "Tool call", color: OFFICE_COLORS.tool };
  if (["read", "search"].includes(kind)) return { kind, icon: "⌕", label: kind === "read" ? "Read call" : "Search call", color: OFFICE_COLORS.active };
  if (["write", "edit"].includes(kind)) return { kind, icon: "✎", label: "Write call · effect not implied", color: OFFICE_COLORS.waiting };
  if (["terminal", "shell"].includes(kind)) return { kind, icon: ">_", label: "Terminal call", color: OFFICE_COLORS.tool };
  if (["web", "network"].includes(kind)) return { kind, icon: "◎", label: "Network call", color: OFFICE_COLORS.binary };
  if (["file", "filesystem"].includes(kind)) return { kind, icon: "▰", label: "File call", color: OFFICE_COLORS.file };
  if (["report", "document"].includes(kind)) return { kind, icon: "▤", label: "Report call", color: OFFICE_COLORS.report };
  return { kind, icon: "▣", label: `${kind.replaceAll("_", " ")} operation`, color: OFFICE_COLORS.tool };
}

const stableNodes = (nodes) => [...nodes].sort((a, b) =>
  a.position[0] - b.position[0] || cmp(a.id, b.id));

function chatIdOf(node) {
  if (!node) return "";
  if (node.kind === "chat") return text(node.item?.chat_id ?? node.dimensions?.provenance?.id);
  return text(node.dimensions?.provenance?.chat);
}

function collectionName(node) {
  return text(node?.item?.name ?? node?.dimensions?.provenance?.slice).replace(/^collection:/, "");
}

function statusPartColor(node) {
  return officeState(node).color;
}

/** A small bounded snapshot note; no invisible truncation is allowed. */
function sliceNote(section, singular) {
  const shown = arrayOf(section?.items).length;
  const total = Number.isFinite(section?.total) ? section.total : shown;
  if (section?.available === false) return `${singular} unavailable in this snapshot`;
  if (!shown) return `No ${singular.toLowerCase()} in this snapshot`;
  if (section?.truncated === true || total > shown) return `Showing ${shown} of ${total} ${singular.toLowerCase()}`;
  return `${shown} ${singular.toLowerCase()}`;
}

/**
 * Build the complete office. `options.map` is the same snapshot the SceneModel
 * came from and is used only for availability/truncation words. `detailStates`
 * and `detailChatIds` distinguish an unloaded desk from a loaded empty one.
 */
export function createOfficeModel(scene, options = {}) {
  const source = arrayOf(scene?.nodes).filter((node) =>
    node && typeof node.id === "string" && Array.isArray(node.position) && node.position.every(Number.isFinite));
  const sourceById = new Map(source.map((node) => [node.id, node]));
  const selectedId = known(options.selectedId);
  const selectedSource = selectedId ? sourceById.get(selectedId) : null;
  const activeChatId = chatIdOf(selectedSource);
  const detailChatIds = new Set(arrayOf(options.detailChatIds).map(text));
  const detailStates = options.detailStates && typeof options.detailStates === "object" ? options.detailStates : {};
  const detailStatus = activeChatId
    ? text(detailStates?.[activeChatId]?.status) || (detailChatIds.has(activeChatId) ? "ready" : "idle")
    : "idle";

  const nodes = [];
  const pickTargets = [];
  const parts = [];
  const routes = [];
  const zones = [];
  const reports = [];
  const byId = new Map();
  const map = options.map && typeof options.map === "object" ? options.map : {};

  const addPart = (role, position, size, color, nodeId = null) => {
    const part = {
      role,
      position: vec(position),
      size: vec(size, [1, 1, 1]).map((value) => Math.max(0.025, Math.abs(value))),
      color: [...(color ?? OFFICE_COLORS.neutral)],
      nodeId: known(nodeId),
    };
    parts.push(part);
    return part;
  };
  const addTarget = (node, position = node.position, size = node.size, suffix = "") => {
    const target = { ...node, position: vec(position), size: vec(size, [1, 1, 1]), pickKey: `${node.id}${suffix}` };
    pickTargets.push(target);
    return target;
  };
  const addObject = (sourceNode, position, size, zone, extra = {}, { pick = true } = {}) => {
    const status = officeState(sourceNode);
    const node = {
      ...sourceNode,
      position: vec(position),
      size: vec(size, [1, 1, 1]).map((value) => Math.max(0.05, Math.abs(value))),
      zone,
      status,
      labelPosition: vec(extra.labelPosition ?? [position[0], position[1] + size[1] / 2 + 0.5, position[2]]),
      caption: text(extra.caption),
      agent: known(extra.agent),
      sessionAgents: arrayOf(extra.sessionAgents).map(text).filter(Boolean),
    };
    nodes.push(node);
    byId.set(node.id, node);
    if (pick) addTarget(node);
    return node;
  };
  const addZone = (id, title, note, position, collectionId = null) => {
    zones.push({ id, title, note, position: vec(position), collectionId, size: [1, 1, 1] });
  };

  const tasks = stableNodes(source.filter((node) => node.kind === "task"));
  const chats = stableNodes(source.filter((node) => node.kind === "chat"));
  const files = stableNodes(source.filter((node) => node.kind === "file"));
  const projects = source.filter((node) => node.kind === "project");
  const collections = source.filter((node) => node.kind === "collection");
  const detailNodes = activeChatId
    ? source.filter((node) => ["message", "execution", "operation"].includes(node.kind) && chatIdOf(node) === activeChatId)
    : [];

  // Fixed architectural coordinates. The back wall and both side zones never
  // move when more desks are added; only the open front of the floor extends.
  const floorWidth = 60;
  const backZ = -21;
  const chatRows = Math.max(1, Math.ceil(chats.length / 4));
  const frontZ = Math.max(24, -10 + (chatRows - 1) * 11 + 12);
  const taskRows = Math.max(1, Math.ceil(tasks.length / 12));
  const fileRows = Math.max(1, Math.ceil(files.length / 8));
  const backHeight = Math.max(10, 4.2 + taskRows * 1.55);
  const fileHeight = Math.max(9, 4 + fileRows * 1.55);

  addPart("floor", [0, -0.35, (backZ + frontZ) / 2], [floorWidth, 0.7, frontZ - backZ + 2], OFFICE_COLORS.floor);
  addPart("back-wall", [0, backHeight / 2, backZ - 0.45], [floorWidth, backHeight, 0.7], OFFICE_COLORS.wall);
  addPart("task-board", [0, backHeight / 2, backZ + 0.02], [55.5, Math.max(7, backHeight - 1), 0.25], OFFICE_COLORS.board);
  addPart("right-wall", [29.5, fileHeight / 2, -1], [0.7, fileHeight, 40], OFFICE_COLORS.wall);
  addPart("left-cutaway", [-29.5, 0.55, (backZ + frontZ) / 2], [0.5, 1.1, frontZ - backZ], OFFICE_COLORS.wallEdge);
  addPart("front-cutaway", [0, 0.18, frontZ], [floorWidth, 0.36, 0.35], OFFICE_COLORS.wallEdge);

  // Floor seams make the cutaway read as a room, without a perpetual GL loop.
  for (let x = -24; x <= 24; x += 6) addPart("floor-seam", [x, 0.015, (backZ + frontZ) / 2], [0.035, 0.025, frontZ - backZ - 1], OFFICE_COLORS.floorLine);
  for (let z = -15; z < frontZ; z += 6) addPart("floor-seam", [0, 0.018, z], [58, 0.025, 0.035], OFFICE_COLORS.floorLine);

  // -- Task wall: real tickets, priorities, statuses and dependencies. -------
  tasks.forEach((task, index) => {
    const column = index % 12;
    const row = Math.floor(index / 12);
    const x = (column - 5.5) * 4.4;
    const y = 2.8 + row * 1.55;
    const z = backZ + 0.45;
    const node = addObject(task, [x, y, z], [3.85, 1.25, 0.28], "tasks", {
      caption: Number.isFinite(task.item?.priority) ? `P${task.item.priority}` : "Priority not given",
      labelPosition: [x, y + 0.85, z + 0.25],
    });
    addPart("task-ticket", node.position, node.size, OFFICE_COLORS.task, node.id);
    addPart("task-state", [x - 1.72, y, z + 0.19], [0.14, 0.96, 0.09], statusPartColor(task), node.id);
    if (Number.isFinite(task.item?.priority)) {
      addPart("task-priority", [x + 1.46, y + 0.36, z + 0.2], [0.46, 0.28, 0.08], OFFICE_COLORS.paperDark, node.id);
    }
    if (known(task.item?.close_reason)) {
      addPart("task-closure-tab", [x + 1.35, y - 0.4, z + 0.21], [0.75, 0.18, 0.08], OFFICE_COLORS.done, node.id);
    }
  });
  addZone("zone:tasks", `TASKS · ${tasks.length}`, sliceNote(map.tasks, "Tasks"), [-27.5, Math.min(backHeight - 1, 7), backZ + 0.6], collections.find((node) => collectionName(node) === "work")?.id ?? null);

  // -- Chat workstations: furniture and a seated figure per real chat. -------
  chats.forEach((chat, index) => {
    const column = index % 4;
    const row = Math.floor(index / 4);
    const x = (column - 1.5) * 12;
    const z = -10 + row * 11;
    const chatId = chatIdOf(chat);
    const ownDetail = detailNodes.filter((node) => node.kind === "execution" && chatIdOf(node) === chatId);
    const sessionAgents = [...new Set(ownDetail.map((node) => known(node.dimensions?.agent)).filter(Boolean))];
    const runningAgents = [...new Set(ownDetail.filter((node) => ["running", "in_progress"].includes(text(node.state)) && node.dimensions?.agent).map((node) => text(node.dimensions.agent)))];
    const agent = runningAgents.length === 1 ? runningAgents[0] : null;
    const lastStep = arrayOf(chat.item?.steps).map(text).filter((value) => value.trim()).at(-1) ?? "";
    const node = addObject(chat, [x, 1.25, z], [9, 2.5, 5.4], "chats", {
      agent,
      sessionAgents,
      caption: lastStep,
      labelPosition: [x, 5.1, z + 2.2],
    });
    addPart("desk-top", [x, 2.05, z], [8.4, 0.35, 4.6], OFFICE_COLORS.desk, node.id);
    addPart("desk-front", [x, 1.72, z + 2.15], [8.5, 0.42, 0.25], OFFICE_COLORS.deskEdge, node.id);
    for (const legX of [-3.55, 3.55]) for (const legZ of [-1.75, 1.75]) {
      addPart("desk-leg", [x + legX, 1, z + legZ], [0.3, 2, 0.3], OFFICE_COLORS.metal, node.id);
    }
    addPart("monitor", [x, 3.25, z - 1.25], [3.3, 1.8, 0.25], OFFICE_COLORS.screen, node.id);
    addPart("monitor-frame", [x, 4.18, z - 1.25], [3.5, 0.12, 0.35], OFFICE_COLORS.metal, node.id);
    addPart("monitor-stand", [x, 2.48, z - 1.1], [0.28, 0.75, 0.28], OFFICE_COLORS.metal, node.id);
    addPart("monitor-status", [x, 3.92, z - 1.08], [2.75, 0.16, 0.08], statusPartColor(chat), node.id);
    addPart("keyboard", [x, 2.29, z + 0.72], [2.6, 0.12, 0.9], OFFICE_COLORS.metal, node.id);
    addPart("chair-seat", [x, 1.2, z + 3.15], [2.1, 0.32, 1.9], OFFICE_COLORS.chair, node.id);
    addPart("chair-back", [x, 2, z + 3.85], [2.1, 1.8, 0.3], OFFICE_COLORS.chair, node.id);
    addPart("worker-torso", [x, 2.35, z + 2.85], [1.35, 1.65, 0.85], OFFICE_COLORS.person, node.id);
    addPart("worker-head", [x, 3.65, z + 2.72], [0.95, 0.95, 0.9], OFFICE_COLORS.skin, node.id);
    addPart("worker-arm-left", [x - 0.85, 2.45, z + 1.95], [0.35, 0.38, 1.65], OFFICE_COLORS.person, node.id);
    addPart("worker-arm-right", [x + 0.85, 2.45, z + 1.95], [0.35, 0.38, 1.65], OFFICE_COLORS.person, node.id);
    addPart("worker-leg-left", [x - 0.38, 0.78, z + 2.15], [0.42, 1.25, 0.55], OFFICE_COLORS.chair, node.id);
    addPart("worker-leg-right", [x + 0.38, 0.78, z + 2.15], [0.42, 1.25, 0.55], OFFICE_COLORS.chair, node.id);
    addTarget(node, [x, 2.4, z + 2.85], [2.2, 4.4, 2.2], ":worker");

    if (chat.state === "awaiting_approval") {
      // An amber physical barrier is waiting, never an animated working avatar.
      addPart("approval-post", [x - 3.5, 1.15, z + 4.8], [0.28, 2.3, 0.28], OFFICE_COLORS.waiting, node.id);
      addPart("approval-post", [x + 3.5, 1.15, z + 4.8], [0.28, 2.3, 0.28], OFFICE_COLORS.waiting, node.id);
      addPart("approval-barrier", [x, 1.75, z + 4.8], [7.2, 0.38, 0.32], OFFICE_COLORS.waiting, node.id);
    } else if (chat.state === "in_progress") {
      addPart("active-screen-bar", [x - 0.8, 3.25, z - 1.08], [0.25, 0.85, 0.08], OFFICE_COLORS.active, node.id);
      addPart("active-screen-bar", [x, 3.25, z - 1.08], [0.25, 0.85, 0.08], OFFICE_COLORS.active, node.id);
      addPart("active-screen-bar", [x + 0.8, 3.25, z - 1.08], [0.25, 0.85, 0.08], OFFICE_COLORS.active, node.id);
    }
  });
  // The chat caption floats above the front-left floor, clear of the closure
  // counter's own caption: two zone words at the same height would hide one.
  addZone("zone:chats", `CHATS · ${chats.length}`, sliceNote(map.chats ?? map.processes, "Chats"), [-23, 9, 18], collections.find((node) => collectionName(node) === "chats")?.id ?? null);

  // -- File shelving: every visible top-level entry, with its real modality. --
  files.forEach((file, index) => {
    const column = index % 8;
    const row = Math.floor(index / 8);
    const x = 26.4;
    const y = 2.5 + row * 1.55;
    const z = -15 + column * 4;
    const isDir = file.item?.kind === "dir";
    const modality = text(file.dimensions?.modality);
    const color = modality === "binary" ? OFFICE_COLORS.binary : modality === "unknown" ? OFFICE_COLORS.unknown : OFFICE_COLORS.file;
    const node = addObject(file, [x, y, z], [isDir ? 1.25 : 0.7, 1.2, 3.15], "files", {
      caption: `${modality || "unknown"} · existence is not acceptance`,
      labelPosition: [x - 0.8, y + 0.85, z],
    });
    addPart(isDir ? "file-folder" : "file-sheet", node.position, node.size, color, node.id);
    addPart("file-label", [x - (isDir ? 0.68 : 0.4), y + 0.18, z], [0.08, 0.34, 2.25], OFFICE_COLORS.paper, node.id);
    if (file.item?.kind === "link") {
      addPart("file-link", [x - 0.48, y - 0.34, z], [0.1, 0.18, 1.3], OFFICE_COLORS.active, node.id);
    }
  });
  for (let row = 0; row < fileRows; row += 1) {
    addPart("file-shelf", [27.1, 1.72 + row * 1.55, -1], [2.6, 0.18, 32], OFFICE_COLORS.metal);
  }
  for (let column = 0; column <= 8; column += 1) {
    addPart("file-shelf-post", [27.6, fileHeight / 2, -17 + column * 4], [0.25, fileHeight, 0.22], OFFICE_COLORS.metal);
  }
  addZone("zone:files", `FILES · ${files.length}`, `${sliceNote(map.files, "Files")} · not results`, [26.2, Math.min(fileHeight - 0.8, 7.5), 16], collections.find((node) => collectionName(node) === "files")?.id ?? null);

  // -- Closure counter: only an explicit close_reason makes a report. --------
  const reportTasks = tasks.filter((task) => task.state === "closed" && known(task.item?.close_reason));
  reportTasks.forEach((task, index) => {
    const column = index % 6;
    const row = Math.floor(index / 6);
    const position = [-26, 1.5 + row * 0.64, -15 + column * 4.6];
    const size = [1.55, 0.48, 3.65];
    const report = {
      ...task,
      position,
      size,
      zone: "results",
      status: { icon: "✓", label: "Closure reason · not acceptance evidence", tone: "done", color: OFFICE_COLORS.done },
      labelPosition: [position[0] + 0.4, position[1] + 0.55, position[2]],
      caption: text(task.item.close_reason),
      pickKey: `report:${task.id}`,
    };
    reports.push(report);
    pickTargets.push(report);
    addPart("report-folder", position, size, OFFICE_COLORS.report, task.id);
    addPart("report-clip", [position[0] + 0.82, position[1] + 0.18, position[2] - 1.1], [0.12, 0.18, 0.75], OFFICE_COLORS.paperDark, task.id);
  });
  const reportRows = Math.max(1, Math.ceil(reportTasks.length / 6));
  for (let row = 0; row < reportRows; row += 1) {
    addPart("report-counter", [-26.6, 1.12 + row * 0.64, -3.5], [2.3, 0.18, 28], OFFICE_COLORS.deskEdge);
  }
  addZone(
    "zone:results",
    `CLOSURE REPORTS · ${reportTasks.length}`,
    reportTasks.length ? "Real close reasons · no acceptance claim" : "No closure reports · files are not verified results",
    [-25.5, 3.2, 16],
  );

  // Project and collection plaques are selectable canonical nodes too.
  for (const project of projects) {
    const node = addObject(project, [0, 1.25, 20], [10, 2.5, 0.6], "project", { labelPosition: [0, 3, 20] });
    addPart("project-plaque", node.position, node.size, OFFICE_COLORS.metal, node.id);
    addPart("project-plaque-line", [0, 1.85, 20.34], [8.8, 0.15, 0.08], OFFICE_COLORS.active, node.id);
  }
  for (const collection of collections) {
    const name = collectionName(collection);
    const zone = zones.find((entry) => entry.collectionId === collection.id);
    if (!zone) continue;
    const position = [zone.position[0], Math.max(0.8, zone.position[1] - 1), zone.position[2] - 0.3];
    const node = addObject(collection, position, [2.4, 1.2, 0.45], name, { labelPosition: zone.position });
    addPart("zone-plaque", node.position, node.size, OFFICE_COLORS.metal, node.id);
  }

  // -- Selected desk only: messages, work sessions and the real tool rail. ---
  const activeDesk = nodes.find((node) => node.kind === "chat" && chatIdOf(node) === activeChatId);
  if (activeDesk && detailStatus === "ready") {
    const dx = activeDesk.position[0];
    const dz = activeDesk.position[2];
    const messages = stableNodes(detailNodes.filter((node) => node.kind === "message"));
    const executions = stableNodes(detailNodes.filter((node) => node.kind === "execution"));
    const operations = stableNodes(detailNodes.filter((node) => node.kind === "operation"));

    messages.forEach((message, index) => {
      const column = index % 5;
      const row = Math.floor(index / 5);
      const position = [dx - 2.5 + column * 1.12, 2.31 + row * 0.075, dz + 0.05 + row * 0.18];
      const node = addObject(message, position, [0.98, 0.1, 0.78], "selected-desk", {
        labelPosition: [position[0], position[1] + 0.55, position[2]],
        caption: text(message.item?.preview),
      });
      addPart("message-paper", position, node.size, message.item?.role === "user" ? OFFICE_COLORS.paperDark : OFFICE_COLORS.message, node.id);
    });

    executions.forEach((execution, index) => {
      const column = index % 10;
      const row = Math.floor(index / 10);
      const position = [dx - 3.45 + column * 0.76, 1.72 + row * 0.43, dz + 2.43];
      const node = addObject(execution, position, [0.65, 0.34, 0.22], "selected-desk", {
        agent: known(execution.dimensions?.agent),
        labelPosition: [position[0], position[1] + 0.65, position[2]],
        caption: execution.dimensions?.agent ? `Recorded session · ${execution.dimensions.agent}` : "Recorded session · agent not specified",
      });
      addPart("execution-badge", position, node.size, statusPartColor(execution), node.id);
    });

    operations.forEach((operation, index) => {
      const column = index % 8;
      const row = Math.floor(index / 8);
      const position = [dx + (column - 3.5) * 0.94, 4.65 + row * 0.49, dz - 1.22];
      const tool = officeToolKind(operation);
      const node = addObject(operation, position, [0.82, 0.37, 0.25], "selected-desk", {
        labelPosition: [position[0], position[1] + 0.55, position[2]],
        caption: `${tool.icon} ${tool.label} · ${operation.title}`,
      });
      addPart("tool-module", position, node.size, statusPartColor(operation), node.id);
      addPart("tool-kind-tab", [position[0] - 0.31, position[1], position[2] + 0.16], [0.14, 0.25, 0.07], tool.color, node.id);
    });
    if (operations.length) {
      const rows = Math.ceil(operations.length / 8);
      addPart("tool-rail", [dx, 4.47 + (rows - 1) * 0.245, dz - 1.43], [8.1, 0.75 + rows * 0.49, 0.18], OFFICE_COLORS.screen, activeDesk.id);
    }
  }

  // Routes use only canonical edges and endpoints actually present in office.
  // Containment is communicated by furniture; it does not become visual noise.
  for (const edge of arrayOf(scene?.edges)) {
    if (!edge || edge.kind === "contains" || edge.kind === "operation") continue;
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) continue;
    let points = [from.position, to.position];
    let relation = text(edge.kind);
    if (edge.kind === "depends") {
      points = [[from.position[0], from.position[1], backZ + 0.68], [to.position[0], to.position[1], backZ + 0.68]];
      relation = `${from.title} waits for ${to.title}`;
    } else if (edge.kind === "turn" || edge.kind === "plan") {
      points = [
        [from.position[0], 0.12, from.position[2]],
        [from.position[0], 0.12, -17.8],
        [to.position[0], 0.12, -17.8],
        [to.position[0], to.position[1], backZ + 0.7],
      ];
      relation = edge.kind === "turn" ? `${from.title} touched ${to.title}` : `${from.title} plans ${to.title}`;
    } else if (edge.kind === "reference") {
      points = [from.position, [24.2, 0.16, from.position[2]], [24.2, 0.16, to.position[2]], to.position];
      relation = `${from.title} mentions ${to.title} · no read/write effect implied`;
    } else if (edge.kind === "delegates") {
      relation = `${from.title} delegates to ${to.title}`;
    } else if (edge.kind === "message-execution") {
      relation = `${from.title} and ${to.title} share an explicit turn`;
    } else if (edge.kind === "contains-operation") {
      relation = `${to.title} belongs to ${from.title}`;
    }
    routes.push({ id: edge.id, from: edge.from, to: edge.to, kind: edge.kind, points: points.map((point) => vec(point)), relation });
  }

  const bounded = [];
  for (const [name, section] of [["tasks", map.tasks], ["chats", map.chats ?? map.processes], ["files", map.files]]) {
    const shown = arrayOf(section?.items).length;
    const total = Number.isFinite(section?.total) ? section.total : shown;
    if (section?.truncated === true || total > shown) bounded.push(`${name} ${shown}/${total}`);
  }
  const activeOperations = nodes.filter((node) => node.kind === "operation").length;
  const activeExecutions = nodes.filter((node) => node.kind === "execution").length;
  const activeMessages = nodes.filter((node) => node.kind === "message").length;
  let detailHint = "Select a desk to read its real sessions and tools. Other desks stay honestly undetailed.";
  if (activeChatId && detailStatus === "loading") detailHint = "Loading this desk’s bounded detail… no sessions or tools are inferred meanwhile.";
  else if (activeChatId && detailStatus === "error") detailHint = "This desk’s detail could not be read. The base snapshot remains; no empty tool rail is claimed.";
  else if (activeChatId && detailStatus === "ready" && !activeOperations) detailHint = "Desk detail loaded · no recorded tool calls in this bounded snapshot.";
  else if (activeChatId && detailStatus === "ready") detailHint = `Selected desk · ${activeExecutions} sessions · ${activeOperations} recorded operations. A call does not prove a file effect.`;
  else if (activeChatId) detailHint = "Desk selected · its detail has not been loaded yet. No sessions or tools are inferred.";

  // Bounds include architecture, so an empty project still has a real floor to
  // frame and honest empty zone labels instead of a blank canvas.
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const part of parts) {
    for (let axis = 0; axis < 3; axis += 1) {
      min[axis] = Math.min(min[axis], part.position[axis] - part.size[axis] / 2);
      max[axis] = Math.max(max[axis], part.position[axis] + part.size[axis] / 2);
    }
  }
  for (let axis = 0; axis < 3; axis += 1) {
    if (!Number.isFinite(min[axis])) min[axis] = -1;
    if (!Number.isFinite(max[axis])) max[axis] = 1;
  }

  return {
    version: OFFICE_MODEL_VERSION,
    projectId: scene?.projectId ?? null,
    selectedId: selectedId ?? null,
    activeChatId: activeChatId || null,
    nodes,
    pickTargets,
    parts,
    routes,
    zones,
    reports,
    bounds: { min, max },
    counts: {
      tasks: tasks.length,
      chats: chats.length,
      files: files.length,
      reports: reportTasks.length,
      messages: activeMessages,
      executions: activeExecutions,
      operations: activeOperations,
      parts: parts.length,
    },
    summary: {
      bounded,
      snapshot: bounded.length ? `Bounded snapshot · ${bounded.join(" · ")}` : "Read-only office · one project snapshot",
      results: reportTasks.length ? "Closure reasons shown · acceptance is not inferred" : "No closure reports in this snapshot · no verified-result claim",
    },
    detail: {
      chatId: activeChatId || null,
      status: activeChatId ? detailStatus : "idle",
      hint: detailHint,
      agents: activeDesk?.sessionAgents ?? [],
      runningAgent: activeDesk?.agent ?? null,
    },
  };
}

/** One compact DOM label; title and state remain words in grayscale. */
export function officeObjectLabel(node) {
  const status = node?.status ?? officeState(node);
  const title = text(node?.title).trim() || "Untitled";
  const short = title.length > 34 ? `${title.slice(0, 33)}…` : title;
  const priority = node?.kind === "task" && Number.isFinite(node?.item?.priority) ? ` P${node.item.priority}` : "";
  const agent = node?.kind === "execution" && known(node?.agent) ? ` · ${node.agent}` : "";
  return `${status.icon} ${short}${priority} · ${status.label}${agent}`;
}

/** A short textual equivalent of a selected object's real visible relations. */
export function officeRelations(model, selectedId) {
  const id = known(selectedId);
  if (!id) return [];
  return arrayOf(model?.routes)
    .filter((route) => route.from === id || route.to === id)
    .map((route) => route.relation)
    .filter(Boolean)
    .slice(0, 8);
}

/** Whether the model is within the deliberately simple instanced contract. */
export function officeModelStats(model) {
  return {
    objects: arrayOf(model?.nodes).length,
    pickTargets: arrayOf(model?.pickTargets).length,
    parts: arrayOf(model?.parts).length,
    routes: arrayOf(model?.routes).length,
    zones: arrayOf(model?.zones).length,
    reports: arrayOf(model?.reports).length,
  };
}
