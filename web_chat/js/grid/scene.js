/**
 * The SceneModel: one project's map answer as a plain, renderer-independent
 * scene (web_chat/js/grid/scene.js).
 *
 * The map answer (``GET /api/projects/{id}/map``) says *what* a project holds;
 * the chat detail (``GET /api/projects/{id}/map/chats/{chat_id}``) says what one
 * selected chat did. This module says *where* every one of those things stands
 * in one space, so a native WebGL renderer and the mobile Layers/Timeline
 * projections draw the very same scene instead of each inventing its own
 * geography. Nothing here touches the DOM, WebGL, the clock or the network: the
 * whole module is arithmetic on plain objects, and the same answers always
 * yield the same scene.
 *
 * ## What the scene holds
 *
 * The hierarchy is the answer's own, exactly:
 *
 * ```text
 * project:<id>
 * ├── collection:<id>:work  (only when the project has tasks)
 * │   └── task:<task>
 * ├── collection:<id>:chats
 * │   └── chat:<chat>                       (map.processes.items)
 * │       ├── message:<chat>:<message>      (detail, only a loaded chat)
 * │       ├── execution:<chat>:<run>        (detail.executions; explicit turn id)
 * │       │   └── operation:<chat>:<op>     (detail.operations, by execution_id)
 * │       │       ├── operation:<chat>:<child>   (parent_id, same trace)
 * │       │       └── execution sub:<chat>:<agent-op>  (an `agent` operation)
 * │       └── ...
 * └── collection:<id>:files
 *     └── file:<path>
 * ```
 *
 * There is no context/agent-island node: the agent hierarchy of a trace is
 * carried by the operations themselves. An operation of kind ``agent`` is a
 * delegation, so it opens a nested execution that owns the operations whose
 * explicit ``parent_id`` names it - exactly what the trace recorded, never a
 * guess. A message is never joined to an execution by timing or by position:
 * only two explicit, equal ``turn_id`` values make a ``message-execution`` edge,
 * and only for the assistant message that owns the turn.
 *
 * ## The three axes
 *
 * - **X - geography.** A stable slot per node inside its own category: tasks by
 *   ``created_at`` then id, chats and files by id, messages by their moment,
 *   executions by their start. The slot is an absolute index, never a fraction
 *   of the current set, so a status change, a new step or a new detail read
 *   moves nothing; a node the answer does not date stands in the undated lane
 *   left of its street. Detail nodes hang to the right of their chat (messages)
 *   or to the left of their owner (executions and operations), so a deeper
 *   trace step never lands on the thing that owns it.
 * - **Y - hierarchy level.** ``project`` at the top, its ``collection`` nodes a
 *   step below, then the three streets - ``intention`` (tasks), ``work``
 *   (chats) and ``artifact`` (files). Inside a chat, each nesting step of the
 *   detail (message/execution, operation, delegated execution, its operations)
 *   stands one level higher than its owner.
 * - **Z - a real moment.** ``created_at`` of a task, ``updated_at`` of a chat,
 *   ``modified`` of a file, ``time`` of a message, ``start_time`` of a run, and
 *   a run's start plus an operation's elapsed ``at_ms`` - on an absolute day
 *   scale from a fixed epoch, not a span normalized to the current minimum and
 *   maximum. A node whose answer carries no timestamp keeps ``time: null`` and
 *   stands in the ``UNKNOWN_Z`` lane: that coordinate is a place in the scene, a
 *   claim that the moment is unknown, never a moment.
 *
 * ## Data honesty
 *
 * Every field of a node comes from the answer: no agent is invented (an
 * execution's ``agent`` is ``unknown`` when the answer says so, and an unknown
 * agent stays ``null``), an unknown file modality stays ``unknown``, an
 * unparsable timestamp stays ``null``, a dependency or a task link to a task the
 * map does not show produces no edge, and a file reference is only drawn when
 * the referenced path (or its visible top-level ancestor) really is in the
 * answer. Message and operation items are re-projected through the safe,
 * allowlisted fields only: no trace body, no reasoning text, no raw metadata.
 *
 * ## Contract
 *
 * ```text
 * createScene(map, {projectId?, chatDetails?}) -> Scene
 * sliceScene(scene, {levels?, from?, to?, query?}) -> Scene
 * relatedIds(scene, selectedId) -> string[]
 * sceneStats(scene) -> object
 * ```
 *
 * A **Scene** is ``{version, projectId, nodes, edges, bounds, timeRange}``.
 * A **SceneNode** is ``{id, kind, title, href, position, size, semantic, state,
 * time, dimensions, item}``:
 *
 * | field | meaning |
 * | --- | --- |
 * | ``id`` | deterministic, type-prefixed: ``project:<id>``, ``collection:<id>:<name>``, ``task:<id>``, ``chat:<chat>``, ``file:<path>``, ``message:<chat>:<id>``, ``execution:<chat>:<run>``, ``operation:<chat>:<id>`` |
 * | ``kind`` | ``project``, ``collection``, ``task``, ``chat``, ``file``, ``message``, ``execution`` or ``operation`` |
 * | ``title`` | the answer's own title (never a body) |
 * | ``href`` | the page that opens the thing; ``""`` when the answer cannot reach it |
 * | ``position`` | ``[x, y, z]`` in scene units |
 * | ``size`` | ``[width, height, depth]`` in scene units |
 * | ``semantic`` | ``{level, label}`` with level ``structure``/``intention``/``work``/``artifact`` |
 * | ``state`` | the node's own state word (a task's state, a chat's run state, a message's role, an execution/operation status, a file's kind) |
 * | ``time`` | ISO 8601 UTC of the real moment, or ``null`` |
 * | ``dimensions`` | ``{type, status, agent, modality, provenance, refs}`` - agent only when real, modality only for files |
 * | ``item`` | the safe object of the answer the node was built from |
 *
 * A **SceneEdge** is ``{id, from, to, kind, item?}`` with kind ``contains``
 * (project -> collection, collection -> its items, chat -> its detail),
 * ``depends`` (task -> dependency), ``turn``/``plan`` (chat -> a task it touched
 * / its plan holds), ``message-execution`` (message -> the run of the same
 * explicit turn), ``delegates`` (an ``agent`` operation -> the nested execution
 * it opened), ``operation`` (execution -> an operation it owns),
 * ``contains-operation`` (operation -> a child operation of the same trace) or
 * ``reference`` (operation -> a real file it named, ``item.path``/``item.ref``
 * the safe referenced path).
 *
 * Bounds are the extents of the nodes (``position ± size / 2``), so a camera
 * can frame the scene without touching node data.
 *
 * @module grid/scene
 */

