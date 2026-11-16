/**
 * The inspector beside the map (web_chat/grid.html).
 *
 * A click on a card, a 3D node or a Layers row selects it: the views light the
 * same thing, and this panel says what the server really knows about it. There
 * is no context/agent island any more: the hierarchy is the project's own -
 *
 *   Project -> Files / Chats / Work
 *   Chat    -> Messages / Executions
 *   Execution -> Operations
 *   `agent` operation -> child execution -> its operations
 *
 * A chat's bounded detail is read by the page (`GET /api/projects/{id}/map/
 * chats/{chat}`) only when a chat or one of its messages/executions/operations
 * is selected. The panel renders exactly that safe answer: a message preview is
 * the server's sanitized preview, an execution carries agent/status/time and an
 * error *boolean* (never error text), and an operation carries a title, a kind
 * and a status - only a message operation may also show its own summary. No raw
 * body, payload or transcript ever reaches this file.
 *
 * A folder or a file of the project's space keeps the old lazy explorer read;
 * a task keeps its safe fields and its way into the tracker.
 */

import { h } from "../lib/dom.js";
import {
  createExplorer,
  fileErrorText,
  formatBytes,
  parentOf,
  parseFilesUrl,
  safePath,
  splitLines,
} from "./explorer.js";
import { chatHref, taskHref, stateOf, when } from "./render.js";
import { mapChats } from "./layout.js";

/** How many lines of a file the panel shows before the file itself. */
export const PREVIEW_LINES = 12;

/** How many entries of a folder listing the file view repeats. */
const LISTING_PREVIEW = 8;

/** How many file chips one operation may carry before it just counts them. */
const MAX_REFS = 4;

const text = (value) => String(value ?? "");
const arrayOf = (value) => (Array.isArray(value) ? value : []);
const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

const TITLES = {
  project: "Project",
  collection: "Group",
  task: "Task",
  chat: "Chat",
  message: "Message",
  execution: "Execution",
  operation: "Operation",
  file: "File",
  dir: "Folder",
};

/** How an execution's status reads; an unknown word stays exactly as sent. */
const STATUS_WORD = { running: "Running", done: "Done", error: "Failed", idle: "Idle" };
const statusWord = (status) => STATUS_WORD[text(status)] ?? text(status).replace(/_/g, " ");

/** A real ISO moment, or "" - never a made-up one. */
function moment(value) {
  if (!value && value !== 0) return "";
  if (typeof value === "number") {
    const ms = value > 1e12 ? value : value * 1000;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? "" : when(date.toISOString());
  }
  return when(value);
}

/** The key a selection and a card name it by. */
export function selectionKey(selection) {
  const kind = text(selection?.kind);
  if (kind === "task") {
    const id = text(selection.id);
    return id ? `task:${id}` : "";
  }
  if (kind === "chat" || kind === "process") {
    const id = text(selection.id);
    return id ? `process:${id}` : "";
  }
  if (kind === "message") {
    const chatId = text(selection.chatId ?? selection.chat);
    const id = text(selection.id ?? selection.messageId);
    return chatId && id ? `message:${chatId}:${id}` : "";
  }
  if (kind === "execution") {
    const chatId = text(selection.chatId ?? selection.chat);
    const id = text(selection.id ?? selection.executionId);
    return chatId && id ? `execution:${selection.sub === true ? "sub:" : ""}${chatId}:${id}` : "";
  }
  if (kind === "operation") {
    const chatId = text(selection.chatId ?? selection.chat);
    const id = text(selection.id ?? selection.operationId);
    return chatId && id ? `operation:${chatId}:${id}` : "";
  }
  if (kind === "project" || kind === "collection") {
    const id = text(selection.id);
    return id ? `${kind}:${id}` : "";
  }
  if (kind === "file" || kind === "dir") {
    const path = safePath(selection.path);
    return path ? `file:${path}` : "";
  }
  return "";
}

