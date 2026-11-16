/**
 * The mobile Layers and Timeline projections of one scene
 * (web_chat/js/grid/layers.js).
 *
 * The 3D Space and these DOM views read the very same SceneModel from
 * `js/grid/scene.js`. The hierarchy is the canonical one, never an island:
 *
 *   Project -> Files / Chats / Work
 *   Chat    -> Messages / Executions
 *   Execution -> Operations
 *   `agent` operation -> child execution -> its operations
 *
 * **Layers** has four tabs - Project, Files, Chats, Work. A container is a
 * tree row: the project opens its collections, a chat opens the messages and
 * executions of the selected chat's detail, an execution opens its operations,
 * and an `agent` operation opens the child execution it delegated to. Only the
 * selected chat's detail is in the scene, so no other chat is expanded.
 *
 * **Timeline** is the whole scene by real moments, grouped by semantic level;
 * a node whose answer carries no timestamp stands in an honest `Unknown time`
 * section instead of being given one. The web/chat detail adds messages,
 * executions and operations with their real times, so they appear here too.
 *
 * The Chats tab groups chats into Now / Recent / Earlier / Unknown time:
 * running chats first, then the freshest update, and a chat without a moment is
 * never given a borrowed one. A selected chat opens one block per explicit turn
 * id (messages and runs that name the same turn sit together), operations keep
 * their causal parent, and an `agent` operation opens the execution it
 * delegated to. While the reader is reading, a fresh answer may not move rows:
 * the order is frozen and the new one is offered as an explicit `Apply new
 * order`, which also keeps the selection and the expanded rows.
 *
 * Both views share the selection: a row selects its node (the same id the 3D
 * renderer and the inspector use), and a separate `Open` link keeps navigation
 * away from reading. A projection draws at most ROW_LIMIT rows; `Show more`
 * adds another SHOW_MORE_STEP and a `Showing X of Y` line tells the truth.
 *
 * @module grid/layers
 */

import { h } from "../lib/dom.js";
import { relatedIds, sliceScene } from "./scene.js";

/** How many rows one projection draws before the reader asks for more. */
export const ROW_LIMIT = 100;
/** How many rows one `Show more` adds. */
export const SHOW_MORE_STEP = 100;

/** The moment windows of the time filter, in milliseconds; `all` has none. */
export const TIME_WINDOW_MS = {
  all: null,
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
};

/** The time filters, in the order they are offered. */
export const TIME_FILTERS = [
  { id: "all", label: "All" },
  { id: "24h", label: "24h" },
  { id: "7d", label: "7d" },
];

/** The four Layers tabs, exactly the canonical top level. */
export const TABS = [
  { id: "project", label: "Project" },
  { id: "files", label: "Files" },
  { id: "chats", label: "Chats" },
  { id: "work", label: "Work" },
];

/** The two modes of the view. */
export const MODES = [
  { id: "layers", label: "Layers" },
  { id: "timeline", label: "Timeline" },
];

/**
 * The chat activity buckets, in the order they are offered: a chat that is
 * running now, one that waits for a person's answer, one updated not long ago,
 * one that is older, and - honestly - one whose answer carries no moment at all.
 *
 * `Now` means only what is really running (`active` / `in_progress`). A chat
 * that is merely idle is never counted as running, however fresh its update:
 * it is Recent, Earlier, or Unknown time. A chat that waits for approval is
 * not running either, and gets its own explicit bucket right after Now.
 */
export const CHAT_BUCKETS = [
  { id: "now", label: "Now" },
  { id: "attention", label: "Needs attention" },
  { id: "recent", label: "Recent" },
  { id: "earlier", label: "Earlier" },
  { id: "unknown", label: "Unknown time" },
];

/**
 * How fresh an idle chat update must be to belong to Recent rather than
 * Earlier. There is no hour-wide "Now" window: Now is only what is running.
 */
export const CHAT_RECENT_MS = 7 * 24 * 60 * 60 * 1000;

/** The honest heading of messages and runs that carry no explicit turn id. */
export const NO_TURN_LABEL = "No turn id";

/** How each kind of node is named and marked: a word, never only a colour. */
export const KIND_SPEC = {
  project: { label: "Project", mark: "▣" },
  collection: { label: "Group", mark: "▤" },
  task: { label: "Task", mark: "◆" },
  chat: { label: "Chat", mark: "▸" },
  message: { label: "Message", mark: "✎" },
  execution: { label: "Execution", mark: "▸" },
  operation: { label: "Operation", mark: "·" },
  file: { label: "File", mark: "▪" },
};

/** How each typed relation reads in a related list. */
export const EDGE_LABEL = {
  contains: "contains",
  turn: "turn",
  plan: "plan",
  depends: "depends",
  "message-execution": "same turn",
  delegates: "delegated to",
  operation: "operation",
  "contains-operation": "step under",
  reference: "references",
};

/** How many related chips one row shows before it just counts the rest. */
export const MAX_CHIPS = 4;

const text = (value) => String(value ?? "");
const arrayOf = (value) => (Array.isArray(value) ? value : []);
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

let viewSeq = 0;

/** The mark and word of a node's kind; an unknown kind says what it is. */
function kindSpec(kind) {
  return KIND_SPEC[text(kind)] ?? { label: text(kind) || "Item", mark: "\u00b7" };
}

/** A state word as it reads: `in_progress` is "in progress", never invented. */
function stateWord(state) {
  return text(state).replace(/_/g, " ").trim();
}