/**
 * The scene contract version; a renderer keys on it.
 *
 * 3 - the canonical Project / Files / Chats / Messages / Executions /
 *     Operations hierarchy. Context nodes and the chat activity island are
 *     gone; a selected chat's detail is passed in explicitly and an ``agent``
 *     operation opens a nested execution.
 */
export const SCENE_VERSION = 3;

/** The semantic levels, top (structure) to bottom (intention) - see SEMANTIC_Y. */
export const SEMANTIC_LEVELS = ["structure", "intention", "work", "artifact"];

/** The Y coordinate of each semantic level, in scene units. */
export const SEMANTIC_Y = { structure: 176, intention: 0, work: 40, artifact: 80 };

/** The human label of each semantic level. */
export const SEMANTIC_LABEL = {
  structure: "Structure",
  intention: "Intention",
  work: "Work",
  artifact: "Artifact",
};

/** The kinds of a node, in draw order. */
export const NODE_KINDS = [
  "project",
  "collection",
  "task",
  "chat",
  "file",
  "message",
  "execution",
  "operation",
];

/** The kinds of an edge. */
export const EDGE_KINDS = [
  "contains",
  "turn",
  "plan",
  "depends",
  "message-execution",
  "delegates",
  "operation",
  "contains-operation",
  "reference",
];

/**
 * The size of each node in scene units. The X stride of a kind is its width plus
 * a gap, so two nodes of one street never overlap along their own street.
 */
export const NODE_SIZE = {
  project: [22, 10, 6],
  collection: [16, 6, 5],
  task: [12, 6, 4],
  chat: [14, 8, 4],
  file: [10, 4, 3],
  message: [9, 4, 3],
  execution: [11, 5, 4],
  operation: [8, 3, 2],
};

/** How much higher each nesting step of a chat's detail stands. */
export const DETAIL_Y_STEP = 12;

/** The Z lane of a node the answer gave no timestamp for. Not a moment. */
export const UNKNOWN_Z = 0;

/** The fixed epoch of the Z axis: 2020-01-01T00:00:00Z, one unit per day. */
const EPOCH_MS = Date.UTC(2020, 0, 1);
const DAY_MS = 24 * 60 * 60 * 1000;

/** The X gap between two nodes of one kind. */
const SLOT_GAP = 4;

/** The X gap between an owner and the detail nodes hanging beside it. */
const DETAIL_X_GAP = 6;

/** The X gap between two collection platforms. */
const COLLECTION_STRIDE = 28;

/** The extensions this module calls text and binary; anything else is unknown. */
export const TEXT_EXTENSIONS = new Set([
  "c", "cfg", "conf", "cpp", "cs", "css", "csv", "go", "h", "html", "ini",
  "java", "js", "json", "jsx", "log", "md", "mjs", "cjs", "php", "pl", "py",
  "rb", "rs", "rst", "sh", "sql", "svg", "toml", "ts", "tsx", "txt", "xml",
  "yaml", "yml", "zsh",
]);
export const BINARY_EXTENSIONS = new Set([
  "7z", "avi", "bin", "bmp", "bz2", "class", "dll", "dylib", "exe", "gif",
  "gz", "ico", "jpeg", "jpg", "mov", "mp3", "mp4", "o", "otf", "parquet",
  "pdf", "pickle", "png", "pt", "pyc", "so", "tar", "ttf", "wav", "webm",
  "webp", "woff", "woff2", "zip",
]);

const text = (value) => (typeof value === "string" ? value : value == null ? "" : String(value));
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const arrayOf = (value) => (Array.isArray(value) ? value : []);
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