/** A selection, or null when it names nothing this panel may show. */
export function normalizeSelection(selection) {
  const kind = text(selection?.kind);
  if (kind === "task" || kind === "project" || kind === "collection") {
    const id = text(selection?.id).trim();
    return id ? { kind, id, ...(text(selection?.sceneId) ? { sceneId: text(selection.sceneId) } : {}) } : null;
  }
  if (kind === "chat" || kind === "process") {
    const id = text(selection?.id).trim();
    return id ? { kind: "chat", id, ...(text(selection?.sceneId) ? { sceneId: text(selection.sceneId) } : {}) } : null;
  }
  if (kind === "message" || kind === "execution" || kind === "operation") {
    const chatId = text(selection?.chatId ?? selection?.chat).trim();
    const id = text(selection?.id ?? selection?.messageId ?? selection?.executionId ?? selection?.operationId).trim();
    if (!chatId || !id) return null;
    const normalized = { kind, chatId, id };
    if (text(selection?.sceneId)) normalized.sceneId = text(selection.sceneId);
    if (kind === "execution" && selection?.sub === true) normalized.sub = true;
    return normalized;
  }
  if (kind === "file" || kind === "dir") {
    const path = safePath(selection?.path);
    return path ? { kind, path, ...(text(selection?.sceneId) ? { sceneId: text(selection.sceneId) } : {}) } : null;
  }
  return null;
}

/** Every top-level file node, with the path it really names in the space. */
function fileNodesOf(geometry, map) {
  const root = parseFilesUrl(map?.files_url)?.root ?? "";
  const nodes = Array.isArray(geometry?.nodes?.files) ? geometry.nodes.files : [];
  const seen = new Set();
  const out = [];
  for (const node of nodes) {
    const relative = text(node?.item?.path);
    const path = safePath(root ? `${root}/${relative}` : relative);
    if (!path || seen.has(path)) continue;
    seen.add(path);
    out.push({ node, path });
  }
  return out;
}

/** The absolute path a project-relative reference names. */
function absoluteRef(root, ref) {
  const safe = safePath(ref);
  if (!safe) return "";
  return root && !safe.startsWith(`${root}/`) ? `${root}/${safe}` : safe;
}

const selectionChatId = (selection) => {
  const kind = text(selection?.kind);
  if (kind === "chat" || kind === "process") return text(selection.id);
  if (kind === "message" || kind === "execution" || kind === "operation") {
    return text(selection.chatId ?? selection.chat);
  }
  return "";
};

/**
 * What a selection lights up in the legacy DOM map: the chat card, the task
 * cards that really link to it, and the routes between them. The detail kinds
 * (message/execution/operation) light their owning chat; the map does not draw
 * those nodes at all.
 */
export function focusKeys(selection, geometry, map = null) {
  const focus = selectionKey(selection);
  if (!focus || !geometry) return null;
  const related = new Set();
  const edges = new Set();
  const links = new Set();
  const kind = text(selection.kind);
  const chatId = selectionChatId(selection);

  if (kind === "task") {
    for (const edge of geometry.edges ?? []) {
      if (edge.from !== selection.id && edge.to !== selection.id) continue;
      edges.add(`${edge.from}>${edge.to}`);
      related.add(`task:${edge.from === selection.id ? edge.to : edge.from}`);
    }
    for (const link of geometry.links ?? []) {
      if (link.from !== selection.id) continue;
      links.add(`${link.from}>${link.to}`);
      related.add(`process:${link.to}`);
    }
    related.delete(focus);
    return { focus, related, edges, links };
  }

  const owningChat = chatId || (kind === "process" ? text(selection.id) : "");
  if (owningChat) {
    for (const link of geometry.links ?? []) {
      if (link.to !== owningChat) continue;
      links.add(`${link.from}>${link.to}`);
      related.add(`task:${link.from}`);
    }
    related.delete(`process:${owningChat}`);
    return { focus: `process:${owningChat}`, related, edges, links };
  }

  if (kind === "file" || kind === "dir") {
    const path = safePath(selection.path);
    for (const entry of fileNodesOf(geometry, map)) {
      if (entry.path === path || path.startsWith(`${entry.path}/`)) related.add(`file:${entry.path}`);
    }
    related.delete(focus);
    return { focus, related, edges, links };
  }
  return null;
}

/**
 * Put a focus on the drawn map: the selected card is lifted, its relatives are
 * marked, everything else steps back - the geometry never changes.
 */
export function applySelection(canvas, keys) {
  if (!canvas?.querySelectorAll) return canvas;
  const active = Boolean(keys?.focus);
  for (const node of canvas.querySelectorAll(".gridNode")) {
    const key = text(node.dataset?.focus);
    node.classList?.remove?.("is-selected", "is-related", "is-dimmed");
    node.setAttribute?.("aria-current", active && key === keys.focus ? "true" : "false");
    if (!active) continue;
    if (key === keys.focus) node.classList?.add?.("is-selected");
    else if (keys.related?.has(key)) node.classList?.add?.("is-related");
    else node.classList?.add?.("is-dimmed");
  }
  for (const route of canvas.querySelectorAll(".gridMap__edge")) {
    const key = `${route.getAttribute("data-from")}>${route.getAttribute("data-to")}`;
    route.classList?.remove?.("is-lit", "is-dimmed");
    if (active) route.classList?.add?.(keys.edges?.has(key) ? "is-lit" : "is-dimmed");
  }
  for (const route of canvas.querySelectorAll(".gridMap__link")) {
    const key = `${route.getAttribute("data-task")}>${route.getAttribute("data-chat")}`;
    route.classList?.remove?.("is-lit", "is-dimmed");
    if (active) route.classList?.add?.(keys.links?.has(key) ? "is-lit" : "is-dimmed");
  }
  return canvas;
}