/** A real ISO timestamp as a plain UTC moment; no time is said, not borrowed. */
export function formatMoment(iso) {
  if (!iso) return "no time";
  const ms = typeof iso === "number" ? iso : Date.parse(text(iso));
  if (!Number.isFinite(ms)) return "no time";
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`
  );
}

/** A timestamp option as epoch milliseconds, or null when it is not a moment. */
function msOfOption(value) {
  if (value == null || value === "") return null;
  const ms = typeof value === "number" ? value : Date.parse(text(value));
  return Number.isFinite(ms) ? ms : null;
}

function nodesOf(scene) {
  return arrayOf(scene?.nodes).filter(isObject);
}
function edgesOf(scene) {
  return arrayOf(scene?.edges).filter(isObject);
}
function timeOf(node) {
  if (!node?.time) return null;
  const ms = Date.parse(text(node.time));
  return Number.isFinite(ms) ? ms : null;
}

/** The tab a node belongs to: the canonical four, no agents/context tab. */
function tabOfNode(node) {
  const kind = text(node?.kind);
  if (kind === "project" || kind === "collection") return "project";
  if (kind === "file") return "files";
  if (kind === "task") return "work";
  return "chats";
}

/** The timeline group of a node: its semantic level, never a fabricated one. */
function levelOfNode(node) {
  const level = text(node?.semantic?.level);
  return level === "intention" || level === "artifact" || level === "structure" ? level : "work";
}

const LEVEL_LABEL = {
  structure: "Structure",
  intention: "Intention",
  work: "Work",
  artifact: "Artifact",
};
const LEVEL_ORDER = ["structure", "intention", "work", "artifact"];

/** The line under a row's title: its real moment, then what is really known. */
function metaOf(node) {
  const parts = [formatMoment(node.time)];
  const dimensions = isObject(node.dimensions) ? node.dimensions : {};
  if (dimensions.agent) parts.push(`agent ${text(dimensions.agent)}`);
  if (dimensions.modality) parts.push(text(dimensions.modality));
  const refs = isObject(dimensions.refs) ? dimensions.refs : {};
  const tasks = arrayOf(refs.tasks).length;
  const files = arrayOf(refs.files).length;
  if (tasks) parts.push(`${tasks} ${tasks === 1 ? "task" : "tasks"}`);
  if (files) parts.push(`${files} ${files === 1 ? "file" : "files"}`);
  return parts.join(" \u00b7 ");
}

/** The typed relations of every node, from the scene's real edges only. */
function relationsOf(scene) {
  const byId = new Map(nodesOf(scene).map((node) => [node.id, node]));
  const relations = new Map();
  const add = (id, entry) => {
    if (!relations.has(id)) relations.set(id, []);
    relations.get(id).push(entry);
  };
  for (const edge of edgesOf(scene)) {
    if (!byId.has(edge.from) || !byId.has(edge.to)) continue;
    const kind = text(edge.kind);
    const label = EDGE_LABEL[kind] ?? kind;
    if (!label) continue;
    add(edge.from, { id: edge.to, kind, label, out: true, title: byId.get(edge.to).title });
    add(edge.to, { id: edge.from, kind, label, out: false, title: byId.get(edge.from).title });
  }
  return relations;
}

function byTime(order) {
  const direction = order === "oldest" ? 1 : -1;
  return (a, b) => {
    const at = timeOf(a);
    const bt = timeOf(b);
    if (at !== null && bt !== null && at !== bt) return (at - bt) * direction;
    if (at === null && bt !== null) return 1;
    if (at !== null && bt === null) return -1;
    return cmp(text(a.id), text(b.id));
  };
}

/**
 * Whether a chat is really running right now, from its own state and its own
 * answer. Waiting for approval is not running: the turn is paused, not active,
 * so it never counts as Now.
 */
export function isActiveChat(node) {
  const state = text(node?.state);
  if (state === "in_progress") return true;
  if (state === "awaiting_approval" || state === "idle") return false;
  return node?.item?.active === true;
}

/** Whether a chat waits for a person: its own state, or its own answer's flag. */
export function needsAttention(node) {
  if (text(node?.state) === "awaiting_approval") return true;
  return node?.item?.awaiting_approval === true;
}

/**
 * The activity bucket of one chat: a running chat is Now whatever its moment, a
 * chat waiting for approval is Needs attention, and an idle one is only ever
 * Recent / Earlier / Unknown time - never Now, and never given a moment it does
 * not carry.
 *
 * @param {object} node a chat scene node
 * @param {number|null} anchorMs the moment the windows count back from
 */
export function chatBucketOf(node, anchorMs) {
  if (isActiveChat(node)) return "now";
  if (needsAttention(node)) return "attention";
  const at = timeOf(node);
  if (at === null || !Number.isFinite(anchorMs)) return "unknown";
  const age = Math.max(0, anchorMs - at);
  if (age <= CHAT_RECENT_MS) return "recent";
  return "earlier";
}

/** Chats that are running now come first, then the freshest, then the id. */
export function compareChatActivity(a, b) {
  const aActive = isActiveChat(a) ? 0 : 1;
  const bActive = isActiveChat(b) ? 0 : 1;
  if (aActive !== bActive) return aActive - bActive;
  const at = timeOf(a);
  const bt = timeOf(b);
  if (at !== null && bt !== null && at !== bt) return bt - at;
  if (at === null && bt !== null) return 1;
  if (at !== null && bt === null) return -1;
  return cmp(text(a.id), text(b.id));
}

/**
 * The chats in their activity buckets, each bucket in activity order.
 *
 * @param {Array<object>} nodes chat scene nodes
 * @param {number|null} anchorMs
 * @returns {Array<{id: string, label: string, chats: Array<object>}>}
 */
export function orderChatsByActivity(nodes, anchorMs) {
  const buckets = CHAT_BUCKETS.map((bucket) => ({ ...bucket, chats: [] }));
  const byId = new Map(buckets.map((bucket) => [bucket.id, bucket]));
  for (const node of arrayOf(nodes).filter((entry) => isObject(entry) && entry.kind === "chat")) {
    const bucket = byId.get(chatBucketOf(node, anchorMs));
    if (bucket && !bucket.chats.includes(node)) bucket.chats.push(node);
  }
  for (const bucket of buckets) bucket.chats.sort(compareChatActivity);
  return buckets;
}

/**
 * Keep the order the reader is already reading while the scene changes.
 *
 * Known chats keep their frozen bucket and relative order; a chat that is new
 * in the answer is appended to its own fresh bucket, and a chat that vanished
 * leaves. Nothing already on screen jumps, and `changed` says how many rows
 * the fresh order would move - the view offers that order for explicit apply.
 * A move between buckets counts even when the flat position stays the same:
 * the chat no longer sits under the heading it is read under.
 *
 * @param {Array<{id: string, ids: Array<string>}>} frozen
 * @param {Array<{id: string, ids: Array<string>}>} fresh
 * @returns {{buckets: Array<{id: string, ids: Array<string>}>, added: Array<string>, moved: number, changed: boolean}}
 */
export function mergeChatOrder(frozen, fresh) {
  const frozenBuckets = arrayOf(frozen).filter(isObject);
  const freshBuckets = arrayOf(fresh).filter(isObject);
  const idsOf = (bucket) => arrayOf(bucket?.ids).map(text).filter((id) => id !== "");
  const freshIds = new Set(freshBuckets.flatMap(idsOf));
  const known = new Set(frozenBuckets.flatMap(idsOf));
  const buckets = freshBuckets.map((bucket) => ({ id: text(bucket.id), ids: [] }));
  const byId = new Map(buckets.map((bucket) => [bucket.id, bucket]));

  for (const bucket of frozenBuckets) {
    const target = byId.get(text(bucket.id));
    if (!target) continue;
    for (const id of idsOf(bucket)) {
      if (!freshIds.has(id) || target.ids.includes(id)) continue;
      target.ids.push(id);
    }
  }

  const added = [];
  for (const bucket of freshBuckets) {
    const target = byId.get(text(bucket.id));
    if (!target) continue;
    for (const id of idsOf(bucket)) {
      if (known.has(id) || target.ids.includes(id)) continue;
      target.ids.push(id);
      added.push(id);
    }
  }

  // A chat's place is its bucket and its index inside that bucket, not only its
  // flat position: a chat that moved from Recent to Now at the same flat index
  // is still a real move under the reader's eyes.
  const placesOf = (list) => {
    const places = new Map();
    for (const bucket of list) {
      idsOf(bucket).forEach((id, index) => places.set(id, `${text(bucket.id)}:${index}`));
    }
    return places;
  };
  const freshPlaces = placesOf(freshBuckets);
  const mergedPlaces = placesOf(buckets);
  const freshOrder = freshBuckets.flatMap(idsOf);
  let moved = 0;
  for (const id of freshOrder) {
    if (!known.has(id)) continue;
    if (mergedPlaces.get(id) !== freshPlaces.get(id)) moved += 1;
  }
  return { buckets, added, moved, changed: moved > 0 || added.length > 0 };
}

function mapToObject(map) {
  return Object.fromEntries(map.entries());
}

/** The chat id a scene id names, for expansion in the Chats tab. */
function chatIdOfSceneId(sceneId) {
  const id = text(sceneId);
  const at = id.indexOf(":");
  if (at <= 0) return "";
  const kind = id.slice(0, at);
  const rest = id.slice(at + 1);
  if (kind === "chat") return rest;
  if (kind === "message" || kind === "execution" || kind === "operation") {
    if (kind === "execution" && rest.startsWith("sub:")) {
      const pair = rest.slice(4);
      return pair.slice(0, pair.indexOf(":"));
    }
    return rest.slice(0, rest.indexOf(":"));
  }
  return "";
}

/**
 * The Layers/Timeline projection.
 *
 * @param {HTMLElement} container
 * @param {object} [options] scene, mode?, selectedId?, onSelect?, now?
 */
export function createProjectionView(container, options = {}) {
  const host = container;
  const id = `gridProjection-${(viewSeq += 1)}`;
  const state = {
    scene: isObject(options?.scene) ? options.scene : { nodes: [], edges: [], timeRange: { from: null, to: null } },
    mode: options?.mode === "timeline" ? "timeline" : "layers",
    tab: "chats",
    timeFilter: "all",
    order: "newest",
    selectedId: options?.selectedId == null ? null : text(options.selectedId),
    now: msOfOption(options?.now),
  };
  let onSelect = typeof options?.onSelect === "function" ? options.onSelect : () => {};
  const depth = new Map();
  const folded = new Set();
  // The order the reader is already reading, and the fresh order the scene
  // asks for. New answers never move a row under the reader; the fresh order
  // is applied only when the reader presses the explicit button.
  let frozenOrder = null;
  let pendingOrder = null;
  let orderMoved = 0;
  let orderAdded = [];
  let projectId = text(state.scene?.projectId) || null;
  let destroyed = false;

  const root = h("div.gridProjection", {
    "data-mode": state.mode,
    role: "region",
    "aria-label": "Projection",
  });

  const keyOf = () => `${state.mode}|${state.tab}|${state.timeFilter}|${state.order}`;

  function anchorOf() {
    if (state.now !== null) return { ms: state.now, source: "now" };
    const to = msOfOption(state.scene?.timeRange?.to);
    return to === null ? { ms: null, source: "none" } : { ms: to, source: "scene" };
  }

  function filteredScene() {
    if (state.timeFilter === "all") return state.scene;
    const { ms } = anchorOf();
    if (ms === null) return { ...state.scene, nodes: [], edges: [] };
    return sliceScene(state.scene, { from: ms - TIME_WINDOW_MS[state.timeFilter], to: ms });
  }

  const offeredTabs = () => TABS;

  function tabTotals() {
    const counts = { project: 0, files: 0, chats: 0, work: 0 };
    for (const node of nodesOf(state.scene)) counts[tabOfNode(node)] += 1;
    return counts;
  }

  function visibleNodes() {
    return nodesOf(filteredScene()).filter((node) => tabOfNode(node) === state.tab);
  }

  function inViewNodes() {
    return state.mode === "timeline" ? nodesOf(filteredScene()) : visibleNodes();
  }

  function tabNodesOfBase() {
    return nodesOf(state.scene).filter((node) => tabOfNode(node) === state.tab);
  }

  function untimedHidden() {
    if (state.timeFilter === "all") return 0;
    return tabNodesOfBase().filter((node) => timeOf(node) === null).length;
  }

  function select(nodeId) {
    const next = nodeId == null ? null : text(nodeId);
    if (state.selectedId === next) return;
    state.selectedId = next;
    try {
      onSelect(next);
    } catch {
      /* a listener's own failure must not break the view */
    }
    render();
  }

  /** One row: kind word, title, real moment, typed relations and Open. */
  function row(node, related, relations, layout = {}) {
    const spec = kindSpec(node.kind);
    const stateText = stateWord(node.state);
    const chips = arrayOf(relations?.get(node.id));
    const isSelected = state.selectedId === node.id;
    const isRelated = !isSelected && related.has(node.id);
    const rowDepth = Number.isFinite(layout.depth) ? Math.max(0, layout.depth) : 0;
    const isFolded = layout.folded === true;
    const childCount = Number.isFinite(layout.children) ? layout.children : 0;
    const classes = ["gridProjection__row"];
    if (isSelected) classes.push("gridProjection__row--selected");
    if (isRelated) classes.push("gridProjection__row--related");
    if (rowDepth > 0) classes.push("gridProjection__row--child");
    if (isFolded) classes.push("gridProjection__row--folded");

    const relatedList = chips.length
      ? h(
          "div.gridProjection__related",
          { "aria-label": "Related" },
          ...chips.slice(0, MAX_CHIPS).map((chip) =>
            h("button.gridProjection__chip", {
              type: "button",
              "data-id": chip.id,
              "data-kind": chip.kind,
              title: `${chip.out ? "to" : "from"} ${chip.title}`,
              text: `${chip.out ? "\u2192" : "\u2190"} ${chip.label} \u00b7 ${chip.title}`,
              on: {
                click: (event) => {
                  event?.stopPropagation?.();
                  select(chip.id);
                },
              },
            }),
          ),
          chips.length > MAX_CHIPS
            ? h("span.gridProjection__more", { text: `+${chips.length - MAX_CHIPS} more` })
            : null,
        )
      : null;

    const open =
      text(node.href) === ""
        ? null
        : h("a.gridProjection__open", {
            href: text(node.href),
            rel: "noreferrer",
            "data-id": node.id,
            text: "Open",
            on: { click: (event) => event?.stopPropagation?.() },
          });

    const click = (event) => {
      if (event?.target?.closest?.("a, button")) return;
      select(node.id);
    };
    const keydown = (event) => {
      const key = text(event?.key);
      if (key !== "Enter" && key !== " " && key !== "Spacebar") return;
      event.preventDefault?.();
      select(node.id);
    };

    return h(
      "li",
      {
        class: classes.join(" "),
        "data-id": node.id,
        "data-kind": node.kind,
        "data-level": levelOfNode(node),
        "data-depth": String(rowDepth),
        ...(layout.parent ? { "data-parent": text(layout.parent) } : {}),
        ...(layout.turn ? { "data-turn": text(layout.turn) } : {}),
        tabindex: "0",
        role: "option",
        "aria-selected": isSelected ? "true" : "false",
        "aria-label": `${spec.label}: ${text(node.title)}`,
        on: { click, keydown },
      },
      h(
        "div.gridProjection__rowMain",
        {},
        layout.expandable
          ? h("button.gridProjection__toggle", {
              type: "button",
              "data-for": node.id,
              "aria-expanded": isFolded ? "false" : "true",
              "aria-label": `${isFolded ? "Expand" : "Fold"} ${spec.label}: ${text(node.title)} (${childCount})`,
              title: `${childCount} ${childCount === 1 ? "item" : "items"} under this row`,
              text: isFolded ? "\u25b8" : "\u25be",
              on: {
                click: (event) => {
                  event?.stopPropagation?.();
                  event?.preventDefault?.();
                  if (folded.has(node.id)) folded.delete(node.id);
                  else folded.add(node.id);
                  render();
                },
              },
            })
          : null,
        h("span.gridProjection__mark", { "aria-hidden": "true", text: spec.mark }),
        h(
          "div.gridProjection__rowBody",
          {},
          h(
            "div.gridProjection__head",
            {},
            h("span.gridProjection__kind", { text: spec.label }),
            stateText ? h("span.gridProjection__state", { text: stateText }) : null,
            h("span.gridProjection__title", { text: text(node.title) }),
          ),
          h("div.gridProjection__meta", { text: metaOf(node) }),
          relatedList,
        ),
        open,
      ),
    );
  }

  function group(label, rows) {
    return h(
      "section.gridProjection__group",
      { "aria-label": label },
      h(
        "h3.gridProjection__groupHead",
        {},
        h("span.gridProjection__groupLabel", { text: label }),
        h("span.gridProjection__groupCount", { text: String(rows.length) }),
      ),
      h("ul.gridProjection__list", { role: "listbox", "aria-label": label }, ...rows),
    );
  }

  function emptyPanel() {
    return h(
      "div.gridProjection__panel",
      {
        role: "tabpanel",
        id: `${id}-panel`,
        "aria-labelledby": `${id}-tab-${state.tab}`,
        "data-tab": state.tab,
        "data-mode": state.mode,
      },
      h("p.gridProjection__empty", { text: emptyText() }),
      h("div.gridProjection__footer", {}, h("span.gridProjection__count", { "data-total": "0", "data-shown": "0", text: "Nothing to show" })),
    );
  }

  function emptyText() {
    const label = (TABS.find((tab) => tab.id === state.tab) ?? TABS[0]).label;
    const hidden = untimedHidden();
    if (state.timeFilter !== "all" && hidden > 0) {
      return `No ${label.toLowerCase()} items in this window. ${hidden} without a time cannot be placed in it.`;
    }
    if (!nodesOf(state.scene).length) return "Nothing to show: the map answer carries no items yet.";
    return `No ${label.toLowerCase()} items in this projection.`;
  }

  function footer(total, shown, more, hidden) {
    return h(
      "div.gridProjection__footer",
      {},
      h("span.gridProjection__count", {
        "data-total": String(total),
        "data-shown": String(shown),
        text: `Showing ${shown} of ${total}`,
      }),
      hidden > 0
        ? h("span.gridProjection__note", { text: `${hidden} without a time are outside the ${state.timeFilter} window` })
        : null,
      more > 0
        ? h("button.gridProjection__action.gridProjection__action--more", {
            type: "button",
            text: `Show more (+${Math.min(SHOW_MORE_STEP, more)})`,
            on: {
              click: () => {
                depth.set(keyOf(), Math.min(total, shown + SHOW_MORE_STEP));
                render();
              },
            },
          })
        : null,
    );
  }

  function panel(...children) {
    return h(
      "div.gridProjection__panel",
      {
        role: "tabpanel",
        id: `${id}-panel`,
        "aria-labelledby": `${id}-tab-${state.tab}`,
        "data-tab": state.tab,
        "data-mode": state.mode,
      },
      ...children,
    );
  }

  /** The explicit turn id of a message or run; only its own item may say it. */
  function explicitTurnOf(node) {
    if (node?.kind !== "message" && node?.kind !== "execution") return null;
    const turn = node?.item?.turn_id;
    return turn == null || text(turn).trim() === "" ? null : text(turn);
  }

  /** A turn heading: real only when an item named the turn, never guessed. */
  function turnHead(turn, count, rowDepth) {
    return h(
      "li.gridProjection__turn",
      { role: "presentation", "data-turn": turn ?? "", "data-depth": String(rowDepth) },
      h("span.gridProjection__turnLabel", { text: turn === null ? NO_TURN_LABEL : `Turn ${turn}` }),
      h("span.gridProjection__turnCount", { text: String(count) }),
    );
  }

  /** `Apply new order` and the honest line saying what it would move. */
  function orderNotice() {
    if (!pendingOrder || (!orderMoved && !orderAdded.length)) return null;
    const parts = [];
    if (orderMoved) parts.push(`${orderMoved} ${orderMoved === 1 ? "chat" : "chats"} moved`);
    if (orderAdded.length) parts.push(`${orderAdded.length} new`);
    return h(
      "div.gridProjection__notice",
      {},
      h("span.gridProjection__note", { text: `Order kept while you read \u00b7 ${parts.join(" \u00b7 ")}` }),
      h("button.gridProjection__action.gridProjection__action--order", {
        type: "button",
        text: "Apply new order",
        on: { click: () => applyOrder() },
      }),
    );
  }

  function applyOrder() {
    if (!pendingOrder) return;
    frozenOrder = pendingOrder.map((bucket) => ({ id: bucket.id, ids: [...bucket.ids] }));
    pendingOrder = null;
    orderMoved = 0;
    orderAdded = [];
    render();
  }

  /**
   * The Chats tab: chats in Now / Recent / Earlier / Unknown time, and only the
   * selected chat's expanded tree - messages and runs inside the explicit turn
   * their own items name, operations under the execution that owns them, and a
   * child execution under the `agent` operation that delegated to it.
   *
   * The order is frozen while the reader reads: a fresh answer may move chats,
   * but the rows stay put and the view offers the new order for explicit apply.
   */
  function renderChatsPanel() {
    const filtered = filteredScene();
    const all = nodesOf(filtered);
    const chatNodes = all.filter((node) => node.kind === "chat");
    if (!chatNodes.length) return emptyPanel();
    const selectedChat = chatIdOfSceneId(state.selectedId);
    const relations = relationsOf(filtered);
    const related = new Set(relatedIds(filtered, state.selectedId));
    related.delete(state.selectedId);
    const limit = Math.max(ROW_LIMIT, Number(depth.get(keyOf())) || ROW_LIMIT);

    const freshBuckets = orderChatsByActivity(chatNodes, anchorOf().ms).map((bucket) => ({
      id: bucket.id,
      ids: bucket.chats.map((chat) => chat.id),
    }));
    if (!frozenOrder) {
      frozenOrder = freshBuckets.map((bucket) => ({ id: bucket.id, ids: [...bucket.ids] }));
      pendingOrder = null;
      orderMoved = 0;
      orderAdded = [];
    } else {
      const merged = mergeChatOrder(frozenOrder, freshBuckets);
      frozenOrder = merged.buckets;
      pendingOrder = freshBuckets;
      orderMoved = merged.moved;
      orderAdded = merged.added;
    }

    const byId = new Map(all.map((node) => [node.id, node]));
    const children = new Map();
    const nestedParentOf = new Map();
    for (const edge of edgesOf(filtered)) {
      if (!["contains", "operation", "contains-operation", "delegates"].includes(text(edge.kind))) continue;
      if (!byId.has(edge.from) || !byId.has(edge.to)) continue;
      if (!children.has(edge.from)) children.set(edge.from, []);
      children.get(edge.from).push({ id: edge.to, kind: text(edge.kind) });
      if (text(edge.kind) === "contains-operation") nestedParentOf.set(edge.to, edge.from);
    }
    /**
     * The children of a node. An `agent` operation delegates to a child
     * execution, so its own child operations hang under that execution, not
     * twice under the operation.
     */
    const childIds = (node) => {
      const entries = arrayOf(children.get(node.id));
      const agent = node.kind === "operation" && text(node.item?.kind) === "agent";
      const seen = new Set();
      const directOps = new Set(entries.filter((entry) => entry.kind === "operation").map((entry) => entry.id));
      return entries
        .filter((entry) => !agent || entry.kind === "delegates")
        // A nested operation is drawn through its causal parent, not again as
        // a direct operation of that same execution.
        .filter(
          (entry) =>
            node.kind !== "execution" ||
            entry.kind !== "operation" ||
            !directOps.has(nestedParentOf.get(entry.id)),
        )
        .map((entry) => byId.get(entry.id))
        .filter((kid) => {
          if (!kid || seen.has(kid.id)) return false;
          seen.add(kid.id);
          return true;
        });
    };
    const byOrder = byTime(state.order);
    // A run's operations are a chronology: they always read forward, whatever
    // order the overview above them is read in. An execution is a sequence of
    // steps, not a list of news.
    const byChronology = byTime("oldest");
    const sortedKids = (node) => childIds(node).sort(node.kind === "execution" ? byChronology : byOrder);

    /** One row and, unless folded, its causal children. */
    const walkRows = (node, rowDepth, parent, turn, out) => {
      out.push({
        type: "row",
        node,
        depth: rowDepth,
        parent,
        turn,
        children: childIds(node).length,
        expandable: childIds(node).length > 0,
      });
      if (folded.has(node.id)) return;
      for (const kid of sortedKids(node)) walkRows(kid, rowDepth + 1, node.id, turn, out);
    };

    /**
     * The selected chat's detail: one block per explicit turn id, and one
     * honest block for messages and runs whose own item names no turn. Nothing
     * is joined by timing, position or name.
     */
    const detailEntries = (chat) => {
      const direct = sortedKids(chat);
      const groups = new Map();
      const noTurn = { id: null, messages: [], executions: [] };
      const add = (node, bucketName) => {
        const turn = explicitTurnOf(node);
        if (turn === null) {
          noTurn[bucketName].push(node);
          return;
        }
        const group = groups.get(turn) ?? { id: turn, messages: [], executions: [] };
        group[bucketName].push(node);
        groups.set(turn, group);
      };
      for (const node of direct) {
        if (node.kind === "message") add(node, "messages");
        else if (node.kind === "execution") add(node, "executions");
      }
      const blockTime = (block) => {
        let first = null;
        for (const node of [...block.messages, ...block.executions]) {
          const at = timeOf(node);
          if (at === null) continue;
          if (first === null || (state.order === "oldest" ? at < first : at > first)) first = at;
        }
        return first;
      };
      const blocks = [...groups.values()].sort((a, b) => {
        const at = blockTime(a);
        const bt = blockTime(b);
        if (at !== null && bt !== null && at !== bt) return state.order === "oldest" ? at - bt : bt - at;
        if (at === null && bt !== null) return 1;
        if (at !== null && bt === null) return -1;
        return cmp(a.id, b.id);
      });
      if (noTurn.messages.length || noTurn.executions.length) blocks.push(noTurn);

      /**
       * What a turn really did, in the order it did it: the explicit user
       * message that opened it, the executions it ran, then the assistant's
       * reply. Each part keeps its own chronology, forward. The newest-first
       * overview never flips the inside of a turn: a reply above its question
       * would be a different story.
       */
      const isUserMessage = (node) => node.kind === "message" && text(node.item?.role) === "user";
      const sequenceOf = (block) => {
        const messages = [...block.messages].sort(byChronology);
        const asked = messages.filter(isUserMessage);
        const answered = messages.filter((node) => !isUserMessage(node));
        const executions = [...block.executions].sort(byChronology);
        return [...asked, ...executions, ...answered];
      };

      const entries = [];
      for (const block of blocks) {
        entries.push({
          type: "turn",
          turn: block.id,
          depth: 1,
          count: block.messages.length + block.executions.length,
        });
        for (const node of sequenceOf(block)) walkRows(node, 1, chat.id, block.id, entries);
      }
      return entries;
    };

    const chatEntries = (chat) => {
      const kids = childIds(chat);
      const entries = [
        {
          type: "row",
          node: chat,
          depth: 0,
          parent: null,
          turn: null,
          children: kids.length,
          expandable: kids.length > 0,
        },
      ];
      if (chatIdOfSceneId(chat.id) !== selectedChat || folded.has(chat.id)) return entries;
      entries.push(...detailEntries(chat));
      return entries;
    };

    const labels = new Map(CHAT_BUCKETS.map((bucket) => [bucket.id, bucket.label]));
    const sections = [];
    let total = 0;
    let shown = 0;
    for (const bucket of frozenOrder) {
      const chats = bucket.ids.map((id) => byId.get(id)).filter(Boolean);
      if (!chats.length) continue;
      const bucketRows = [];
      for (const entry of chats.flatMap(chatEntries)) {
        if (entry.type === "turn") {
          if (shown < limit) bucketRows.push(turnHead(entry.turn, entry.count, entry.depth));
          continue;
        }
        total += 1;
        if (shown >= limit) continue;
        bucketRows.push(
          row(entry.node, related, relations, {
            depth: entry.depth,
            parent: entry.parent,
            turn: entry.turn,
            children: entry.children,
            expandable: entry.expandable,
            folded: folded.has(entry.node.id),
          }),
        );
        shown += 1;
      }
      if (bucketRows.length) sections.push(group(labels.get(bucket.id) ?? bucket.id, bucketRows));
    }

    if (!sections.length) return emptyPanel();
    return panel(
      ...[orderNotice(), ...sections, footer(total, shown, Math.max(0, total - shown), untimedHidden())].filter(
        (child) => child != null,
      ),
    );
  }

  /** The Project, Files and Work tabs: honest rows grouped by state word. */
  function renderSimplePanel() {
    const entries = visibleNodes().sort(byTime(state.order));
    if (!entries.length) return emptyPanel();
    const limit = Math.max(ROW_LIMIT, Number(depth.get(keyOf())) || ROW_LIMIT);
    const filtered = filteredScene();
    const relations = relationsOf(filtered);
    const related = new Set(relatedIds(filtered, state.selectedId));
    related.delete(state.selectedId);
    let left = limit;
    let shownCount = 0;
    const groups = new Map();
    for (const node of entries) {
      const word = stateWord(node.state) || (node.kind === "project" || node.kind === "collection" ? "structure" : "no state");
      if (!groups.has(word)) groups.set(word, []);
      groups.get(word).push(node);
    }
    const sections = [];
    for (const [word, nodes] of [...groups.entries()].sort((a, b) => cmp(a[0], b[0]))) {
      if (left <= 0) break;
      const shown = nodes.slice(0, left);
      left -= shown.length;
      shownCount += shown.length;
      sections.push(group(word, shown.map((node) => row(node, related, relations))));
    }
    return panel(...sections, footer(entries.length, shownCount, Math.max(0, entries.length - shownCount), untimedHidden()));
  }

  function renderLayersPanel() {
    if (state.tab === "chats") return renderChatsPanel();
    return renderSimplePanel();
  }

  /** The Timeline: real moments, grouped by semantic level, unknown time apart. */
  function renderTimelinePanel() {
    const filtered = filteredScene();
    const entries = nodesOf(filtered).sort(byTime(state.order));
    const timed = entries.filter((node) => timeOf(node) !== null);
    const untimed = entries.filter((node) => timeOf(node) === null);
    const relations = relationsOf(filtered);
    const related = new Set(relatedIds(filtered, state.selectedId));
    related.delete(state.selectedId);
    const limit = Math.max(ROW_LIMIT, Number(depth.get(keyOf())) || ROW_LIMIT);
    let left = limit;
    let shownCount = 0;
    const sections = [];
    for (const level of LEVEL_ORDER) {
      const nodes = timed.filter((node) => levelOfNode(node) === level);
      if (!nodes.length || left <= 0) continue;
      const shown = nodes.slice(0, left);
      left -= shown.length;
      shownCount += shown.length;
      sections.push(group(LEVEL_LABEL[level], shown.map((node) => row(node, related, relations))));
    }
    if (untimed.length && left > 0) {
      const shown = untimed.slice(0, left);
      left -= shown.length;
      shownCount += shown.length;
      sections.push(group("Unknown time", shown.map((node) => row(node, related, relations))));
    }
    return panel(...sections, footer(entries.length, shownCount, Math.max(0, entries.length - shownCount), 0));
  }

  function renderTabs() {
    const totals = tabTotals();
    const tabs = offeredTabs();
    return h(
      "div.gridProjection__tabs",
      { role: "tablist", "aria-label": "Projections" },
      ...tabs.map((tab) =>
        h(
          "button.gridProjection__tab",
          {
            type: "button",
            role: "tab",
            id: `${id}-tab-${tab.id}`,
            "aria-selected": state.tab === tab.id ? "true" : "false",
            "data-tab": tab.id,
            "data-count": String(totals[tab.id] ?? 0),
            tabindex: state.tab === tab.id ? "0" : "-1",
            title: `${tab.label}: ${totals[tab.id] ?? 0}`,
            on: {
              click: () => {
                if (state.tab === tab.id) return;
                state.tab = tab.id;
                render();
              },
            },
          },
          h("span.gridProjection__tabLabel", { text: tab.label }),
          h("span.gridProjection__tabCount", { text: String(totals[tab.id] ?? 0) }),
        ),
      ),
    );
  }

  function renderActions() {
    const modes = MODES.map((mode) =>
      h("button.gridProjection__action", {
        type: "button",
        "data-mode": mode.id,
        "aria-pressed": state.mode === mode.id ? "true" : "false",
        text: mode.label,
        on: {
          click: () => {
            if (state.mode === mode.id) return;
            state.mode = mode.id;
            render();
          },
        },
      }),
    );
    const filters = TIME_FILTERS.map((filter) =>
      h("button.gridProjection__action", {
        type: "button",
        "data-filter": filter.id,
        "aria-pressed": state.timeFilter === filter.id ? "true" : "false",
        text: filter.label,
        on: {
          click: () => {
            if (state.timeFilter === filter.id) return;
            state.timeFilter = filter.id;
            frozenOrder = null;
            render();
          },
        },
      }),
    );
    const order = h("button.gridProjection__action", {
      type: "button",
      "data-order": state.order,
      "aria-pressed": "false",
      text: state.order === "oldest" ? "Oldest first" : "Newest first",
      on: {
        click: () => {
          state.order = state.order === "oldest" ? "newest" : "oldest";
          frozenOrder = null;
          render();
        },
      },
    });
    const anchor = anchorOf();
    return h(
      "div.gridProjection__actions",
      { role: "group", "aria-label": "View" },
      ...modes,
      h("span.gridProjection__divider", { "aria-hidden": "true" }),
      ...filters,
      state.mode === "timeline" ? order : null,
      state.timeFilter !== "all"
        ? h("span.gridProjection__note", {
            "data-anchor-source": anchor.source,
            text:
              anchor.source === "now"
                ? `${state.timeFilter} back from now`
                : anchor.source === "scene"
                  ? `${state.timeFilter} back from the newest item (${formatMoment(state.scene?.timeRange?.to)})`
                  : `${state.timeFilter}: the scene has no moment to count back from`,
          })
        : null,
    );
  }

  function selectionNote() {
    if (!state.selectedId) return null;
    const node = nodesOf(state.scene).find((entry) => entry.id === state.selectedId);
    if (!node) return h("p.gridProjection__note", { text: "The selection is not in this scene." });
    if (!inViewNodes().some((entry) => entry.id === state.selectedId)) {
      return h("p.gridProjection__note", { text: `Selected: ${text(node.title)} (outside this view)` });
    }
    return null;
  }

  /**
   * A render replaces the rows, so the reader's place in them is carried over
   * by hand: the scroll of the frame and of the panel, and the focused control
   * named by its stable attributes (`data-id`, `data-for`, `data-tab`, ...).
   * Focus is restored only when the reader has not already moved it elsewhere,
   * and nothing is focused that the reader did not focus: a fresh answer never
   * takes the keyboard.
   */
  const FOCUS_ATTRS = ["data-for", "data-id", "data-tab", "data-mode", "data-filter", "data-order", "aria-label"];

  function ownerDocumentOf() {
    return root.ownerDocument ?? globalThis.document ?? null;
  }

  function panelElement() {
    return typeof root.querySelector === "function" ? root.querySelector(".gridProjection__panel") : null;
  }

  function captureScroll() {
    const entries = [];
    const add = (element, key) => {
      if (!element) return;
      const top = Number(element.scrollTop) || 0;
      const left = Number(element.scrollLeft) || 0;
      if (top || left) entries.push({ key, top, left });
    };
    add(host, "host");
    add(root, "root");
    add(panelElement(), "panel");
    return entries;
  }

  function restoreScroll(entries) {
    for (const entry of arrayOf(entries)) {
      const element = entry.key === "host" ? host : entry.key === "root" ? root : panelElement();
      if (!element) continue;
      element.scrollTop = entry.top;
      element.scrollLeft = entry.left;
    }
  }

  function captureFocus() {
    const doc = ownerDocumentOf();
    const active = doc?.activeElement;
    if (!active || active === doc?.body || !root.contains?.(active)) return null;
    const attrs = {};
    for (const name of FOCUS_ATTRS) {
      const value = active.getAttribute?.(name);
      if (value != null) attrs[name] = value;
    }
    // The control's own classes keep a button without data attributes (Show
    // more) recognisable across the replace.
    const fromValue = text(active.classList?.value);
    const fromNames = [...(active.classList?.names ?? [])].map(text).join(" ");
    const classes = (fromValue || fromNames).split(/\s+/).filter(Boolean);
    if (!Object.keys(attrs).length && !classes.length) return null;
    return { tag: text(active.tagName), attrs, classes };
  }

  function restoreFocus(saved) {
    if (!saved) return;
    const doc = ownerDocumentOf();
    const active = doc?.activeElement;
    // The reader already focused something else: leave that choice alone.
    if (active && active !== doc?.body && root.contains?.(active)) return;
    const candidates = [];
    const walk = (node) => {
      for (const child of Array.from(node?.childNodes ?? [])) {
        if (typeof child.getAttribute === "function") candidates.push(child);
        walk(child);
      }
    };
    walk(root);
    // The stable attributes (`data-id`, `data-for`, ...) identify the control;
    // classes only stand in for a button that carries none.
    const hasAttrs = Object.keys(saved.attrs).length > 0;
    const match = candidates.find(
      (element) =>
        text(element.tagName) === saved.tag &&
        (hasAttrs
          ? Object.entries(saved.attrs).every(([name, value]) => element.getAttribute?.(name) === value)
          : saved.classes.every((name) => element.classList?.contains?.(name))),
    );
    if (!match) return;
    try {
      match.focus?.({ preventScroll: true });
    } catch {
      match.focus?.();
    }
  }

  function render() {
    if (destroyed) return;
    const offered = offeredTabs();
    if (!offered.some((tab) => tab.id === state.tab)) state.tab = offered[0]?.id ?? "chats";
    const scroll = captureScroll();
    const focus = captureFocus();
    const current = state.mode === "timeline" ? renderTimelinePanel() : renderLayersPanel();
    const note = selectionNote();
    root.setAttribute("data-mode", state.mode);
    root.replaceChildren(...[state.mode === "layers" ? renderTabs() : null, renderActions(), note, current].filter((child) => child != null));
    if (host && root.parentNode !== host) host.replaceChildren(root);
    restoreScroll(scroll);
    restoreFocus(focus);
  }

  function update(patch = {}) {
    if (destroyed) return getState();
    if (isObject(patch?.scene)) {
      state.scene = patch.scene;
      const nextProject = text(patch.scene?.projectId) || null;
      if (nextProject !== projectId) {
        projectId = nextProject;
        depth.clear();
        frozenOrder = null;
      }
    }
    if (patch?.mode === "layers" || patch?.mode === "timeline") state.mode = patch.mode;
    if ("selectedId" in (patch ?? {})) state.selectedId = patch.selectedId == null ? null : text(patch.selectedId);
    if (typeof patch?.onSelect === "function") onSelect = patch.onSelect;
    if ("now" in (patch ?? {})) state.now = msOfOption(patch.now);
    render();
    return getState();
  }

  function getState() {
    const totals = tabTotals();
    const visible = visibleNodes();
    const inView = inViewNodes();
    const filtered = filteredScene();
    const anchor = anchorOf();
    const selectedNode = nodesOf(state.scene).find((node) => node.id === state.selectedId) ?? null;
    return {
      mode: state.mode,
      tab: state.tab,
      timeFilter: state.timeFilter,
      order: state.order,
      selectedId: state.selectedId,
      selectedInView: Boolean(selectedNode) && inView.some((node) => node.id === state.selectedId),
      tabs: offeredTabs().map((tab) => ({ id: tab.id, label: tab.label, count: totals[tab.id] })),
      total: visible.length,
      shown: visible.slice(0, Math.max(ROW_LIMIT, Number(depth.get(keyOf())) || ROW_LIMIT)).length,
      depth: Math.max(ROW_LIMIT, Number(depth.get(keyOf())) || ROW_LIMIT),
      hiddenUntimed: untimedHidden(),
      orderPending: Boolean(pendingOrder && (orderMoved > 0 || orderAdded.length > 0)),
      orderMoved,
      orderAdded: orderAdded.length,
      projectId,
      timeAnchor: anchor.ms,
      timeAnchorSource: anchor.source,
      sceneNodes: nodesOf(state.scene).length,
      sceneEdges: edgesOf(state.scene).length,
      visibleNodes: nodesOf(filtered).length,
      showMore: mapToObject(depth),
    };
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    root.remove();
    if (host) host.replaceChildren();
  }

  render();
  return { update, destroy, getState, applyOrder };
}