/** A real non-empty string, or null: a value this module may call known. */
function known(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

/** A real number, never a bool, NaN or infinity; else null. */
function numberOrNull(value) {
  if (typeof value === "boolean" || typeof value !== "number") return null;
  return Number.isFinite(value) ? value : null;
}

/**
 * A timestamp as epoch milliseconds, or null when the answer has none.
 *
 * Only a finite number or a string JavaScript can parse are admitted; anything
 * else stays unknown instead of becoming "now" or zero.
 *
 * @param {unknown} value
 * @returns {number|null}
 */
export function msOf(value) {
  if (typeof value === "number") return numberOrNull(value);
  const string = known(value);
  if (string === null) return null;
  const parsed = Date.parse(string);
  return Number.isFinite(parsed) ? parsed : null;
}

/** An ISO 8601 UTC stamp of a real moment, or null. */
function stampOf(ms) {
  if (ms === null) return null;
  try {
    return new Date(ms).toISOString();
  } catch (error) {
    return null;
  }
}

/** The Z coordinate of a real moment; the unknown lane when there is none. */
function zOf(ms) {
  return ms === null ? UNKNOWN_Z : (ms - EPOCH_MS) / DAY_MS;
}

/** A run's start as epoch milliseconds (the API gives it in seconds). */
function startMsOf(run) {
  const seconds = numberOrNull(run?.start_time);
  return seconds === null ? null : seconds * 1000;
}

/** A type-prefixed node id, so ids of two kinds never collide. */
export function nodeId(kind, id) {
  return `${text(kind)}:${text(id)}`;
}

/** The node id of a project. */
const projectNodeId = (projectId) => nodeId("project", text(projectId));
/** The node id of one collection platform of a project. */
const collectionNodeId = (projectId, name) => nodeId("collection", `${text(projectId)}:${text(name)}`);
/** The node id of a task of the answer. */
const taskNodeId = (task) => nodeId("task", text(task?.id));
/** The node id of a chat of the answer. */
const chatNodeId = (chat) => nodeId("chat", text(chat?.chat_id));
/** The node id of a file entry of the answer. */
const fileNodeId = (entry) => nodeId("file", text(entry?.path) || text(entry?.name));
/** The node id of one message of a chat's detail. */
const messageNodeId = (chatId, messageId) => nodeId("message", `${text(chatId)}:${text(messageId)}`);
/** The node id of one execution (run) of a chat's detail. */
const executionNodeId = (chatId, runId) => nodeId("execution", `${text(chatId)}:${text(runId)}`);
/** The node id of the execution an ``agent`` operation of a trace opened. */
const subExecutionNodeId = (chatId, operationId) =>
  nodeId("execution", `sub:${text(chatId)}:${text(operationId)}`);
/** The node id of one operation of a chat's detail. */
const operationNodeId = (chatId, operationId) => nodeId("operation", `${text(chatId)}:${text(operationId)}`);

/** The X stride of a kind: the width of its nodes plus a gap. */
const strideOf = (kind) => (NODE_SIZE[kind]?.[0] ?? 10) + SLOT_GAP;

/** The Y of a node that is not on a street: the project and its platforms. */
const PROJECT_Y = SEMANTIC_Y.structure + 24;
const COLLECTION_Y = SEMANTIC_Y.structure;

/** The highest edge kind rank, for a stable edge order. */
const edgeRank = (kind) => {
  const index = EDGE_KINDS.indexOf(kind);
  return index === -1 ? EDGE_KINDS.length : index;
};

/** A node's state word: the answer's own, or null when it has none. */
function taskState(task) {
  return known(task?.state) ?? known(task?.status) ?? null;
}

/** A chat's state word: what the rail says of the chat now. */
function chatState(chat) {
  if (chat?.awaiting_approval === true) return "awaiting_approval";
  if (chat?.active === true) return "in_progress";
  return "idle";
}

/** The modality of a file entry: directory, link, text, binary or honestly unknown. */
function modalityOf(entry) {
  const kind = known(entry?.kind) || "other";
  if (kind === "dir") return "directory";
  if (kind === "link") return "link";
  if (kind !== "file") return "unknown";
  const name = text(entry?.name) || text(entry?.path);
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : "";
  if (TEXT_EXTENSIONS.has(extension)) return "text";
  if (BINARY_EXTENSIONS.has(extension)) return "binary";
  return "unknown";
}

/** The task page of one task; the project is named only when it is known. */
function taskHref(projectId, taskId) {
  const params = new URLSearchParams();
  if (projectId) params.set("project", projectId);
  params.set("task", text(taskId));
  return `/tasks?${params.toString()}`;
}

/** The chat page of one conversation. */
function chatHref(chatId) {
  return `/?conversation=${encodeURIComponent(text(chatId))}`;
}

/** The files page URL of one entry, from the answer's own `files_url`. */
function fileHref(filesUrl, entry) {
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

/**
 * A project-relative POSIX path as the answer normalizes it, or null.
 *
 * The same check the server applies to a file chip: no absolute path, no
 * backslash, no empty/`.`/`..` segment, nothing hidden. Syntactic only - it is
 * not a claim that the path exists.
 */
function safeRelativePath(value) {
  const raw = known(value);
  if (raw === null) return null;
  if (raw.startsWith("/") || raw.startsWith("~") || raw.includes("\\") || raw.includes("\0")) return null;
  if (raw.includes("://") || /^[A-Za-z]:/.test(raw)) return null;
  let parts = raw.split("/");
  if (parts.some((part) => !part || part === "." || part === ".." || part.startsWith("."))) return null;
  if (parts[0] === "project") parts = parts.slice(1);
  if (!parts.length) return null;
  return parts.join("/");
}

/** Every safe, unique, project-relative path an operation names. */
function safeFiles(item) {
  const files = [];
  const seen = new Set();
  for (const candidate of arrayOf(item?.files)) {
    const path = safeRelativePath(candidate);
    if (path === null || seen.has(path)) continue;
    seen.add(path);
    files.push(path);
  }
  return files;
}

/** The safe projection of one message of a chat detail. */
function safeMessageItem(item) {
  const safe = {
    id: text(item?.id),
    role: text(item?.role) || "other",
    time: known(item?.time),
    kind: known(item?.kind),
    preview: text(item?.preview),
    has_image: item?.has_image === true,
  };
  const turnId = known(item?.turn_id);
  if (turnId !== null) safe.turn_id = turnId;
  return safe;
}

/** The safe projection of one execution (run) of a chat detail. */
function safeExecutionItem(run) {
  return {
    id: text(run?.id),
    turn_id: known(run?.turn_id),
    agent: text(run?.agent) || "unknown",
    status: known(run?.status) || "",
    start_time: numberOrNull(run?.start_time),
    end_time: numberOrNull(run?.end_time),
    duration_ms: numberOrNull(run?.duration_ms),
    error: run?.error === true,
  };
}

/** The safe projection of one operation of a chat detail (no body ever). */
function safeOperationItem(operation) {
  const kind = known(operation?.kind) || "step";
  return {
    id: text(operation?.id),
    execution_id: text(operation?.execution_id),
    parent_id: known(operation?.parent_id),
    kind,
    title: text(operation?.title),
    status: known(operation?.status) || "",
    at_ms: numberOrNull(operation?.at_ms),
    duration_ms: numberOrNull(operation?.duration_ms),
    // Only a message's own subtitle is public narration; every other body stays out.
    summary: kind === "message" ? text(operation?.summary).trim() : "",
    files: safeFiles(operation),
  };
}

/**
 * A chat detail as this module trusts it: the reply of
 * ``GET /api/projects/{id}/map/chats/{chat_id}``, or anything shaped like it.
 * A detail that is not an object is no detail at all.
 */
function detailOf(value) {
  return isObject(value) ? value : null;
}

/**
 * One slot per entry, ordered by a caller's deterministic key.
 *
 * The slot is an absolute index in that order, so a later entry never moves an
 * earlier one and nothing here depends on how many entries there happen to be.
 * An entry with no real timestamp sorts after every dated one, which is why
 * callers keep the undated ones in a lane of their own.
 *
 * @param {Array<object>} entries
 * @param {(entry: object) => {time: number|null, id: string}} keyOf
 * @returns {Map<object, number>}
 */
function slotsOf(entries, keyOf) {
  const ordered = entries.map((entry, order) => ({ entry, order, key: keyOf(entry) }));
  ordered.sort((a, b) => {
    if (a.key.time === null && b.key.time !== null) return 1;
    if (a.key.time !== null && b.key.time === null) return -1;
    if (a.key.time !== null && b.key.time !== null && a.key.time !== b.key.time) {
      return a.key.time - b.key.time;
    }
    const byId = cmp(a.key.id, b.key.id);
    if (byId !== 0) return byId;
    return a.order - b.order;
  });
  const slots = new Map();
  ordered.forEach((row, slot) => slots.set(row.entry, slot));
  return slots;
}

/** A sort key: a real timestamp (or null, which sorts last) and a stable id. */
function keyOf(time, id) {
  return { time, id };
}

/** The extents of a set of nodes: `position ± size / 2`, never NaN. */
function computeBounds(nodes) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const node of nodes) {
    for (let axis = 0; axis < 3; axis += 1) {
      const at = numberOrNull(node?.position?.[axis]);
      const half = numberOrNull(node?.size?.[axis]);
      if (at === null) continue;
      const extent = half === null ? 0 : Math.abs(half) / 2;
      min[axis] = Math.min(min[axis], at - extent);
      max[axis] = Math.max(max[axis], at + extent);
    }
  }
  for (let axis = 0; axis < 3; axis += 1) {
    if (!Number.isFinite(min[axis]) || !Number.isFinite(max[axis])) {
      min[axis] = 0;
      max[axis] = 0;
    }
  }
  return { min, max };
}