const fact = (term, value) =>
  h("div.gridInspector__fact", {}, h("dt.gridInspector__term", { text: term }), h("dd.gridInspector__value", { text: value }));
const note = (words, { off = false } = {}) =>
  h(off ? "p.gridInspector__note.gridInspector__note--off" : "p.gridInspector__note", { text: words });
const openLink = (href, words, extra = {}) => h("a.gridInspector__open.btn", { href, text: words, ...extra });
const section = (title, ...children) =>
  h("section.gridInspector__section", {}, h("h4.gridInspector__heading", { text: title }), ...children);

/**
 * The panel. `setMap` gives it the map answer, `setChatDetail` the safe detail
 * of one selected chat, `select` a node, and `element()` the DOM for whatever
 * it holds; `onUpdate` is called when a lazy read settles.
 */
export class Inspector {
  constructor({ projectId = "", fetchJson = null, onUpdate = null, onSelect = null, onClose = null } = {}) {
    this.projectId = projectId;
    this.fetchJson = fetchJson;
    this.onUpdate = onUpdate;
    this.onSelect = onSelect;
    this.onClose = onClose;
    this.map = null;
    this.geometry = null;
    this.selection = null;
    this.filesUrl = "";
    this.explorer = null;
    this.state = { status: "idle" };
    /** chat id -> safe detail (only the selected chat is present). */
    this.chatDetails = new Map();
    /** chat id -> {status, error}. */
    this.chatDetailStates = new Map();
    this.collapsed = new Set();
    this.uid = 0;
  }

  /** A new map answer: keep what was read; nothing is refetched here. */
  setMap(map, geometry) {
    this.map = map;
    this.geometry = geometry;
    const filesUrl = text(map?.files_url);
    if (filesUrl !== this.filesUrl) {
      this.filesUrl = filesUrl;
      this.explorer = filesUrl ? createExplorerFor(filesUrl, this.fetchJson) : null;
    }
    return this;
  }

  /**
   * Remember one chat's safe detail, or clear every detail when `chatId` is "".
   * No request is made here: the page owns the lazy read.
   */
  setChatDetail(chatId, state) {
    const id = text(chatId);
    if (!id) {
      this.chatDetails.clear();
      this.chatDetailStates.clear();
      return this;
    }
    if (!state) {
      this.chatDetails.delete(id);
      this.chatDetailStates.delete(id);
    } else if (state.status === "ready" && isObject(state.detail)) {
      this.chatDetails.set(id, state.detail);
      this.chatDetailStates.delete(id);
    } else {
      this.chatDetailStates.set(id, { status: text(state.status) || "loading", error: text(state.error) });
    }
    return this;
  }

  /** Select a node; a folder or a file is read lazily. */
  select(selection) {
    const next = normalizeSelection(selection);
    if (selectionKey(next) !== selectionKey(this.selection)) this.collapsed.clear();
    this.selection = next;
    if (!next) {
      this.state = { status: "idle" };
      this.onUpdate?.();
      return Promise.resolve();
    }
    return this.load();
  }

  clear() {
    this.selection = null;
    this.state = { status: "idle" };
    this.onUpdate?.();
  }

  get tasks() {
    return Array.isArray(this.map?.tasks?.items) ? this.map.tasks.items : [];
  }

  /** The map's chat records: the canonical `chats` section of the answer. */
  get chats() {
    return mapChats(this.map);
  }

  async load() {
    const selection = this.selection;
    if (!selection) return;
    if (selection.kind !== "file" && selection.kind !== "dir") {
      this.state = { status: "ready" };
      this.onUpdate?.();
      return;
    }
    if (!this.explorer?.supported) {
      this.state = { status: "unavailable" };
      this.onUpdate?.();
      return;
    }
    this.state = { status: "loading" };
    this.onUpdate?.();
    let state;
    try {
      if (selection.kind === "dir") {
        const listing = await this.explorer.tree(selection.path);
        state = listing.ok
          ? { status: "ready", listing }
          : { status: "error", status_code: listing.status, error: listing.error };
      } else {
        const [listing, file] = await Promise.all([
          this.explorer.tree(parentOf(selection.path)),
          this.explorer.text(selection.path),
        ]);
        state = { status: "ready", listing: listing.ok ? listing : null, text: file };
      }
    } catch {
      state = { status: "error", status_code: 0, error: fileErrorText(0) };
    }
    if (this.selection !== selection) return;
    this.state = state;
    this.onUpdate?.();
  }

  /** The panel, or null when nothing is selected. */
  element() {
    const selection = this.selection;
    if (!selection) return null;
    const box = h("section.gridInspector", { "aria-label": "Inspector", dataset: { kind: selection.kind } });
    box.append(
      h(
        "header.gridInspector__head",
        {},
        h("h3.gridInspector__title", { text: TITLES[selection.kind] ?? "Item" }),
        h("button.gridInspector__close", {
          type: "button",
          "aria-label": "Close the inspector",
          title: "Close (Esc)",
          text: "×",
          on: { click: () => this.onClose?.() },
        }),
      ),
    );
    if (selection.kind === "task") box.append(...this.taskParts(selection));
    else if (selection.kind === "chat") box.append(...this.chatParts(selection));
    else if (selection.kind === "message" || selection.kind === "execution" || selection.kind === "operation") {
      box.append(...this.chatParts({ kind: "chat", id: selection.chatId }));
    } else if (selection.kind === "project") box.append(...this.projectParts(selection));
    else if (selection.kind === "collection") box.append(...this.collectionParts(selection));
    else box.append(...this.fileParts(selection));
    return box;
  }

  pick(selection) {
    this.onSelect?.(selection);
  }

  projectParts(selection) {
    const project = this.map?.project ?? {};
    return [
      h("p.gridInspector__name", { text: text(project.title) || text(selection.id) }),
      h(
        "dl.gridInspector__facts",
        {},
        fact("Files", `${arrayOf(this.map?.files?.items).length} top-level entries`),
        fact("Chats", `${this.chats.length} chats`),
        fact("Work", `${arrayOf(this.map?.tasks?.items).length} tasks`),
        fact("Id", text(selection.id)),
      ),
    ];
  }

  collectionParts(selection) {
    const name = text(selection.id).split(":").pop() || "Group";
    return [
      h("p.gridInspector__name", { text: name }),
      note("This group contains the real entities of the project; it is not an entity itself."),
    ];
  }

  taskParts(selection) {
    const task = this.tasks.find((item) => text(item.id) === selection.id);
    if (!task) return [note("This task is no longer on the map.")];
    const state = stateOf(task);
    const parts = [
      h("p.gridInspector__name", { text: text(task.title) || text(task.id) }),
      h(
        "dl.gridInspector__facts",
        {},
        fact("State", `${state.mark} ${state.label}`),
        fact("Priority", task.priority == null ? "—" : `P${task.priority}`),
        fact("Type", text(task.type) || "—"),
        fact("Assignee", text(task.assignee) || "Unassigned"),
        fact("Updated", when(task.updated_at) || "—"),
        fact("Id", text(task.id)),
      ),
      openLink(taskHref(this.projectId, task.id), "Open task"),
    ];
    if (text(task.close_reason)) parts.push(note(`Closed: ${text(task.close_reason)}`));
    return parts;
  }

  /** The chat header and its exact tree: Messages, Executions, Operations. */
  chatParts(selection) {
    const chatId = text(selection.id);
    const chat = this.chats.find((item) => text(item.chat_id) === chatId);
    const detail = this.chatDetails.get(chatId) ?? null;
    const status = this.chatDetailStates.get(chatId) ?? null;
    const title = text(chat?.title) || text(detail?.chat?.title) || chatId;
    const href = text(detail?.chat?.href) || chatHref(chatId);
    const parts = [
      h("p.gridInspector__name", { text: title }),
      h(
        "dl.gridInspector__facts",
        {},
        fact("Status", chat ? (chat.awaiting_approval ? "Awaiting approval" : chat.active ? "Active" : "Idle") : "—"),
        fact("Updated", when(chat?.updated_at) || "—"),
        fact("Chat", chatId),
      ),
      openLink(href, "Open chat"),
    ];
    if (!detail) {
      if (status?.status === "error") parts.push(note(status.error || "The chat detail could not be loaded.", { off: true }));
      else parts.push(note(status?.status === "loading" ? "Loading the chat…" : "Select the chat to read its messages and executions."));
      return parts;
    }
    parts.push(this.chatTree(detail, chatId, selection));
    return parts;
  }