/** The real moments a set of nodes carries, as epoch milliseconds. */
function computeTimeRange(nodes) {
  let from = null;
  let to = null;
  for (const node of nodes) {
    const at = msOf(node?.time);
    if (at === null) continue;
    if (from === null || at < from) from = at;
    if (to === null || at > to) to = at;
  }
  return { from, to };
}

/** A node of the given kind at the given place. */
function makeNode(kind, id, title, href, position, timeMs, state, dimensions, item) {
  return {
    id,
    kind,
    title: text(title) || id,
    href: text(href),
    position,
    size: [...NODE_SIZE[kind]],
    semantic: { level: semanticOf(kind), label: SEMANTIC_LABEL[semanticOf(kind)] },
    state: state ?? null,
    time: stampOf(timeMs),
    dimensions,
    item,
  };
}

/** The semantic level of a kind: what the thing is, not where it nests. */
function semanticOf(kind) {
  if (kind === "project" || kind === "collection") return "structure";
  if (kind === "task") return "intention";
  if (kind === "file") return "artifact";
  return "work";
}

/** The dimensions of a node: what it is, without inventing what the answer omits. */
function makeDimensions(
  kind,
  {
    status = null,
    agent = null,
    modality = null,
    slice,
    id,
    project = null,
    chat = null,
    parent = null,
    execution = null,
    turn = null,
    total = null,
    refs = null,
  } = {},
) {
  const provenance = { slice, id: text(id), project };
  if (chat !== null) provenance.chat = chat;
  if (parent !== null) provenance.parent = parent;
  if (execution !== null) provenance.execution = execution;
  if (turn !== null) provenance.turn = turn;
  const dimensions = {
    type: kind,
    status,
    agent,
    modality,
    provenance,
    refs: {
      tasks: arrayOf(refs?.tasks),
      files: arrayOf(refs?.files),
      chat: refs?.chat ?? null,
    },
  };
  const owned = numberOrNull(total);
  if (owned !== null) dimensions.total = owned;
  return dimensions;
}

/** The file entries of the answer that a reference may match: visible files and dirs. */
function visibleFileIndex(files) {
  const index = new Map();
  for (const entry of files) {
    if (!isObject(entry)) continue;
    const kind = known(entry.kind);
    if (kind !== "file" && kind !== "dir") continue;
    const path = known(entry.path) || known(entry.name);
    if (path === null || path.startsWith(".")) continue;
    if (!index.has(path)) index.set(path, entry);
  }
  return index;
}

/** The visible entry a referenced path names: the exact one, or its top-level ancestor. */
function matchVisibleFile(index, path) {
  const exact = index.get(path);
  if (exact) return exact;
  const slash = path.indexOf("/");
  if (slash <= 0) return null;
  const top = path.slice(0, slash);
  const ancestor = index.get(top);
  return ancestor && known(ancestor.kind) === "dir" ? ancestor : null;
}

/** A node id's own place in its street: the chat, an execution and a message all name one. */
const collectionNames = ["work", "chats", "files"];

/**
 * Build the scene of one map answer and, optionally, the loaded detail of some
 * of its chats.
 *
 * @param {object} map the answer of ``GET /api/projects/{id}/map`` (or anything
 *   shaped like it); missing slices are simply empty.
 * @param {{projectId?: string, chatDetails?: Record<string, object>}} [options]
 *   ``projectId`` overrides the answer's own project id (the hrefs and the
 *   project node are built from it); ``chatDetails`` maps a chat id to the
 *   answer of ``GET /api/projects/{id}/map/chats/{chat_id}`` for the chats whose
 *   detail is loaded (a selected chat only - never a speculation).
 * @returns {{
 *   version: number,
 *   projectId: string|null,
 *   nodes: Array<object>,
 *   edges: Array<object>,
 *   bounds: {min: number[], max: number[]},
 *   timeRange: {from: number|null, to: number|null},
 * }}
 */
export function createScene(map, options = {}) {
  const answer = isObject(map) ? map : {};
  const project = isObject(answer?.project) ? answer.project : {};
  const projectId = known(options?.projectId) ?? known(project?.id) ?? null;
  const filesUrl = known(answer?.files_url) || "";
  const details = isObject(options?.chatDetails) ? options.chatDetails : {};

  const tasks = arrayOf(answer?.tasks?.items).filter(isObject);
  const chats = arrayOf(answer?.chats?.items ?? answer?.processes?.items).filter(isObject);
  const files = arrayOf(answer?.files?.items).filter(isObject);

  const taskIndex = new Map();
  for (const task of tasks) {
    const id = known(task.id);
    if (id !== null && !taskIndex.has(id)) taskIndex.set(id, task);
  }
  const fileIndex = visibleFileIndex(files);

  // A task stands by its created_at; one the answer does not date stands in the
  // undated lane left of the street, so a new dated task never displaces it.
  const datedTasks = tasks.filter((task) => msOf(task.created_at) !== null);
  const undatedTasks = tasks.filter((task) => msOf(task.created_at) === null);
  const taskSlots = slotsOf(datedTasks, (task) => keyOf(msOf(task.created_at), text(task.id)));
  const undatedSlots = slotsOf(undatedTasks, (task) => keyOf(null, text(task.id)));
  const chatSlots = slotsOf(chats, (chat) => keyOf(null, text(chat.chat_id) || text(chat.title)));
  const fileSlots = slotsOf(files, (entry) => keyOf(null, text(entry.path) || text(entry.name)));

  const nodes = [];
  const edges = [];
  const edgeIds = new Set();

  /**
   * Add an edge once, whatever order the answer names it in.
   *
   * Both ends are checked when the scene is finished, not here: an operation's
   * parent may be laid out after its child, and an edge is still real the
   * moment both nodes exist anywhere in the finished scene.
   */
  const addEdge = (kind, from, to, item) => {
    if (!from || !to || from === to) return;
    const id = `${kind}:${from}->${to}${item?.path ? `#${item.path}` : ""}`;
    if (edgeIds.has(id)) return;
    edgeIds.add(id);
    const edge = { id, from, to, kind };
    if (item !== undefined) edge.item = item;
    edges.push(edge);
  };

  // -- the project and its collection platforms ------------------------------

  const projectTime = msOf(project.updated_at);
  if (projectId !== null) {
    nodes.push(
      makeNode(
        "project",
        projectNodeId(projectId),
        project.title || projectId,
        "",
        [0, PROJECT_Y, zOf(projectTime)],
        projectTime,
        null,
        makeDimensions("project", {
          slice: "project",
          id: projectId,
          project: projectId,
        }),
        isObject(answer.project) ? answer.project : { id: projectId },
      ),
    );

    const work = arrayOf(answer?.tasks?.items).length > 0 || answer?.tasks?.available === true;
    const names = work ? collectionNames : collectionNames.filter((name) => name !== "work");
    names.forEach((name) => {
      const x = (collectionNames.indexOf(name) - 1) * COLLECTION_STRIDE;
      const title = name === "work" ? "Work" : name === "chats" ? "Chats" : "Files";
      nodes.push(
        makeNode(
          "collection",
          collectionNodeId(projectId, name),
          title,
          "",
          [x, COLLECTION_Y, zOf(projectTime)],
          projectTime,
          null,
          makeDimensions("collection", {
            slice: `collection:${name}`,
            id: `${projectId}:${name}`,
            project: projectId,
          }),
          { id: `${projectId}:${name}`, name, project: projectId },
        ),
      );
      addEdge("contains", projectNodeId(projectId), collectionNodeId(projectId, name));
    });
  }
  const workCollection = projectId === null ? null : collectionNodeId(projectId, "work");
  const chatsCollection = projectId === null ? null : collectionNodeId(projectId, "chats");
  const filesCollection = projectId === null ? null : collectionNodeId(projectId, "files");

  // -- tasks: the Work branch (the map's own plan, preserved) -----------------

  tasks.forEach((task) => {
    const timeMs = msOf(task.created_at);
    const dated = timeMs !== null;
    const slot = dated ? taskSlots.get(task) ?? 0 : undatedSlots.get(task) ?? 0;
    const x = dated ? slot * strideOf("task") : -(slot + 1) * strideOf("task");
    const state = taskState(task);
    nodes.push(
      makeNode(
        "task",
        taskNodeId(task),
        task.title,
        taskHref(projectId, task.id),
        [x, SEMANTIC_Y.intention, zOf(timeMs)],
        timeMs,
        state,
        makeDimensions("task", {
          status: known(task.status),
          agent: known(task.assignee),
          slice: "tasks",
          id: task.id,
          project: projectId,
        }),
        task,
      ),
    );
    if (workCollection !== null) addEdge("contains", workCollection, taskNodeId(task));
  });

  // -- chats: the Chats branch -----------------------------------------------

  const chatNodes = new Map();
  chats.forEach((chat) => {
    const chatId = text(chat.chat_id);
    if (chatNodes.has(chatId)) return;
    const timeMs = msOf(chat.updated_at);
    const slot = chatSlots.get(chat) ?? 0;
    const state = chatState(chat);
    const linkedTasks = [];
    for (const link of arrayOf(chat.task_links)) {
      if (!isObject(link)) continue;
      const taskId = known(link.task_id);
      if (taskId !== null && taskIndex.has(taskId) && !linkedTasks.includes(taskId)) linkedTasks.push(taskId);
    }
    const node = makeNode(
      "chat",
      chatNodeId(chat),
      chat.title,
      chatHref(chat.chat_id),
      [slot * strideOf("chat"), SEMANTIC_Y.work, zOf(timeMs)],
      timeMs,
      state,
      makeDimensions("chat", {
        status: state,
        slice: "chats",
        id: chatId,
        project: projectId,
        refs: { tasks: linkedTasks, files: [], chat: chatId || null },
      }),
      chat,
    );
    nodes.push(node);
    chatNodes.set(chatId, node);
    if (chatsCollection !== null) addEdge("contains", chatsCollection, node.id);

    for (const link of arrayOf(chat.task_links)) {
      if (!isObject(link)) continue;
      const relation = known(link.relation);
      const taskId = known(link.task_id);
      if (taskId === null || !taskIndex.has(taskId)) continue;
      if (relation !== "turn" && relation !== "plan") continue;
      addEdge(relation, node.id, taskNodeId(taskIndex.get(taskId)));
    }
  });

  // -- files: the Files branch -----------------------------------------------

  files.forEach((entry) => {
    const kind = known(entry.kind) || "other";
    const timeMs = msOf(entry.modified);
    const slot = fileSlots.get(entry) ?? 0;
    const id = text(entry.path) || text(entry.name);
    const node = makeNode(
      "file",
      fileNodeId(entry),
      entry.name || entry.path,
      fileHref(filesUrl, entry),
      [slot * strideOf("file"), SEMANTIC_Y.artifact, zOf(timeMs)],
      timeMs,
      kind,
      makeDimensions("file", {
        status: kind,
        modality: modalityOf(entry),
        slice: "files",
        id,
        project: projectId,
      }),
      entry,
    );
    nodes.push(node);
    if (filesCollection !== null) addEdge("contains", filesCollection, node.id);
  });

  for (const task of tasks) {
    const from = taskNodeId(task);
    for (const dependency of arrayOf(task.depends_on)) {
      const id = known(dependency);
      if (id === null || !taskIndex.has(id)) continue;
      addEdge("depends", from, taskNodeId(taskIndex.get(id)));
    }
  }

  // -- a selected chat's detail: messages, executions and operations ---------
  //
  // Only the chats whose detail was handed in are read; every node is built
  // from the detail's own safe items, and every link is explicit (a matching
  // turn id, an operation's execution_id, or its parent_id within one trace).

  /** The X of a detail node hanging left of its owner. */
  const leftOf = (anchor, slot, kind) =>
    anchor.position[0] - anchor.size[0] / 2 - DETAIL_X_GAP - slot * strideOf(kind);

  /** The X of a detail node hanging right of its owner. */
  const rightOf = (anchor, slot, kind) =>
    anchor.position[0] + anchor.size[0] / 2 + DETAIL_X_GAP + slot * strideOf(kind);

  const messageIds = new Set();
  const executionIds = new Set();
  const operationIds = new Set();

  chats.forEach((chat) => {
    const chatId = text(chat.chat_id);
    const chatNode = chatNodes.get(chatId);
    const detail = detailOf(details[chatId] ?? details[text(chat.chat_id)]);
    if (!chatNode || detail === null) return;
    const detailHref = text(detail?.chat?.href) || chatNode.href;

    // Messages: the safe projection only, one node per real message id.
    const rawMessages = [];
    for (const item of arrayOf(detail?.messages?.items)) {
      if (!isObject(item)) continue;
      const id = known(item.id);
      if (id === null) continue;
      const nodeIdValue = messageNodeId(chatId, id);
      if (messageIds.has(nodeIdValue)) continue;
      messageIds.add(nodeIdValue);
      rawMessages.push(item);
    }
    const messageSlots = slotsOf(rawMessages, (item) => keyOf(msOf(item.time), text(item.id)));
    const messageNodes = new Map();
    for (const item of rawMessages) {
      const safe = safeMessageItem(item);
      const timeMs = msOf(safe.time);
      const slot = messageSlots.get(item) ?? 0;
      const node = makeNode(
        "message",
        messageNodeId(chatId, safe.id),
        safe.preview || safe.role,
        detailHref,
        [rightOf(chatNode, slot, "message"), chatNode.position[1] + DETAIL_Y_STEP, zOf(timeMs)],
        timeMs,
        safe.role,
        makeDimensions("message", {
          status: safe.kind,
          slice: "messages",
          id: safe.id,
          project: projectId,
          chat: chatId,
          turn: safe.turn_id ?? null,
          refs: { chat: chatId || null },
        }),
        safe,
      );
      nodes.push(node);
      messageNodes.set(safe.id, node);
      addEdge("contains", chatNode.id, node.id);
    }

    // Executions: the runs the detail holds, by their stable run id.
    const rawRuns = [];
    for (const run of arrayOf(detail?.executions?.items)) {
      if (!isObject(run)) continue;
      const id = known(run.id);
      if (id === null) continue;
      const nodeIdValue = executionNodeId(chatId, id);
      if (executionIds.has(nodeIdValue)) continue;
      executionIds.add(nodeIdValue);
      rawRuns.push(run);
    }
    const runSlots = slotsOf(rawRuns, (run) => keyOf(startMsOf(run), text(run.id)));
    const runsById = new Map();
    for (const run of rawRuns) {
      const safe = safeExecutionItem(run);
      const start = startMsOf(run);
      const slot = runSlots.get(run) ?? 0;
      const agent = safe.agent !== "unknown" ? known(safe.agent) : null;
      const node = makeNode(
        "execution",
        executionNodeId(chatId, safe.id),
        safe.turn_id ? `Turn ${safe.turn_id}` : safe.agent,
        detailHref,
        [leftOf(chatNode, slot, "execution"), chatNode.position[1] + DETAIL_Y_STEP, zOf(start)],
        start,
        safe.status || null,
        makeDimensions("execution", {
          status: safe.status || null,
          agent,
          slice: "executions",
          id: safe.id,
          project: projectId,
          chat: chatId,
          turn: safe.turn_id,
          refs: { chat: chatId || null },
        }),
        safe,
      );
      nodes.push(node);
      runsById.set(safe.id, { node, start, turn: safe.turn_id });
      addEdge("contains", chatNode.id, node.id);
    }

    // The one explicit join between a message and a run: the same turn id, and
    // only the assistant message that owns that turn.
    for (const messageNode of messageNodes.values()) {
      const message = messageNode.item;
      if (message.role !== "assistant") continue;
      const turn = known(message.turn_id);
      if (turn === null) continue;
      for (const run of runsById.values()) {
        if (run.turn === turn) addEdge("message-execution", messageNode.id, run.node.id);
      }
    }

    // Operations: grouped by the execution its own execution_id names.
    const operationsByRun = new Map();
    for (const operation of arrayOf(detail?.operations?.items)) {
      if (!isObject(operation)) continue;
      const id = known(operation.id);
      const executionId = known(operation.execution_id);
      if (id === null || executionId === null || !runsById.has(executionId)) continue;
      const nodeIdValue = operationNodeId(chatId, id);
      if (operationIds.has(nodeIdValue)) continue;
      operationIds.add(nodeIdValue);
      const bucket = operationsByRun.get(executionId) ?? [];
      bucket.push(operation);
      operationsByRun.set(executionId, bucket);
    }

    for (const [executionId, operations] of operationsByRun) {
      const run = runsById.get(executionId);
      if (!run) continue;
      const operationById = new Map();
      for (const operation of operations) {
        const id = text(operation.id);
        if (!operationById.has(id)) operationById.set(id, operation);
      }
      const parentOf = new Map();
      for (const operation of operations) {
        const id = text(operation.id);
        const parent = known(operation.parent_id);
        parentOf.set(id, parent !== null && parent !== id && operationById.has(parent) ? parent : null);
      }
      /** The nearest `agent` operation above *id*: the execution that really owns it. */
      const agentAncestorOf = (id) => {
        const seen = new Set();
        let current = parentOf.get(id) ?? null;
        while (current !== null && !seen.has(current)) {
          seen.add(current);
          const operation = operationById.get(current);
          if (!operation) return null;
          if (known(operation.kind) === "agent") return current;
          current = parentOf.get(current) ?? null;
        }
        return null;
      };
      const groups = new Map();
      for (const operation of operations) {
        const id = text(operation.id);
        const owner = agentAncestorOf(id) ?? "";
        const bucket = groups.get(owner) ?? [];
        bucket.push(operation);
        groups.set(owner, bucket);
      }

      /** Place one execution's own operations, and the executions they opened. */
      const placeOperations = (ownerNode, operationList) => {
        const ordered = operationList
          .map((operation) => ({
            operation,
            id: text(operation.id),
            at: numberOrNull(operation.at_ms),
          }))
          .sort((a, b) => {
            if (a.at === null && b.at !== null) return 1;
            if (a.at !== null && b.at === null) return -1;
            if (a.at !== null && b.at !== null && a.at !== b.at) return a.at - b.at;
            return cmp(a.id, b.id);
          });
        ordered.forEach(({ operation, id, at }, index) => {
          const safe = safeOperationItem(operation);
          const timeMs = run.start !== null && at !== null ? run.start + at : null;
          const node = makeNode(
            "operation",
            operationNodeId(chatId, safe.id),
            safe.title,
            detailHref,
            [
              leftOf(ownerNode, index, "operation"),
              ownerNode.position[1] + DETAIL_Y_STEP,
              zOf(timeMs),
            ],
            timeMs,
            safe.status || null,
            makeDimensions("operation", {
              status: safe.status || null,
              agent: known(safe.kind) === "agent" ? safe.title : null,
              slice: "operations",
              id: safe.id,
              project: projectId,
              chat: chatId,
              execution: ownerNode.id,
              parent: parentOf.get(id) === null ? null : operationNodeId(chatId, parentOf.get(id)),
              refs: { files: safe.files, chat: chatId || null },
            }),
            safe,
          );
          nodes.push(node);
          addEdge("operation", ownerNode.id, node.id);
          const parent = parentOf.get(id);
          if (parent !== null) addEdge("contains-operation", operationNodeId(chatId, parent), node.id);

          for (const path of safe.files) {
            const entry = matchVisibleFile(fileIndex, path);
            if (!entry) continue;
            addEdge("reference", node.id, fileNodeId(entry), {
              path,
              match: text(entry.path) || text(entry.name),
              ref: path,
            });
          }

          if (safe.kind !== "agent") return;
          // An `agent` operation is a delegation: it opens the execution that
          // owns the operations whose explicit parent_id names it.
          const nested = groups.get(id) ?? [];
          const subNode = makeNode(
            "execution",
            subExecutionNodeId(chatId, safe.id),
            safe.title,
            detailHref,
            [
              leftOf(node, 0, "execution"),
              node.position[1] + DETAIL_Y_STEP,
              zOf(timeMs),
            ],
            timeMs,
            safe.status || null,
            makeDimensions("execution", {
              status: safe.status || null,
              agent: known(safe.title),
              slice: "subexecutions",
              id: `sub:${chatId}:${safe.id}`,
              project: projectId,
              chat: chatId,
              parent: node.id,
              refs: { chat: chatId || null },
            }),
            { id: `sub:${chatId}:${safe.id}`, parent_id: safe.id, kind: "agent", agent: safe.title },
          );
          nodes.push(subNode);
          addEdge("delegates", node.id, subNode.id);
          if (nested.length) placeOperations(subNode, nested);
        });
      };

      placeOperations(run.node, groups.get("") ?? []);
    }
  });

  const nodeIds = new Set(nodes.map((node) => node.id));
  nodes.sort((a, b) => {
    const byKind = cmp(NODE_KINDS.indexOf(a.kind), NODE_KINDS.indexOf(b.kind));
    return byKind !== 0 ? byKind : cmp(a.id, b.id);
  });

  const realEdges = edges.filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to));
  realEdges.sort(
    (a, b) =>
      cmp(edgeRank(a.kind), edgeRank(b.kind)) ||
      cmp(a.from, b.from) ||
      cmp(a.to, b.to) ||
      cmp(text(a.item?.path), text(b.item?.path)) ||
      cmp(a.id, b.id),
  );

  return {
    version: SCENE_VERSION,
    projectId,
    nodes,
    edges: realEdges,
    bounds: computeBounds(nodes),
    timeRange: computeTimeRange(nodes),
  };
}