  /**
   * The whole chat tree. Every level is explicit: a message is a message, an
   * execution is a run, an operation belongs to its own `execution_id`, and an
   * `agent` operation opens the child execution that owns its `parent_id`
   * children. Nothing is joined by timing or position.
   */
  chatTree(detail, chatId, selection) {
    const uid = (this.uid = (this.uid ?? 0) + 1);
    const messages = arrayOf(detail?.messages?.items);
    const runs = arrayOf(detail?.executions?.items);
    const operations = arrayOf(detail?.operations?.items);
    const selected = selectionKey(this.selection) || selectionKey(selection);
    const tree = h("div.gridInspector__tree", { role: "tree", "aria-label": `Hierarchy of ${chatId}` });

    const href = text(detail?.chat?.href) || chatHref(chatId);
    const rowOpen = () =>
      h("a.gridInspector__rowOpen", {
        href,
        text: "Open chat",
        rel: "noreferrer",
        on: { click: (event) => event?.stopPropagation?.() },
      });

    /** A collapsible group with a real button and an honest aria-expanded. */
    const group = (label, count, children, collapsedId) => {
      const bodyId = `gridInspector-${uid}-${collapsedId}`;
      const folded = this.collapsed.has(collapsedId);
      const caret = h("span.gridInspector__caret", { "aria-hidden": "true", text: folded ? "▸" : "▾" });
      const toggle = h(
        "button.gridInspector__treeToggle",
        {
          type: "button",
          "aria-expanded": folded ? "false" : "true",
          "aria-controls": bodyId,
          "aria-label": `${folded ? "Expand" : "Collapse"} ${label}`,
          on: {
            click: () => {
              if (this.collapsed.has(collapsedId)) this.collapsed.delete(collapsedId);
              else this.collapsed.add(collapsedId);
              this.onUpdate?.();
            },
          },
        },
        caret,
        h("span.gridInspector__treeLabel", { text: label }),
        h("span.gridInspector__treeCount", { text: String(count) }),
      );
      const body = h("div.gridInspector__treeBody", { id: bodyId, hidden: folded });
      body.append(...children);
      return h("div.gridInspector__treeGroup", {}, toggle, body);
    };

    const row = (kind, id, title, meta, extra = {}) => {
      const key = extra.key ?? "";
      const isSelected = Boolean(key) && key === selected;
      return h(
        "li.gridInspector__treeRow",
        {
          role: "treeitem",
          tabindex: "0",
          "aria-selected": isSelected ? "true" : "false",
          dataset: { kind, id, selected: isSelected ? "true" : "false" },
          on: {
            click: (event) => {
              if (event?.target?.closest?.("a, button")) return;
              this.pick(extra.selection ?? { kind, chatId, id });
              // Trace operation rows may be nested under an agent operation.
              // Do not let the parent row's bubbled click replace the child.
              event?.stopPropagation?.();
            },
            keydown: (event) => {
              if (event?.key !== "Enter" && event?.key !== " ") return;
              event.preventDefault?.();
              this.pick(extra.selection ?? { kind, chatId, id });
              event.stopPropagation?.();
            },
          },
        },
        h("span.gridInspector__treeMark", { "aria-hidden": "true", text: extra.mark ?? "·" }),
        h(
          "div.gridInspector__treeText",
          {},
          h("span.gridInspector__treeTitle", { text: title }),
          meta ? h("span.gridInspector__treeMeta", { text: meta }) : null,
        ),
        rowOpen(),
      );
    };

    // -- Messages -----------------------------------------------------------
    const messageRows = messages.map((item) => {
      const id = text(item?.id);
      const meta = [
        text(item?.role) || "other",
        moment(item?.time),
        item?.kind ? `kind ${text(item.kind)}` : "",
        item?.has_image === true ? "image" : "",
      ].filter(Boolean).join(" · ");
      const list = h("ul.gridInspector__treeList", { role: "group" }, row("message", id, text(item?.preview) || `Message ${id}`, meta, {
        mark: "✎",
        key: `message:${chatId}:${id}`,
        selection: { kind: "message", chatId, id },
      }));
      return list;
    });
    tree.append(
      group(
        "Messages",
        Number(detail?.messages?.total ?? messages.length),
        messageRows.length ? messageRows : [note("This chat has no safe message preview.")],
        `messages:${chatId}`,
      ),
    );

    // -- Executions and their operations -----------------------------------
    const byExecution = new Map();
    for (const operation of operations) {
      const executionId = text(operation?.execution_id);
      if (!executionId) continue;
      if (!byExecution.has(executionId)) byExecution.set(executionId, []);
      byExecution.get(executionId).push(operation);
    }

    const runRows = runs.map((run) => {
      const runId = text(run?.id);
      const ops = byExecution.get(runId) ?? [];
      const opById = new Map(ops.map((operation) => [text(operation?.id), operation]));
      const parentOfOp = new Map();
      for (const operation of ops) {
        const id = text(operation?.id);
        const parent = text(operation?.parent_id);
        parentOfOp.set(id, parent && parent !== id && opById.has(parent) ? parent : null);
      }
      /** The nearest `agent` operation above an id, exactly as the trace says. */
      const agentAncestorOf = (id) => {
        const seen = new Set();
        let current = parentOfOp.get(id) ?? null;
        while (current && !seen.has(current)) {
          seen.add(current);
          const operation = opById.get(current);
          if (!operation) return null;
          if (text(operation?.kind) === "agent") return current;
          current = parentOfOp.get(current) ?? null;
        }
        return null;
      };
      const childrenOf = (id) => ops.filter((operation) => parentOfOp.get(text(operation?.id)) === id);

      const executionMeta = [
        text(run?.agent) && text(run.agent) !== "unknown" ? `agent ${text(run.agent)}` : "agent unknown",
        statusWord(run?.status),
        moment(run?.start_time),
        run?.duration_ms != null ? `${Math.round(Number(run.duration_ms))} ms` : "",
        run?.error === true ? "error" : "",
      ].filter(Boolean).join(" · ");
      const executionKey = `execution:${chatId}:${runId}`;
      const executionRow = row(
        "execution",
        runId,
        text(run?.turn_id) ? `Turn ${text(run.turn_id)}` : text(run?.agent) || "Execution",
        executionMeta,
        {
          mark: "▸",
          key: executionKey,
          selection: { kind: "execution", chatId, id: runId },
        },
      );

      /** One operation, its children, and the child execution of an agent op. */
      const renderOperation = (operation, depth) => {
        const id = text(operation?.id);
        const kind = text(operation?.kind) || "step";
        const meta = [kind, statusWord(operation?.status), operation?.duration_ms != null ? `${Math.round(Number(operation.duration_ms))} ms` : ""]
          .filter(Boolean).join(" · ");
        const item = row("operation", id, text(operation?.title) || id, meta, {
          mark: kind === "agent" ? "◈" : "·",
          key: `operation:${chatId}:${id}`,
          selection: { kind: "operation", chatId, id },
        });
        if (kind === "message" && text(operation?.summary)) {
          item.append(h("p.gridInspector__treeSummary", { text: text(operation.summary) }));
        }
        const refs = this.fileRefs(operation?.files);
        if (refs.length) {
          item.append(
            h(
              "ul.gridInspector__treeFiles",
              { "aria-label": "Referenced files" },
              ...refs.map((ref) =>
                h(
                  "li",
                  {},
                  h(
                    "button.gridFileRef",
                    {
                      type: "button",
                      dataset: { path: ref.path },
                      title: `Open ${ref.ref}`,
                      on: { click: (event) => { event?.stopPropagation?.(); this.pick({ kind: "file", path: ref.path }); } },
                    },
                    h("span.gridFileRef__mark", { "aria-hidden": "true", text: "↗" }),
                    h("span.gridFileRef__label", { text: ref.ref }),
                  ),
                ),
              ),
            ),
          );
        }
        const nested = h("ul.gridInspector__treeList.gridInspector__treeList--child", { role: "group" });
        if (kind === "agent") {
          const childExec = h(
            "li.gridInspector__childExecution",
            { dataset: { kind: "execution", agent: id } },
            h("span.gridInspector__treeMark", { "aria-hidden": "true", text: "▸" }),
            h("span.gridInspector__treeTitle", { text: `Child execution · ${text(operation?.title) || id}` }),
          );
          const childList = h("ul.gridInspector__treeList.gridInspector__treeList--child", { role: "group" });
          for (const child of childrenOf(id)) {
            childList.append(renderOperation(child, depth + 2));
          }
          if (childList.childNodes.length) childExec.append(childList);
          nested.append(childExec);
        } else {
          const nearest = agentAncestorOf(id);
          for (const child of childrenOf(id)) {
            if (agentAncestorOf(text(child?.id)) !== nearest) continue;
            nested.append(renderOperation(child, depth + 1));
          }
        }
        if (nested.childNodes.length) item.append(nested);
        return item;
      };

      const operationList = h("ul.gridInspector__treeList.gridInspector__treeList--child", { role: "group" });
      for (const operation of ops) {
        const id = text(operation?.id);
        if (agentAncestorOf(id) !== null) continue;
        if (parentOfOp.get(id) !== null) continue;
        operationList.append(renderOperation(operation, 0));
      }
      if (!ops.length) operationList.append(note("This execution recorded no operations."));

      const bodyId = `gridInspector-${uid}-run-${runId}`;
      const executionBody = h("div.gridInspector__treeBody", { id: bodyId }, operationList);
      const executionToggle = h(
        "button.gridInspector__treeToggle",
        {
          type: "button",
          "aria-expanded": "true",
          "aria-controls": bodyId,
          "aria-label": `Collapse operations of ${text(run?.turn_id) ? `turn ${text(run.turn_id)}` : runId}`,
          on: {
            click: () => {
              const open = executionToggle.getAttribute("aria-expanded") === "true";
              executionToggle.setAttribute("aria-expanded", open ? "false" : "true");
              executionBody.hidden = open;
            },
          },
        },
        h("span.gridInspector__caret", { "aria-hidden": "true", text: "▾" }),
        h("span.gridInspector__treeLabel", { text: "Operations" }),
        h("span.gridInspector__treeCount", { text: String(ops.length) }),
      );
      return h("li.gridInspector__execution", {}, executionRow, executionToggle, executionBody);
    });

    tree.append(
      group(
        "Executions",
        Number(detail?.executions?.total ?? runs.length),
        runRows.length ? [h("ul.gridInspector__treeList", { role: "group" }, ...runRows)] : [note("This chat has no executions.")],
        `executions:${chatId}`,
      ),
    );

    const totals = [];
    if (detail?.messages?.truncated) totals.push(`${messages.length} of ${detail.messages.total} messages`);
    if (detail?.executions?.truncated) totals.push(`${runs.length} of ${detail.executions.total} executions`);
    if (detail?.operations?.truncated) totals.push(`${operations.length} of ${detail.operations.total} operations`);
    if (totals.length) tree.append(note(`Showing ${totals.join(" · ")}.`));
    return tree;
  }