/** A timestamp option as epoch milliseconds, or null when the option is absent. */
function boundOf(value) {
  if (value === null || value === undefined || value === "") return null;
  return msOf(value);
}

/** The moment of a node as milliseconds, parsed from its ISO stamp. */
function nodeTime(node) {
  return msOf(node?.time);
}

/**
 * A filtered view of *scene*: nodes by semantic level, by an inclusive time
 * window and by a title query, and only the edges whose both ends stay.
 *
 * The scene is never mutated; the result is a new scene with its own bounds and
 * time range. A node without a time (``time: null``) cannot be inside a window
 * - it is excluded whenever a bound is given, rather than being placed at some
 * invented moment.
 *
 * @param {object} scene
 * @param {{levels?: string[]|Set<string>, from?: string|number, to?: string|number, query?: string}} [filter]
 * @returns {object} a new scene; the same object shape as :func:`createScene`.
 */
export function sliceScene(scene, filter = {}) {
  const base = isObject(scene) ? scene : createScene({});
  const levels = filter?.levels == null ? null : new Set(arrayOf(filter.levels).map((level) => text(level)));
  const from = boundOf(filter?.from);
  const to = boundOf(filter?.to);
  const hasWindow = from !== null || to !== null;
  const needle = text(filter?.query).trim().toLowerCase();

  const nodes = arrayOf(base.nodes).filter((node) => {
    if (!isObject(node)) return false;
    if (levels !== null && !levels.has(node.semantic?.level)) return false;
    if (hasWindow) {
      const at = nodeTime(node);
      if (at === null) return false;
      if (from !== null && at < from) return false;
      if (to !== null && at > to) return false;
    }
    if (needle && !text(node.title).toLowerCase().includes(needle)) return false;
    return true;
  });
  const visible = new Set(nodes.map((node) => node.id));
  const edges = arrayOf(base.edges).filter(
    (edge) => isObject(edge) && visible.has(edge.from) && visible.has(edge.to),
  );
  return {
    ...base,
    nodes,
    edges,
    bounds: computeBounds(nodes),
    timeRange: computeTimeRange(nodes),
  };
}