  /** The safe file references of one operation, as {ref, path}. */
  fileRefs(files) {
    const root = this.explorer?.root ?? parseFilesUrl(this.filesUrl)?.root ?? "";
    const seen = new Set();
    const out = [];
    for (const raw of Array.isArray(files) ? files : []) {
      const safe = safePath(text(raw));
      if (!safe) continue;
      const relative = root && safe.startsWith(`${root}/`) ? safe.slice(root.length + 1) : safe;
      const path = absoluteRef(root, relative);
      if (!relative || !path || seen.has(path)) continue;
      seen.add(path);
      out.push({ ref: relative, path });
      if (out.length >= MAX_REFS) break;
    }
    return out;
  }

  crumbs(path) {
    const parts = path.split("/").filter(Boolean);
    const list = h("ol.gridInspector__crumbs", { "aria-label": "Path" });
    parts.forEach((part, index) => {
      const full = parts.slice(0, index + 1).join("/");
      const last = index === parts.length - 1;
      list.append(
        h(
          "li.gridInspector__crumb",
          {},
          last
            ? h("span.gridInspector__crumbCurrent", { text: part, "aria-current": "page" })
            : h("button.gridInspector__crumbButton", { type: "button", text: part, on: { click: () => this.pick({ kind: "dir", path: full }) } }),
        ),
      );
    });
    return list;
  }