/**
 * The selection and everything one typed edge away from it: the selected id
 * first, then the ids of its neighbours (in either direction) in ascending
 * order. An id the scene does not hold yields an empty array.
 *
 * Every canonical relation is a scene edge - containment, a turn's tasks, the
 * delegated execution of an agent operation, an operation's execution and
 * children, a file an operation named - so one traversal answers for every
 * level of the hierarchy, and a detail that is not loaded simply has no edges.
 *
 * @param {object} scene
 * @param {string} selectedId
 * @returns {string[]}
 */
export function relatedIds(scene, selectedId) {
  const id = known(selectedId);
  if (id === null) return [];
  const nodes = arrayOf(scene?.nodes).filter(isObject);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  if (!byId.has(id)) return [];

  const related = new Set();
  for (const edge of arrayOf(scene?.edges)) {
    if (!isObject(edge)) continue;
    if (edge.from === id && byId.has(edge.to)) related.add(edge.to);
    if (edge.to === id && byId.has(edge.from)) related.add(edge.from);
  }
  related.delete(id);
  return [id, ...[...related].sort(cmp)];
}

/**
 * The legacy chat-activity expansion, kept as a no-op for callers that still
 * ask for it.
 *
 * A chat's steps are no longer part of the map answer: they arrive as the
 * explicit ``executions``/``operations`` of a loaded chat detail, and
 * :func:`createScene` builds them from ``options.chatDetails``. There is
 * therefore nothing left to expand on focus, and the scene is returned as it
 * is - a new scene is never invented for a chat nobody loaded.
 *
 * @param {object} scene
 * @returns {object} the same scene
 */