  fileParts(selection) {
    const path = safePath(selection.path);
    if (!path) return [note("That path is not inside this project's space.")];
    const parts = [this.crumbs(path)];
    if (!this.explorer?.supported) {
      parts.push(note("The files of this project are not open to you."));
      return parts;
    }
    parts.push(openLink(this.explorer.href({ path, kind: selection.kind === "dir" ? "dir" : "file" }), "Open in Files"));
    if (this.state.status === "loading") parts.push(note("Loading…"));
    if (this.state.status === "error") {
      parts.push(note(this.state.error || fileErrorText(this.state.status_code), { off: true }));
      return parts;
    }
    if (selection.kind === "dir") parts.push(...this.dirParts(path));
    else parts.push(...this.fileView(path));
    return parts;
  }

  dirParts(path) {
    const listing = this.state.listing;
    if (!listing?.ok) return [note(listing?.error || "The folder could not be read.", { off: true })];
    const entries = listingEntriesOf(listing.data, this.explorer.root);
    if (!entries.length) return [note("This folder is empty.")];
    return [
      section(
        "Entries",
        h("ul.gridInspector__entries", { "aria-label": "Entries of the folder" }, ...entries.map((entry) => this.entryRow(entry))),
        this.state.listing.data?.truncated ? note("The server showed only the start of this folder.") : null,
      ),
    ];
  }

  entryRow(entry) {
    const kind = entry.kind === "dir" ? "dir" : "file";
    return h(
      "li.gridInspector__entry",
      { dataset: { kind: entry.kind, selected: entry.selected ? "true" : "" } },
      h(
        "button.gridInspector__entryButton",
        {
          type: "button",
          dataset: { path: entry.path, kind },
          on: { click: () => this.pick({ kind, path: entry.path }) },
        },
        h("span.gridInspector__entryMark", { "aria-hidden": "true", text: entry.kind === "dir" ? "▸" : "·" }),
        h("span.gridInspector__entryName", { text: entry.name }),
        h("span.gridInspector__entryMeta", {
          text: entry.kind === "dir" ? "Folder" : entry.kind === "link" ? "Link" : formatBytes(entry.size),
        }),
      ),
    );
  }

  fileView(path) {
    const result = this.state.text;
    const data = result?.data ?? null;
    const listing = this.state.listing;
    const entries = listing?.ok ? listingEntriesOf(listing.data, this.explorer.root) : [];
    const entry = entries.find((item) => item.path === path) ?? null;
    const size = data?.size ?? entry?.size ?? null;
    const parts = [
      h(
        "dl.gridInspector__facts",
        {},
        fact("Path", path),
        fact("Size", formatBytes(size) || "—"),
        fact("Kind", entry?.kind === "link" ? "Link" : "File"),
      ),
    ];
    if (entries.length) {
      const parent = parentOf(path);
      parts.push(
        section(
          `In ${parent || path}`,
          h(
            "ul.gridInspector__entries.gridInspector__entries--brief",
            { "aria-label": "The folder this file is in" },
            ...entries.slice(0, LISTING_PREVIEW).map((sibling) => this.entryRow({ ...sibling, selected: sibling.path === path })),
          ),
          entries.length > LISTING_PREVIEW ? note(`Showing ${LISTING_PREVIEW} of ${entries.length} entries.`) : null,
        ),
      );
    }
    parts.push(section("Preview", this.preview(result)));
    return parts;
  }

  preview(result) {
    if (!result) return note("The text was not read.");
    if (!result.ok) return note(result.error || fileErrorText(result.status), { off: true });
    const data = result.data ?? {};
    if (data.binary === true) {
      const size = formatBytes(data.size);
      return note(`This is not a text file: no preview${size ? ` (${size})` : ""}.`);
    }
    if (typeof data.text !== "string") return note("The server sent no text for this file.");
    const all = splitLines(data.text, 0);
    const shown = all.slice(0, PREVIEW_LINES);
    const list = h(
      "ol.gridPreview",
      { "aria-label": "First lines of the file" },
      ...shown.map((line, index) =>
        h(
          "li.gridPreview__line",
          {},
          h("span.gridPreview__number", { "aria-hidden": "true", text: String(index + 1) }),
          h("span.gridPreview__text", { text: line === "" ? " " : line }),
        ),
      ),
    );
    const loaded = all.length;
    const words =
      loaded > shown.length
        ? `Showing ${shown.length} of ${loaded} lines · open the file for the rest`
        : `${loaded} ${loaded === 1 ? "line" : "lines"} loaded`;
    const cut = data.truncated ? " · the server read only the start of a very long file" : "";
    return h("div.gridInspector__preview", {}, list, note(`${words}${cut}`));
  }
}

/** A listing's entries, kept to the space this explorer reads. */
function listingEntriesOf(listing, root) {
  const entries = Array.isArray(listing?.entries) ? listing.entries : [];
  const out = [];
  for (const entry of entries) {
    const path = safePath(text(entry?.path));
    if (!path || (root && path !== root && !path.startsWith(`${root}/`))) continue;
    const kind = ["dir", "file", "link"].includes(text(entry?.kind)) ? text(entry.kind) : "other";
    out.push({
      name: text(entry?.name) || path.slice(path.lastIndexOf("/") + 1),
      path,
      kind,
      size: Number.isFinite(entry?.size) ? entry.size : null,
    });
  }
  return out;
}

/** The explorer for a files_url: the inspector's one way into the space. */
const createExplorerFor = (filesUrl, fetchJson) => createExplorer({ filesUrl, fetchJson });