export function expandActivity(scene) {
  return isObject(scene) ? scene : createScene({});
}

/**
 * The counts a render test (or a status bar) needs: how many nodes and edges of
 * each kind, how many nodes on each semantic level, how many without a time,
 * and the scene's extents.
 *
 * @param {object} scene
 * @returns {{
 *   nodes: number,
 *   edges: number,
 *   kinds: Record<string, number>,
 *   edgeKinds: Record<string, number>,
 *   levels: Record<string, number>,
 *   timed: number,
 *   untimed: number,
 *   bounds: {min: number[], max: number[]},
 *   timeRange: {from: number|null, to: number|null},
 * }}
 */
export function sceneStats(scene) {
  const nodes = arrayOf(scene?.nodes).filter(isObject);
  const edges = arrayOf(scene?.edges).filter(isObject);
  const kinds = Object.fromEntries(NODE_KINDS.map((kind) => [kind, 0]));
  const edgeKinds = Object.fromEntries(EDGE_KINDS.map((kind) => [kind, 0]));
  const levels = Object.fromEntries(SEMANTIC_LEVELS.map((level) => [level, 0]));
  let timed = 0;
  for (const node of nodes) {
    if (node.kind in kinds) kinds[node.kind] += 1;
    const level = node.semantic?.level;
    if (level in levels) levels[level] += 1;
    if (msOf(node.time) === null) continue;
    timed += 1;
  }
  for (const edge of edges) {
    if (edge.kind in edgeKinds) edgeKinds[edge.kind] += 1;
  }
  return {
    nodes: nodes.length,
    edges: edges.length,
    kinds,
    edgeKinds,
    levels,
    timed,
    untimed: nodes.length - timed,
    bounds: isObject(scene?.bounds) ? scene.bounds : computeBounds(nodes),
    timeRange: isObject(scene?.timeRange) ? scene.timeRange : computeTimeRange(nodes),
  };
}
